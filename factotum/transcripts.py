"""Full transcript of every call, written to disk as it happens.

Each call produces two files in `<data>/transcripts/<user>/`:
- `.jsonl`: one event per line (machine-readable)
- `.txt`: human-readable version, handy to open or attach
Files are updated at every utterance, so a crash loses nothing.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

LABELS = {
    "agent": "AGENT",
    "callee": "CALLEE",
    "system": "SYSTEM",
    "tool": "TOOL",
}


class Transcript:
    def __init__(self, directory: Path, call_id: str, header: dict):
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{datetime.now():%Y%m%d-%H%M%S}-{call_id}"
        self.jsonl_path = directory / f"{name}.jsonl"
        self.txt_path = directory / f"{name}.txt"
        self.entries: list[dict] = []
        with self.txt_path.open("w", encoding="utf-8") as f:
            for key, value in header.items():
                f.write(f"{key}: {value}\n")
            f.write("-" * 60 + "\n")
        self._write({"role": "system", "text": "start", "header": header})

    def _write(self, entry: dict) -> None:
        entry = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), **entry}
        self.entries.append(entry)
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if "header" not in entry:
            with self.txt_path.open("a", encoding="utf-8") as f:
                time = entry["ts"][11:19]
                f.write(f"[{time}] {LABELS.get(entry['role'], entry['role'])}: {entry['text']}\n")

    def agent(self, text: str) -> None:
        self._write({"role": "agent", "text": text})

    def callee(self, text: str) -> None:
        self._write({"role": "callee", "text": text})

    def system(self, text: str) -> None:
        self._write({"role": "system", "text": text})

    def tool(self, name: str, arguments: dict, result: dict) -> None:
        self._write({
            "role": "tool",
            "text": f"{name}({json.dumps(arguments, ensure_ascii=False)}) -> {json.dumps(result, ensure_ascii=False)}",
        })

    def costs(self, summary: dict, rendered: str) -> None:
        """Append the cost section: a JSON entry and a final block in the text file."""
        entry = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "role": "costs", "text": f"total {summary['total']:.6f} {summary['currency']}",
                 "costs": summary, "rendered": rendered}
        self.entries.append(entry)
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        with self.txt_path.open("a", encoding="utf-8") as f:
            f.write("-" * 60 + "\n" + rendered + "\n")

    def dialogue(self) -> list[dict]:
        return [e for e in self.entries if e["role"] in ("agent", "callee")]

    def excerpt(self, turns: int = 8) -> str:
        """Last turns of the dialogue, to include in calendar events."""
        return "\n".join(
            f"{LABELS[e['role']].capitalize()}: {e['text']}" for e in self.dialogue()[-turns:]
        )

    def text(self) -> str:
        return self.txt_path.read_text(encoding="utf-8")


def read_transcript(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
