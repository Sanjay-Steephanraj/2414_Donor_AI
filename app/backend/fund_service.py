"""Deterministic prescribed-fund matching for the donor guidance flow.

The mapping below comes from the approved Donor AI functional specification.
It is intentionally separate from ministry category matching and from Gloo
retrieval so a clear donor interest always resolves to the prescribed fund.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FundProfile:
    key: str
    name: str
    interest: str
    explanation: str
    interest_aliases: tuple[str, ...]
    name_aliases: tuple[str, ...] = ()
    ministry_code: str | None = None


FUNDS: tuple[FundProfile, ...] = (
    FundProfile(
        key="martyrs_fund",
        ministry_code='095MAF',
        name="Martyrs Fund",
        interest="families of murdered believers",
        explanation="It sustains and supports families left behind after martyrdom.",
        interest_aliases=(
            "families of murdered believers",
            "families of martyred believers",
            "families of martyrs",
            "martyrs families",
            "martyrs' families",
            "families left behind after martyrdom",
        ),
    ),
    FundProfile(
        key="living_martyrs_fund",
        ministry_code='095LMF',
        name="Living Martyrs Fund",
        interest="living persecuted Christians",
        explanation="It supports believers who face continuing risk and persecution for their faith.",
        interest_aliases=(
            "living persecuted christians",
            "persecuted christians",
            "living martyrs",
            "believers facing persecution",
            "christians facing persecution",
        ),
    ),
    FundProfile(
        key="bibles_for_the_persecuted",
        ministry_code='095BPF',
        name="Bibles for the Persecuted Fund",
        interest="getting God's Word to believers",
        explanation="It delivers printed and digital Bibles to believers in restricted regions.",
        interest_aliases=(
            "getting god's word to believers",
            "getting gods word to believers",
            "get god's word to believers",
            "get gods word to believers",
            "bibles for persecuted believers",
            "scripture for persecuted believers",
            "bibles in restricted regions",
        ),
        name_aliases=("Bibles for the Persecuted",),
    ),
    FundProfile(
        key="compassion_relief_fund",
        ministry_code='095CRF',
        name="Compassion Relief Fund",
        interest="disaster or crisis response",
        explanation="It provides immediate humanitarian and spiritual relief during disasters and crises.",
        interest_aliases=(
            "disaster response",
            "crisis response",
            "disaster or crisis response",
            "disaster and crisis response",
            "disaster victims",
            "humanitarian crisis relief",
        ),
    ),
    FundProfile(
        key="race_ministry",
        ministry_code='097RACE',
        name="RACE Ministry",
        interest="Muslim outreach or apologetics",
        explanation="It engages Islam and supports Muslim Background Believers.",
        interest_aliases=(
            "muslim outreach",
            "outreach to muslims",
            "muslim apologetics",
            "apologetics to muslims",
            "muslim background believers",
            "supporting mbbs",
        ),
    ),
    FundProfile(
        key="petals_of_hope",
        ministry_code='532PHI',
        name="Petals of Hope",
        interest="girls' education or trafficking response",
        explanation="It supports education and safe houses for vulnerable girls in Liberia.",
        interest_aliases=(
            "girls' education",
            "girls education",
            "educating vulnerable girls",
            "human trafficking",
            "girls affected by trafficking",
            "safe houses for girls",
            "vulnerable girls in liberia",
        ),
        name_aliases=("Petals of Hope Ministry",),
    ),
    FundProfile(
        key="spiritual_mutual_fund",
        ministry_code=None,
        name="Spiritual Mutual Fund",
        interest="general missions or unreached peoples",
        explanation="It provides diversified frontline mission support across unreached people groups.",
        interest_aliases=(
            "general missions",
            "unreached peoples",
            "unreached people groups",
            "frontline missions",
            "frontline mission support",
        ),
        name_aliases=("24:14 World Spiritual Mutual Fund",),
    ),
)


_DONOR_PURPOSE_RE = re.compile(
    r"\b(?:donate|donation|give|giving|contribute|contribution)\b|"
    r"\b(?:i|we)\s+(?:want|would\s+like|wish|hope|plan|feel\s+called)\s+to\s+"
    r"(?:support|help)\b|"
    r"\b(?:recommend|suggest)\b.{0,50}\bfund\b|"
    r"\b(?:which|what)\s+fund\b.{0,60}\b(?:support|donate|give|choose|matches)\b",
    re.IGNORECASE,
)
_MINISTRY_LIST_RE = re.compile(
    r"\b(?:show|list|find|which|what)\b.{0,80}\bministr(?:y|ies)\b",
    re.IGNORECASE,
)


def _normalized(text: str) -> str:
    text = (text or "").casefold().replace("’", "'")
    return re.sub(r"\s+", " ", text).strip()


def _contains_phrase(text: str, phrase: str) -> bool:
    words = re.escape(_normalized(phrase)).replace(r"\ ", r"\s+")
    return bool(re.search(rf"(?<!\w){words}(?!\w)", text))


def match_fund_interest(message: str) -> FundProfile | None:
    """Return the prescribed fund for an explicitly stated donor interest."""

    text = _normalized(message)
    matches: list[tuple[int, FundProfile]] = []
    for fund in FUNDS:
        for alias in fund.interest_aliases:
            if _contains_phrase(text, alias):
                matches.append((len(_normalized(alias)), fund))
    return max(matches, key=lambda item: item[0])[1] if matches else None


def match_fund_name(message: str) -> FundProfile | None:
    """Return a fund explicitly named in the current message."""

    text = _normalized(message)
    matches: list[tuple[int, FundProfile]] = []
    for fund in FUNDS:
        for alias in (fund.name, *fund.name_aliases):
            if _contains_phrase(text, alias):
                matches.append((len(_normalized(alias)), fund))
    return max(matches, key=lambda item: item[0])[1] if matches else None


def fund_candidates(message: str) -> list[FundProfile]:
    """Return every explicitly matched approved fund; keep existing aliases."""
    text = _normalized(message)
    hits = []
    for fund in FUNDS:
        for phrase in (*fund.interest_aliases, fund.name, *fund.name_aliases):
            pattern = re.escape(_normalized(phrase)).replace(r"\ ", r"\s+")
            for match in re.finditer(rf"(?<!\w){pattern}(?!\w)", text):
                hits.append((match.start(), match.end(), fund.key))

    occupied = []
    selected = set()
    for start, end, key in sorted(hits, key=lambda hit: (-(hit[1] - hit[0]), hit[0])):
        # "Living Martyrs Fund" must not also match its nested "Martyrs Fund".
        if any(start < right and end > left for left, right in occupied):
            continue
        occupied.append((start, end))
        clause = re.split(r"[,;.!?]|\b(?:but|instead|rather)\b", text[:start])[-1]
        negative = re.search(
            r"\b(?:not|no|without|except|exclude|avoid|never|don't|do not)\b"
            r"(?!\s+only\b)",
            clause,
        )
        if not negative:
            selected.add(key)

    return [fund for fund in FUNDS if fund.key in selected]


def fund_display_name(fund: FundProfile) -> str:
    return f"{fund.name} (Code: {fund.ministry_code})" if fund.ministry_code else fund.name


def fund_by_key(key: str | None) -> FundProfile | None:
    return next((fund for fund in FUNDS if fund.key == key), None)


def build_fund_interest_suggestions() -> list[dict[str, str]]:
    """Build the guided-choice buttons from the approved mapping itself."""

    return [
        {
            "label": fund.interest[0].upper() + fund.interest[1:],
            "message": f"I want to support {fund.interest}.",
        }
        for fund in FUNDS
    ]


def is_fund_guidance_request(
    message: str,
    fund: FundProfile | None,
    *,
    awaiting_interest: bool = False,
) -> bool:
    """Keep ministry-list requests separate from prescribed-fund guidance."""

    if fund is None or _MINISTRY_LIST_RE.search(message or ""):
        return False
    return awaiting_interest or bool(_DONOR_PURPOSE_RE.search(message or ""))

