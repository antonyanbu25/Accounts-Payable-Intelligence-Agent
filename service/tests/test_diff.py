import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from compute import LedgerFacts, TaxDetermination, CategoryConflict, compute  # noqa: E402
from diff import diff_advice  # noqa: E402

EVAL_SET_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "eval", "eval_set.json")
with open(EVAL_SET_PATH) as f:
    EVAL_SET = json.load(f)


def adv_case(case_id):
    for c in EVAL_SET["adversarial_cases"]:
        if c["id"] == case_id:
            return c
    raise KeyError(case_id)


def test_false_positive_advice_shows_no_mismatch():
    """A3: an entirely-correct advice must produce overall_match=True.
    Vendor is Maharashtra, office is Karnataka -> inter-state -> IGST."""
    c = adv_case("A3-false-positive-check")
    facts = LedgerFacts(base_amount=85000.00, po_amount=85000.00, receipt_amount=85000.00)
    tax = TaxDetermination(gst_rate_pct=18.0, tds_rate_pct=0.0, tds_section=None, split_type="IGST")
    true_result = compute(facts, tax)
    advice = c["submitted_advice"]
    diff = diff_advice(true_result, advice)
    assert diff.overall_match is True, [f for f in diff.fields if not f.match]
    assert all(f.match for f in diff.fields)


def test_multi_error_advice_catches_both_independently():
    """A4: two independent errors (GST rate wrong, TDS wrongly applied) must BOTH surface."""
    c = adv_case("A4-multi-error-advice")
    facts = LedgerFacts(base_amount=61000.00, po_amount=61000.00, receipt_amount=61000.00)
    tax = TaxDetermination(gst_rate_pct=18.0, tds_rate_pct=0.0, tds_section=None, split_type="CGST_SGST")
    true_result = compute(facts, tax)
    advice = c["submitted_advice"]
    diff = diff_advice(true_result, advice)
    assert diff.overall_match is False
    mismatched_fields = {f.field for f in diff.fields if not f.match}
    assert "gst_rate_pct" in mismatched_fields
    assert "tds_amount" in mismatched_fields
    assert "net_payable" in mismatched_fields
    # every mismatch carries a distinct, non-empty reason
    for f in diff.fields:
        if not f.match:
            assert f.reason


def test_category_conflict_blocks_validation_entirely():
    """When the true result itself can't resolve tax treatment, the diff must say so
    rather than compare against a partial/undefined 'correct' figure."""
    facts = LedgerFacts(base_amount=90000.00, po_amount=90000.00, receipt_amount=90000.00)
    conflict = CategoryConflict(po_category="Software", invoice_category="Services")
    true_result = compute(facts, tax=None, category_conflict=conflict)
    advice = {"base_amount": 90000.00, "gst_rate_pct": 18.0, "gst_cgst": 8100.0, "gst_sgst": 8100.0,
              "tds_amount": 9000.0, "net_payable_claimed": 97200.0}
    diff = diff_advice(true_result, advice)
    assert diff.blocked is True
    assert diff.overall_match is False


def test_category_mismatch_row_carries_real_string_values_not_none():
    """Found by an independent recruiter-style evaluation (round 3): the
    category row's claimed/correct used to be hardcoded to None (typed
    float-only), which the frontend's pandas.DataFrame silently coerced to
    NaN, rendering as the literal text "nan" in that table cell -- even
    though the narrative and reason both stated the real category names
    correctly. claimed/correct must now carry the actual submitted-vs-
    invoice category strings."""
    facts = LedgerFacts(base_amount=50000.00, po_amount=50000.00, receipt_amount=50000.00)
    tax = TaxDetermination(gst_rate_pct=18.0, tds_rate_pct=0.0, tds_section=None, split_type="CGST_SGST")
    true_result = compute(facts, tax)
    advice = {"base_amount": 50000.00, "gst_rate_pct": 18.0, "gst_cgst": 4500.0, "gst_sgst": 4500.0,
              "tds_amount": 0.0, "net_payable_claimed": 59000.0}
    diff = diff_advice(true_result, advice,
                        category_reason="Submitted category 'Furniture' does not match the invoice on file ('Appliances').",
                        category_claimed="Furniture", category_correct="Appliances")
    category_field = next(f for f in diff.fields if f.field == "category")
    assert category_field.claimed == "Furniture"
    assert category_field.correct == "Appliances"
    assert category_field.match is False


def test_blocked_vendor_flagged_even_when_numbers_match():
    """Found by recruiter-mindset live testing: a numerically-correct advice
    for a blocked vendor must still surface ineligibility -- an accountant
    seeing 'numbers correct' with no other signal could release a payment
    that should never go out."""
    facts = LedgerFacts(base_amount=30000.00, vendor_status="blocked",
                         po_amount=30000.00, receipt_amount=30000.00)
    tax = TaxDetermination(gst_rate_pct=18.0, tds_rate_pct=10.0, tds_section="194J", split_type="CGST_SGST")
    true_result = compute(facts, tax)
    advice = {"base_amount": 30000.00, "gst_rate_pct": 18.0, "gst_cgst": 2700.00, "gst_sgst": 2700.00,
              "tds_amount": 3000.00, "net_payable_claimed": true_result.net_disbursement_due}
    diff = diff_advice(true_result, advice)
    assert diff.overall_match is True  # the NUMBERS are genuinely correct
    assert diff.eligible is False       # but the payment is not clear to release
    assert any("blocked" in r for r in diff.eligibility_reasons)


def test_non_po_invoice_split_undetermined_skips_split_fields_not_verified_as_verified():
    """Regression test: a non-PO invoice (INV-20 in the seed data) has no
    linked office, so the true GST split is genuinely unknown. The diff must
    NOT compare gst_cgst/gst_sgst/gst_igst against a fabricated guess (that
    would either wrongly fail a correct advice or wrongly pass a wrong one)
    -- it must flag split_undetermined instead, while still fully verifying
    base_amount/gst_rate_pct/tds_amount/net_payable."""
    from compute import determine_gst_split_type
    split = determine_gst_split_type("Karnataka", None)
    facts = LedgerFacts(base_amount=8000.00, po_amount=None, receipt_amount=None)
    tax = TaxDetermination(gst_rate_pct=5.0, tds_rate_pct=2.0, tds_section="194C", split_type=split)
    true_result = compute(facts, tax)
    # Accountant submits a CGST/SGST split -- their own guess, since they
    # also have no office to verify against.
    advice = {"base_amount": 8000.00, "gst_rate_pct": 5.0, "gst_cgst": 200.0, "gst_sgst": 200.0,
              "tds_amount": true_result.tds_amount, "net_payable_claimed": true_result.net_disbursement_due}
    diff = diff_advice(true_result, advice)
    assert diff.split_undetermined is True
    assert diff.split_undetermined_note  # non-empty, explains why
    field_names = {f.field for f in diff.fields}
    assert "gst_cgst" not in field_names and "gst_sgst" not in field_names and "gst_igst" not in field_names
    # Every field that COULD be verified was, and the advice above is
    # correct on all of them -- overall_match must be True, not dragged
    # down by the split it deliberately doesn't grade.
    assert diff.overall_match is True


def test_tolerance_absorbs_one_rupee_rounding_noise():
    facts = LedgerFacts(base_amount=100000.00, po_amount=100000.00, receipt_amount=100000.00)
    tax = TaxDetermination(gst_rate_pct=18.0, tds_rate_pct=10.0, tds_section="194J", split_type="CGST_SGST")
    true_result = compute(facts, tax)
    advice = {"base_amount": 100000.00, "gst_rate_pct": 18.0, "gst_cgst": 9000.0, "gst_sgst": 9000.0,
              "tds_amount": 10000.0, "net_payable_claimed": true_result.net_disbursement_due + 1.0}  # off by exactly ₹1
    diff = diff_advice(true_result, advice)
    net_field = [f for f in diff.fields if f.field == "net_payable"][0]
    assert net_field.match is True


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
