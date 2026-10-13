"""Per-call cost estimate of the Mistral APIs (LLM, STT, TTS).

Usage comes from the API responses when available (tokens, audio seconds);
TTS is billed per character of the input text. Prices live in the `[pricing]`
section of `config/sip.toml`, so they can be updated when Mistral changes them.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _requests(n: int) -> str:
    return f"{n} request" if n == 1 else f"{n} requests"


@dataclass
class Pricing:
    currency: str = "USD"
    llm_input_per_mtok: float = 0.15    # mistral-small: per 1M input tokens
    llm_output_per_mtok: float = 0.60   # mistral-small: per 1M output tokens
    stt_per_minute: float = 0.003       # Voxtral Mini Transcribe: per audio minute
    tts_per_mchar: float = 16.0         # Voxtral TTS: per 1M characters


@dataclass
class CostTracker:
    pricing: Pricing
    llm_model: str = ""
    stt_model: str = ""
    tts_model: str = ""
    llm_requests: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    stt_requests: int = 0
    stt_seconds: float = 0.0
    tts_requests: int = 0
    tts_characters: int = 0
    notes: list[str] = field(default_factory=list)

    def add_llm(self, input_tokens: int, output_tokens: int) -> None:
        self.llm_requests += 1
        self.llm_input_tokens += input_tokens or 0
        self.llm_output_tokens += output_tokens or 0

    def add_stt(self, seconds: float) -> None:
        self.stt_requests += 1
        self.stt_seconds += seconds or 0.0

    def add_tts(self, characters: int) -> None:
        self.tts_requests += 1
        self.tts_characters += characters

    @property
    def llm_cost(self) -> float:
        p = self.pricing
        return (self.llm_input_tokens * p.llm_input_per_mtok + self.llm_output_tokens * p.llm_output_per_mtok) / 1e6

    @property
    def stt_cost(self) -> float:
        return self.stt_seconds / 60 * self.pricing.stt_per_minute

    @property
    def tts_cost(self) -> float:
        return self.tts_characters * self.pricing.tts_per_mchar / 1e6

    @property
    def total(self) -> float:
        return self.llm_cost + self.stt_cost + self.tts_cost

    def summary(self) -> dict:
        return {
            "currency": self.pricing.currency,
            "llm": {"model": self.llm_model, "requests": self.llm_requests,
                    "input_tokens": self.llm_input_tokens, "output_tokens": self.llm_output_tokens,
                    "cost": round(self.llm_cost, 6)},
            "stt": {"model": self.stt_model, "requests": self.stt_requests,
                    "audio_seconds": round(self.stt_seconds, 1), "cost": round(self.stt_cost, 6)},
            "tts": {"model": self.tts_model, "requests": self.tts_requests,
                    "characters": self.tts_characters, "cost": round(self.tts_cost, 6)},
            "total": round(self.total, 6),
        }

    def render(self) -> str:
        """Human-readable cost section for the end of the transcript."""
        c = self.pricing.currency
        p = self.pricing
        rows = [
            ("LLM", self.llm_model,
             f"{_requests(self.llm_requests)}, {self.llm_input_tokens:,} input / {self.llm_output_tokens:,} output tokens",
             self.llm_cost),
            ("STT", self.stt_model, f"{_requests(self.stt_requests)}, {self.stt_seconds:.1f} s of audio", self.stt_cost),
            ("TTS", self.tts_model, f"{_requests(self.tts_requests)}, {self.tts_characters:,} characters", self.tts_cost),
        ]
        lines = [f"COSTS (estimate, {c})"]
        for name, model, usage, cost in rows:
            lines.append(f"  {name}  {model:<24} {usage:<52} {cost:>10.6f} {c}")
        lines.append(f"  {'TOTAL':<82} {self.total:>10.6f} {c}")
        lines.append(f"  Prices: LLM {p.llm_input_per_mtok}/{p.llm_output_per_mtok} {c} per 1M input/output tokens, "
                     f"STT {p.stt_per_minute} {c}/min, TTS {p.tts_per_mchar} {c} per 1M characters")
        lines += [f"  Note: {note}" for note in self.notes]
        return "\n".join(lines)
