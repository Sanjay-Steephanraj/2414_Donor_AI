import json
import os
import re
import requests
from urllib.parse import urlparse, urlsplit

from datetime import (
    datetime,
    timezone
)
from uuid import uuid4

from dotenv import load_dotenv

from fastapi import HTTPException

from app_loggers import get_logger

from prompts import (
    SYSTEM_PROMPT,
    DONATION_LOCATION_SYSTEM_PROMPT,
    MINISTRY_SYSTEM_PROMPT,
    UNSUPPORTED_CREATION_MESSAGE,
)

# ============================================================
# Load Environment Variables
# ============================================================

load_dotenv()

API_KEY = os.getenv("GLOO_API_KEY") or os.getenv("API_KEY")

PUBLISHER_ID = (
    os.getenv("PUBLISHER_ID")
)
PUBLISHER_NAME = (
    os.getenv("PUBLISHER_NAME")
)
MODEL = os.getenv(
    "MODEL",
    "gloo-openai-gpt-5.6-luna"
)

SEARCH_URL = (
    "https://platform.ai.gloo.com/ai/v1/data/search"
)

CHAT_URL = (
    "https://platform.ai.gloo.com/ai/v2/chat/completions"
)

# ============================================================
# Logger
# ============================================================

logger = get_logger()

# ============================================================
# Validate Environment Variables
# ============================================================

if not API_KEY:
    logger.error("GLOO_API_KEY (or API_KEY) missing in environment")
    raise ValueError("GLOO_API_KEY (or API_KEY) missing in environment")

if not PUBLISHER_ID:

    logger.error(
        "PUBLISHER_ID missing in .env"
    )

    raise ValueError(
        "PUBLISHER_ID missing in .env"
    )

if not PUBLISHER_NAME:

    logger.error(
        "PUBLISHER_NAME missing in .env"
    )

    raise ValueError(
        "PUBLISHER_NAME missing in .env"
    )

logger.info(
    f"PUBLISHER_ID: {PUBLISHER_ID}"
)

logger.info(
    f"MODEL: {MODEL}"
)

# ============================================================
# Auth Headers
# ============================================================

def auth_headers():

    return {
        "Authorization":
        f"Bearer {API_KEY}",

        "Content-Type":
        "application/json"
    }

# ============================================================
# Constants
# ============================================================

DONATION_URL = "https://www.2414world.com/donate"
DONOR_REGISTER_URL = "https://www.2414world.com/auth/register"

DONATION_TUTORIAL_URL = (
    "https://www.youtube.com/watch?v=ymH1tVId24Y"
)

DONATION_OPTION_HELP = "Help me choose"

_URL_RE = re.compile(r'https?://[^\s<>"\')\]]+', re.IGNORECASE)
_URL_TRAILING_PUNCTUATION = '.,;:!?)]}>’"\''


def _clean_url(value: str) -> str:
    return (value or "").strip().rstrip(_URL_TRAILING_PUNCTUATION)


def _url_key(value: str) -> tuple:
    parsed = urlparse(_clean_url(value))
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    return host, parsed.port, path, parsed.query


def sanitize_urls_in_text(text: str) -> str:
    """Remove URL-adjacent punctuation and model security commentary."""

    if not isinstance(text, str):
        return text

    cleaned = re.sub(
        r'(https?://[^\s<>"\')\]]+)(?:\s+and)?\s*\((?:secure link|https is secure|secured)\)',
        lambda match: _clean_url(match.group(1)),
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r'(https?://[^\s<>"\')\]]+)\s*(?:-\s*the secure version|\(secured\))',
        lambda match: _clean_url(match.group(1)),
        cleaned,
        flags=re.IGNORECASE,
    )
    return _URL_RE.sub(lambda match: _clean_url(match.group(0)), cleaned)


def _dedupe_urls(urls):
    """Dedupe equivalent URLs, preferring https while preserving order."""

    result = []
    indexes = {}
    for raw in urls or []:
        if not isinstance(raw, str):
            continue
        url = _clean_url(raw)
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            continue
        key = _url_key(url)
        index = indexes.get(key)
        if index is None:
            indexes[key] = len(result)
            result.append(url)
        elif url.lower().startswith("https://") and not result[index].lower().startswith("https://"):
            result[index] = url
    return result


def _prefer_https_in_text(text: str, preferred_urls) -> str:
    preferred = {_url_key(url): url for url in preferred_urls or []}
    if not preferred:
        return sanitize_urls_in_text(text)

    seen = set()

    def replace(match):
        raw = _clean_url(match.group(0))
        replacement = preferred.get(_url_key(raw))
        if replacement:
            key = _url_key(replacement)
            if key in seen:
                return ""
            seen.add(key)
            return replacement
        return raw

    cleaned = _URL_RE.sub(replace, text or "")
    cleaned = re.sub(r"\s+(?:and|or)\s+(?=[.!?]|$)", "", cleaned, flags=re.IGNORECASE)
    return sanitize_urls_in_text(cleaned).strip()

# Keep the button wording concise while submitting an unambiguous message to
# the backend. This prevents the ministry-selection router from treating this
# donor-guidance choice as a request to browse ministry categories.
FUND_GUIDANCE_MESSAGE = "Help me find the fund that matches my interests"

DONATION_OPTION_GIVE = "Give now"

DONATION_OPTION_WATCH = "Watch how to give"

DONATION_SUGGESTIONS = [
    {"label": "Find a fund", "message": FUND_GUIDANCE_MESSAGE},
    {"label": "Get donation link", "message": DONATION_OPTION_GIVE},
    {"label": "How to give", "message": DONATION_OPTION_WATCH},
]

# Keep the public option list in sync with the labels rendered from the
# structured suggestions. Each button still submits its exact message above.
DONATION_OPTIONS = [item["label"] for item in DONATION_SUGGESTIONS]

DONATION_INTENT_MESSAGE = (
    "I'd love to help you give! You can make a one-time gift, set up monthly recurring support, or make a designated gift for a specific missionary, project, or fund. If you have a donor account, you can sign in to manage your giving. How would you like to proceed?"
)

DONATION_GIVE_MESSAGE = (
    "You can make your donation here. If you already have a donor account, sign in to manage your giving."
)

DONATION_WATCH_MESSAGE = (
    "Here's a short video on how to give:"
)

DONATION_KEYWORDS = [
    "donate",
    "donation",
    "donations",
    "give",
    "giving",
    "contribute",
    "contribution",
]

MINISTRY_KEYWORDS = [
    "ministry",
    "ministries",
    "missionary",
    "missionaries",
    "fund",
    "funds",
    "mission",
    "missions",
]

QUICK_REPLIES = [
    "Learn about 24:14",
    "Guidance to choose a ministry to donate",
    "Give / Donate via 24:14",
    "Types of donation in 24:14",
    "24:14 contact and partnership"
]

WELCOME_QUICK_REPLIES = [
    {"label": "About 24:14", "message": "Learn about 24:14"},
    {"label": "Explore ministries", "message": "Help me choose a ministry"},
    {"label": "Give or donate", "message": "Give or Donate"},
    {"label": "Contact", "message": "Contact and Partnership"},
]

# ============================================================
# Query / Intent Router
# ============================================================

# These words indicate that the user is asking to retrieve, list, show,
# find, or inspect ministry information.  They are lookup signals even
# when donation language is also present.
LOOKUP_ACTION_WORDS = {
    "list",
    "show",
    "find",
    "details",
    "detail",
    "describe",
    "description",
    "information",
    "info",
}

# "give me ..." is common lookup grammar.  In that construction, "give"
# must NOT be interpreted as donation intent.
_GIVE_ME_LOOKUP_RE = re.compile(
    r"\b(?:give|show)\s+me\b",
    re.IGNORECASE
)


def _normalized_query(message: str) -> str:
    return re.sub(r"\s+", " ", (message or "").strip().lower())


def has_lookup_action(message: str) -> bool:
    """Return True for explicit list/show/find/details-style lookup wording."""

    text = _normalized_query(message)

    if _GIVE_ME_LOOKUP_RE.search(text):
        return True

    words = {
        word.strip(".,!?;:\"'()[][]{}")
        for word in text.split()
    }

    return bool(words & LOOKUP_ACTION_WORDS)


def is_donation_query(message: str) -> bool:
    """
    Donation language detector.

    The word 'give' is donation language only when it is not being used
    in the lookup construction 'give me ...'.  Other donation terms such
    as 'donate'/'donation' remain donation signals.
    """

    text = _normalized_query(message)

    donation_terms = set(DONATION_KEYWORDS) - {"give"}

    if any(
        re.search(rf"\b{re.escape(keyword)}\b", text)
        for keyword in donation_terms
    ):
        return True

    if re.search(r"\bgive(?:s|ing)?\b", text):
        return not bool(_GIVE_ME_LOOKUP_RE.search(text))

    return False


def is_ministry_query(message: str) -> bool:
    """Return True when the message contains a ministry-related term."""

    text = _normalized_query(message)

    return any(
        re.search(rf"\b{re.escape(keyword)}\b", text)
        for keyword in MINISTRY_KEYWORDS
    )


def determine_intent(
    message: str,
    *,
    matched_ministry: str | None = None,
    matched_category: str | None = None,
    matched_location: str | None = None,
) -> str:
    """
    Determine the request intent exactly once.

    Priority:
      1. exact ministry name/code
      2. filtered ministry lookup
      3. generic donation entry
      4. general grounded question

    Donation language can coexist with a lookup signal, but it never
    overrides an explicit ministry/category/location lookup.
    """

    if matched_ministry:
        return "ministry_lookup"

    if (
        has_lookup_action(message)
        or matched_category
        or matched_location
    ):
        return "filtered_ministry_lookup"

    if is_donation_query(message):
        return "generic_donation"

    return "general_grounded"


# ============================================================
# Backward-compatible aliases
# ============================================================

def is_lookup_query(message: str) -> bool:
    """Convenience helper for callers that only need lookup detection."""

    return has_lookup_action(message)


# ============================================================
# Citation Helpers
# ============================================================

def _coerce_text(value):
    """Normalize varied LLM/Gloo payload text shapes into a plain string."""

    if value is None:
        return ""

    if isinstance(value, str):
        return value

    if isinstance(value, list):
        parts = []
        for item in value:
            text = _coerce_text(item)
            if text:
                parts.append(text)
        return "".join(parts)

    if isinstance(value, dict):
        for key in ("text", "content", "value"):
            item = value.get(key)
            if item is not None:
                text = _coerce_text(item)
                if text:
                    return text
        return ""

    return str(value)


def source_url_candidate(source_url):
    """Derive the only permitted URL candidate from an authoritative source URL."""

    if not isinstance(source_url, str) or not source_url.strip():
        return ""

    original = source_url.strip()
    parsed = urlsplit(original)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return original

    if " " not in parsed.path:
        return original

    path_start = len(parsed.scheme) + 3 + len(parsed.netloc)
    path_end = path_start + len(parsed.path)
    return original[:path_start] + parsed.path.replace(" ", "-") + original[path_end:]


def extract_source_url(snippet):
    """Extract the SOURCE_URL value from a retrieved Gloo snippet."""

    if not isinstance(snippet, str):
        return ""

    match = re.search(r"^\s*SOURCE_URL:\s*(https?://.*)\s*$", snippet, re.MULTILINE)
    return match.group(1).strip() if match else ""


def extract_citation_titles(data):

    citations = data.get("citations", []) if isinstance(data, dict) else []
    items = []

    if isinstance(citations, dict):
        citations = [citations]

    for citation in citations or []:
        if isinstance(citation, dict):
            item_title = citation.get("item_title") or citation.get("title")
            if item_title:
                items.append(str(item_title))
        elif isinstance(citation, str) and citation.strip():
            items.append(citation.strip())

    return items


def extract_citation_links(data):
    """Extract unique HTTP(S) citation URLs in the client link format."""
    citations = data.get("citations", []) if isinstance(data, dict) else []
    if isinstance(citations, dict):
        citations = [citations]
    links, seen = [], set()
    for citation in citations or []:
        if not isinstance(citation, dict):
            continue
        title = citation.get("item_title") or citation.get("title") or citation.get("label") or "Source"
        metadata = citation.get("metadata") if isinstance(citation.get("metadata"), dict) else {}
        source = citation.get("source") if isinstance(citation.get("source"), dict) else {}
        source_url = citation.get("source_url") or citation.get("item_url")
        if not isinstance(source_url, str) or not source_url.strip():
            source_url = citation.get("url") or citation.get("href") or metadata.get("url") or source.get("url")
        candidate = source_url_candidate(source_url) if isinstance(source_url, str) else ""
        if candidate:
            parsed = urlparse(candidate)
            if parsed.scheme.lower() in {"http", "https"} and parsed.netloc and candidate not in seen:
                seen.add(candidate)
                links.append({"label": str(title), "url": candidate})
    return links


def _source_url_records(data):
    """Return source URL records while keeping each original URL immutable."""

    citations = data.get("citations", []) if isinstance(data, dict) else []
    if isinstance(citations, dict):
        citations = [citations]
    records = []
    for citation in citations or []:
        if not isinstance(citation, dict):
            continue
        original = citation.get("source_url") or citation.get("item_url")
        if isinstance(original, str) and original.strip():
            original = original.strip()
            records.append((original, source_url_candidate(original)))
    return records


def _correct_answer_urls_from_sources(answer, data):
    """Correct only exact authoritative source URL occurrences in the answer."""

    if not isinstance(answer, str):
        return answer
    corrected = answer
    for original, candidate in _source_url_records(data):
        if original and candidate and original != candidate:
            corrected = corrected.replace(original, candidate)
    return corrected


# ============================================================
# Structured Response Helpers

_RETRIEVER_TONE_RE = re.compile(
    r"\b(?:the\s+)?(?:context|contact page|provided (?:information|content|context)|"
    r"available (?:data|information|content)|source documents?|retrieved)\b|"
    r"\b(?:is|are)\s+(?:not\s+)?(?:listed|mentioned|provided|included)\b|"
    r"\bdoes(?:n't| not)\s+(?:list|mention|contain|include|provide)\b|"
    r"\bno\s+.{0,40}\s+(?:is|are)\s+(?:listed|available|provided)\b",
    re.IGNORECASE,
)


def sounds_like_retriever(answer: str) -> bool:
    return bool(_RETRIEVER_TONE_RE.search(answer or ""))
# ============================================================

DESIGN_INPUT_CLARIFICATION_RE = re.compile(
    r"\b(?:dimension(?:s)?|size|resolution|colour|color\s+scheme|style|layout|"
    r"logo|mission\s+statement|vision\s+statement|brand(?:ing)?|template|"
    r"what\s+should\s+the\s+website|what\s+kind\s+of\s+image)\b",
    re.IGNORECASE,
)


def is_design_input_clarification(answer: str) -> bool:
    return bool(DESIGN_INPUT_CLARIFICATION_RE.search(answer or ""))

def _extract_first_json_object(text: str):
    """
    Scan text for the first parseable JSON object using json.JSONDecoder.
    Respects quoted braces/escapes so it does not truncate on literal '}'.
    Returns the parsed dict, or None if no valid object is found.
    """

    decoder = json.JSONDecoder()
    idx = 0

    while idx < len(text):
        char = text[idx]

        if char in (" ", "\t", "\n", "\r"):
            idx += 1
            continue

        if char != "{":
            idx += 1
            continue

        try:
            parsed, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            idx += 1
            continue

        if isinstance(parsed, dict):
            return parsed

        idx += max(end, 1)

    return None


def _extract_structured_content(content: str, user_message: str | None = None) -> tuple[str, list[dict[str, str]], str]:
    """
    Parse model output that should be a JSON object with
    'answer', 'answer_status', and 'follow_ups' keys.

    Returns (answer, follow_ups, status). Follow-ups are normalized to
    ``{"label": ..., "message": ...}`` objects and discarded unless
    answer_status is exactly 'answered'. On any parsing or type failure
    the original raw content is returned as the answer with no follow-ups
    and status set to 'out_of_scope'.
    """

    content_text = _coerce_text(content)
    if not content_text:
        return "", [], "out_of_scope"

    text = content_text.strip()
    parsed = None

    # Try plain/fenced JSON first.
    fenced_match = re.search(
        r"```(?:json)?\s*(\{.*)\s*```",
        text,
        re.DOTALL | re.IGNORECASE
    )

    candidates = [text]

    if fenced_match:
        candidates.insert(0, fenced_match.group(1))

    for candidate in candidates:
        try:
            maybe = json.loads(candidate)
            if isinstance(maybe, dict):
                parsed = maybe
                break
        except Exception:
            pass

    # If direct parse failed, scan for the first valid JSON object.
    if parsed is None:
        parsed = _extract_first_json_object(text)

    if not isinstance(parsed, dict):
        logger.warning(
            "Structured response could not be parsed; "
            "returning raw content as answer with no follow-ups"
        )
        return text, [], "out_of_scope"

    answer = parsed.get("answer", "")
    follow_ups = parsed.get("follow_ups", [])
    status = str(parsed.get("answer_status", "") or "").strip().lower()

    valid_statuses = {
        "answered",
        "needs_clarification",
        "no_result",
        "out_of_scope",
        "unsupported",
    }

    if status not in valid_statuses:
        logger.warning(
            "Unknown answer_status %r; defaulting to out_of_scope",
            status
        )
        status = "out_of_scope"

    # Normalize model-facing "unsupported" to the downstream-facing
    # "out_of_scope" cannot-answer state without emitting a warning.
    status = {"unsupported": "out_of_scope"}.get(status, status)

    if not isinstance(answer, str):
        logger.warning(
            "Parsed 'answer' is not a string; falling back to raw content"
        )
        return text, [], "out_of_scope"

    from ministry_service import is_gibberish
    if user_message is not None and is_gibberish(user_message):
        return "I didn't quite understand that. Could you please rephrase your question about 24:14 World?", [], "needs_clarification"

    answer = sanitize_urls_in_text(answer.strip())

    if status == "needs_clarification" and is_design_input_clarification(answer):
        logger.warning("Model asked for creative-asset requirements; returning creation refusal")
        return UNSUPPORTED_CREATION_MESSAGE, [], "out_of_scope"

    # Server-side enforcement: suggestions are only permitted for
    # genuinely answered, relevant 24:14 questions.
    if status != "answered":
        logger.info(
            "Follow-ups suppressed due to answer_status: %s",
            status
        )
        return answer, [], status

    if not isinstance(follow_ups, (list, tuple)):
        follow_ups = []

    cleaned = []

    for item in follow_ups:
        if isinstance(item, str):
            # Accept the old shape while callers migrate; always expose the
            # normalized object shape downstream.
            message = item.strip()
            if message:
                cleaned.append({"label": message, "message": message})
            continue

        if not isinstance(item, dict):
            continue

        label = item.get("label")
        message = item.get("message")

        if not isinstance(label, str) or not isinstance(message, str):
            continue

        label = label.strip()
        message = message.strip()

        if label and message:
            cleaned.append({"label": label, "message": message})

    return answer, cleaned[:3], "answered"



# ============================================================
# Answer Helpers
# ============================================================

def _answer_and_follow_ups(data: dict, user_message: str | None = None) -> tuple[str, list[dict[str, str]], str]:
    """
    Extract the assistant content from a Gloo grounded chat response
    and parse it for structured answer + follow-ups + status.
    """

    choices = data.get("choices", []) if isinstance(data, dict) else []

    if not choices:
        return "", [], "out_of_scope"

    first = choices[0]
    content = ""

    if isinstance(first, dict):
        message = first.get("message")
        if isinstance(message, dict):
            content = _coerce_text(message.get("content"))
        else:
            content = _coerce_text(message)

        if not content:
            content = _coerce_text(first.get("content"))

    return _extract_structured_content(content, user_message=user_message)



# ============================================================
# Bot Response Builder
# ============================================================

def normalize_suggestions(items, current_message=None):
    """
    Normalize a list of suggestion/button items into a list of
    {"label", "message"} dicts.

    Accepts, per item:
      - a plain string (legacy): becomes {"label": s, "message": s}
      - a dict with "label" and/or "message": missing side is filled
        from the present one; both blank -> item skipped.

    The "label" is what the client renders on the button; the
    "message" is the self-contained text submitted when clicked.
    Order is preserved; empty/invalid items are dropped, and at most five
    suggestions are returned.
    """

    def comparable_text(value):
        return " ".join(re.sub(r"[^\w]+", " ", str(value or "").casefold()).split())

    current_text = comparable_text(current_message)
    normalized = []
    seen = set()

    for item in items or []:
        if len(normalized) >= 5:
            break

        if isinstance(item, str):
            text = item.strip()
            if text and comparable_text(text) != current_text:
                key = (text.casefold(), text.casefold())
                if key not in seen:
                    seen.add(key); normalized.append({"label": text, "message": text})
            continue

        if isinstance(item, dict):
            label = str(item.get("label", "") or "").strip()
            message = str(item.get("message", "") or "").strip()

            if not label and not message:
                continue

            if not message:
                message = label

            if not label:
                label = message

            if current_text and current_text in {
                comparable_text(label), comparable_text(message)
            }:
                continue

            key = (label.casefold(), message.casefold())
            if key not in seen:
                seen.add(key); normalized.append({"label": label, "message": message})
            continue

        # Unknown item type: skip silently.

    return normalized


def bot_response(
    user_message,
    answer,
    data,
    follow_ups=None,
    urls=None,
    resource_links=None,
    session_id=None,
    ministries=None,
    status="answered"
):

    expose_citations = os.getenv("EXPOSE_CITATIONS", "false").strip().lower() in {"1", "true", "yes", "on"}
    citation_links = extract_citation_links(data) if expose_citations else []
    links, link_keys = [], {}
    for link in list(resource_links or []) + citation_links:
        if isinstance(link, str):
            link = {"label": "Resource", "url": link}
        if isinstance(link, dict) and isinstance(link.get("url"), str):
            candidate = _clean_url(link["url"])
            for original, source_candidate in _source_url_records(data):
                if candidate == original:
                    candidate = source_candidate
                    break
            candidate = _clean_url(candidate)
            parsed = urlparse(candidate)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                continue
            key = _url_key(candidate)
            previous = link_keys.get(key)
            if previous is None:
                link_keys[key] = len(links)
                links.append({"label": link.get("label") or "Resource", "url": candidate})
            elif candidate.lower().startswith("https://") and not links[previous]["url"].lower().startswith("https://"):
                links[previous] = {"label": link.get("label") or "Resource", "url": candidate}
    for url, label in ((DONATION_URL, "Donate now"), (DONATION_TUTORIAL_URL, "Watch how to give"), (DONOR_REGISTER_URL, "Register as a donor")):
        if url in (urls or []):
            key = _url_key(url)
            previous = link_keys.get(key)
            if previous is None:
                link_keys[key] = len(links); links.append({"label": label, "url": _clean_url(url)})
            elif url.lower().startswith("https://") and not links[previous]["url"].lower().startswith("https://"):
                links[previous] = {"label": label, "url": _clean_url(url)}
    normalized_urls = _dedupe_urls(urls)
    for link in links:
        link["url"] = _clean_url(link["url"])
    normalized_urls = _dedupe_urls(normalized_urls)
    answer_text = _prefer_https_in_text(_correct_answer_urls_from_sources(answer, data), [item["url"] for item in links] + normalized_urls)
    response = {
        "id": str(uuid4()),
        "sender": "bot",
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat(),
        "message": answer_text,
        "status": status,
        "suggestions": normalize_suggestions(follow_ups, current_message=user_message),
        "urls": normalized_urls,
        "citations": extract_citation_titles(data) if expose_citations else [],
        "links": links,
        "session_id": session_id,
        "ministries": ministries or []
    }

    return response

# ============================================================
# Grounded Chat
# ============================================================

def grounded_chat(
    question,
    donation_location=False,
    ministry_query=False,
    sources_limit: int = 8
):

    # Gloo's source count is independent of the number of ministry matches.
    # Invalid caller values cannot turn an otherwise valid request into 422.
    try:
        sources_limit = min(10, max(1, int(sources_limit)))
    except (TypeError, ValueError, OverflowError):
        sources_limit = 8

    grounding_query = (
        f"In the context of 24:14 World "
        f"(the 24:14 missionary movement / organisation): "
        f"{question}"
    )
    # Keep retrieval focused on the user's exact wording. Adding the
    # organisation description causes verse references to match generic
    # organisation pages instead of the requested passage.
    search_query = str(question).strip()

    if donation_location:
        system_prompt = DONATION_LOCATION_SYSTEM_PROMPT
    elif ministry_query:
        system_prompt = MINISTRY_SYSTEM_PROMPT
    else:
        system_prompt = SYSTEM_PROMPT

    messages = [
        {
            "role": "system",
            "content": system_prompt
        }
    ]

    messages.append(
        {
            "role": "user",
            "content": grounding_query
        }
    )

    try:
        search_response = requests.post(
            SEARCH_URL,
            headers=auth_headers(),
            json={
                "query": search_query,
                "collection": "GlooProd",
                "tenant": re.sub(r"\s+", "", PUBLISHER_NAME),
                "certainty": 0.0,
                "limit": sources_limit,
            },
            timeout=60,
        )

    except requests.RequestException as e:

        logger.exception(
            f"Gloo search request failed: {str(e)}"
        )

        raise HTTPException(
            status_code=500,
            detail="Gloo search request failed"
        )

    logger.info(
        f"Gloo search status: {search_response.status_code}"
    )

    try:
        search_json = search_response.json()

        logger.info(
            f"Gloo search response: {search_json}"
        )

    except Exception:

        logger.exception(
            f"Invalid JSON response: "
            f"{search_response.text}"
        )

        raise HTTPException(
            status_code=500,
            detail="Invalid JSON response"
        )

    if search_response.status_code in (404, 422):

        logger.info(
            f"No search results available "
            f"(status={search_response.status_code}): "
            f"{search_response.text}"
        )

        return {"no_answer": True, "status": "failed"}

    if search_response.status_code == 403:

        logger.error(
            f"Gloo search forbidden: "
            f"{search_response.text}"
        )

        raise HTTPException(
            status_code=403,
            detail=(
                "Gloo API rejected this search because the configured "
                "credentials do not have permission to search this publisher."
            )
        )

    if search_response.status_code != 200:

        logger.error(
            f"Gloo search returned error: "
            f"{search_response.text}"
        )

        raise HTTPException(
            status_code=search_response.status_code,
            detail=search_response.text
        )

    search_results = search_json.get("data", [])
    if not isinstance(search_results, list) or not search_results:
        return {
            "no_answer": True,
            "status": "failed",
            "sources_returned": False,
            "citations": [],
        }

    citations = []
    retrieved_context = []
    for result in search_results:
        if not isinstance(result, dict):
            continue
        properties = result.get("properties")
        metadata = result.get("metadata")
        properties = properties if isinstance(properties, dict) else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        title = (
            properties.get("item_title")
            or properties.get("filename")
            or "Knowledge base source"
        )
        snippet = str(properties.get("snippet") or "").strip()
        if not snippet:
            continue
        source_url = properties.get("item_url") or extract_source_url(snippet)
        source_url = source_url.strip() if isinstance(source_url, str) else ""
        retrieved_context.append(f"[{title}]\nSOURCE_URL: {source_url}\n{snippet}")
        citations.append({
            "item_title": title,
            "source_url": source_url,
            "item_url": source_url_candidate(source_url),
            "author": properties.get("author") or [],
            "publisher": properties.get("publisher") or PUBLISHER_NAME,
            "publication_date": properties.get("publication_date") or "",
            "snippets": [snippet],
            "certainty": metadata.get("certainty"),
        })

    if not retrieved_context:
        return {
            "no_answer": True,
            "status": "failed",
            "sources_returned": False,
            "citations": [],
        }

    scripture_reference = re.search(
        r"\b(Genesis|Exodus|Leviticus|Numbers|Deuteronomy|Joshua|Judges|Ruth|"
        r"1 Samuel|2 Samuel|1 Kings|2 Kings|1 Chronicles|2 Chronicles|Ezra|"
        r"Nehemiah|Esther|Job|Psalms?|Proverbs|Ecclesiastes|Song of Solomon|"
        r"Isaiah|Jeremiah|Lamentations|Ezekiel|Daniel|Hosea|Joel|Amos|Obadiah|"
        r"Jonah|Micah|Nahum|Habakkuk|Zephaniah|Haggai|Zechariah|Malachi|"
        r"Matthew|Mark|Luke|John|Acts|Romans|1 Corinthians|2 Corinthians|"
        r"Galatians|Ephesians|Philippians|Colossians|1 Thessalonians|"
        r"2 Thessalonians|1 Timothy|2 Timothy|Titus|Philemon|Hebrews|James|"
        r"1 Peter|2 Peter|1 John|2 John|3 John|Jude|Revelation)\s+\d+:\d+",
        search_query,
        re.IGNORECASE,
    )
    if scripture_reference:
        retrieval_instruction = (
            f"The excerpts identify the requested Bible passage "
            f"{scripture_reference.group(0)}. Answer with the complete passage "
            "wording represented by the retrieved excerpts. Search snippets "
            "may be truncated at their boundaries; do not refuse merely "
            "because a snippet ends mid-verse. Do not add commentary about "
            "the passage being incomplete."
        )
    else:
        retrieval_instruction = (
            "Use only the following retrieved knowledge-base excerpts to "
            "answer the user's question. If the excerpts do not support an "
            "answer, return exactly NO_RELEVANT_CONTEXT. Do not invent facts."
        )
    context_message = (
        retrieval_instruction
        + "\n\nUse the union of ALL relevant excerpts below. Do not answer "
        "from only the first result, and do not discard a relevant excerpt "
        "because another excerpt overlaps with it. Reconcile overlapping "
        "snippets and combine complementary wording into one grounded answer. "
        "Do not return NO_RELEVANT_CONTEXT when any relevant excerpt "
        "contains it.\n\n"
        + "\n\n".join(retrieved_context)
    )
    chat_payload = {
        "messages": messages[:1] + [
            {"role": "system", "content": context_message},
            messages[1],
        ],
        "model": MODEL,
        "temperature": 0.2,
        "max_tokens": 1000,
    }

    try:
        response = requests.post(
            CHAT_URL,
            headers=auth_headers(),
            json=chat_payload,
            timeout=120,
        )
    except requests.RequestException as e:
        logger.exception(f"Gloo chat request failed after search: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail="Gloo chat request failed after search",
        )

    try:
        response_json = response.json()
    except Exception:
        logger.exception(f"Invalid JSON response: {response.text}")
        raise HTTPException(status_code=500, detail="Invalid JSON response")

    if response.status_code != 200:
        logger.error(f"Gloo chat returned error: {response.text}")
        raise HTTPException(status_code=response.status_code, detail=response.text)

    response_json["sources_returned"] = True
    response_json["citations"] = citations
    logger.info(
        "Search-grounded metadata | publisher=%s sources_returned=%r citations=%d",
        PUBLISHER_NAME,
        response_json.get("sources_returned"),
        len(response_json.get("citations") or []),
    )
    return response_json


def grounded_response_has_sources(data: dict) -> bool:
    """Native retrieval evidence, not the model's own answer_status.

    Retrieval does not prove every generated claim. CSV entity/fact checks
    remain separate, and citations are retained for QA rather than fabricated.
    """
    return isinstance(data, dict) and data.get("sources_returned") is True


def llm_extract_slots(query: str, categories: list[str], locations: list[str]) -> dict | None:
    """
    Use a plain (non-grounded) Gloo chat completion to extract the intended
    ministry category and location from a natural-language query.

    Returns a dict like {"category": <str-or-None>, "location": <str-or-None>}
    where each value is EITHER exactly one of the provided canonical
    `categories` / `locations`, OR None. Returns None on any failure so the
    caller can fall back to deterministic matching.

    The model is instructed to only choose from the provided lists and to
    never invent a value. It must return strict JSON.
    """

    system_prompt = (
        "You are a slot extractor for the 24:14 World missionary organisation. "
        "Extract exactly two fields from the user query:\n"
        '- "category": the ministry type/focus. It MUST be exactly one of the '
        "provided canonical category names, or null if none clearly applies.\n"
        '- "location": the geographic location. It MUST be exactly one of the '
        "provided canonical location names, or null if none clearly applies.\n"
        "Map synonyms or related intent to the closest item in the lists "
        "(for example, 'kids' maps to 'Children Care' and 'war refugees' maps to "
        "'Refugee Support'). NEVER invent categories or locations that are not in "
        "the provided lists. Return ONLY a JSON object of the form "
        '{"category": "...", "location": "..."} with no prose, no markdown, '
        "and no code fences.\n\n"
        f"Allowed categories: {', '.join(categories)}\n"
        f"Allowed locations: {', '.join(locations)}"
    )

    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query},
        ],
        "model": MODEL,
        "temperature": 0.0,
        "max_tokens": 200,
    }

    try:
        response = requests.post(
            CHAT_URL,
            headers=auth_headers(),
            json=payload,
            timeout=30,
        )
        response.raise_for_status()

        data = response.json()
        choices = data.get("choices", [])
        first_choice = choices[0] if isinstance(choices, list) and choices else {}
        message = first_choice.get("message", {}) if isinstance(first_choice, dict) else {}
        content = _coerce_text(message.get("content") if isinstance(message, dict) else message).strip()

        fenced_match = re.search(
            r"```(?:json)?\s*(.*?)\s*```",
            content,
            re.DOTALL | re.IGNORECASE,
        )
        if fenced_match:
            content = fenced_match.group(1).strip()

        parsed = json.loads(content)

        if not isinstance(parsed, dict):
            logger.warning(
                "llm_extract_slots: parsed content is not a dict for query %r",
                query,
            )
            return None

        category_map = {item.lower(): item for item in categories}
        location_map = {item.lower(): item for item in locations}

        raw_category = parsed.get("category")
        raw_location = parsed.get("location")

        category = None
        if isinstance(raw_category, str) and raw_category.strip():
            category = category_map.get(raw_category.strip().lower())

        location = None
        if isinstance(raw_location, str) and raw_location.strip():
            location = location_map.get(raw_location.strip().lower())

        logger.info(
            "llm_extract_slots: query=%r category=%r location=%r",
            query,
            category,
            location,
        )

        return {"category": category, "location": location}

    except requests.RequestException as e:
        logger.warning(
            "llm_extract_slots: request failed for query %r: %s",
            query,
            e,
        )

    except json.JSONDecodeError as e:
        logger.warning(
            "llm_extract_slots: JSON decode failed for query %r: %s",
            query,
            e,
        )

    except Exception as e:
        logger.warning(
            "llm_extract_slots: unexpected error for query %r: %s",
            query,
            e,
        )

    return None


def chat_completion(
    system_prompt: str,
    user_content: str,
    *,
    temperature: float = 0.0,
    max_tokens: int = 300,
    timeout: int = 15,
) -> str:
    """
    Reusable non-grounded Gloo chat-completion helper.

    Mirrors the existing `llm_extract_slots` call pattern: it reuses the
    module-level `CHAT_URL`, `MODEL`, and `auth_headers()` to call the plain
    Gloo chat completions endpoint. Intended for the
    intent classifier so it does not duplicate auth/token/HTTP logic.

    Raises on failure so callers with deterministic fallbacks can catch and
    handle timeouts separately from other errors.
    """

    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "model": MODEL,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    try:
        response = requests.post(
            CHAT_URL,
            headers=auth_headers(),
            json=payload,
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        raise
    except requests.RequestException as e:
        logger.warning(
            "chat_completion: request failed: %s",
            e,
        )
        raise RuntimeError(f"chat_completion request failed: {e}") from e

    if response.status_code != 200:
        logger.warning(
            "chat_completion: Gloo API returned status %s",
            response.status_code,
        )
        raise RuntimeError(
            f"chat_completion failed with status {response.status_code}"
        )

    try:
        data = response.json()
    except Exception as e:
        logger.warning(
            "chat_completion: invalid JSON response: %s",
            e,
        )
        raise RuntimeError(f"chat_completion invalid JSON response: {e}") from e

    choices = data.get("choices", [])
    first_choice = choices[0] if isinstance(choices, list) and choices else {}
    message = first_choice.get("message", {}) if isinstance(first_choice, dict) else {}
    content = _coerce_text(message.get("content") if isinstance(message, dict) else message).strip()

    if not content:
        raise RuntimeError(
            "chat_completion response missing choices[0].message.content"
        )

    return content
