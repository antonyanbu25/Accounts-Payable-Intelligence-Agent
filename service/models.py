"""Pydantic request/response models for the FastAPI service."""
from datetime import date
from typing import Optional, Union

from pydantic import BaseModel


class TaxLookupRequest(BaseModel):
    category: str
    hsn_or_sac: str
    invoice_date: date
    vendor_state: str
    # Optional: a non-PO ("maverick spend") invoice has no requisition/office
    # chain to source this from. None is a real, distinct answer -- not an
    # error -- and is handled explicitly (see determine_gst_split_type).
    office_state: Optional[str] = None
    needs_tds: bool = True


class TaxLookupSubResult(BaseModel):
    status: str  # "found" | "ambiguous" | "not_found"
    rate_pct: Optional[float] = None
    tds_section: Optional[str] = None
    source_filename: Optional[str] = None
    key_clause: Optional[str] = None
    candidate_count: int = 0


class TaxLookupResponse(BaseModel):
    split_type: str  # "CGST_SGST" | "IGST"
    gst: TaxLookupSubResult
    tds: Optional[TaxLookupSubResult] = None


class ComputeRequest(BaseModel):
    base_amount: float
    gst_rate_pct: Optional[float] = None
    tds_rate_pct: Optional[float] = 0.0
    tds_section: Optional[str] = None
    split_type: Optional[str] = "CGST_SGST"
    advances_applied: float = 0.0
    credits_applied: float = 0.0
    payments_made: float = 0.0
    unapplied_advances: float = 0.0
    po_amount: Optional[float] = None
    receipt_amount: Optional[float] = None
    vendor_status: str = "active"
    po_status: Optional[str] = None
    invoice_status: str = "open"
    category_conflict_po_category: Optional[str] = None
    category_conflict_invoice_category: Optional[str] = None


class ComputeResponse(BaseModel):
    base_amount: float
    gst: Optional[dict] = None
    tds_amount: Optional[float] = None
    gross_liability: Optional[float] = None
    net_disbursement_due: Optional[float] = None
    pre_tax_ledger_position: Optional[float] = None
    eligibility: str
    eligibility_reasons: list
    three_way_match: Optional[dict] = None
    unapplied_advance_advisory: Optional[float] = None
    category_conflict: Optional[dict] = None
    tax_treatment_refused: bool
    # Previously computed but never returned -- see compute.py's ComputeResult
    # for why this matters (a correct net_disbursement_due that nothing could
    # explain when driven by a partial payment, since this figure was never
    # visible anywhere outside the opaque netted totals).
    advances_applied: float = 0.0
    credits_applied: float = 0.0
    payments_made: float = 0.0


class SubmittedAdvice(BaseModel):
    base_amount: Optional[float] = None
    category: Optional[str] = None
    gst_rate_pct: Optional[float] = None
    gst_cgst: Optional[float] = None
    gst_sgst: Optional[float] = None
    gst_igst: Optional[float] = None
    tds_rate_pct: Optional[float] = None
    tds_amount: Optional[float] = None
    net_payable_claimed: Optional[float] = None


class DiffRequest(BaseModel):
    compute_result: dict  # the exact JSON body returned by /compute
    submitted_advice: SubmittedAdvice
    category_reason: Optional[str] = None
    # The actual submitted-vs-correct category names, populated alongside
    # category_reason above -- see FieldDiffModel.claimed/.correct below for
    # why these are needed as real values rather than left as None.
    category_claimed: Optional[str] = None
    category_correct: Optional[str] = None


class FieldDiffModel(BaseModel):
    field: str
    # Union[float, str], not just float -- the "category" row is a genuine
    # string comparison. Found by an independent recruiter-style evaluation
    # (round 3): this was float-only with the category row hardcoded to
    # None, which the frontend's pandas.DataFrame silently coerced to NaN,
    # rendering as the literal text "nan" in that table cell.
    claimed: Optional[Union[float, str]] = None
    correct: Optional[Union[float, str]] = None
    match: bool
    reason: str = ""


class DiffResponse(BaseModel):
    overall_match: bool
    blocked: bool
    blocked_reason: str = ""
    fields: list[FieldDiffModel]
    eligible: bool = True
    eligibility_reasons: list[str] = []
    # An advance never gets netted into overall_match/eligible -- it's a
    # separate advisory a human must act on regardless of whether the
    # advice's numbers are correct. UC1 (ComputeResponse) already carries
    # this; UC2 was silently dropping it at this response boundary before
    # it ever reached the diff or the narration.
    unapplied_advance_advisory: Optional[float] = None
    # True when the true CGST/SGST-vs-IGST split couldn't be determined
    # (a non-PO invoice with no linked office to compare against the
    # vendor's state) -- base_amount/gst_rate_pct/tds_amount/net_payable
    # are still fully verified in this case, only the split classification
    # is withheld rather than guessed.
    split_undetermined: bool = False
    split_undetermined_note: str = ""


class NarrationCheckRequest(BaseModel):
    narrative_text: str
    structured_values: list  # every number that's legitimately allowed to appear


class NarrationCheckResponse(BaseModel):
    passed: bool
    numbers_found_in_narrative: list
    numbers_not_in_structured_result: list


# --------------------------------------------------------------------------
# UC1/UC2 orchestration request models -- the native-Python replacement for
# n8n's UC1/UC2 webhooks (see uc1_orchestration.py / uc2_orchestration.py).
# Response shapes are deliberately plain dict, not modeled here -- see
# main.py's /webhook/uc1-ask and /webhook/uc2-validate route comments.
# --------------------------------------------------------------------------

class HistoryTurn(BaseModel):
    role: str
    content: str


class UC1AskRequest(BaseModel):
    question: str
    history: Optional[list[HistoryTurn]] = None


class UC2ValidateRequest(BaseModel):
    invoice_id: Optional[int] = None
    submitted_advice: SubmittedAdvice
    invoice_date: Optional[str] = None
