from __future__ import annotations

import json
from dataclasses import dataclass

from app_loggers import get_logger
from intent_service import _extract_json, is_api_controlled_message, is_unsupported_creation_request, is_underspecified_request, is_giving_guidance_request
from ministry_service import is_gibberish
from prompts import SCOPE_GATE_SYSTEM_PROMPT
from rag_service import chat_completion

logger = get_logger()
ALLOWED_SCOPES = {"in_scope", "creation_request", "out_of_scope", "needs_clarification"}


@dataclass(frozen=True)
class ScopeResult:
    scope: str
    confidence: float
    source: str


def _value(state, key, default=None):
    if isinstance(state, dict):
        return state.get(key, default)
    return getattr(state, key, default)


def _fallback(message, state):
    if is_giving_guidance_request(message):
        scope = "in_scope"
    elif is_unsupported_creation_request(message):
        scope = "creation_request"
    elif is_underspecified_request(message, state) or is_gibberish(message):
        scope = "needs_clarification"
    else:
        scope = "in_scope"
    return ScopeResult(scope, 1.0, "fallback")


def evaluate_scope(message, state, history, timeout=8):
    """Classify the user's underlying request before any other routing."""

    if is_api_controlled_message(message):
        return ScopeResult("in_scope", 1.0, "fast_path")
    if is_giving_guidance_request(message):
        return ScopeResult("in_scope", 1.0, "donation_guidance_fast_path")
    normalized_message = (message or "").strip().casefold().rstrip(".")
    if (
        (normalized_message in {"see more", "show more", "view all"} or normalized_message.startswith("show remaining"))
        and _value(state, "last_result", [])
    ):
        return ScopeResult("in_scope", 1.0, "pagination_fast_path")

    context = {
        "flow": _value(state, "flow"),
        "step": _value(state, "step"),
        "pending": _value(state, "pending"),
        "has_prior_results": bool(_value(state, "last_result", []) or []),
        "recent_history": list(history or [])[-6:],
        "user_message": message,
    }
    try:
        raw = chat_completion(
            SCOPE_GATE_SYSTEM_PROMPT,
            json.dumps(context, ensure_ascii=False),
            temperature=0,
            max_tokens=80,
            timeout=timeout,
        )
        parsed = _extract_json(raw)
        if not isinstance(parsed, dict) or parsed.get("scope") not in ALLOWED_SCOPES:
            raise ValueError("malformed scope response")
        confidence = max(0.0, min(1.0, float(parsed.get("confidence"))))
        scope = parsed["scope"] if confidence >= 0.6 else "in_scope"
        return ScopeResult(scope, confidence, "llm")
    except Exception:
        logger.exception("Scope gate failed; using deterministic fallback")
        return _fallback(message, state)
