"""API tests with a fake application (no SIP, no Mistral)."""

import asyncio
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from factotum.auth import Authenticator
from factotum.config import CallsConfig
from factotum.scheduler import Scheduler
from factotum.users import UserStore
from factotum.web import create_web_app


def _fake_app(tmp_path):
    users = UserStore(tmp_path / "users.json")
    users.create_local("anna", "secret", "Anna", admin=True)
    users.create_local("bob", "secret", "Bob")

    async def run(_):
        pass

    return SimpleNamespace(
        users=users,
        auth=Authenticator(users, ["local"], "key"),
        cfg=SimpleNamespace(calls=CallsConfig()),
        scheduler=Scheduler(CallsConfig(), run, ready=lambda: False),
        sip=None, google=None, calendars={},
    )


async def _login(client, username):
    r = await client.post("/login", data={"username": username, "password": "secret"}, allow_redirects=False)
    assert r.status == 302


def test_calls_api(tmp_path):
    async def scenario():
        app = _fake_app(tmp_path)
        async with TestClient(TestServer(create_web_app(app))) as client:
            assert (await client.get("/api/calls")).status == 401
            await _login(client, "anna")
            r = await client.post("/api/calls", json={
                "number": "+49 30 1234567", "window_start": "2099-01-01T09:00",
                "window_end": "2099-01-01T12:00", "goal": "Book a table", "language": "auto",
            })
            assert r.status == 201, await r.text()
            call = (await r.json())["call"]
            assert call["language"] == "de" and call["auto_language"] is True
            # Mutating APIs reject non-JSON bodies (CSRF protection)
            assert (await client.post("/api/calls", data="x")).status == 415
            # Calendars must belong to the user's profile
            r = await client.post("/api/calls", json={
                "number": "0444123456", "window_start": "2099-01-01T09:00",
                "window_end": "2099-01-01T12:00", "goal": "x", "calendars": ["other@example.com"],
            })
            assert r.status == 400
            client.session.cookie_jar.clear()
            await _login(client, "bob")
            assert (await (await client.get("/api/calls")).json())["calls"] == []
            assert (await client.get("/api/users")).status == 403

    asyncio.run(scenario())
