"""
Unit tests for uc2_orchestration.py -- mocks tax_lookup/do_compute/do_diff/
check_narration_endpoint (bound inside uc2_orchestration, per its own
"from X import Y" patching note) and db.*/call_text so these run fast,
free, and deterministic, with no real DB/Anthropic calls. Every branch here
was ALSO verified live against the real DB + real Anthropic + real
compute layer (see the session notes) -- these tests are the fast
regression net for that already-proven behavior, not the first line of
verification.

IMPORT ORDER: must import `main` before `uc2_orchestration`, or
uc2_orchestration's own `from main import ...` hits a circular-import error
-- see uc2_orchestration.py's module docstring.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@localhost/unused")
os.environ.setdefault("ANTHROPIC_API_KEY", "unused-in-these-tests")

import main  # noqa: E402  -- must precede uc2_orchestration, breaks the circular import otherwise
import uc2_orchestration  # noqa: E402
from models import ComputeResponse, DiffResponse, FieldDiffModel, NarrationCheckResponse, TaxLookupResponse, TaxLookupSubResult  # noqa: E402


def _tax_response(rate_pct=18.0, tds_rate_pct=10.0, split_type="CGST_SGST"):
    return TaxLookupResponse(
        split_type=split_type,
        gst=TaxLookupSubResult(status="found", rate_pct=rate_pct, source_filename="x.md", key_clause="clause"),
        tds=TaxLookupSubResult(status="found", rate_pct=tds_rate_pct, tds_section="194J"),
    )


def _facts_uc2(**overrides):
    base = dict(
        invoice_id=1, invoice_date="2025-09-01", base_amount=100000.0, invoice_status="open", po_id=1,
        vendor_id=1, legal_name="Acme Vendor", registered_state="Karnataka", vendor_status="active",
        payment_terms="Net 30", gstin="X", onboarding_state="Karnataka",
        invoice_category="Software", invoice_hsn_sac="998313",
        po_category="Software", po_amount=100000.0, po_status="issued",
        received_amount=100000.0, receipt_status="full", office_state="Karnataka",
        advances_applied=0.0, unapplied_advances=0.0, credits_applied=0.0, payments_made=0.0,
    )
    base.update(overrides)
    return base


def test_not_found_when_invoice_missing(monkeypatch):
    monkeypatch.setattr(uc2_orchestration.db, "retrieve_invoice_facts_uc2", lambda invoice_id: None)
    result = uc2_orchestration.handle_uc2_validate(9999, {"base_amount": 1000})
    assert result["error"] == "not_found"
    assert "9999" in result["message"]


def test_blind_reconstruction_never_sees_submitted_advice(monkeypatch):
    """The compute request must never be built from anything in
    submitted_advice -- assert it directly on the captured call args, not
    just by comment."""
    facts = _facts_uc2()
    captured = {}

    def fake_do_compute(req):
        captured["req"] = req
        return ComputeResponse(base_amount=100000.0, gst={"gst_amount": 18000.0, "cgst": 9000.0, "sgst": 9000.0, "igst": None, "split_type": "CGST_SGST"},
                                tds_amount=10000.0, gross_liability=118000.0, net_disbursement_due=108000.0,
                                pre_tax_ledger_position=100000.0, eligibility="eligible", eligibility_reasons=[],
                                tax_treatment_refused=False)

    monkeypatch.setattr(uc2_orchestration.db, "retrieve_invoice_facts_uc2", lambda invoice_id: facts)
    monkeypatch.setattr(uc2_orchestration, "tax_lookup", lambda req: _tax_response())
    monkeypatch.setattr(uc2_orchestration, "do_compute", fake_do_compute)
    monkeypatch.setattr(uc2_orchestration, "do_diff", lambda req: DiffResponse(
        overall_match=True, blocked=False, fields=[], eligible=True))
    monkeypatch.setattr(uc2_orchestration, "call_text", lambda prompt, max_tokens: "Correct.")
    monkeypatch.setattr(uc2_orchestration, "check_narration_endpoint", lambda req: NarrationCheckResponse(
        passed=True, numbers_found_in_narrative=[], numbers_not_in_structured_result=[]))

    # A wildly wrong submitted_advice -- if this leaked into do_compute's
    # request, the "reconstructed" base_amount would be 1 instead of 100000.
    uc2_orchestration.handle_uc2_validate(1, {"base_amount": 1.0, "net_payable_claimed": 1.0})

    assert captured["req"].base_amount == 100000.0  # from facts, never from submitted_advice


def test_category_conflict_blocks_via_do_compute_args(monkeypatch):
    facts = _facts_uc2(po_category="Software", invoice_category="Services")
    captured = {}

    def fake_do_compute(req):
        captured["req"] = req
        return ComputeResponse(base_amount=100000.0, eligibility="on hold: category unresolved",
                                eligibility_reasons=[], tax_treatment_refused=True,
                                pre_tax_ledger_position=100000.0,
                                category_conflict={"po_category": "Software", "invoice_category": "Services"})

    monkeypatch.setattr(uc2_orchestration.db, "retrieve_invoice_facts_uc2", lambda invoice_id: facts)
    monkeypatch.setattr(uc2_orchestration, "tax_lookup", lambda req: _tax_response())
    monkeypatch.setattr(uc2_orchestration, "do_compute", fake_do_compute)
    monkeypatch.setattr(uc2_orchestration, "do_diff", lambda req: DiffResponse(
        overall_match=False, blocked=True, blocked_reason="unresolved", fields=[], eligible=True))
    monkeypatch.setattr(uc2_orchestration, "call_text", lambda prompt, max_tokens: "Held for review.")
    monkeypatch.setattr(uc2_orchestration, "check_narration_endpoint", lambda req: NarrationCheckResponse(
        passed=True, numbers_found_in_narrative=[], numbers_not_in_structured_result=[]))

    result = uc2_orchestration.handle_uc2_validate(1, {"base_amount": 100000.0})

    assert captured["req"].category_conflict_po_category == "Software"
    assert captured["req"].category_conflict_invoice_category == "Services"
    assert captured["req"].gst_rate_pct is None  # never set when conflict is present
    assert result["diff"]["blocked"] is True


def test_category_mismatch_narration_guard_does_not_crash_on_string_value(monkeypatch):
    """End-to-end regression for the round-3 'nan' fix: the category row's
    claimed/correct now carry real strings (e.g. "Furniture"), and that list
    feeds straight into the narration guard's structured_values, which calls
    float(v) on every entry. do_diff and check_narration_endpoint are
    deliberately left UNMOCKED here so the real diff.py/narration_guard.py
    code runs end-to-end, not a stub that would hide a crash."""
    facts = _facts_uc2(invoice_category="Appliances", po_category="Appliances")
    monkeypatch.setattr(uc2_orchestration.db, "retrieve_invoice_facts_uc2", lambda invoice_id: facts)
    monkeypatch.setattr(uc2_orchestration, "tax_lookup", lambda req: _tax_response())
    monkeypatch.setattr(uc2_orchestration, "do_compute", lambda req: ComputeResponse(
        base_amount=100000.0, gst={"gst_amount": 18000.0, "cgst": 9000.0, "sgst": 9000.0, "igst": None, "split_type": "CGST_SGST"},
        tds_amount=10000.0, gross_liability=118000.0, net_disbursement_due=108000.0,
        pre_tax_ledger_position=100000.0, eligibility="eligible", eligibility_reasons=[], tax_treatment_refused=False))
    monkeypatch.setattr(uc2_orchestration, "call_text", lambda prompt, max_tokens: "Category mismatch found.")

    result = uc2_orchestration.handle_uc2_validate(1, {"base_amount": 100000.0, "category": "Furniture"})

    category_field = next(f for f in result["diff"]["fields"] if f["field"] == "category")
    assert category_field["claimed"] == "Furniture"
    assert category_field["correct"] == "Appliances"
    assert result["guard"] in ("passed", "failed_fallback_used")  # ran to completion, no crash


def test_zero_value_fields_survive_guard_filtering(monkeypatch):
    """A genuine $0 claimed/correct must not be dropped from
    structured_values -- filtering must use `is not None`, never
    truthiness."""
    facts = _facts_uc2()
    monkeypatch.setattr(uc2_orchestration.db, "retrieve_invoice_facts_uc2", lambda invoice_id: facts)
    monkeypatch.setattr(uc2_orchestration, "tax_lookup", lambda req: _tax_response(tds_rate_pct=0.0))
    monkeypatch.setattr(uc2_orchestration, "do_compute", lambda req: ComputeResponse(
        base_amount=100000.0, gst={"gst_amount": 18000.0, "cgst": 9000.0, "sgst": 9000.0, "igst": None, "split_type": "CGST_SGST"},
        tds_amount=0.0, gross_liability=118000.0, net_disbursement_due=118000.0,
        pre_tax_ledger_position=100000.0, eligibility="eligible", eligibility_reasons=[], tax_treatment_refused=False))
    monkeypatch.setattr(uc2_orchestration, "do_diff", lambda req: DiffResponse(
        overall_match=True, blocked=False,
        fields=[FieldDiffModel(field="tds_amount", claimed=0.0, correct=0.0, match=True)],
        eligible=True))
    monkeypatch.setattr(uc2_orchestration, "call_text", lambda prompt, max_tokens: "Correct, TDS is 0.")
    captured_guard_req = {}

    def fake_guard(req):
        captured_guard_req["values"] = req.structured_values
        return NarrationCheckResponse(passed=True, numbers_found_in_narrative=[], numbers_not_in_structured_result=[])

    monkeypatch.setattr(uc2_orchestration, "check_narration_endpoint", fake_guard)

    uc2_orchestration.handle_uc2_validate(1, {"base_amount": 100000.0, "tds_amount": 0.0})
    assert 0.0 in captured_guard_req["values"]


def test_guard_fallback_builds_templated_verdict(monkeypatch):
    facts = _facts_uc2()
    monkeypatch.setattr(uc2_orchestration.db, "retrieve_invoice_facts_uc2", lambda invoice_id: facts)
    monkeypatch.setattr(uc2_orchestration, "tax_lookup", lambda req: _tax_response())
    monkeypatch.setattr(uc2_orchestration, "do_compute", lambda req: ComputeResponse(
        base_amount=100000.0, gst={"gst_amount": 18000.0, "cgst": 9000.0, "sgst": 9000.0, "igst": None, "split_type": "CGST_SGST"},
        tds_amount=10000.0, gross_liability=118000.0, net_disbursement_due=108000.0,
        pre_tax_ledger_position=100000.0, eligibility="eligible", eligibility_reasons=[], tax_treatment_refused=False))
    monkeypatch.setattr(uc2_orchestration, "do_diff", lambda req: DiffResponse(
        overall_match=False, blocked=False,
        fields=[FieldDiffModel(field="gst_rate_pct", claimed=12.0, correct=18.0, match=False, reason="wrong rate")],
        eligible=True))
    monkeypatch.setattr(uc2_orchestration, "call_text", lambda prompt, max_tokens: "hallucinated nonsense")
    monkeypatch.setattr(uc2_orchestration, "check_narration_endpoint", lambda req: NarrationCheckResponse(
        passed=False, numbers_found_in_narrative=[99], numbers_not_in_structured_result=[99]))

    result = uc2_orchestration.handle_uc2_validate(1, {"base_amount": 100000.0, "gst_rate_pct": 12.0})
    assert result["guard"] == "failed_fallback_used"
    assert "gst_rate_pct: claimed 12, correct 18" in result["verdict"]
    assert "hallucinated nonsense" not in result["verdict"]


def test_draft_mode_no_db_lookup(monkeypatch):
    called = {"db": False}

    def fail_if_called(*a, **kw):
        called["db"] = True
        raise AssertionError("draft mode must never query the DB")

    monkeypatch.setattr(uc2_orchestration.db, "retrieve_invoice_facts_uc2", fail_if_called)
    monkeypatch.setattr(uc2_orchestration, "tax_lookup", lambda req: _tax_response())
    monkeypatch.setattr(uc2_orchestration, "do_compute", lambda req: ComputeResponse(
        base_amount=10000.0, gst={"gst_amount": 1800.0, "cgst": 900.0, "sgst": 900.0, "igst": None, "split_type": "CGST_SGST"},
        tds_amount=0.0, gross_liability=11800.0, net_disbursement_due=11800.0,
        pre_tax_ledger_position=10000.0, eligibility="eligible", eligibility_reasons=[], tax_treatment_refused=False))
    monkeypatch.setattr(uc2_orchestration, "do_diff", lambda req: DiffResponse(
        overall_match=True, blocked=False, fields=[], eligible=True))
    monkeypatch.setattr(uc2_orchestration, "call_text", lambda prompt, max_tokens: "Rate matches.")
    monkeypatch.setattr(uc2_orchestration, "check_narration_endpoint", lambda req: NarrationCheckResponse(
        passed=True, numbers_found_in_narrative=[], numbers_not_in_structured_result=[]))

    result = uc2_orchestration.handle_uc2_validate(None, {"base_amount": 10000.0, "category": "Furniture"})
    assert called["db"] is False
    assert result["mode"] == "category_only"
    assert "note" in result


def test_draft_mode_fallback_excludes_base_amount_and_keeps_note(monkeypatch):
    monkeypatch.setattr(uc2_orchestration, "tax_lookup", lambda req: _tax_response())
    monkeypatch.setattr(uc2_orchestration, "do_compute", lambda req: ComputeResponse(
        base_amount=10000.0, gst={"gst_amount": 1800.0, "cgst": 900.0, "sgst": 900.0, "igst": None, "split_type": "CGST_SGST"},
        tds_amount=0.0, gross_liability=11800.0, net_disbursement_due=11800.0,
        pre_tax_ledger_position=10000.0, eligibility="eligible", eligibility_reasons=[], tax_treatment_refused=False))
    monkeypatch.setattr(uc2_orchestration, "do_diff", lambda req: DiffResponse(
        overall_match=False, blocked=False,
        fields=[
            FieldDiffModel(field="base_amount", claimed=999.0, correct=10000.0, match=False, reason="not verified"),
            FieldDiffModel(field="gst_rate_pct", claimed=12.0, correct=18.0, match=False, reason="wrong rate"),
        ],
        eligible=True))
    monkeypatch.setattr(uc2_orchestration, "call_text", lambda prompt, max_tokens: "junk")
    monkeypatch.setattr(uc2_orchestration, "check_narration_endpoint", lambda req: NarrationCheckResponse(
        passed=False, numbers_found_in_narrative=[], numbers_not_in_structured_result=[]))

    result = uc2_orchestration.handle_uc2_validate(None, {"base_amount": 999.0, "category": "Furniture", "gst_rate_pct": 12.0})
    assert "base_amount" not in result["verdict"]
    assert "gst_rate_pct: claimed 12, correct 18" in result["verdict"]
    assert result["note"]  # fallback keeps the note, unlike UC1's fallbacks


def test_computed_returned_on_guard_passed(monkeypatch):
    """The full ComputeResponse must reach the frontend (as `computed`) so
    the step-by-step calculation walkthrough has real figures to render --
    previously built in memory (see uc2_orchestration's `computed` var) and
    silently discarded before this fix."""
    facts = _facts_uc2()
    monkeypatch.setattr(uc2_orchestration.db, "retrieve_invoice_facts_uc2", lambda invoice_id: facts)
    monkeypatch.setattr(uc2_orchestration, "tax_lookup", lambda req: _tax_response())
    monkeypatch.setattr(uc2_orchestration, "do_compute", lambda req: ComputeResponse(
        base_amount=100000.0, gst={"gst_amount": 18000.0, "cgst": 9000.0, "sgst": 9000.0, "igst": None, "split_type": "CGST_SGST"},
        tds_amount=10000.0, gross_liability=118000.0, net_disbursement_due=108000.0,
        pre_tax_ledger_position=100000.0, eligibility="eligible", eligibility_reasons=[], tax_treatment_refused=False))
    monkeypatch.setattr(uc2_orchestration, "do_diff", lambda req: DiffResponse(
        overall_match=True, blocked=False, fields=[], eligible=True))
    monkeypatch.setattr(uc2_orchestration, "call_text", lambda prompt, max_tokens: "Correct.")
    monkeypatch.setattr(uc2_orchestration, "check_narration_endpoint", lambda req: NarrationCheckResponse(
        passed=True, numbers_found_in_narrative=[], numbers_not_in_structured_result=[]))

    result = uc2_orchestration.handle_uc2_validate(1, {"base_amount": 100000.0})

    assert "computed" in result
    assert result["computed"]["gross_liability"] == 118000.0
    assert result["computed"]["net_disbursement_due"] == 108000.0


def test_computed_returned_on_guard_fallback(monkeypatch):
    """Same as above, but on the guard-rejected/fallback path -- `computed`
    must not be dropped just because narration failed the guard."""
    facts = _facts_uc2()
    monkeypatch.setattr(uc2_orchestration.db, "retrieve_invoice_facts_uc2", lambda invoice_id: facts)
    monkeypatch.setattr(uc2_orchestration, "tax_lookup", lambda req: _tax_response())
    monkeypatch.setattr(uc2_orchestration, "do_compute", lambda req: ComputeResponse(
        base_amount=100000.0, gst={"gst_amount": 18000.0, "cgst": 9000.0, "sgst": 9000.0, "igst": None, "split_type": "CGST_SGST"},
        tds_amount=10000.0, gross_liability=118000.0, net_disbursement_due=108000.0,
        pre_tax_ledger_position=100000.0, eligibility="eligible", eligibility_reasons=[], tax_treatment_refused=False))
    monkeypatch.setattr(uc2_orchestration, "do_diff", lambda req: DiffResponse(
        overall_match=True, blocked=False, fields=[], eligible=True))
    monkeypatch.setattr(uc2_orchestration, "call_text", lambda prompt, max_tokens: "hallucinated nonsense")
    monkeypatch.setattr(uc2_orchestration, "check_narration_endpoint", lambda req: NarrationCheckResponse(
        passed=False, numbers_found_in_narrative=[99], numbers_not_in_structured_result=[99]))

    result = uc2_orchestration.handle_uc2_validate(1, {"base_amount": 100000.0})

    assert result["guard"] == "failed_fallback_used"
    assert "computed" in result
    assert result["computed"]["gross_liability"] == 118000.0


def test_computed_returned_on_category_conflict(monkeypatch):
    """Blocked/category-conflict responses must still carry `computed` so
    the step-by-step view can render its pre-tax-ledger-only branch."""
    facts = _facts_uc2(po_category="Software", invoice_category="Services")
    monkeypatch.setattr(uc2_orchestration.db, "retrieve_invoice_facts_uc2", lambda invoice_id: facts)
    monkeypatch.setattr(uc2_orchestration, "tax_lookup", lambda req: _tax_response())
    monkeypatch.setattr(uc2_orchestration, "do_compute", lambda req: ComputeResponse(
        base_amount=100000.0, eligibility="on hold: category unresolved",
        eligibility_reasons=[], tax_treatment_refused=True,
        pre_tax_ledger_position=100000.0,
        category_conflict={"po_category": "Software", "invoice_category": "Services"}))
    monkeypatch.setattr(uc2_orchestration, "do_diff", lambda req: DiffResponse(
        overall_match=False, blocked=True, blocked_reason="unresolved", fields=[], eligible=True))
    monkeypatch.setattr(uc2_orchestration, "call_text", lambda prompt, max_tokens: "Held for review.")
    monkeypatch.setattr(uc2_orchestration, "check_narration_endpoint", lambda req: NarrationCheckResponse(
        passed=True, numbers_found_in_narrative=[], numbers_not_in_structured_result=[]))

    result = uc2_orchestration.handle_uc2_validate(1, {"base_amount": 100000.0})

    assert "computed" in result
    assert result["computed"]["tax_treatment_refused"] is True
    assert result["computed"]["category_conflict"] == {"po_category": "Software", "invoice_category": "Services"}


def test_computed_returned_in_draft_mode(monkeypatch):
    """Draft/category-only mode must also carry `computed` -- this is the
    branch whose net-disbursement figure the frontend needs to caption as
    'structurally zero ledger terms', not just 'coincidentally zero'."""
    monkeypatch.setattr(uc2_orchestration, "tax_lookup", lambda req: _tax_response())
    monkeypatch.setattr(uc2_orchestration, "do_compute", lambda req: ComputeResponse(
        base_amount=10000.0, gst={"gst_amount": 1800.0, "cgst": 900.0, "sgst": 900.0, "igst": None, "split_type": "CGST_SGST"},
        tds_amount=0.0, gross_liability=11800.0, net_disbursement_due=11800.0,
        pre_tax_ledger_position=10000.0, eligibility="eligible", eligibility_reasons=[], tax_treatment_refused=False))
    monkeypatch.setattr(uc2_orchestration, "do_diff", lambda req: DiffResponse(
        overall_match=True, blocked=False, fields=[], eligible=True))
    monkeypatch.setattr(uc2_orchestration, "call_text", lambda prompt, max_tokens: "Rate matches.")
    monkeypatch.setattr(uc2_orchestration, "check_narration_endpoint", lambda req: NarrationCheckResponse(
        passed=True, numbers_found_in_narrative=[], numbers_not_in_structured_result=[]))

    result = uc2_orchestration.handle_uc2_validate(None, {"base_amount": 10000.0, "category": "Furniture"})

    assert "computed" in result
    assert result["computed"]["advances_applied"] == 0.0
    assert result["computed"]["net_disbursement_due"] == 11800.0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
