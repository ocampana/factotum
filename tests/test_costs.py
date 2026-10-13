import pytest

from factotum.costs import CostTracker, Pricing


def test_cost_computation():
    tracker = CostTracker(Pricing(llm_input_per_mtok=0.15, llm_output_per_mtok=0.60,
                                  stt_per_minute=0.003, tts_per_mchar=16.0))
    tracker.add_llm(10_000, 2_000)       # 0.0015 + 0.0012
    tracker.add_llm(5_000, 1_000)        # 0.00075 + 0.0006
    tracker.add_stt(90)                  # 1.5 min * 0.003 = 0.0045
    tracker.add_tts(500)                 # 500 * 16 / 1e6 = 0.008
    assert tracker.llm_cost == pytest.approx(0.00405)
    assert tracker.stt_cost == pytest.approx(0.0045)
    assert tracker.tts_cost == pytest.approx(0.008)
    assert tracker.total == pytest.approx(0.01655)
    summary = tracker.summary()
    assert summary["llm"]["requests"] == 2 and summary["llm"]["input_tokens"] == 15_000
    assert summary["stt"]["audio_seconds"] == 90 and summary["tts"]["characters"] == 500
    assert summary["total"] == pytest.approx(0.01655)


def test_render_mentions_every_service():
    tracker = CostTracker(Pricing(currency="EUR"), "mistral-small-latest", "voxtral-mini-latest", "voxtral-mini-tts-2603")
    tracker.notes.append("example note")
    text = tracker.render()
    for expected in ("COSTS (estimate, EUR)", "LLM", "STT", "TTS", "TOTAL", "Prices:", "Note: example note"):
        assert expected in text


def test_pricing_from_config(tmp_path):
    from factotum.config import load_config

    path = tmp_path / "sip.toml"
    path.write_text('[pricing]\ncurrency = "EUR"\ntts_per_mchar = 20.0\n')
    pricing = load_config(path).pricing
    assert pricing.currency == "EUR" and pricing.tts_per_mchar == 20.0
    assert pricing.llm_input_per_mtok == 0.15  # default kept
