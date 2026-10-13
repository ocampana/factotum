from factotum.transcripts import Transcript, read_transcript


def test_transcript_on_disk(tmp_path):
    t = Transcript(tmp_path, "abc", {"Number": "+39044412345"})
    t.agent("Good morning, I'm Anna's assistant.")
    t.callee("Good morning, go ahead.")
    t.tool("check_availability", {"start": "x"}, {"free": True})
    entries = read_transcript(t.jsonl_path)
    assert [e["role"] for e in entries] == ["system", "agent", "callee", "tool"]
    text = t.txt_path.read_text()
    assert "Number: +39044412345" in text and "CALLEE: Good morning, go ahead." in text
    assert t.excerpt().splitlines()[-1] == "Callee: Good morning, go ahead."


def test_cost_section_at_the_end(tmp_path):
    from factotum.costs import CostTracker, Pricing

    t = Transcript(tmp_path, "abc", {"Number": "+39044412345"})
    t.callee("Hello?")
    tracker = CostTracker(Pricing(), "mistral-small-latest", "voxtral-mini-latest", "voxtral-mini-tts-2603")
    tracker.add_llm(1000, 100)
    t.costs(tracker.summary(), tracker.render())
    lines = t.txt_path.read_text().splitlines()
    assert lines[-6].startswith("COSTS (estimate, USD)")
    assert "TOTAL" in lines[-2] and lines[-1].strip().startswith("Prices:")
    last = read_transcript(t.jsonl_path)[-1]
    assert last["role"] == "costs" and last["costs"]["llm"]["input_tokens"] == 1000
