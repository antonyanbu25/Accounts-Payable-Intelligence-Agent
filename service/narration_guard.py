"""
The numeral guard: a hard, code-level check that the narration LLM never
states a number that isn't in the structured result it was given.

This is deliberately a heuristic, not a perfect natural-language parser --
its job is to catch the failure mode that matters (an invented or altered
monetary figure or rate), not to parse every possible number in English
text. Known, accepted limitations: it does not attempt to validate dates or
section references as "correct", it only avoids flagging them as false
positives. It is one layer of a larger design (the narration prompt is also
instructed to use only given numbers) -- not the sole safeguard.
"""
import re

# Recognizable date shapes are stripped BEFORE number-scanning, so a date's
# digits (day/month/year) are never mistaken for an invented monetary figure.
_DATE_PATTERNS = [
    r"\b\d{1,2}[-/](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[-/]\d{2,4}\b",
    r"\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s*,?\s*\d{4}\b",
    r"\b\d{4}-\d{2}-\d{2}\b",
    r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
]

# A number immediately followed by a letter (e.g. "194J", "998313" is fine,
# but "194J" is a section reference) is a code, not an amount -- \b after the
# digits requires a non-word boundary, so this is naturally excluded by the
# main pattern below; this list is for extra, explicit safety on the section
# format specifically.
_SECTION_REF_PATTERN = r"\b\d+[A-Z]\b"

# ONE combined pattern, scanned in a SINGLE pass. The comma-grouped
# alternative is listed first so it wins at any position where it applies
# (e.g. "84,000.00" matches whole) -- a second, independent scan for plain
# numbers would otherwise re-match the leftover digit fragments between the
# commas ("84", "000") as spurious standalone numbers.
_NUMBER_PATTERN = re.compile(
    r"(?:₹\s?)?\d{1,3}(?:,\d{2,3})+(?:\.\d+)?%?"   # comma-grouped, e.g. 84,000.00
    r"|(?:₹\s?)?\d+(?:\.\d+)?%"                     # plain number with a % sign
    r"|(?:₹\s?)\d+(?:\.\d+)?"                       # plain number with a ₹ sign
    r"|\b\d+(?:\.\d+)?\b"                           # bare plain number, no separators
)


def _strip_dates_and_sections(text: str) -> str:
    for pat in _DATE_PATTERNS:
        text = re.sub(pat, " ", text, flags=re.IGNORECASE)
    text = re.sub(_SECTION_REF_PATTERN, " ", text)
    return text


def _normalize(token: str) -> float:
    cleaned = token.replace("₹", "").replace(",", "").replace("%", "").strip()
    return float(cleaned)


def extract_numbers(narrative_text: str) -> list:
    cleaned = _strip_dates_and_sections(narrative_text)
    found = set()
    for m in _NUMBER_PATTERN.finditer(cleaned):
        try:
            found.add(_normalize(m.group()))
        except ValueError:
            continue
    return sorted(found)


def check_narration(narrative_text: str, structured_values: list, tolerance: float = 0.01) -> dict:
    allowed = set()
    for v in structured_values:
        if v is None:
            continue
        allowed.add(round(float(v), 2))
        allowed.add(round(float(v) * 100, 2))  # allow the same figure expressed as a % (e.g. rate * 100 forms)

    found = extract_numbers(narrative_text)
    not_allowed = [n for n in found if not any(abs(n - a) <= tolerance for a in allowed)]

    return {
        "passed": len(not_allowed) == 0,
        "numbers_found_in_narrative": found,
        "numbers_not_in_structured_result": not_allowed,
    }
