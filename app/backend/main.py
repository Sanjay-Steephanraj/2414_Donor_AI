import time
import sys
import json
import re
from functools import wraps
from urllib.parse import urlencode

from datetime import (
    datetime,
    timezone
)
from typing import Optional
from uuid import uuid4
from pathlib import Path
from fastapi import (
    FastAPI,
    Request
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

# Support local development from the repository root and the container entry point.
BASE_DIR = Path(__file__).resolve().parent
APP_DIR = BASE_DIR.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from pydantic import (
    AliasChoices,
    BaseModel,
    Field
)

from app_loggers import (
    get_logger,
    set_request_id
)

from rag_service import (
    API_KEY,
    determine_intent,
    is_donation_query,
    is_ministry_query,
    grounded_chat,
    grounded_response_has_sources,
    _answer_and_follow_ups,
    sounds_like_retriever,
    is_design_input_clarification,
    bot_response,
    DONATION_URL,
    DONOR_REGISTER_URL,
    DONATION_TUTORIAL_URL,
    DONATION_OPTION_HELP,
    FUND_GUIDANCE_MESSAGE,
    DONATION_OPTION_GIVE,
    DONATION_OPTION_WATCH,
    DONATION_OPTIONS,
    DONATION_SUGGESTIONS,
    DONATION_INTENT_MESSAGE,
    DONATION_GIVE_MESSAGE,
    DONATION_WATCH_MESSAGE,
    WELCOME_QUICK_REPLIES,
    PUBLISHER_NAME
)

from prompts import (
    NO_ANSWER_MESSAGE,
    WELCOME_MESSAGE,
    CONTACT_EMAIL,
    CONTACT_PHONE,
    CONTACT_ADDRESS,
    CONTACT_URL,
    UNSUPPORTED_CREATION_MESSAGE,
    MISSION_TRIP_URL,
    MISSION_TRIP_FALLBACK_MESSAGE,
)

from ministry_service import (
    find_ministries,
    find_ministry_by_name,
    find_ministry_by_code,
    get_all_ministries,
    build_category_suggestions,
    build_location_suggestions,
    build_no_result_alternatives,
    get_locations_for_category,
    format_list,
    match_category,
    match_categories,
    match_location,
    match_locations,
    match_ministry,
    match_ministry_code,
    get_all_categories,
    is_all_locations_request,
    is_gibberish,
    is_vague_donation_intent,
    extract_slots,
    is_general_information_question
)

from conversation_service import (
    get_conversation,
    update_conversation,
    update_slots,
    add_message,
    replace_last_assistant_message,
    get_recent_history,
    reset_flow,
    conversation_lock,
    FLOW_MINISTRY,
    FLOW_DONATION
)

from language_service import (
    is_non_english,
    extract_requested_output_language,
    NON_ENGLISH_MESSAGE,
    ENGLISH_ONLY_NOTICE_TEMPLATE,
    ENGLISH_ONLY_NOTICE_NO_QUERY,
)

from intent_service import (
    choose_intent,
    deterministic_intent,
    IntentClassifierError,
    result_reference_index,
    parse_see_more_offset,
    is_see_more_request,
    is_ministry_detail_follow_up,
    is_vague_ministry_detail_follow_up,
    is_vague_ministry_detail_follow_up_with_state,
    is_other_ministries_request,
    is_underspecified_request,
    is_unsupported_creation_request,
    is_giving_guidance_request,
)
from scope_service import evaluate_scope
from response_guard import has_meta_commentary, strip_meta_commentary
from fund_service import (
    build_fund_interest_suggestions, match_fund_interest, match_fund_name,
    is_fund_guidance_request, fund_by_key, fund_candidates, fund_display_name, FUNDS,
)

# ============================================================
# Logger
# ============================================================

logger = get_logger()

EXPLICIT_MINISTRY_CODE_RE = re.compile(
    r"(?<!\w)code(?:\s*:\s*|\s+)([A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)(?![\w-])",
    re.IGNORECASE,
)


def _extract_explicit_ministry_code(message: str) -> Optional[str]:
    match = EXPLICIT_MINISTRY_CODE_RE.search(message or "")
    if match:
        candidate = match.group(1)
        # In prose, 'code and how ...' is not an unknown ministry identifier.
        # A colon is explicit; unmarked unknown codes must be code-shaped.
        if ":" in match.group(0) or any(char.isdigit() for char in candidate) or find_ministry_by_code(candidate):
            return candidate
    return match_ministry_code(message)


def _is_prayer_request_link_request(message: str) -> bool:
    text = " ".join((message or "").casefold().replace("-", " ").split())
    if "prayer request" not in text and "prayer requests" not in text:
        return False
    return any(
        phrase in text
        for phrase in (
            "link", "url", "form", "submit", "share", "send", "provide",
            "where can", "how can", "where do i",
        )
    )


def _is_mission_trip_query(message: str) -> bool:
    text = " ".join((message or "").casefold().replace("-", " ").split())
    return bool(re.search(r"\bmission(?:ary)?\s+trips?\b", text))


def _mission_trip_fallback(req, session_id: str):
    return bot_response(
        user_message=req.message,
        answer=MISSION_TRIP_FALLBACK_MESSAGE,
        data={},
        follow_ups=[],
        urls=[MISSION_TRIP_URL],
        session_id=session_id,
        status="answered",
    )


def _mission_trip_answer_indicates_unavailable(answer: str) -> bool:
    text = " ".join((answer or "").casefold().split())
    return bool(re.search(
        r"\b(?:no|not|none|without)\s+(?:current\s+|upcoming\s+|available\s+)?"
        r"(?:mission\s+)?trips?\b|\b(?:currently|at\s+this\s+time)\b.*\b(?:no|not|none|unavailable)\b",
        text,
    ))


CONTACT_TOPIC_SUGGESTIONS = [
    {"label": "General question", "message": "How can I contact 24:14 World with a general question?"},
    {"label": "Ministry partnership", "message": "How can a ministry partner with 24:14 World?"},
    {"label": "Donation support", "message": "How can I get help with a 24:14 World donation?"},
    {"label": "Prayer and engagement", "message": "How can I pray for or engage with 24:14 World?"},
]
CONTACT_TOPIC_MESSAGES = frozenset(
    choice["message"].casefold() for choice in CONTACT_TOPIC_SUGGESTIONS
)
DONATION_SELECTION_REMINDER = (
    "Before completing your gift, please verify that the ministry or fund "
    "pre-selected on the donation page is the one you intended to support."
)


def _build_donation_resources(recipients):
    unique = []
    seen = set()
    for recipient in recipients:
        name = str(recipient.get("name") or "").strip()
        code = str(recipient.get("code") or "").strip()
        if not name:
            continue
        identity = ("code", code.casefold()) if code else ("name", name.casefold())
        if identity not in seen:
            seen.add(identity)
            unique.append({"name": name, "code": code})

    links = []
    coded = []
    warnings = []
    needs_general_link = not unique
    for recipient in unique:
        name, code = recipient["name"], recipient["code"]
        if code:
            url = DONATION_URL + "?" + urlencode({"code": code})
            links.append({
                "label": f"Open giving page for {name} (Code: {code})",
                "url": url,
            })
            coded.append(recipient)
        else:
            needs_general_link = True
            warnings.append(
                f"No donation code is available for {name}. "
                "The general giving link does not preselect it. Confirm "
                "the correct designation with 24:14 World before donating."
            )

    if len(unique) == 1 and len(coded) == 1:
        links[0]["label"] = "Donate now"

    if needs_general_link:
        # Preserve the established client label for the unqualified donation
        # URL while still exposing recipient-specific labels when available.
        links.append({"label": "Donate now", "url": DONATION_URL})

    note = ""
    if len(coded) > 1:
        note = (
            "Each link opens the donation page with one ministry or fund "
            "code in its URL. To give "
            "to these ministries or funds in one checkout, "
            "open one link, click \"Add another Donation Code\", and select "
            "the other code(s) listed below if they are available. Enter an "
            "amount for each and "
            "review all selections before donating. You do not need to "
            "open every link.\n\nDonation codes:\n"
            + "\n".join(
                f"- {item['name']} (Code: {item['code']})" for item in coded
            )
        )

    if coded:
        warnings.append(DONATION_SELECTION_REMINDER)

    return {
        "links": links,
        "urls": [link["url"] for link in links],
        "note": note,
        "warnings": warnings,
    }


def _append_donation_notes(answer, resources):
    additions = [resources["note"], *resources["warnings"]]
    return answer + "".join("\n\n" + text for text in additions if text)


def _row_recipient(row):
    return {"name": str(row.get("Ministry name") or "").strip(),
            "code": str(row.get("Ministry code") or "").strip()}


def _fund_recipient(fund):
    return {"name": fund.name, "code": fund.ministry_code}


def _resource_arguments(recipients, extra_links=()):
    resources = _build_donation_resources(recipients)
    links = resources["links"] + list(extra_links)
    return resources, {"resource_links": links,
                       "urls": list(dict.fromkeys(link["url"] for link in links))}


def _set_giving_selection(session_id, *, funds=(), rows=()):
    keys = [fund.key for fund in funds]
    codes = [str(row.get("Ministry code") or "").strip() for row in rows]
    update_conversation(
        session_id, flow=FLOW_DONATION, step="fund_selected", pending=None,
        slots={"selected_funds": keys, "selected_ministry_codes": codes,
               "ministry": rows[0].get("Ministry name") if len(rows) == 1 else None,
               "category": None, "locations": [], "all_locations": False,
               "category_preference_skipped": False},
        last_result=list(rows), last_query_criteria={},
        last_intent="donation_flow", last_action="show_donation_options",
    )


def _donate_now_options(session_id, rows):
    """Return the single-ministry donation resource and card metadata."""

    rows = list(rows or [])
    for row in rows:
        code = str(row.get("Ministry code") or "").strip()
        row["donation_url"] = (
            DONATION_URL + "?" + urlencode({"code": code}) if code else None
        )

    if len(rows) != 1:
        return [], []

    row = rows[0]
    code = str(row.get("Ministry code") or "").strip()
    if not code:
        return [], []

    _, arguments = _resource_arguments([_row_recipient(row)])
    return [], arguments["resource_links"]


def _selected_giving_recipients(session_id):
    slots = get_conversation(session_id).slots
    keys = slots.get("selected_funds") or ([slots["selected_fund"]] if slots.get("selected_fund") else [])
    codes = slots.get("selected_ministry_codes") or ([slots["ministry_code"]] if slots.get("ministry_code") else [])
    recipients, unresolved = [], []
    for key in keys:
        fund = fund_by_key(key)
        if fund:
            recipients.append(_fund_recipient(fund))
        else:
            unresolved.append(key)
    for code in codes:
        rows = find_ministry_by_code(code)
        if len(rows) == 1:
            recipients.append(_row_recipient(rows[0]))
        else:
            unresolved.append(code)
    return recipients, unresolved


def _unresolved_giving_response(message, session_id, unresolved):
    update_conversation(session_id, pending="giving_recipient_clarification")
    return bot_response(
        message,
        "I couldn't resolve the complete donation selection. Please confirm the exact ministry or fund codes: "
        + ", ".join(unresolved) + ". No donation links have been created for this selection.",
        {}, session_id=session_id, status="needs_clarification",
        follow_ups=[
            {"label": "Help me choose a ministry", "message": "Help me choose a ministry to support."},
            {"label": "Show all ministries", "message": "Show ministries in all locations"},
        ],
    )


def _giving_code_references(message):
    # Only full code-shaped tokens or exact known codes; never a code prefix.
    hits = list(re.finditer(r"(?<![\w-])\d+[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*(?![\w-])", message))
    references = [(match.start(), match.end(), match.group()) for match in hits]
    known_codes = {str(row.get("Ministry code") or "").strip() for row in get_all_ministries()}
    known_codes.update(fund.ministry_code for fund in FUNDS if fund.ministry_code)
    for code in known_codes:
        if not code:
            continue
        for match in re.finditer(r"(?<![\w-])" + re.escape(code) + r"(?![\w-])", message, re.I):
            if not any(match.start() < right and match.end() > left for left, right, _ in references):
                references.append((match.start(), match.end(), code))
    result = []
    seen = set()
    for start, end, code in sorted(references):
        if code.casefold() not in seen:
            result.append(code)
            seen.add(code.casefold())
    return result


def _route_explicit_multi_giving(message, session_id):
    # This route handles explicit giving, not detail/search/comparison questions.
    if not re.search(r"\b(?:donate|give|giving|contribute)\b|\b(?:i|we)\s+want\s+to\s+support\b", message, re.I):
        return None
    if re.search(r"\bcompare\b|\b(?:tell|give|show)\s+me\s+(?:details|information|about)\b", message, re.I):
        return None
    codes = _giving_code_references(message)
    funds = fund_candidates(message)
    if not codes and len(funds) < 2:
        named = {str(row.get("Ministry name") or "").strip() for row in get_all_ministries()}
        named = [name for name in named if name and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", message, re.I)]
        if len(named) > 1:
            return _unresolved_giving_response(message, session_id, ["the exact codes for " + ", ".join(sorted(named))])
    if len(codes) + len(funds) < 2:
        return None
    if codes and re.search(r"\b(?:or|either|maybe)\b", message, re.I):
        return _unresolved_giving_response(message, session_id, ["whether you want both codes or only one"])
    if re.search(r"\b(?:not|without|except|exclude|avoid|don't|do not)\b", message, re.I) and codes:
        return _unresolved_giving_response(message, session_id, ["which of the mentioned codes you want to support"])
    rows, unresolved = [], []
    for code in codes:
        matches = find_ministry_by_code(code)
        mapped = next((fund for fund in FUNDS if fund.ministry_code and fund.ministry_code.casefold() == code.casefold()), None)
        if len(matches) == 1:
            rows.append(matches[0])
        elif not matches and mapped:
            if mapped not in funds:
                funds.append(mapped)
        else:
            unresolved.append(code)
    if unresolved:
        return _unresolved_giving_response(message, session_id, unresolved)
    # If a mapped fund code is also explicitly present, one resolved target wins.
    row_codes = {row["Ministry code"].casefold() for row in rows}
    funds = [fund for fund in funds if not fund.ministry_code or fund.ministry_code.casefold() not in row_codes]
    if not codes:
        return None  # The all-fund route supplies the existing fund explanations.
    _set_giving_selection(session_id, funds=funds, rows=rows)
    recipients = [_fund_recipient(fund) for fund in funds] + [_row_recipient(row) for row in rows]
    resources, arguments = _resource_arguments(recipients)
    answer = "You selected these donation recipients:\n\n" + "\n".join(
        "- " + item["name"] + (f" (Code: {item['code']})" if item["code"] else "") for item in recipients
    )
    return bot_response(
        message, _append_donation_notes(answer, resources), {},
        session_id=session_id, **arguments,
        follow_ups=[{"label": "Donate now", "message": "Give now"}],
        ministries=[{"name": row["Ministry name"], "code": row["Ministry code"], "location": row.get("Location", "")} for row in rows],
    )


def _route_selected_give_now(message, session_id):
    if message.strip().casefold().rstrip(".") not in {"give now", "donate now"}:
        return None
    state = get_conversation(session_id)
    if state.pending == "giving_recipient_clarification":
        response = _unresolved_giving_response(message, session_id, ["your intended donation recipients"])
        add_message(session_id, "assistant", _response_text(response))
        return response
    recipients, unresolved = _selected_giving_recipients(session_id)
    if not recipients and not unresolved:
        return None
    # Keep the selected-recipient route ahead of stale ministry/classifier slots.
    from types import SimpleNamespace
    return _handle_donation_flow(
        ChatRequest(message=message, session_id=session_id), session_id,
        SimpleNamespace(intent="donation_flow", action="give_now"),
    )


# Transitional adapters for the older routing helpers.  They derive their
# values exclusively from conversation_service; no second session store exists.
def get_session(session_id: str) -> dict:
    state = get_conversation(session_id)
    locations = list(state.slots.get("locations") or [])
    return {
        "category": state.slots.get("category"),
        "categories": list(state.slots.get("categories") or []),
        # ``location`` remains for legacy callers; ``locations`` is the
        # canonical complete selection used for multi-location searches.
        "location": locations[0] if locations else None,
        "locations": locations,
        "active": state.flow == FLOW_MINISTRY,
    }


def update_session(session_id: str, **fields) -> dict:
    slots = {}
    if "category" in fields:
        slots["category"] = fields["category"]
    if "categories" in fields:
        slots["categories"] = fields["categories"]
    if "location" in fields:
        slots["locations"] = [fields["location"]] if fields["location"] else []
    if "locations" in fields:
        raw_locations = fields["locations"]
        slots["locations"] = list(raw_locations or []) if isinstance(raw_locations, (list, tuple)) else []
    if "all_locations" in fields:
        slots["all_locations"] = bool(fields["all_locations"])
    if slots:
        update_slots(session_id, **slots)
    return get_session(session_id)

# ============================================================
# FastAPI App
# ============================================================

app = FastAPI(
    title="Gloo Grounded Chat API"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://2414ai.thearkofrevival.com"
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"]
)

# The conversation tester is served from the same application as the API, so
# the browser uses same-origin requests for /api/welcome and /api/chat.
FRONTEND_FILE = BASE_DIR / "index.html"
if not FRONTEND_FILE.is_file():
    FRONTEND_FILE = APP_DIR / "frontend" / "static" / "index.html"

# ============================================================
# Middleware
# ============================================================

@app.middleware("http")

async def add_request_logging(
    request: Request,
    call_next
):

    request_id = set_request_id()

    logger.info(
        f"Incoming request | "
        f"method={request.method} "
        f"path={request.url.path}"
    )

    start_time = time.time()

    try:

        response = await call_next(request)

        duration = round(
            time.time() - start_time,
            3
        )

        logger.info(
            f"Request completed | "
            f"status={response.status_code} "
            f"duration={duration}s"
        )

        response.headers[
            "X-Request-ID"
        ] = request_id

        return response

    except Exception as e:

        logger.exception(
            f"Unhandled exception: {str(e)}"
        )

        raise

# ============================================================
# Request Model
# ============================================================

class ChatRequest(BaseModel):

    message: str = Field(
        validation_alias=AliasChoices(
            "message",
            "question"
        )
    )

    session_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "session_id",
            "sessionId"
        )
    )

# ============================================================
# Health Endpoint
# ============================================================

@app.get("/api/health")

def health():

    logger.info(
        "Health endpoint called"
    )

    return {
        "status": "ok",
        "publisher_name": PUBLISHER_NAME,
        "api_key_configured": bool(API_KEY),
    }

# ============================================================
# Readiness Endpoint
# ============================================================

@app.get("/api/ready")

def ready():

    logger.info(
        "Ready endpoint called"
    )

    return {
        "ready": True
    }

# ============================================================
# Liveness Endpoint
# ============================================================

@app.get("/api/live")

def live():

    logger.info(
        "Live endpoint called"
    )

    return {
        "live": True
    }

# ============================================================
# Welcome Endpoint
# ============================================================

@app.get("/api/welcome")

def welcome():

    logger.info(
        "Welcome endpoint called"
    )

    return {
        "id": str(uuid4()),
        "sender": "bot",
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat(),
        "message": WELCOME_MESSAGE,
        "suggestions": WELCOME_QUICK_REPLIES
    }

# ============================================================
# Chat Endpoint
# ============================================================

_ADDITIVE_FUND_RE = re.compile(
    r"^\s*(?:and|what\s+about)\b|"
    r"\b(?:also|too|another\s+cause|in\s+addition(?:\s+to)?|"
    r"as\s+well(?:\s+as)?|add|plus|along\s+with|together\s+with|both)\b",
    re.IGNORECASE,
)
_REPLACE_FUND_RE = re.compile(
    r"\b(?:instead(?:\s+of)?|rather\s+than|replace|"
    r"switch(?:ing)?\s+to|change\s+to|only|not(?!\s+only\b))\b",
    re.IGNORECASE,
)
_FUND_INFORMATION_RE = re.compile(
    r"\b(?:tell\s+me\s+(?:about|more)|what\s+(?:is|does)|"
    r"how\s+does|explain|learn\s+about|want\s+to\s+know\s+about)\b",
    re.IGNORECASE,
)


def _replacement_fund_candidates(message, funds):
    """Choose the affirmative cause, not a cause explicitly being replaced."""

    match = re.search(r"\breplace\b.*?\bwith\b|\b(?:switch|change)\b.*?\bto\b", message, re.I)
    if match:
        wanted = fund_candidates(message[match.end():])
        return wanted or funds

    contrast = re.search(r"\b(?:instead\s+of|rather\s+than)\b", message, re.I)
    if contrast:
        wanted_before = fund_candidates(message[:contrast.start()])
        if wanted_before:
            return wanted_before
        after = re.split(r"[,;]|\bbut\b", message[contrast.end():], maxsplit=1, flags=re.I)
        return fund_candidates(after[1]) if len(after) > 1 else []

    return funds


def _route_deterministic_fund_guidance(message: str, session_id: str) -> Optional[dict]:
    """Return a prescribed fund response without invoking the classifier."""

    normalized_message = (message or "").strip().casefold().rstrip(".")
    if normalized_message in {
        DONATION_OPTION_HELP.casefold(),
        FUND_GUIDANCE_MESSAGE.casefold(),
    }:
        update_conversation(
            session_id,
            flow=FLOW_DONATION,
            step="fund_interest",
            pending="fund_interest",
            slots={
                "category": None,
                "locations": [],
                "all_locations": False,
                "ministry": None,
                "ministry_code": None,
                "selected_fund": None,
                "selected_funds": [],
                "category_preference_skipped": False,
            },
            last_result=[],
            last_query_criteria={},
            last_intent="donation_flow",
            last_action="show_donation_options",
        )
        response = bot_response(
            message,
            "What moves you most? Choose an area below, or describe what you feel called to support.",
            {},
            follow_ups=build_fund_interest_suggestions(),
            session_id=session_id,
            status="needs_clarification",
        )
    else:
        funds = fund_candidates(message)
        if not funds or _extract_explicit_ministry_code(message):
            return None
        # Keep a single named ministry's existing exact lookup, but never
        # discard other approved funds in an explicit multi-cause request.
        if len(funds) == 1 and match_ministry(message):
            return None
        fund = funds[0]
        state = get_conversation(session_id)
        awaiting_interest = (
            state.pending == "fund_interest"
            and not is_general_information_question(message)
        )
        previous_keys = state.slots.get("selected_funds") or (
            [state.slots["selected_fund"]] if state.slots.get("selected_fund") else []
        )
        selection_active = state.flow == FLOW_DONATION and bool(previous_keys)
        replacement_cue = (
            bool(_REPLACE_FUND_RE.search(message))
            and not re.search(r"\bnot\s+only\b", message, re.I)
        )
        replacement = selection_active and replacement_cue
        additive = (
            selection_active
            and bool(_ADDITIVE_FUND_RE.search(message))
            and not replacement
        )
        # An informational follow-up about a cause is not an instruction to
        # add it to the donor's selected recipients.
        if _FUND_INFORMATION_RE.search(message) and not re.search(
            r"\b(?:donate|give|support|add|include|select|choose)\b", message, re.I
        ):
            return None
        if replacement_cue:
            funds = _replacement_fund_candidates(message, funds)
            if not funds:
                return None
        explicit_multi = len(funds) > 1 and bool(re.search(r"^\s*both\b", message, re.I))
        if not (additive or replacement or explicit_multi or is_fund_guidance_request(
            message, fund, awaiting_interest=awaiting_interest
        )):
            return None
        if additive:
            previous_funds = [fund_by_key(key) for key in previous_keys]
            funds = list(dict.fromkeys(
                item for item in (*previous_funds, *funds) if item is not None
            ))
        fund = funds[0]

        update_conversation(
            session_id,
            flow=FLOW_DONATION,
            step="fund_selected",
            pending=None,
            slots={
                "category": None,
                "locations": [],
                "all_locations": False,
                "ministry": None,
                "ministry_code": None,
                "selected_fund": fund.key if len(funds) == 1 else None,
                "selected_funds": [item.key for item in funds],
                "category_preference_skipped": False,
            },
            last_result=[],
            last_query_criteria={},
            last_intent="donation_flow",
            last_action="show_donation_options",
        )
        resources, arguments = _resource_arguments([_fund_recipient(item) for item in funds])
        answer = (
            f"The best match is {fund_display_name(fund)}. {fund.explanation}"
            if len(funds) == 1 else
            "These funds match the causes you selected:\n\n" + "\n\n".join(
                f"{fund_display_name(item)}. {item.explanation}" for item in funds
            )
        )
        response = bot_response(
            message, _append_donation_notes(answer, resources), {},
            follow_ups=[{"label": "Donate now", "message": "Give now"}],
            session_id=session_id, **arguments,
        )

    add_message(session_id, "assistant", _response_text(response))
    return response


CONTACT_DETAIL_RE = re.compile(
    r"\b(?:e-?mail(?:\s+(?:address|id))?|phone(?:\s+number)?|telephone|"
    r"call\s+(?:you|us|them)|contact\s+(?:details|info|information|number)|"
    r"mailing\s+address|postal\s+address|(?:your|their|office|physical)\s+address|"
    r"(?:24\s*:\s*14|2414)(?:\s+world)?\s+(?:mailing\s+)?address|"
    r"reach\s+(?:you|us|them|out)|get\s+in\s+touch|how\s+(?:can|do|could)\s+i\s+contact)\b",
    re.IGNORECASE,
)
CONTACT_FORM_RE = re.compile(
    r"(?:\bcontact\s+(?:us\s+)?form\b|\bform\s+to\s+contact\b)"
    r".{0,40}\b(?:link|url|website|page|address)\b|"
    r"\b(?:link|url|website|page|address)\b.{0,40}"
    r"(?:\bcontact\s+(?:us\s+)?form\b|\bform\s+to\s+contact\b)",
    re.IGNORECASE,
)


def _route_contact_details(message: str, session_id: str) -> Optional[dict]:
    """Answer contact-detail questions from verified data, not retrieval."""
    is_contact_form_request = bool(CONTACT_FORM_RE.search(message or ""))
    if not is_contact_form_request and not CONTACT_DETAIL_RE.search(message or ""):
        return None
    # "my email" is usually an account question; a named ministry's contact
    # details are not in our data. Let the normal route handle both.
    if re.search(r"\bmy\s+(?:e-?mail|phone)", message, re.I) or match_ministry(message):
        return None
    if is_contact_form_request:
        response = bot_response(
            message,
            f"Here is the 24:14 World contact form: {CONTACT_URL}",
            {},
            follow_ups=CONTACT_TOPIC_SUGGESTIONS,
            resource_links=[{"label": "Contact form", "url": CONTACT_URL}],
            urls=[CONTACT_URL],
            session_id=session_id,
            status="answered",
        )
        add_message(session_id, "assistant", _response_text(response))
        return response

    answer = (
        "Here's how you can reach the 24:14 World team:\n\n"
        f"- Email: {CONTACT_EMAIL}\n"
        f"- Phone: {CONTACT_PHONE}\n"
        f"- Mail: {CONTACT_ADDRESS}\n\n"
        "You can also send us a message through the contact form on our website."
    )
    response = bot_response(
        message, answer, {},
        follow_ups=CONTACT_TOPIC_SUGGESTIONS,
        resource_links=[{"label": "Contact us", "url": CONTACT_URL}],
        urls=[CONTACT_URL],
        session_id=session_id,
        status="answered",
    )
    add_message(session_id, "assistant", _response_text(response))
    return response


def _duplicate_ministry_suggestions(rows):
    """Offer one code-specific choice for each same-named CSV record."""
    return [
        {
            "label": (
                f"{row.get('Ministry name', 'Ministry')} — "
                f"{row.get('Location') or 'Location not listed'} "
                f"(Code: {row.get('Ministry code', '')})"
            ),
            "message": (
                f"Tell me about {row.get('Ministry name', 'Ministry')} "
                f"(Code: {row.get('Ministry code', '')})"
            ),
        }
        for row in rows
    ]


def _route_duplicate_ministry_choice(message: str, session_id: str) -> Optional[dict]:
    """Resolve a typed choice from the duplicate-name choices just shown."""
    state = get_conversation(session_id)
    if state.pending != "select_duplicate_ministry":
        return None
    candidates = [row for row in state.last_result if isinstance(row, dict)]
    if len(candidates) < 2:
        update_conversation(session_id, pending=None)
        return None
    # Code-specific button messages retain the existing authoritative route.
    if _extract_explicit_ministry_code(message) or match_ministry(message):
        return None

    ordinals = re.findall(r"\b(?:first|second|third|last)\b", message, re.IGNORECASE)
    ordinal_choice = len(ordinals) == 1 and (
        len(message.split()) <= 4
        or re.search(r"\b(?:one|record|option|ministry|result)\b", message, re.IGNORECASE)
    )
    if ordinal_choice:
        index = result_reference_index(message, len(candidates))
        if index is not None:
            code = candidates[index].get("Ministry code")
            exact_rows = find_ministry_by_code(code) if code else []
            if exact_rows:
                update_conversation(session_id, pending=None)
                response = _exact_ministry_lookup(message, session_id, row=exact_rows[0])
                add_message(session_id, "assistant", _response_text(response))
                return response
    elif not (
        is_vague_ministry_detail_follow_up_with_state(message, {}, state)
        or re.fullmatch(r"\s*(?:(?:this|that)(?:\s+one)?|it)[.!?]?\s*", message, re.IGNORECASE)
    ):
        return None

    response = bot_response(
        message,
        "I found multiple ministries with that name. Please choose the exact record:",
        {},
        follow_ups=_duplicate_ministry_suggestions(candidates),
        session_id=session_id,
        status="needs_clarification",
    )
    add_message(session_id, "assistant", _response_text(response))
    return response

def _serialize_conversation_request(handler):
    """Apply each session's complete request lifecycle in order."""

    @wraps(handler)
    def wrapped(req: ChatRequest):
        req.session_id = req.session_id or str(uuid4())
        with conversation_lock(req.session_id):
            return handler(req)

    return wrapped


def _invalid_input_follow_ups(state):
    pending = state.pending or state.step
    if pending == "select_category":
        return build_category_suggestions()
    if pending == "select_location":
        return build_location_suggestions(state.slots.get("category"))
    return []


def _underspecified_response(req: ChatRequest, session_id: str):
    return bot_response(
        user_message=req.message,
        answer="Happy to help! Which links are you looking for?",
        data={},
        follow_ups=[
            {"label": "Donation links", "message": "Give now"},
            {"label": "Prayer links", "message": "How can I pray for or engage with 24:14 World?"},
            {"label": "Ministry links", "message": "Help me choose a ministry"},
            {"label": "Contact links", "message": "Contact and Partnership"},
            {"label": "About 24:14", "message": "Learn about 24:14"},
        ],
        urls=[],
        session_id=session_id,
        status="needs_clarification",
    )


_CREATION_CONTINUATION_RE = re.compile(
    r"(?:\b\d{3,5}\s*[x×]\s*\d{3,5}\b|"
    r"\b(?:mission|vision|brand(?:ing)?|dimension(?:s)?|size|resolution|"
    r"colour|color|style|layout|logo|template|website|web\s*page|image|"
    r"picture|poster|graphic|proceed|continue|blue)\b)",
    re.IGNORECASE,
)


def _creation_refusal_continuation(message: str, session_id: str) -> bool:
    """Keep short requirement-like continuations in the refusal path only."""

    text = " ".join((message or "").strip().split())
    if not text or len(text) > 220:
        return False
    history = get_recent_history(session_id)
    if not history:
        return False
    previous = history[-2] if history[-1].get("role") == "user" and len(history) >= 2 else history[-1]
    return (
        previous.get("role") == "assistant"
        and previous.get("content") == UNSUPPORTED_CREATION_MESSAGE
        and bool(_CREATION_CONTINUATION_RE.search(text))
    )


def _unsupported_creation_response(message, session_id):
    logger.info("Unsupported creation request intercepted | message=%r", message)
    return bot_response(
        user_message=message,
        answer=UNSUPPORTED_CREATION_MESSAGE,
        data={},
        follow_ups=WELCOME_QUICK_REPLIES,
        urls=[],
        session_id=session_id,
        ministries=[],
        status="out_of_scope",
    )


def _chat_inner(req: ChatRequest):

    logger.info(
        f"Question received: "
        f"{req.message}"
    )

    session_id = req.session_id or str(uuid4())

    state = get_conversation(session_id)
    prior_flow = state.flow
    prior_step = state.step

    clean_for_history, _ = parse_see_more_offset(req.message)
    add_message(session_id, "user", clean_for_history)

    # Contact-form requests are fully deterministic and must be answered
    # before the scope/LLM gates can misclassify a short phrase such as
    # "Contact form link" as an underspecified ministry request.
    contact_response = _route_contact_details(req.message, session_id)
    if contact_response is not None:
        return contact_response

    scope_result = evaluate_scope(req.message, state, get_recent_history(session_id))
    if (
        scope_result.scope != "in_scope"
        and scope_result.confidence >= 0.6
        and not is_giving_guidance_request(req.message)
    ):
        logger.info(
            "Scope gate blocked request | session_id=%s message=%r scope=%s confidence=%s source=%s",
            session_id, req.message, scope_result.scope,
            scope_result.confidence, scope_result.source,
        )
        if scope_result.scope == "creation_request":
            response = _unsupported_creation_response(req.message, session_id)
        elif scope_result.scope == "out_of_scope":
            response = bot_response(
                req.message,
                "I can only help with questions about 24:14 World. Could you please rephrase your question so it relates to 24:14 World?",
                {}, session_id=session_id, status="out_of_scope",
                follow_ups=WELCOME_QUICK_REPLIES,
            )
        else:
            response = _underspecified_response(req, session_id)
        add_message(session_id, "assistant", _response_text(response))
        return response

    if _is_prayer_request_link_request(req.message):
        response = _run_general_rag(req, session_id)
        update_conversation(
            session_id,
            last_intent="general_rag",
            last_action="answer_general_question",
        )
        add_message(session_id, "assistant", _response_text(response))
        return response

    for giving_route in (_route_selected_give_now, _route_explicit_multi_giving):
        giving_response = giving_route(req.message, session_id)
        if giving_response is not None:
            if giving_route is not _route_selected_give_now:
                add_message(session_id, "assistant", _response_text(giving_response))
            return giving_response

    duplicate_choice = _route_duplicate_ministry_choice(req.message, session_id)
    if duplicate_choice is not None:
        return duplicate_choice

    prior_slots = dict(state.slots)

    # The fund-guidance prompt and all approved fund interests are fully
    # deterministic. Handle them before choose_intent(), which can call Gloo.
    fund_response = _route_deterministic_fund_guidance(req.message, session_id)
    if fund_response is not None:
        return fund_response

    if (
        is_unsupported_creation_request(req.message)
        or _creation_refusal_continuation(req.message, session_id)
    ):
        response = _unsupported_creation_response(req.message, session_id)
        add_message(session_id, "assistant", _response_text(response))
        return response

    if is_underspecified_request(req.message, state):
        logger.info("Underspecified request intercepted | message=%r", req.message)
        response = _underspecified_response(req, session_id)
        add_message(session_id, "assistant", _response_text(response))
        return response

    if is_gibberish(req.message):
        logger.info("Invalid input intercepted before intent routing | session_id=%s message=%r", session_id, req.message)
        response = bot_response(
            user_message=req.message,
            answer="I didn't quite understand that. Could you please rephrase your question about 24:14 World?",
            data={},
            follow_ups=_invalid_input_follow_ups(state),
            urls=[],
            session_id=session_id,
            status="needs_clarification",
        )
        add_message(session_id, "assistant", _response_text(response))
        return response

    classified = None
    intent = None
    action = None
    confidence = None
    validated_slots = {}

    try:
        state_payload = {
            "flow": state.flow,
            "step": state.step,
            "pending": state.pending,
            "slots": dict(state.slots),
            "last_query": state.last_query,
            "last_query_criteria": dict(state.last_query_criteria or {}),
            "last_list_criteria": dict(state.last_list_criteria or {}),
            "last_result": list(state.last_result),
            "recommendation_mode": state.recommendation_mode,
        }
        history = get_recent_history(session_id)
        classified = choose_intent(
            message=req.message,
            state=state_payload,
            history=history
        )
        intent = classified.intent
        action = classified.action
        confidence = classified.confidence
        validated_slots = {
            key: classified.slots.get(key)
            for key in ("category", "categories", "locations", "ministry", "ministry_code")
        }
    except IntentClassifierError:
        logger.warning(
            "Intent classifier failed; falling back to deterministic routing"
        )

    try:
        if classified is not None:
            response = _route_by_intent(
                req=req,
                session_id=session_id,
                classified=classified
            )
            final_route = response.get("status", "unknown")
        else:
            final_route = "deterministic_fallback"
            response = _deterministic_route(req, session_id)
            _sync_legacy_session_to_conversation(session_id)
            add_message(session_id, "assistant", _response_text(response))
            return response
    except Exception:
        logger.exception(
            "Unhandled error while routing chat request; returning safe fallback response"
        )
        response = bot_response(
            user_message=req.message,
            answer=NO_ANSWER_MESSAGE,
            data={},
            follow_ups=[],
            urls=[],
            session_id=session_id,
            status="failed",
        )
        add_message(session_id, "assistant", _response_text(response))
        final_route = "safe_fallback"

    new_state = get_conversation(session_id)
    logger.info(
        "Routing | "
        f"session_id={session_id} "
        f"prior_flow={prior_flow} "
        f"prior_step={prior_step} "
        f"message={req.message!r} "
        f"intent={intent} "
        f"action={action} "
        f"confidence={confidence} "
        f"validated_slots={validated_slots} "
        f"route={final_route} "
        f"new_flow={new_state.flow} "
        f"new_step={new_state.step}"
    )

    return response


def _route_by_intent(req: ChatRequest, session_id: str, classified):
    """Apply backend-controlled state transitions after classifier success."""

    message = (req.message or "").strip()
    lowered = message.casefold()
    category_preference_message = lowered.rstrip(".")
    if category_preference_message == "continue without a ministry category preference" or (
        category_preference_message == "no preference"
        and get_conversation(session_id).pending != "select_location"
    ):
        update_conversation(
            session_id,
            flow=FLOW_MINISTRY,
            step="select_location",
            pending="select_location",
            slots={
                "category": None, "locations": [], "all_locations": False,
                "ministry": None, "ministry_code": None, "selected_fund": None,
                "category_preference_skipped": True,
            },
            last_result=[], last_query_criteria={},
            last_intent="ministry_lookup", last_action="select_location",
        )
        response = _ministry_flow(session_id, None, None)
        add_message(session_id, "assistant", _response_text(response))
        return response
    # Religious affiliation is not a ministry-data field. Keep this separate
    # from the approved donor-interest mapping for Muslim outreach.
    if ("ministr" in lowered and re.search(r"\bserving\s+(?:muslims?|christians?|hindus?)\b", lowered)):
        response = bot_response(message, "I can help you find ministries by the work they do or where they serve. Which would you like to explore?", {}, session_id=session_id, status="needs_clarification")
        add_message(session_id, "assistant", _response_text(response))
        return response
    # An explicit code is authoritative and must be resolved before the
    # classifier/name path can produce duplicate-name clarification.
    explicit_code = _extract_explicit_ministry_code(message)
    if explicit_code:
        exact_rows = find_ministry_by_code(explicit_code)
        if exact_rows:
            update_slots(session_id, ministry=None, ministry_code=explicit_code)
            update_conversation(
                session_id,
                flow=FLOW_MINISTRY,
                step="show_results",
                pending=None,
                last_intent="ministry_lookup",
                last_action="search_ministry",
            )
            response = _exact_ministry_lookup(message, session_id, row=exact_rows[0])
            add_message(session_id, "assistant", _response_text(response))
            return response

    # Detect a code-shaped reference even when it is absent from the CSV.
    unknown_code_value = explicit_code
    if unknown_code_value and not find_ministry_by_code(unknown_code_value):
        recovery_actions = [
            {
                "label": "Help me choose a ministry",
                "message": "Help me choose a ministry to support.",
            },
            {
                "label": "Show all ministries",
                "message": "Show ministries in all locations",
            },
        ]
        response = bot_response(message, f"I couldn't find a ministry with code {unknown_code_value.upper()}. Please check the code and try again.", {}, follow_ups=recovery_actions, session_id=session_id, status="no_result")
        add_message(session_id, "assistant", _response_text(response))
        return response
    root_option = message.casefold()
    if root_option in {"show ministries in all locations", "show all ministries in all locations"}:
        update_slots(session_id, category=None, locations=[], all_locations=True, ministry=None, ministry_code=None, selected_fund=None, category_preference_skipped=True)
        update_conversation(session_id, flow=FLOW_MINISTRY, step="show_results", pending=None, last_intent="ministry_lookup", last_action="search_ministry")
        response = _ministry_flow(
            session_id,
            None,
            [],
            show_all=root_option == "show all ministries in all locations",
        )
        add_message(session_id, "assistant", _response_text(response))
        return response
    if root_option in {"impact stories", "show me impact stories"}:
        response = bot_response(
            req.message,
            "Impact Stories are not currently offered here. "
            "I can help you learn about 24:14 World, choose a ministry, or give.",
            {}, follow_ups=WELCOME_QUICK_REPLIES, session_id=session_id,
            status="out_of_scope",
        )
        add_message(session_id, "assistant", _response_text(response))
        return response
    if root_option in {"learn about 24:14", "contact and partnership"}:
        answers = {
            "learn about 24:14": "24:14 World is a movement focused on seeing a thriving church for every people and place. What would you like to learn about?",
            "contact and partnership": "We'd be glad to help with contact or partnership questions. Please tell us what kind of partnership you have in mind.",
        }
        learn_suggestions = [
            {"label": "Vision and mission", "message": "What are 24:14 World's vision and mission?"},
            {"label": "Native ministry", "message": "What is native ministry at 24:14 World?"},
            {"label": "10/40 Window", "message": "What is the 10/40 Window, and how does 24:14 World serve there?"},
            {"label": "Crisis response", "message": "How does 24:14 World respond to crises?"},
            {"label": "How 24:14 works", "message": "How does 24:14 World work?"},
        ]
        response = bot_response(
            req.message, answers[root_option], {}, session_id=session_id,
            follow_ups=learn_suggestions if root_option == "learn about 24:14" else CONTACT_TOPIC_SUGGESTIONS,
        )
        update_conversation(session_id, last_intent="general_rag", last_action="answer_general_question")
        add_message(session_id, "assistant", _response_text(response))
        return response
    if root_option in CONTACT_TOPIC_MESSAGES:
        response = _run_general_rag(req, session_id)
        update_conversation(session_id, last_intent="general_rag", last_action="answer_general_question")
        add_message(session_id, "assistant", _response_text(response))
        return response
    if message.lower() == "find another ministry":
        update_slots(session_id, category=None, locations=[], all_locations=False, ministry=None, ministry_code=None, selected_fund=None, category_preference_skipped=False)
        update_conversation(
            session_id,
            last_list_criteria={},
            flow=FLOW_MINISTRY,
            step="select_category",
            pending="select_category",
            last_intent="ministry_lookup",
            last_action="select_category",
        )
        response = _ministry_flow(session_id=session_id, category=None, location=None)
        add_message(session_id, "assistant", _response_text(response))
        return response

    state = get_conversation(session_id)

    should_reset = (
        classified.action == "restart_flow"
        or (
            classified.context_mode == "new"
            and classified.intent != state.flow
            and classified.intent != "general_rag"
        )
    )

    if should_reset:
        reset_flow(session_id)
        update_session(session_id, category=None, location=None, active=False)

    if classified.intent == "general_rag":
        return _handle_general_rag(req, session_id, classified)

    if classified.intent == "ministry_lookup":
        return _handle_ministry_lookup(req, session_id, classified)

    if classified.intent == "donation_flow":
        return _handle_donation_flow(req, session_id, classified)

    # Unknown intent should not happen after validate_intent, but stay safe.
    return _handle_general_rag(req, session_id, classified)


def _handle_general_rag(req: ChatRequest, session_id: str, classified):
    """Answer general questions while preserving active flows."""

    if is_unsupported_creation_request(req.message) or _creation_refusal_continuation(req.message, session_id):
        return _unsupported_creation_response(req.message, session_id)
    if is_underspecified_request(req.message, get_conversation(session_id)):
        logger.info("Underspecified request intercepted | message=%r", req.message)
        return _underspecified_response(req, session_id)

    det_categories = match_categories(req.message)
    det_locations = match_locations(req.message)

    if (
        not is_general_information_question(req.message)
        and (det_categories or det_locations)
        and find_ministries(det_categories, det_locations)
    ):
        logger.info(
            "Deterministic ministry override triggered in general_rag | "
            f"categories={det_categories!r} locations={det_locations!r}"
        )
        return _handle_ministry_lookup(
            req,
            session_id,
            classified,
            override_slots={
                "categories": det_categories,
                "category": det_categories[0] if det_categories else None,
                "locations": det_locations,
                "all_locations": False,
                "ministry": None,
                "ministry_code": None,
            }
        )

    response = _run_general_rag(req, session_id)

    state = get_conversation(session_id)
    update_conversation(
        session_id,
        last_intent=classified.intent,
        last_action=classified.action
    )
    add_message(session_id, "assistant", _response_text(response))

    return response


def _run_general_rag(req: ChatRequest, session_id: str):
    """Execute the existing grounded-RAG path, with CSV fallback on no_result."""

    from ministry_service import is_gibberish
    if is_unsupported_creation_request(req.message) or _creation_refusal_continuation(req.message, session_id):
        return _unsupported_creation_response(req.message, session_id)
    if is_gibberish(req.message):
        return bot_response(
            user_message=req.message,
            answer="I didn't quite understand that. Could you please rephrase your question about 24:14 World?",
            data={}, follow_ups=[], urls=[], session_id=session_id,
            status="needs_clarification",
        )

    if is_underspecified_request(req.message, get_conversation(session_id)):
        logger.info("Underspecified request intercepted | message=%r", req.message)
        return _underspecified_response(req, session_id)

    donation_query = is_donation_query(req.message)
    # Use the ministry-specific prompt only for record lookups. Broad questions
    # about 24:14's approach to native ministry partners are website-content
    # questions; sending them to the structured ministry prompt makes Gloo
    # expect a matching row in the ministry dataset and can yield a false
    # "JSON data unavailable" response.
    ministry_query = (
        is_ministry_query(req.message)
        and not is_general_information_question(req.message)
    )

    # Anchor these lookups to their indexed page titles because generic
    # queries can retrieve unrelated pages instead.
    grounding_question = req.message
    if _is_prayer_request_link_request(req.message):
        grounding_question = (
            "What is the URL of the Prayer Requests page on 24:14 World? "
            "Give the canonical URL, not a page title or a URL with spaces. "
            "User question: "
            + req.message
        )
    elif re.search(
        r"\b(?:address|mailing|postal|p\s*\.?\s*o\s*\.?\s*box|"
        r"contact(?:\s+(?:information|details))?|phone|telephone|email)\b",
        req.message,
        re.IGNORECASE,
    ):
        grounding_question = (
            "On the 'Contact us | 2414 World' page, what contact information "
            "is listed? Include the postal mailing address and telephone number "
            "if provided. User question: "
            + req.message
        )

    data = grounded_chat(
        grounding_question,
        donation_location=donation_query,
        ministry_query=ministry_query
    )

    status = "no_result"
    if data.get("no_answer"):
        answer = NO_ANSWER_MESSAGE
        follow_ups = []
        status = "failed" if data.get("status") == "failed" else "no_result"
    else:
        answer, follow_ups, status = _answer_and_follow_ups(data, user_message=req.message)
        answer = strip_meta_commentary(answer)
        if not answer:
            answer, follow_ups, status = NO_ANSWER_MESSAGE, [], "no_result"

        if sounds_like_retriever(answer) or answer.strip() == "NO_RELEVANT_CONTEXT":
            logger.info("Retriever-style or empty answer replaced with friendly fallback")
            answer, follow_ups, status = NO_ANSWER_MESSAGE, [], "no_result"

        if status == "out_of_scope" and answer == UNSUPPORTED_CREATION_MESSAGE:
            logger.warning("Model drift detected in general_rag; returning creation refusal")
            return _unsupported_creation_response(req.message, session_id)
        if status == "needs_clarification" and is_design_input_clarification(answer):
            logger.warning("Model asked for creative-asset requirements; returning creation refusal")
            return _unsupported_creation_response(req.message, session_id)

        if status == "answered" and not grounded_response_has_sources(data):
            logger.warning("General answer rejected: no native retrieval evidence")
            answer, follow_ups, status = NO_ANSWER_MESSAGE, [], "no_result"

        if (
            (status == "no_result" or answer.strip() == "")
            and not answer.startswith("NO_RELEVANT_CONTEXT")
            and ministry_query
            and not is_general_information_question(req.message)
        ):
            det_category = match_category(req.message)
            det_locations = match_locations(req.message)
            csv_matches = (
                find_ministries(det_category, det_locations)
                if det_category or det_locations else []
            )

            if csv_matches:
                logger.info(
                    "CSV fallback used for general_rag after no_result"
                )
                donate_follow_ups, donate_links = _donate_now_options(session_id, csv_matches)
                ministries = [
                    {
                        "name": row["Ministry name"],
                        "code": row["Ministry code"],
                        "location": row.get("Location", ""),
                        "donation_url": row.get("donation_url"),
                    }
                    for row in csv_matches
                ]
                return bot_response(
                    user_message=req.message,
                    answer=(
                        "Here are matching ministries from our records.\n\n"
                        + DONATION_SELECTION_REMINDER
                        if any(item["donation_url"] for item in ministries)
                        else "Here are matching ministries from our records."
                    ),
                    data={},
                    follow_ups=donate_follow_ups,
                    resource_links=donate_links + [{"label": "Watch how to give", "url": DONATION_TUTORIAL_URL}],
                    urls=[link["url"] for link in donate_links] + [DONATION_TUTORIAL_URL],
                    session_id=session_id,
                    ministries=ministries,
                    status="answered",
                )

        if "NO_RELEVANT_CONTEXT" in answer:
            logger.info(
                "Model reported no relevant context; returning default message"
            )
            answer = NO_ANSWER_MESSAGE
            follow_ups = []
            status = "no_result"

        if not answer.strip():
            answer = NO_ANSWER_MESSAGE
            follow_ups = []
            if status == "answered":
                status = "no_result"

    if (
        _is_mission_trip_query(req.message)
        and (
            status != "answered"
            or not answer.strip()
            or answer.strip() == "NO_RELEVANT_CONTEXT"
            or _mission_trip_answer_indicates_unavailable(answer)
        )
    ):
        logger.info("Mission-trip fallback used after no current opportunity was found")
        return _mission_trip_fallback(req, session_id)

    exact_grounded_rows = []
    if ministry_query:
        matched_code = match_ministry_code(req.message)
        matched_name = match_ministry(req.message)
        if matched_code:
            exact_grounded_rows = find_ministry_by_code(matched_code)
        elif matched_name:
            exact_grounded_rows = find_ministry_by_name(matched_name)
        if len(exact_grounded_rows) != 1:
            exact_grounded_rows = []

    grounded_donate_follow_ups, grounded_donate_links = _donate_now_options(
        session_id, exact_grounded_rows
    )
    if grounded_donate_follow_ups:
        follow_ups = grounded_donate_follow_ups + list(follow_ups or [])

    urls = (
        [DONATION_URL, DONATION_TUTORIAL_URL]
        if donation_query and status == "answered"
        else []
    )
    resource_links = list(grounded_donate_links)
    if _is_prayer_request_link_request(req.message) and status == "answered":
        url_match = re.search(r"https?://[^\s<>()\]]+", answer)
        if url_match:
            resource_links.append({
                "label": "Prayer request form",
                "url": url_match.group(0).rstrip(".,;:!?\"'"),
            })

    return bot_response(
        user_message=req.message,
        answer=answer,
        data=data,
        follow_ups=follow_ups,
        urls=urls,
        resource_links=resource_links,
        session_id=session_id,
        status=status,
    )


def _see_more_matches_criteria(message: str, criteria: dict) -> bool:
    """Return whether any explicit see-more filters match saved browse criteria."""

    cleaned, _ = parse_see_more_offset(message)
    requested_categories = {item.casefold() for item in match_categories(cleaned)}
    requested_locations = {item.casefold() for item in match_locations(cleaned)}
    requested_all_locations = bool(re.search(r"\ball\s+locations?\b", cleaned, re.I))
    categories = criteria.get("categories") or (
        [criteria.get("category")] if criteria.get("category") else []
    )
    locations = criteria.get("locations") or (
        [criteria.get("location")] if criteria.get("location") else []
    )
    saved_categories = {str(item).casefold() for item in categories if item}
    saved_locations = {str(item).casefold() for item in locations if item}
    if requested_categories and requested_categories != saved_categories:
        return False
    if requested_locations and requested_locations != saved_locations:
        return False
    return not requested_all_locations or bool(criteria.get("all_locations"))


def _handle_ministry_lookup(
    req: ChatRequest,
    session_id: str,
    classified,
    override_slots=None
):
    """Advance the ministry guided-selection flow, merging state across turns."""

    clean_message, parsed_see_more_offset = parse_see_more_offset(req.message)

    incoming_classified_slots = {
        key: classified.slots.get(key)
        for key in ("category", "categories", "locations", "all_locations", "ministry", "ministry_code")
    }
    pending = get_conversation(session_id).pending or get_conversation(session_id).step
    if (
        pending in {"select_category", "select_location"}
        and not any(incoming_classified_slots.values())
        and is_gibberish(req.message)
    ):
        response = bot_response(
            req.message,
            "I didn't quite understand that. Could you please rephrase your question about 24:14 World?",
            {},
        follow_ups=(build_category_suggestions() if pending == "select_category" else build_location_suggestions(get_conversation(session_id).slots.get("categories"))),
            session_id=session_id,
            status="needs_clarification",
        )
        add_message(session_id, "assistant", _response_text(response))
        return response
    # A bare expansion request must reuse the authoritative criteria from the
    # previous ministry search.  It is deliberately handled before any fresh
    # lookup logic so category/location slots cannot be wiped.
    bare_see_more = is_see_more_request(req.message)
    see_more_offset = parsed_see_more_offset
    if bare_see_more:
        saved_state = get_conversation(session_id)
        saved_list = saved_state.last_list_criteria or {}
        saved_query = saved_state.last_query_criteria or {}
        if not (saved_list or saved_query):
            if incoming_classified_slots.get("all_locations"):
                # A request that names a category/location and explicitly
                # selects all locations is a fresh lookup even when its
                # wording matches the broad expansion pattern.
                bare_see_more = False
        elif not (
                (saved_list and _see_more_matches_criteria(req.message, saved_list))
                or (saved_query and _see_more_matches_criteria(req.message, saved_query))
        ):
            # A named, different category/location is a fresh lookup.
            bare_see_more = False
    if bare_see_more:
        state = get_conversation(session_id)
        list_criteria = state.last_list_criteria or {}
        legacy_criteria = state.last_query_criteria or {}
        if list_criteria and _see_more_matches_criteria(req.message, list_criteria):
            criteria = list_criteria
            criteria_source = "last_list_criteria"
        elif legacy_criteria and _see_more_matches_criteria(req.message, legacy_criteria):
            criteria = legacy_criteria
            criteria_source = "last_query_criteria"
        else:
            criteria = {}
            criteria_source = "none"

        logger.info(
            "See more restore | source=%s criteria=%s offset=%s",
            criteria_source,
            criteria,
            criteria.get("next_offset") if criteria else None,
        )
        if not criteria or not any(
            criteria.get(key) for key in ("category", "categories", "locations", "all_locations")
        ):
            response = bot_response(
                req.message,
                "What category or location would you like to search for?",
                {}, session_id=session_id, status="needs_clarification",
            )
            add_message(session_id, "assistant", _response_text(response))
            return response
        restored_locations = criteria.get("locations") or []
        if see_more_offset is None:
            see_more_offset = criteria.get("next_offset")
        if see_more_offset is None:
            see_more_offset = 3
        update_slots(
            session_id,
            categories=criteria.get("categories") or ([criteria.get("category")] if criteria.get("category") else []),
            locations=list(restored_locations),
            all_locations=bool(criteria.get("all_locations")),
            ministry=None,
            ministry_code=None,
            selected_ministry_codes=[],
        )
        incoming_classified_slots = {
            "category": criteria.get("category"),
            "categories": criteria.get("categories") or ([criteria.get("category")] if criteria.get("category") else []),
            "locations": list(restored_locations),
            "all_locations": bool(criteria.get("all_locations")),
            "ministry": None,
            "ministry_code": None,
        }
        classified = classified.__class__(
            "continue", "ministry_lookup", "search_ministry", .99,
            incoming_classified_slots, [], "deterministic",
        )
    # Resolve vague detail questions directly from the existing authoritative
    # result state.  This must happen before any category/location flow logic,
    # which would otherwise repeat the previous search.
    if is_vague_ministry_detail_follow_up_with_state(
        req.message, incoming_classified_slots, get_conversation(session_id)
    ):
        prior_results = get_conversation(session_id).last_result
        if len(prior_results) == 1 and isinstance(prior_results[0], dict):
            prior_code = prior_results[0].get("Ministry code") or prior_results[0].get("code")
            ministry_rows = find_ministry_by_code(prior_code) if prior_code else []
            if ministry_rows:
                update_conversation(session_id, flow=FLOW_MINISTRY, step="show_results", pending=None, last_intent=classified.intent, last_action=classified.action)
                response = _exact_ministry_lookup(req.message, session_id, row=ministry_rows[0])
                add_message(session_id, "assistant", _response_text(response))
                return response
        if len(prior_results) > 1:
            suggestions = []
            for prior in prior_results:
                if not isinstance(prior, dict):
                    continue
                code = prior.get("Ministry code") or prior.get("code")
                rows = find_ministry_by_code(code) if code else []
                details = rows[0] if rows else prior
                name = details.get("Ministry name") or details.get("name") or "Ministry"
                location = details.get("Location") or details.get("location") or "Unknown"
                exact_code = details.get("Ministry code") or details.get("code") or code or ""
                if exact_code:
                    suggestions.append({
                        "label": f"{name} — {location} (Code: {exact_code})",
                        "message": f"Tell me about {name} (Code: {exact_code})",
                    })
            response = bot_response(req.message, "I found multiple ministries. Please choose one:", {}, follow_ups=suggestions, session_id=session_id, status="needs_clarification")
            add_message(session_id, "assistant", _response_text(response))
            return response
        response = bot_response(req.message, "Please provide a ministry name or ministry code so I can help.", {}, session_id=session_id, status="needs_clarification")
        add_message(session_id, "assistant", _response_text(response))
        return response

    # Explicit "list all" requests should return every match just like
    # "show all". Otherwise the preview cap silently truncates a user's
    # requested complete list to three entries.
    all_location_phrase_request = bool(
        re.search(r"\ball\s+(?:the\s+)?locations?\b", clean_message or "", re.IGNORECASE)
    )
    show_all_request = bool(
        re.match(r"^\s*(?:show|list)\s+all\b", clean_message or "", re.IGNORECASE)
        and not all_location_phrase_request
    )

    # Remember this before consuming the category. Category quick-reply
    # messages are self-contained ("Show … ministries"), but in the guided
    # chooser they answer a mandatory select-category step, not a direct
    # search request.
    guided_category_selection = get_conversation(session_id).pending == "select_category"

    is_fresh_lookup = (
        override_slots is not None
        or (
            getattr(classified, "context_mode", "continue") == "new"
        )
    )

    if is_fresh_lookup:
        update_slots(
            session_id,
            category=None,
            categories=[],
            locations=[],
            all_locations=False,
            ministry=None,
            ministry_code=None,
            selected_fund=None,
            category_preference_skipped=False,
        )
        update_session(session_id, category=None, location=None)
        update_conversation(session_id, recommendation_mode=None, last_list_criteria={})

    if getattr(classified, "clear_slots", None):
        cleared = {key: ([] if key in {"locations", "categories"} else False if key == "all_locations" else None) for key in classified.clear_slots}
        update_slots(session_id, **cleared)

    incoming_slots = override_slots or {
        key: classified.slots.get(key)
        for key in ("category", "categories", "locations", "all_locations", "ministry", "ministry_code")
    }

    # A name actually present in this message beats a code carried over from
    # an earlier turn or inferred by the classifier. Name-only duplicates must
    # reach clarification, not silently open one of their codes.
    named_in_message = match_ministry(clean_message) if override_slots is None else None
    if named_in_message and not _extract_explicit_ministry_code(req.message):
        incoming_slots["ministry"] = named_in_message
        incoming_slots["ministry_code"] = None
        update_slots(session_id, ministry_code=None)

    # A new category/location filter supersedes a previously selected result.
    # Without this, a correction such as "actually community ministries"
    # would keep returning the old ministry's detail page.
    if (
        (incoming_slots.get("categories") or incoming_slots.get("category") or incoming_slots.get("locations") or incoming_slots.get("all_locations"))
        and not (incoming_slots.get("ministry") or incoming_slots.get("ministry_code"))
    ):
        update_slots(session_id, ministry=None, ministry_code=None)

    incoming_categories = list(incoming_slots.get("categories") or [])
    if incoming_slots.get("category") and not incoming_categories:
        incoming_categories = [incoming_slots["category"]]
    additive = bool(re.search(r"\b(?:also|as well|too|in addition|add|plus)\b", clean_message, re.I))
    if additive:
        incoming_categories = list(get_conversation(session_id).slots.get("categories") or []) + incoming_categories
        incoming_slots["locations"] = list(get_conversation(session_id).slots.get("locations") or []) + list(incoming_slots.get("locations") or [])
    if incoming_categories:
        update_slots(session_id, categories=incoming_categories, category_preference_skipped=False)
        update_session(session_id, categories=incoming_categories, category=incoming_categories[0])

    if incoming_slots.get("locations"):
        # "Show all ... in West Bengal" expands that region's result set;
        # its word 'all' does not mean 'ignore the supplied location'.
        update_slots(session_id, locations=incoming_slots["locations"], all_locations=False)
        update_session(session_id, locations=incoming_slots["locations"])

    if incoming_slots.get("ministry"):
        update_slots(session_id, ministry=incoming_slots["ministry"])
    if incoming_slots.get("ministry_code"):
        update_slots(session_id, ministry_code=incoming_slots["ministry_code"])
    donation_category_needs_location = (
        bool(incoming_categories)
        and not incoming_slots.get("locations")
        and is_donation_query(req.message)
    )
    if donation_category_needs_location:
        # Donation-qualified category requests are guided: preserve the
        # category, clear any stale all-location selection, and ask where.
        update_slots(session_id, all_locations=False, locations=[])
    elif incoming_slots.get("all_locations") and not incoming_slots.get("locations"):
        update_slots(session_id, all_locations=True, locations=[])
        update_conversation(session_id, recommendation_mode=None)

    # A user who explicitly asks to show/list/find a category is making a
    # complete direct lookup.  A location is optional, not a required guided
    # step.  Bare category selections in a guided flow retain the prompt.
    direct_lookup_words = ("show", "list")
    # A fresh location-only request ("Show me ministries in Ukraine") is a
    # new filter set, not a request to keep an old category. Corrections such
    # as "Actually Nepal" intentionally retain the category.
    if (
        incoming_slots.get("locations")
        and not incoming_categories
        and any(word in clean_message.casefold() for word in direct_lookup_words)
        and not any(word in clean_message.casefold() for word in ("actually", "instead", "change", "rather"))
    ):
        update_slots(session_id, categories=[], category=None)
    if (
        incoming_categories
        and not incoming_slots.get("locations")
        and any(word in clean_message.casefold() for word in direct_lookup_words)
        and not guided_category_selection
        and not donation_category_needs_location
    ):
        update_slots(session_id, all_locations=True, locations=[])

    state = get_conversation(session_id)
    reference = result_reference_index(req.message, len(state.last_result))
    if reference is not None and state.last_result:
        selected = None
        if reference is not None and reference < len(state.last_result):
            selected = state.last_result[reference]
        elif len(state.last_result) == 1:
            selected = state.last_result[0]
        if isinstance(selected, dict) and (
            is_ministry_detail_follow_up(req.message)
            or not _looks_like_new_question(req.message)
        ):
            selected_name = selected.get("Ministry name") or selected.get("name")
            selected_code = selected.get("Ministry code") or selected.get("code")
            if selected_name:
                update_slots(session_id, ministry=selected_name)
                state = get_conversation(session_id)
            elif selected_code:
                update_slots(session_id, ministry_code=selected_code)
                state = get_conversation(session_id)
    category = state.slots.get("categories") or None
    location = state.slots.get("locations") or []
    ministry = state.slots.get("ministry")
    ministry_code = state.slots.get("ministry_code")

    # A CSV code is an authoritative identifier and always wins over a name.
    if ministry_code:
        ministry_rows = find_ministry_by_code(ministry_code)
        if ministry_rows:
            update_conversation(session_id, flow=FLOW_MINISTRY, step="show_results", pending=None, last_intent=classified.intent, last_action=classified.action)
            response = _exact_ministry_lookup(req.message, session_id, row=ministry_rows[0])
            add_message(session_id, "assistant", _response_text(response))
            return response

    if ministry:
        ministry_rows = find_ministry_by_name(ministry)
        if len(ministry_rows) > 1:
            update_conversation(
                session_id,
                flow=FLOW_MINISTRY,
                step="show_results",
                pending="select_duplicate_ministry",
                last_result=ministry_rows,
                last_query=req.message,
                last_query_criteria={"ministry_name": ministry},
            )
            response = bot_response(
                req.message,
                "I found multiple ministries with that name. Please choose the exact record:",
                {},
                follow_ups=_duplicate_ministry_suggestions(ministry_rows),
                session_id=session_id,
                status="needs_clarification",
            )
            add_message(session_id, "assistant", _response_text(response))
            return response
        if len(ministry_rows) == 1:
            update_conversation(
                session_id,
                flow=FLOW_MINISTRY,
                step="show_results",
                pending=None,
                last_intent=classified.intent,
                last_action=classified.action
            )
            response = _exact_ministry_lookup(
                req.message,
                session_id,
                row=ministry_rows[0],
            )
            add_message(session_id, "assistant", _response_text(response))
            return response

    # "All locations" answers the location slot, but a category is still
    # needed before a donor-facing list can be produced.
    if state.slots.get("all_locations") and not category and not state.slots.get("category_preference_skipped"):
        update_conversation(
            session_id,
            flow=FLOW_MINISTRY,
            step="select_category",
            pending="select_category",
            last_intent=classified.intent,
            last_action=classified.action,
        )
        response = _ministry_flow(session_id=session_id, category=None, location=None)
        add_message(session_id, "assistant", _response_text(response))
        return response

    if location or state.slots.get("all_locations"):
        update_conversation(
            session_id,
            flow=FLOW_MINISTRY,
            step="show_results",
            pending=None,
            last_intent=classified.intent,
            last_action=classified.action
        )
        response = _ministry_flow(
            session_id=session_id,
            category=category,
            location=location or None,
            show_all=show_all_request or bare_see_more,
            see_more_offset=see_more_offset,
        )
        add_message(session_id, "assistant", _response_text(response))
        return response

    if category:
        update_conversation(
            session_id,
            flow=FLOW_MINISTRY,
            step="select_location",
            pending="select_location",
            last_intent=classified.intent,
            last_action=classified.action
        )
        response = _ministry_flow(
            session_id=session_id,
            category=category,
            location=None
        )
        add_message(session_id, "assistant", _response_text(response))
        return response

    update_conversation(
        session_id,
        flow=FLOW_MINISTRY,
        step="select_category",
        pending="select_category",
        last_intent=classified.intent,
        last_action=classified.action
    )
    response = _ministry_flow(
        session_id=session_id,
        category=None,
        location=None
    )
    add_message(session_id, "assistant", _response_text(response))
    return response


@app.post("/api/chat")
@_serialize_conversation_request
def chat(req: ChatRequest):
    """Serialize the session, then perform language screening before all routing."""
    session_id = req.session_id
    state = get_conversation(session_id)

    if is_non_english(req.message):
        add_message(session_id, "user", req.message)
        response = bot_response(
            user_message=req.message,
            answer=NON_ENGLISH_MESSAGE,
            data={},
            follow_ups=_invalid_input_follow_ups(state),
            urls=[],
            session_id=session_id,
            ministries=[],
            status="needs_clarification",
        )
        add_message(session_id, "assistant", _response_text(response))
        return response

    directive = extract_requested_output_language(req.message)
    if directive:
        cleaned, language = directive
        notice = ENGLISH_ONLY_NOTICE_TEMPLATE.format(language=language or "the requested language")
        if not cleaned.strip():
            answer = notice + "\n\n" + ENGLISH_ONLY_NOTICE_NO_QUERY
            response = bot_response(
                user_message=req.message, answer=answer, data={}, follow_ups=[], urls=[],
                session_id=session_id, ministries=[], status="needs_clarification",
            )
            add_message(session_id, "user", req.message)
            add_message(session_id, "assistant", _response_text(response))
            return response
        req.message = cleaned
        response = _chat_inner(req)
        response["message"] = notice + "\n\n" + _response_text(response)
        replace_last_assistant_message(session_id, response["message"])
        return response

    return _chat_inner(req)


def _handle_donation_flow(req: ChatRequest, session_id: str, classified):
    """Handle donation option selection or hand off to ministry flow."""

    action = classified.action
    resource_links = None
    if action == "give_now":
        recipients, unresolved = _selected_giving_recipients(session_id)
        if unresolved or get_conversation(session_id).pending == "giving_recipient_clarification":
            response = _unresolved_giving_response(req.message, session_id, unresolved or ["your intended donation recipients"])
            add_message(session_id, "assistant", _response_text(response))
            return response
        resources, arguments = _resource_arguments(recipients, [
            {"label": "Create a donor account", "url": DONOR_REGISTER_URL},
        ])
        answer = "Open a giving link below and review the selected designation and amount before donating. You can also register for a donor account if you need one."
        if recipients:
            answer += "\n\nSelected recipients:\n" + "\n".join(
                "- " + item["name"] + (f" (Code: {item['code']})" if item["code"] else "") for item in recipients
            )
        answer = _append_donation_notes(answer, resources)
        urls, resource_links = arguments["urls"], arguments["resource_links"]
        follow_ups = []
    elif action == "watch_donation_guide":
        answer = DONATION_WATCH_MESSAGE
        urls = [DONATION_TUTORIAL_URL]
        follow_ups = []
    elif action in ("select_category", "search_ministry"):
        update_conversation(
            session_id,
            flow=FLOW_MINISTRY,
            step="select_category",
            pending="select_category",
            slots={"category": None, "locations": [], "all_locations": False, "ministry": None, "ministry_code": None, "selected_fund": None, "category_preference_skipped": False}
        )
        update_session(
            session_id,
            category=None,
            location=None,
            active=False
        )
        response = _ministry_flow(
            session_id=session_id,
            category=None,
            location=None
        )
        update_conversation(
            session_id,
            last_intent=classified.intent,
            last_action=classified.action
        )
        add_message(session_id, "assistant", _response_text(response))
        return response
    else:
        answer = DONATION_INTENT_MESSAGE
        urls = []
        follow_ups = DONATION_SUGGESTIONS

    # The donation-intent menu must always show every donation option. In
    # particular, "Find a fund" can also be the message that triggered this
    # response, but it is still a valid option for the newly displayed menu.
    suggestion_filter_message = (
        None if answer == DONATION_INTENT_MESSAGE else req.message
    )

    update_conversation(
        session_id,
        flow=FLOW_DONATION,
        step="donation_options",
        pending="donation_options",
        last_intent=classified.intent,
        last_action=classified.action
    )
    response = bot_response(
        user_message=suggestion_filter_message,
        answer=answer,
        data={},
        follow_ups=follow_ups,
        urls=urls,
        resource_links=resource_links,
        session_id=session_id
    )
    add_message(session_id, "assistant", _response_text(response))
    return response


def _deterministic_route(req: ChatRequest, session_id: str):
    """Route through the same deterministic intent path used before LLM classification."""

    state = get_conversation(session_id)
    classified = deterministic_intent(
        req.message,
        state=state.to_dict(),
        history=get_recent_history(session_id),
    )
    return _route_by_intent(req, session_id, classified)


def _sync_legacy_session_to_conversation(session_id: str):
    """Sync legacy per-session category/location into conversation state."""

    session = get_session(session_id)
    categories = list(session.get("categories") or [])
    category = categories[0] if categories else session.get("category")
    locations = list(session.get("locations") or [])
    convo = get_conversation(session_id)

    # Preserve any currently paused in-flight flow when the user is mid-conversation.
    if session.get("active") or convo.flow in {FLOW_MINISTRY, FLOW_DONATION}:
        if category and locations:
            update_conversation(
                session_id,
                flow=FLOW_MINISTRY,
                step="show_results",
                slots={"categories": categories or [category], "locations": locations, "ministry": None}
            )
        elif category:
            update_conversation(
                session_id,
                flow=FLOW_MINISTRY,
                step="select_location",
                slots={"categories": categories or [category], "locations": [], "ministry": None}
            )
        return

    if category and locations:
        update_conversation(
            session_id,
            flow=FLOW_MINISTRY,
            step="show_results",
            slots={"categories": categories or [category], "locations": locations, "ministry": None}
        )
    elif category:
        update_conversation(
            session_id,
            flow=FLOW_MINISTRY,
            step="select_location",
            slots={"categories": categories or [category], "locations": [], "ministry": None}
        )
    else:
        reset_flow(session_id)


def _response_text(response: dict) -> str:
    """Extract the text the assistant will present to the user."""

    return str(response.get("message", "") or "").strip()


# ============================================================
# Exact Ministry Lookup
# ============================================================

def _build_csv_ministry_summary(row: dict) -> str:
    name = str(row.get("Ministry name") or "").strip()
    location = str(row.get("Location") or "").strip()
    description = str(row.get("Description") or "").strip()
    if description:
        explanation = description
    else:
        phrases = {
            "Children Care": "care and support for vulnerable children",
            "Community Development": "community development work",
            "Church Planting": "church planting work",
            "Refugee Support": "support for refugees",
            "Disaster Relief": "disaster relief work",
            "Education": "education work",
            "Healthcare": "healthcare work",
        }
        categories = [str(row.get(key) or "").strip() for key in ("Category 1", "Category 2", "Category 3")]
        categories = [item for item in categories if item]
        work = ", ".join(phrases.get(item, f"{item} work") for item in categories) or "ministry work"
        explanation = f"{name} serves in {location} through {work}."
    return (
        f"{explanation.rstrip('.')}.") + (
        f" You can give to {name} using code {str(row.get('Ministry code') or '').strip()} "
        "through the giving links below, or reach the 24:14 World team through the contact link."
    )


def _enrich_ministry_details(row: dict) -> str | None:
    name = str(row.get("Ministry name") or "").strip()
    location = str(row.get("Location") or "").strip()
    code = str(row.get("Ministry code") or "").strip()
    prompts = [
        (f"Tell me more about the 24:14 World ministry named {name} (Ministry code: {code}, location: {location}). Explain its purpose, who it serves, its work, and how someone can support or learn more. Keep the exact ministry name and code. Do not invent details. Write as the 24:14 World assistant. State only what is known. Do not mention what is missing, unavailable, or not provided. Do not mention sources, content, or listings.", True),
        (f"What does {name} in {location} do? Explain its purpose, who it serves, its work, and how to support it. Write as the 24:14 World assistant, state only what is known, and do not mention what is missing, unavailable, or not provided, sources, content, or listings.", False),
        (f"What does {name} do? Write as the 24:14 World assistant. State only what is known. Do not mention what is missing, unavailable, or not provided. Do not mention sources, content, or listings.", False),
    ]
    for attempt, (query, ministry_query) in enumerate(prompts, 1):
        try:
            data = grounded_chat(query, ministry_query=ministry_query, sources_limit=5)
            raw, _, status = _answer_and_follow_ups(data)
            answer = strip_meta_commentary(raw)
            removed_too_much = bool(raw.strip()) and len(answer) <= len(raw.strip()) / 2
            accepted = bool(answer) and not removed_too_much and _accept_grounded_enrichment(data, answer, status, [row])
            logger.info("Ministry enrichment | attempt=%s decision=%s", attempt, "accepted" if accepted else "rejected")
            if accepted:
                return answer
        except Exception as exc:
            logger.info("Ministry enrichment | attempt=%s decision=error:%s", attempt, exc)
    return None

def _exact_ministry_lookup(user_message: str, session_id: str, *, row: dict):
    """
    Handle an exact ministry-name/code lookup.

    This intentionally bypasses generic donation routing so a request
    such as "give me details about <ministry>" remains a ministry
    lookup rather than entering donation guidance.
    """

    ministry_name = str(row.get("Ministry name") or "").strip()

    logger.info(
        f"Exact ministry lookup: {ministry_name}"
    )

    if row:
        details = row
        # Selecting a ministry supersedes an earlier fund recommendation.
        update_slots(session_id, selected_fund=None, category_preference_skipped=False)
        category_bits = []
        for key in ("Category 1", "Category 2", "Category 3"):
            value = (details.get(key) or "").strip()
            if value:
                category_bits.append(value)

        categories = ", ".join(category_bits) if category_bits else "General"
        location = (details.get("Location") or "Unknown").strip() or "Unknown"
        csv_description = (details.get("Description") or "").strip()
        ministry_code = str(details.get("Ministry code") or "").strip()
        update_slots(session_id, ministry= ministry_name, ministry_code=ministry_code,
                     selected_fund=None, selected_funds=[], selected_ministry_codes=[ministry_code])
        answer = (
            f"Here are the details for {ministry_name}:\n\n"
            f"Location: {location}\n"
            f"Category: {categories}\n"
            f"Ministry code: {ministry_code}"
        )

        explanation = _enrich_ministry_details(details) or _build_csv_ministry_summary(details)
        answer += "\n\n" + explanation

        _, donate_links = _donate_now_options(session_id, [details])
        follow_ups = [
            {"label": "Find another ministry", "message": "Find another ministry"}
        ]
        ministries = []

        update_conversation(
            session_id,
            last_result=[details],
            last_query=user_message,
            last_query_criteria={
                "category": None, "location": location, "all_locations": False,
                "ministry_name": ministry_name,
                "ministry_code": details.get("Ministry code"),
            },
        )

        update_session(session_id, active=False)
        resources, arguments = _resource_arguments([_row_recipient(details)], [
            {"label": "Watch how to give", "url": DONATION_TUTORIAL_URL},
            {"label": "Create a donor account", "url": DONOR_REGISTER_URL},
        ])
        answer = _append_donation_notes(answer, resources)
        arguments["resource_links"] = donate_links + [
            {"label": "Watch how to give", "url": DONATION_TUTORIAL_URL},
            {"label": "Create a donor account", "url": DONOR_REGISTER_URL},
            {"label": "Contact us", "url": CONTACT_URL},
        ]
        arguments["urls"] = [link["url"] for link in arguments["resource_links"]]
        return bot_response(
            user_message=user_message,
            answer=answer,
            data={},
            follow_ups=follow_ups,
            **arguments,
            session_id=session_id,
            ministries=ministries,
            status="answered"
        )

    # ``row`` comes from the authoritative CSV. Reaching this fallback would
    # mean a caller violated that contract, so do not fabricate an LLM result.
    return bot_response(user_message, "I couldn't find that ministry in our records.", {}, session_id=session_id, status="no_result")


def _grounded_code_is_present(text: str, code: str) -> bool:
    """Match a complete code, including any hyphenated suffix."""

    return bool(
        code
        and re.search(
            rf"(?<![a-z0-9-]){re.escape(code)}(?![a-z0-9-])",
            text,
            re.IGNORECASE,
        )
    )


def _grounded_answer_matches_rows(answer: str, rows: list[dict]) -> bool:
    """Accept grounded text only when it identifies only selected CSV rows."""

    text = (answer or "").casefold()
    if not text or not rows:
        return False

    names = [str(row.get("Ministry name") or "").strip().casefold() for row in rows]
    selected_codes = {
        str(row.get("Ministry code") or "").strip().casefold()
        for row in rows
    }

    # Do not append a grounded answer that identifies a different CSV record.
    # This is especially important for prefix-related codes such as 368GSM
    # and 368GSM-SD.
    for candidate in get_all_ministries():
        candidate_code = str(candidate.get("Ministry code") or "").strip().casefold()
        if candidate_code and candidate_code not in selected_codes and _grounded_code_is_present(text, candidate_code):
            return False

    duplicate_names = {name for name in names if name and names.count(name) > 1}
    for row, name in zip(rows, names):
        code = str(row.get("Ministry code") or "").strip().casefold()
        if code and _grounded_code_is_present(text, code):
            continue
        if name and name not in duplicate_names and name in text:
            continue
        return False
    return True


def _explicit_csv_facts_match(answer: str, rows: list[dict]) -> bool:
    """Conservatively check explicit location/category assertions."""
    location_re = re.compile(
        r"\blocation\s*:\s*([^\n.;()]+)|"
        r"\b(?:based|located|headquartered)\s+in\s+([^\n.;()]+)",
        re.IGNORECASE,
    )
    category_re = re.compile(r"\bcategor(?:y|ies)\s*:\s*([^\n.]+)", re.IGNORECASE)
    current_rows = rows if len(rows) == 1 else []
    for unit in re.split(r"\n+|(?<=[.!?])\s+", answer or ""):
        text = unit.casefold()
        identified = [
            row for row in rows
            if _grounded_code_is_present(text, str(row.get("Ministry code") or ""))
            or (str(row.get("Ministry name") or "").strip().casefold() in text
                and str(row.get("Ministry name") or "").strip())
        ]
        if identified:
            current_rows = identified
        locations = list(location_re.finditer(unit))
        categories = list(category_re.finditer(unit))
        if not locations and not categories:
            continue
        if len(current_rows) != 1:
            return False
        row = current_rows[0]
        expected = str(row.get("Location") or "").strip().casefold()
        valid_locations = {expected, *(part.strip() for part in expected.split(","))} - {""}
        for match in locations:
            claim = match.group(1) or match.group(2) or ""
            claim = re.split(
                r",?\s+\b(?:with|where|which|focused|focusing|providing|categories)\b|"
                r",?\s+and\s+(?:focuses|works|supports|provides)\b|"
                r",?\s+\b(?:ministry\s+)?code\s*:",
                claim, maxsplit=1, flags=re.IGNORECASE,
            )[0].strip(" ,:—–\"'").casefold()
            if claim not in valid_locations:
                return False
        valid_categories = {
            str(row.get(key) or "").strip().casefold()
            for key in ("Category 1", "Category 2", "Category 3")
        } - {""}
        for match in categories:
            claimed = [part.strip().casefold() for part in match.group(1).split(",")]
            if any(part not in valid_categories for part in claimed):
                return False
    return True


def _accept_grounded_enrichment(data: dict, answer: str, status: str, rows: list[dict]) -> bool:
    """One acceptance gate shared by exact details and ministry lists."""
    lowered_answer = (answer or "").casefold()
    rejected_phrases = (
        "no matching entry",
        "no_relevant_context",
        "no relevant context",
    )
    canonical_categories = {item.casefold() for item in get_all_categories()}
    row_categories = {
        str(row.get(key) or "").strip().casefold()
        for row in rows
        for key in ("Category 1", "Category 2", "Category 3")
        if str(row.get(key) or "").strip()
    }
    claims_unlisted_category = (
        len(rows) == 1 and any(
            re.search(rf"(?<!\w){re.escape(category)}(?!\w)", lowered_answer)
            and category not in row_categories
            for category in canonical_categories
        )
    )
    if has_meta_commentary(answer):
        reason = "meta_commentary"
    elif claims_unlisted_category:
        reason = "category_conflict"
    elif any(phrase in lowered_answer for phrase in rejected_phrases):
        reason = "invalid_no_match_answer"
    elif status != "answered" or not (answer or "").strip():
        reason = "not_answered"
    elif not grounded_response_has_sources(data):
        reason = "sources_not_returned"
    elif not _grounded_answer_matches_rows(answer, rows) or not _explicit_csv_facts_match(answer, rows):
        reason = "entity_or_csv_fact_conflict"
    else:
        reason = "accepted"
    logger.info(
        "Ministry enrichment | codes=%s sources_returned=%r decision=%s",
        [row.get("Ministry code") for row in rows],
        data.get("sources_returned") if isinstance(data, dict) else None,
        reason,
    )
    return reason == "accepted"



# ============================================================
# Question Detection Helper
# ============================================================

_QUESTION_WORDS = {
    "what",
    "who",
    "when",
    "where",
    "why",
    "how",
    "which",
    "whose",
    "whom",
}


def _looks_like_new_question(message: str) -> bool:
    """True when the message appears to be a fresh factual question."""

    text = (message or "").strip()

    if text.endswith("?"):
        return True

    words = {
        word.strip(".,!?;:\"'()[]{})")
        for word in text.lower().split()
    }

    return bool(words & _QUESTION_WORDS)


# ============================================================
# Ministry Flow Helpers
# ============================================================

def _ministry_flow(
    session_id: str,
    category,
    location,
    *,
    show_all=False,
    all_ministry_preview=False,
    see_more_offset=None,
):
    """
    Handle the guided ministry selection conversation.

    This flow runs only after the one-shot intent router has selected
    either a filtered ministry lookup or a generic donation entry.
    It is also used to consume a slot selection in an active session.
    """

    categories = list(category or []) if isinstance(category, (list, tuple, set)) else ([category] if category else [])
    category_display = format_list(categories)
    # A new discovery list is not a donation selection.
    update_slots(session_id, selected_fund=None, selected_funds=[],
                 ministry_code=None, selected_ministry_codes=[], ministry=None)
    all_locations = get_conversation(session_id).slots.get("all_locations") is True

    # An all-location preference fills the location slot, not the category
    # slot.  Ask for a category before attempting an unfiltered search.
    if not category and not location and not show_all and not get_conversation(session_id).slots.get("category_preference_skipped"):

        logger.info(
            "Ministry flow: asking for category"
        )

        suggestions = build_category_suggestions()

        return bot_response(
            user_message="",
            answer=(
                "I'd be happy to help you give to a ministry. "
                "What type of ministry are you interested in?"
            ),
            data={},
            follow_ups=suggestions,
            urls=[],
            session_id=session_id,
            status="answered",
        )

    if not location and not all_locations and not show_all:

        logger.info(
            "Ministry flow: asking for location"
        )

        suggestions = build_location_suggestions(categories)

        return bot_response(
            user_message="",
            answer=(
                (f"You chose '{category_display}'. " if categories else "")
                + "Which region or location would you like to support?"
            ),
            data={},
            follow_ups=suggestions,
            urls=[],
            session_id=session_id,
            status="needs_clarification",
        )

    logger.info(
        f"Ministry flow: finding ministries for "
        f"categories={categories}, location={location}"
    )

    # Filtered pagination must keep the original criteria.  The complete
    # source list is reserved for the explicit, unfiltered all-location path.
    unfiltered_all_request = (
        all_locations
        and not categories
        and not location
    )
    matches = (
        get_all_ministries()
        if unfiltered_all_request
        else find_ministries(category, location)
    )
    if all_locations and not location:
        location_label = "all locations"
    elif isinstance(location, (list, tuple)):
        location_label = format_list(location)
    else:
        location_label = location

    if not matches:

        logger.info(
            "No ministries matched; offering location suggestions"
        )

        alternatives = build_no_result_alternatives(
            f"Show {category_display} ministries in {location_label or ''}".strip()
        )
        update_conversation(
            session_id,
            flow=FLOW_MINISTRY,
            step="select_location",
            pending="select_location",
        )
        # Keep the attempted, validated criteria for corrections and auditing.
        # The clarification suggestions can still replace it on the next turn.

        suggestions = alternatives or build_location_suggestions(categories)

        return bot_response(
            user_message="",
            answer=(
                f"I couldn't find any {category_display or 'selected'} ministries in "
                f"{location_label}. Would you like to choose a different "
                f"region?"
            ),
            data={},
            follow_ups=suggestions,
            urls=[],
            session_id=session_id,
            status="no_result"
        )

    ministries = [
        {
            "name": row["Ministry name"],
            "code": row["Ministry code"],
            "location": (row.get("Location") or "Unknown").strip() or "Unknown",
            "categories": [
                cat.strip()
                for cat in (
                    row.get("Category 1", ""),
                    row.get("Category 2", ""),
                    row.get("Category 3", ""),
                )
                if cat and cat.strip()
            ],
            "description": row.get("Description", ""),
            "donation_url": (
                DONATION_URL + "?" + urlencode({"code": str(row["Ministry code"]).strip()})
                if str(row.get("Ministry code") or "").strip() else None
            ),
        }
        for row in matches
    ]
    donate_follow_ups, donate_links = _donate_now_options(session_id, matches)
    for item, row in zip(ministries, matches):
        item["donation_url"] = row.get("donation_url")
    # Always clear any stale recommendation_mode; paging is driven by the
    # explicit offset in the request and the original CSV order.
    update_conversation(session_id, recommendation_mode=None)

    SEE_MORE_THRESHOLD = 5
    PREVIEW_COUNT = 3
    total = len(matches)
    needs_see_more = total > SEE_MORE_THRESHOLD and not show_all
    # ``show_all`` is also used by the explicit paging action. Only page from
    # the preview when an offset was supplied/restored; a fresh "show all"
    # request must include the complete result set from the first row.
    remaining_only = (
        show_all
        and total > SEE_MORE_THRESHOLD
        and (
            see_more_offset is not None
            or bool(get_conversation(session_id).last_list_criteria)
        )
    )
    page_offset = (
        max(PREVIEW_COUNT, int(see_more_offset))
        if see_more_offset is not None
        else PREVIEW_COUNT
    )
    remaining_rows = matches[page_offset:] if remaining_only else []
    buttons = remaining_rows[:PREVIEW_COUNT] if remaining_only else []
    preview_rows = matches[:PREVIEW_COUNT] if needs_see_more else matches
    if needs_see_more and len(categories) > 1:
        preview_rows = []
        grouped = []
        for selected_category in categories:
            grouped.append([row for row in matches if selected_category.casefold() in {
                str(row.get(key, "")).strip().casefold() for key in ("Category 1", "Category 2", "Category 3")
            }])
        index = 0
        while len(preview_rows) < PREVIEW_COUNT and any(index < len(rows) for rows in grouped):
            for rows in grouped:
                if index < len(rows) and rows[index] not in preview_rows:
                    preview_rows.append(rows[index])
                    if len(preview_rows) >= PREVIEW_COUNT:
                        break
            index += 1

    visible_result_rows = (
        buttons
        if remaining_only
        else preview_rows if needs_see_more else matches
    )

    list_criteria = {
        "category": categories[0] if categories else None,
        "categories": categories,
        "location": (location[0] if isinstance(location, list) and location else location),
        "locations": list(location) if isinstance(location, (list, tuple)) else ([location] if location else []),
        "all_locations": all_locations,
        "next_offset": (
            page_offset + PREVIEW_COUNT
            if remaining_only and remaining_rows
            else PREVIEW_COUNT if needs_see_more else None
        ),
    }

    update_conversation(
        session_id,
        last_result=visible_result_rows,
        last_query=f"{category} {location}",
        last_query_criteria={
            **list_criteria,
            "ministry_name": None,
            "ministry_code": None,
        },
        last_list_criteria=list_criteria,
    )

    scope = f"{category_display} ministries" if categories else "ministries"

    def see_more_message(offset):
        if not categories and location_label == "all locations":
            return f"Show all ministries in all locations [from {offset}]"
        if categories and location_label and location_label != "all locations":
            return f"Show all {category_display} ministries in {location_label} [from {offset}]"
        if categories:
            return f"Show all {category_display} ministries [from {offset}]"
        if location_label and location_label != "all locations":
            return f"Show all ministries in {location_label} [from {offset}]"
        return f"Show all ministries in all locations [from {offset}]"

    def remaining_message():
        if categories and location_label and location_label != "all locations":
            return f"Show remaining {category_display} ministries in {location_label}"
        if categories:
            return f"Show remaining {category_display} ministries"
        if location_label and location_label != "all locations":
            return f"Show remaining ministries in {location_label}"
        return "Show remaining ministries"

    if remaining_only:
        remaining_count = len(remaining_rows)
        if remaining_rows:
            detail_lines = []
            for row in remaining_rows:
                row_categories = [
                    cat.strip()
                    for cat in (
                        row.get("Category 1", ""),
                        row.get("Category 2", ""),
                        row.get("Category 3", ""),
                    )
                    if cat and cat.strip()
                ]
                categories_text = (
                    f" Categories: {', '.join(row_categories)}."
                    if row_categories else ""
                )
                detail_lines.append(
                    f"- {row['Ministry name']} (Code: {row['Ministry code']}) — "
                    f"Location: {(row.get('Location') or 'Unknown').strip() or 'Unknown'}."
                    f"{categories_text}"
                )
            short_message = (
                f"Here are the other {remaining_count} {scope} in "
                f"{location_label} ({total} in total):\n\n"
                + "\n".join(detail_lines)
                + "\n\nSelect a ministry below to read its description."
            )
            visible_ministries = [
                {
                    "name": row["Ministry name"],
                    "code": row["Ministry code"],
                    "location": (row.get("Location") or "Unknown").strip() or "Unknown",
                }
                for row in buttons
            ]
        else:
            short_message = (
                f"There are no more {scope} in {location_label}. "
                "Would you like to find another ministry?"
            )
            visible_ministries = []

        update_session(session_id, active=False)
        follow_ups = (
            [{"label": "See more", "message": remaining_message()}]
            if len(remaining_rows) > PREVIEW_COUNT
            else [{"label": "Find another ministry", "message": "Find another ministry"}]
            if len(remaining_rows) <= PREVIEW_COUNT
            else []
        )
        return bot_response(
            user_message="",
            answer=short_message,
            data={},
            follow_ups=follow_ups,
            resource_links=[],
            urls=[],
            session_id=session_id,
            ministries=visible_ministries,
        )

    intro = (
        f"Here are all {total} {scope} in {location_label}:"
        if show_all
        else f"Here are the {scope} in {location_label} I found:"
    )

    per_ministry_blocks = []
    for row in preview_rows:
        row_categories = [
            cat.strip()
            for cat in (
                row.get("Category 1", ""),
                row.get("Category 2", ""),
                row.get("Category 3", "")
            )
            if cat and cat.strip()
        ]

        categories_text = (
            " Categories: " + ", ".join(row_categories) + "."
            if row_categories
            else ""
        )

        fallback = (
            f"{row['Ministry name']} "
            f"(Code: {row['Ministry code']}) — "
            f"Location: {(row.get('Location') or 'Unknown').strip() or 'Unknown'}."
            f"{categories_text}"
        )

        csv_row_description = (row.get("Description") or "").strip()
        if csv_row_description:
            fallback += f" Description: {csv_row_description}"

        per_ministry_blocks.append(fallback)

    partial_notes = []
    if categories:
        criteria_location = None if all_locations and not location else location
        for selected_category in categories:
            if not find_ministries(selected_category, criteria_location):
                partial_notes.append(
                    f"No {selected_category} ministries were found in {location_label}."
                )
    csv_description = intro + ("\n\n" + "\n".join(partial_notes) if partial_notes else "") + "\n\n" + "\n\n".join(per_ministry_blocks)
    description = csv_description

    # The CSV remains authoritative for filtering, names, codes, and locations.
    # Use one grounded request for the human-readable ministry context instead
    # of making one Gloo request per result card.
    grounded_query = (
        f"{intro} Provide concise descriptions for these exact ministries, "
        "using the ministry content library. Keep every name and code exactly "
        "as written and do not invent details. Write as the 24:14 World assistant. "
        "State only what is known. Do not mention what is missing, unavailable, "
        "or not provided. Do not mention sources, content, or listings:\n\n"
        + "\n".join(
            f"- {row['Ministry name']} (Code: {row['Ministry code']}, "
            f"Location: {(row.get('Location') or 'Unknown').strip() or 'Unknown'})"
            for row in preview_rows
        )
    )
    if not show_all:
        try:
            grounded_data = grounded_chat(
                grounded_query,
                ministry_query=True,
                sources_limit=min(10, max(3, len(preview_rows))),
            )
            grounded_answer, _, grounded_status = _answer_and_follow_ups(grounded_data)
            grounded_answer = strip_meta_commentary(grounded_answer)
            if (
                grounded_answer
                and _accept_grounded_enrichment(grounded_data, grounded_answer, grounded_status, preview_rows)
            ):
                description += "\n\nAdditional context:\n" + grounded_answer.strip()
        except Exception:
            logger.exception("Grounded ministry enrichment failed; using CSV results")

    # Build the single self-contained expansion quick-reply when the result
    # exceeds the preview threshold.
    follow_ups = list(donate_follow_ups)
    if needs_see_more:
        follow_ups = [{"label": "See more", "message": remaining_message()}]


    logger.info(
        "Returning ministry match response"
    )

    update_session(session_id, active=False)
    if len(matches) == 1:
        only = matches[0]
        update_slots(session_id, ministry=only["Ministry name"], ministry_code=only["Ministry code"])
    visible_ministries = ministries[:PREVIEW_COUNT] if needs_see_more else ministries
    if any(item["donation_url"] for item in visible_ministries):
        description += "\n\n" + DONATION_SELECTION_REMINDER

    return bot_response(
        user_message="",
        answer=description,
        data={},
        follow_ups=follow_ups,
        resource_links=donate_links + [{"label": "Watch how to give", "url": DONATION_TUTORIAL_URL}],
        urls=[link["url"] for link in donate_links] + [DONATION_TUTORIAL_URL],
        session_id=session_id,
        ministries=visible_ministries
    )

# ============================================================
# Root Endpoint
# ============================================================

@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def conversation_tester():
    """Serve the interactive API conversation tester."""

    return FileResponse(FRONTEND_FILE)

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Return no content when browsers request the optional site icon."""

    return Response(status_code=204)

@app.get("/api")

def root():

    logger.info(
        "Root endpoint called"
    )

    return {
        "message":
        "Gloo Grounded RAG API Running",
        "publisher_name":
        PUBLISHER_NAME
    }
