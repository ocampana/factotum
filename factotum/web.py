"""Web interface and REST API.

All page URLs are relative, so factotum also works under a prefix (e.g.
behind the Home Assistant Ingress). APIs that change data only accept JSON:
together with the SameSite=Strict cookie this protects against CSRF.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aiohttp import web

from .auth import SESSION_COOKIE, SESSION_DURATION_S
from .languages import LANGUAGES, language_for_number, normalize_number
from .scheduler import IN_PROGRESS, PENDING, Call
from .transcripts import read_transcript

if TYPE_CHECKING:
    from app import Application

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
PUBLIC_PATHS = {"/login", "/healthz", "/i18n.js"}
APP_KEY = web.AppKey("factotum", object)
# aiohttp >= 3.12 recommends typed request keys; plain strings work everywhere
USER_KEY = web.RequestKey("user", object) if hasattr(web, "RequestKey") else "user"
PROVIDER_KEY = web.RequestKey("provider", str) if hasattr(web, "RequestKey") else "provider"


def _app(request: web.Request) -> "Application":
    return request.app[APP_KEY]


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path in PUBLIC_PATHS:
        return await handler(request)
    result = _app(request).auth.resolve(request.headers, request.remote, request.cookies)
    if result.user is None:
        if request.path.startswith("/api/"):
            return _error(result.http_status, result.error)
        raise web.HTTPFound("login")
    if request.method in ("POST", "PUT", "PATCH") and request.path.startswith("/api/") \
            and request.content_type != "application/json":
        return _error(415, "Content-Type: application/json is required")
    request[USER_KEY] = result.user
    request[PROVIDER_KEY] = result.provider
    return await handler(request)


def _admin_only(request: web.Request) -> None:
    if not request[USER_KEY].admin:
        raise web.HTTPForbidden(text="administrators only")


# ---------------------------------------------------------------------- pages


async def index_page(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


async def i18n_script(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "i18n.js")


async def login_page(request: web.Request) -> web.StreamResponse:
    if request.method == "GET":
        return web.FileResponse(STATIC / "login.html")
    data = await request.post()
    user = _app(request).auth.login(str(data.get("username", "")), str(data.get("password", "")))
    if not user:
        log.warning("Failed login for %r from %s", data.get("username"), request.remote)
        raise web.HTTPFound("login?error=1")
    response = web.HTTPFound("./")
    response.set_cookie(SESSION_COOKIE, _app(request).auth.create_session(user), max_age=SESSION_DURATION_S,
                        httponly=True, samesite="Strict", secure=request.secure, path="/")
    raise response


async def logout(request: web.Request) -> web.Response:
    response = web.json_response({"ok": True})
    response.del_cookie(SESSION_COOKIE, path="/")
    return response


async def healthz(request: web.Request) -> web.Response:
    sip = _app(request).sip
    return web.json_response({"ok": True, "sip_registered": bool(sip and sip.registered)})


# ---------------------------------------------------------------------- profile


async def me(request: web.Request) -> web.Response:
    app = _app(request)
    return web.json_response({
        "user": request[USER_KEY].public(),
        "provider": request[PROVIDER_KEY],
        "service_account": app.google.email if app.google else "",
        "google_enabled": app.google is not None,
        "languages": sorted(LANGUAGES),
        "sip": {
            "registered": bool(app.sip and app.sip.registered),
            "status": app.sip.registration_status if app.sip else "not started",
        },
    })


async def update_me(request: web.Request) -> web.Response:
    try:
        user = _app(request).users.update_profile(request[USER_KEY], await request.json())
    except ValueError as e:
        return _error(400, str(e))
    return web.json_response({"user": user.public()})


async def check_calendars(request: web.Request) -> web.Response:
    app = _app(request)
    results = []
    for cal in request[USER_KEY].calendars:
        backend = app.calendars.get(cal.backend)
        if backend is None:
            results.append({"id": cal.id, "ok": False, "message": f"backend {cal.backend} not configured"})
            continue
        ok, message = await backend.check_access(cal.id)
        results.append({"id": cal.id, "ok": ok, "message": message})
    return web.json_response({"calendars": results})


# ---------------------------------------------------------------------- calls


def _parse_local_time(value: str, tz: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo(tz))


async def list_calls(request: web.Request) -> web.Response:
    calls = _app(request).scheduler.list(request[USER_KEY].id)
    return web.json_response({"calls": [c.public() for c in calls]})


async def create_call(request: web.Request) -> web.Response:
    app = _app(request)
    user = request[USER_KEY]
    data = await request.json()
    try:
        number = normalize_number(str(data.get("number", "")), app.cfg.calls.default_country_code)
        requested_language = str(data.get("language") or "auto")
        if requested_language != "auto" and requested_language not in LANGUAGES:
            raise ValueError(f"unsupported language: {requested_language}")
        goal = str(data.get("goal", "")).strip()
        if not goal:
            raise ValueError("the call goal is required")
        own = {c.id for c in user.calendars}
        calendars = [str(c) for c in data.get("calendars", [])]
        foreign = set(calendars) - own
        if foreign:
            raise ValueError(f"calendars not in your profile: {', '.join(sorted(foreign))}")
        call = Call(
            user_id=user.id,
            number=number,
            language=language_for_number(number) if requested_language == "auto" else requested_language,
            auto_language=requested_language == "auto",
            goal=goal,
            window_start=_parse_local_time(str(data["window_start"]), user.timezone),
            window_end=_parse_local_time(str(data["window_end"]), user.timezone),
            calendars=calendars,
            report=bool(data.get("report")),
        )
        app.scheduler.add(call)
    except KeyError as e:
        return _error(400, f"missing field: {e}")
    except ValueError as e:
        return _error(400, str(e))
    return web.json_response({"call": call.public()}, status=201)


async def delete_call(request: web.Request) -> web.Response:
    """Cancel (if pending), hang up (if in progress) or remove from the list."""
    app = _app(request)
    call = app.scheduler.get(request.match_info["id"], request[USER_KEY].id)
    if not call:
        return _error(404, "call not found")
    if call.state in (IN_PROGRESS, PENDING):
        app.scheduler.cancel(call)
    else:
        app.scheduler.remove(call)
    return web.json_response({"call": call.public()})


async def transcript(request: web.Request) -> web.Response:
    call = _app(request).scheduler.get(request.match_info["id"], request[USER_KEY].id)
    if not call:
        return _error(404, "call not found")
    entries = read_transcript(Path(call.transcript)) if call.transcript else []
    return web.json_response({"entries": entries})


async def retry_events(request: web.Request) -> web.Response:
    app = _app(request)
    call = app.scheduler.get(request.match_info["id"], request[USER_KEY].id)
    if not call:
        return _error(404, "call not found")
    created = await app.retry_events(call)
    return web.json_response({"created": created, "call": call.public()})


# ---------------------------------------------------------------------- user administration


async def list_users(request: web.Request) -> web.Response:
    _admin_only(request)
    return web.json_response({"users": [u.public() for u in _app(request).users.all()]})


async def create_user(request: web.Request) -> web.Response:
    _admin_only(request)
    data = await request.json()
    try:
        user = _app(request).users.create_local(
            str(data.get("username", "")), str(data.get("password", "")),
            f"{data.get('first_name', '')} {data.get('last_name', '')}".strip(), bool(data.get("admin")))
    except ValueError as e:
        return _error(400, str(e))
    return web.json_response({"user": user.public()}, status=201)


async def delete_user(request: web.Request) -> web.Response:
    _admin_only(request)
    if request.match_info["id"] == request[USER_KEY].id:
        return _error(400, "you cannot delete yourself")
    _app(request).users.delete(request.match_info["id"])
    return web.json_response({"ok": True})


def create_web_app(application: "Application") -> web.Application:
    app = web.Application(middlewares=[auth_middleware], client_max_size=64 * 1024)
    app[APP_KEY] = application
    app.add_routes([
        web.get("/", index_page),
        web.get("/i18n.js", i18n_script),
        web.get("/login", login_page),
        web.post("/login", login_page),
        web.get("/healthz", healthz),
        web.post("/api/logout", logout),
        web.get("/api/me", me),
        web.put("/api/me", update_me),
        web.get("/api/me/calendars/check", check_calendars),
        web.get("/api/calls", list_calls),
        web.post("/api/calls", create_call),
        web.delete("/api/calls/{id}", delete_call),
        web.get("/api/calls/{id}/transcript", transcript),
        web.post("/api/calls/{id}/retry-events", retry_events),
        web.get("/api/users", list_users),
        web.post("/api/users", create_user),
        web.delete("/api/users/{id}", delete_user),
    ])
    return app
