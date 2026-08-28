"""Read-only smoke tests for the deployed end-to-end workflows.

These call the same public n8n webhooks as Streamlit.  They exercise all
production boundaries: n8n, Postgres retrieval, Source C lookup, FastAPI
calculation/diffing, and the response contract returned to the UI.

They are intentionally opt-in because they use the deployed infrastructure
and can invoke the narrative LLM.  Run with:

    RUN_LIVE_E2E=1 E2E_N8N_BASE_URL=https://your-n8n-host pytest e2e -v -m live_e2e
"""

import os

import pytest
import requests


pytestmark = pytest.mark.live_e2e


def _base_url():
    if os.environ.get("RUN_LIVE_E2E") != "1":
        pytest.skip("Live E2E is opt-in. Set RUN_LIVE_E2E=1 after deployment.")
    url = os.environ.get("E2E_N8N_BASE_URL")
    if not url:
        pytest.skip("Set E2E_N8N_BASE_URL to the deployed n8n base URL.")
    return url.rstrip("/")


def _post(path, payload):
    response = requests.post(f"{_base_url()}/webhook/{path}", json=payload, timeout=120)
    assert response.status_code == 200, response.text
    try:
        return response.json()
    except ValueError as error:
        pytest.fail(f"{path} did not return JSON: {response.text[:500]}")


def _field(diff, name):
    for field in diff["fields"]:
        if field["field"] == name:
            return field
    raise AssertionError(f"Missing {name!r} in diff: {diff['fields']}")


def test_uc1_vendor_stated_superseded_rate_is_recomputed_from_source_c():
    result = _post(
        "uc1-ask",
        {"question": "Is TechNova Software Solutions invoice INV-17 correctly taxed?"},
    )

    evidence = result["evidence"]
    tax = result["tax_evidence"]
    assert evidence["net_disbursement_due"] == 112200.0
    assert evidence["gst"]["gst_amount"] == 13200.0
    assert tax["gst"]["status"] == "found"
    assert tax["gst"]["rate_pct"] == 12.0
    assert tax["gst"]["source_filename"] == "gst_software_post_sep2025.md"
    assert tax["gst"]["key_clause"]
    assert result["guard"] in {"passed", "failed_fallback_used"}


def test_uc1_effective_date_changes_the_answer():
    # The query pins the invoice ID so n8n cannot choose a newer vendor invoice.
    pre = _post(
        "uc1-ask",
        {"question": "What GST rate and net payable apply to Skyline Software Labs invoice INV-13?"},
    )
    post = _post(
        "uc1-ask",
        {"question": "What GST rate and net payable apply to Skyline Software Labs invoice INV-14?"},
    )

    assert pre["tax_evidence"]["gst"]["rate_pct"] == 18.0
    assert post["tax_evidence"]["gst"]["rate_pct"] == 12.0
    assert pre["evidence"]["net_disbursement_due"] == 108000.0
    assert post["evidence"]["net_disbursement_due"] == 102000.0


def test_uc2_correct_advice_is_accepted():
    result = _post(
        "uc2-validate",
        {
            "invoice_id": 1,
            "submitted_advice": {
                "base_amount": 85000.0,
                "category": "Furniture",
                "gst_rate_pct": 18.0,
                "gst_igst": 15300.0,
                "tds_amount": 0.0,
                "net_payable_claimed": 100300.0,
            },
        },
    )

    assert result["diff"]["blocked"] is False
    assert result["diff"]["overall_match"] is True
    assert all(field["match"] for field in result["diff"]["fields"])
    assert result["guard"] in {"passed", "failed_fallback_used"}


def test_uc2_multi_error_advice_reports_all_key_errors():
    result = _post(
        "uc2-validate",
        {
            "invoice_id": 4,
            "submitted_advice": {
                "base_amount": 61000.0,
                "category": "Appliances",
                "gst_rate_pct": 12.0,
                "gst_cgst": 3660.0,
                "gst_sgst": 3660.0,
                "tds_amount": 6100.0,
                "net_payable_claimed": 62220.0,
            },
        },
    )

    diff = result["diff"]
    assert diff["overall_match"] is False
    assert _field(diff, "gst_rate_pct")["match"] is False
    assert _field(diff, "tds_amount")["match"] is False
    assert _field(diff, "net_payable")["match"] is False


def test_uc2_unresolved_category_conflict_blocks_validation():
    """A plausible advice cannot pass when PO and invoice categories disagree."""
    result = _post(
        "uc2-validate",
        {
            "invoice_id": 16,
            "submitted_advice": {
                "base_amount": 90000.0,
                "category": "Services",
                "gst_rate_pct": 18.0,
                "gst_cgst": 8100.0,
                "gst_sgst": 8100.0,
                "tds_amount": 9000.0,
                "net_payable_claimed": 97200.0,
            },
        },
    )

    assert result["diff"]["blocked"] is True
    assert "unresolved" in result["diff"]["blocked_reason"].lower()


def test_uc2_unknown_invoice_is_a_clean_not_found_response():
    result = _post(
        "uc2-validate",
        {"invoice_id": 999999, "submitted_advice": {"base_amount": 1.0}},
    )

    assert result["error"] == "not_found"
    assert "999999" in result["message"]
