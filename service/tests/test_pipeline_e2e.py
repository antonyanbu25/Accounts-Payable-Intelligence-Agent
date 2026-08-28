"""
End-to-end test: real Source C documents -> tax_lookup.py -> compute.py,
compared against the hand-verified eval_set.json. This is the test that
proves the whole reasoning chain, not just its individual pieces.
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from compute import LedgerFacts, TaxDetermination, CategoryConflict, compute, determine_gst_split_type  # noqa: E402
from tax_lookup import TaxCorpus, determine_tax  # noqa: E402

EVAL_SET_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "eval", "eval_set.json")
with open(EVAL_SET_PATH) as f:
    EVAL_SET = json.load(f)


def case(case_id):
    for c in EVAL_SET["tier1_cases"]:
        if c["id"] == case_id:
            return c
    raise KeyError(case_id)


CORPUS = None  # loaded once per test run


def get_corpus():
    global CORPUS
    if CORPUS is None:
        CORPUS = TaxCorpus()
    return CORPUS


def run_case(base_amount, category, hsn_or_sac, invoice_date_iso, vendor_state, office_state,
             needs_tds, **ledger_kwargs):
    corpus = get_corpus()
    d = datetime.date.fromisoformat(invoice_date_iso)
    tax = determine_tax(corpus, category, hsn_or_sac, d, needs_tds=needs_tds)
    assert tax["gst"].status == "found", f"GST lookup failed: {tax['gst']}"
    split = determine_gst_split_type(vendor_state, office_state)
    tds_rate = tax["tds"].rate_pct if needs_tds and tax["tds"].status == "found" else 0.0
    tds_section = tax["tds"].tds_section if needs_tds and tax["tds"].status == "found" else None
    tax_det = TaxDetermination(gst_rate_pct=tax["gst"].rate_pct, tds_rate_pct=tds_rate,
                                tds_section=tds_section, split_type=split)
    facts = LedgerFacts(base_amount=base_amount, **ledger_kwargs)
    return compute(facts, tax_det), tax


def test_e2e_effective_dating_pre_and_post():
    """The sharpest test: same vendor/category, only the DATE differs -- the
    documents must genuinely change the answer, proving the lookup reads
    the effective-date range rather than 'the current rate'."""
    pre = case("T1-effective-date-pre")["expected"]
    r_pre, _ = run_case(100000.00, "Software", "998313", "2025-09-01",
                         "Tamil Nadu", "Tamil Nadu", needs_tds=True,
                         po_amount=100000.00, receipt_amount=100000.00)
    assert r_pre.gst["gst_amount"] == 18000.0
    assert r_pre.net_disbursement_due == pre["net_disbursement_due"] == 108000.0

    post = case("T1-effective-date-post")["expected"]
    r_post, _ = run_case(100000.00, "Software", "998313", "2025-10-01",
                          "Tamil Nadu", "Tamil Nadu", needs_tds=True,
                          po_amount=100000.00, receipt_amount=100000.00)
    assert r_post.gst["gst_amount"] == 12000.0
    assert r_post.net_disbursement_due == post["net_disbursement_due"] == 102000.0


def test_e2e_hard_to_ingest_food_document():
    """Uses the deliberately messy amendment document (no clean header,
    written as a cross-reference) as the SOLE source of the rate."""
    r, tax = run_case(70000.00, "Food", "996331", "2026-01-15",
                       "Karnataka", "Karnataka", needs_tds=True,
                       po_amount=70000.00, receipt_amount=70000.00)
    assert tax["gst"].source_filename == "gst_food_post_sep2025_amendment.md"
    assert tax["gst"].rate_pct == 5.0
    assert "five per cent (5%)" in tax["gst"].key_clause
    assert tax["tds"].rate_pct == 2.0 and tax["tds"].tds_section == "194C"


def test_e2e_vendor_stated_superseded_rate_never_used():
    c = case("T1-vendor-stated-superseded-rate")["expected"]
    r, tax = run_case(110000.00, "Software", "998313", "2025-11-15",
                       "Karnataka", "Karnataka", needs_tds=True,
                       po_amount=110000.00, receipt_amount=110000.00)
    assert tax["gst"].rate_pct == 12.0  # NOT 18 -- the vendor's stated rate is never consulted here
    assert r.net_disbursement_due == c["true_net_disbursement_due"] == 112200.0


def test_e2e_igst_vs_cgst_sgst():
    igst = case("T1-igst-split-demo")["expected"]
    r, _ = run_case(130000.00, "Software", "998313", "2026-03-12",
                     "Tamil Nadu", "Karnataka", needs_tds=True,
                     po_amount=130000.00, receipt_amount=130000.00)
    assert r.gst["split_type"] == "IGST" and r.gst["igst"] == igst["gst_split"]["igst"] == 15600.0

    cgst = case("T1-cgst-sgst-split-demo")["expected"]
    r2, _ = run_case(130000.00, "Software", "998313", "2026-03-13",
                      "Karnataka", "Karnataka", needs_tds=True,
                      po_amount=130000.00, receipt_amount=130000.00)
    assert r2.gst["cgst"] == cgst["gst_split"]["cgst"] == 7800.0


def test_e2e_no_rule_found_for_nonexistent_scenario():
    """Not in the eval set as a Tier-1 case, but a real robustness check:
    asking about a category/date combination with genuinely no matching
    circular must come back 'not_found', never a guessed rate."""
    corpus = get_corpus()
    d = datetime.date(2010, 1, 1)  # before ANY circular in the corpus took effect
    result = corpus.lookup_rate("GST_RATE", "Software", "998313", d)
    assert result.status == "not_found"


def test_e2e_distractors_dont_leak_into_wrong_category():
    """Furniture and Appliances share nearly identical boilerplate text
    (deliberately) -- confirms metadata filtering, not text similarity,
    is what separates them."""
    corpus = get_corpus()
    d = datetime.date(2026, 1, 1)
    furn = corpus.lookup_rate("GST_RATE", "Furniture", "9403", d)
    appl = corpus.lookup_rate("GST_RATE", "Appliances", "8516", d)
    assert furn.source_filename == "gst_furniture.md"
    assert appl.source_filename == "gst_appliances.md"
    assert furn.source_filename != appl.source_filename


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
