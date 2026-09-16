"""Language detection and every fixed bot message in Gujarati, Hindi and English.

Language codes use BCP-47 style: "gu-IN", "hi-IN", "en-IN".
"""

from __future__ import annotations

import re

GU, HI, EN = "gu-IN", "hi-IN", "en-IN"

LANGUAGE_NAMES = {
    GU: ("Gujarati", "Gujarati"),  # (language, script)
    HI: ("Hindi", "Devanagari"),
    EN: ("English", "Latin"),
}

# Reply buttons: id -> title. WhatsApp allows max 3 buttons, titles max 20 chars.
LANGUAGE_BUTTON_PREFIX = "lang:"
LANGUAGE_BUTTONS = [
    (f"{LANGUAGE_BUTTON_PREFIX}{GU}", "ગુજરાતી"),
    (f"{LANGUAGE_BUTTON_PREFIX}{HI}", "हिंदी"),
    (f"{LANGUAGE_BUTTON_PREFIX}{EN}", "English"),
]

# Words that re-open the language menu at any time.
LANGUAGE_COMMANDS = {"ભાષા", "भाषा", "bhasha", "bhaasha", "bhasa", "language", "lang"}


def detect_script(text: str) -> str | None:
    """Guess the language from the script used.

    Gujarati letters (U+0A80–U+0AFF) -> gu-IN, Devanagari (U+0900–U+097F) -> hi-IN.
    Latin-only text returns None: it may be English *or* Hindi/Gujarati typed in
    English letters, so the caller should fall back to the saved language.
    """
    gujarati = sum(1 for ch in text if "\u0a80" <= ch <= "\u0aff")
    devanagari = sum(1 for ch in text if "\u0900" <= ch <= "\u097f")
    if gujarati == 0 and devanagari == 0:
        return None
    return GU if gujarati >= devanagari else HI


def normalize_language(code: str | None) -> str | None:
    """Map a speech-to-text language code to one we support (or None if unusable)."""
    if not code:
        return None
    base = code.split("-")[0].lower()
    if base == "gu":
        return GU
    if base == "en":
        return EN
    # Close Devanagari languages are usually a Hindi speaker being mis-detected.
    if base in {"hi", "mr", "ne", "mai", "sa", "doi", "ur"}:
        return HI
    return None


def is_language_command(text: str) -> bool:
    cleaned = re.sub(r"[\s\.\!\?।,]+", "", text).lower()
    return cleaned in LANGUAGE_COMMANDS


def language_from_button(button_id: str) -> str | None:
    if not button_id.startswith(LANGUAGE_BUTTON_PREFIX):
        return None
    code = button_id[len(LANGUAGE_BUTTON_PREFIX):]
    return code if code in LANGUAGE_NAMES else None


# ── Fixed messages ──────────────────────────────────────────────────────────
# Hindi uses feminine forms for the bot ("सकती हूँ") and for the farmer ("भेज सकती हैं").

CHOOSE_LANGUAGE = (
    "નમસ્તે! હું કૃષિ સખી છું. 🙏\n"
    "नमस्ते! मैं कृषि सखी हूँ।\n"
    "Namaste! I am Krishi Sakhi.\n\n"
    "તમારી ભાષા પસંદ કરો\n"
    "अपनी भाषा चुनिए\n"
    "Choose your language"
)

MESSAGES: dict[str, dict[str, str]] = {
    "welcome": {
        GU: (
            "નમસ્તે બહેન! 🙏 હું કૃષિ સખી છું.\n"
            "ખેતી, પશુપાલન, હવામાન કે બજાર ભાવ વિશે મને કંઈ પણ પૂછો.\n"
            "તમે લખીને, વોઇસ મેસેજમાં બોલીને કે પાકનો ફોટો મોકલીને પૂછી શકો છો.\n"
            "ભાષા બદલવા માટે \"ભાષા\" લખો."
        ),
        HI: (
            "नमस्ते बहन! 🙏 मैं कृषि सखी हूँ।\n"
            "खेती, पशुपालन, मौसम या मंडी भाव के बारे में मुझसे कुछ भी पूछिए।\n"
            "आप लिखकर, वॉइस मैसेज में बोलकर या फसल की फोटो भेजकर पूछ सकती हैं।\n"
            "भाषा बदलने के लिए \"भाषा\" लिखिए।"
        ),
        EN: (
            "Namaste, sister! 🙏 I am Krishi Sakhi.\n"
            "Ask me anything about farming, animals, weather or market prices.\n"
            "You can type, send a voice message, or send a photo of your crop.\n"
            "To change the language, type \"language\"."
        ),
    },
    "limit_reached": {
        GU: (
            "બહેન, આજે તમે ઘણા પ્રશ્નો પૂછ્યા છે અને આજની મર્યાદા પૂરી થઈ ગઈ છે. "
            "કાલે ફરી પૂછજો. 🙏\n"
            "તાત્કાલિક મદદ માટે કિસાન કોલ સેન્ટર 1800-180-1551 પર ફોન કરો."
        ),
        HI: (
            "बहन, आज आपने बहुत सवाल पूछे हैं और आज की सीमा पूरी हो गई है। "
            "कल फिर से पूछिए। 🙏\n"
            "ज़रूरी मदद के लिए किसान कॉल सेंटर 1800-180-1551 पर फ़ोन कीजिए।"
        ),
        EN: (
            "Sister, you have asked many questions today and today's limit is reached. "
            "Please ask again tomorrow. 🙏\n"
            "For urgent help, call the Kisan Call Centre on 1800-180-1551."
        ),
    },
    "error": {
        GU: "માફ કરજો, અત્યારે થોડી તકલીફ છે. થોડી વાર પછી ફરી મોકલજો. 🙏",
        HI: "माफ़ कीजिए, अभी थोड़ी दिक्कत आ रही है। थोड़ी देर बाद फिर से भेजिए। 🙏",
        EN: "Sorry, something went wrong on my side. Please send it again in a little while. 🙏",
    },
    "unsupported": {
        GU: "માફ કરજો, હું ફક્ત લખેલો મેસેજ, વોઇસ મેસેજ અને ફોટો સમજી શકું છું. 🙏",
        HI: "माफ़ कीजिए, मैं सिर्फ़ लिखा हुआ मैसेज, वॉइस मैसेज और फोटो समझ सकती हूँ। 🙏",
        EN: "Sorry, I can only understand text, voice messages and photos. 🙏",
    },
    "voice_too_long": {
        GU: (
            "બહેન, આ વોઇસ મેસેજ બહુ લાંબો છે. "
            "કૃપા કરીને {limit} સેકન્ડથી ટૂંકો મેસેજ મોકલો, અથવા પ્રશ્ન નાના ભાગમાં પૂછો. 🙏"
        ),
        HI: (
            "बहन, यह वॉइस मैसेज बहुत लंबा है। "
            "कृपया {limit} सेकंड से छोटा मैसेज भेजिए, या सवाल छोटे हिस्सों में पूछिए। 🙏"
        ),
        EN: (
            "Sister, this voice message is too long. "
            "Please send one shorter than {limit} seconds, or ask in smaller parts. 🙏"
        ),
    },
    "voice_unclear": {
        GU: "માફ કરજો, તમારો અવાજ બરાબર સંભળાયો નહીં. ફોન મોં પાસે રાખીને ફરી બોલજો. 🙏",
        HI: "माफ़ कीजिए, आपकी आवाज़ ठीक से सुनाई नहीं दी। फ़ोन मुँह के पास रखकर फिर से बोलिए। 🙏",
        EN: "Sorry, I could not hear you clearly. Please hold the phone close and speak again. 🙏",
    },
}


def message(key: str, lang: str, **fmt: object) -> str:
    """Return a fixed message in `lang` (falls back to Gujarati)."""
    texts = MESSAGES[key]
    text = texts.get(lang) or texts[GU]
    return text.format(**fmt) if fmt else text
