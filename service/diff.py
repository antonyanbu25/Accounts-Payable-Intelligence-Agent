"""
UC2 diff engine: compares an accountant's submitted payment advice against
the independently-computed true result. Deterministic, tolerance-based
comparison -- never an LLM judgment call, same principle as compute.py.

Critically, this function is only ever called AFTER the true result has
been computed from source data alone -- the caller (main.py's /diff
endpoint) never lets the submitted advice influence /tax-lookup or
/compute. That ordering is what makes this a genuine "reconstruct
independently, then compare" check rather than a review of the human's
work.
"""
from dataclasses import dataclass, field
from typing import Optional, Union

from compute import ComputeResult, r2

TOLERANCE = 1.0  # ±₹1, absorbs rounding-order noise between how the agent and a human round


@dataclass
class FieldDiff:
    field: str
    # Union[float, str], not just float -- the "category" row (below) is a
    # genuine string comparison, not a number. Found by an independent
    # recruiter-style evaluation (round 3): this used to be typed float-only
    # with the category row's claimed/correct hardcoded to None, which the
    # frontend's pandas.DataFrame then silently coerced to NaN, rendering as
    # the literal text "nan" in that table cell.
    claimed: Optional[Union[float, str]]
    correct: Optional[Union[float, str]]
    match: bool
    reason: str = ""


@dataclass
class DiffResult:
    overall_match: bool
    fields: list = field(default_factory=list)
    blocked: bool = False  # true if the true result itself has refused tax treatment
    blocked_reason: str = ""
    # Deliberately separate from overall_match: whether the ADVICE'S NUMBERS
    # are correct is a different question from whether this PAYMENT is
    # actually clear to release (vendor blocked, PO cancelled, 3-way match
    # failed). Found by recruiter-mindset testing: a diff that only reports
    # numeric correctness lets a fully "correct" advice for an ineligible
    # payment go through with no warning -- a real AP control failure, not
    # just a missing nicety. Always populated, regardless of overall_match.
    eligible: bool = True
    eligibility_reasons: list = field(default_factory=list)
    # Same class of gap, found by a second independent recruiter-style
    # evaluation: an advance is never netted into overall_match either, and
    # was previously dropped entirely at this layer -- a numerically
    # "correct" advice for an invoice with an unapplied advance on file must
    # never read as unconditionally safe to pay in full.
    unapplied_advance_advisory: Optional[float] = None
    # True when the true GST split (compute.py's determine_gst_split_type)
    # came back "UNKNOWN" -- a non-PO invoice with no office to compare the
    # vendor's state against. gst_cgst/gst_sgst/gst_igst are deliberately
    # NOT added to `fields` in this case (there is no true value to grade
    # them against -- comparing to a fabricated same-state guess would be
    # worse than not comparing at all); base_amount/gst_rate_pct/tds_amount/
    # net_payable are still fully verified and graded as normal.
    split_undetermined: bool = False
    split_undetermined_note: str = ""


def _compare(field_name: str, claimed, correct, reason_if_mismatch: str) -> FieldDiff:
    if claimed is None or correct is None:
        match = claimed == correct
    else:
        match = abs(float(claimed) - float(correct)) <= TOLERANCE
    return FieldDiff(field=field_name, claimed=claimed, correct=correct, match=match,
                      reason="" if match else reason_if_mismatch)


def diff_advice(true_result: ComputeResult, submitted: dict, category_reason: str = "",
                 category_claimed: str = "", category_correct: str = "") -> DiffResult:
    is_eligible = true_result.eligibility == "eligible"
    eligibility_reasons = [] if is_eligible else (true_result.eligibility_reasons or [true_result.eligibility])

    if true_result.tax_treatment_refused:
        return DiffResult(
            overall_match=False, blocked=True,
            blocked_reason=("Cannot validate this advice: the correct tax treatment itself is unresolved "
                             "(category conflict between the PO and the invoice, no defensible rule to pick one). "
                             "The advice's tax-dependent fields cannot be checked until this is resolved."),
            fields=[_compare("base_amount", submitted.get("base_amount"), true_result.pre_tax_ledger_position or true_result.base_amount, "")],
            eligible=is_eligible, eligibility_reasons=eligibility_reasons,
            unapplied_advance_advisory=true_result.unapplied_advance_advisory,
        )

    gst = true_result.gst or {}
    fields = [
        _compare("base_amount", submitted.get("base_amount"), true_result.base_amount,
                  "Base amount does not match the invoice on file."),
        _compare("gst_rate_pct", submitted.get("gst_rate_pct"),
                  None if gst.get("gst_amount") is None else r2(100 * gst["gst_amount"] / true_result.base_amount) if true_result.base_amount else None,
                  "GST rate does not match the applicable rate for this category and date."),
    ]

    split_undetermined = gst.get("split_type") == "UNKNOWN"
    split_note = ""
    if split_undetermined:
        split_note = ("This invoice has no linked purchase order/office on file, so the CGST/SGST-vs-IGST "
                       "split can't be independently verified — only the total GST amount above was checked.")
    elif gst.get("split_type") == "IGST":
        fields.append(_compare("gst_igst", submitted.get("gst_igst"), gst.get("igst"),
                                "Submitted advice does not use IGST, but vendor and office are in different states."))
    else:
        fields.append(_compare("gst_cgst", submitted.get("gst_cgst"), gst.get("cgst"),
                                "Submitted CGST amount does not match (or CGST/SGST split was not used when it should have been)."))
        fields.append(_compare("gst_sgst", submitted.get("gst_sgst"), gst.get("sgst"),
                                "Submitted SGST amount does not match (or CGST/SGST split was not used when it should have been)."))

    fields.append(_compare("tds_amount", submitted.get("tds_amount"), true_result.tds_amount,
                            "TDS amount does not match the applicable section/rate for this category."))

    claimed_net = submitted.get("net_payable_claimed")
    fields.append(_compare("net_payable", claimed_net, true_result.net_disbursement_due,
                            "Net payable does not match once all fields above are correctly applied."))

    if category_reason:
        fields.append(FieldDiff(field="category", claimed=category_claimed or None, correct=category_correct or None,
                                 match=False, reason=category_reason))

    overall = all(f.match for f in fields)
    return DiffResult(overall_match=overall, fields=fields, eligible=is_eligible,
                       eligibility_reasons=eligibility_reasons,
                       unapplied_advance_advisory=true_result.unapplied_advance_advisory,
                       split_undetermined=split_undetermined, split_undetermined_note=split_note)
