from __future__ import annotations
import json, re
from dataclasses import dataclass
import requests
from app_loggers import get_logger
from prompts import INTENT_CLASSIFIER_SYSTEM_PROMPT
from rag_service import chat_completion, determine_intent, is_donation_query
from ministry_service import match_category, match_categories, match_locations, match_ministry, match_ministry_code, match_negated_categories, is_all_locations_request, find_ministry_by_code, is_general_information_question, is_gibberish
from fund_service import fund_candidates
logger = get_logger()
SEE_MORE_RE = re.compile(
    r"^(?:"
    r"see\s+more|show\s+more|view\s+all|"
    r"(?:show|see|view|list)\s+(?:the\s+)?"
    r"(?:all|remaining|rest(?:\s+of)?|more|other)\b"
    r".*\bministr(?:y|ies)\b.*"
    r")\s*[.!?]*$",
    re.I,
)
ALLOWED_INTENTS = {"ministry_lookup", "donation_flow", "general_rag"}
ALLOWED_ACTIONS = {"continue_current_flow", "select_category", "select_location", "search_ministry", "show_donation_options", "give_now", "watch_donation_guide", "answer_general_question", "restart_flow"}
CORRECTION_RE = re.compile(r"\b(?:actually|instead|change|rather|not .* but)\b", re.I)
RESULT_REFERENCE_RE = re.compile(r"\b(first|second|third|last|that(?: one)?|this(?: one)?|it)\b", re.I)
ELLIPTICAL_FOLLOW_UP_RE = re.compile(r"^(?:and |in |for )?[\w ,'-]+\??$", re.I)
QUESTION_START_RE = re.compile(r"^(?:what|who|when|where|why|how|which)\b", re.I)
GENERAL_TOPIC_REQUEST_RE = re.compile(
    r"\b(?:trending|trend|latest|breaking|current|recent)\s+"
    r"(?:news|updates?|stories?|developments?)\b|"
    r"\b(?:news|updates?|stories?|developments?)\b.*\b"
    r"(?:trending|latest|breaking|current|recent)\b",
    re.I,
)
DETAIL_FOLLOW_UP_RE = re.compile(
    r"\b(?:tell me more|more (?:about|details)|give me more details|"
    r"details?|describe|what does (?:this|it|that)(?: ministry)? do|"
    r"what is (?:this|its|that)(?: ministry)?(?:'s)? work|"
    r"tell me about (?:this|it|that)(?: ministry)?)\b",
    re.I,
)
OTHER_MINISTRIES_RE = re.compile(r"\b(?:other|more|another) ministr(?:y|ies)\b|find another ministry", re.I)
GUIDED_CLARIFICATION_RE = re.compile(r"\b(?:no preference|any(?:where| location)?|all locations?)\b", re.I)
UNDERSPECIFIED_REQUEST_WORDS = (
    "link", "links", "url", "website", "page", "info", "information",
    "details", "detail", "help", "question", "more", "resources", "know",
)
_UNDERSPECIFIED_RE = re.compile(
    r"^(?:can\s+you\s+|please\s+|i\s+)?"
    r"(?:(?:i\s+)?(?:need|want|send|give|share|get|have)\s+(?:to\s+)?"
    r"(?:(?:me|the|a|some)\s+)*"
    r"(?:link|links|url|website|page|info|information|details?|help|question|more|resources|know)"
    r"|tell\s+me|help(?:\s+me)?|(?:link|links|url|website|page|info|information|details?|help|question|more|resources|know))"
    r"[.!?]*$",
    re.I,
)
UNDERSPECIFIED_TOPIC_RE = re.compile(
    r"\b(?:prayer|pray|ministr(?:y|ies)|fund|contact|partner(?:ship)?|volunteer|"
    r"24\s*:\s*14|mission|vision|impact|video|tutorial|register|account)\b",
    re.I,
)
_CREATION_OBJECT_RE = (
    r"(?:web\s*site|website|web\s*page|webpage|landing\s+page|web\s+app|"
    r"app|html|css|logos?|images?|pictures?|photos?|posters?|banners?|flyers?|"
    r"graphics?|infographics?|illustrations?|icons?|thumbnails?|"
    r"social\s+media\s+(?:posts?|graphics?)|templates?|mockups?|wireframes?|"
    r"videos?|something)"
)
_CREATION_REQUEST_RE = re.compile(
    r"(?:(?:create|make|build|design|generate|develop|draw|produce|code|"
    r"set\s+up|prepare)\b[^.!?\n]{0,100}\b" + _CREATION_OBJECT_RE + r"\b|"
    r"\bwrite\s+the\s+code\s+for\b[^.!?\n]{0,80}\b" + _CREATION_OBJECT_RE + r"\b|"
    r"\b(?:help\s+me\s+)?(?:create|make|build|design|generate)\b[^.!?\n]{0,100}\b" + _CREATION_OBJECT_RE + r"\b|"
    r"\bcan\s+you\s+(?:create|make|build|design|generate|draw)\b[^.!?\n]{0,100}\b" + _CREATION_OBJECT_RE + r"\b|"
    r"\bi\s+(?:need|want)\s+(?:an?|some)\s+" + _CREATION_OBJECT_RE + r"\b)",
    re.I,
)
_ACCOUNT_CREATION_RE = re.compile(
    r"\b(?:create|make|build|design|generate|develop|draw|produce|code)\b"
    r"[^.!?\n]{0,80}\b(?:donor\s+)?account\b",
    re.I,
)
GIVING_GUIDANCE_RE = re.compile(
    r"\b(?:whom|who|where|which\s+(?:ministry|ministries|fund|funds|cause|causes))\b"
    r".{0,30}\b(?:give|donate|support|contribute)\b",
    re.I,
)


def is_giving_guidance_request(message: str) -> bool:
    """Return True for requests asking whom, where, or which cause to support."""

    text = " ".join((message or "").strip().split())
    return bool(text) and len(text) <= 80 and bool(GIVING_GUIDANCE_RE.search(text))


def is_unsupported_creation_request(message: str) -> bool:
    """Return True when the user asks for a website or creative asset."""

    text = " ".join((message or "").strip().lower().split())
    if _ACCOUNT_CREATION_RE.search(text):
        return False
    return bool(text and _CREATION_REQUEST_RE.search(text))


def is_underspecified_request(message: str, state=None) -> bool:
    """Return True for generic requests whose topic cannot be inferred safely."""

    text = " ".join((message or "").strip().split())
    if not text or not _UNDERSPECIFIED_RE.fullmatch(text):
        return False
    if (
        match_categories(text)
        or match_locations(text)
        or is_all_locations_request(text)
        or match_ministry(text)
        or match_ministry_code(text)
        or fund_candidates(text)
        or is_donation_query(text)
        or UNDERSPECIFIED_TOPIC_RE.search(text)
    ):
        return False

    slots = _state_value(state, "slots", {}) or {}
    last_result = _state_value(state, "last_result", []) or []
    selected_fund = slots.get("selected_fund") or slots.get("selected_funds")
    selected_code = slots.get("ministry_code") or slots.get("selected_ministry_codes")
    if last_result or selected_fund or selected_code or _state_value(state, "last_action") == "give_now":
        return False
    return True


class IntentClassifierError(Exception): pass
@dataclass
class IntentResult:
    context_mode: str
    intent: str
    action: str
    confidence: float
    slots: dict
    clear_slots: list[str]
    source: str = "llm"
    needs_clarification: bool = False
    def to_dict(self): return self.__dict__.copy()


def _empty_slots():
    return {
        "category": None,
        "categories": [],
        "location": None,
        "locations": [],
        "all_locations": False,
        "ministry": None,
        "ministry_code": None,
    }


def _state_value(state, key, default=None):
    if isinstance(state, dict):
        return state.get(key, default)
    return getattr(state, key, default)


def parse_see_more_offset(message):
    """Return the command without its paging suffix and the requested offset."""

    text = (message or "").strip()
    match = re.search(r"\[from\s+(\d+)\]", text, re.IGNORECASE)
    if not match:
        return text, None
    clean = (text[:match.start()] + text[match.end():]).strip()
    return clean, int(match.group(1))


def is_see_more_request(message: str) -> bool:
    """Return True for bare or descriptive ministry-list expansion requests."""

    cleaned, _ = parse_see_more_offset(message)
    text = " ".join((cleaned or "").strip().split())
    return bool(text and SEE_MORE_RE.fullmatch(text))


def result_reference_index(message, last_result_count=0):
    match = RESULT_REFERENCE_RE.search(message or "")
    if not match or not last_result_count:
        return None
    index = {
        "first": 0,
        "second": 1,
        "third": 2,
        "last": last_result_count - 1,
        "that": 0,
        "this": 0,
        "it": 0,
    }.get(match.group(1).lower())
    return index if index is not None and index < last_result_count else None


def is_ministry_detail_follow_up(message):
    return bool(DETAIL_FOLLOW_UP_RE.search(message or ""))


def is_vague_ministry_detail_follow_up(message, slots=None, state=None):
    """True for a detail request which does not name a new ministry/filter."""

    return is_vague_ministry_detail_follow_up_with_state(message, slots, state)


def is_vague_ministry_detail_follow_up_with_state(message, slots=None, state=None):
    """Require prior ministry context before treating a detail request as a reference."""

    if not is_ministry_detail_follow_up(message):
        return False
    slots = slots or {}
    if any(slots.get(key) for key in (
        "category", "categories", "locations", "all_locations", "ministry", "ministry_code"
    )):
        return False
    if state is None:
        return False
    state_slots = _state_value(state, "slots", {}) or {}
    return bool(
        _state_value(state, "last_result", [])
        or state_slots.get("ministry")
        or state_slots.get("ministry_code")
    )


def is_api_controlled_message(message):
    """Return True for exact or generated messages emitted by API buttons."""

    cleaned, offset = parse_see_more_offset(message)
    text = cleaned.casefold().rstrip(".")
    if text in {
        "learn about 24:14", "contact and partnership", "impact stories",
        "show me impact stories", "help me choose a ministry",
        "help me choose a ministry to support", "explore ministries",
        "guidance to choose a ministry to donate", "no preference",
        "continue without a ministry category preference", "give now",
        "donate now", "watch how to give", "give or donate",
        "show ministries in all locations", "show all ministries in all locations",
        "see more", "show more", "view all",
    }:
        return True
    if offset is not None and re.match(r"^show\s+all\b", text):
        return True
    if is_see_more_request(text):
        return True
    return bool(re.match(r"^show\s+(?:all\s+)?[^?]+\s+ministr(?:y|ies)\s+in\s+.+$", text))


def is_other_ministries_request(message):
    return bool(OTHER_MINISTRIES_RE.search(message or ""))


def deterministic_intent(message, state=None, history=None):
    state = state or {}
    text, _ = parse_see_more_offset(message)
    slots = _empty_slots()
    slots.update({
        "categories": match_categories(text),
        "locations": match_locations(text),
        "all_locations": is_all_locations_request(text),
        "ministry": match_ministry(text),
        "ministry_code": match_ministry_code(text),
    })
    negated_categories = {item.casefold() for item in match_negated_categories(text)}
    slots["categories"] = [
        item for item in slots["categories"]
        if item.casefold() not in negated_categories
    ]
    slots["category"] = slots["categories"][0] if slots["categories"] else None
    flow = _state_value(state, "flow", "idle")
    pending = _state_value(state, "pending") or _state_value(state, "step")
    last_result = _state_value(state, "last_result", []) or []
    recommendation_mode = _state_value(state, "recommendation_mode") or ""
    if is_underspecified_request(text, state):
        return IntentResult(
            "interrupt" if flow != "idle" else "new",
            "general_rag", "answer_general_question", .99, _empty_slots(), [],
            "deterministic", True,
        )
    clear = []
    for category in match_negated_categories(text):
        if category in slots["categories"] or CORRECTION_RE.search(text):
            clear.extend(["category", "categories"])
    if CORRECTION_RE.search(text):
        # A correction replaces only the dimension explicitly supplied.
        # This preserves a selected category when the user changes location,
        # and vice versa.
        if slots["categories"]:
            clear.extend(["category", "categories"])
        if slots["locations"] or slots["all_locations"]:
            clear.extend(["locations", "all_locations"])
        if slots["ministry"] or slots["ministry_code"]:
            clear.extend(["ministry", "ministry_code"])
    if text.lower() in {"give now", "donate now"}: return IntentResult("continue", "donation_flow", "give_now", .99, slots, clear, "deterministic")
    if "watch how to give" in text.lower() or "donation guide" in text.lower(): return IntentResult("continue", "donation_flow", "watch_donation_guide", .99, slots, clear, "deterministic")
    if text.casefold().rstrip(".") in {
        "no preference", "continue without a ministry category preference",
    }:
        if text.casefold().rstrip(".") == "no preference" and pending == "select_location":
            skipped_location = _empty_slots()
            skipped_location["all_locations"] = True
            return IntentResult("continue", "ministry_lookup", "search_ministry", .99, skipped_location, [], "deterministic")
        return IntentResult("continue", "ministry_lookup", "select_location", .99, _empty_slots(), [], "deterministic")
    if is_see_more_request(text):
        return IntentResult("continue", "ministry_lookup", "search_ministry", .99, slots, [], "deterministic")
    if text.casefold().rstrip(".") in {"see more", "show more", "view all"}:
        return IntentResult("continue", "ministry_lookup", "search_ministry", .99, slots, [], "deterministic")
    if text.casefold().rstrip(".") in {
        "help me choose",
        "help me choose a ministry",
        "help me choose a ministry to support",
        "explore ministries",
        "guidance to choose a ministry to donate",
    }:
        return IntentResult("continue", "donation_flow", "select_category", .99, slots, clear, "deterministic")
    # "More ministry descriptions" used to trigger pagination via a
    # recommendation_mode offset.  That mechanism is removed; the phrase is
    # now treated the same as any other result-expansion request.
    if is_other_ministries_request(text):
        # Do NOT classify as select_category — that wipes slots and restarts
        # the guided flow.  Route to search_ministry so the current
        # category/location is reused.
        return IntentResult("continue", "ministry_lookup", "search_ministry", .98, slots, clear, "deterministic")
    # A new explicit ministry name/code/category/location always wins over a
    # pronoun such as "this".  Vague details are backend-resolved from the
    # existing result set, never by an LLM-selected result.
    if (
        is_vague_ministry_detail_follow_up_with_state(text, slots, state)
        and not GENERAL_TOPIC_REQUEST_RE.search(text)
    ):
        return IntentResult("continue", "ministry_lookup", "search_ministry", .96, slots, clear, "deterministic")
    reference = result_reference_index(text, len(last_result))
    if reference is not None and not any(slots.get(key) for key in ("category", "categories", "locations", "all_locations", "ministry", "ministry_code")):
        return IntentResult("continue", "ministry_lookup", "search_ministry", .96, slots, clear, "deterministic")
    # Explicit topic requests must interrupt an active ministry flow. Without
    # this guard, the LLM classifier can interpret words such as "details" as
    # a request to reuse the previously selected ministry.
    if (
        GENERAL_TOPIC_REQUEST_RE.search(text)
        and not any(slots.get(key) for key in (
            "category", "categories", "locations", "all_locations", "ministry", "ministry_code"
        ))
    ):
        return IntentResult(
            "interrupt" if flow != "idle" else "new",
            "general_rag",
            "answer_general_question",
            .99,
            _empty_slots(),
            [],
            "deterministic",
        )
    if is_giving_guidance_request(text) and not slots["ministry"] and not slots["ministry_code"]:
        return IntentResult("new", "donation_flow", "show_donation_options", .99, _empty_slots(), [], "deterministic")
    if is_general_information_question(text) and not is_donation_query(text):
        return IntentResult("interrupt" if flow != "idle" else "new", "general_rag", "answer_general_question", .99, _empty_slots(), [], "deterministic")
    # A donor who names a ministry category without a location needs the
    # guided location prompt. Do not turn this into an all-locations lookup.
    if (
        slots["categories"]
        and not slots["locations"]
        and not slots["all_locations"]
        and is_donation_query(text)
    ):
        return IntentResult("new", "ministry_lookup", "select_location", .99, slots, clear, "deterministic")
    if slots["ministry"] or slots["ministry_code"] or slots["categories"] or slots["locations"] or slots["all_locations"]:
        return IntentResult("new" if flow != "ministry_lookup" else "continue", "ministry_lookup", "search_ministry", .95, slots, clear, "deterministic")
    if is_donation_query(text): return IntentResult("new", "donation_flow", "show_donation_options", .95, slots, clear, "deterministic")
    if (
        flow == "ministry_lookup"
        and pending in {"select_category", "select_location"}
        and ELLIPTICAL_FOLLOW_UP_RE.match(text)
        and not QUESTION_START_RE.match(text)
        and not is_gibberish(text)
        and (
            (pending == "select_category" and bool(slots["categories"]))
            or (pending == "select_location" and (bool(slots["locations"]) or bool(slots["all_locations"])))
        )
    ):
        return IntentResult("continue", "ministry_lookup", "continue_current_flow", .75, slots, clear, "deterministic")
    return IntentResult("new", "general_rag", "answer_general_question", .7, slots, clear, "deterministic")
def choose_intent(message, state, history=None, timeout=15):
    """Classify the current turn with deterministic failover.

    Deterministic parsing supplies validation and failure recovery, but it
    cannot decide whether an explicit new question temporarily interrupts an
    active flow. The state-aware classifier is therefore the primary router.
    """

    detected = deterministic_intent(message, state, history)
    if is_unsupported_creation_request(message):
        return IntentResult(
            "interrupt" if _state_value(state, "flow", "idle") != "idle" else "new",
            "general_rag", "answer_general_question", .99, _empty_slots(), [],
            "deterministic",
        )
    if is_giving_guidance_request(message):
        return detected
    if detected.needs_clarification:
        return detected
    if is_gibberish(message):
        return detected
    cleaned_text, see_more_offset = parse_see_more_offset(message)
    text = cleaned_text.casefold().rstrip(".")
    # API-controlled commands and authoritative codes must not depend on a
    # classifier's interpretation of a button or a duplicate ministry name.
    if detected.slots.get("ministry_code") or re.search(r"\bcode\s*(?::\s*|\s+)[a-z0-9-]+", text):
        return detected
    if is_api_controlled_message(message):
        return detected
    if is_general_information_question(message) and detected.intent == "general_rag":
        return detected
    # Explicit browse requests with validated filters are complete ministry
    # lookups. Do not let the classifier reinterpret them as the start of the
    # guided flow and ask the user to choose a location or category again.
    explicit_browse = bool(
        re.search(r"\b(?:show|list|find|search)\b", text)
        and not re.search(r"\b(?:remaining|more|other)\b", text)
    )
    if (
        detected.intent == "ministry_lookup"
        and detected.action == "search_ministry"
        and (
            (
                detected.slots.get("categories")
                and (
                    detected.slots.get("locations")
                    or detected.slots.get("all_locations")
                )
            )
            or (
                explicit_browse
                and (
                    detected.slots.get("categories")
                    or detected.slots.get("locations")
                    or detected.slots.get("all_locations")
                )
            )
        )
    ):
        return detected
    pending = _state_value(state, "pending") or _state_value(state, "step")
    if pending == "select_location" and detected.slots.get("locations") and not detected.slots.get("category"):
        return detected
    # The classifier may recognize the same wording, but this state-dependent
    # route must remain deterministic so it cannot select an item from a
    # previous result set.
    if is_vague_ministry_detail_follow_up_with_state(message, detected.slots, state):
        return detected
    # A category paired with donation wording, but no location, has a
    # prescribed guided-flow outcome. Bypass the classifier so it cannot
    # convert the request into an all-locations result or generic donation.
    if (
        detected.intent == "ministry_lookup"
        and detected.action == "select_location"
        and detected.slots["categories"]
        and not detected.slots["locations"]
        and is_donation_query(message)
    ):
        return detected
    try:
        classified = classify_intent(
            message,
            state,
            history,
            timeout=timeout,
            deterministic=detected,
        )
        if (
            classified.intent == "general_rag"
            and classified.confidence < 0.6
            and is_underspecified_request(message, state)
        ):
            return IntentResult(
                "interrupt" if _state_value(state, "flow", "idle") != "idle" else "new",
                "general_rag", "answer_general_question", .99, _empty_slots(), [],
                "deterministic", True,
            )
        return classified
    except IntentClassifierError:
        logger.warning("Intent classifier failed; falling back to deterministic routing")
        return detected
def classify_intent(message, state, history=None, *, timeout=15, deterministic=None):
    detected = deterministic or deterministic_intent(message, state, history)
    context = {
        "flow": _state_value(state, "flow"),
        "step": _state_value(state, "step"),
        "pending": _state_value(state, "pending"),
        "slots": _state_value(state, "slots", {}) or {},
        "last_query": _state_value(state, "last_query"),
        "last_result_count": len(_state_value(state, "last_result", []) or []),
        "recent_history": history or [],
        "user_message": message,
    }
    try: raw = chat_completion(INTENT_CLASSIFIER_SYSTEM_PROMPT, json.dumps(context, ensure_ascii=False), temperature=0, max_tokens=300, timeout=timeout)
    except (requests.RequestException, RuntimeError) as exc: raise IntentClassifierError(str(exc)) from exc
    parsed = _extract_json(raw)
    if not isinstance(parsed, dict): raise IntentClassifierError("malformed intent response")
    return merge_message_slots(validate_intent(parsed), message)


def merge_message_slots(result, message):
    """Union classifier slots with authoritative deterministic message matches."""
    if result.intent != "ministry_lookup":
        return result
    slots = dict(result.slots or {})
    llm_categories = slots.get("categories") or []
    if not llm_categories and slots.get("category"):
        llm_categories = [slots["category"]]
    message_categories = match_categories(message)
    categories = []
    for value in [*llm_categories, *message_categories]:
        if isinstance(value, str) and value.strip() and value.casefold() not in {item.casefold() for item in categories}:
            categories.append(value.strip())
    negated = {item.casefold() for item in match_negated_categories(message)}
    categories = [item for item in categories if item.casefold() not in negated]
    llm_locations = slots.get("locations") or ([] if not slots.get("location") else [slots["location"]])
    locations = []
    for value in [*llm_locations, *match_locations(message)]:
        if isinstance(value, str) and value.strip() and value.casefold() not in {item.casefold() for item in locations}:
            locations.append(value.strip())
    slots["categories"] = categories
    slots["category"] = categories[0] if categories else None
    slots["locations"] = locations
    slots["location"] = locations[0] if locations else None
    slots["all_locations"] = bool(slots.get("all_locations")) or is_all_locations_request(message)
    result.slots = slots
    return result


def validate_intent(raw, message=""):
    if not isinstance(raw, dict):
        raise IntentClassifierError("invalid intent payload")
    intent = str(raw.get("intent") or "").strip()
    if intent not in ALLOWED_INTENTS: raise IntentClassifierError("invalid intent")
    action = str(raw.get("action") or "").strip()
    source = "llm"
    if action not in ALLOWED_ACTIONS:
        action = {
            "ministry_lookup": "search_ministry",
            "donation_flow": "show_donation_options",
            "general_rag": "answer_general_question",
        }[intent]
        source = "fallback"

    given, slots = raw.get("slots") or {}, _empty_slots()
    if not isinstance(given, dict):
        given = {}
    raw_categories = given.get("categories", given.get("category", []))
    category_values = list(raw_categories) if isinstance(raw_categories, (list, tuple)) else [raw_categories]
    category_text = " ".join(str(item) for item in category_values if item)
    raw_locations = given.get("locations", given.get("location", []))
    location_text = (
        " ".join(str(item) for item in raw_locations)
        if isinstance(raw_locations, (list, tuple))
        else str(raw_locations or "")
    )
    slots["categories"] = match_categories(category_text)
    slots["category"] = slots["categories"][0] if slots["categories"] else None
    slots["locations"] = match_locations(location_text)
    slots["location"] = slots["locations"][0] if slots["locations"] else None
    slots["all_locations"] = bool(given.get("all_locations")) or is_all_locations_request(location_text)
    slots["ministry"] = match_ministry(str(given.get("ministry") or ""))
    supplied_code = str(given.get("ministry_code") or "").strip()
    exact_rows = find_ministry_by_code(supplied_code)
    slots["ministry_code"] = exact_rows[0]["Ministry code"] if exact_rows else None
    try: confidence = max(0., min(1., float(raw.get("confidence", 0))))
    except (TypeError, ValueError): confidence = 0.
    mode = raw.get("context_mode")
    if mode not in {"new", "continue", "interrupt"}:
        if raw.get("is_new_request") is True:
            mode = "new"
        elif raw.get("should_continue_current_flow") is True:
            mode = "continue"
        else:
            mode = "new"
    clear = [item for item in raw.get("clear_slots", []) if item in slots] if isinstance(raw.get("clear_slots"), list) else []
    return IntentResult(mode, intent, action, confidence, slots, clear, source)
def _extract_json(text):
    try: return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text or "", re.S)
        try: return json.loads(match.group()) if match else None
        except Exception: return None
