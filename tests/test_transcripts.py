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
