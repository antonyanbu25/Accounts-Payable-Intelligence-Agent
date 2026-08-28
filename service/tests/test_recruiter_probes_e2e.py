"""Recruiter-style end-to-end probes for failure modes hidden by happy paths.

Each test uses FastAPI's public contract and follows the same endpoint
sequence as n8n. They model the questions a skeptical reviewer asks after a
successful demo: does the system refuse unknown rules, distinguish correct
math from a safe payment release, preserve a human category disagreement,
and prevent prose from inventing monetary figures?
"""

import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from main import app  # noqa: E402


def _lookup(client, category, code, date, vendor_state="Karnataka", office_state="Karnataka", needs_tds=True):
    response = client.post(
        "/tax-lookup",
        json={
            "category": category,
            "hsn_or_sac": code,
            "invoice_date": date,
            "vendor_state": vendor_state,
            "office_state": office_state,
            "needs_tds": needs_tds,
        },
    )
    assert response.status_code == 200
    return response.json()


def _compute(client, tax, base_amount, **overrides):
    payload = {
        "base_amount": base_amount,
        "gst_rate_pct": tax["gst"]["rate_pct"],
        "tds_rate_pct": tax["tds"]["rate_pct"] if tax.get("tds") else 0,
        "tds_section": tax["tds"]["tds_section"] if tax.get("tds") else None,
        "split_type": tax["split_type"],
        "po_amount": base_amount,
        "receipt_amount": base_amount,
    }
    payload.update(overrides)
    response = client.post("/compute", json=payload)
    assert response.status_code == 200
    return response.json()


def test_recruiter_probe_unknown_tax_rule_is_not_guessed():
    """A familiar/current rate must not substitute for a missing tax rule."""
    with TestClient(app) as client:
        tax = _lookup(client, "Software", "998313", "2010-01-01")

    assert tax["gst"]["status"] == "not_found"
    assert tax["gst"]["rate_pct"] is None
    assert tax["gst"]["source_filename"] is None
    assert tax["tds"]["status"] == "not_found"


def test_recruiter_probe_correct_math_is_not_a_payment_release_approval():
    """Correct arithmetic for a cancelled PO must still display a release hold."""
    with TestClient(app) as client:
        tax = _lookup(client, "Appliances", "8516", "2026-03-11", needs_tds=False)
        computed = _compute(client, tax, 25000, po_status="cancelled")
        response = client.post(
            "/diff",
            json={
                "compute_result": computed,
                "submitted_advice": {
                    "base_amount": 25000,
                    "gst_rate_pct": 18,
                    "gst_cgst": 2250,
                    "gst_sgst": 2250,
                    "tds_amount": 0,
                    "net_payable_claimed": 29500,
                },
            },
        )

    assert response.status_code == 200
    diff = response.json()
    assert diff["overall_match"] is True  # numerical validation remains truthful
    assert diff["eligible"] is False      # release is still explicitly blocked
    assert "PO cancelled" in diff["eligibility_reasons"]
    assert all(field["match"] for field in diff["fields"])


def test_recruiter_probe_wrong_category_is_not_ignored_when_amounts_match():
    """A human category claim is controlled input, not decorative free text."""
    with TestClient(app) as client:
        tax = _lookup(client, "Furniture", "9403", "2026-03-10", "Maharashtra", "Karnataka", needs_tds=False)
        computed = _compute(client, tax, 85000)
        response = client.post(
            "/diff",
            json={
                "compute_result": computed,
                "category_reason": "Submitted category 'Software' does not match the invoice on file ('Furniture').",
                "submitted_advice": {
                    "base_amount": 85000,
                    "category": "Software",
                    "gst_rate_pct": 18,
                    "gst_igst": 15300,
                    "tds_amount": 0,
                    "net_payable_claimed": 100300,
                },
            },
        )

    assert response.status_code == 200
    diff = response.json()
    category = next(field for field in diff["fields"] if field["field"] == "category")
    assert category["match"] is False
    assert "Furniture" in category["reason"]
    assert diff["overall_match"] is False


def test_recruiter_probe_narration_guard_rejects_an_unsupported_currency_amount():
    """A fluent sentence remains invalid if one monetary amount is invented."""
    with TestClient(app) as client:
        response = client.post(
            "/check-narration",
            json={
                "narrative_text": "The net amount to release is 29,999 rupees.",
                "structured_values": [25000, 4500, 29500],
            },
        )

    assert response.status_code == 200
    result = response.json()
    assert result["passed"] is False
    assert 29999.0 in result["numbers_not_in_structured_result"]
