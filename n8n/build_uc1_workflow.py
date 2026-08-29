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
# Both overridable so this same script builds either the local dev workflow
# or the deployed one, without hardcoding one target's values over the
# other's. Defaults are the local dev instance's values.
PG_CRED_ID = os.environ.get("N8N_PG_CRED_ID", "RuLJlMwbZqaK22dc")
FASTAPI_BASE = os.environ.get("FASTAPI_BASE_URL", "http://127.0.0.1:8123")

HEADERS = {"X-N8N-API-KEY": N8N_KEY, "Content-Type": "application/json"}

PARSE_INTENT_BODY_EXPR = """={{ JSON.stringify({
  model: "claude-sonnet-4-5-20250929",
  max_tokens: 300,
  temperature: 0,
  tools: [{
    name: "parse_ap_question",
    description: "Parse the LATEST user question (the last item in messages) into structured intent. If earlier turns are present in messages, use them ONLY to resolve a pronoun or implicit reference in the latest question ('them', 'that vendor', 'the same invoice') -- never to re-answer a question the user isn't currently asking, and never to keep a fact 'active' once the conversation has moved on to something unrelated. If the latest question does not ask about a vendor balance, tax treatment, or general vendor information, set intent to 'unsupported'.",
    input_schema: {
      type: "object",
      properties: {
        intent: {type: "string", enum: ["balance_lookup","tax_lookup","combined_lookup","vendor_lookup","unsupported"], description: "'vendor_lookup' is for general vendor information that ISN'T a specific balance or tax calculation -- e.g. 'what state is this vendor registered in', 'what other invoices does this vendor have', 'give me this vendor's details', 'is this vendor active or blocked'. Use 'balance_lookup'/'tax_lookup'/'combined_lookup' only when the question is actually asking to compute or state a monetary/tax figure for a specific invoice or transaction."},
        vendor_name_mentioned: {type: "string", description: "The vendor the LATEST question is about. If it names a vendor, use that name verbatim. If it instead refers back to a vendor via a pronoun or implicit reference ('them', 'that vendor', 'the same company') and exactly one vendor was discussed in the immediately preceding turn(s), use that vendor's name as it appeared earlier. If the latest question is a fresh question that doesn't reference any vendor (e.g. a category-only question, or a change of topic), leave this empty even if a different vendor was named earlier -- do not default to the last-mentioned vendor just because one exists in the history."},
        invoice_id_mentioned: {type: ["integer","null"], description: "If the latest question references a specific invoice by number (e.g. 'INV-9', 'invoice 17', 'invoice #12'), extract JUST the numeric id as an integer (e.g. 9, 17, 12). If it instead unambiguously refers back to a specific invoice discussed in the immediately preceding turn (e.g. 'is that invoice correctly taxed?' right after INV-17 was discussed), use that invoice's id. Do NOT carry an invoice id forward from earlier in the conversation once the topic has moved on -- if the latest question names a different vendor, or doesn't itself imply continuity with a specific invoice, use null. Never guess one."},
        category_mentioned: {type: ["string","null"], enum: ["Furniture","Software","Services","Food","Appliances",null], description: "If the latest question is about a purchase category in general (no specific vendor named or resolved from history), which of these fixed categories it refers to. Null if a specific vendor was named (or resolved from history) instead, or if no category is identifiable in the latest question itself."},
        explicit_date_mentioned: {type: ["string","null"], description: "If the question states ANY date reference -- a full date ('1 September 2025' -> 2025-09-01), or just a bare year ('in 2010', 'during 2018') -> use January 1 of that year (2010-01-01, 2018-01-01). Null ONLY if no date or year is mentioned at all -- do not default to today yourself, that is handled downstream."}
      },
      required: ["intent","vendor_name_mentioned","invoice_id_mentioned","category_mentioned","explicit_date_mentioned"]
    }
  }],
  tool_choice: {type:"tool", name:"parse_ap_question"},
  messages: ($json.body.history || []).map(h => ({role: h.role, content: h.content})).concat([{role:"user", content: $json.body.question}])
}) }}"""

# Switched from symmetric similarity() to word_similarity() after Day-5
# testing surfaced a real grounding failure AND, when a naive threshold-raise
# was tried as the fix, a regression: symmetric similarity penalizes a short
# genuine reference ("TechNova") against a long legal name so heavily that it
# scores BELOW a coincidental same-length false match ("Zenith Global
# Traders" vs "...Traders", sharing only one generic word). word_similarity
# is asymmetric -- it asks "does the query match well against SOME part of
# the name", which is the actually-correct question for a short company
# nickname. Empirically: genuine matches (incl. short forms and the
# deliberate "Acme" ambiguity case) score 0.9-1.0; the coincidental
# "Traders" overlap tops out at 0.36. Threshold of 0.4 sits cleanly between.
RESOLVE_VENDOR_SQL = """SELECT vendor_id, legal_name, word_similarity('{{ $json.content.find(c => c.type === "tool_use").input.vendor_name_mentioned.replace(/'/g, "''") }}', legal_name) AS score
FROM vendor_master
WHERE word_similarity('{{ $json.content.find(c => c.type === "tool_use").input.vendor_name_mentioned.replace(/'/g, "''") }}', legal_name) > 0.4
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
  COALESCE((SELECT SUM(amount) FROM payment WHERE invoice_id = i.invoice_id), 0) AS payments_made,
  (SELECT COUNT(*) FROM invoice WHERE vendor_id = v.vendor_id AND status = 'open') AS vendor_open_invoice_count
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

# --- Vendor-lookup path: general vendor info (state, status, other
# invoices), NOT a balance/tax calculation -- no /tax-lookup, no /compute
# call at all, this is a pure data retrieval + narration. json_agg bundles
# the invoice list into one row from one query, matching this file's
# existing style (RETRIEVE_FACTS_SQL_TEMPLATE's COALESCE subqueries) rather
# than adding a second Postgres node for a one-to-many list.
RETRIEVE_VENDOR_DETAILS_SQL_TEMPLATE = """SELECT
  v.legal_name, v.gstin, v.registered_state, v.pan, v.payment_terms, v.status AS vendor_status,
  vo.submitted_state AS onboarding_state, vo.onboarding_status, TO_CHAR(vo.onboarding_date, 'YYYY-MM-DD') AS onboarding_date,
  (SELECT json_agg(json_build_object(
      'invoice_id', i.invoice_id, 'category', c.category_name, 'base_amount', i.base_amount,
      'invoice_date', TO_CHAR(i.invoice_date, 'YYYY-MM-DD'), 'status', i.status
    ) ORDER BY i.invoice_date DESC)
   FROM invoice i JOIN category c ON i.category_id = c.category_id
   WHERE i.vendor_id = v.vendor_id) AS invoices
FROM vendor_master v
JOIN vendor_onboarding vo ON v.vendor_id = vo.vendor_id
WHERE v.vendor_id = __VENDOR_ID__;"""

RETRIEVE_VENDOR_DETAILS_SQL_EXPR = ("={{ (() => {\n"
    "  const vendorId = $('Resolve Vendor').first().json.vendor_id;\n"
    f"  const template = `{RETRIEVE_VENDOR_DETAILS_SQL_TEMPLATE}`;\n"
    "  return template.replace('__VENDOR_ID__', vendorId);\n"
    "})() }}")

NARRATE_VENDOR_DETAILS_BODY_EXPR = """={{ JSON.stringify({
  model: "claude-sonnet-4-5-20250929",
  max_tokens: 400,
  messages: [{role:"user", content:
    "Write a short (2-4 sentence), plain-language answer to a general question about a vendor (not a balance/tax calculation), using ONLY the facts in this JSON -- never invent or alter a figure, and never compute a total. If the vendor's onboarding state differs from its GSTIN-registered state, mention this briefly as a data-quality note (GSTIN is the authoritative one for tax purposes, but both are worth surfacing here). If the vendor has multiple invoices, summarize them briefly (count, and category/status pattern) rather than listing every field of every one -- the evidence panel already shows the full list. Vendor details: " + JSON.stringify($json)
  }]
}) }}"""

CHECK_NARRATION_VENDOR_BODY_EXPR = """={{ (() => {
  const v = $('Retrieve Vendor Details').first().json;
  const invoices = v.invoices || [];
  // Both base_amount AND invoice_id, plus the count -- a natural summary
  // ("3 invoices, the largest being INV-19 at Rs 1,30,000") mentions all
  // three kinds of number, not just currency figures.
  const nums = [];
  invoices.forEach(inv => { nums.push(Number(inv.base_amount)); nums.push(Number(inv.invoice_id)); });
  nums.push(invoices.length);
  // payment_terms (e.g. "Net 30") can embed a legitimate number the
  // narration is likely to repeat -- found live: the guard rejected a
  // narration that said "Net 30 payment terms" because "30" wasn't in
  // structured_values. Extract digits from it directly rather than
  // guessing which other fields might someday contain a number too.
  const termsMatch = (v.payment_terms || "").match(/\\d+/g);
  if (termsMatch) { termsMatch.forEach(n => nums.push(Number(n))); }
  return JSON.stringify({
    narrative_text: $json.content[0].text,
    structured_values: nums
  });
})() }}"""

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

NARRATE_BODY_EXPR = """={{ (() => {
  const f = $('Retrieve Facts').first().json;
  const parsed = $('Parse Intent').first().json.content.find(c => c.type === 'tool_use').input;
  // Found live: asking a vendor-balance question with no invoice named
  // silently returned just the most-recent invoice's figures, with nothing
  // telling the user other open invoices exist -- risked being mistaken
  // for the vendor's total. Disclose explicitly whenever that ambiguity is
  // real (no invoice named AND more than one open invoice exists).
  const ambiguous = !parsed.invoice_id_mentioned && Number(f.vendor_open_invoice_count) > 1;
  const ambiguityNote = ambiguous
    ? (" This vendor has " + f.vendor_open_invoice_count + " open invoices in total, and no specific invoice was named in the question -- this data covers only invoice #" + f.invoice_id + " (the most recent one). You MUST say so explicitly and briefly, so this is never mistaken for the vendor's total balance across all invoices.")
    : "";
  return JSON.stringify({
    model: "claude-sonnet-4-5-20250929",
    max_tokens: 400,
    messages: [{role:"user", content:
      "Write a short (2-4 sentence), plain-language answer to an accounts-payable question, using ONLY the numbers in this JSON -- never invent or alter a figure. Vendor: " + f.legal_name + ". Onboarding state on file: " + f.onboarding_state + " (vs. GSTIN-registered state used for tax: " + f.registered_state + " -- mention this discrepancy briefly if they differ)." + ambiguityNote + " Structured result: " + JSON.stringify($json)
    }]
  });
})() }}"""

CHECK_NARRATION_BODY_EXPR = """={{ (() => {
  const c = $('Compute').first().json;
  const f = $('Retrieve Facts').first().json;
  const nums = [c.base_amount, c.gross_liability, c.net_disbursement_due, c.tds_amount, c.pre_tax_ledger_position,
                Number(f.invoice_id), Number(f.vendor_open_invoice_count)];
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


def postgres_node(name, sql_expr, node_id, x, y, always_output=False):
    node = {
        "id": node_id, "name": name, "type": "n8n-nodes-base.postgres", "typeVersion": 2.6,
        "position": [x, y],
        "parameters": {"operation": "executeQuery", "query": "=" + sql_expr, "options": {}},
        "credentials": {"postgres": {"id": PG_CRED_ID, "name": "AP Postgres (Render)"}},
    }
    if always_output:
        node["alwaysOutputData"] = True
    return node


def if_node(name, node_id, x, y, left_expr, right_value, op_type, op_name):
    return {
        "id": node_id, "name": name, "type": "n8n-nodes-base.if", "typeVersion": 2,
        "position": [x, y],
        "parameters": {"conditions": {"options": {"caseSensitive": True, "typeValidation": "loose"},
                        "conditions": [{"leftValue": left_expr, "rightValue": right_value,
                                        "operator": {"type": op_type, "operation": op_name}}],
                        "combinator": "and"}},
    }


def respond_node(name, node_id, x, y, body_expr):
    return {
        "id": node_id, "name": name, "type": "n8n-nodes-base.respondToWebhook", "typeVersion": 1.1,
        "position": [x, y],
        "parameters": {"respondWith": "json", "responseBody": body_expr},
    }


# --- Category-only ("Hypothetical Path") lookup: for questions with no
# vendor named -- e.g. "what GST rate applies to software purchases?" ---
CATEGORY_TAX_LOOKUP_BODY_EXPR = """={{ (() => {
  const parsed = $('Parse Intent').first().json.content.find(c => c.type === 'tool_use').input;
  const usedToday = !parsed.explicit_date_mentioned;
  const dateToUse = parsed.explicit_date_mentioned || new Date().toISOString().slice(0,10);
  const HSN_SAC_BY_CATEGORY = {Furniture: '9403', Software: '998313', Services: '998311', Food: '996331', Appliances: '8516'};
  return JSON.stringify({
    category: parsed.category_mentioned,
    hsn_or_sac: HSN_SAC_BY_CATEGORY[parsed.category_mentioned] || '',
    invoice_date: dateToUse,
    vendor_state: 'Karnataka',
    office_state: 'Karnataka',
    needs_tds: true,
    _used_today_fallback: usedToday,
    _date_used: dateToUse
  });
})() }}"""
# NOTE: vendor_state/office_state are placeholders (both Karnataka) purely so
# /tax-lookup's split-type calculation runs -- the split (CGST+SGST vs IGST)
# is NOT meaningful for a vendor-less question and is deliberately excluded
# from the narration prompt below; only the rate and its source are used.

NARRATE_HYPOTHETICAL_BODY_EXPR = """={{ (() => {
  const parsed = $('Parse Intent').first().json.content.find(c => c.type === 'tool_use').input;
  const tax = $json;
  const usedToday = !parsed.explicit_date_mentioned;
  const dateNote = usedToday
    ? ('No date was stated in the question, so today\\'s date (' + new Date().toISOString().slice(0,10) + ') was assumed -- say this explicitly.')
    : ('The question specified the date ' + parsed.explicit_date_mentioned + ' -- use that date, do not mention an assumption.');
  return JSON.stringify({
    model: "claude-sonnet-4-5-20250929",
    max_tokens: 300,
    messages: [{role:"user", content:
      "Write a short (2-3 sentence) plain-language answer to a general (non-vendor-specific) tax-rate question, using ONLY the rate and figures in this JSON -- never invent one. This is a category-level rate, not tied to any specific transaction, so do NOT state a CGST/SGST/IGST split (that depends on a specific vendor and office, which aren't known here). " + dateNote + " Category: " + parsed.category_mentioned + ". Tax lookup result: " + JSON.stringify(tax)
    }]
  });
})() }}"""

CHECK_NARRATION_HYPOTHETICAL_BODY_EXPR = """={{ (() => {
  const t = $('Category Tax Lookup').first().json;
  const nums = [];
  if (t.gst && t.gst.rate_pct !== null && t.gst.rate_pct !== undefined) nums.push(t.gst.rate_pct);
  if (t.tds && t.tds.rate_pct !== null && t.tds.rate_pct !== undefined) nums.push(t.tds.rate_pct);
  return JSON.stringify({ narrative_text: $json.content[0].text, structured_values: nums });
})() }}"""


# Reads from the n8n process's own environment at runtime, rather than
# embedding the literal key in the workflow definition -- so the exported
# workflow JSON (committed to GitHub) never contains a real secret, and the
# actual key only ever lives in Render's environment-variable store.
anthropic_headers = [
    {"name": "x-api-key", "value": "={{ $env.ANTHROPIC_API_KEY }}"},
    {"name": "anthropic-version", "value": "2023-06-01"},
    {"name": "content-type", "value": "application/json"},
]

nodes = [
    {
        "id": "webhook", "name": "UC1 Webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2,
        "position": [0, 400],
        "parameters": {"httpMethod": "POST", "path": "uc1-ask", "responseMode": "responseNode", "options": {}},
        "webhookId": "uc1-ask",
    },
    http_node("Parse Intent", "https://api.anthropic.com/v1/messages", PARSE_INTENT_BODY_EXPR,
              "parse_intent", 260, 400, anthropic_headers),
    if_node("Intent Supported?", "intent_supported_if", 400, 400,
            "={{ $json.content.find(c => c.type === 'tool_use').input.intent }}",
            "unsupported", "string", "notEquals"),
    respond_node("Respond Unsupported", "respond_unsupported", 520, 620,
        "={{ JSON.stringify({ error: 'unsupported', message: 'This assistant only answers questions about vendor balances, tax treatment, and general vendor information in the accounts payable system -- it can\\'t help with that.' }) }}"),
    postgres_node("Resolve Vendor", RESOLVE_VENDOR_SQL, "resolve_vendor", 660, 400, always_output=True),
    # Ambiguity gate, BEFORE "Vendor Found?": RESOLVE_VENDOR_SQL deliberately
    # returns up to 5 candidates (see its comment -- the seeded "Acme" pair
    # scores 1.0/1.0), but every downstream node keys off .first(), which
    # used to mean an ambiguous name silently answered with whichever
    # candidate happened to sort first, with the other never mentioned.
    # A raw ">1 row" check was tried first and rejected after live testing:
    # a full/multi-word query like "Acme Traders" legitimately has a single
    # correct match (score 1.0) but ALSO clears the 0.4 threshold against an
    # unrelated vendor purely on a shared generic word ("...Traders", score
    # 0.615) -- that's a weak coincidental second row, not a genuine tie, and
    # flagging it as ambiguous would be a new false-refusal regression.
    # Comparing the gap between the top two scores separates this correctly:
    # the real "Acme"/"Acme" tie has a gap of 0; every other real query
    # tested (full legal names, "Acme Traders", "Techno Sofware Solutionz")
    # has a gap >= 0.29. 0.2 sits cleanly between, with margin either way.
    if_node("Vendor Ambiguous?", "vendor_ambiguous_if", 720, 520,
            "={{ (() => { const rows = $('Resolve Vendor').all(); "
            "if (rows.length < 2) return 1; "
            "return rows[0].json.score - rows[1].json.score; })() }}",
            0.2, "number", "lt"),
    respond_node("Respond Vendor Ambiguous", "respond_vendor_ambiguous", 900, 620,
        "={{ (() => { const names = $('Resolve Vendor').all().map(item => item.json.legal_name); "
        "const asked = $('Parse Intent').first().json.content.find(c => c.type === 'tool_use').input.vendor_name_mentioned; "
        "return JSON.stringify({ error: 'ambiguous_vendor', message: '\"' + asked + '\" matches multiple vendors on file: ' + "
        "names.join(', ') + '. Please specify which one you mean.', candidates: names }); })() }}"),
    if_node("Vendor Found?", "vendor_found_if", 780, 400,
            "={{ $json.vendor_id }}", "", "string", "notEmpty"),
    if_node("Is Vendor Lookup?", "vendor_lookup_if", 900, 260,
            "={{ $('Parse Intent').first().json.content.find(c => c.type === 'tool_use').input.intent }}",
            "vendor_lookup", "string", "equals"),

    # ---- Branch A1: vendor was resolved AND it's a general vendor-info
    # question -- pure retrieval + narration, no tax/compute involved at all ----
    postgres_node("Retrieve Vendor Details", RETRIEVE_VENDOR_DETAILS_SQL_EXPR[1:], "retrieve_vendor_details", 1040, 60),
    http_node("Narrate Vendor Details", "https://api.anthropic.com/v1/messages", NARRATE_VENDOR_DETAILS_BODY_EXPR,
              "narrate_vendor_details", 1300, 60, anthropic_headers),
    http_node("Check Narration Vendor", f"{FASTAPI_BASE}/check-narration", CHECK_NARRATION_VENDOR_BODY_EXPR,
              "check_narration_vendor", 1560, 60),
    if_node("Guard Passed Vendor?", "guard_if_vendor", 1820, 60, "={{ $json.passed }}", True, "boolean", "true"),
    respond_node("Respond OK Vendor", "respond_ok_vendor", 2080, 0,
        "={{ JSON.stringify({ narrative: $('Narrate Vendor Details').first().json.content[0].text, evidence: $('Retrieve Vendor Details').first().json, guard: 'passed', note: 'general vendor information, not a balance/tax calculation' }) }}"),
    respond_node("Respond Fallback Vendor", "respond_fallback_vendor", 2080, 160,
        "={{ (() => { const v = $('Retrieve Vendor Details').first().json; return JSON.stringify({ narrative: v.legal_name + ' -- status: ' + v.vendor_status + ', ' + (v.invoices ? v.invoices.length : 0) + ' invoice(s) on file. [Narration guard rejected the AI-generated explanation as containing an unverified number; showing the retrieved data directly.]', evidence: v, guard: 'failed_fallback_used' }); })() }}"),

    # ---- Branch A2: vendor was resolved AND it's a balance/tax question -- full transactional path ----
    postgres_node("Retrieve Facts", RETRIEVE_FACTS_SQL_EXPR[1:], "retrieve_facts", 1040, 260),
    http_node("Tax Lookup", f"{FASTAPI_BASE}/tax-lookup", TAX_LOOKUP_BODY_EXPR, "tax_lookup", 1300, 260),
    http_node("Compute", f"{FASTAPI_BASE}/compute", COMPUTE_BODY_EXPR, "compute", 1560, 260),
    http_node("Narrate", "https://api.anthropic.com/v1/messages", NARRATE_BODY_EXPR,
              "narrate", 1820, 260, anthropic_headers),
    http_node("Check Narration", f"{FASTAPI_BASE}/check-narration", CHECK_NARRATION_BODY_EXPR,
              "check_narration", 2080, 260),
    if_node("Guard Passed?", "guard_if", 2340, 260, "={{ $json.passed }}", True, "boolean", "true"),
    respond_node("Respond OK", "respond_ok", 2600, 160,
        "={{ JSON.stringify({ narrative: $('Narrate').first().json.content[0].text, evidence: $('Compute').first().json, tax_evidence: $('Tax Lookup').first().json, guard: 'passed' }) }}"),
    respond_node("Respond Fallback (templated)", "respond_fallback", 2600, 360,
        "={{ (() => { const c = $('Compute').first().json; return JSON.stringify({ narrative: 'Net disbursement due: ' + c.net_disbursement_due + ' (eligibility: ' + c.eligibility + '). [Narration guard rejected the AI-generated explanation as containing an unverified number; showing the computed result directly.]', evidence: c, tax_evidence: $('Tax Lookup').first().json, guard: 'failed_fallback_used' }); })() }}"),

    # ---- Branch B: no vendor resolved -> category-only or decline ----
    if_node("Category Mentioned?", "category_if", 1040, 560,
            "={{ $('Parse Intent').first().json.content.find(c => c.type === 'tool_use').input.category_mentioned }}",
            "", "string", "notEmpty"),
    http_node("Category Tax Lookup", f"{FASTAPI_BASE}/tax-lookup", CATEGORY_TAX_LOOKUP_BODY_EXPR,
              "category_tax_lookup", 1300, 500, []),
    http_node("Narrate Hypothetical", "https://api.anthropic.com/v1/messages", NARRATE_HYPOTHETICAL_BODY_EXPR,
              "narrate_hypothetical", 1560, 500, anthropic_headers),
    http_node("Check Narration H", f"{FASTAPI_BASE}/check-narration", CHECK_NARRATION_HYPOTHETICAL_BODY_EXPR,
              "check_narration_h", 1820, 500, []),
    if_node("Guard Passed H?", "guard_if_h", 2080, 500, "={{ $json.passed }}", True, "boolean", "true"),
    respond_node("Respond OK Hypothetical", "respond_ok_h", 2340, 460,
        "={{ JSON.stringify({ narrative: $('Narrate Hypothetical').first().json.content[0].text, evidence: $('Category Tax Lookup').first().json, guard: 'passed', note: 'category-level answer, not tied to a specific vendor or transaction' }) }}"),
    respond_node("Respond Fallback H", "respond_fallback_h", 2340, 620,
        "={{ (() => { const t = $('Category Tax Lookup').first().json; return JSON.stringify({ narrative: 'Applicable GST rate: ' + (t.gst ? t.gst.rate_pct : 'unknown') + '%. [Narration guard rejected the AI-generated explanation as containing an unverified number; showing the computed result directly.]', evidence: t, guard: 'failed_fallback_used' }); })() }}"),
    respond_node("Respond Not Found", "respond_not_found", 1300, 700,
        "={{ JSON.stringify({ error: 'not_found', message: 'No vendor matching \\'' + $('Parse Intent').first().json.content.find(c => c.type === 'tool_use').input.vendor_name_mentioned + '\\' was found, and no purchase category was identifiable either -- cannot answer without guessing.' }) }}"),
]

connections = {
    "UC1 Webhook": {"main": [[{"node": "Parse Intent", "type": "main", "index": 0}]]},
    "Parse Intent": {"main": [[{"node": "Intent Supported?", "type": "main", "index": 0}]]},
    "Intent Supported?": {"main": [[{"node": "Resolve Vendor", "type": "main", "index": 0}],
                                    [{"node": "Respond Unsupported", "type": "main", "index": 0}]]},
    "Resolve Vendor": {"main": [[{"node": "Vendor Ambiguous?", "type": "main", "index": 0}]]},
    "Vendor Ambiguous?": {"main": [[{"node": "Respond Vendor Ambiguous", "type": "main", "index": 0}],
                                    [{"node": "Vendor Found?", "type": "main", "index": 0}]]},
    "Vendor Found?": {"main": [[{"node": "Is Vendor Lookup?", "type": "main", "index": 0}],
                                [{"node": "Category Mentioned?", "type": "main", "index": 0}]]},
    "Is Vendor Lookup?": {"main": [[{"node": "Retrieve Vendor Details", "type": "main", "index": 0}],
                                    [{"node": "Retrieve Facts", "type": "main", "index": 0}]]},

    "Retrieve Vendor Details": {"main": [[{"node": "Narrate Vendor Details", "type": "main", "index": 0}]]},
    "Narrate Vendor Details": {"main": [[{"node": "Check Narration Vendor", "type": "main", "index": 0}]]},
    "Check Narration Vendor": {"main": [[{"node": "Guard Passed Vendor?", "type": "main", "index": 0}]]},
    "Guard Passed Vendor?": {"main": [[{"node": "Respond OK Vendor", "type": "main", "index": 0}],
                                       [{"node": "Respond Fallback Vendor", "type": "main", "index": 0}]]},

    "Retrieve Facts": {"main": [[{"node": "Tax Lookup", "type": "main", "index": 0}]]},
    "Tax Lookup": {"main": [[{"node": "Compute", "type": "main", "index": 0}]]},
    "Compute": {"main": [[{"node": "Narrate", "type": "main", "index": 0}]]},
    "Narrate": {"main": [[{"node": "Check Narration", "type": "main", "index": 0}]]},
    "Check Narration": {"main": [[{"node": "Guard Passed?", "type": "main", "index": 0}]]},
    "Guard Passed?": {"main": [[{"node": "Respond OK", "type": "main", "index": 0}],
                                [{"node": "Respond Fallback (templated)", "type": "main", "index": 0}]]},

    "Category Mentioned?": {"main": [[{"node": "Category Tax Lookup", "type": "main", "index": 0}],
                                      [{"node": "Respond Not Found", "type": "main", "index": 0}]]},
    "Category Tax Lookup": {"main": [[{"node": "Narrate Hypothetical", "type": "main", "index": 0}]]},
    "Narrate Hypothetical": {"main": [[{"node": "Check Narration H", "type": "main", "index": 0}]]},
    "Check Narration H": {"main": [[{"node": "Guard Passed H?", "type": "main", "index": 0}]]},
    "Guard Passed H?": {"main": [[{"node": "Respond OK Hypothetical", "type": "main", "index": 0}],
                                  [{"node": "Respond Fallback H", "type": "main", "index": 0}]]},
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

# Activate -- a webhook's production URL only responds once its workflow is
# active (test URLs work either way, which is how this went unnoticed
# locally). UC2's build script already does this; this brings UC1 in line.
if r.status_code in (200, 201):
    wf_id = r.json()["id"]
    r2 = requests.post(f"{N8N_BASE}/api/v1/workflows/{wf_id}/activate", headers=HEADERS)
    print("activated:", r2.status_code, wf_id)
