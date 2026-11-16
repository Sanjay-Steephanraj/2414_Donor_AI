import re

META_COMMENTARY_RE = re.compile(
    r"(?:does\s+not\s+provide|do\s+not\s+provide|not\s+provided|"
    r"not\s+available|unavailable|no\s+additional|no\s+further|"
    r"no\s+other\s+details|not\s+listed|not\s+mentioned|"
    r"does\s+not\s+mention|cannot\s+find|could\s+not\s+find|"
    r"couldn['’]?t\s+find\s+information|not\s+specified|"
    r"no\s+information\s+(?:is\s+)?(?:available|provided)|"
    r"available\s+content|provided\s+(?:information|content|context)|"
    r"the\s+(?:content|context|dataset|listing|json|documents?|sources?|retrieved)|"
    r"based\s+on\s+the\s+(?:content|context|information\s+provided)|"
    r"according\s+to\s+the\s+(?:content|listing|dataset|data|sources?)|"
    r"ministry\s+listing\s+is\s+the\s+best\s+place)",
    re.IGNORECASE,
)

def _segments(text: str) -> list[str]:
    return re.split(r"\n+|(?<=[.!?])\s+(?=[A-Z0-9\"'\-(])", text.strip())

def has_meta_commentary(text: str) -> bool:
    return bool(META_COMMENTARY_RE.search(text or ""))

def strip_meta_commentary(text: str) -> str:
    kept = [part.strip() for part in _segments(text or "") if part.strip()]
    kept = [part for part in kept if not META_COMMENTARY_RE.search(part)]
    return re.sub(r"\s+", " ", " ".join(kept)).strip()
