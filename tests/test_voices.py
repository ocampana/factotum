from types import SimpleNamespace

from factotum.mistral_client import choose_voices, language_code


def _voice(id_, name, languages, type_="preset"):
    return SimpleNamespace(id=id_, name=name, languages=languages, type=type_)


def test_language_code():
    assert language_code("it_it") == "it"
    assert language_code("en-GB") == "en"


def test_prefers_neutral_presets_and_custom_voices():
    voices = [
        _voice("paul-sad", "Paul - Sad", ["en_us"]),
        _voice("paul-neutral", "Paul - Neutral", ["en_us"]),
        _voice("marie-sad", "Marie - Sad", ["fr_fr"]),
        _voice("marie-neutral", "Marie - Neutral", ["fr_fr"]),
        _voice("giulia", "Giulia", ["it_it"], type_="custom"),
        _voice("my-french", "Clone FR", ["fr_fr"], type_="custom"),
    ]
    per_language, fallback = choose_voices(voices)
    assert per_language == {"en": "paul-neutral", "fr": "my-french", "it": "giulia"}
    assert fallback == "paul-neutral"


def test_no_voices():
    assert choose_voices([]) == ({}, "")
