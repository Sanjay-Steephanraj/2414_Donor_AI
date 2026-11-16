import csv
import re

from pathlib import Path

from app_loggers import get_logger
from rag_service import (
    DONATION_KEYWORDS,
    llm_extract_slots,
)

# ============================================================
# Logger
# ============================================================

logger = get_logger()

# ============================================================
# CSV Loading
# ============================================================

CSV_PATH = Path(__file__).resolve().parent.parent / "Ministry_details.csv"

# Locations that are administrative junk, not real geographic places.
# They are excluded from free-text inference and user-facing suggestions
# but kept in exact find_ministries lookups if explicitly requested.
EXCLUDED_LOCATIONS = {
    "-",
    "general",
    "local",
    "staff",
    "not active",
}

# Categories that are administrative/internal or catch-all "other"/
# "unique" buckets rather than real, user-facing ministry categories.
# They are excluded from free-text inference and user-facing suggestions
# (get_all_categories / match_category) but kept in exact find_ministries
# lookups if explicitly requested.
EXCLUDED_CATEGORIES = {
    "-",
    "internal/admin",
    "admin",
    "other",
    "others",
    "unique/other"
}

_MINISTRY_ROWS: list[dict] = []


def _load_ministry_rows(csv_path: Path) -> list[dict]:
    """
    Load ministry rows from the CSV bundled next to this module.

    Whitespace is stripped from every field so downstream matching
    does not have to worry about trailing spaces in the source data.
    """

    rows: list[dict] = []

    try:

        with open(
            csv_path,
            mode="r",
            newline="",
            encoding="utf-8-sig"
        ) as file:

            reader = csv.DictReader(file)

            for row in reader:
                rows.append(
                    {
                        key: (value or "").strip()
                        for key, value in row.items()
                    }
                )

    except Exception as e:

        logger.error(
            f"Failed to load ministry CSV from {csv_path}: {e}"
        )

        return []

    logger.info(
        f"Loaded {len(rows)} ministry rows from {csv_path}"
    )

    return rows


_MINISTRY_ROWS = _load_ministry_rows(CSV_PATH)


# ============================================================
# Public Query Functions
# ============================================================

def _is_eligible_category(category: str) -> bool:
    """True for real, user-facing categories, false for admin/other/unique junk."""

    normalized = (category or "").strip().lower()

    if not normalized:
        return False

    if normalized in EXCLUDED_CATEGORIES:
        return False

    return True


def get_all_categories() -> list[str]:
    """
    Return sorted unique non-empty categories across Category 1/2/3.

    Administrative/internal categories and catch-all "other"/"unique"
    style categories are filtered out so the suggestion list only
    contains real, user-facing categories.
    """

    categories: set[str] = set()

    for row in _MINISTRY_ROWS:
        for key in ("Category 1", "Category 2", "Category 3"):
            value = row.get(key, "")
            if value and _is_eligible_category(value):
                categories.add(value)

    return sorted(categories)


def get_all_locations() -> list[str]:
    """Return the eligible donor-facing locations in canonical spelling."""
    return _eligible_locations()


def get_all_ministries() -> list[dict]:
    """Return a copy of the source ministry records."""
    return list(_MINISTRY_ROWS)


def get_locations_for_category(category: str | list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
    """
    Return sorted unique non-empty Locations for rows matching the
    given category (case-insensitive, whitespace-tolerant).

    Administrative junk locations (e.g. "-", "General", "Local",
    "Staff") are filtered out so the suggestion list only contains
    real geographic candidates.
    """

    categories = category if isinstance(category, (list, tuple, set)) else [category]
    needles = {str(item).strip().casefold() for item in categories if isinstance(item, str) and item.strip()}
    locations: set[str] = set()

    if not needles:
        return []

    for row in _MINISTRY_ROWS:
        for key in ("Category 1", "Category 2", "Category 3"):
            if row.get(key, "").strip().casefold() in needles:
                location = row.get("Location", "").strip()
                if location and location.lower() not in EXCLUDED_LOCATIONS:
                    locations.add(location)
                break

    # Case-insensitive de-duplication with a stable, human-friendly
    # display spelling. "West Bengal" and "West bengal" collapse to one.
    grouped: dict[str, str] = {}

    for loc in locations:
        key = loc.casefold()
        current = grouped.get(key)

        if current is None:
            grouped[key] = loc
            continue

        # Prefer a Title Case spelling; otherwise keep the lexicographically
        # smaller for determinism.
        candidates = [current, loc]
        titled = [c for c in candidates if c == c.title()]

        if titled:
            grouped[key] = sorted(titled)[0]
        else:
            grouped[key] = sorted(candidates)[0]

    return sorted(grouped.values())


def _row_matches_category(row: dict, category: str) -> bool:
    """Check whether a row matches a category exactly (case-insensitive)."""

    needle = (category or "").strip().lower()

    if not needle:
        return False

    for key in ("Category 1", "Category 2", "Category 3"):
        if row.get(key, "").strip().lower() == needle:
            return True

    return False

def _row_matches_location(row: dict, location: str) -> bool:
    """
    Check whether a row's Location matches the requested location.

    Matching is inclusive/substring-aware in both directions so that
    "India" matches "Kashmir, India" and "Odissa, India", while
    "Kashmir" matches both "Kashmir, India" and "Jammu and Kashmir".
    """

    needle = (location or "").strip().lower()

    if not needle:
        return False

    haystack = row.get("Location", "").strip().lower()

    if not haystack:
        return False

    return needle in haystack or haystack in needle


def format_list(items) -> str:
    values = [str(item) for item in (items if isinstance(items, (list, tuple, set)) else [items]) if item]
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return ", ".join(values[:-1]) + " and " + values[-1]


def _category_command(category) -> str:
    """Self-contained command message for choosing a category."""

    return f"Show {format_list(category)} ministries"


def _category_location_command(category, location: str) -> str:
    """Self-contained command message for a category + location search."""

    return f"Show {format_list(category)} ministries in {format_list(location)}" if category else f"Show ministries in {format_list(location)}"


def build_category_suggestions(limit: int | None = None) -> list[dict]:
    """
    Return category buttons as {label, message} objects. The label is the
    category name; the message is a self-contained command such as
    "Show Children Care ministries". Only categories that actually return
    records are included.
    """

    suggestions: list[dict] = [{"label": "No preference", "message": "Continue without a ministry category preference"}]

    for category in get_all_categories():
        if not find_ministries(category, None):
            continue

        suggestions.append(
            {
                "label": category,
                "message": _category_command(category),
            }
        )

        if limit is not None and len(suggestions) >= limit:
            break

    return suggestions


def build_location_suggestions(
    category=None,
    limit: int | None = None
) -> list[dict]:
    """
    Return location buttons for a given category as {label, message}
    objects. The label is the location name; the message is a
    self-contained command such as "Show Children Care ministries in
    Kenya". Only locations that actually return records for the category
    are included.
    """

    suggestions: list[dict] = []

    # A category-level "All" choice deliberately omits a specific location.
    # The ministry flow recognizes its message as an all-locations request and
    # returns every ministry in the selected category.
    if (find_ministries(category, None) if category else get_all_ministries()):
        suggestions.append(
            {
                "label": "All",
                "message": (
                    f"Show {format_list(category)} ministries in all locations"
                    if category else "Show ministries in all locations"
                ),
            }
        )

        if limit is not None and len(suggestions) >= limit:
            return suggestions

    locations = get_locations_for_category(category) if category else get_all_locations()
    for location in locations:
        if not find_ministries(category, location):
            continue

        suggestions.append(
            {
                "label": location,
                "message": _category_location_command(category, location),
            }
        )

        if limit is not None and len(suggestions) >= limit:
            break

    return suggestions


def build_no_result_alternatives(message: str, limit: int = 3) -> list[dict]:
    """
    Return up to `limit` in-domain alternative searches for a no_result
    outcome. Each returned item is a {label, message} object whose
    message is an executable, self-contained ministry search command
    (a real category or location present in the dataset) that is
    guaranteed to return at least one record. Never invents options and
    never returns generic prompts.
    """

    if not _MINISTRY_ROWS:
        return []

    attempted_categories = match_categories(message)
    attempted_location = match_location(message)

    alternatives: list[dict] = []
    seen: set[str] = set()

    def add(label: str, message: str):
        if message and message not in seen:
            seen.add(message)
            alternatives.append({"label": label, "message": message})

    if attempted_categories:
        known_locations = get_locations_for_category(attempted_categories)

        for loc in known_locations:
            if attempted_location and loc.lower() == attempted_location.lower():
                # Skip the exact failed pair; we already know it yields nothing.
                continue

            if find_ministries(attempted_categories, loc):
                add(
                    f"{format_list(attempted_categories)} in {loc}",
                    _category_location_command(attempted_categories, loc),
                )

            if len(alternatives) >= limit:
                return alternatives[:limit]

    if attempted_location and len(alternatives) < limit:
        for cat in get_all_categories():
            if attempted_categories and cat.casefold() in {item.casefold() for item in attempted_categories}:
                continue

            if find_ministries(cat, attempted_location):
                add(
                    f"{cat} in {attempted_location}",
                    _category_location_command(cat, attempted_location),
                )

            if len(alternatives) >= limit:
                return alternatives[:limit]

    if not attempted_categories and not attempted_location and len(alternatives) < limit:
        for cat in get_all_categories():
            if find_ministries(cat, None):
                add(cat, _category_command(cat))

            if len(alternatives) >= limit:
                return alternatives[:limit]

    return alternatives[:limit]


def find_ministries(
    category: str | list[str] | tuple[str, ...] | set[str] | None = None,
    location: str | list[str] | None = None
) -> list[dict]:
    """
    Return full row dicts filtered by category and/or location.

    Category and location are INDEPENDENT optional filters:
      - both empty  -> [] (a caller must supply at least one filter)
      - category only -> rows matching the category in any of the three
        category columns (exact, case-insensitive)
      - location only -> rows whose Location matches (bidirectional
        substring)
      - both -> rows matching BOTH

    Excluded/admin categories and locations are intentionally NOT filtered
    here, so an explicit lookup still works if a caller passes one.
    """

    categories = category if isinstance(category, (list, tuple, set)) else [category]
    categories = [item for item in categories if isinstance(item, str) and item.strip()]
    has_category = bool(categories)
    locations = location if isinstance(location, (list, tuple, set)) else [location]
    locations = [item for item in locations if isinstance(item, str) and item.strip()]
    has_location = bool(locations)

    if not has_category and not has_location:
        return []

    results: list[dict] = []

    for row in _MINISTRY_ROWS:

        if has_category and not any(_row_matches_category(row, item) for item in categories):
            continue

        if has_location and not any(_row_matches_location(row, item) for item in locations):
            continue

        if row not in results:
            results.append(row)

    return results


def find_ministry_by_name(name: str) -> list[dict]:
    """Return ministry rows whose ``Ministry name`` matches ``name`` exactly.

    Matching is case-insensitive and strips surrounding whitespace; the CSV
    remains the single source of truth. This helper lets callers turn a
    validated ministry name slot into the matching rows without reaching
    into internal row storage.
    """

    needle = (name or "").strip().lower()

    if not needle:
        return []

    return [
        row
        for row in _MINISTRY_ROWS
        if row.get("Ministry name", "").strip().lower() == needle
    ]


def find_ministry_by_code(code: str) -> list[dict]:
    needle = _normalize(code)
    return [row for row in _MINISTRY_ROWS if _normalize(row.get("Ministry code", "")) == needle] if needle else []


# ============================================================
# Free-text Matching Helpers
# ============================================================

def _normalize(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _phrase_pattern(phrase: str) -> re.Pattern:
    words = [re.escape(word) for word in _normalize(phrase).split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.I)


def _is_negated_occurrence(text: str, start: int) -> bool:
    prefix = _normalize(text[:start])[-45:]
    return bool(re.search(r"\b(?:not|no|without|except|not interested in)$", prefix))


# ============================================================
# Category Alias Resolution
# ============================================================

# Explicit alias map: user phrasing (lowercased) -> canonical category.
# This is a DELIBERATE, restricted map, NOT unrestricted semantic
# guessing. Every value MUST be one of the canonical categories returned
# by get_all_categories(); we never invent or rename a category.
#
# Extend this map with additional approved aliases over time.
CATEGORY_ALIASES: dict[str, str] = {
    # Children Care
    "child": "Children Care",
    "children": "Children Care",
    "children-based": "Children Care",
    "children based": "Children Care",
    "child-related": "Children Care",
    "child related": "Children Care",
    "kids": "Children Care",
    "orphan": "Children Care",
    "orphans": "Children Care",

    # Disaster Relief
    "disaster management": "Disaster Relief",
    "disaster response": "Disaster Relief",
    "disaster aid": "Disaster Relief",
    "relief": "Disaster Relief",

    # Women Empowerment  (NOTE: canonical is "Women Empowerment"; we do
    # NOT create a "Women Development" category)
    "women development": "Women Empowerment",
    "women empowerment": "Women Empowerment",
    "women": "Women Empowerment",

    # Refugee Support
    "refugee": "Refugee Support",
    "refugees": "Refugee Support",

    # Persecuted Believers Support
    "persecuted": "Persecuted Believers Support",
    "persecution": "Persecuted Believers Support",

    # Bible Distribution
    "bible": "Bible Distribution",
    "bibles": "Bible Distribution",

    # Training & Leadership Development
    "leadership": "Training & Leadership Development",
    "training": "Training & Leadership Development",

    # Prayer & Intercession
    "prayer": "Prayer & Intercession",
    "intercession": "Prayer & Intercession",
    "church": "Church Support",
    "churches": "Church Support",
    "community": "Community Development",
    "communities": "Community Development",
    "literature": "Media & Literature",
    "media": "Media & Literature",
    "books": "Media & Literature",
    "publishing": "Media & Literature",
    "evangelism": "Mission Outreach",
    "evangelistic": "Mission Outreach",
    "outreach": "Mission Outreach",
}


_COMMON_MEANINGFUL_WORDS = {
    "a", "about", "all", "and", "are", "can", "care", "choose", "do", "for",
    "give", "help", "hello", "hi", "how", "i", "in", "is", "it", "me", "ministry", "ministries",
    "more", "my", "of", "or", "please", "show", "support", "tell", "that",
    "the", "this", "to", "what", "where", "which", "who", "why", "with",
    "world", "you", "your", "no", "preference", "location", "locations",
    "any", "anywhere", "everywhere", "now", "donate", "donation", "fund",
    "funds", "missionary", "missionaries", "question", "information", "start", "over",
    "want", "find", "details", "know", "may",
}
_DOMAIN_WORDS = {
    "24:14", "2414", "24", "world", "ministry", "ministries", "donate", "donation",
    "give", "fund", "funds", "missionary", "missionaries", "church", "bible",
}


def is_gibberish(text: str) -> bool:
    """Return True only for text with no credible domain or English meaning."""

    raw = (text or "").strip()
    if not raw:
        return True

    lowered = _normalize(raw)
    if any(word in lowered for word in _DOMAIN_WORDS):
        return False
    if re.search(r"\b(?:what|who|when|where|why|how|which|can|could|would|please|tell|show)\b", lowered):
        return False
    if resolve_category(raw) or match_locations(raw) or match_ministry(raw) or match_ministry_code(raw):
        return False
    if is_all_locations_request(raw):
        return False

    tokens = re.findall(r"[a-z]+", lowered)
    if not tokens:
        return not bool(re.search(r"\d", raw))
    if any(token in _COMMON_MEANINGFUL_WORDS for token in tokens):
        return False

    for token in tokens:
        if len(token) >= 4 and not re.search(r"[aeiouy]", token):
            return True
        if len(token) >= 5 and re.search(r"[^aeiouy]{5,}", token):
            return True
        if len(token) >= 6 and re.search(r"(.)\1\1\1", token):
            return True
    return True


def _canonical_categories_lower() -> dict[str, str]:
    """Map lowercased canonical category -> exact canonical spelling."""

    return {cat.lower(): cat for cat in get_all_categories()}


def resolve_category(text: str) -> str | None:
    """
    Resolve free text to exactly ONE canonical category, or None.

    Resolution order (deterministic, restricted — never invents a
    category):
      1. Exact canonical name present as a substring (longest wins) —
         this is the existing match_category behaviour.
      2. Explicit approved alias present as a whole-word/substring match
         (longest alias wins), mapped to its canonical category.

    A hook is left for a future LLM step that must ALSO be constrained to
    return one of get_all_categories() (or None); it must never create or
    rename a category. That step is intentionally not implemented here.
    """

    lowered = _normalize(text)

    if not lowered:
        return None

    # 1. Direct canonical-name substring match (longest wins).
    canon = _canonical_categories_lower()
    best_canon: str | None = None

    for cat_lower, cat_exact in canon.items():
        match = _phrase_pattern(cat_lower).search(lowered)
        if match and not _is_negated_occurrence(lowered, match.start()):
            if best_canon is None or len(cat_lower) > len(best_canon.lower()):
                best_canon = cat_exact

    if best_canon is not None:
        return best_canon

    # 2. Explicit alias match (longest alias wins). Only aliases whose
    #    target is a real canonical category are honoured.
    best_alias: str | None = None
    best_target: str | None = None

    for alias, target in CATEGORY_ALIASES.items():
        if target.lower() not in canon:
            # defensive: never resolve to a non-canonical category
            continue

        match = _phrase_pattern(alias).search(lowered)
        if match and not _is_negated_occurrence(lowered, match.start()):
            if best_alias is None or len(alias) > len(best_alias):
                best_alias = alias
                best_target = target

    return best_target


def match_category(text: str) -> str | None:
    """
    Find the canonical category referenced in free text.

    Delegates to resolve_category, which first matches an exact canonical
    category name and then falls back to the explicit approved alias map.
    Never returns a non-canonical or invented category.
    """

    return resolve_category(text)


def match_categories(text: str) -> list[str]:
    """Return every non-negated canonical category mentioned in text."""
    lowered = _normalize(text)
    canonical = _canonical_categories_lower()
    phrases = [(phrase, target) for phrase, target in canonical.items()]
    phrases.extend(
        (alias, target) for alias, target in CATEGORY_ALIASES.items()
        if target.casefold() in {value.casefold() for value in canonical.values()}
    )
    occurrences = []
    for phrase, target in phrases:
        for occurrence in _phrase_pattern(phrase).finditer(lowered):
            if not _is_negated_occurrence(lowered, occurrence.start()):
                occurrences.append((occurrence.start(), -len(_normalize(phrase)), occurrence.end(), target))
    selected = []
    occupied = []
    for start, neg_length, end, target in sorted(occurrences, key=lambda item: (item[0], item[1])):
        if any(start < right and end > left for left, right in occupied):
            continue
        if target.casefold() not in {item[3].casefold() for item in selected}:
            selected.append((start, neg_length, end, target))
        occupied.append((start, end))
    selected.sort(key=lambda item: item[0])
    return [item[3] for item in selected]


def match_negated_categories(text: str) -> list[str]:
    """Return canonical categories explicitly excluded by the user."""
    found = []
    lowered = _normalize(text)
    phrases = {**_canonical_categories_lower(), **CATEGORY_ALIASES}
    for phrase, category in phrases.items():
        for match in _phrase_pattern(phrase).finditer(lowered):
            if _is_negated_occurrence(lowered, match.start()) and category not in found:
                found.append(category)
    return found


def _all_locations() -> set[str]:
    locations: set[str] = set()

    for row in _MINISTRY_ROWS:
        location = row.get("Location", "").strip()
        if location:
            locations.add(location)

    return locations


def _is_eligible_location(location: str) -> bool:
    """True for real geographic locations, false for admin junk."""

    normalized = location.lower().strip()

    if normalized in EXCLUDED_LOCATIONS:
        return False

    return len(location.strip()) >= 3


def match_location(text: str) -> str | None:
    """Backward-compatible wrapper returning the first matched location."""
    locations = match_locations(text)
    return locations[0] if locations else None


ALL_LOCATION_PHRASES = {"all", "all location", "all locations", "all the locations", "any location", "any locations", "anywhere", "everywhere", "no preference", "no location preference"}


def is_all_locations_request(text: str) -> bool:
    lowered = _normalize(text)
    return any(_phrase_pattern(phrase).search(lowered) for phrase in ALL_LOCATION_PHRASES)


def match_locations(text: str) -> list[str]:
    """Return unique mentioned locations in textual order; longer overlaps win."""
    lowered = _normalize(text)
    matches = []
    for location in _all_locations():
        if _is_eligible_location(location):
            for occurrence in _phrase_pattern(location).finditer(lowered):
                matches.append((occurrence.start(), -len(location), location))

    # Resolve overlaps by longest phrase first, then restore the user's
    # mention order for the final result.
    selected = []
    occupied = []
    for start, neg_length, location in sorted(matches, key=lambda item: (item[0], item[1])):
        end = start - neg_length
        if any(start < right and end > left for left, right in occupied):
            continue
        if location.casefold() not in {item[2].casefold() for item in selected}:
            selected.append((start, end, location))
            occupied.append((start, end))

    selected.sort(key=lambda item: item[0])
    return [item[2] for item in selected]


def _eligible_locations() -> list[str]:
    """Canonical, de-duplicated, eligible location names for the LLM to choose from."""

    seen: dict[str, str] = {}

    for loc in _all_locations():
        if _is_eligible_location(loc):
            seen.setdefault(loc.strip().casefold(), loc.strip())

    return sorted(seen.values())


def extract_slots(text: str) -> tuple[str | None, str | None]:
    """
    Resolve (category, location) from a natural-language query using an
    LLM first, then falling back to the deterministic matchers.

    The LLM is constrained to return ONLY a canonical category from
    get_all_categories() (or None) and ONLY an eligible location (or None).
    Its output is re-validated here against the canonical vocabulary before
    use, so it can never introduce an invented category/location.

    On ANY LLM failure (returns None) OR when the LLM yields None for a
    slot, that slot falls back to resolve_category()/match_location() so
    behaviour is never worse than the previous regex-only path.

    Returns (category_or_None, location_or_None).
    """

    if not text or not text.strip():
        return (None, None)

    categories = get_all_categories()
    locations = _eligible_locations()

    llm_category: str | None = None
    llm_location: str | None = None

    categories_lower = {cat.lower(): cat for cat in categories}
    locations_lower = {loc.lower(): loc for loc in locations}

    result = llm_extract_slots(text, categories, locations)

    if isinstance(result, dict):
        raw_category = result.get("category")
        raw_location = result.get("location")

        if isinstance(raw_category, str):
            llm_category = categories_lower.get(raw_category.lower())

        if isinstance(raw_location, str):
            llm_location = locations_lower.get(raw_location.lower())

    category = llm_category if llm_category else resolve_category(text)
    location = llm_location if llm_location else match_location(text)

    logger.info(
        "extract_slots: text=%r llm_result=%r final_category=%r final_location=%r",
        text,
        result,
        category,
        location,
    )

    return (category, location)


def match_ministry(message: str) -> str | None:
    """
    Return the Ministry *name* if the message references a specific
    ministry by its Code (e.g. "470SFA") or by its Name (e.g.
    "Starfish Missions"); otherwise None.

    This is what lets is_vague_donation_intent() tell apart a user who IS
    aware of a particular ministry from one who is UNAWARE.
    """
    lowered = _normalize(message)
    best_match: str | None = None

    for row in _MINISTRY_ROWS:
        code = (row.get("Ministry code") or "").strip().lower()
        name = (row.get("Ministry name") or "").strip().lower()

        if (code and _phrase_pattern(code).search(lowered)) or (name and _phrase_pattern(name).search(lowered)):
            candidate = row.get("Ministry name")
            if best_match is None or len(candidate) > len(best_match):
                best_match = candidate

    return best_match


def match_ministry_code(message: str) -> str | None:
    lowered = _normalize(message)
    # Never resolve the shorter identifier inside a hyphenated code. Code
    # boundaries intentionally differ from ordinary name/alias boundaries.
    for row in _MINISTRY_ROWS:
        code = row.get("Ministry code", "").strip()
        if code and re.search(
            rf"(?<![a-z0-9_-]){re.escape(code)}(?![a-z0-9_-])",
            lowered, re.IGNORECASE,
        ):
            return code
    return None


def is_general_information_question(message: str) -> bool:
    """Do not turn topic questions into searches just because an alias matches.

    Plural ministry requests and explicitly named records retain the existing
    lookup path. Pronoun-based detail requests are resolved by intent_service
    before this general-question check.
    """
    text = _normalize(message)
    if not re.match(
        r"^(?:what|who|when|where|why|how|which|tell me about|explain|"
        r"describe|help me understand)\b", text,
    ):
        return False
    if match_ministry(text) or match_ministry_code(text):
        return False
    if re.search(r"\b(?:ministries|missionaries)\b", text):
        return False
    if re.search(r"\b(?:show|list|find|search|recommend)\b.{0,80}\bministry\b", text):
        return False
    return True


# ============================================================
# Donation Intent Helpers
# ============================================================

def is_vague_donation_intent(message: str) -> bool:
    """
    True when the message expresses donate/give intent but names no
    specific category and no specific location.
    """

    text = _normalize(message)

    if not any(keyword in text for keyword in DONATION_KEYWORDS):
        return False

    if match_ministry(message) is not None:
        return False

    return match_category(message) is None and match_location(message) is None


# ============================================================
# New-Request Cue Detection
# ============================================================

# Whole-word lookup/action verb cues. Matching is case-insensitive and
# uses word boundaries so substrings like "listen" do not trigger "list".
NEW_REQUEST_CUE_WORDS = [
    "list",
    "show",
    "find",
    "tell",
    "details",
    "detail",
    "description",
    "information",
    "info",
]

_NEW_REQUEST_VERB_RE = re.compile(
    r"\b(?:" + "|".join(map(re.escape, NEW_REQUEST_CUE_WORDS)) + r")\b",
    re.IGNORECASE
)

# "give me details" or "give me ..." are explicitly treated as lookup grammar
# (a fresh request for information), not as donation intent.
_NEW_REQUEST_GIVE_ME_RE = re.compile(
    r"\bgive\s+me(?:\s+details?)?\b",
    re.IGNORECASE
)


def is_new_request_cue(message: str) -> bool:
    """
    Broad new-request cue: True when the user is asking for a fresh
    lookup/answer rather than continuing a guided slot-filling
    conversation.

    This includes strong cues (lookup verbs, "give me ...", or a specific
    ministry name/code) and also explicit category/location references.

    Use this signal when no guided selection is active, so a message like
    "education ministries in India" routes to lookup.
    """

    if is_strong_request_cue(message):
        return True

    if match_category(message) is not None:
        return True

    if match_location(message) is not None:
        return True

    return False
def is_strong_request_cue(message: str) -> bool:
    """
    Strong new-request cue: True when the user explicitly asks for a
    fresh lookup/answer.

    A strong cue is a lookup/action verb ("list", "show", "find",
    "tell", "details", "info", etc.), the "give me ..." grammar, or a
    specific ministry name/code. Bare category or location values are
    intentionally NOT strong cues so that a reply like "Kenya" to a
    "which region?" prompt is treated as a slot answer rather than an
    interruption.

    Use this signal to interrupt an active guided selection.
    """

    text = message or ""

    if _NEW_REQUEST_VERB_RE.search(text):
        return True

    if _NEW_REQUEST_GIVE_ME_RE.search(text):
        return True

    if match_ministry(message) is not None:
        return True

    return False
