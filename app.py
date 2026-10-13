"""factotum — ultra-light AI voice agent for outbound calls over SIP.

Entry point: loads the configuration, starts the SIP stack (pjsua2), the call
scheduler and the web interface, and coordinates each phone call
(conversation, calendar, report, transcript).
Clean shutdown on SIGTERM/SIGINT: hang up, unregister and close everything.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web

from factotum.auth import Authenticator
from factotum.calendars import CalendarBackend, CalendarError
from factotum.config import Config, load_config, load_dotenv, validate_config
from factotum.conversation import Conversation
from factotum.google_workspace import GoogleCalendar, GoogleClient, GoogleDocs
from factotum.languages import LANGUAGES
from factotum.mistral_client import MistralServices
from factotum.scheduler import Call, Scheduler
from factotum.sip import SipEngine
from factotum.transcripts import Transcript
from factotum.users import User, UserStore
from factotum.web import create_web_app

log = logging.getLogger("factotum")


class Application:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.users = UserStore(cfg.data_dir / "users.json", cfg.timezone)
        self.users.ensure_admin(cfg.admin_user, cfg.admin_password)
        self.auth = Authenticator(self.users, cfg.auth_providers, cfg.secret,
                                  cfg.api_token, cfg.ha_trusted_proxies)
        self.services = MistralServices(cfg.mistral)

        self.google: GoogleClient | None = None
        self.calendars: dict[str, CalendarBackend] = {}
        self.docs: GoogleDocs | None = None
        if cfg.google_sa_file:
            try:
                self.google = GoogleClient(cfg.google_sa_file)
                self.calendars["google"] = GoogleCalendar(self.google)
                self.docs = GoogleDocs(self.google)
                log.info("Google enabled with service account %s", self.google.email)
            except (OSError, ValueError, KeyError) as e:
                log.error("Google service account unusable (%s): calendar and reports disabled", e)

        self.sip: SipEngine | None = None
        self.scheduler = Scheduler(cfg.calls, self.run_call,
                                   ready=lambda: self.sip is not None and self.sip.registered)
        self._runner: web.AppRunner | None = None
        self._scheduler_task: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self.sip = SipEngine(self.cfg.sip, asyncio.get_running_loop())
        await self.sip.start()
        await self.services.load_voices()

        self._runner = web.AppRunner(create_web_app(self), access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.cfg.web_host, self.cfg.web_port).start()
        log.info("Web interface on http://%s:%d", self.cfg.web_host, self.cfg.web_port)

        self._scheduler_task = asyncio.create_task(self.scheduler.run())

    async def stop(self) -> None:
        log.info("Shutting down...")
        await self.scheduler.stop()
        if self._scheduler_task:
            self._scheduler_task.cancel()
        if self._background:
            await asyncio.wait(self._background, timeout=10)
        if self.sip:
            await self.sip.stop()
        if self._runner:
            await self._runner.cleanup()
        if self.google:
            await self.google.close()
        log.info("factotum stopped")

    def _in_background(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ------------------------------------------------------------------ single call

    async def run_call(self, call: Call) -> None:
        user = self.users.get(call.user_id)
        if user is None:
            call.error = "user no longer exists"
            return
        transcript = Transcript(
            self.cfg.data_dir / "transcripts" / user.id,
            f"{call.id}-a{call.attempts}",
            {
                "Call": call.id,
                "Attempt": call.attempts,
                "User": f"{user.name} ({user.username})",
                "Number": call.number,
                "Language": f"{LANGUAGES.get(call.language, call.language)}"
                            f"{' (from country code)' if call.auto_language else ''}",
                "Goal": call.goal,
            },
        )
        call.transcript = str(transcript.jsonl_path)
        calendar = self.calendars.get("google") if call.calendars else None
        conversation = Conversation(
            call, user, self.sip, self.services, calendar, transcript,
            self.cfg.calls, self.cfg.vad, self.cfg.system_prompt, self.cfg.pricing,
        )
        try:
            await conversation.run()
        finally:
            if call.report and call.answered:
                self._in_background(self._write_report(call, user, transcript))

    async def _write_report(self, call: Call, user: User, transcript: Transcript) -> None:
        if not (self.docs and user.report_doc_id):
            transcript.system("Report requested but no Google document configured in the profile")
            return
        now = datetime.now(ZoneInfo(user.timezone))
        lines = [
            "",
            f"=== Call of {now:%Y-%m-%d %H:%M} – {call.number} ===",
            f"Goal: {call.goal}",
            f"Outcome: {call.outcome or 'n/a'}",
            f"Spoke with: {call.contact or 'not given'}",
            f"Language: {LANGUAGES.get(call.language, call.language)}",
        ]
        if call.summary:
            lines.append(f"Summary: {call.summary}")
        for event in call.events:
            lines.append(f"Calendar appointment: {event.get('link') or event.get('id')}")
        if call.costs:
            lines.append(f"Estimated API cost: {call.costs['total']:.4f} {call.costs['currency']}")
        lines += ["", "Transcript:"]
        lines += [f"{e['ts'][11:19]} {e['role']}: {e['text']}" for e in transcript.dialogue()]
        try:
            await self.docs.append(user.report_doc_id, "\n".join(lines) + "\n")
            transcript.system("Report appended to the Google document")
        except CalendarError as e:
            log.warning("Google Docs report not written: %s", e)
            transcript.system(f"Report not written: {e}")

    async def retry_events(self, call: Call) -> int:
        """Retry creating the appointments that failed during the call."""
        backend = self.calendars.get("google")
        if backend is None:
            return 0
        remaining, created = [], 0
        for item in call.failed_events:
            try:
                call.events.append(await backend.create_event(item["calendar"], item["event"]))
                created += 1
            except CalendarError as e:
                item["error"] = str(e)
                remaining.append(item)
        call.failed_events = remaining
        return created


async def main() -> int:
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env")
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    cfg = load_config()
    problems = validate_config(cfg)
    if problems:
        for problem in problems:
            log.error("Configuration: %s", problem)
        return 2

    application = Application(cfg)
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)

    try:
        await application.start()
    except Exception:  # noqa: BLE001
        log.exception("Startup failed")
        await application.stop()
        return 1
    await shutdown.wait()
    await application.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
