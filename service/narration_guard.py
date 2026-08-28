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
    # Month DD, YYYY (US convention -- day AFTER the month name, e.g. "June
    # 15, 2030"). Found by live testing: only the DD-Month-YYYY order was
    # handled before, leaving a bare day number unstripped and flagged.
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}\s*,?\s*\d{4}\b",
    r"\b\d{4}-\d{2}-\d{2}\b",
    r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
    # bare 4-digit year (e.g. "Income Tax Act, 1961", "Notification ... 2017")
    # -- found by live testing: real amounts in this domain are never bare,
    # un-grouped 4-digit numbers landing in this narrow 1900-2099 range, so
    # this is safe here even though it's not a universal rule.
    r"\b(19|20)\d{2}\b",
]

# Real HSN/SAC classification codes used in this project's tax_docs corpus --
# legitimate for the narration to quote verbatim from a source clause, never
# a monetary figure. A fixed, known, finite list (not user input), so
# whitelisting them by value doesn't weaken protection against a genuinely
# invented amount the way a blanket "long bare number = code" rule would.
_KNOWN_CODES = ["9403", "998313", "998311", "996331", "8516", "6810", "2523"]

# A number immediately followed by a letter (e.g. "194J") is a section
# reference, not an amount -- \b after the digits requires a non-word
# boundary, so a plain \d+\b pattern naturally can't isolate "194" from
# "194J" anyway; this pattern makes that exclusion explicit and robust.
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
    for code in _KNOWN_CODES:
        text = re.sub(r"\b" + re.escape(code) + r"\b", " ", text)
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
