"""Google integration: Calendar and Docs through REST and a service account.

`google-api-python-client` is avoided (heavy on RAM): the service account
OAuth token is obtained by signing a JWT with `google-auth`, and the APIs are
called directly with aiohttp.

Each user shares their calendars (and the report document) with the service
account email; there are no per-user tokens to keep.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from urllib.parse import quote

import aiohttp
from google.auth import crypt, jwt

from .calendars import CalendarError

log = logging.getLogger(__name__)

SCOPES = "https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/documents"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_URL = "https://www.googleapis.com/calendar/v3"
DOCS_URL = "https://docs.googleapis.com/v1/documents"
TIMEOUT = aiohttp.ClientTimeout(total=15)


class GoogleClient:
    """Service account token + shared HTTP session."""

    def __init__(self, service_account_file: str):
        with open(service_account_file, encoding="utf-8") as f:
            self._info = json.load(f)
        self.email = self._info["client_email"]
        self._signer = crypt.RSASigner.from_service_account_info(self._info)
        self._token = ""
        self._expiry = 0.0
        self._lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT)
        return self._session

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def token(self) -> str:
        async with self._lock:
            if self._token and time.time() < self._expiry - 60:
                return self._token
            now = int(time.time())
            assertion = jwt.encode(self._signer, {
                "iss": self.email,
                "scope": SCOPES,
                "aud": TOKEN_URL,
                "iat": now,
                "exp": now + 3600,
            })
            session = await self._http()
            async with session.post(TOKEN_URL, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion.decode() if isinstance(assertion, bytes) else assertion,
            }) as r:
                data = await r.json(content_type=None)
                if r.status != 200:
                    raise CalendarError(f"Google token rejected: {data}")
            self._token = data["access_token"]
            self._expiry = now + int(data.get("expires_in", 3600))
            return self._token

    async def request(self, method: str, url: str, body: dict | None = None, retries: int = 2) -> dict:
        error = CalendarError("Google request not attempted")
        for attempt in range(retries + 1):
            try:
                session = await self._http()
                headers = {"Authorization": f"Bearer {await self.token()}"}
                async with session.request(method, url, json=body, headers=headers) as r:
                    data = await r.json(content_type=None) if r.content_length != 0 else {}
                    if r.status < 300:
                        return data or {}
                    message = (data.get("error", {}).get("message", r.reason)
                               if isinstance(data, dict) else r.reason)
                    if r.status == 401:
                        self._token = ""
                    if r.status not in (401, 429) and r.status < 500:
                        raise CalendarError(f"Google {r.status}: {message}")
                    error = CalendarError(f"Google {r.status}: {message}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                error = CalendarError(f"Google unreachable: {e}")
            if attempt < retries:
                await asyncio.sleep(0.5 * (2 ** attempt))
        raise error


class GoogleCalendar:
    name = "google"

    def __init__(self, client: GoogleClient):
        self.client = client

    async def busy(self, calendars: list[str], start: datetime, end: datetime) -> list[tuple[str, str]]:
        data = await self.client.request("POST", f"{CALENDAR_URL}/freeBusy", {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "items": [{"id": c} for c in calendars],
        })
        intervals = []
        for cal_id, info in data.get("calendars", {}).items():
            if info.get("errors"):
                log.warning("freeBusy on %s: %s", cal_id, info["errors"])
            intervals += [(b["start"], b["end"]) for b in info.get("busy", [])]
        return sorted(intervals)

    async def create_event(self, calendar: str, event: dict) -> dict:
        data = await self.client.request("POST", f"{CALENDAR_URL}/calendars/{quote(calendar, safe='')}/events", event)
        return {"id": data.get("id", ""), "link": data.get("htmlLink", ""), "calendar": calendar}

    async def check_access(self, calendar: str, _subscribe: bool = True) -> tuple[bool, str]:
        cal = quote(calendar, safe="")
        try:
            data = await self.client.request("GET", f"{CALENDAR_URL}/users/me/calendarList/{cal}", retries=0)
            role = data.get("accessRole", "")
            if role in ("writer", "owner"):
                return True, f"write access ({role})"
            return False, f"insufficient permission ({role or 'unknown'}): 'Make changes to events' is required"
        except CalendarError as e:
            if not _subscribe:
                return False, f"not accessible: share it with {self.client.email} ({e})"
            # A shared calendar only shows up in calendarList once inserted:
            # try to read it directly and subscribe to it.
            try:
                await self.client.request("GET", f"{CALENDAR_URL}/calendars/{cal}", retries=0)
                await self.client.request("POST", f"{CALENDAR_URL}/users/me/calendarList", {"id": calendar}, retries=0)
                return await self.check_access(calendar, _subscribe=False)
            except CalendarError:
                return False, f"not accessible: share it with {self.client.email} ({e})"


class GoogleDocs:
    def __init__(self, client: GoogleClient):
        self.client = client

    async def append(self, document_id: str, text: str) -> None:
        """Append text at the end of the document (a service account cannot create files)."""
        await self.client.request("POST", f"{DOCS_URL}/{quote(document_id, safe='')}:batchUpdate", {
            "requests": [{"insertText": {"endOfSegmentLocation": {}, "text": text}}],
        })
