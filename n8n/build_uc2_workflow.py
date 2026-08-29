#!/usr/bin/env python3
"""
Builds the UC2 (validation of a human-prepared payment advice) workflow.

Deliberately has NO intent-parsing LLM call and NO vendor-name resolution --
the input is already structured JSON (invoice_id + submitted_advice), per
the plan's UC2 input design (form/paste/upload, never LLM-parsed). The
retrieval -> tax-lookup -> compute chain runs identically to UC1 and is
never given the submitted_advice -- that's what makes this a genuine blind
reconstruction, not a review of the human's numbers.

Two branches, chosen by whether the request includes an invoice_id:
  - invoice_id present -> the full reconstruction above (existing record,
    3-way match, vendor eligibility, real tax jurisdiction).
  - invoice_id absent -> a deliberately WEAKER "category rate check" branch
    for an invoice that isn't recorded in the system yet (a real accountant
    workflow: sanity-check the tax math on a draft advice before the
    invoice is even entered). Reuses the same tested /compute + /diff
    engine (base_amount is fed in as the accountant's own claim -- there is
    no PO/receipt to verify it against, which is exactly the pre-existing,
    already-tested "non-PO invoice" case in compute.py), but the response is
    explicitly labeled mode: "category_only" and never lets eligibility or
    3-way-match read as verified -- those fields simply aren't surfaced,
    with a note explaining why, rather than silently defaulting to
    misleading values.
"""
import os

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

N8N_BASE = os.environ["N8N_BASE_URL"]
N8N_KEY = os.environ["N8N_API_KEY"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
# Both overridable so this same script builds either the local dev workflow
# or the deployed one, without hardcoding one target's values over the
# other's. Defaults are the local dev instance's values.
PG_CRED_ID = os.environ.get("N8N_PG_CRED_ID", "RuLJlMwbZqaK22dc")
FASTAPI_BASE = os.environ.get("FASTAPI_BASE_URL", "http://127.0.0.1:8123")
HEADERS = {"X-N8N-API-KEY": N8N_KEY, "Content-Type": "application/json"}

RETRIEVE_FACTS_SQL = """SELECT
  i.invoice_id, TO_CHAR(i.invoice_date, 'YYYY-MM-DD') AS invoice_date, i.base_amount, i.gst_rate_stated, i.gst_amount_stated, i.status AS invoice_status,
  i.po_id,
  v.vendor_id, v.legal_name, v.registered_state, v.status AS vendor_status, v.payment_terms, v.gstin,
  vo.submitted_state AS onboarding_state,
  ci.category_id AS invoice_category_id, ci.category_name AS invoice_category, ci.hsn_or_sac_code AS invoice_hsn_sac,
  po.category_id AS po_category_id, cp.category_name AS po_category,
  po.po_amount, po.status AS po_status,
  r.received_amount, r.status AS receipt_status,
  o.state AS office_state,
  COALESCE((SELECT SUM(amount) FROM advance WHERE applied_against_invoice_id = i.invoice_id), 0) AS advances_applied,
  COALESCE((SELECT SUM(amount) FROM advance WHERE vendor_id = v.vendor_id AND applied_against_invoice_id IS NULL), 0) AS unapplied_advances,
  COALESCE((SELECT SUM(amount) FROM credit_note WHERE invoice_id = i.invoice_id), 0) AS credits_applied,
  COALESCE((SELECT SUM(amount) FROM payment WHERE invoice_id = i.invoice_id), 0) AS payments_made
FROM invoice i
JOIN vendor_master v ON i.vendor_id = v.vendor_id
JOIN vendor_onboarding vo ON v.vendor_id = vo.vendor_id
JOIN category ci ON i.category_id = ci.category_id
LEFT JOIN purchase_order po ON i.po_id = po.po_id
LEFT JOIN category cp ON po.category_id = cp.category_id
LEFT JOIN receipt r ON r.po_id = po.po_id
LEFT JOIN requisition req ON po.requisition_id = req.requisition_id
LEFT JOIN office o ON req.office_id = o.office_id
WHERE i.invoice_id = {{ $json.body.invoice_id }}
LIMIT 1;"""
# NOTE: this query never references $json.body.submitted_advice -- that's
# the blind-reconstruction guarantee, enforced structurally, not by promise.

TAX_LOOKUP_BODY_EXPR = """={{ JSON.stringify({
  category: $('Retrieve Facts').first().json.invoice_category,
  hsn_or_sac: $('Retrieve Facts').first().json.invoice_hsn_sac,
  invoice_date: $('Retrieve Facts').first().json.invoice_date,
  vendor_state: $('Retrieve Facts').first().json.registered_state,
  office_state: $('Retrieve Facts').first().json.office_state,
  needs_tds: true
}) }}"""

COMPUTE_BODY_EXPR = """={{ (() => {
  const f = $('Retrieve Facts').first().json;
  const tax = $json;
  const conflict = f.po_category && f.invoice_category && f.po_category !== f.invoice_category;
  const body = {
    base_amount: Number(f.base_amount),
    advances_applied: Number(f.advances_applied),
    credits_applied: Number(f.credits_applied),
    payments_made: Number(f.payments_made),
    unapplied_advances: Number(f.unapplied_advances),
    po_amount: f.po_amount !== null ? Number(f.po_amount) : null,
    receipt_amount: f.received_amount !== null ? Number(f.received_amount) : null,
    vendor_status: f.vendor_status,
    po_status: f.po_status,
    invoice_status: f.invoice_status
  };
  if (conflict) {
    body.category_conflict_po_category = f.po_category;
    body.category_conflict_invoice_category = f.invoice_category;
  } else {
    body.gst_rate_pct = tax.gst.rate_pct;
    body.tds_rate_pct = tax.tds ? tax.tds.rate_pct : 0.0;
    body.tds_section = tax.tds ? tax.tds.tds_section : null;
    body.split_type = tax.split_type;
  }
  return JSON.stringify(body);
})() }}"""

DIFF_BODY_EXPR = """={{ (() => {
  const advice = $('UC2 Webhook').first().json.body.submitted_advice;
  const facts = $('Retrieve Facts').first().json;
  let categoryReason = '';
  if (advice.category && facts.invoice_category && advice.category !== facts.invoice_category) {
    categoryReason = 'Submitted category \\'' + advice.category + '\\' does not match the invoice on file (\\'' + facts.invoice_category + '\\').';
  }
  return JSON.stringify({
    compute_result: $json,
    submitted_advice: advice,
    category_reason: categoryReason
  });
})() }}"""

NARRATE_DIFF_BODY_EXPR = """={{ JSON.stringify({
  model: "claude-sonnet-4-5-20250929",
  max_tokens: 500,
  messages: [{role:"user", content:
    "You are validating an accountant's payment advice against an independently-computed true result. Write a short, plain-language verdict (3-7 sentences): state clearly whether the advice's NUMBERS are CORRECT or have DIVERGENCES, and for every mismatched field state what was claimed, what's correct, and briefly why -- using ONLY the numbers and reasons in this JSON, never inventing or altering one. If overall_match is true, say so plainly and do not invent a divergence. SEPARATELY and REGARDLESS of whether the numbers match: if eligible is false, you MUST prominently warn that this payment is NOT clear to release and state every reason in eligibility_reasons -- a numerically-correct advice for an ineligible payment (blocked vendor, cancelled PO, failed 3-way match) is still not safe to pay, and this warning must never be omitted or softened. ALSO SEPARATELY: if unapplied_advance_advisory is present and non-zero, state it plainly as its own note -- an advance sitting against this vendor/PO that hasn't been netted into this figure is a real overpayment risk, and a numerically-correct advice must never read as unconditionally 'clear to release in full' when one exists; never omit or soften this note either. ALSO SEPARATELY: if split_undetermined is true, state plainly (using split_undetermined_note) that the CGST/SGST-vs-IGST split could not be independently verified because this invoice has no linked purchase order/office -- do NOT claim the split was checked or that it matches/doesn't match, and do not let this read as a numeric divergence; the total GST amount and every other field were still fully verified. Diff result: " + JSON.stringify($json)
  }]
}) }}"""

CHECK_NARRATION_BODY_EXPR = """={{ (() => {
  const d = $('Diff').first().json;
  const nums = [];
  for (const f of d.fields) { if (f.claimed !== null && f.claimed !== undefined) nums.push(f.claimed); if (f.correct !== null && f.correct !== undefined) nums.push(f.correct); }
  if (d.unapplied_advance_advisory !== null && d.unapplied_advance_advisory !== undefined) { nums.push(d.unapplied_advance_advisory); }
  return JSON.stringify({
    narrative_text: $json.content[0].text,
    structured_values: nums
  });
})() }}"""

# --------------------------------------------------------------------------
# "New invoice" branch: no invoice_id in the request -- the accountant is
# sanity-checking a draft advice for something not yet entered anywhere.
# Same HSN/SAC-by-category map as UC1's category-only path (build_uc1_workflow.py
# CATEGORY_TAX_LOOKUP_BODY_EXPR) -- duplicated rather than shared, matching
# this codebase's existing convention of each workflow-builder script being
# self-contained. vendor_state/office_state are the same neutral placeholder
# UC1 uses for the same reason: the rate itself (compute.py / tax_lookup.py)
# never depends on state, only the CGST/SGST-vs-IGST split does, and that
# split isn't meaningful (or checked) here without a real vendor/office.
# --------------------------------------------------------------------------
NEW_INVOICE_TAX_LOOKUP_BODY_EXPR = """={{ (() => {
  const advice = $('UC2 Webhook').first().json.body.submitted_advice || {};
  const invoiceDate = $('UC2 Webhook').first().json.body.invoice_date || new Date().toISOString().slice(0,10);
  const HSN_SAC_BY_CATEGORY = {Furniture: '9403', Software: '998313', Services: '998311', Food: '996331', Appliances: '8516'};
  return JSON.stringify({
    category: advice.category,
    hsn_or_sac: HSN_SAC_BY_CATEGORY[advice.category] || '',
    invoice_date: invoiceDate,
    vendor_state: 'Karnataka',
    office_state: 'Karnataka',
    needs_tds: true
  });
})() }}"""

# Reuses /compute exactly as-is -- no po_amount/receipt_amount means
# check_three_way_match(...) returns None (compute.py's own documented
# "non-PO invoice, nothing to match" case, not a new code path), and
# base_amount is the accountant's own claim since there is no PO/receipt to
# independently source it from. vendor_status/po_status/invoice_status are
# left at /compute's defaults (active/None/open) purely so the arithmetic
# runs -- the response layer below never surfaces the resulting eligibility
# as if it were a verified fact.
NEW_INVOICE_COMPUTE_BODY_EXPR = """={{ (() => {
  const advice = $('UC2 Webhook').first().json.body.submitted_advice || {};
  const tax = $json;
  return JSON.stringify({
    base_amount: Number(advice.base_amount),
    gst_rate_pct: tax.gst.rate_pct,
    tds_rate_pct: tax.tds ? tax.tds.rate_pct : 0.0,
    tds_section: tax.tds ? tax.tds.tds_section : null,
    split_type: tax.split_type
  });
})() }}"""

NEW_INVOICE_DIFF_BODY_EXPR = """={{ (() => {
  const advice = $('UC2 Webhook').first().json.body.submitted_advice || {};
  return JSON.stringify({
    compute_result: $json,
    submitted_advice: advice,
    category_reason: ''
  });
})() }}"""

NEW_INVOICE_NARRATE_BODY_EXPR = """={{ JSON.stringify({
  model: "claude-sonnet-4-5-20250929",
  max_tokens: 400,
  messages: [{role:"user", content:
    "You are checking a DRAFT payment advice for an invoice that is NOT YET recorded in the system -- there is no purchase order, receipt, or invoice record to reconstruct against, so this is a REDUCED check: only whether the submitted GST rate, TDS amount, and net-payable math use the CURRENT correct tax rate for the stated category and date. The submitted base amount is taken as given, not independently verified. Do NOT say the payment is eligible, matched, or clear to release, and do NOT mention a 3-way match -- neither can be assessed without a real record. Write a short, plain-language verdict (3-5 sentences), using ONLY the numbers in this JSON, never inventing one, and explicitly note this is a rate-only check on an unrecorded invoice. Diff result: " + JSON.stringify($json)
  }]
}) }}"""

NEW_INVOICE_CHECK_NARRATION_BODY_EXPR = """={{ (() => {
  const d = $('New Invoice Diff').first().json;
  const nums = [];
  for (const f of d.fields) { if (f.claimed !== null && f.claimed !== undefined) nums.push(f.claimed); if (f.correct !== null && f.correct !== undefined) nums.push(f.correct); }
  return JSON.stringify({
    narrative_text: $json.content[0].text,
    structured_values: nums
  });
})() }}"""


def http_node(name, url, body_expr, node_id, x, y, extra_headers=None):
    return {
        "id": node_id, "name": name, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [x, y],
        "parameters": {
            "method": "POST", "url": url, "sendHeaders": True,
            "headerParameters": {"parameters": extra_headers or []},
            "sendBody": True, "specifyBody": "json", "jsonBody": body_expr, "options": {},
        },
    }


def postgres_node(name, sql, node_id, x, y, always_output=False):
    node = {
        "id": node_id, "name": name, "type": "n8n-nodes-base.postgres", "typeVersion": 2.6,
        "position": [x, y],
        "parameters": {"operation": "executeQuery", "query": "=" + sql, "options": {}},
        "credentials": {"postgres": {"id": PG_CRED_ID, "name": "AP Postgres (Render)"}},
    }
    if always_output:
        node["alwaysOutputData"] = True
    return node


# See build_uc1_workflow.py for why this reads from the environment rather
# than embedding the key directly.
anthropic_headers = [
    {"name": "x-api-key", "value": "={{ $env.ANTHROPIC_API_KEY }}"},
    {"name": "anthropic-version", "value": "2023-06-01"},
    {"name": "content-type", "value": "application/json"},
]

nodes = [
    {
        "id": "webhook", "name": "UC2 Webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 300],
        "parameters": {"httpMethod": "POST", "path": "uc2-validate", "responseMode": "responseNode", "options": {}},
        "webhookId": "uc2-validate",
    },
    {
        "id": "invoice_id_provided_if", "name": "Invoice ID Provided?", "type": "n8n-nodes-base.if", "typeVersion": 2,
        "position": [140, 300],
        "parameters": {"conditions": {"options": {"caseSensitive": True, "typeValidation": "loose"},
                        "conditions": [{"leftValue": "={{ $json.body.invoice_id }}", "rightValue": "",
                                        "operator": {"type": "string", "operation": "notEmpty"}}],
                        "combinator": "and"}},
    },
    postgres_node("Retrieve Facts", RETRIEVE_FACTS_SQL, "retrieve_facts", 260, 200, always_output=True),
    {
        "id": "found_if", "name": "Invoice Found?", "type": "n8n-nodes-base.if", "typeVersion": 2,
        "position": [520, 300],
        "parameters": {"conditions": {"options": {"caseSensitive": True, "typeValidation": "loose"},
                        "conditions": [{"leftValue": "={{ $json.invoice_id }}", "rightValue": "",
                                        "operator": {"type": "string", "operation": "notEmpty"}}],
                        "combinator": "and"}},
    },
    {
        "id": "respond_not_found", "name": "Respond Not Found", "type": "n8n-nodes-base.respondToWebhook",
        "typeVersion": 1.1, "position": [780, 460],
        "parameters": {"respondWith": "json", "responseBody":
            "={{ JSON.stringify({ error: 'not_found', message: 'No invoice found with id ' + $('UC2 Webhook').first().json.body.invoice_id + ' -- cannot validate an advice against a record that does not exist.' }) }}"},
    },
    http_node("Tax Lookup", f"{FASTAPI_BASE}/tax-lookup", TAX_LOOKUP_BODY_EXPR, "tax_lookup", 780, 200),
    http_node("Compute", f"{FASTAPI_BASE}/compute", COMPUTE_BODY_EXPR, "compute", 1040, 200),
    http_node("Diff", f"{FASTAPI_BASE}/diff", DIFF_BODY_EXPR, "diff", 1300, 200),
    http_node("Narrate Diff", "https://api.anthropic.com/v1/messages", NARRATE_DIFF_BODY_EXPR,
              "narrate_diff", 1560, 200, anthropic_headers),
    http_node("Check Narration", f"{FASTAPI_BASE}/check-narration", CHECK_NARRATION_BODY_EXPR,
              "check_narration", 1820, 200),
    {
        "id": "guard_if", "name": "Guard Passed?", "type": "n8n-nodes-base.if", "typeVersion": 2,
        "position": [2080, 200],
        "parameters": {"conditions": {"options": {"caseSensitive": True, "typeValidation": "loose"},
                        "conditions": [{"leftValue": "={{ $json.passed }}", "rightValue": True,
                                        "operator": {"type": "boolean", "operation": "true"}}],
                        "combinator": "and"}},
    },
    {
        "id": "respond_ok", "name": "Respond OK", "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.1,
        "position": [2340, 100],
        "parameters": {"respondWith": "json", "responseBody":
            "={{ JSON.stringify({ verdict: $('Narrate Diff').first().json.content[0].text, diff: $('Diff').first().json, tax_evidence: $('Tax Lookup').first().json, guard: 'passed' }) }}"},
    },
    {
        "id": "respond_fallback", "name": "Respond Fallback (templated)", "type": "n8n-nodes-base.respondToWebhook",
        "typeVersion": 1.1, "position": [2340, 300],
        "parameters": {"respondWith": "json", "responseBody":
            "={{ (() => { const d = $('Diff').first().json; const mism = d.fields.filter(f => !f.match).map(f => f.field + ': claimed ' + f.claimed + ', correct ' + f.correct); const numVerdict = d.overall_match ? 'Advice matches the independently computed result.' : ('Advice diverges on: ' + mism.join('; ') + '.'); const eligVerdict = d.eligible ? '' : (' ⚠ NOT CLEAR TO PAY regardless of the numbers above: ' + d.eligibility_reasons.join('; ') + '.'); const advanceVerdict = (d.unapplied_advance_advisory !== null && d.unapplied_advance_advisory !== undefined && d.unapplied_advance_advisory !== 0) ? (' ⚠ Advisory: an unapplied advance of ' + d.unapplied_advance_advisory + ' exists against this vendor/PO and has NOT been netted here -- a possible overpayment risk if missed.') : ''; const splitVerdict = d.split_undetermined ? (' ℹ ' + d.split_undetermined_note) : ''; return JSON.stringify({ verdict: numVerdict + eligVerdict + advanceVerdict + splitVerdict + ' [Showing the verified figures directly -- the written summary did not pass our accuracy check.]', diff: d, tax_evidence: $('Tax Lookup').first().json, guard: 'failed_fallback_used' }); })() }}"},
    },

    # ---- New-invoice branch: no invoice_id -- category rate check only ----
    http_node("New Invoice Tax Lookup", f"{FASTAPI_BASE}/tax-lookup", NEW_INVOICE_TAX_LOOKUP_BODY_EXPR,
              "new_invoice_tax_lookup", 260, 460),
    http_node("New Invoice Compute", f"{FASTAPI_BASE}/compute", NEW_INVOICE_COMPUTE_BODY_EXPR,
              "new_invoice_compute", 520, 460),
    http_node("New Invoice Diff", f"{FASTAPI_BASE}/diff", NEW_INVOICE_DIFF_BODY_EXPR,
              "new_invoice_diff", 780, 460),
    http_node("New Invoice Narrate Diff", "https://api.anthropic.com/v1/messages", NEW_INVOICE_NARRATE_BODY_EXPR,
              "new_invoice_narrate_diff", 1040, 460, anthropic_headers),
    http_node("New Invoice Check Narration", f"{FASTAPI_BASE}/check-narration", NEW_INVOICE_CHECK_NARRATION_BODY_EXPR,
              "new_invoice_check_narration", 1300, 460),
    {
        "id": "new_invoice_guard_if", "name": "New Invoice Guard Passed?", "type": "n8n-nodes-base.if", "typeVersion": 2,
        "position": [1560, 460],
        "parameters": {"conditions": {"options": {"caseSensitive": True, "typeValidation": "loose"},
                        "conditions": [{"leftValue": "={{ $json.passed }}", "rightValue": True,
                                        "operator": {"type": "boolean", "operation": "true"}}],
                        "combinator": "and"}},
    },
    {
        "id": "respond_ok_new_invoice", "name": "Respond OK New Invoice", "type": "n8n-nodes-base.respondToWebhook",
        "typeVersion": 1.1, "position": [1820, 400],
        "parameters": {"respondWith": "json", "responseBody":
            "={{ JSON.stringify({ mode: 'category_only', verdict: $('New Invoice Narrate Diff').first().json.content[0].text, diff: $('New Invoice Diff').first().json, tax_evidence: $('New Invoice Tax Lookup').first().json, guard: 'passed', note: 'This invoice is not yet recorded in the system -- only the GST rate, TDS, and net-payable math were checked against current category tax rules. Base amount, 3-way match, and vendor eligibility could not be verified.' }) }}"},
    },
    {
        "id": "respond_fallback_new_invoice", "name": "Respond Fallback New Invoice", "type": "n8n-nodes-base.respondToWebhook",
        "typeVersion": 1.1, "position": [1820, 560],
        "parameters": {"respondWith": "json", "responseBody":
            "={{ (() => { const d = $('New Invoice Diff').first().json; const mism = d.fields.filter(f => !f.match && f.field !== 'base_amount').map(f => f.field + ': claimed ' + f.claimed + ', correct ' + f.correct); const numVerdict = mism.length === 0 ? 'Submitted GST/TDS math matches the current category tax rate.' : ('Divergence found: ' + mism.join('; ') + '.'); return JSON.stringify({ mode: 'category_only', verdict: numVerdict + ' [Showing the verified figures directly -- the written summary did not pass our accuracy check.]', diff: d, tax_evidence: $('New Invoice Tax Lookup').first().json, guard: 'failed_fallback_used', note: 'This invoice is not yet recorded in the system -- only the GST rate, TDS, and net-payable math were checked against current category tax rules. Base amount, 3-way match, and vendor eligibility could not be verified.' }); })() }}"},
    },
]

connections = {
    "UC2 Webhook": {"main": [[{"node": "Invoice ID Provided?", "type": "main", "index": 0}]]},
    "Invoice ID Provided?": {"main": [[{"node": "Retrieve Facts", "type": "main", "index": 0}],
                                       [{"node": "New Invoice Tax Lookup", "type": "main", "index": 0}]]},
    "Retrieve Facts": {"main": [[{"node": "Invoice Found?", "type": "main", "index": 0}]]},
    "Invoice Found?": {"main": [[{"node": "Tax Lookup", "type": "main", "index": 0}],
                                 [{"node": "Respond Not Found", "type": "main", "index": 0}]]},
    "Tax Lookup": {"main": [[{"node": "Compute", "type": "main", "index": 0}]]},
    "Compute": {"main": [[{"node": "Diff", "type": "main", "index": 0}]]},
    "Diff": {"main": [[{"node": "Narrate Diff", "type": "main", "index": 0}]]},
    "Narrate Diff": {"main": [[{"node": "Check Narration", "type": "main", "index": 0}]]},
    "Check Narration": {"main": [[{"node": "Guard Passed?", "type": "main", "index": 0}]]},
    "Guard Passed?": {"main": [[{"node": "Respond OK", "type": "main", "index": 0}],
                                [{"node": "Respond Fallback (templated)", "type": "main", "index": 0}]]},

    "New Invoice Tax Lookup": {"main": [[{"node": "New Invoice Compute", "type": "main", "index": 0}]]},
    "New Invoice Compute": {"main": [[{"node": "New Invoice Diff", "type": "main", "index": 0}]]},
    "New Invoice Diff": {"main": [[{"node": "New Invoice Narrate Diff", "type": "main", "index": 0}]]},
    "New Invoice Narrate Diff": {"main": [[{"node": "New Invoice Check Narration", "type": "main", "index": 0}]]},
    "New Invoice Check Narration": {"main": [[{"node": "New Invoice Guard Passed?", "type": "main", "index": 0}]]},
    "New Invoice Guard Passed?": {"main": [[{"node": "Respond OK New Invoice", "type": "main", "index": 0}],
                                            [{"node": "Respond Fallback New Invoice", "type": "main", "index": 0}]]},
}

workflow = {"name": "UC2 - Validation", "nodes": nodes, "connections": connections,
            "settings": {"executionOrder": "v1"}}

existing = requests.get(f"{N8N_BASE}/api/v1/workflows", headers=HEADERS, params={"limit": 50}).json()
for wf in existing.get("data", []):
    if wf["name"] == workflow["name"]:
        requests.delete(f"{N8N_BASE}/api/v1/workflows/{wf['id']}", headers=HEADERS)
        print(f"Deleted existing workflow {wf['id']}")

r = requests.post(f"{N8N_BASE}/api/v1/workflows", headers=HEADERS, json=workflow)
print(r.status_code)
wf_id = r.json().get("id")
if wf_id:
    r2 = requests.post(f"{N8N_BASE}/api/v1/workflows/{wf_id}/activate", headers=HEADERS)
    print("activated:", r2.status_code, wf_id)
else:
    print(r.text[:2000])
