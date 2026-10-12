"""Command line tool to list and clone Mistral TTS voices.

Preset voices only cover English and French. For other languages (e.g.
Italian, German) clone a voice from a short recording of a native speaker
(8-15 seconds of clear speech work well; only use recordings you have the
right to use):

    python -m factotum.voices list
    python -m factotum.voices create clip.wav --language it --name Giulia --gender female

A voice created with a language is picked automatically by factotum for
calls in that language; MISTRAL_TTS_VOICES can still force a specific one.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys
from pathlib import Path

from mistralai.client import Mistral

from .config import ROOT, load_dotenv
from .mistral_client import choose_voices

# Language tags in the format used by the voice catalog (e.g. "fr_fr", "en_gb")
LANGUAGE_TAGS = {
    "it": "it_it", "de": "de_de", "fr": "fr_fr", "en": "en_us", "es": "es_es",
    "pt": "pt_pt", "nl": "nl_nl", "hi": "hi_in", "ar": "ar_sa",
}


def _client() -> Mistral:
    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        sys.exit("MISTRAL_API_KEY is not set")
    return Mistral(api_key=api_key)


async def list_voices() -> None:
    response = await _client().audio.voices.list_async(limit=100, timeout_ms=15000)
    for voice in sorted(response.items, key=lambda v: (str(v.type), v.name)):
        print(f"{voice.id}  {voice.type!s:7}  {','.join(voice.languages or []):12}  {voice.name}")
    per_language, fallback = choose_voices(response.items)
    print("\nAutomatic choice per language:")
    for language, voice_id in sorted(per_language.items()):
        print(f"  {language}: {voice_id}")
    print(f"  fallback: {fallback}")


async def create_voice(clip: Path, language: str, name: str, gender: str | None) -> None:
    if not clip.exists():
        sys.exit(f"file not found: {clip}")
    tag = LANGUAGE_TAGS.get(language.lower(), language.lower())
    voice = await _client().audio.voices.create_async(
        name=name,
        languages=[tag],
        gender=gender,
        sample_audio=base64.b64encode(clip.read_bytes()).decode(),
        sample_filename=clip.name,
        timeout_ms=60000,
    )
    print(f"Created voice {voice.name!r} ({','.join(voice.languages or [])})")
    print(f"voice_id: {voice.id}")
    print(f"\nIt will be used automatically for '{language}' calls, or force it with:\n"
          f"  MISTRAL_TTS_VOICES={language}:{voice.id}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m factotum.voices", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list the voices available to the account")
    create = commands.add_parser("create", help="clone a voice from an audio clip")
    create.add_argument("clip", type=Path, help="audio file (wav or mp3), ideally 8-15 s of speech")
    create.add_argument("--language", required=True, help="ISO code, e.g. it, de")
    create.add_argument("--name", required=True, help="voice name, e.g. Giulia")
    create.add_argument("--gender", choices=["female", "male"], help="optional")
    args = parser.parse_args()

    if args.command == "list":
        asyncio.run(list_voices())
    else:
        asyncio.run(create_voice(args.clip, args.language, args.name, args.gender))


if __name__ == "__main__":
    main()
