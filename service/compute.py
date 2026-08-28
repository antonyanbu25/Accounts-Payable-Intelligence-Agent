"""
Deterministic compute engine for the Accounts Payable Intelligence Agent.

This module is the ONLY place money is calculated. It never calls an LLM
and takes no free-text input -- every function here is pure, deterministic
Python over structured numbers, matching the "grounding matters more than
fluency" requirement: nothing here can hallucinate.

See plan v4, section "Compute Contract" for the rules this implements.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional


def r2(value) -> float:
    """Round to the nearest rupee (whole number), per the plan's rounding policy."""
    return float(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------
# GST split (place-of-supply rule) — see tax_docs/gst_place_of_supply.md
# --------------------------------------------------------------------------

def determine_gst_split_type(vendor_state: str, office_state: str) -> str:
    """Intra-state (same state) -> CGST+SGST. Inter-state (different) -> IGST."""
    return "CGST_SGST" if vendor_state == office_state else "IGST"


def compute_gst(base_amount: float, rate_pct: float, split_type: str) -> dict:
    """Applies the rate to the base amount, then splits per the place-of-supply rule.
    Rounding happens on the total GST amount first, then the split is derived from
    that already-rounded total, so cgst+sgst (or igst alone) always sum exactly
    back to gst_amount -- no silent rounding drift between the two views."""
    gst_amount = r2(base_amount * rate_pct / 100)
    if split_type == "IGST":
        return {"gst_amount": gst_amount, "split_type": "IGST", "igst": gst_amount,
                "cgst": None, "sgst": None}
    half = r2(gst_amount / 2)
    other_half = gst_amount - half  # keeps cgst+sgst == gst_amount exactly, even on odd totals
    return {"gst_amount": gst_amount, "split_type": "CGST_SGST", "igst": None,
            "cgst": half, "sgst": other_half}


# --------------------------------------------------------------------------
# TDS — CBDT Circular 23/2017: computed on the base amount, EXCLUDING GST
# --------------------------------------------------------------------------

def compute_tds(base_amount: float, tds_rate_pct: float) -> float:
    return r2(base_amount * tds_rate_pct / 100)


# --------------------------------------------------------------------------
# 3-way match — tax-exclusive amounts only, tolerance = greater of ₹100 or 2% of PO
# --------------------------------------------------------------------------

@dataclass
class ThreeWayMatchResult:
    matched: bool
    po_amount: float
    receipt_amount: float
    invoice_base_amount: float
    max_diff: float
    tolerance: float


def check_three_way_match(po_amount: Optional[float], receipt_amount: Optional[float],
                           invoice_base_amount: float) -> Optional[ThreeWayMatchResult]:
    """Returns None for a non-PO invoice (maverick spend) — there is nothing to match."""
    if po_amount is None or receipt_amount is None:
        return None
    tolerance = max(100.0, 0.02 * po_amount)
    max_diff = max(abs(invoice_base_amount - po_amount), abs(invoice_base_amount - receipt_amount),
                    abs(po_amount - receipt_amount))
    return ThreeWayMatchResult(matched=max_diff <= tolerance, po_amount=po_amount,
                                receipt_amount=receipt_amount, invoice_base_amount=invoice_base_amount,
                                max_diff=r2(max_diff), tolerance=r2(tolerance))


# --------------------------------------------------------------------------
# Vendor-state conflict — GSTIN is ALWAYS authoritative for tax jurisdiction.
# This is a real legal rule, not a judgment call, so it's resolved silently
# (with a flag), never refused.
# --------------------------------------------------------------------------

@dataclass
class StateResolution:
    authoritative_state: str
    conflict_flagged: bool
    portal_value: Optional[str] = None
    financial_db_value: Optional[str] = None


def resolve_vendor_state(gstin_registered_state: str, onboarding_submitted_state: str) -> StateResolution:
    conflict = gstin_registered_state != onboarding_submitted_state
    return StateResolution(
        authoritative_state=gstin_registered_state,
        conflict_flagged=conflict,
        portal_value=onboarding_submitted_state if conflict else None,
        financial_db_value=gstin_registered_state if conflict else None,
    )


# --------------------------------------------------------------------------
# Category conflict (PO vs. Invoice) — NO defensible authority rule exists.
# This one genuinely blocks every tax-dependent figure.
# --------------------------------------------------------------------------

@dataclass
class CategoryConflict:
    po_category: str
    invoice_category: str


# --------------------------------------------------------------------------
# The main computation
# --------------------------------------------------------------------------

@dataclass
class LedgerFacts:
    base_amount: float
    advances_applied: float = 0.0
    credits_applied: float = 0.0
    payments_made: float = 0.0
    unapplied_advances: float = 0.0          # advisory only, never netted
    po_amount: Optional[float] = None
    receipt_amount: Optional[float] = None
    vendor_status: str = "active"            # 'active' | 'blocked'
    po_status: Optional[str] = None          # 'issued' | 'amended' | 'closed' | 'cancelled'
    invoice_status: str = "open"             # 'open' | 'cancelled' | 'disputed'


@dataclass
class TaxDetermination:
    """What /tax-lookup would hand to /compute. Built by hand here for the
    Day-2 compute-engine tests; produced by the real /tax-lookup endpoint
    once Source C ingestion + retrieval exists."""
    gst_rate_pct: float
    tds_rate_pct: float
    tds_section: Optional[str]
    split_type: str          # "CGST_SGST" | "IGST"
    source_citations: list = field(default_factory=list)


@dataclass
class ComputeResult:
    base_amount: float
    gst: Optional[dict]
    tds_amount: Optional[float]
    gross_liability: Optional[float]
    net_disbursement_due: Optional[float]
    pre_tax_ledger_position: Optional[float]
    eligibility: str
    eligibility_reasons: list
    three_way_match: Optional[ThreeWayMatchResult]
    unapplied_advance_advisory: Optional[float]
    category_conflict: Optional[CategoryConflict]
    tax_treatment_refused: bool


def compute(facts: LedgerFacts, tax: Optional[TaxDetermination],
            category_conflict: Optional[CategoryConflict] = None) -> ComputeResult:
    """
    The single entry point. If `category_conflict` is set, all tax-dependent
    figures are refused together and only the pre-tax ledger position is
    returned -- per the plan's category-conflict special case. `tax` must be
    None in that case (there is nothing to compute a rate for yet).
    """
    three_way = check_three_way_match(facts.po_amount, facts.receipt_amount, facts.base_amount)

    # Payment eligibility is evaluated independently of whether tax could be
    # determined -- a blocked vendor or cancelled PO gates eligibility either way.
    reasons = []
    if facts.vendor_status == "blocked":
        reasons.append("vendor blocked")
    if facts.po_status == "cancelled":
        reasons.append("PO cancelled")
    if facts.invoice_status in ("cancelled", "disputed"):
        reasons.append(f"invoice {facts.invoice_status}")
    if three_way is not None and not three_way.matched:
        reasons.append("3-way match failed")
    eligibility = "eligible" if not reasons else "not eligible: " + ", ".join(reasons)

    pre_tax_ledger_position = r2(
        facts.base_amount - facts.advances_applied - facts.credits_applied - facts.payments_made
    )

    if category_conflict is not None:
        return ComputeResult(
            base_amount=facts.base_amount, gst=None, tds_amount=None,
            gross_liability=None, net_disbursement_due=None,
            pre_tax_ledger_position=pre_tax_ledger_position,
            eligibility="on hold: category unresolved",
            eligibility_reasons=reasons + ["category unresolved (PO vs. invoice disagree, no rule to resolve)"],
            three_way_match=three_way,
            unapplied_advance_advisory=facts.unapplied_advances or None,
            category_conflict=category_conflict,
            tax_treatment_refused=True,
        )

    assert tax is not None, "tax determination required when there is no category conflict"

    gst = compute_gst(facts.base_amount, tax.gst_rate_pct, tax.split_type)
    tds_amount = compute_tds(facts.base_amount, tax.tds_rate_pct)
    gross_liability = r2(facts.base_amount + gst["gst_amount"])
    net_disbursement_due = r2(
        gross_liability - tds_amount - facts.advances_applied - facts.credits_applied - facts.payments_made
    )

    return ComputeResult(
        base_amount=facts.base_amount, gst=gst, tds_amount=tds_amount,
        gross_liability=gross_liability, net_disbursement_due=net_disbursement_due,
        pre_tax_ledger_position=pre_tax_ledger_position,
        eligibility=eligibility, eligibility_reasons=reasons,
        three_way_match=three_way,
        unapplied_advance_advisory=facts.unapplied_advances or None,
        category_conflict=None,
        tax_treatment_refused=False,
    )
