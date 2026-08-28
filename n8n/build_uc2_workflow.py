#!/usr/bin/env python3
"""
Builds the UC2 (validation of a human-prepared payment advice) workflow.

Deliberately has NO intent-parsing LLM call and NO vendor-name resolution --
the input is already structured JSON (invoice_id + submitted_advice), per
the plan's UC2 input design (form/paste/upload, never LLM-parsed). The
retrieval -> tax-lookup -> compute chain runs identically to UC1 and is
never given the submitted_advice -- that's what makes this a genuine blind
reconstruction, not a review of the human's numbers.
"""
import os

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

N8N_BASE = os.environ["N8N_BASE_URL"]
N8N_KEY = os.environ["N8N_API_KEY"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
PG_CRED_ID = "RuLJlMwbZqaK22dc"
FASTAPI_BASE = "http://127.0.0.1:8123"
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
    "You are validating an accountant's payment advice against an independently-computed true result. Write a short, plain-language verdict (3-7 sentences): state clearly whether the advice's NUMBERS are CORRECT or have DIVERGENCES, and for every mismatched field state what was claimed, what's correct, and briefly why -- using ONLY the numbers and reasons in this JSON, never inventing or altering one. If overall_match is true, say so plainly and do not invent a divergence. SEPARATELY and REGARDLESS of whether the numbers match: if eligible is false, you MUST prominently warn that this payment is NOT clear to release and state every reason in eligibility_reasons -- a numerically-correct advice for an ineligible payment (blocked vendor, cancelled PO, failed 3-way match) is still not safe to pay, and this warning must never be omitted or softened. Diff result: " + JSON.stringify($json)
  }]
}) }}"""

CHECK_NARRATION_BODY_EXPR = """={{ (() => {
  const d = $('Diff').first().json;
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
    postgres_node("Retrieve Facts", RETRIEVE_FACTS_SQL, "retrieve_facts", 260, 300, always_output=True),
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
            "={{ (() => { const d = $('Diff').first().json; const mism = d.fields.filter(f => !f.match).map(f => f.field + ': claimed ' + f.claimed + ', correct ' + f.correct); const numVerdict = d.overall_match ? 'Advice matches the independently computed result.' : ('Advice diverges on: ' + mism.join('; ') + '.'); const eligVerdict = d.eligible ? '' : (' ⚠ NOT CLEAR TO PAY regardless of the numbers above: ' + d.eligibility_reasons.join('; ') + '.'); return JSON.stringify({ verdict: numVerdict + eligVerdict + ' [Narration guard rejected the AI-generated explanation as containing an unverified number; showing the computed diff directly.]', diff: d, tax_evidence: $('Tax Lookup').first().json, guard: 'failed_fallback_used' }); })() }}"},
    },
]

connections = {
    "UC2 Webhook": {"main": [[{"node": "Retrieve Facts", "type": "main", "index": 0}]]},
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
