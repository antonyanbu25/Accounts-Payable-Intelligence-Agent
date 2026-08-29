"""
FastAPI service: the deterministic compute + tax-lookup + narration-guard
layer. n8n calls these endpoints; this service never talks to an LLM about
money, and the LLM never talks to this service's math directly -- n8n sits
between them, passing structured JSON both ways.
"""
import os
from functools import lru_cache

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

from compute import (  # noqa: E402
    CategoryConflict, ComputeResult, LedgerFacts, TaxDetermination, compute, determine_gst_split_type,
)
from diff import diff_advice  # noqa: E402
from models import (  # noqa: E402
    ComputeRequest, ComputeResponse, DiffRequest, DiffResponse, FieldDiffModel, NarrationCheckRequest,
    NarrationCheckResponse, TaxLookupRequest, TaxLookupResponse, TaxLookupSubResult,
)
from narration_guard import check_narration  # noqa: E402
from tax_lookup import TaxCorpus  # noqa: E402

app = FastAPI(title="AP Intelligence Agent — Compute & Tax Lookup Service")


@lru_cache()
def get_corpus() -> TaxCorpus:
    return TaxCorpus()


@app.get("/health")
def health():
    try:
        n = len(get_corpus().docs)
    except Exception as e:
        return {"status": "degraded", "detail": str(e)}
    return {"status": "ok", "tax_documents_loaded": n}


def _sub_result(lookup_result) -> TaxLookupSubResult:
    return TaxLookupSubResult(
        status=lookup_result.status,
        rate_pct=lookup_result.rate_pct,
        tds_section=lookup_result.tds_section,
        source_filename=lookup_result.source_filename,
        key_clause=lookup_result.key_clause,
        candidate_count=len(lookup_result.candidates),
    )


@app.post("/tax-lookup", response_model=TaxLookupResponse)
def tax_lookup(req: TaxLookupRequest):
    corpus = get_corpus()
    # determine_gst_split_type already returns exactly one of "CGST_SGST" /
    # "IGST" / "UNKNOWN" -- passed straight through, not collapsed. (Was
    # previously silently collapsed to CGST_SGST/IGST, so an UNKNOWN would
    # have been coerced into a claimed-CGST_SGST answer the moment this
    # branch was added -- caught before ever running.)
    split_type = determine_gst_split_type(req.vendor_state, req.office_state)

    gst_result = corpus.lookup_rate("GST_RATE", req.category, req.hsn_or_sac, req.invoice_date)
    tds_result = None
    if req.needs_tds:
        tds_result = corpus.lookup_rate("TDS_RATE", req.category, req.hsn_or_sac, req.invoice_date)

    return TaxLookupResponse(
        split_type=split_type,
        gst=_sub_result(gst_result),
        tds=_sub_result(tds_result) if tds_result else None,
    )


@app.post("/compute", response_model=ComputeResponse)
def do_compute(req: ComputeRequest):
    facts = LedgerFacts(
        base_amount=req.base_amount,
        advances_applied=req.advances_applied,
        credits_applied=req.credits_applied,
        payments_made=req.payments_made,
        unapplied_advances=req.unapplied_advances,
        po_amount=req.po_amount,
        receipt_amount=req.receipt_amount,
        vendor_status=req.vendor_status,
        po_status=req.po_status,
        invoice_status=req.invoice_status,
    )

    category_conflict = None
    if req.category_conflict_po_category and req.category_conflict_invoice_category:
        category_conflict = CategoryConflict(
            po_category=req.category_conflict_po_category,
            invoice_category=req.category_conflict_invoice_category,
        )
        tax = None
    else:
        if req.gst_rate_pct is None:
            raise HTTPException(400, "gst_rate_pct is required unless a category_conflict is supplied")
        tax = TaxDetermination(
            gst_rate_pct=req.gst_rate_pct, tds_rate_pct=req.tds_rate_pct or 0.0,
            tds_section=req.tds_section, split_type=req.split_type or "CGST_SGST",
        )

    result = compute(facts, tax, category_conflict)

    return ComputeResponse(
        base_amount=result.base_amount,
        gst=result.gst,
        tds_amount=result.tds_amount,
        gross_liability=result.gross_liability,
        net_disbursement_due=result.net_disbursement_due,
        pre_tax_ledger_position=result.pre_tax_ledger_position,
        eligibility=result.eligibility,
        eligibility_reasons=result.eligibility_reasons,
        three_way_match=(result.three_way_match.__dict__ if result.three_way_match else None),
        unapplied_advance_advisory=result.unapplied_advance_advisory,
        category_conflict=(result.category_conflict.__dict__ if result.category_conflict else None),
        tax_treatment_refused=result.tax_treatment_refused,
    )


@app.post("/diff", response_model=DiffResponse)
def do_diff(req: DiffRequest):
    """Compares an independently-computed true result (from /compute) against a
    submitted advice. The caller is responsible for ensuring /compute was run
    WITHOUT ever seeing the submitted advice -- this endpoint only compares,
    it never re-derives the true result itself."""
    cr = req.compute_result
    tr = ComputeResult(
        base_amount=cr.get("base_amount"), gst=cr.get("gst"), tds_amount=cr.get("tds_amount"),
        gross_liability=cr.get("gross_liability"), net_disbursement_due=cr.get("net_disbursement_due"),
        pre_tax_ledger_position=cr.get("pre_tax_ledger_position"), eligibility=cr.get("eligibility", ""),
        eligibility_reasons=cr.get("eligibility_reasons", []), three_way_match=None,
        unapplied_advance_advisory=cr.get("unapplied_advance_advisory"),
        category_conflict=cr.get("category_conflict"), tax_treatment_refused=cr.get("tax_treatment_refused", False),
    )
    result = diff_advice(tr, req.submitted_advice.model_dump(), category_reason=req.category_reason or "")
    return DiffResponse(
        overall_match=result.overall_match, blocked=result.blocked, blocked_reason=result.blocked_reason,
        fields=[FieldDiffModel(field=f.field, claimed=f.claimed, correct=f.correct, match=f.match, reason=f.reason)
                for f in result.fields],
        eligible=result.eligible, eligibility_reasons=result.eligibility_reasons,
        unapplied_advance_advisory=result.unapplied_advance_advisory,
        split_undetermined=result.split_undetermined, split_undetermined_note=result.split_undetermined_note,
    )


@app.post("/check-narration", response_model=NarrationCheckResponse)
def check_narration_endpoint(req: NarrationCheckRequest):
    result = check_narration(req.narrative_text, req.structured_values)
    return NarrationCheckResponse(**result)
