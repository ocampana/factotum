# factotum

Ultra-light AI voice agent that places outbound phone calls over SIP and talks
with the callee using the Mistral AI stack:

- **Speech-to-text**: Voxtral (`voxtral-mini-latest`)
- **LLM**: `mistral-small-latest`, with tool calling for calendar and call control
- **Text-to-speech**: Voxtral TTS (`voxtral-mini-tts-2603`, streaming PCM)

It runs in an Alpine-based Docker image (~100 MB) and targets less than 150 MB of RAM.

## Features

- Registers on **any SIP server** as a client (UDP/TCP/TLS, outbound proxy, STUN).
- Web UI to queue calls: number, time window, goal, conversation language and target calendars.
  The UI is in Italian when the browser prefers Italian, in English otherwise.
- **Conversation language from the country code**: Italian for +39, German for +49/+43/+41,
  French for +33, and so on (English fallback). It can be forced per call.
- **Multi-user**: local users, Home Assistant users (Ingress) and an API token for integrations.
  Each user has their own calls, transcripts, calendars and contact details.
- **Appointments**: the agent checks availability and books events in the calendars the user
  selected, including who it spoke with, the call goal, notes and the last turns of the call.
- If asked during a booking, the agent provides the user's **email and phone** from their profile.
- Optional **report** appended to a Google Doc after each call.
- **Full transcript** of every call (`.jsonl` + `.txt`) under `data/transcripts/<user>/`.
- Robustness: SIP re-registration, retries on busy/no answer/dropped calls within the window,
  timeouts and retries on every Mistral request, pre-synthesized fallback phrases, clean shutdown.

The call list lives in RAM by design; user profiles and transcripts are persisted under `data/`.

## Architecture

```
            ┌──────────────── factotum (one Python process, asyncio) ────────────────┐
 SIP/RTP    │  pjsua2 (C)            audio.py           conversation.py               │
 server ◄──►│  REGISTER/INVITE  ──►  VAD, framing  ──►  STT ─► LLM (+tools) ─► TTS ─┐ │
            │  G.711/G.722, jitter   resampling    ◄──────────── playback buffer ◄──┘ │
            │  buffer, conf bridge                                                     │
            │  scheduler.py (time windows, retries)   web.py (UI + REST API)           │
            │  calendars.py / google_workspace.py     users.py / auth.py               │
            └──────────────────────────────────────────────────────────────────────────┘
```

pjsip runs in its own thread; all library calls go through a command queue,
and audio frames (16 kHz, 20 ms) are exchanged with an `AudioMediaPort`
attached to the conference bridge, which also handles G.711 transcoding.

## Quick start (Docker)

```sh
cp .env.example .env        # fill in MISTRAL_API_KEY, SIP_*, FACTOTUM_ADMIN_PASSWORD, FACTOTUM_SECRET
docker compose up -d --build
```

Open `http://<host>:8080`, sign in with the admin credentials and add a call.

The compose file uses `network_mode: host`: SIP/RTP behind Docker NAT is the
classic cause of one-way audio. If you cannot use host networking, publish
UDP 5060 and the RTP range (10000-10099 by default) and set `SIP_STUN` if needed.

### Configuration

Environment variables (see `.env.example`) override `config/sip.toml`.

| Variable | Meaning |
|---|---|
| `MISTRAL_API_KEY` | Mistral API key |
| `SIP_SERVER`, `SIP_USER`, `SIP_PASSWORD` | SIP registrar and credentials |
| `SIP_TRANSPORT`, `SIP_PORT`, `SIP_DOMAIN`, `SIP_OUTBOUND_PROXY`, `SIP_STUN` | optional SIP settings |
| `SIP_CODECS` | codec preference, default `PCMA,PCMU` (also `G722`) |
| `SIP_PLUS_REPLACEMENT` | how to dial international numbers: `+` (default) or e.g. `00` |
| `FACTOTUM_ADMIN_USER`, `FACTOTUM_ADMIN_PASSWORD` | initial administrator |
| `FACTOTUM_SECRET` | key used to sign session cookies |
| `FACTOTUM_AUTH` | `local`, `ha_ingress`, `api_token` (comma separated) |
| `FACTOTUM_DEFAULT_COUNTRY_CODE` | country code for numbers without `+`/`00` (default `39`) |
| `FACTOTUM_SUMMARY_LANGUAGE` | language of summaries, events and reports (default `English`) |
| `MISTRAL_TTS_VOICES` | voice per language, e.g. `it:<voice_id>,de:<voice_id>` |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | enables Google Calendar and Docs |

`config/system_prompt.txt` contains the agent instructions (placeholders such as
`{goal}`, `{language}`, `{user_name}`, `{user_email}`). Mount your own `config/`
to customize it.

### TTS voices

At startup factotum lists the voices available to your Mistral account and picks,
for each language, a voice declaring it (custom voices first). Preset voices may
not cover every language: create a voice for Italian or German with the Mistral
voices API and map it with `MISTRAL_TTS_VOICES`.

## Google Calendar and Docs

factotum uses a single **service account**, so there are no per-user OAuth tokens:

1. In Google Cloud, create a service account, enable the Calendar and Docs APIs and
   download its JSON key; set `GOOGLE_SERVICE_ACCOUNT_FILE`.
2. Each user shares their calendar(s) with the service account email
   ("Make changes to events") and enters the calendar IDs in their profile.
   The **Check access** button verifies the sharing.
3. For reports, each user creates a Google Doc, shares it with the service account
   as Editor and enters its ID in the profile. factotum appends one section per call
   (a service account cannot own files in a personal Drive).

## Home Assistant (planned)

The user model is already prepared:

- `ha_ingress`: when factotum runs as an add-on behind Ingress, Home Assistant users
  are recognized from the `X-Remote-User-*` headers, accepted only from the Supervisor
  proxy (`FACTOTUM_HA_TRUSTED_PROXIES`, default `172.30.32.2`), and created on first access.
- `api_token`: a custom integration can call the REST API with `Authorization: Bearer <token>`
  and `X-Factotum-User: <HA user id>` to queue calls from automations or voice assistants.
- The UI only uses relative URLs, so it works under the Ingress path prefix.
- Calendar backends sit behind an interface, so a backend using Home Assistant
  `calendar.*` entities can be added later.

## REST API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/me` | profile, SIP status, supported languages |
| `PUT` | `/api/me` | update profile (names, email, phone, time zone, calendars, report doc) |
| `GET` | `/api/me/calendars/check` | verify calendar access |
| `GET` / `POST` | `/api/calls` | list / queue calls |
| `DELETE` | `/api/calls/{id}` | cancel, hang up or remove a call |
| `GET` | `/api/calls/{id}/transcript` | transcript entries |
| `POST` | `/api/calls/{id}/retry-events` | retry appointments that failed to save |
| `GET` / `POST` / `DELETE` | `/api/users[/{id}]` | user administration (admins) |

Mutating endpoints require `Content-Type: application/json`.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

The unit tests do not need pjsua2. Running the full application requires the
pjsua2 bindings, which are built in the Docker image (`Dockerfile`, build stage).
