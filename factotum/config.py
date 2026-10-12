"""Configuration loading: TOML file with defaults + environment variables.

Environment variables always take precedence, so the same Docker image is
configured with a `.env` file in development and with `docker run -e` in
production.
"""

from __future__ import annotations

import logging
import os
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """Read a `.env` file (development only) without overriding the environment."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass
class SipConfig:
    server: str
    user: str
    password: str
    port: int = 5060
    transport: str = "udp"
    domain: str = ""
    outbound_proxy: str = ""
    local_port: int = 5060
    codecs: list[str] = field(default_factory=lambda: ["PCMA", "PCMU"])
    register_timeout_s: int = 300
    register_retry_s: int = 30
    rtp_port_start: int = 10000
    rtp_port_range: int = 100
    stun: str = ""
    plus_replacement: str = "+"
    user_agent: str = "factotum"

    @property
    def sip_domain(self) -> str:
        return self.domain or self.server


@dataclass
class CallsConfig:
    invite_timeout_s: int = 45
    max_duration_s: int = 600
    max_silence_s: int = 15
    max_attempts: int = 3
    retry_after_s: int = 600
    default_country_code: str = "39"
    summary_language: str = "English"


@dataclass
class VadConfig:
    end_of_turn_ms: int = 700
    min_speech_ms: int = 250
    max_segment_ms: int = 30000
    min_threshold: int = 300
    noise_factor: float = 3.0
    barge_in_factor: float = 2.0


@dataclass
class MistralConfig:
    api_key: str
    llm_model: str = "mistral-small-latest"
    stt_model: str = "voxtral-mini-latest"
    tts_model: str = "voxtral-mini-tts-2603"
    tts_voice: str = ""
    tts_voices: dict[str, str] = field(default_factory=dict)
    stt_timeout_s: float = 15.0
    llm_timeout_s: float = 10.0
    tts_timeout_s: float = 15.0
    retries: int = 2


@dataclass
class Config:
    sip: SipConfig
    calls: CallsConfig
    vad: VadConfig
    mistral: MistralConfig
    data_dir: Path
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    auth_providers: list[str] = field(default_factory=lambda: ["local"])
    admin_user: str = "admin"
    admin_password: str = ""
    secret: str = ""
    api_token: str = ""
    ha_trusted_proxies: list[str] = field(default_factory=lambda: ["172.30.32.2"])
    google_sa_file: str = ""
    timezone: str = "Europe/Rome"
    system_prompt: str = ""


def _pick(section: dict, cls) -> dict:
    return {k: section[k] for k in cls.__dataclass_fields__ if k in section}


def load_config(path: Path | None = None) -> Config:
    """Read `config/sip.toml` (if present) and apply environment variables."""
    path = path or Path(_env("FACTOTUM_CONFIG", str(ROOT / "config" / "sip.toml")))
    toml: dict = {}
    if path.exists():
        with path.open("rb") as f:
            toml = tomllib.load(f)
    else:
        log.warning("Configuration file %s not found, using defaults", path)

    s = toml.get("sip", {})
    sip = SipConfig(
        server=_env("SIP_SERVER", s.get("server", "")),
        user=_env("SIP_USER", s.get("user", "")),
        password=_env("SIP_PASSWORD", ""),
        port=int(_env("SIP_PORT", s.get("port", 5060))),
        transport=_env("SIP_TRANSPORT", s.get("transport", "udp")).lower(),
        domain=_env("SIP_DOMAIN", s.get("domain", "")),
        outbound_proxy=_env("SIP_OUTBOUND_PROXY", s.get("outbound_proxy", "")),
        local_port=int(_env("SIP_LOCAL_PORT", s.get("local_port", 5060))),
        codecs=_split(_env("SIP_CODECS")) or s.get("codecs", ["PCMA", "PCMU"]),
        register_timeout_s=int(s.get("register_timeout_s", 300)),
        register_retry_s=int(s.get("register_retry_s", 30)),
        rtp_port_start=int(_env("SIP_RTP_PORT_START", s.get("rtp_port_start", 10000))),
        rtp_port_range=int(s.get("rtp_port_range", 100)),
        stun=_env("SIP_STUN", s.get("stun", "")),
        plus_replacement=_env("SIP_PLUS_REPLACEMENT", s.get("plus_replacement", "+")),
        user_agent=s.get("user_agent", "factotum"),
    )

    calls = CallsConfig(**_pick(toml.get("calls", {}), CallsConfig))
    calls.default_country_code = _env("FACTOTUM_DEFAULT_COUNTRY_CODE", str(calls.default_country_code)).lstrip("+")
    calls.summary_language = _env("FACTOTUM_SUMMARY_LANGUAGE", calls.summary_language)

    vad = VadConfig(**_pick(toml.get("vad", {}), VadConfig))

    voices = {}
    for pair in _split(_env("MISTRAL_TTS_VOICES")):
        if ":" in pair:
            language, voice = pair.split(":", 1)
            voices[language.strip().lower()] = voice.strip()
    mistral = MistralConfig(
        api_key=_env("MISTRAL_API_KEY", ""),
        llm_model=_env("MISTRAL_LLM_MODEL", "mistral-small-latest"),
        stt_model=_env("MISTRAL_STT_MODEL", "voxtral-mini-latest"),
        tts_model=_env("MISTRAL_TTS_MODEL", "voxtral-mini-tts-2603"),
        tts_voice=_env("MISTRAL_TTS_VOICE", ""),
        tts_voices=voices,
    )

    secret = _env("FACTOTUM_SECRET", "")
    if not secret:
        log.warning("FACTOTUM_SECRET not set: web sessions will expire at every restart")
        secret = secrets.token_hex(32)

    prompt_file = Path(_env("FACTOTUM_PROMPT_FILE", str(ROOT / "config" / "system_prompt.txt")))
    prompt = prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else ""

    return Config(
        sip=sip,
        calls=calls,
        vad=vad,
        mistral=mistral,
        data_dir=Path(_env("FACTOTUM_DATA_DIR", str(ROOT / "data"))),
        web_host=_env("FACTOTUM_WEB_HOST", "0.0.0.0"),
        web_port=int(_env("FACTOTUM_WEB_PORT", "8080")),
        auth_providers=_split(_env("FACTOTUM_AUTH", "local")),
        admin_user=_env("FACTOTUM_ADMIN_USER", "admin"),
        admin_password=_env("FACTOTUM_ADMIN_PASSWORD", ""),
        secret=secret,
        api_token=_env("FACTOTUM_API_TOKEN", ""),
        ha_trusted_proxies=_split(_env("FACTOTUM_HA_TRUSTED_PROXIES", "172.30.32.2")),
        google_sa_file=_env("GOOGLE_SERVICE_ACCOUNT_FILE", ""),
        timezone=_env("TZ", "Europe/Rome"),
        system_prompt=prompt,
    )


def validate_config(cfg: Config) -> list[str]:
    """Return the list of blocking configuration problems."""
    problems = []
    if not cfg.mistral.api_key:
        problems.append("MISTRAL_API_KEY is missing")
    if not cfg.sip.server:
        problems.append("SIP_SERVER is missing")
    if not cfg.sip.user:
        problems.append("SIP_USER is missing")
    if cfg.sip.transport not in ("udp", "tcp", "tls"):
        problems.append(f"invalid SIP_TRANSPORT: {cfg.sip.transport}")
    if "api_token" in cfg.auth_providers and not cfg.api_token:
        problems.append("FACTOTUM_AUTH includes api_token but FACTOTUM_API_TOKEN is empty")
    return problems
