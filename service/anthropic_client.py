"""
Direct Anthropic SDK access for the native orchestration layer, replacing
the HTTP nodes n8n's build_uc1_workflow.py / build_uc2_workflow.py used to
call https://api.anthropic.com/v1/messages. Every prompt/schema built from
here must match the n8n originals verbatim -- these are real, previously
debugged pieces of prompt engineering (pronoun resolution, topic-drift
guards, the various MUST-state disclosure notes), not free text to
paraphrase.

get_client() mirrors main.py's get_corpus() singleton pattern (@lru_cache,
one client for the process lifetime).
"""
import os
from functools import lru_cache

from anthropic import Anthropic

MODEL = "claude-sonnet-4-5-20250929"


@lru_cache()
def get_client() -> Anthropic:
    return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def call_text(prompt: str, max_tokens: int, model: str = MODEL) -> str:
    """Generic narration wrapper -- used by all 7 narration call sites (5 in
    UC1, 2 in UC2). Returns the plain text of the first content block."""
    resp = get_client().messages.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


def call_tool_forced(tool: dict, messages: list, max_tokens: int,
                      temperature: float = None, model: str = MODEL) -> dict:
    """Generic forced-tool-use wrapper -- used by Parse Intent. Same
    defensive "find the tool_use block or raise" pattern already used in
    service/ingest.py's extract_metadata, not a new convention."""
    kwargs = dict(
        model=model, max_tokens=max_tokens,
        tools=[tool], tool_choice={"type": "tool", "name": tool["name"]},
        messages=messages,
    )
    if temperature is not None:
        kwargs["temperature"] = temperature
    resp = get_client().messages.create(**kwargs)
    for block in resp.content:
        if block.type == "tool_use":
            return block.input
    raise RuntimeError(f"No tool_use block returned for tool '{tool['name']}'")


# --------------------------------------------------------------------------
# Parse Intent tool schema -- copied character-for-character from
# PARSE_INTENT_BODY_EXPR in n8n/build_uc1_workflow.py. Every field
# description is load-bearing prompt engineering from real prior bug fixes
# (pronoun resolution, topic-drift anti-carryover, the primary-vs-additional
# invoice id split for comparison questions) -- never paraphrase these.
# --------------------------------------------------------------------------
PARSE_AP_QUESTION_TOOL = {
    "name": "parse_ap_question",
    "description": (
        "Parse the LATEST user question (the last item in messages) into structured intent. "
        "If earlier turns are present in messages, use them ONLY to resolve a pronoun or implicit "
        "reference in the latest question ('them', 'that vendor', 'the same invoice') -- never to "
        "re-answer a question the user isn't currently asking, and never to keep a fact 'active' "
        "once the conversation has moved on to something unrelated. If the latest question does not "
        "ask about a vendor balance, tax treatment, or general vendor information, set intent to "
        "'unsupported'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["balance_lookup", "tax_lookup", "combined_lookup", "vendor_lookup", "unsupported"],
                "description": (
                    "'vendor_lookup' is for general vendor information that ISN'T a specific balance or "
                    "tax calculation -- e.g. 'what state is this vendor registered in', 'what other "
                    "invoices does this vendor have', 'give me this vendor's details', 'is this vendor "
                    "active or blocked'. Use 'balance_lookup'/'tax_lookup'/'combined_lookup' when "
                    "the question is asking to compute or state a monetary/tax figure for a specific "
                    "invoice or transaction, OR for a purchase category in general (no specific vendor "
                    "or invoice at all) -- e.g. 'what GST rate applies to Furniture purchases in "
                    "general' is 'tax_lookup' too, use 'tax_lookup' for it, NEVER 'unsupported', even "
                    "with prior turns about a specific vendor/invoice still in history: a fresh, "
                    "vendor-less category-rate question is always answerable and must never be refused "
                    "just because it doesn't name a specific invoice or transaction -- that's exactly "
                    "what the category_mentioned field exists to capture."
                ),
            },
            "vendor_name_mentioned": {
                "type": "string",
                "description": (
                    "The vendor the LATEST question is about. If it names a vendor, use that name "
                    "verbatim. If it instead refers back to a vendor via a pronoun or implicit reference "
                    "('them', 'that vendor', 'the same company') and exactly one vendor was discussed in "
                    "the immediately preceding turn(s), use that vendor's name as it appeared earlier. If "
                    "the latest question is a fresh question that doesn't reference any vendor (e.g. a "
                    "category-only question, or a change of topic), leave this empty even if a different "
                    "vendor was named earlier -- do not default to the last-mentioned vendor just because "
                    "one exists in the history."
                ),
            },
            "invoice_id_mentioned": {
                "type": ["integer", "null"],
                "description": (
                    "If the latest question references a specific invoice by number (e.g. 'INV-9', "
                    "'invoice 17', 'invoice #12'), extract JUST the numeric id as an integer (e.g. 9, 17, "
                    "12) -- the FIRST/primary one mentioned, if more than one. If it instead unambiguously "
                    "refers back to a specific invoice discussed in the immediately preceding turn (e.g. "
                    "'is that invoice correctly taxed?' right after INV-17 was discussed), use that "
                    "invoice's id. Do NOT carry an invoice id forward from earlier in the conversation "
                    "once the topic has moved on -- if the latest question names a different vendor, or "
                    "doesn't itself imply continuity with a specific invoice, use null. Never guess one. "
                    "If a SECOND invoice is also named for comparison ('...invoice 13, and does it differ "
                    "from invoice 14?'), put that second one in additional_invoice_ids_mentioned instead, "
                    "not here."
                ),
            },
            "additional_invoice_ids_mentioned": {
                "type": "array",
                "items": {"type": "integer"},
                "description": (
                    "If the latest question asks to COMPARE the primary invoice (in invoice_id_mentioned) "
                    "against one or more OTHER invoices of the SAME vendor -- e.g. 'does it differ from "
                    "invoice 14', 'compare INV-9 and INV-11', 'and invoice 15 too' -- list every other "
                    "invoice id mentioned here, in the order named. Empty array [] for the overwhelming "
                    "majority of questions, which are about a single invoice (or no invoice at all). Do "
                    "NOT populate this for a question naming two DIFFERENT vendors (comparison is only "
                    "supported within one vendor), and never repeat the same id that is already in "
                    "invoice_id_mentioned."
                ),
            },
            "category_mentioned": {
                "type": ["string", "null"],
                "enum": ["Furniture", "Software", "Services", "Food", "Appliances", None],
                "description": (
                    "If the latest question is about a purchase category in general (no specific vendor "
                    "named or resolved from history), which of these fixed categories it refers to. Null "
                    "if a specific vendor was named (or resolved from history) instead, or if no category "
                    "is identifiable in the latest question itself."
                ),
            },
            "explicit_date_mentioned": {
                "type": ["string", "null"],
                "description": (
                    "If the question states ANY date reference -- a full date ('1 September 2025' -> "
                    "2025-09-01), or just a bare year ('in 2010', 'during 2018') -> use January 1 of that "
                    "year (2010-01-01, 2018-01-01). Null ONLY if no date or year is mentioned at all -- do "
                    "not default to today yourself, that is handled downstream."
                ),
            },
        },
        "required": [
            "intent", "vendor_name_mentioned", "invoice_id_mentioned", "additional_invoice_ids_mentioned",
            "category_mentioned", "explicit_date_mentioned",
        ],
    },
}


def parse_intent(question: str, history: list = None) -> dict:
    """Port of PARSE_INTENT_BODY_EXPR's messages construction: prior history
    (role/content only, exactly mirroring the JS .map(...).concat(...)) plus
    the current question appended as the final user message. temperature=0,
    max_tokens=300, matching the original exactly."""
    messages = [{"role": h["role"], "content": h["content"]} for h in (history or [])]
    messages.append({"role": "user", "content": question})
    return call_tool_forced(PARSE_AP_QUESTION_TOOL, messages, max_tokens=300, temperature=0)
