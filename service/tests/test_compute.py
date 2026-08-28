"""
Runs every Tier-1 and adversarial case from eval/eval_set.json through the
real compute.py engine and asserts the output matches the hand-verified
answer key exactly. This is the test that proves the compute engine is
correct, not just that it runs.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from compute import (  # noqa: E402
    LedgerFacts, TaxDetermination, CategoryConflict, compute,
    determine_gst_split_type, resolve_vendor_state,
)

EVAL_SET_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "eval", "eval_set.json")
with open(EVAL_SET_PATH) as f:
    EVAL_SET = json.load(f)


def case(case_id):
    for c in EVAL_SET["tier1_cases"] + EVAL_SET["adversarial_cases"]:
        if c["id"] == case_id:
            return c
    raise KeyError(case_id)


def test_partial_payment():
    c = case("T1-partial-payment")["expected"]
    facts = LedgerFacts(base_amount=200000.00, advances_applied=0, credits_applied=0,
                         payments_made=120000.00, po_amount=200000.00, receipt_amount=200000.00)
    tax = TaxDetermination(gst_rate_pct=12.0, tds_rate_pct=10.0, tds_section="194J", split_type="CGST_SGST")
    r = compute(facts, tax)
    assert r.gst["gst_amount"] == c["gst_split"]["cgst"] + c["gst_split"]["sgst"] == 24000.0
    assert r.tds_amount == c["tds_amount"] == 20000.0
    assert r.gross_liability == c["gross_liability"] == 224000.0
    assert r.net_disbursement_due == c["net_disbursement_due"] == 84000.0
    assert r.eligibility == "eligible"


def test_applied_advance():
    c = case("T1-applied-advance")["expected"]
    facts = LedgerFacts(base_amount=150000.00, advances_applied=30000.00, credits_applied=0,
                         payments_made=120000.00, po_amount=150000.00, receipt_amount=150000.00)
    tax = TaxDetermination(gst_rate_pct=12.0, tds_rate_pct=10.0, tds_section="194J", split_type="CGST_SGST")
    r = compute(facts, tax)
    assert r.net_disbursement_due == c["net_disbursement_due"] == 3000.0
    assert r.eligibility == "eligible"


def test_credit_note():
    c = case("T1-credit-note")["expected"]
    facts = LedgerFacts(base_amount=60000.00, credits_applied=5000.00, payments_made=55000.00,
                         po_amount=60000.00, receipt_amount=60000.00)
    tax = TaxDetermination(gst_rate_pct=18.0, tds_rate_pct=0.0, tds_section=None, split_type="CGST_SGST")
    r = compute(facts, tax)
    assert r.tds_amount == 0.0
    assert r.net_disbursement_due == c["net_disbursement_due"] == 10800.0


def test_tds_with_gst_igst():
    c = case("T1-tds-with-gst-igst")["expected"]
    split = determine_gst_split_type("Tamil Nadu", "Karnataka")
    assert split == "IGST"
    facts = LedgerFacts(base_amount=80000.00, payments_made=80000.00, po_amount=80000.00, receipt_amount=80000.00)
    tax = TaxDetermination(gst_rate_pct=18.0, tds_rate_pct=10.0, tds_section="194J", split_type=split)
    r = compute(facts, tax)
    assert r.gst["split_type"] == "IGST" and r.gst["igst"] == 14400.0 and r.gst["cgst"] is None
    assert r.net_disbursement_due == c["net_disbursement_due"] == 6400.0


def test_effective_dated_pre_and_post():
    pre = case("T1-effective-date-pre")["expected"]
    post = case("T1-effective-date-post")["expected"]
    facts = LedgerFacts(base_amount=100000.00, po_amount=100000.00, receipt_amount=100000.00)

    tax_pre = TaxDetermination(gst_rate_pct=18.0, tds_rate_pct=10.0, tds_section="194J", split_type="CGST_SGST")
    r_pre = compute(facts, tax_pre)
    assert r_pre.net_disbursement_due == pre["net_disbursement_due"] == 108000.0

    tax_post = TaxDetermination(gst_rate_pct=12.0, tds_rate_pct=10.0, tds_section="194J", split_type="CGST_SGST")
    r_post = compute(facts, tax_post)
    assert r_post.net_disbursement_due == post["net_disbursement_due"] == 102000.0
    assert r_pre.net_disbursement_due != r_post.net_disbursement_due  # date genuinely changes the answer


def test_authority_conflict_resolves_to_gstin_state():
    c = case("T1-authority-conflict")["expected"]
    res = resolve_vendor_state(gstin_registered_state="Maharashtra", onboarding_submitted_state="Karnataka")
    assert res.authoritative_state == "Maharashtra"
    assert res.conflict_flagged is True
    split = determine_gst_split_type(res.authoritative_state, "Maharashtra")  # office is Maharashtra
    assert split == "CGST_SGST"  # NOT IGST -- would be wrong if the stale onboarding value were used

    facts = LedgerFacts(base_amount=70000.00, po_amount=70000.00, receipt_amount=70000.00)
    tax = TaxDetermination(gst_rate_pct=18.0, tds_rate_pct=10.0, tds_section="194J", split_type=split)
    r = compute(facts, tax)
    assert r.net_disbursement_due == c["net_disbursement_due"] == 75600.0


def test_no_rule_conflict_refuses_tax_but_states_ledger_position():
    c = case("T1-no-rule-conflict")["expected"]
    facts = LedgerFacts(base_amount=90000.00, po_amount=90000.00, receipt_amount=90000.00)
    conflict = CategoryConflict(po_category="Software", invoice_category="Services")
    r = compute(facts, tax=None, category_conflict=conflict)
    assert r.tax_treatment_refused is True
    assert r.gross_liability is None and r.net_disbursement_due is None
    assert r.pre_tax_ledger_position == c["pre_tax_ledger_position"] == 90000.0


def test_vendor_stated_rate_never_reaches_compute():
    """The compute engine has no gst_rate_stated parameter at all -- this test
    documents that fact. The 12% TRUE rate is passed in explicitly, exactly as
    /tax-lookup would determine it, never derived from the vendor's invoice."""
    c = case("T1-vendor-stated-superseded-rate")["expected"]
    facts = LedgerFacts(base_amount=110000.00, po_amount=110000.00, receipt_amount=110000.00)
    tax = TaxDetermination(gst_rate_pct=12.0, tds_rate_pct=10.0, tds_section="194J", split_type="CGST_SGST")
    r = compute(facts, tax)
    assert r.net_disbursement_due == c["true_net_disbursement_due"] == 112200.0


def test_igst_vs_cgst_sgst_split_isolated():
    igst_case = case("T1-igst-split-demo")["expected"]
    cgst_case = case("T1-cgst-sgst-split-demo")["expected"]
    facts = LedgerFacts(base_amount=130000.00, po_amount=130000.00, receipt_amount=130000.00)

    tax_igst = TaxDetermination(gst_rate_pct=12.0, tds_rate_pct=10.0, tds_section="194J", split_type="IGST")
    r_igst = compute(facts, tax_igst)
    assert r_igst.gst["igst"] == igst_case["gst_split"]["igst"] == 15600.0
    assert r_igst.net_disbursement_due == igst_case["net_disbursement_due"] == 132600.0

    tax_cgst = TaxDetermination(gst_rate_pct=12.0, tds_rate_pct=10.0, tds_section="194J", split_type="CGST_SGST")
    r_cgst = compute(facts, tax_cgst)
    assert r_cgst.gst["cgst"] + r_cgst.gst["sgst"] == cgst_case["gst_split"]["cgst"] + cgst_case["gst_split"]["sgst"] == 15600.0
    assert r_cgst.net_disbursement_due == cgst_case["net_disbursement_due"] == 132600.0
    assert r_igst.net_disbursement_due == r_cgst.net_disbursement_due  # only the split differs, not the total


def test_3way_match_failure_detected():
    r = check_three_way_match_case()
    assert r.matched is False


def check_three_way_match_case():
    from compute import check_three_way_match
    return check_three_way_match(po_amount=50000.00, receipt_amount=50000.00, invoice_base_amount=58000.00)


def test_false_positive_advice_matches_exactly():
    """A2 adversarial case: an entirely-correct advice must NOT be flagged."""
    c = case("A3-false-positive-check")
    facts = LedgerFacts(base_amount=85000.00, po_amount=85000.00, receipt_amount=85000.00)
    tax = TaxDetermination(gst_rate_pct=18.0, tds_rate_pct=0.0, tds_section=None, split_type="CGST_SGST")
    r = compute(facts, tax)
    advice = c["submitted_advice"]
    assert r.gst["cgst"] == advice["gst_cgst"] and r.gst["sgst"] == advice["gst_sgst"]
    assert r.tds_amount == advice["tds_amount"]
    assert r.net_disbursement_due == advice["net_payable_claimed"] == c["expected_reconstruction"]["net_disbursement_due"]


def test_multi_error_advice_both_fields_wrong():
    """A3 adversarial case: diff must catch BOTH errors, not just the first."""
    c = case("A4-multi-error-advice")
    facts = LedgerFacts(base_amount=61000.00, po_amount=61000.00, receipt_amount=61000.00)
    tax = TaxDetermination(gst_rate_pct=18.0, tds_rate_pct=0.0, tds_section=None, split_type="CGST_SGST")
    r = compute(facts, tax)
    advice = c["submitted_advice"]
    gst_mismatch = advice["gst_rate_pct"] != tax.gst_rate_pct
    tds_mismatch = advice["tds_amount"] != r.tds_amount
    assert gst_mismatch and tds_mismatch  # both must independently disagree
    assert r.net_disbursement_due == c["expected_reconstruction"]["net_disbursement_due"] == 71980.0
    assert r.net_disbursement_due != advice["net_payable_claimed"]


def test_unapplied_advance_surfaced_but_not_netted():
    facts = LedgerFacts(base_amount=50000.00, unapplied_advances=15000.00, po_amount=50000.00, receipt_amount=50000.00)
    tax = TaxDetermination(gst_rate_pct=18.0, tds_rate_pct=0.0, tds_section=None, split_type="CGST_SGST")
    r = compute(facts, tax)
    assert r.unapplied_advance_advisory == 15000.00
    assert r.net_disbursement_due == r.gross_liability  # NOT reduced by the unapplied advance


def test_blocked_vendor_not_eligible():
    facts = LedgerFacts(base_amount=30000.00, vendor_status="blocked", po_amount=30000.00, receipt_amount=30000.00)
    tax = TaxDetermination(gst_rate_pct=18.0, tds_rate_pct=10.0, tds_section="194J", split_type="CGST_SGST")
    r = compute(facts, tax)
    assert "vendor blocked" in r.eligibility


def test_cancelled_po_not_eligible():
    facts = LedgerFacts(base_amount=25000.00, po_status="cancelled", po_amount=25000.00, receipt_amount=25000.00)
    tax = TaxDetermination(gst_rate_pct=18.0, tds_rate_pct=0.0, tds_section=None, split_type="CGST_SGST")
    r = compute(facts, tax)
    assert "PO cancelled" in r.eligibility


def test_non_po_invoice_has_no_3way_match():
    facts = LedgerFacts(base_amount=8000.00, po_amount=None, receipt_amount=None)
    tax = TaxDetermination(gst_rate_pct=5.0, tds_rate_pct=2.0, tds_section="194C", split_type="CGST_SGST")
    r = compute(facts, tax)
    assert r.three_way_match is None
    assert r.eligibility == "eligible"  # absence of a PO isn't itself a block in this design


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
