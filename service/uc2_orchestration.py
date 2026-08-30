"""
Native Python replacement for n8n's UC2 workflow (n8n/build_uc2_workflow.py)
-- validation of a human-prepared payment advice. Every branch, SQL query,
prompt, and response shape here is a deliberate, verbatim port of that file;
re-read it directly if anything here looks like it needs adjusting, rather
than trusting memory.

Deliberately has no intent-parsing LLM call and no vendor-name resolution --
the input is already structured JSON (invoice_id + submitted_advice), same
as the original. The retrieval -> tax-lookup -> compute chain never sees
submitted_advice before do_compute() runs -- that's what makes this a
genuine blind reconstruction, not a review of the human's numbers. This is
enforced structurally here: submitted_advice is simply not in scope for the
functions that build the /compute request.

IMPORT-ORDER NOTE: this module does `from main import tax_lookup, do_compute,
do_diff, check_narration_endpoint` -- those are main.py's existing FastAPI
route functions, called directly as plain Python (no HTTP self-call). This
only works because main.py imports THIS module at the *bottom* of its file,
after those four functions are already defined -- see main.py's own comment
at that import. Anything that imports uc2_orchestration directly (tests
included) must import `main` first (e.g. `import main` or construct a
`TestClient(main.app)`), or this module's own top-level import will try to
execute main.py from scratch and hit the same not-yet-defined-names problem
in the other direction. Tests that mock tax_lookup/do_compute/do_diff/
check_narration_endpoint must patch the names as bound *inside this module*
(`uc2_orchestration.tax_lookup`, not `main.tax_lookup`) -- standard
"from X import Y" patching.
"""
import json
from datetime import date

import db
from anthropic_client import call_text
from main import check_narration_endpoint, do_compute, do_diff, tax_lookup
from models import ComputeRequest, DiffRequest, NarrationCheckRequest, TaxLookupRequest
from shared_constants import HSN_SAC_BY_CATEGORY

NARRATE_DIFF_PROMPT_PREFIX = (
    "You are validating an accountant's payment advice against an independently-computed true result. "
    "Write a short, plain-language verdict (3-7 sentences): state clearly whether the advice's NUMBERS are "
    "CORRECT or have DIVERGENCES, and for every mismatched field state what was claimed, what's correct, and "
    "briefly why -- using ONLY the numbers and reasons in this JSON, never inventing or altering one. If "
    "overall_match is true, say so plainly and do not invent a divergence. SEPARATELY and REGARDLESS of "
    "whether the numbers match: if eligible is false, you MUST prominently warn that this payment is NOT clear "
    "to release and state every reason in eligibility_reasons -- a numerically-correct advice for an "
    "ineligible payment (blocked vendor, cancelled PO, failed 3-way match) is still not safe to pay, and this "
    "warning must never be omitted or softened. ALSO SEPARATELY: if unapplied_advance_advisory is present and "
    "non-zero, state it plainly as its own note -- an advance sitting against this vendor/PO that hasn't been "
    "netted into this figure is a real overpayment risk, and a numerically-correct advice must never read as "
    "unconditionally 'clear to release in full' when one exists; never omit or soften this note either. ALSO "
    "SEPARATELY: if split_undetermined is true, state plainly (using split_undetermined_note) that the "
    "CGST/SGST-vs-IGST split could not be independently verified because this invoice has no linked purchase "
    "order/office -- do NOT claim the split was checked or that it matches/doesn't match, and do not let this "
    "read as a numeric divergence; the total GST amount and every other field were still fully verified. "
    "Diff result: "
)

NEW_INVOICE_NARRATE_PROMPT_PREFIX = (
    "You are checking a DRAFT payment advice for an invoice that is NOT YET recorded in the system -- there is "
    "no purchase order, receipt, or invoice record to reconstruct against, so this is a REDUCED check: only "
    "whether the submitted GST rate, TDS amount, and net-payable math use the CURRENT correct tax rate for the "
    "stated category and date. The submitted base amount is taken as given, not independently verified. Do NOT "
    "say the payment is eligible, matched, or clear to release, and do NOT mention a 3-way match -- neither can "
    "be assessed without a real record. Write a short, plain-language verdict (3-5 sentences), using ONLY the "
    "numbers in this JSON, never inventing one, and explicitly note this is a rate-only check on an unrecorded "
    "invoice. Diff result: "
)

NEW_INVOICE_NOTE = (
    "This invoice is not yet recorded in the system -- only the GST rate, TDS, and net-payable math were "
    "checked against current category tax rules. Base amount, 3-way match, and vendor eligibility could not "
    "be verified."
)

GUARD_FALLBACK_SUFFIX = " [Showing the verified figures directly -- the written summary did not pass our accuracy check.]"


def _num(v) -> str:
    """Match JS's implicit number-to-string coercion in the fallback verdict
    builders (e.g. 'claimed ' + f.claimed) -- an integral float prints
    without a trailing .0, same as JS."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _fallback_verdict_existing(diff) -> str:
    """Verbatim port of respond_fallback's JS in n8n/build_uc2_workflow.py."""
    mism = [f"{f.field}: claimed {_num(f.claimed)}, correct {_num(f.correct)}" for f in diff.fields if not f.match]
    num_verdict = "Advice matches the independently computed result." if diff.overall_match \
        else "Advice diverges on: " + "; ".join(mism) + "."
    elig_verdict = "" if diff.eligible \
        else " ⚠ NOT CLEAR TO PAY regardless of the numbers above: " + "; ".join(diff.eligibility_reasons) + "."
    advance_verdict = ""
    if diff.unapplied_advance_advisory is not None and diff.unapplied_advance_advisory != 0:
        advance_verdict = (" ⚠ Advisory: an unapplied advance of " + _num(diff.unapplied_advance_advisory)
                            + " exists against this vendor/PO and has NOT been netted here -- a possible "
                              "overpayment risk if missed.")
    split_verdict = f" ℹ {diff.split_undetermined_note}" if diff.split_undetermined else ""
    return num_verdict + elig_verdict + advance_verdict + split_verdict + GUARD_FALLBACK_SUFFIX


def _fallback_verdict_new_invoice(diff) -> str:
    """Verbatim port of respond_fallback_new_invoice's JS -- mism excludes
    base_amount (never independently verified in this mode)."""
    mism = [f"{f.field}: claimed {_num(f.claimed)}, correct {_num(f.correct)}"
            for f in diff.fields if not f.match and f.field != "base_amount"]
    num_verdict = "Submitted GST/TDS math matches the current category tax rate." if not mism \
        else "Divergence found: " + "; ".join(mism) + "."
    return num_verdict + GUARD_FALLBACK_SUFFIX


def _guard_values_existing(diff) -> list:
    """Port of CHECK_NARRATION_BODY_EXPR's structured_values construction --
    is not None, never truthiness, so a legitimate $0 claimed/correct
    survives. Numeric fields only -- the "category" row's claimed/correct
    are strings (e.g. "Furniture"), and check_narration()'s float(v) call
    would raise on one; category names are guarded by narrative-text
    presence, not this numeral check, so excluding them here is correct,
    not just a crash workaround."""
    nums = []
    for f in diff.fields:
        if isinstance(f.claimed, (int, float)):
            nums.append(f.claimed)
        if isinstance(f.correct, (int, float)):
            nums.append(f.correct)
    if diff.unapplied_advance_advisory is not None:
        nums.append(diff.unapplied_advance_advisory)
    return nums


def _guard_values_new_invoice(diff) -> list:
    """Port of NEW_INVOICE_CHECK_NARRATION_BODY_EXPR -- no
    unapplied_advance_advisory (not applicable/not present in this branch)."""
    nums = []
    for f in diff.fields:
        if f.claimed is not None:
            nums.append(f.claimed)
        if f.correct is not None:
            nums.append(f.correct)
    return nums


def handle_uc2_validate(invoice_id, submitted_advice: dict, invoice_date_str: str = None) -> dict:
    if invoice_id is not None:
        return _handle_existing_invoice(invoice_id, submitted_advice)
    return _handle_new_invoice(submitted_advice, invoice_date_str)


def _handle_existing_invoice(invoice_id: int, submitted_advice: dict) -> dict:
    facts = db.retrieve_invoice_facts_uc2(invoice_id)
    if facts is None:
        return {"error": "not_found",
                "message": f"No invoice found with id {invoice_id} -- cannot validate an advice against a "
                            "record that does not exist."}

    tax = tax_lookup(TaxLookupRequest(
        category=facts["invoice_category"], hsn_or_sac=facts["invoice_hsn_sac"],
        invoice_date=facts["invoice_date"], vendor_state=facts["registered_state"],
        office_state=facts["office_state"], needs_tds=True,
    ))

    conflict = bool(facts["po_category"]) and bool(facts["invoice_category"]) and facts["po_category"] != facts["invoice_category"]
    compute_kwargs = dict(
        base_amount=facts["base_amount"], advances_applied=facts["advances_applied"],
        credits_applied=facts["credits_applied"], payments_made=facts["payments_made"],
        unapplied_advances=facts["unapplied_advances"], po_amount=facts["po_amount"],
        receipt_amount=facts["received_amount"], vendor_status=facts["vendor_status"],
        po_status=facts["po_status"], invoice_status=facts["invoice_status"],
    )
    if conflict:
        compute_kwargs["category_conflict_po_category"] = facts["po_category"]
        compute_kwargs["category_conflict_invoice_category"] = facts["invoice_category"]
    else:
        compute_kwargs["gst_rate_pct"] = tax.gst.rate_pct
        compute_kwargs["tds_rate_pct"] = tax.tds.rate_pct if tax.tds else 0.0
        compute_kwargs["tds_section"] = tax.tds.tds_section if tax.tds else None
        compute_kwargs["split_type"] = tax.split_type
    computed = do_compute(ComputeRequest(**compute_kwargs))

    category_reason = ""
    advice_category = submitted_advice.get("category")
    if advice_category and facts["invoice_category"] and advice_category != facts["invoice_category"]:
        category_reason = (f"Submitted category '{advice_category}' does not match the invoice on file "
                            f"('{facts['invoice_category']}').")

    diff = do_diff(DiffRequest(
        compute_result=computed.model_dump(), submitted_advice=submitted_advice, category_reason=category_reason,
        category_claimed=advice_category or "", category_correct=facts["invoice_category"] or "",
    ))

    prompt = NARRATE_DIFF_PROMPT_PREFIX + json.dumps(diff.model_dump())
    narrative = call_text(prompt, max_tokens=500)
    guard = check_narration_endpoint(NarrationCheckRequest(
        narrative_text=narrative, structured_values=_guard_values_existing(diff),
    ))

    if guard.passed:
        return {"verdict": narrative, "diff": diff.model_dump(), "tax_evidence": tax.model_dump(),
                "computed": computed.model_dump(), "guard": "passed"}
    return {"verdict": _fallback_verdict_existing(diff), "diff": diff.model_dump(),
            "tax_evidence": tax.model_dump(), "computed": computed.model_dump(), "guard": "failed_fallback_used"}


def _handle_new_invoice(submitted_advice: dict, invoice_date_str: str = None) -> dict:
    invoice_date_to_use = invoice_date_str or date.today().isoformat()
    category = submitted_advice.get("category")

    tax = tax_lookup(TaxLookupRequest(
        category=category, hsn_or_sac=HSN_SAC_BY_CATEGORY.get(category, ""),
        invoice_date=invoice_date_to_use, vendor_state="Karnataka", office_state="Karnataka", needs_tds=True,
    ))

    computed = do_compute(ComputeRequest(
        base_amount=submitted_advice.get("base_amount"), gst_rate_pct=tax.gst.rate_pct,
        tds_rate_pct=tax.tds.rate_pct if tax.tds else 0.0, tds_section=tax.tds.tds_section if tax.tds else None,
        split_type=tax.split_type,
    ))

    diff = do_diff(DiffRequest(
        compute_result=computed.model_dump(), submitted_advice=submitted_advice, category_reason="",
    ))

    prompt = NEW_INVOICE_NARRATE_PROMPT_PREFIX + json.dumps(diff.model_dump())
    narrative = call_text(prompt, max_tokens=400)
    guard = check_narration_endpoint(NarrationCheckRequest(
        narrative_text=narrative, structured_values=_guard_values_new_invoice(diff),
    ))

    if guard.passed:
        return {"mode": "category_only", "verdict": narrative, "diff": diff.model_dump(),
                "tax_evidence": tax.model_dump(), "computed": computed.model_dump(),
                "guard": "passed", "note": NEW_INVOICE_NOTE}
    return {"mode": "category_only", "verdict": _fallback_verdict_new_invoice(diff), "diff": diff.model_dump(),
            "tax_evidence": tax.model_dump(), "computed": computed.model_dump(),
            "guard": "failed_fallback_used", "note": NEW_INVOICE_NOTE}
