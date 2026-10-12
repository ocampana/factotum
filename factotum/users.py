"""factotum users and their profiles (calendars, report document, time zone).

Identity (who the user is) is separate from authentication (how they logged
in): a user can be local (username and password) or come from Home
Assistant, in which case it is created automatically on first access.
Profiles are stored in `<data>/users.json` with atomic writes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt${n}${r}${p}${s}${d}".format(
        s=base64.b64encode(salt).decode(), d=base64.b64encode(digest).decode(), **_SCRYPT
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = encoded.split("$")
        if scheme != "scrypt":
            return False
        computed = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), dklen=32,
                                  n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(computed, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def _split_name(full_name: str) -> tuple[str, str]:
    """Split a display name into first and last name (first word / rest)."""
    first, _, last = full_name.strip().partition(" ")
    return first, last.strip()


@dataclass
class CalendarRef:
    id: str
    label: str = ""
    backend: str = "google"


@dataclass
class User:
    id: str
    username: str
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    origin: str = "local"  # "local" | "homeassistant"
    admin: bool = False
    password_hash: str = ""
    ha_user_id: str = ""
    calendars: list[CalendarRef] = field(default_factory=list)
    report_doc_id: str = ""
    timezone: str = "Europe/Rome"

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or self.username

    def public(self) -> dict:
        data = asdict(self)
        data.pop("password_hash")
        data["name"] = self.name
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "User":
        data = dict(data)
        data["calendars"] = [CalendarRef(**c) for c in data.get("calendars", [])]
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class UserStore:
    def __init__(self, path: Path, default_timezone: str = "Europe/Rome"):
        self.path = path
        self.default_timezone = default_timezone
        self._users: dict[str, User] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self._users = {u["id"]: User.from_dict(u) for u in data.get("users", [])}
        log.info("Loaded %d users from %s", len(self._users), self.path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps({"users": [asdict(u) for u in self._users.values()]},
                             ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".users-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------ lookup

    def all(self) -> list[User]:
        return sorted(self._users.values(), key=lambda u: u.name.lower())

    def get(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    def by_username(self, username: str) -> User | None:
        username = username.strip().lower()
        return next((u for u in self._users.values() if u.username.lower() == username), None)

    def by_ha_id(self, ha_user_id: str) -> User | None:
        return next((u for u in self._users.values() if u.ha_user_id == ha_user_id), None)

    # ------------------------------------------------------------------ changes

    def _new_id(self) -> str:
        return secrets.token_hex(6)

    def create_local(self, username: str, password: str, name: str = "", admin: bool = False) -> User:
        username = username.strip()
        if not username or not password:
            raise ValueError("username and password are required")
        if self.by_username(username):
            raise ValueError(f"user {username!r} already exists")
        first, last = _split_name(name)
        user = User(id=self._new_id(), username=username, first_name=first, last_name=last,
                    admin=admin, password_hash=hash_password(password),
                    timezone=self.default_timezone)
        self._users[user.id] = user
        self.save()
        return user

    def from_home_assistant(self, ha_user_id: str, username: str, name: str) -> User:
        """Return the user linked to the Home Assistant user, creating it if needed."""
        user = self.by_ha_id(ha_user_id)
        if user:
            if name and not (user.first_name or user.last_name):
                user.first_name, user.last_name = _split_name(name)
                self.save()
            return user
        base = username or f"ha-{ha_user_id[:8]}"
        free_username = base if not self.by_username(base) else f"{base}-ha"
        first, last = _split_name(name)
        user = User(id=self._new_id(), username=free_username, first_name=first, last_name=last,
                    origin="homeassistant", ha_user_id=ha_user_id,
                    timezone=self.default_timezone)
        self._users[user.id] = user
        self.save()
        log.info("Created user %s linked to Home Assistant user %s", user.username, ha_user_id)
        return user

    def update_profile(self, user: User, data: dict) -> User:
        if "timezone" in data and str(data["timezone"]).strip():
            tz = str(data["timezone"]).strip()
            try:
                ZoneInfo(tz)
            except (ZoneInfoNotFoundError, ValueError) as e:
                raise ValueError(f"unknown time zone: {tz}") from e
            user.timezone = tz
        if "email" in data:
            email = str(data["email"]).strip()
            if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                raise ValueError(f"invalid email: {email}")
            user.email = email
        if "phone" in data:
            phone = str(data["phone"]).strip()
            if phone and not re.fullmatch(r"\+?[\d\s\-./()]{5,20}", phone):
                raise ValueError(f"invalid phone number: {phone}")
            user.phone = phone
        for key in ("first_name", "last_name"):
            if key in data:
                setattr(user, key, str(data[key]).strip())
        if "calendars" in data:
            calendars = []
            for c in data["calendars"]:
                cal_id = str(c.get("id", "")).strip()
                if cal_id:
                    calendars.append(CalendarRef(id=cal_id, label=str(c.get("label", "")).strip() or cal_id,
                                                 backend=str(c.get("backend", "google"))))
            user.calendars = calendars
        if "report_doc_id" in data:
            user.report_doc_id = str(data["report_doc_id"]).strip()
        if data.get("password") and user.origin == "local":
            user.password_hash = hash_password(str(data["password"]))
        self.save()
        return user

    def delete(self, user_id: str) -> None:
        if self._users.pop(user_id, None):
            self.save()

    def ensure_admin(self, username: str, password: str) -> None:
        """Create the initial administrator if no local admin exists."""
        if any(u.admin and u.origin == "local" for u in self._users.values()):
            return
        if not password:
            log.warning("No administrator: set FACTOTUM_ADMIN_PASSWORD to create one")
            return
        self.create_local(username, password, admin=True)
        log.info("Created the initial administrator %r", username)
