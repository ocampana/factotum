"""Provider-based authentication, independent of the user model.

Available providers (FACTOTUM_AUTH, comma separated):
- `local`: username and password login, HMAC-signed session cookie.
- `ha_ingress`: factotum behind the Home Assistant Ingress. HA adds the
  `X-Remote-User-*` headers; they are trusted ONLY when the request comes
  from a trusted proxy (by default the Supervisor ingress IP, 172.30.32.2).
- `api_token`: bearer token for the HA integration or other services; the
  user on whose behalf the request is made goes in `X-Factotum-User`
  (HA user id, username or factotum id).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import time
from dataclasses import dataclass
from typing import Mapping

from .users import User, UserStore, verify_password

SESSION_COOKIE = "factotum_session"
SESSION_DURATION_S = 7 * 24 * 3600


@dataclass
class AuthResult:
    user: User | None = None
    provider: str = ""
    error: str = ""
    http_status: int = 401


class Authenticator:
    def __init__(self, store: UserStore, providers: list[str], secret: str,
                 api_token: str = "", trusted_proxies: list[str] | None = None):
        self.store = store
        self.providers = providers
        self._key = hashlib.sha256(secret.encode()).digest()
        self._api_token = api_token
        self._trusted_proxies = [ipaddress.ip_network(p, strict=False) for p in (trusted_proxies or [])]

    # ------------------------------------------------------------------ local sessions

    def create_session(self, user: User, now: float | None = None) -> str:
        expiry = int((time.time() if now is None else now) + SESSION_DURATION_S)
        payload = f"{user.id}|{expiry}".encode()
        signature = hmac.new(self._key, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload).decode() + "." + base64.urlsafe_b64encode(signature).decode()

    def read_session(self, value: str, now: float | None = None) -> User | None:
        try:
            payload_part, signature_part = value.split(".", 1)
            payload = base64.urlsafe_b64decode(payload_part)
            signature = base64.urlsafe_b64decode(signature_part)
        except (ValueError, TypeError):
            return None
        expected = hmac.new(self._key, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            user_id, expiry = payload.decode().split("|")
            if int(expiry) < (time.time() if now is None else now):
                return None
        except ValueError:
            return None
        return self.store.get(user_id)

    def login(self, username: str, password: str) -> User | None:
        if "local" not in self.providers:
            return None
        user = self.store.by_username(username)
        if user and user.origin == "local" and verify_password(password, user.password_hash):
            return user
        return None

    # ------------------------------------------------------------------ request resolution

    def _from_trusted_proxy(self, remote: str | None) -> bool:
        if not remote:
            return False
        try:
            address = ipaddress.ip_address(remote)
        except ValueError:
            return False
        return any(address in network for network in self._trusted_proxies)

    def _user_from_reference(self, reference: str, name: str = "") -> User | None:
        return (self.store.by_ha_id(reference)
                or self.store.by_username(reference)
                or self.store.get(reference)
                or (self.store.from_home_assistant(reference, "", name) if name else None))

    def resolve(self, headers: Mapping[str, str], remote: str | None, cookies: Mapping[str, str]) -> AuthResult:
        """Determine the user of an HTTP request by trying the enabled providers."""
        authorization = headers.get("Authorization", "")
        if authorization.startswith("Bearer ") and "api_token" in self.providers and self._api_token:
            token = authorization[7:].strip()
            if not hmac.compare_digest(token.encode(), self._api_token.encode()):
                return AuthResult(error="invalid token")
            reference = headers.get("X-Factotum-User", "").strip()
            if not reference:
                return AuthResult(error="missing X-Factotum-User header", http_status=400)
            user = self._user_from_reference(reference, headers.get("X-Factotum-User-Name", "").strip())
            if not user:
                return AuthResult(error=f"unknown user {reference!r}", http_status=403)
            return AuthResult(user=user, provider="api_token")

        if "ha_ingress" in self.providers and headers.get("X-Remote-User-Id"):
            if not self._from_trusted_proxy(remote):
                return AuthResult(error="Home Assistant headers from an untrusted address", http_status=403)
            user = self.store.from_home_assistant(
                headers["X-Remote-User-Id"],
                headers.get("X-Remote-User-Name", ""),
                headers.get("X-Remote-User-Display-Name", ""),
            )
            return AuthResult(user=user, provider="ha_ingress")

        if "local" in self.providers and cookies.get(SESSION_COOKIE):
            user = self.read_session(cookies[SESSION_COOKIE])
            if user:
                return AuthResult(user=user, provider="local")

        return AuthResult(error="authentication required")
