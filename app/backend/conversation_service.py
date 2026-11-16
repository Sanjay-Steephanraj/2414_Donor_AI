"""Conversation/session state store for the FastAPI RAG chatbot.

This module owns ALL in-memory conversation/session state. It deliberately
contains NO LLM calls, NO Gloo calls, and NO ministry CSV lookup logic.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Optional

from app_loggers import get_logger

logger = get_logger()

# ============================================================
# Constants
# ============================================================

FLOW_IDLE = "idle"
FLOW_MINISTRY = "ministry_lookup"
FLOW_DONATION = "donation_flow"
ALLOWED_FLOWS = {FLOW_IDLE, FLOW_MINISTRY, FLOW_DONATION}

MINISTRY_STEPS = {"select_category", "select_location", "show_results"}
DONATION_STEPS = {"donation_options", "ministry_selection"}

SESSION_TTL_SECONDS = 30 * 60
SESSION_MAX_COUNT = 10_000
MAX_HISTORY_MESSAGES = 10
MAX_MESSAGE_CONTENT_LENGTH = 4000

SLOT_KEYS = ("category", "categories", "location", "locations", "all_locations", "ministry", "ministry_code", "selected_fund", "selected_funds", "selected_ministry_codes", "category_preference_skipped")


def _default_slots() -> dict[str, Any]:
    """Return a fresh slots dict with the canonical slot shapes."""
    return {
        "category": None,
        "categories": [],
        # ``location`` is the canonical single-location state used by the
        # stateful intent contract. ``locations`` remains for existing
        # multi-location matching and is kept synchronized below.
        "location": None,
        "locations": [],
        "all_locations": False,
        "ministry": None,
        "ministry_code": None,
        "selected_fund": None,
        "selected_funds": [],
        "selected_ministry_codes": [],
        # An intentional no-category choice is different from an unanswered
        # category question. This is internal state, not a new API field.
        "category_preference_skipped": False,
    }


def _normalize_locations(raw_locations: Any) -> list[str]:
    """Return ordered, non-empty, case-insensitively unique locations."""

    values = (
        list(raw_locations)
        if isinstance(raw_locations, (list, tuple, set))
        else ([] if raw_locations is None else [raw_locations])
    )
    locations: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        location = value.strip()
        key = location.casefold()
        if location and key not in seen:
            seen.add(key)
            locations.append(location)
    return locations


# ============================================================
# State schema
# ============================================================

@dataclass
class ConversationState:
    """Mutable, plain-dataclass conversation state for a single session."""

    session_id: str
    flow: str = FLOW_IDLE
    step: Optional[str] = None
    slots: dict[str, Any] = field(default_factory=_default_slots)
    last_intent: Optional[str] = None
    last_action: Optional[str] = None
    history: list[dict[str, str]] = field(default_factory=list)
    last_result: list[Any] = field(default_factory=list)
    pending: Optional[str] = None
    last_query: Optional[str] = None
    # Structured, CSV-validated ministry criteria.  ``last_query`` remains a
    # legacy display/debug value; routing must use this machine-readable form.
    last_query_criteria: dict[str, Any] = field(default_factory=dict)
    # Criteria for the most recent ministry list, preserved across detail views
    # so pagination can resume from the original filtered result set.
    last_list_criteria: dict[str, Any] = field(default_factory=dict)
    recommendation_mode: Optional[str] = None
    last_seen: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Return a normalized plain-dictionary representation of the state."""
        slots = _default_slots()
        slots.update(self.slots or {})
        raw_locations = slots.get("locations")
        slots["locations"] = (
            list(raw_locations)
            if isinstance(raw_locations, (list, tuple))
            else ([raw_locations] if raw_locations else [])
        )
        slots["location"] = slots["locations"][0] if slots["locations"] else None
        raw_categories = slots.get("categories")
        slots["categories"] = _normalize_locations(raw_categories if raw_categories is not None else slots.get("category"))
        slots["category"] = slots["categories"][0] if slots["categories"] else None
        slots["all_locations"] = bool(slots.get("all_locations"))
        return {
            "session_id": self.session_id,
            "flow": self.flow,
            "step": self.step,
            "slots": slots,
            "last_intent": self.last_intent,
            "last_action": self.last_action,
            "history": list(self.history),
            "last_result": list(self.last_result or []),
            "pending": self.pending,
            "last_query": self.last_query,
            "last_query_criteria": dict(self.last_query_criteria or {}),
            "last_list_criteria": dict(self.last_list_criteria or {}),
            "recommendation_mode": self.recommendation_mode,
            "last_seen": self.last_seen,
        }


# ============================================================
# In-memory store
# ============================================================

# NOTE:
# This in-memory store can later be replaced by Redis or another
# shared backend store for multi-worker production deployments.
_STORE: dict[str, ConversationState] = {}
_LOCK = threading.Lock()
_SESSION_LOCKS: dict[str, threading.RLock] = {}


# ============================================================
# Internal helpers (caller must hold _LOCK)
# ============================================================

def _evict_stale_sessions(now: float) -> None:
    """Remove sessions older than SESSION_TTL_SECONDS."""

    stale = [
        session_id
        for session_id, state in _STORE.items()
        if now - state.last_seen > SESSION_TTL_SECONDS
    ]

    for session_id in stale:
        del _STORE[session_id]

    if stale:
        logger.info("Evicted %d stale conversation sessions", len(stale))


def _trim_oldest_if_needed() -> None:
    """Cap total sessions by dropping the oldest entries."""

    if len(_STORE) <= SESSION_MAX_COUNT:
        return

    sorted_sessions = sorted(
        _STORE.items(),
        key=lambda item: item[1].last_seen,
    )

    overflow = len(sorted_sessions) - SESSION_MAX_COUNT

    for session_id, _ in sorted_sessions[:overflow]:
        del _STORE[session_id]

    logger.info("Trimmed %d oldest conversation sessions", overflow)


def _truncate_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep the most recent MAX_HISTORY_MESSAGES entries."""
    return history[-MAX_HISTORY_MESSAGES:] if len(history) > MAX_HISTORY_MESSAGES else history


# ============================================================
# Public API
# ============================================================

def get_conversation(session_id: str) -> ConversationState:
    """Return an existing conversation state or create a new one.

    Bumps ``last_seen``, evicts stale sessions, and trims the store if
    it has exceeded SESSION_MAX_COUNT.
    """

    now = time.time()

    with _LOCK:
        _evict_stale_sessions(now)

        state = _STORE.get(session_id)
        if state is not None:
            state.last_seen = now
            return state

        _trim_oldest_if_needed()

        state = ConversationState(session_id=session_id, last_seen=now)
        _STORE[session_id] = state
        return state


@contextmanager
def conversation_lock(session_id: str):
    """Serialize a complete request lifecycle for one session.

    The store lock protects individual state operations; this lock prevents
    concurrent turns in the same session from overwriting each other's state
    transitions. Different sessions remain concurrent.
    """

    with _LOCK:
        lock = _SESSION_LOCKS.setdefault(session_id, threading.RLock())
    with lock:
        yield


def create_conversation(session_id: str) -> ConversationState:
    """Force-create a fresh conversation state for ``session_id``.

    Any existing state for the session is discarded.
    """

    now = time.time()

    with _LOCK:
        _evict_stale_sessions(now)

        state = ConversationState(session_id=session_id, last_seen=now)
        _STORE[session_id] = state
        return state


def update_conversation(session_id: str, **fields: Any) -> ConversationState:
    """Update top-level fields of a conversation state.

    Accepted top-level fields: ``flow``, ``step``, ``last_intent``,
    ``last_action``, ``last_result``, ``pending``, ``last_query``,
    ``last_query_criteria``, ``last_list_criteria``, and
    ``recommendation_mode``. If ``slots`` is passed as a dict,
    it is merged into the existing slots dict without wiping unspecified
    keys. ``last_seen`` is always bumped to the current time.

    An invalid ``flow`` value is logged as a warning and ignored.
    """

    now = time.time()
    top_level = {"flow", "step", "last_intent", "last_action", "last_result", "pending", "last_query", "last_query_criteria", "last_list_criteria", "recommendation_mode"}

    with _LOCK:
        _evict_stale_sessions(now)
        state = _STORE.setdefault(
            session_id,
            ConversationState(session_id=session_id, last_seen=now),
        )
        _trim_oldest_if_needed()

        for key, value in fields.items():
            if key == "slots" and isinstance(value, dict):
                slots = {slot: item for slot, item in value.items() if slot in SLOT_KEYS}
                if "locations" in slots:
                    slots["locations"] = _normalize_locations(slots["locations"])
                    slots["location"] = slots["locations"][0] if slots["locations"] else None
                elif "location" in slots:
                    slots["locations"] = _normalize_locations(slots["location"])
                    slots["location"] = slots["locations"][0] if slots["locations"] else None
                if "categories" in slots:
                    slots["categories"] = _normalize_locations(slots["categories"])
                    slots["category"] = slots["categories"][0] if slots["categories"] else None
                elif "category" in slots:
                    slots["categories"] = _normalize_locations(slots["category"])
                    slots["category"] = slots["categories"][0] if slots["categories"] else None
                if "all_locations" in slots:
                    slots["all_locations"] = bool(slots["all_locations"])
                for singleton, plural in (
                    ("selected_fund", "selected_funds"),
                    ("ministry_code", "selected_ministry_codes"),
                ):
                    if plural in slots:
                        slots[plural] = _normalize_locations(slots[plural])
                        slots[singleton] = slots[plural][0] if len(slots[plural]) == 1 else None
                    elif singleton in slots:
                        slots[plural] = _normalize_locations(slots[singleton])
                state.slots.update(slots)
                state.slots["location"] = (state.slots.get("locations") or [None])[0]
                state.slots["categories"] = _normalize_locations(state.slots.get("categories"))
                state.slots["category"] = (state.slots.get("categories") or [None])[0]
                continue

            if key not in top_level:
                continue

            if key == "flow" and value not in ALLOWED_FLOWS:
                logger.warning(
                    "Ignoring invalid flow %r for session %s",
                    value,
                    session_id,
                )
                continue

            if key == "last_result":
                value = list(value or [])
            if key == "last_query_criteria":
                value = dict(value or {}) if isinstance(value, dict) else {}
            if key == "last_list_criteria":
                value = dict(value or {}) if isinstance(value, dict) else {}
            setattr(state, key, value)

        state.last_seen = now
        return state


def update_slots(session_id: str, **slot_fields: Any) -> ConversationState:
    """Merge ``slot_fields`` into the conversation's slots dict.

    This is a convenience wrapper around ``update_conversation`` that
    only touches ``slots``. Setting a slot to ``None`` is allowed.
    """

    return update_conversation(session_id, slots=slot_fields)


def add_message(session_id: str, role: str, content: str) -> None:
    """Append a message to the conversation history.

    ``role`` must be one of ``"user"`` or ``"assistant"``. The content
    is truncated to MAX_MESSAGE_CONTENT_LENGTH characters and the
    history is trimmed to the most recent MAX_HISTORY_MESSAGES entries.
    """

    if role not in {"user", "assistant"}:
        logger.warning("Ignoring message with unexpected role %r", role)
        return

    content = (content or "")[:MAX_MESSAGE_CONTENT_LENGTH]

    now = time.time()

    with _LOCK:
        _evict_stale_sessions(now)
        state = _STORE.setdefault(
            session_id,
            ConversationState(session_id=session_id, last_seen=now),
        )
        _trim_oldest_if_needed()

        state.history.append({"role": role, "content": content})
        state.history = _truncate_history(state.history)
        state.last_seen = now


def replace_last_assistant_message(session_id: str, content: str) -> None:
    """Replace the most recent assistant message, preserving conversation order."""
    with _LOCK:
        state = _STORE.get(session_id)
        if state is None:
            return
        for item in reversed(state.history):
            if item.get("role") == "assistant":
                item["content"] = (content or "")[:MAX_MESSAGE_CONTENT_LENGTH]
                state.last_seen = time.time()
                return


def get_recent_history(session_id: str, limit: int = MAX_HISTORY_MESSAGES) -> list[dict[str, str]]:
    """Return a bounded copy of the conversation's recent history."""

    with _LOCK:
        state = _STORE.get(session_id)
        if state is None:
            return []
        return list(state.history[-limit:]) if limit > 0 else []


def clear_conversation(session_id: str) -> None:
    """Remove the session entirely from the store."""

    with _LOCK:
        _STORE.pop(session_id, None)


def reset_flow(session_id: str) -> ConversationState:
    """Reset the conversation flow to idle while preserving history.

    ``flow`` becomes ``idle``, ``step`` becomes ``None``, and ``slots``
    are cleared to all-None values. ``session_id`` and ``history`` are
    retained.
    """

    now = time.time()

    with _LOCK:
        _evict_stale_sessions(now)
        state = _STORE.setdefault(
            session_id,
            ConversationState(session_id=session_id, last_seen=now),
        )
        _trim_oldest_if_needed()

        state.flow = FLOW_IDLE
        state.step = None
        state.slots = _default_slots()
        state.last_intent = None
        state.last_action = None
        state.last_result = []
        state.pending = None
        state.last_query = None
        state.last_query_criteria = {}
        state.last_list_criteria = {}
        state.recommendation_mode = None
        state.last_seen = now
        return state


def touch_conversation(session_id: str) -> None:
    """Bump ``last_seen`` for the session without changing any other state."""

    now = time.time()

    with _LOCK:
        _evict_stale_sessions(now)
        state = _STORE.get(session_id)
        if state is not None:
            state.last_seen = now
