import pytest

from factotum.languages import dial_string, language_for_number, normalize_number, phrase


@pytest.mark.parametrize("number,expected", [
    ("+39 0444 123456", "+390444123456"),
    ("0039 333 1234567", "+393331234567"),
    ("333-123.4567", "+393331234567"),
    ("+49 30 1234567", "+49301234567"),
])
def test_normalize(number, expected):
    assert normalize_number(number, "39") == expected


@pytest.mark.parametrize("number", ["", "abc", "+12"])
def test_normalize_invalid(number):
    with pytest.raises(ValueError):
        normalize_number(number, "39")


@pytest.mark.parametrize("e164,language", [
    ("+390444123456", "it"),   # Italy
    ("+49301234567", "de"),    # Germany
    ("+43123456789", "de"),    # Austria
    ("+33123456789", "fr"),    # France
    ("+34912345678", "es"),    # Spain
    ("+351212345678", "pt"),   # Portugal (3-digit code)
    ("+353112345678", "en"),   # Ireland
    ("+12125551234", "en"),    # USA
    ("+31201234567", "nl"),    # Netherlands
    ("+212522123456", "ar"),   # Morocco
    ("+81312345678", "en"),    # Japan: not supported by TTS -> fallback
])
def test_language_from_country_code(e164, language):
    assert language_for_number(e164) == language


def test_dial_string():
    assert dial_string("+390444123456", "00") == "00390444123456"
    assert dial_string("+390444123456", "+") == "+390444123456"


def test_phrase_fallback():
    assert phrase("repeat", "de").startswith("Entschuldigung")
    assert phrase("repeat", "xx") == phrase("repeat", "en")
