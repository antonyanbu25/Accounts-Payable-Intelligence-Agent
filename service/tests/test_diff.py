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
    """A3: an entirely-correct advice must produce overall_match=True."""
    c = adv_case("A3-false-positive-check")
    facts = LedgerFacts(base_amount=85000.00, po_amount=85000.00, receipt_amount=85000.00)
    tax = TaxDetermination(gst_rate_pct=18.0, tds_rate_pct=0.0, tds_section=None, split_type="CGST_SGST")
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
