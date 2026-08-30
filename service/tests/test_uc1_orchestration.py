"""
Unit tests for uc1_orchestration.py -- mocks parse_intent/tax_lookup/
do_compute/check_narration_endpoint (bound inside uc1_orchestration, per
its own "from X import Y" patching note) and db.* so these run fast, free,
and deterministic. Every branch here was ALSO verified live against the
real DB + real Anthropic + real compute layer (see the session notes) --
these tests are the fast regression net for that already-proven behavior.

IMPORT ORDER: must import `main` before `uc1_orchestration` -- see that
module's docstring.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@localhost/unused")
os.environ.setdefault("ANTHROPIC_API_KEY", "unused-in-these-tests")

import main  # noqa: E402  -- must precede uc1_orchestration, breaks the circular import otherwise
import uc1_orchestration  # noqa: E402
from models import ComputeResponse, NarrationCheckResponse, TaxLookupResponse, TaxLookupSubResult  # noqa: E402


def _tax_response(rate_pct=18.0, tds_rate_pct=10.0, split_type="CGST_SGST"):
    return TaxLookupResponse(
        split_type=split_type,
        gst=TaxLookupSubResult(status="found", rate_pct=rate_pct, source_filename="x.md", key_clause="clause"),
        tds=TaxLookupSubResult(status="found", rate_pct=tds_rate_pct, tds_section="194J"),
    )


def _passing_guard(monkeypatch, module=uc1_orchestration):
    monkeypatch.setattr(module, "check_narration_endpoint", lambda req: NarrationCheckResponse(
        passed=True, numbers_found_in_narrative=[], numbers_not_in_structured_result=[]))


def _facts(**overrides):
    base = dict(
        invoice_id=9, invoice_date="2025-09-01", base_amount=100000.0, invoice_status="open", po_id=1,
        vendor_id=1, legal_name="TechNova Software Solutions Pvt Ltd", registered_state="Karnataka",
        vendor_status="active", payment_terms="Net 30", gstin="X", onboarding_state="Karnataka",
        invoice_category="Software", invoice_hsn_sac="998313",
        po_category="Software", po_amount=100000.0, po_status="issued",
        received_amount=100000.0, receipt_status="full", office_state="Karnataka",
        advances_applied=0.0, unapplied_advances=0.0, credits_applied=0.0, payments_made=0.0,
        vendor_open_invoice_count=1,
    )
    base.update(overrides)
    return base


def test_unsupported_intent_short_circuits(monkeypatch):
    monkeypatch.setattr(uc1_orchestration, "parse_intent", lambda q, h: {"intent": "unsupported"})
    result = uc1_orchestration.handle_uc1_ask("tell me a joke", None)
    assert result == uc1_orchestration.UNSUPPORTED_RESPONSE


def test_vendor_not_found_with_category_falls_back_to_category_only(monkeypatch):
    monkeypatch.setattr(uc1_orchestration, "parse_intent", lambda q, h: {
        "intent": "tax_lookup", "vendor_name_mentioned": "Nonexistent Corp", "invoice_id_mentioned": None,
        "additional_invoice_ids_mentioned": [], "category_mentioned": "Furniture", "explicit_date_mentioned": None,
    })
    monkeypatch.setattr(uc1_orchestration.db, "resolve_vendor", lambda name: [])
    monkeypatch.setattr(uc1_orchestration, "tax_lookup", lambda req: _tax_response())
    monkeypatch.setattr(uc1_orchestration, "call_text", lambda prompt, max_tokens: "18% GST applies.")
    _passing_guard(monkeypatch)

    result = uc1_orchestration.handle_uc1_ask("what's the rate on furniture", None)
    assert "error" not in result
    assert result["note"] == "category-level answer, not tied to a specific vendor or transaction"


def test_vendor_not_found_no_category_refuses(monkeypatch):
    monkeypatch.setattr(uc1_orchestration, "parse_intent", lambda q, h: {
        "intent": "balance_lookup", "vendor_name_mentioned": "Zenith Global Traders", "invoice_id_mentioned": None,
        "additional_invoice_ids_mentioned": [], "category_mentioned": None, "explicit_date_mentioned": None,
    })
    monkeypatch.setattr(uc1_orchestration.db, "resolve_vendor", lambda name: [])

    result = uc1_orchestration.handle_uc1_ask("what does Zenith Global Traders owe", None)
    assert result["error"] == "not_found"
    assert "Zenith Global Traders" in result["message"]


def test_ambiguous_vendor_gap_under_threshold(monkeypatch):
    """The seeded 'Acme' pair: genuine tie, gap 0 -> ambiguous."""
    monkeypatch.setattr(uc1_orchestration, "parse_intent", lambda q, h: {
        "intent": "balance_lookup", "vendor_name_mentioned": "Acme", "invoice_id_mentioned": None,
        "additional_invoice_ids_mentioned": [], "category_mentioned": None, "explicit_date_mentioned": None,
    })
    monkeypatch.setattr(uc1_orchestration.db, "resolve_vendor", lambda name: [
        {"vendor_id": 1, "legal_name": "Acme Furnishings Pvt Ltd", "score": 1.0},
        {"vendor_id": 2, "legal_name": "Acme Traders Pvt Ltd", "score": 1.0},
    ])
    result = uc1_orchestration.handle_uc1_ask("what does Acme owe", None)
    assert result["error"] == "ambiguous_vendor"
    assert result["candidates"] == ["Acme Furnishings Pvt Ltd", "Acme Traders Pvt Ltd"]


def test_ambiguous_invoice_multiple_open_invoices_asks_instead_of_guessing(monkeypatch):
    """Found by an independent recruiter-style evaluation (round 3): a
    vendor with several open invoices and no invoice named in the question
    used to silently answer from only the most recent one. Must now mirror
    the ambiguous_vendor treatment above -- return a clarification, never
    compute/narrate an answer for a specific invoice the user didn't ask
    about."""
    monkeypatch.setattr(uc1_orchestration, "parse_intent", lambda q, h: {
        "intent": "balance_lookup", "vendor_name_mentioned": "TechNova Software Solutions",
        "invoice_id_mentioned": None, "additional_invoice_ids_mentioned": [], "category_mentioned": None,
        "explicit_date_mentioned": None,
    })
    monkeypatch.setattr(uc1_orchestration.db, "resolve_vendor", lambda name: [
        {"vendor_id": 1, "legal_name": "TechNova Software Solutions Pvt Ltd", "score": 1.0},
    ])
    monkeypatch.setattr(uc1_orchestration.db, "retrieve_invoice_facts_uc1",
                         lambda vid, iid: _facts(vendor_open_invoice_count=3))
    monkeypatch.setattr(uc1_orchestration.db, "list_open_invoices", lambda vid: [
        {"invoice_id": 17, "invoice_date": "2025-10-01", "base_amount": 110000.0},
        {"invoice_id": 9, "invoice_date": "2025-09-01", "base_amount": 100000.0},
        {"invoice_id": 3, "invoice_date": "2025-06-01", "base_amount": 50000.0},
    ])
    # No tax_lookup/do_compute/call_text mocked -- a clarification must
    # return before any of them are reached; a stray call would raise
    # AttributeError/TypeError here and fail the test.

    result = uc1_orchestration.handle_uc1_ask("what does TechNova owe", None)
    assert result["error"] == "ambiguous_invoice"
    assert result["candidates"] == [
        "INV-17 (2025-10-01, ₹110000)", "INV-9 (2025-09-01, ₹100000)", "INV-3 (2025-06-01, ₹50000)",
    ]
    assert "TechNova Software Solutions Pvt Ltd" in result["message"]


def test_single_open_invoice_not_ambiguous(monkeypatch):
    """A vendor with exactly one open invoice must still answer directly,
    even with no invoice named -- there's nothing to disambiguate."""
    monkeypatch.setattr(uc1_orchestration, "parse_intent", lambda q, h: {
        "intent": "balance_lookup", "vendor_name_mentioned": "TechNova Software Solutions",
        "invoice_id_mentioned": None, "additional_invoice_ids_mentioned": [], "category_mentioned": None,
        "explicit_date_mentioned": None,
    })
    monkeypatch.setattr(uc1_orchestration.db, "resolve_vendor", lambda name: [
        {"vendor_id": 1, "legal_name": "TechNova Software Solutions Pvt Ltd", "score": 1.0},
    ])
    monkeypatch.setattr(uc1_orchestration.db, "retrieve_invoice_facts_uc1",
                         lambda vid, iid: _facts(vendor_open_invoice_count=1))
    monkeypatch.setattr(uc1_orchestration, "tax_lookup", lambda req: _tax_response())
    monkeypatch.setattr(uc1_orchestration, "do_compute", lambda req: ComputeResponse(
        base_amount=100000.0, gst={"gst_amount": 18000.0, "cgst": 9000.0, "sgst": 9000.0, "igst": None, "split_type": "CGST_SGST"},
        tds_amount=10000.0, gross_liability=118000.0, net_disbursement_due=108000.0,
        pre_tax_ledger_position=100000.0, eligibility="eligible", eligibility_reasons=[], tax_treatment_refused=False))
    monkeypatch.setattr(uc1_orchestration, "call_text", lambda prompt, max_tokens: "Correct.")
    _passing_guard(monkeypatch)

    result = uc1_orchestration.handle_uc1_ask("what does TechNova owe", None)
    assert "error" not in result


def test_aggregate_lookup_intent_distinct_from_out_of_domain_refusal(monkeypatch):
    """Found by an independent recruiter-style evaluation (round 3): a
    vendor-less AP question ('what do we owe right now') used to get the
    exact same message as a genuinely off-topic one ('what's the weather'),
    both via intent='unsupported'. Must now resolve to its own distinct
    scope-limit response instead."""
    monkeypatch.setattr(uc1_orchestration, "parse_intent", lambda q, h: {"intent": "aggregate_lookup"})
    result = uc1_orchestration.handle_uc1_ask("what do we owe right now", None)
    assert result == uc1_orchestration.AGGREGATE_UNSUPPORTED_RESPONSE
    assert result != uc1_orchestration.UNSUPPORTED_RESPONSE


def test_full_query_with_weak_coincidental_second_row_not_ambiguous(monkeypatch):
    """The 'Acme Traders' false-positive this codebase already debugged
    once: a legitimate 1.0 match plus a weak 0.615 coincidental second row
    (gap 0.385 >= 0.2) must NOT be flagged ambiguous."""
    monkeypatch.setattr(uc1_orchestration, "parse_intent", lambda q, h: {
        "intent": "balance_lookup", "vendor_name_mentioned": "Acme Traders", "invoice_id_mentioned": 9,
        "additional_invoice_ids_mentioned": [], "category_mentioned": None, "explicit_date_mentioned": None,
    })
    monkeypatch.setattr(uc1_orchestration.db, "resolve_vendor", lambda name: [
        {"vendor_id": 2, "legal_name": "Acme Traders Pvt Ltd", "score": 1.0},
        {"vendor_id": 3, "legal_name": "Metro Furniture Traders Pvt Ltd", "score": 0.615},
    ])
    monkeypatch.setattr(uc1_orchestration.db, "retrieve_invoice_facts_uc1", lambda vid, iid: _facts(vendor_id=2, legal_name="Acme Traders Pvt Ltd"))
    monkeypatch.setattr(uc1_orchestration, "tax_lookup", lambda req: _tax_response())
    monkeypatch.setattr(uc1_orchestration, "do_compute", lambda req: ComputeResponse(
        base_amount=100000.0, gst={"gst_amount": 18000.0, "cgst": 9000.0, "sgst": 9000.0, "igst": None, "split_type": "CGST_SGST"},
        tds_amount=10000.0, gross_liability=118000.0, net_disbursement_due=108000.0,
        pre_tax_ledger_position=100000.0, eligibility="eligible", eligibility_reasons=[], tax_treatment_refused=False))
    monkeypatch.setattr(uc1_orchestration, "call_text", lambda prompt, max_tokens: "Correct.")
    _passing_guard(monkeypatch)

    result = uc1_orchestration.handle_uc1_ask("what does Acme Traders owe on invoice 9", None)
    assert "error" not in result


def test_gap_fix_invoice_not_belonging_to_vendor_returns_not_found(monkeypatch):
    """The gap n8n's original had no handling for: a named invoice_id that
    doesn't belong to the resolved vendor."""
    monkeypatch.setattr(uc1_orchestration, "parse_intent", lambda q, h: {
        "intent": "balance_lookup", "vendor_name_mentioned": "TechNova Software Solutions",
        "invoice_id_mentioned": 999, "additional_invoice_ids_mentioned": [], "category_mentioned": None,
        "explicit_date_mentioned": None,
    })
    monkeypatch.setattr(uc1_orchestration.db, "resolve_vendor", lambda name: [
        {"vendor_id": 1, "legal_name": "TechNova Software Solutions Pvt Ltd", "score": 1.0},
    ])
    monkeypatch.setattr(uc1_orchestration.db, "retrieve_invoice_facts_uc1", lambda vid, iid: None)

    result = uc1_orchestration.handle_uc1_ask("how much on invoice 999 for TechNova", None)
    assert result["error"] == "not_found"
    assert "999" in result["message"]


def test_gap_fix_vendor_with_zero_invoices(monkeypatch):
    monkeypatch.setattr(uc1_orchestration, "parse_intent", lambda q, h: {
        "intent": "balance_lookup", "vendor_name_mentioned": "TechNova Software Solutions",
        "invoice_id_mentioned": None, "additional_invoice_ids_mentioned": [], "category_mentioned": None,
        "explicit_date_mentioned": None,
    })
    monkeypatch.setattr(uc1_orchestration.db, "resolve_vendor", lambda name: [
        {"vendor_id": 1, "legal_name": "TechNova Software Solutions Pvt Ltd", "score": 1.0},
    ])
    monkeypatch.setattr(uc1_orchestration.db, "retrieve_invoice_facts_uc1", lambda vid, iid: None)

    result = uc1_orchestration.handle_uc1_ask("what does TechNova owe", None)
    assert result["error"] == "not_found"
    assert "no invoices on file" in result["message"]


def test_vendor_lookup_payment_terms_digit_extraction(monkeypatch):
    """Real bug fix from earlier this session: "Net 30" must not get
    flagged as an unverified number by the guard."""
    monkeypatch.setattr(uc1_orchestration, "parse_intent", lambda q, h: {
        "intent": "vendor_lookup", "vendor_name_mentioned": "TechNova Software Solutions",
        "invoice_id_mentioned": None, "additional_invoice_ids_mentioned": [], "category_mentioned": None,
        "explicit_date_mentioned": None,
    })
    monkeypatch.setattr(uc1_orchestration.db, "resolve_vendor", lambda name: [
        {"vendor_id": 1, "legal_name": "TechNova Software Solutions Pvt Ltd", "score": 1.0},
    ])
    monkeypatch.setattr(uc1_orchestration.db, "retrieve_vendor_details", lambda vid: {
        "legal_name": "TechNova Software Solutions Pvt Ltd", "vendor_status": "active", "payment_terms": "Net 30",
        "gstin": "X", "registered_state": "Karnataka", "pan": "Y", "onboarding_state": "Karnataka",
        "onboarding_status": "approved", "onboarding_date": "2024-01-02",
        "invoices": [{"invoice_id": 9, "category": "Software", "base_amount": 200000.0,
                      "invoice_date": "2025-11-01", "status": "open"}],
    })
    monkeypatch.setattr(uc1_orchestration, "call_text",
                         lambda prompt, max_tokens: "TechNova has Net 30 payment terms and 1 invoice.")
    captured = {}

    def fake_guard(req):
        captured["values"] = req.structured_values
        return NarrationCheckResponse(passed=True, numbers_found_in_narrative=[], numbers_not_in_structured_result=[])

    monkeypatch.setattr(uc1_orchestration, "check_narration_endpoint", fake_guard)

    result = uc1_orchestration.handle_uc1_ask("what state is TechNova in", None)
    assert 30 in captured["values"]  # the "Net 30" digit
    assert result["guard"] == "passed"


def test_comparison_complete(monkeypatch):
    monkeypatch.setattr(uc1_orchestration.db, "retrieve_invoice_facts_comparison", lambda vid, ids: [
        _facts(invoice_id=13, invoice_date="2025-09-01"),
        _facts(invoice_id=14, invoice_date="2025-10-01"),
    ])
    monkeypatch.setattr(uc1_orchestration, "tax_lookup", lambda req: _tax_response(rate_pct=18.0 if req.invoice_date == "2025-09-01" else 12.0))
    monkeypatch.setattr(uc1_orchestration, "do_compute", lambda req: ComputeResponse(
        base_amount=100000.0, gst={"gst_amount": 18000.0 if req.gst_rate_pct == 18.0 else 12000.0,
                                    "cgst": None, "sgst": None, "igst": None, "split_type": "CGST_SGST"},
        tds_amount=10000.0, gross_liability=118000.0 if req.gst_rate_pct == 18.0 else 112000.0,
        net_disbursement_due=108000.0 if req.gst_rate_pct == 18.0 else 102000.0,
        pre_tax_ledger_position=100000.0, eligibility="eligible", eligibility_reasons=[], tax_treatment_refused=False))
    monkeypatch.setattr(uc1_orchestration, "call_text", lambda prompt, max_tokens: "Rates differ due to the reform.")
    _passing_guard(monkeypatch)

    result = uc1_orchestration._handle_comparison({"vendor_id": 1, "legal_name": "Skyline Software Labs"}, 13, [14])
    assert result["comparison"] is True
    assert len(result["invoices"]) == 2
    # Positional pairing follows whatever order db.retrieve_invoice_facts_comparison
    # returns (real DB orders by invoice_date DESC; this mock returns [13, 14]).
    assert result["invoices"][0]["invoice_id"] == 13


def test_comparison_incomplete_names_missing_id(monkeypatch):
    monkeypatch.setattr(uc1_orchestration.db, "retrieve_invoice_facts_comparison", lambda vid, ids: [
        _facts(invoice_id=13),
    ])
    result = uc1_orchestration._handle_comparison({"vendor_id": 1, "legal_name": "Skyline Software Labs"}, 13, [9999])
    assert result["error"] == "comparison_incomplete"
    assert "9999" in result["message"]
    assert "Found: invoice 13" in result["message"]  # 13 correctly reported as found, not missing


def test_comparison_additional_ids_without_primary(monkeypatch):
    """additional_invoice_ids_mentioned populated with no invoice_id_mentioned
    (invoice_id_mentioned is None) -- must not crash, and must not filter
    out the additional id."""
    monkeypatch.setattr(uc1_orchestration.db, "retrieve_invoice_facts_comparison", lambda vid, ids: [])
    result = uc1_orchestration._handle_comparison({"vendor_id": 1, "legal_name": "V"}, None, [13, 14])
    assert result["error"] == "comparison_incomplete"
    assert "13" in result["message"] and "14" in result["message"]


def test_single_invoice_guard_fallback(monkeypatch):
    facts = _facts()
    monkeypatch.setattr(uc1_orchestration.db, "retrieve_invoice_facts_uc1", lambda vid, iid: facts)
    monkeypatch.setattr(uc1_orchestration, "tax_lookup", lambda req: _tax_response())
    monkeypatch.setattr(uc1_orchestration, "do_compute", lambda req: ComputeResponse(
        base_amount=100000.0, gst={"gst_amount": 18000.0, "cgst": 9000.0, "sgst": 9000.0, "igst": None, "split_type": "CGST_SGST"},
        tds_amount=10000.0, gross_liability=118000.0, net_disbursement_due=108000.0,
        pre_tax_ledger_position=100000.0, eligibility="eligible", eligibility_reasons=[], tax_treatment_refused=False))
    monkeypatch.setattr(uc1_orchestration, "call_text", lambda prompt, max_tokens: "hallucinated")
    monkeypatch.setattr(uc1_orchestration, "check_narration_endpoint", lambda req: NarrationCheckResponse(
        passed=False, numbers_found_in_narrative=[], numbers_not_in_structured_result=[]))

    result = uc1_orchestration._handle_single_invoice(
        {"invoice_id_mentioned": 9}, {"vendor_id": 1, "legal_name": "TechNova"})
    assert result["guard"] == "failed_fallback_used"
    assert "108000" in result["narrative"]


def test_unapplied_advance_instructed_and_allowed_by_guard(monkeypatch):
    """Regression test for a real gap found by an independent recruiter-
    style eval: UC1's narration never had a MUST-state instruction for
    unapplied_advance_advisory (unlike UC2, and unlike UC1's own settled-
    amount note), so the LLM was handed the number with no instruction to
    mention it, and even a compliant mention would have been rejected by
    the guard (the number wasn't in structured_values either)."""
    facts = _facts()
    monkeypatch.setattr(uc1_orchestration.db, "retrieve_invoice_facts_uc1", lambda vid, iid: facts)
    monkeypatch.setattr(uc1_orchestration, "tax_lookup", lambda req: _tax_response())
    monkeypatch.setattr(uc1_orchestration, "do_compute", lambda req: ComputeResponse(
        base_amount=85000.0, gst={"gst_amount": 15300.0, "cgst": None, "sgst": None, "igst": 15300.0, "split_type": "IGST"},
        tds_amount=0.0, gross_liability=100300.0, net_disbursement_due=100300.0,
        pre_tax_ledger_position=85000.0, eligibility="eligible", eligibility_reasons=[], tax_treatment_refused=False,
        unapplied_advance_advisory=15000.0))
    captured_prompt = {}

    def fake_call_text(prompt, max_tokens):
        captured_prompt["text"] = prompt
        return f"Net payable is 100300, with an unapplied advance of 15000 against this vendor."

    monkeypatch.setattr(uc1_orchestration, "call_text", fake_call_text)
    captured_guard_req = {}

    def fake_guard(req):
        captured_guard_req["values"] = req.structured_values
        return NarrationCheckResponse(passed=True, numbers_found_in_narrative=[], numbers_not_in_structured_result=[])

    monkeypatch.setattr(uc1_orchestration, "check_narration_endpoint", fake_guard)

    result = uc1_orchestration._handle_single_invoice(
        {"invoice_id_mentioned": 9}, {"vendor_id": 1, "legal_name": "Bright Office Furnishings"})
    assert "unapplied advance" in captured_prompt["text"].lower()
    assert "MUST state this plainly" in captured_prompt["text"]
    assert 15000.0 in captured_guard_req["values"]
    assert result["guard"] == "passed"


def test_unapplied_advance_fallback_states_it_too(monkeypatch):
    """The advisory must survive into the templated fallback text as well,
    not only the successful-narration path -- matching UC2's existing
    fallback pattern."""
    facts = _facts()
    monkeypatch.setattr(uc1_orchestration.db, "retrieve_invoice_facts_uc1", lambda vid, iid: facts)
    monkeypatch.setattr(uc1_orchestration, "tax_lookup", lambda req: _tax_response())
    monkeypatch.setattr(uc1_orchestration, "do_compute", lambda req: ComputeResponse(
        base_amount=85000.0, gst={"gst_amount": 15300.0, "cgst": None, "sgst": None, "igst": 15300.0, "split_type": "IGST"},
        tds_amount=0.0, gross_liability=100300.0, net_disbursement_due=100300.0,
        pre_tax_ledger_position=85000.0, eligibility="eligible", eligibility_reasons=[], tax_treatment_refused=False,
        unapplied_advance_advisory=15000.0))
    monkeypatch.setattr(uc1_orchestration, "call_text", lambda prompt, max_tokens: "hallucinated nonsense")
    monkeypatch.setattr(uc1_orchestration, "check_narration_endpoint", lambda req: NarrationCheckResponse(
        passed=False, numbers_found_in_narrative=[], numbers_not_in_structured_result=[]))

    result = uc1_orchestration._handle_single_invoice(
        {"invoice_id_mentioned": 9}, {"vendor_id": 1, "legal_name": "Bright Office Furnishings"})
    assert result["guard"] == "failed_fallback_used"
    assert "15000" in result["narrative"]
    assert "overpayment risk" in result["narrative"]


def test_stale_vendor_rate_instructed_and_allowed_by_guard(monkeypatch):
    """Regression test for a real gap found while writing the demo script:
    retrieve_invoice_facts_uc1() has fetched gst_rate_stated/
    gst_amount_stated all along, but nothing in this branch ever compared
    it against the true rate or instructed the narration to mention it --
    the documented "vendor's own invoice states a superseded rate" scenario
    (README's Assumption & Decision Log, eval case
    T1-vendor-stated-superseded-rate) was silently never actually surfaced
    in live narration. The eval only checks computed figures, never
    narrative text, so nothing caught this until a live check."""
    facts = _facts(gst_rate_stated=18.0, gst_amount_stated=19800.0)
    monkeypatch.setattr(uc1_orchestration.db, "retrieve_invoice_facts_uc1", lambda vid, iid: facts)
    monkeypatch.setattr(uc1_orchestration, "tax_lookup", lambda req: _tax_response(rate_pct=12.0))
    monkeypatch.setattr(uc1_orchestration, "do_compute", lambda req: ComputeResponse(
        base_amount=110000.0, gst={"gst_amount": 13200.0, "cgst": 6600.0, "sgst": 6600.0, "igst": None, "split_type": "CGST_SGST"},
        tds_amount=11000.0, gross_liability=123200.0, net_disbursement_due=112200.0,
        pre_tax_ledger_position=110000.0, eligibility="eligible", eligibility_reasons=[], tax_treatment_refused=False))
    captured_prompt = {}

    def fake_call_text(prompt, max_tokens):
        captured_prompt["text"] = prompt
        return "GST applied at the correct 12% rate, though the vendor's invoice states 18%."

    monkeypatch.setattr(uc1_orchestration, "call_text", fake_call_text)
    captured_guard_req = {}

    def fake_guard(req):
        captured_guard_req["values"] = req.structured_values
        return NarrationCheckResponse(passed=True, numbers_found_in_narrative=[], numbers_not_in_structured_result=[])

    monkeypatch.setattr(uc1_orchestration, "check_narration_endpoint", fake_guard)

    result = uc1_orchestration._handle_single_invoice(
        {"invoice_id_mentioned": 17}, {"vendor_id": 1, "legal_name": "TechNova Software Solutions Pvt Ltd"})
    assert "18" in captured_prompt["text"] and "12" in captured_prompt["text"]
    assert "MUST state this discrepancy plainly" in captured_prompt["text"]
    assert 18.0 in captured_guard_req["values"] and 12.0 in captured_guard_req["values"]
    assert result["guard"] == "passed"


def test_stale_vendor_rate_not_flagged_when_rates_match(monkeypatch):
    """No false positive: when the vendor's stated rate happens to match
    the true current rate, no discrepancy instruction should be added."""
    facts = _facts(gst_rate_stated=12.0, gst_amount_stated=13200.0)
    monkeypatch.setattr(uc1_orchestration.db, "retrieve_invoice_facts_uc1", lambda vid, iid: facts)
    monkeypatch.setattr(uc1_orchestration, "tax_lookup", lambda req: _tax_response(rate_pct=12.0))
    monkeypatch.setattr(uc1_orchestration, "do_compute", lambda req: ComputeResponse(
        base_amount=110000.0, gst={"gst_amount": 13200.0, "cgst": 6600.0, "sgst": 6600.0, "igst": None, "split_type": "CGST_SGST"},
        tds_amount=11000.0, gross_liability=123200.0, net_disbursement_due=112200.0,
        pre_tax_ledger_position=110000.0, eligibility="eligible", eligibility_reasons=[], tax_treatment_refused=False))
    captured_prompt = {}

    def fake_call_text(prompt, max_tokens):
        captured_prompt["text"] = prompt
        return "GST applied at 12%."

    monkeypatch.setattr(uc1_orchestration, "call_text", fake_call_text)
    _passing_guard(monkeypatch)

    uc1_orchestration._handle_single_invoice(
        {"invoice_id_mentioned": 17}, {"vendor_id": 1, "legal_name": "TechNova Software Solutions Pvt Ltd"})
    assert "MUST state this discrepancy" not in captured_prompt["text"]


def test_blocked_vendor_lookup_gets_narration_nudge(monkeypatch):
    """Regression test for the second finding from the same eval: a
    vendor-lookup question (no invoice named) about a blocked vendor got a
    correct-but-implicit answer. This branch structurally can't compute
    invoice-level eligibility (no PO/receipt join), but "vendor blocked"
    alone should be stated plainly as blocking any payment."""
    monkeypatch.setattr(uc1_orchestration.db, "retrieve_vendor_details", lambda vid: {
        "legal_name": "Coastal Facility Services", "vendor_status": "blocked", "payment_terms": "Net 45",
        "gstin": "X", "registered_state": "Karnataka", "pan": "Y", "onboarding_state": "Karnataka",
        "onboarding_status": "approved", "onboarding_date": "2024-01-02", "invoices": [],
    })
    captured_prompt = {}

    def fake_call_text(prompt, max_tokens):
        captured_prompt["text"] = prompt
        return "Coastal Facility Services is blocked; no payment can proceed."

    monkeypatch.setattr(uc1_orchestration, "call_text", fake_call_text)
    _passing_guard(monkeypatch)

    result = uc1_orchestration._handle_vendor_details(1)
    assert "BLOCKED" in captured_prompt["text"]
    assert "MUST state plainly" in captured_prompt["text"]
    assert result["guard"] == "passed"


def test_active_vendor_lookup_gets_no_blocked_nudge(monkeypatch):
    """The blocked-vendor instruction must not appear for an active vendor."""
    monkeypatch.setattr(uc1_orchestration.db, "retrieve_vendor_details", lambda vid: {
        "legal_name": "TechNova Software Solutions Pvt Ltd", "vendor_status": "active", "payment_terms": "Net 30",
        "gstin": "X", "registered_state": "Karnataka", "pan": "Y", "onboarding_state": "Karnataka",
        "onboarding_status": "approved", "onboarding_date": "2024-01-02", "invoices": [],
    })
    captured_prompt = {}

    def fake_call_text(prompt, max_tokens):
        captured_prompt["text"] = prompt
        return "TechNova is an active vendor."

    monkeypatch.setattr(uc1_orchestration, "call_text", fake_call_text)
    _passing_guard(monkeypatch)

    uc1_orchestration._handle_vendor_details(1)
    assert "BLOCKED" not in captured_prompt["text"]


def test_blocked_vendor_fallback_warns_too(monkeypatch):
    monkeypatch.setattr(uc1_orchestration.db, "retrieve_vendor_details", lambda vid: {
        "legal_name": "Coastal Facility Services", "vendor_status": "blocked", "payment_terms": "Net 45",
        "gstin": "X", "registered_state": "Karnataka", "pan": "Y", "onboarding_state": "Karnataka",
        "onboarding_status": "approved", "onboarding_date": "2024-01-02", "invoices": [],
    })
    monkeypatch.setattr(uc1_orchestration, "call_text", lambda prompt, max_tokens: "hallucinated")
    monkeypatch.setattr(uc1_orchestration, "check_narration_endpoint", lambda req: NarrationCheckResponse(
        passed=False, numbers_found_in_narrative=[], numbers_not_in_structured_result=[]))

    result = uc1_orchestration._handle_vendor_details(1)
    assert result["guard"] == "failed_fallback_used"
    assert "BLOCKED" in result["narrative"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
