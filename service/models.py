"""Pydantic request/response models for the FastAPI service."""
from datetime import date
from typing import Optional

from pydantic import BaseModel


class TaxLookupRequest(BaseModel):
    category: str
    hsn_or_sac: str
    invoice_date: date
    vendor_state: str
    office_state: str
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


class FieldDiffModel(BaseModel):
    field: str
    claimed: Optional[float] = None
    correct: Optional[float] = None
    match: bool
    reason: str = ""


class DiffResponse(BaseModel):
    overall_match: bool
    blocked: bool
    blocked_reason: str = ""
    fields: list[FieldDiffModel]


class NarrationCheckRequest(BaseModel):
    narrative_text: str
    structured_values: list  # every number that's legitimately allowed to appear


class NarrationCheckResponse(BaseModel):
    passed: bool
    numbers_found_in_narrative: list
    numbers_not_in_structured_result: list
