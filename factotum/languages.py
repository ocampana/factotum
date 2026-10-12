"""Choice of the conversation language from the international dialing code.

When calling an Italian number the agent speaks Italian, when calling a German
number it speaks German, and so on. For countries whose language is not
supported by Voxtral TTS, English is used.
"""

from __future__ import annotations

import re

# Languages supported by Voxtral TTS (and thus usable in a conversation).
# The name is what the LLM is told in the system prompt.
LANGUAGES: dict[str, str] = {
    "it": "Italian",
    "en": "English",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "pt": "Portuguese",
    "nl": "Dutch",
    "hi": "Hindi",
    "ar": "Arabic",
}

FALLBACK_LANGUAGE = "en"

# Country calling code -> main language of the country.
# Multilingual countries use the most widespread language (the language can
# always be forced from the web UI).
COUNTRY_CODES: dict[str, str] = {
    # Italian
    "39": "it", "378": "it", "379": "it",
    # German
    "49": "de", "43": "de", "41": "de", "423": "de",
    # French
    "33": "fr", "32": "fr", "352": "fr", "377": "fr", "225": "fr", "221": "fr",
    "237": "fr", "243": "fr", "509": "fr",
    # Spanish
    "34": "es", "52": "es", "54": "es", "56": "es", "57": "es", "58": "es",
    "51": "es", "53": "es", "591": "es", "593": "es", "595": "es", "598": "es",
    "502": "es", "503": "es", "504": "es", "505": "es", "506": "es", "507": "es",
    # Portuguese
    "351": "pt", "55": "pt", "244": "pt", "258": "pt", "238": "pt",
    # Dutch
    "31": "nl", "597": "nl",
    # English
    "1": "en", "44": "en", "353": "en", "61": "en", "64": "en", "27": "en",
    "356": "en", "65": "en",
    # Hindi
    "91": "hi",
    # Arabic
    "20": "ar", "212": "ar", "213": "ar", "216": "ar", "218": "ar", "961": "ar",
    "962": "ar", "963": "ar", "964": "ar", "965": "ar", "966": "ar", "968": "ar",
    "970": "ar", "971": "ar", "973": "ar", "974": "ar", "967": "ar", "249": "ar",
}

_MAX_CODE_LENGTH = 3


def normalize_number(number: str, default_country_code: str) -> str:
    """Convert a phone number to E.164 (`+<country code><number>`).

    Accepts `+39...`, `0039...` or a national number, which gets
    `default_country_code` prepended.
    """
    cleaned = re.sub(r"[\s\-./()]", "", number.strip())
    if not cleaned:
        raise ValueError("empty number")
    if cleaned.startswith("+"):
        digits = cleaned[1:]
    elif cleaned.startswith("00"):
        digits = cleaned[2:]
    else:
        digits = default_country_code.lstrip("+") + cleaned
    if not digits.isdigit() or not 6 <= len(digits) <= 15:
        raise ValueError(f"invalid number: {number!r}")
    return "+" + digits


def country_code(e164: str) -> str | None:
    """Return the recognized country calling code (longest match)."""
    digits = e164.lstrip("+")
    for length in range(_MAX_CODE_LENGTH, 0, -1):
        if digits[:length] in COUNTRY_CODES:
            return digits[:length]
    return None


def language_for_number(e164: str) -> str:
    """Conversation language for an E.164 number."""
    code = country_code(e164)
    language = COUNTRY_CODES.get(code, FALLBACK_LANGUAGE) if code else FALLBACK_LANGUAGE
    return language if language in LANGUAGES else FALLBACK_LANGUAGE


def dial_string(e164: str, plus_replacement: str) -> str:
    """Number to send to the SIP server (e.g. `+39...` -> `0039...`)."""
    return plus_replacement + e164[1:] if e164.startswith("+") else e164


# Service phrases, synthesized once per language and used when the APIs do not
# respond, so that the callee is never left in silence.
PHRASES: dict[str, dict[str, str]] = {
    "repeat": {
        "it": "Mi scusi, può ripetere?",
        "en": "Sorry, could you say that again?",
        "fr": "Excusez-moi, pouvez-vous répéter ?",
        "de": "Entschuldigung, können Sie das bitte wiederholen?",
        "es": "Perdone, ¿puede repetir?",
        "pt": "Desculpe, pode repetir?",
        "nl": "Sorry, kunt u dat herhalen?",
        "hi": "माफ़ कीजिए, क्या आप दोहरा सकते हैं?",
        "ar": "عذرًا، هل يمكنك التكرار؟",
    },
    "prompt": {
        "it": "Pronto? Mi sente?",
        "en": "Hello? Can you hear me?",
        "fr": "Allô ? Vous m'entendez ?",
        "de": "Hallo? Hören Sie mich?",
        "es": "¿Hola? ¿Me oye?",
        "pt": "Está? Consegue ouvir-me?",
        "nl": "Hallo? Hoort u mij?",
        "hi": "हैलो? क्या आप मुझे सुन पा रहे हैं?",
        "ar": "مرحبًا؟ هل تسمعني؟",
    },
    "technical_problem": {
        "it": "Mi scusi, ho un problema tecnico. Richiamerò più tardi. Arrivederci.",
        "en": "I'm sorry, I'm having a technical problem. I'll call back later. Goodbye.",
        "fr": "Excusez-moi, j'ai un problème technique. Je rappellerai plus tard. Au revoir.",
        "de": "Entschuldigung, ich habe ein technisches Problem. Ich rufe später zurück. Auf Wiederhören.",
        "es": "Disculpe, tengo un problema técnico. Volveré a llamar más tarde. Adiós.",
        "pt": "Desculpe, estou com um problema técnico. Volto a ligar mais tarde. Adeus.",
        "nl": "Excuses, ik heb een technisch probleem. Ik bel later terug. Tot ziens.",
        "hi": "माफ़ कीजिए, मुझे एक तकनीकी समस्या है। मैं बाद में फिर से कॉल करूँगा। नमस्ते।",
        "ar": "عذرًا، لدي مشكلة تقنية. سأعاود الاتصال لاحقًا. مع السلامة.",
    },
    "time_up": {
        "it": "Purtroppo devo chiudere la chiamata. Grazie e arrivederci.",
        "en": "Unfortunately I have to end the call now. Thank you and goodbye.",
        "fr": "Malheureusement, je dois raccrocher. Merci et au revoir.",
        "de": "Leider muss ich das Gespräch jetzt beenden. Vielen Dank und auf Wiederhören.",
        "es": "Lamentablemente tengo que terminar la llamada. Gracias y adiós.",
        "pt": "Infelizmente tenho de terminar a chamada. Obrigado e adeus.",
        "nl": "Helaas moet ik het gesprek nu beëindigen. Bedankt en tot ziens.",
        "hi": "दुर्भाग्य से मुझे अब कॉल समाप्त करनी होगी। धन्यवाद, नमस्ते।",
        "ar": "للأسف يجب أن أنهي المكالمة الآن. شكرًا ومع السلامة.",
    },
}


def phrase(key: str, language: str) -> str:
    variants = PHRASES[key]
    return variants.get(language) or variants[FALLBACK_LANGUAGE]
