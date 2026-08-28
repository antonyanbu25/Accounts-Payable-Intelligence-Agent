"""End-to-end API workflow tests.

These tests use the actual FastAPI application and the real Source C corpus.
They deliberately follow the same boundary sequence n8n uses in production:

    /tax-lookup -> /compute -> /diff

They are fast and deterministic, so they run on every change without calling
Anthropic, n8n, or a hosted database.  The separate ``e2e/`` suite covers the
deployed n8n + Postgres path.
"""

import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from main import app  # noqa: E402


def _tax_payload(category, hsn_or_sac, invoice_date, vendor_state, office_state, needs_tds=True):
    return {
        "category": category,
        "hsn_or_sac": hsn_or_sac,
        "invoice_date": invoice_date,
        "vendor_state": vendor_state,
        "office_state": office_state,
        "needs_tds": needs_tds,
    }


def _compute_payload(tax_result, *, base_amount, **overrides):
    """Build the compute request exactly as the n8n workflow does.

    Keeping this adapter in the test makes the dependency explicit: a computed
    payment figure must use the regulatory rate returned from ``/tax-lookup``;
    it must never use an invoice's vendor-stated GST rate.
    """
    payload = {
        "base_amount": base_amount,
        "gst_rate_pct": tax_result["gst"]["rate_pct"],
        "tds_rate_pct": tax_result["tds"]["rate_pct"] if tax_result.get("tds") else 0,
        "tds_section": tax_result["tds"]["tds_section"] if tax_result.get("tds") else None,
        "split_type": tax_result["split_type"],
        "po_amount": base_amount,
        "receipt_amount": base_amount,
    }
    payload.update(overrides)
    return payload


def test_health_reports_the_real_tax_corpus_loaded():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["tax_documents_loaded"] >= 10


def test_uc1_api_flow_uses_the_post_change_rule_and_returns_traceable_evidence():
    """UC1 happy path: Source C -> deterministic math -> source citations.

    This reproduces the vendor-stated-superseded-rate scenario.  No claimed
    18% invoice value is sent to compute; only the 12% rate grounded in Source
    C crosses the service boundary.
    """
    with TestClient(app) as client:
        tax_response = client.post(
            "/tax-lookup",
            json=_tax_payload("Software", "998313", "2025-11-15", "Karnataka", "Karnataka"),
        )
        assert tax_response.status_code == 200
        tax = tax_response.json()

        compute_response = client.post(
            "/compute",
            json=_compute_payload(tax, base_amount=110000),
        )

    assert tax["gst"]["status"] == "found"
    assert tax["gst"]["rate_pct"] == 12.0
    assert tax["gst"]["source_filename"] == "gst_software_post_sep2025.md"
    assert tax["gst"]["key_clause"]
    assert tax["tds"]["rate_pct"] == 10.0

    assert compute_response.status_code == 200
    result = compute_response.json()
    assert result["gst"]["gst_amount"] == 13200.0
    assert result["tds_amount"] == 11000.0
    assert result["net_disbursement_due"] == 112200.0
    assert result["eligibility"] == "eligible"


def test_uc1_api_flow_changes_only_when_the_effective_date_changes():
    """Same source category and amount, but different dates must change GST."""
    with TestClient(app) as client:
        pre_tax = client.post(
            "/tax-lookup",
            json=_tax_payload("Software", "998313", "2025-09-01", "Tamil Nadu", "Tamil Nadu"),
        ).json()
        post_tax = client.post(
            "/tax-lookup",
            json=_tax_payload("Software", "998313", "2025-10-01", "Tamil Nadu", "Tamil Nadu"),
        ).json()

        pre = client.post("/compute", json=_compute_payload(pre_tax, base_amount=100000)).json()
        post = client.post("/compute", json=_compute_payload(post_tax, base_amount=100000)).json()

    assert pre_tax["gst"]["rate_pct"] == 18.0
    assert post_tax["gst"]["rate_pct"] == 12.0
    assert pre["net_disbursement_due"] == 108000.0
    assert post["net_disbursement_due"] == 102000.0


def test_uc1_api_flow_refuses_all_tax_dependent_figures_for_category_conflict():
    """A conflict with no authority rule may expose only the pre-tax position."""
    request = {
        "base_amount": 90000,
        "po_amount": 90000,
        "receipt_amount": 90000,
        "category_conflict_po_category": "Software",
        "category_conflict_invoice_category": "Services",
    }
    with TestClient(app) as client:
        response = client.post("/compute", json=request)

    assert response.status_code == 200
    result = response.json()
    assert result["tax_treatment_refused"] is True
    assert result["pre_tax_ledger_position"] == 90000.0
    assert result["gst"] is None
    assert result["gross_liability"] is None
    assert result["net_disbursement_due"] is None
    assert result["eligibility"] == "on hold: category unresolved"


def test_uc2_api_flow_correct_advice_is_not_false_positive():
    """UC2: reconstruct first, then verify that a correct advice stays correct."""
    with TestClient(app) as client:
        tax = client.post(
            "/tax-lookup",
            json=_tax_payload("Furniture", "9403", "2026-03-10", "Maharashtra", "Karnataka", False),
        ).json()
        computed = client.post("/compute", json=_compute_payload(tax, base_amount=85000)).json()
        diff_response = client.post(
            "/diff",
            json={
                "compute_result": computed,
                "submitted_advice": {
                    "base_amount": 85000,
                    "category": "Furniture",
                    "gst_rate_pct": 18,
                    "gst_igst": 15300,
                    "tds_amount": 0,
                    "net_payable_claimed": 100300,
                },
            },
        )

    assert diff_response.status_code == 200
    diff = diff_response.json()
    assert diff["blocked"] is False
    assert diff["overall_match"] is True
    assert all(field["match"] for field in diff["fields"])


def test_uc2_api_flow_reports_each_independent_error():
    """UC2 must report all errors, rather than stop at the first mismatch."""
    with TestClient(app) as client:
        tax = client.post(
            "/tax-lookup",
            json=_tax_payload("Appliances", "8516", "2026-02-20", "Karnataka", "Karnataka", False),
        ).json()
        computed = client.post("/compute", json=_compute_payload(tax, base_amount=61000)).json()
        diff_response = client.post(
            "/diff",
            json={
                "compute_result": computed,
                "submitted_advice": {
                    "base_amount": 61000,
                    "category": "Appliances",
                    "gst_rate_pct": 12,
                    "gst_cgst": 3660,
                    "gst_sgst": 3660,
                    "tds_amount": 6100,
                    "net_payable_claimed": 62220,
                },
            },
        )

    assert diff_response.status_code == 200
    diff = diff_response.json()
    mismatches = {field["field"] for field in diff["fields"] if not field["match"]}
    assert diff["overall_match"] is False
    assert {"gst_rate_pct", "tds_amount", "net_payable"}.issubset(mismatches)
    assert all(field["reason"] for field in diff["fields"] if not field["match"])
