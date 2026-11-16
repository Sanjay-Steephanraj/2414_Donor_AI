"""LLM-based language screening for chat input."""

from __future__ import annotations

import json
import re

NON_ENGLISH_MESSAGE = "Kindly ask question in English"
ENGLISH_ONLY_NOTICE_TEMPLATE = (
    "Please note: I can only respond in English, so I can't provide this answer in {language}."
)
ENGLISH_ONLY_NOTICE_NO_QUERY = (
    "Please let me know what you'd like to know about 24:14 World, and I'll answer in English."
)

DONATION_OPTION_HELP = "Help me choose"
FUND_GUIDANCE_MESSAGE = "Help me find the fund that matches my interests"
DONATION_OPTION_GIVE = "Give now"
DONATION_OPTION_WATCH = "Watch how to give"
_BUTTON_MESSAGES = {
    "learn about 24:14", "help me choose a ministry", "give or donate",
    "contact and partnership", DONATION_OPTION_HELP.casefold(),
    FUND_GUIDANCE_MESSAGE.casefold(), DONATION_OPTION_GIVE.casefold(),
    DONATION_OPTION_WATCH.casefold(), "find a fund", "get donation link",
    "how to give", "guidance to choose a ministry to donate",
    "give / donate via 24:14", "types of donation in 24:14",
    "24:14 contact and partnership",
}

LANGUAGE_NAMES = {
    "Arabic", "Hindi", "Tamil", "Telugu", "Malayalam", "Kannada", "Bengali",
    "Urdu", "Persian", "Farsi", "Spanish", "French", "German", "Portuguese",
    "Chinese", "Mandarin", "Cantonese", "Japanese", "Korean", "Russian",
    "Turkish", "Indonesian", "Swahili", "Nepali", "Punjabi", "Marathi",
    "Gujarati", "Hebrew", "Italian", "Vietnamese", "Thai", "Filipino",
    "Tagalog", "Dutch", "Polish", "Ukrainian", "Greek", "Amharic", "Somali",
    "Hausa", "Yoruba", "Zulu", "Afrikaans", "Albanian", "Armenian", "Azerbaijani",
    "Basque", "Belarusian", "Bosnian", "Bulgarian", "Catalan", "Croatian",
    "Czech", "Danish", "Estonian", "Finnish", "Georgian", "Hungarian",
    "Icelandic", "Irish", "Latvian", "Lithuanian", "Macedonian", "Malay",
    "Mongolian", "Norwegian", "Romanian", "Serbian", "Slovak", "Slovenian",
    "Sundanese", "Swedish", "Tamil", "Welsh", "Xhosa",
}
_LANGUAGE_LOOKUP = {name.casefold(): name for name in LANGUAGE_NAMES}
_LANGUAGE_RE = "|".join(sorted((re.escape(x) for x in LANGUAGE_NAMES), key=len, reverse=True))
_PAGE_RE = re.compile(r"\s*\[from\s+\d+\]\s*$", re.I)


def _project_matchers(message: str):
    # Lazy imports prevent language_service -> ministry_service -> rag_service cycles.
    from ministry_service import match_categories, match_locations, match_ministry, match_ministry_code
    from intent_service import is_api_controlled_message
    return (match_categories, match_locations, match_ministry, match_ministry_code, is_api_controlled_message)


def _strip_known_tokens(message: str) -> str:
    match_categories, match_locations, match_ministry, match_ministry_code, _ = _project_matchers(message)
    result = message
    for matcher in (match_ministry_code, match_ministry):
        value = matcher(message)
        if value:
            result = re.sub(re.escape(value), " ", result, flags=re.I)
    for matcher in (match_locations, match_categories):
        for value in matcher(message) or []:
            result = re.sub(re.escape(value), " ", result, flags=re.I)
    return result


def is_non_english(message: str) -> bool:
    """Return whether the message is primarily non-English using the LLM gate.

    This intentionally fails open when the classifier is unavailable or does
    not return the required shape; language screening must not take down the
    regular chat path.
    """
    text = (message or "").strip()
    if not text:
        return False
    text = _PAGE_RE.sub("", text).strip()
    _, _, _, _, is_api_controlled_message = _project_matchers(text)
    if is_api_controlled_message(text) or text.casefold() in _BUTTON_MESSAGES:
        return False
    try:
        from prompts import LANGUAGE_SCREENING_SYSTEM_PROMPT
        from rag_service import chat_completion

        raw = chat_completion(
            LANGUAGE_SCREENING_SYSTEM_PROMPT,
            text,
            temperature=0.0,
            max_tokens=20,
            timeout=10,
        )
        candidate = raw.strip()
        if candidate.startswith("```"):
            candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I)
        result = json.loads(candidate)
        is_english = result.get("is_english")
        if isinstance(is_english, bool):
            return not is_english
    except Exception:
        return False
    return False


def extract_requested_output_language(message: str) -> tuple[str, str] | None:
    text = (message or "").strip()
    if not text:
        return None
    # CSV guard findings: language-like values include Germany, Thailand, and Tamil Nadu;
    # no exact LANGUAGE_NAMES value is currently a location/category/ministry name.
    match_categories, match_locations, match_ministry, _, _ = _project_matchers(text)

    patterns = [
        rf"\b(?:in|into|using|with)\s+(?:the\s+)?(?P<lang>{_LANGUAGE_RE})(?:\s+language)?\b",
        rf"\b(?:translate|translated|translation)\b.*?\b(?:to|into|in)\s+(?P<lang>{_LANGUAGE_RE})\b",
        rf"\b(?:reply|respond|answer|write|speak|say|explain|tell)\b.*?\b(?:in|using)\s+(?P<lang>{_LANGUAGE_RE})\b",
        rf"\b(?P<lang>{_LANGUAGE_RE})\s+(?:version|translation|language)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        lang = _LANGUAGE_LOOKUP[match.group("lang").casefold()]
        if lang.casefold() == "english":
            return None
        tail = text[match.end():].lstrip(" ,.!?;:")
        if re.match(r"(?:speakers?|people|world|peninsula|region)\b", tail, re.I):
            continue
        phrase = text[max(0, match.start() - 50):match.end()]
        catalog_values = {
            str(value).casefold() for value in (match_locations(phrase) or [])
        }
        catalog_values.update(str(value).casefold() for value in (match_categories(phrase) or []))
        catalog_values.add(str(match_ministry(phrase) or "").casefold())
        if lang.casefold() in catalog_values:
            continue
        cleaned = (text[:match.start()] + " " + text[match.end():]).strip()
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.!?;:")
        if re.fullmatch(r"(?:reply|respond|answer|write|speak|say|explain|tell)(?:\s+(?:in|using))?", cleaned, re.I):
            cleaned = ""
        return cleaned, lang
    return None
