#!/usr/bin/env python3
"""
Builds the UC1 (conversational lookup) workflow in the local n8n instance
via its REST API. Run once (or re-run to update — it upserts by name).

NOTE on secrets: this LOCAL dev instance embeds the Anthropic API key
directly in node parameters for speed of iteration. The Render deployment
must instead use n8n's credential system / environment variables, per the
plan's Stack & Hosting table -- flagged clearly, not treated as final.
"""
import json
import os

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

N8N_BASE = os.environ["N8N_BASE_URL"]
N8N_KEY = os.environ["N8N_API_KEY"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
PG_CRED_ID = "dfbUmJhCjrz5cw6G"  # from the credential created earlier
FASTAPI_BASE = "http://127.0.0.1:8123"

HEADERS = {"X-N8N-API-KEY": N8N_KEY, "Content-Type": "application/json"}

PARSE_INTENT_BODY_EXPR = """={{ JSON.stringify({
  model: "claude-sonnet-4-5-20250929",
  max_tokens: 300,
  tools: [{
    name: "parse_ap_question",
    description: "Parse an accounts-payable question into structured intent. If the question does not ask about a vendor balance or tax treatment, set intent to 'unsupported'.",
    input_schema: {
      type: "object",
      properties: {
        intent: {type: "string", enum: ["balance_lookup","tax_lookup","combined_lookup","unsupported"]},
        vendor_name_mentioned: {type: "string", description: "The vendor name as mentioned in the question, verbatim, or empty string if none."},
        invoice_id_mentioned: {type: ["integer","null"], description: "If the question references a specific invoice by number (e.g. 'INV-9', 'invoice 17', 'invoice #12'), extract JUST the numeric id as an integer (e.g. 9, 17, 12). If no specific invoice is referenced, use null -- do not guess one."}
      },
      required: ["intent","vendor_name_mentioned","invoice_id_mentioned"]
    }
  }],
  tool_choice: {type:"tool", name:"parse_ap_question"},
  messages: [{role:"user", content: $json.body.question}]
}) }}"""

RESOLVE_VENDOR_SQL = """SELECT vendor_id, legal_name, similarity(legal_name, '{{ $json.content.find(c => c.type === "tool_use").input.vendor_name_mentioned.replace(/'/g, "''") }}') AS score
FROM vendor_master
WHERE similarity(legal_name, '{{ $json.content.find(c => c.type === "tool_use").input.vendor_name_mentioned.replace(/'/g, "''") }}') > 0.15
ORDER BY score DESC
LIMIT 5;"""

RETRIEVE_FACTS_SQL_TEMPLATE = """SELECT
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
WHERE i.vendor_id = __VENDOR_ID__ __INVOICE_FILTER__
__ORDER_CLAUSE__
LIMIT 1;"""

# Built as a JS IIFE rather than a static template: if the parsed question
# named a specific invoice, filter to exactly that one (scoped to the
# resolved vendor, so a mismatched invoice number for a DIFFERENT vendor
# correctly returns zero rows rather than silently picking a wrong one);
# otherwise fall back to that vendor's most recent invoice.
RETRIEVE_FACTS_SQL_EXPR = ("={{ (() => {\n"
    "  const parsed = $('Parse Intent').first().json.content.find(c => c.type === 'tool_use').input;\n"
    "  const vendorId = $('Resolve Vendor').first().json.vendor_id;\n"
    "  const invFilter = (parsed.invoice_id_mentioned !== null && parsed.invoice_id_mentioned !== undefined)\n"
    "    ? ('AND i.invoice_id = ' + parseInt(parsed.invoice_id_mentioned))\n"
    "    : '';\n"
    "  const orderClause = invFilter ? '' : 'ORDER BY i.invoice_date DESC';\n"
    f"  const template = `{RETRIEVE_FACTS_SQL_TEMPLATE}`;\n"
    "  return template.replace('__VENDOR_ID__', vendorId).replace('__INVOICE_FILTER__', invFilter).replace('__ORDER_CLAUSE__', orderClause);\n"
    "})() }}")

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

NARRATE_BODY_EXPR = """={{ JSON.stringify({
  model: "claude-sonnet-4-5-20250929",
  max_tokens: 400,
  messages: [{role:"user", content:
    "Write a short (2-4 sentence), plain-language answer to an accounts-payable question, using ONLY the numbers in this JSON -- never invent or alter a figure. Vendor: " + $('Retrieve Facts').first().json.legal_name + ". Onboarding state on file: " + $('Retrieve Facts').first().json.onboarding_state + " (vs. GSTIN-registered state used for tax: " + $('Retrieve Facts').first().json.registered_state + " -- mention this discrepancy briefly if they differ). Structured result: " + JSON.stringify($json)
  }]
}) }}"""

CHECK_NARRATION_BODY_EXPR = """={{ (() => {
  const c = $('Compute').first().json;
  const nums = [c.base_amount, c.gross_liability, c.net_disbursement_due, c.tds_amount, c.pre_tax_ledger_position];
  if (c.gst) { nums.push(c.gst.gst_amount, c.gst.cgst, c.gst.sgst, c.gst.igst); }
  return JSON.stringify({
    narrative_text: $json.content[0].text,
    structured_values: nums.filter(n => n !== null && n !== undefined)
  });
})() }}"""


def http_node(name, url, body_expr, node_id, x, y, extra_headers=None):
    headers = extra_headers or []
    return {
        "id": node_id, "name": name, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
        "position": [x, y],
        "parameters": {
            "method": "POST", "url": url,
            "sendHeaders": True,
            "headerParameters": {"parameters": headers},
            "sendBody": True, "specifyBody": "json", "jsonBody": body_expr,
            "options": {},
        },
    }


def postgres_node(name, sql_expr, node_id, x, y):
    return {
        "id": node_id, "name": name, "type": "n8n-nodes-base.postgres", "typeVersion": 2.6,
        "position": [x, y],
        "parameters": {"operation": "executeQuery", "query": "=" + sql_expr, "options": {}},
        "credentials": {"postgres": {"id": PG_CRED_ID, "name": "AP Postgres (Render)"}},
    }


anthropic_headers = [
    {"name": "x-api-key", "value": ANTHROPIC_KEY},
    {"name": "anthropic-version", "value": "2023-06-01"},
    {"name": "content-type", "value": "application/json"},
]

nodes = [
    {
        "id": "webhook", "name": "UC1 Webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 300],
        "parameters": {"httpMethod": "POST", "path": "uc1-ask", "responseMode": "responseNode", "options": {}},
        "webhookId": "uc1-ask",
    },
    http_node("Parse Intent", "https://api.anthropic.com/v1/messages", PARSE_INTENT_BODY_EXPR,
              "parse_intent", 260, 300, anthropic_headers),
    postgres_node("Resolve Vendor", RESOLVE_VENDOR_SQL, "resolve_vendor", 520, 300),
    postgres_node("Retrieve Facts", RETRIEVE_FACTS_SQL_EXPR[1:], "retrieve_facts", 780, 300),  # [1:] strips the leading '=' postgres_node() re-adds
    http_node("Tax Lookup", f"{FASTAPI_BASE}/tax-lookup", TAX_LOOKUP_BODY_EXPR, "tax_lookup", 1040, 300),
    http_node("Compute", f"{FASTAPI_BASE}/compute", COMPUTE_BODY_EXPR, "compute", 1300, 300),
    http_node("Narrate", "https://api.anthropic.com/v1/messages", NARRATE_BODY_EXPR,
              "narrate", 1560, 300, anthropic_headers),
    http_node("Check Narration", f"{FASTAPI_BASE}/check-narration", CHECK_NARRATION_BODY_EXPR,
              "check_narration", 1820, 300),
    {
        "id": "guard_if", "name": "Guard Passed?", "type": "n8n-nodes-base.if", "typeVersion": 2,
        "position": [2080, 300],
        "parameters": {"conditions": {"options": {"caseSensitive": True, "typeValidation": "loose"},
                        "conditions": [{"leftValue": "={{ $json.passed }}", "rightValue": True,
                                        "operator": {"type": "boolean", "operation": "true"}}],
                        "combinator": "and"}},
    },
    {
        "id": "respond_ok", "name": "Respond OK", "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.1,
        "position": [2340, 200],
        "parameters": {"respondWith": "json", "responseBody":
            "={{ JSON.stringify({ narrative: $('Narrate').first().json.content[0].text, evidence: $('Compute').first().json, tax_evidence: $('Tax Lookup').first().json, guard: 'passed' }) }}"},
    },
    {
        "id": "respond_fallback", "name": "Respond Fallback (templated)", "type": "n8n-nodes-base.respondToWebhook",
        "typeVersion": 1.1, "position": [2340, 420],
        "parameters": {"respondWith": "json", "responseBody":
            "={{ (() => { const c = $('Compute').first().json; return JSON.stringify({ narrative: 'Net disbursement due: ' + c.net_disbursement_due + ' (eligibility: ' + c.eligibility + '). [Narration guard rejected the AI-generated explanation as containing an unverified number; showing the computed result directly.]', evidence: c, tax_evidence: $('Tax Lookup').first().json, guard: 'failed_fallback_used' }); })() }}"},
    },
]

connections = {
    "UC1 Webhook": {"main": [[{"node": "Parse Intent", "type": "main", "index": 0}]]},
    "Parse Intent": {"main": [[{"node": "Resolve Vendor", "type": "main", "index": 0}]]},
    "Resolve Vendor": {"main": [[{"node": "Retrieve Facts", "type": "main", "index": 0}]]},
    "Retrieve Facts": {"main": [[{"node": "Tax Lookup", "type": "main", "index": 0}]]},
    "Tax Lookup": {"main": [[{"node": "Compute", "type": "main", "index": 0}]]},
    "Compute": {"main": [[{"node": "Narrate", "type": "main", "index": 0}]]},
    "Narrate": {"main": [[{"node": "Check Narration", "type": "main", "index": 0}]]},
    "Check Narration": {"main": [[{"node": "Guard Passed?", "type": "main", "index": 0}]]},
    "Guard Passed?": {"main": [[{"node": "Respond OK", "type": "main", "index": 0}],
                                [{"node": "Respond Fallback (templated)", "type": "main", "index": 0}]]},
}

workflow = {"name": "UC1 - Conversational Lookup", "nodes": nodes, "connections": connections,
            "settings": {"executionOrder": "v1"}}

# Upsert: delete any existing workflow with the same name first
existing = requests.get(f"{N8N_BASE}/api/v1/workflows", headers=HEADERS, params={"limit": 50}).json()
for wf in existing.get("data", []):
    if wf["name"] == workflow["name"]:
        requests.delete(f"{N8N_BASE}/api/v1/workflows/{wf['id']}", headers=HEADERS)
        print(f"Deleted existing workflow {wf['id']}")

r = requests.post(f"{N8N_BASE}/api/v1/workflows", headers=HEADERS, json=workflow)
print(r.status_code)
print(json.dumps(r.json(), indent=2)[:3000])
