"""
Streamlit frontend for the Accounts Payable Intelligence Agent.

This is a thin client: all reasoning happens in n8n + the FastAPI service.
This app only renders the chat, the UC2 input surface, and a read-only view
of the mock data -- it never computes anything itself.
"""
import csv
import io
import json
import os
import re
from typing import Optional

import pandas as pd
import psycopg2
import psycopg2.extras
import requests
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

N8N_BASE = os.environ.get("N8N_BASE_URL", "http://localhost:5678")
DATABASE_URL = os.environ["DATABASE_URL"]

# Last N user+assistant exchanges sent to Parse Intent as context, so a
# follow-up like "how much do we owe them?" can resolve "them" against the
# vendor named a turn or two earlier. Capped rather than sending the whole
# conversation -- reference resolution essentially never needs more than
# 1-2 turns back, and a fixed cap bounds token cost regardless of how long
# the conversation grows.
HISTORY_TURNS = 3

st.set_page_config(page_title="AP Intelligence Agent", page_icon="📒", layout="wide")

# ---------------------------------------------------------------------------
# Global styling. Targets Streamlit's data-testid attributes directly, which
# is why streamlit is version-pinned in requirements.txt -- these attribute
# names have changed across major Streamlit releases before. Verified
# against the actual rendered DOM of this deployment (not guessed) before
# writing these selectors.
# ---------------------------------------------------------------------------
st.markdown("""
<style>
/* layout="wide" removes Streamlit's own max-width; re-cap it here so
   content reads as a centered column on a wide viewer instead of running
   edge-to-edge. Widened back to 1200px (from an earlier, narrower 1000px
   pass) at the user's direct request -- seeing it live, the narrower cap
   read as wasting real estate rather than looking intentional. */
[data-testid="stMainBlockContainer"] {
    max-width: 1200px;
    margin: 0 auto;
}

/* Header + tabs centered within that column -- an earlier pass left these
   left-aligned on the reasoning that centering short text/labels usually
   looks worse than a centered block of left-aligned content; the user saw
   it live and still read it as too left-heavy, which overrides that
   secondhand reasoning. [role="tablist"] is Baseweb's own ARIA attribute
   (not a Streamlit data-testid), used here because it's the stable handle
   on the actual flex row of tab buttons inside stTabs. */
[data-testid="stHeading"], [data-testid="stCaptionContainer"] {
    text-align: center;
}
[data-testid="stTabs"] [role="tablist"] {
    justify-content: center;
}

/* Currency/ID figures read as a ledger, not chat prose */
[data-testid="stMetricValue"] {
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}

/* Elevation against the off-white page background (see config.toml) --
   strong enough to actually read as "raised," not just a hairline.
   stBaseButton-secondary (the suggestion chips) is deliberately NOT in
   this group -- see its own lighter "pill chip" rule further down;
   confirmed via grep that every other button in this app is type="primary"
   (styled separately by Streamlit's theme), so this selector only ever
   matches the 4 suggestion buttons and is safe to restyle on its own. */
[data-testid="stMetric"], [data-testid="stAlertContainer"],
[data-testid="stExpander"], [data-testid="stTableStyledTable"] {
    box-shadow: 0 1px 4px rgba(0,0,0,0.08), 0 2px 8px rgba(0,0,0,0.05);
    border: 1px solid #e4e7e7 !important;
}
[data-testid="stExpander"] {
    border-radius: 8px;
}

/* Suggestion chips: light pill treatment, not a card. The earlier shared
   card-shadow style read as "a row of form buttons," not example prompts
   -- no shadow, rounder, thinner border, sized to content rather than
   stretched to a fixed grid width. */
[data-testid="stBaseButton-secondary"] {
    border-radius: 999px !important;
    border: 1px solid #d8dede !important;
    box-shadow: none !important;
    background-color: #fbfcfc !important;
}
[data-testid="stBaseButton-secondary"]:hover {
    border-color: #3d6b6f !important;
    background-color: #eef4f4 !important;
}

/* The chat input is the primary interaction surface for a conversational
   agent -- it needs real visual weight: taller, an accent-tinted fill (not
   just a border) so it unmistakably reads as "the" input rather than "an"
   input among several controls. Pinned to the bottom of the scrollable
   conversation area (position: sticky) so it's always reachable without
   scrolling, the same way ChatGPT/Claude.ai/any real chat product anchors
   its composer -- verified stMain (not the window) is the actual
   overflow:auto container here before relying on sticky positioning
   working at all. A small margin-top keeps it from sitting flush against
   the last message once stuck. */
[data-testid="stChatInput"] {
    background-color: #eef4f4;
    box-shadow: 0 2px 12px rgba(61,107,111,0.16), 0 1px 3px rgba(0,0,0,0.06);
    border: 2px solid #3d6b6f;
    border-radius: 12px;
    min-height: 60px;
    position: sticky;
    bottom: 0;
    z-index: 10;
    margin-top: 12px;
}
[data-testid="stChatInputTextArea"] {
    font-size: 1.15rem;
    padding-top: 16px;
    padding-bottom: 16px;
    background-color: transparent;
    /* Explicit cap, not just min-height on the wrapper -- older Streamlit
       versions (confirmed on 1.50.0, not present on the deployed 1.62.0)
       auto-expand this textarea to ~260px with no cap otherwise. */
    max-height: 60px !important;
}
/* Double-border fix: stChatInput renders a middle, un-testid'd wrapper div
   between the outer container above and the textarea below. That wrapper
   carries Streamlit's OWN native 1px border plus a native :focus-within
   rule pointing at theme.colors.primary -- which config.toml pins to the
   identical #3d6b6f used above, so on focus (including right after Enter,
   since Streamlit's JS refocuses the textarea on submit) both borders
   render at once, reading as a doubled outline. No data-testid exists on
   that div, so this targets it structurally (direct child). Verified
   against the installed 1.50.0 bundle; re-check against the deployed
   1.62.0 in all three states (unfocused/focused/post-Enter) once live. */
[data-testid="stChatInput"] > div,
[data-testid="stChatInput"] > div:focus-within {
    border: none !important;
    box-shadow: none !important;
    outline: none !important;
}

/* Restrained, harmonized alert palette (this app pins base="light" in
   .streamlit/config.toml, so these are calibrated for a light background) */
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentSuccess"]) {
    background-color: #e8f3ea; border: 1px solid #b8dcc0;
}
[data-testid="stAlertContentSuccess"] { color: #1e6b33 !important; }

[data-testid="stAlertContainer"]:has([data-testid="stAlertContentError"]) {
    background-color: #fbeaea; border: 1px solid #eec3c3;
}
[data-testid="stAlertContentError"] { color: #a12020 !important; }

[data-testid="stAlertContainer"]:has([data-testid="stAlertContentInfo"]) {
    background-color: #eaf1f4; border: 1px solid #c5dbe3;
}
[data-testid="stAlertContentInfo"] { color: #2a5a70 !important; }

/* Warning = the "hold / not eligible" family -- deliberately its own color,
   distinct from error red. A numeric mismatch (error) and a payment-
   eligibility hold (warning) are different questions and must never be
   visually confusable with each other -- see render_evidence() and the
   eligibility block in the UC2 tab below. */
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentWarning"]) {
    background-color: #fbf0d9; border: 1px solid #e8c873; border-left: 4px solid #b8860b;
}
[data-testid="stAlertContentWarning"] { color: #7a5200 !important; font-weight: 600; }

/* Muted secondary text instead of near-full-opacity */
[data-testid="stCaptionContainer"] { opacity: 0.72; }

/* Bordered metric cards instead of borderless floating labels */
[data-testid="stMetric"] {
    background-color: #f4f6f6; border: 1px solid #e0e4e4; border-radius: 6px; padding: 12px 16px;
}
</style>
""", unsafe_allow_html=True)

st.title("Accounts Payable Intelligence Agent")
st.caption(
    "A working prototype reasoning across a Procurement Portal, an SAP-style Vendor & Payments DB, "
    "and synthetic tax/regulatory documents. Every number shown traces to a source record or a quoted "
    "regulatory clause — see the Evidence panel under each answer."
)

tab_ask, tab_validate, tab_browse = st.tabs(
    ["Ask a Question", "Validate a Payment Advice", "Browse Mock Data"]
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def render_evidence(evidence: dict, tax_evidence: Optional[dict] = None):
    if "invoices" in evidence:
        # Vendor-lookup response (general vendor info, e.g. "which state is
        # this vendor in", "what other invoices do they have") -- NOT a
        # balance/tax calculation, so this renders as a profile + invoice
        # list rather than forcing it through the money-metric layout below,
        # which would show misleading blank placeholders for fields (GST,
        # TDS, net disbursement) that simply don't apply to this response.
        c1, c2 = st.columns(2)
        c1.write(f"**GSTIN:** {evidence.get('gstin', '—')}")
        c1.write(f"**Registered state (GSTIN):** {evidence.get('registered_state', '—')}")
        c1.write(f"**Payment terms:** {evidence.get('payment_terms', '—')}")
        c2.write(f"**Status:** {evidence.get('vendor_status', '—')}")
        c2.write(f"**Onboarding state (portal):** {evidence.get('onboarding_state', '—')}")
        c2.write(f"**Onboarded:** {evidence.get('onboarding_date', '—')} ({evidence.get('onboarding_status', '—')})")
        if (evidence.get("registered_state") and evidence.get("onboarding_state")
                and evidence["registered_state"] != evidence["onboarding_state"]):
            st.warning(f"⚠️ Onboarding state ({evidence['onboarding_state']}) differs from the GSTIN-registered "
                       f"state ({evidence['registered_state']}) — GSTIN is authoritative for tax purposes; "
                       f"see the authority-conflict rule this system applies everywhere else.")
        invoices = evidence.get("invoices") or []
        st.write(f"**Invoices on file ({len(invoices)}):**")
        if invoices:
            st.dataframe(invoices, use_container_width=True, hide_index=True)

    elif evidence.get("tax_treatment_refused"):
        st.warning(
            f"**Tax treatment on hold — {evidence.get('eligibility', '')}**\n\n"
            f"Pre-tax ledger position (base − advances − credits − payments): "
            f"₹{evidence.get('pre_tax_ledger_position'):,.2f}"
        )
        cc = evidence.get("category_conflict")
        if cc:
            st.write(f"⚠️ PO says **{cc['po_category']}**, invoice says **{cc['invoice_category']}** — "
                     f"no rule to resolve which is correct, so tax treatment is withheld pending review.")
    else:
        gst = evidence.get("gst") or {}
        c1, c2 = st.columns(2)
        c3, c4 = st.columns(2)
        c1.metric("Base amount", f"₹{evidence.get('base_amount', 0):,.2f}")
        c2.metric("GST", f"₹{gst.get('gst_amount', 0):,.2f}" if gst else "—")
        c3.metric("TDS withheld", f"₹{evidence.get('tds_amount', 0):,.2f}" if evidence.get("tds_amount") is not None else "—")
        c4.metric("Net disbursement due", f"₹{evidence.get('net_disbursement_due', 0):,.2f}")

        if gst.get("split_type") == "IGST":
            st.caption(f"GST split: IGST ₹{gst.get('igst', 0):,.2f} (inter-state — vendor and office are in different states)")
        elif gst:
            st.caption(f"GST split: CGST ₹{gst.get('cgst', 0):,.2f} + SGST ₹{gst.get('sgst', 0):,.2f} (intra-state)")

        st.write(f"**Payment eligibility:** {evidence.get('eligibility')}")
        if evidence.get("unapplied_advance_advisory"):
            st.info(f"⚠️ Advisory: an unapplied advance of ₹{evidence['unapplied_advance_advisory']:,.2f} "
                     f"exists against this vendor/PO and has NOT been netted here — a possible overpayment risk if missed.")
        twm = evidence.get("three_way_match")
        if twm and not twm.get("matched"):
            st.error(f"⚠️ 3-way match FAILED: PO ₹{twm['po_amount']:,.2f} / receipt ₹{twm['receipt_amount']:,.2f} "
                      f"/ invoice ₹{twm['invoice_base_amount']:,.2f} — difference exceeds tolerance (₹{twm['tolerance']:,.2f}).")

    if tax_evidence:
        with st.expander("Tax document sources (exact clause quoted)"):
            for label, key in [("GST", "gst"), ("TDS", "tds")]:
                sub = tax_evidence.get(key)
                if not sub:
                    continue
                if sub.get("status") == "found":
                    st.markdown(f"**{label}** — *{sub['source_filename']}*")
                    st.markdown(f"> {sub['key_clause']}")
                elif sub.get("status") == "not_found":
                    st.markdown(f"**{label}** — no applicable rule found in the corpus.")

    with st.expander("Full structured evidence (raw)"):
        st.json(evidence)


# ---------------------------------------------------------------------------
# TAB 1 — UC1: Ask a question
# ---------------------------------------------------------------------------

with tab_ask:
    suggestions = [
        "How much is pending for TechNova Software Solutions on invoice INV-9?",
        "Is TechNova invoice 17 correctly taxed?",
        "What tax treatment applies to Skyline Software Labs' latest invoice?",
        "What's the outstanding balance for Zenith Global Traders?",
    ]
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "pending_question" not in st.session_state:
        st.session_state.pending_question = None

    def _use_suggestion(s):
        st.session_state.pending_question = s

    # Suggestions only before a conversation starts -- once there's history,
    # they're just clutter to scroll past, and hiding them shortens the
    # distance between the latest answer and the input (see sticky-input
    # note below). Matches how every real chat product treats starter
    # prompts (e.g. ChatGPT's disappear after the first message).
    #
    # Uses on_click (a real Streamlit callback), not an inline `if
    # button(...)` check -- callbacks run and update session_state BEFORE
    # the script reruns and re-renders from the top, so this condition
    # already sees pending_question set on the very same click, hiding
    # suggestions immediately rather than one interaction later. (An
    # earlier attempt just checked pending_question inline without a
    # callback -- that doesn't work, since the button's own assignment
    # only happens *after* this condition already rendered it, on the
    # same pass.) The one case this still can't cover: typing a question
    # directly into chat_input as literally the first message -- that
    # value isn't known until st.chat_input() itself is called, further
    # down. Harmless and self-correcting from the second question on.
    if not st.session_state.messages and not st.session_state.pending_question:
        st.write("**Try one of these, or type your own question below:**")
        cols = st.columns(len(suggestions))
        for i, s in enumerate(suggestions):
            cols[i].button(s, key=f"sugg_{i}", use_container_width=True,
                            on_click=_use_suggestion, args=(s,))

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])
            if msg.get("evidence"):
                render_evidence(msg["evidence"], msg.get("tax_evidence"))

    if st.session_state.messages:
        # Auto-scroll to the newest message on every rerun -- the sticky
        # input (below) only stays visible once you've *already* scrolled
        # near it; without this, a conversation taller than the viewport
        # still needs a manual scroll down after every answer. components.
        # html runs in its own same-origin iframe, so window.parent.document
        # is how it reaches the actual app DOM -- a standard, documented
        # pattern for this exact "scroll a Streamlit chat to bottom" case.
        # height=0 keeps the iframe itself invisible.
        components.html("""
            <script>
            setTimeout(function() {
                const mainEl = window.parent.document.querySelector('[data-testid="stMain"]');
                if (mainEl) { mainEl.scrollTop = mainEl.scrollHeight; }
            }, 60);
            </script>
        """, height=0)

    # Input last in the DOM, pinned to the bottom of the scroll area via
    # CSS (position: sticky -- see the shared style block). This replaces
    # an earlier "input first" placement: that made sense for the empty
    # landing state, but once a real conversation exists it meant scrolling
    # up to type after every answer, then down again to read the next one
    # -- exactly the friction every real chat product (ChatGPT, Claude.ai,
    # WhatsApp) avoids by keeping the composer anchored while the
    # conversation scrolls, not the other way around. Confirmed via the
    # live DOM before relying on it: stMain (not the window) is the actual
    # overflow:auto scroll container here, and nothing between it and this
    # input has an overflow/position that would break sticky positioning.
    typed = st.chat_input("Ask about a vendor balance or tax treatment...")

    question = typed or st.session_state.pending_question
    st.session_state.pending_question = None

    if question:
        # Deliberately NOT rendered live/inline here (no st.chat_message
        # calls in this block) -- this code runs *after* chat_input above,
        # so anything drawn here would land below the sticky input rather
        # than above it, defeating the whole point of pinning it. Instead:
        # process silently, append both turns to session_state, then
        # st.rerun() so the history loop above (which now includes this
        # turn) draws everything in the correct order on the very next
        # pass, with chat_input rendered fresh (and empty) after it again.
        st.session_state.messages.append({"role": "user", "content": question})
        with st.spinner("Looking this up across all three sources..."):
            try:
                # Prior turns only -- exclude the question just appended
                # above. role/content ONLY, never the evidence/tax_evidence
                # JSON blobs: the narrative prose already states prior
                # figures in plain language, and sending the raw structured
                # evidence would balloon token cost for no resolution
                # benefit (Parse Intent only needs to know *what* was asked
                # and answered, not re-derive numbers from it).
                prior_turns = st.session_state.messages[:-1]
                history = [{"role": m["role"], "content": m["content"]}
                           for m in prior_turns[-(HISTORY_TURNS * 2):]]
                resp = requests.post(f"{N8N_BASE}/webhook/uc1-ask",
                                      json={"question": question, "history": history}, timeout=90)
                data = resp.json()
                if "narrative" not in data and data.get("message"):
                    # Graceful-refusal paths (nonexistent vendor, out-of-
                    # domain question) return {error, message}, not
                    # {narrative, evidence} -- surface that message
                    # directly instead of falling through to a blank
                    # "(no answer returned)".
                    data = {"narrative": data["message"], "evidence": {}}
            except Exception as e:
                data = {"narrative": f"Something went wrong reaching the agent: {e}", "evidence": {}}
        st.session_state.messages.append({
            "role": "assistant", "content": data.get("narrative", "(no answer returned)"),
            "evidence": data.get("evidence"), "tax_evidence": data.get("tax_evidence"),
        })
        st.rerun()


# ---------------------------------------------------------------------------
# TAB 2 — UC2: Validate a payment advice
# ---------------------------------------------------------------------------

# A real payment advice comes out of an accounting system as a spreadsheet/
# CSV export -- nobody hand-writes JSON. This is a deterministic, structured
# parse (same guarantee as the JSON path: no LLM, no OCR, nothing that could
# misread a digit), just of a different structured format. Shared by both
# the "Upload a file" and "Paste JSON or CSV row" modes below so the parsing
# logic lives in exactly one place.
_ADVICE_NUMERIC_FIELDS = ("base_amount", "gst_rate_pct", "gst_cgst", "gst_sgst",
                          "gst_igst", "tds_amount", "net_payable_claimed")

# Same fixed category enum the n8n side uses (build_uc1_workflow.py's
# category_mentioned tool field, build_uc2_workflow.py's HSN/SAC map) --
# a dropdown here instead of free text avoids a typo silently producing a
# false "category doesn't match" or "rate not found" result downstream.
CATEGORY_OPTIONS = ["Furniture", "Software", "Services", "Food", "Appliances"]


@st.cache_data(ttl=60)
def _load_invoice_options():
    """Real invoices, for the Manual Form's invoice picker -- so validating
    an existing invoice never requires knowing or typing an ID up front, or
    tab-hopping to Browse Mock Data to find one. Same columns as that tab's
    Invoices table. Cached briefly since this is read-only reference data
    re-queried on every widget interaction otherwise."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT i.invoice_id, v.legal_name AS vendor, c.category_name AS category,
               i.base_amount, i.status
        FROM invoice i
        JOIN vendor_master v ON i.vendor_id = v.vendor_id
        JOIN category c ON i.category_id = c.category_id
        ORDER BY i.invoice_id;
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def _parse_csv_row(text: str) -> dict:
    """Parses a one-row CSV (header + one data row, e.g. pasted straight out
    of Excel) into the {invoice_id, submitted_advice: {...}} shape every
    other input mode already produces. Only populates submitted_advice keys
    that are actually present and non-empty in the row, so e.g. a CGST+SGST
    row doesn't send an empty gst_igst downstream. invoice_id is optional --
    an omitted or empty column is a draft advice for an invoice that isn't
    recorded in the system yet, same as the Manual Form's "No" toggle."""
    reader = csv.DictReader(io.StringIO(text.strip()))
    row = next(reader)
    advice = {}
    for key, val in row.items():
        if key == "invoice_id" or val is None or val.strip() == "":
            continue
        advice[key] = float(val) if key in _ADVICE_NUMERIC_FIELDS else val
    result = {"submitted_advice": advice}
    if row.get("invoice_id"):
        result["invoice_id"] = int(row["invoice_id"])
    return result


FIXTURES = {
    "✅ Correct advice (INV-1)": {
        "invoice_id": 1,
        "submitted_advice": {
            "base_amount": 85000.00, "category": "Furniture",
            "gst_rate_pct": 18.0, "gst_igst": 15300.00,
            "tds_amount": 0.00, "net_payable_claimed": 100300.00,
        },
    },
    "❌ Multi-error advice (INV-4)": {
        "invoice_id": 4,
        "submitted_advice": {
            "base_amount": 61000.00, "category": "Appliances",
            "gst_rate_pct": 12.0, "gst_cgst": 3660.00, "gst_sgst": 3660.00,
            "tds_amount": 6100.00, "net_payable_claimed": 62220.00,
        },
    },
    "⚠️ Advice matching vendor's outdated rate (INV-17)": {
        "invoice_id": 17,
        "submitted_advice": {
            "base_amount": 110000.00, "category": "Software",
            "gst_rate_pct": 18.0, "gst_cgst": 9900.00, "gst_sgst": 9900.00,
            "tds_amount": 11000.00, "net_payable_claimed": 118800.00,
        },
    },
    "🚫 Correct numbers, but vendor is blocked (INV-24)": {
        "invoice_id": 24,
        "submitted_advice": {
            "base_amount": 30000.00, "category": "Services",
            "gst_rate_pct": 18.0, "gst_cgst": 2700.00, "gst_sgst": 2700.00,
            "tds_amount": 3000.00, "net_payable_claimed": 32400.00,
        },
    },
    # No invoice_id at all -- this is the OTHER real UC2 scenario: a draft
    # advice for something not yet entered anywhere. Deliberately uses a
    # stale/incorrect rate (12% instead of the actual current 18% for
    # Furniture) so this example demonstrates the rate-check catching a
    # real error, not just a clean pass.
    "📝 Draft advice for a NEW invoice (not yet recorded)": {
        "submitted_advice": {
            "base_amount": 15000.00, "category": "Furniture",
            "gst_rate_pct": 12.0, "gst_cgst": 1000.00, "gst_sgst": 1000.00,
            "tds_amount": 0.00, "net_payable_claimed": 17000.00,
        },
    },
}

with tab_validate:
    st.write("An accountant's payment advice is uploaded, pasted, or entered — the agent independently "
             "reconstructs the correct figure first, *then* compares.")

    mode = st.radio("Input method", ["Use an example", "Manual form", "Paste JSON or CSV row", "Upload a file"], horizontal=True)

    payload = None

    if mode == "Use an example":
        choice = st.selectbox("Pick a scenario", list(FIXTURES.keys()), index=None,
                               placeholder="Choose a scenario to validate...")
        if choice is None:
            st.caption("Five scenarios to try: a clean match, multiple errors, a vendor's own outdated "
                       "rate, a blocked vendor, and a draft advice for an invoice not yet in the system.")
        else:
            st.json(FIXTURES[choice])
            if st.button("Validate this advice", type="primary"):
                payload = FIXTURES[choice]

    elif mode == "Manual form":
        invoice_kind = st.radio(
            "Is this invoice already recorded in the system?",
            ["Yes — validate against the full record", "No — this is a draft, not yet recorded"],
            horizontal=True,
        )
        is_existing = invoice_kind.startswith("Yes")

        inv_options = {}
        if is_existing:
            try:
                invoices = _load_invoice_options()
            except Exception as e:
                # Same discipline as Browse Mock Data: never echo a raw DB
                # exception (it can contain a misconfigured secret) into a
                # public-facing message.
                print(f"[uc2-validate-picker] Could not load invoice list: {e}")
                invoices = []
                st.warning("Could not load the list of existing invoices right now — try Paste/Upload "
                           "instead, or switch to \"this is a draft, not yet recorded\" below.")
            inv_options = {
                f"INV-{r['invoice_id']} · {r['vendor']} · {r['category']} · "
                f"₹{r['base_amount']:,.0f} · {r['status']}": r["invoice_id"]
                for r in invoices
            }
            # Deliberately OUTSIDE the form below: st.form only reruns the
            # script on submit, not on interior widget changes, so a radio
            # INSIDE a form can't swap which fields are visible before the
            # user submits -- confirmed live (selecting IGST left the CGST/
            # SGST boxes on screen; the real IGST box only appeared on the
            # next run, after submission had already captured the old
            # branch's values). Reading it here instead makes the field
            # swap immediate.
            split = st.radio("GST split", ["CGST + SGST (intra-state)", "IGST (inter-state)"])
            is_igst = split.startswith("IGST")

        with st.form("manual_advice_form"):
            picked_label = None
            invoice_date = None
            if is_existing:
                picked_label = st.selectbox(
                    "Which invoice?", list(inv_options.keys()), index=None,
                    placeholder="Search or select an invoice...",
                )
            else:
                st.caption("⚠️ **Reduced check** — this invoice has no record yet, so only the GST/TDS "
                           "**rate** math can be verified against current category tax rules. Base amount "
                           "is taken as given, and 3-way match / vendor eligibility can't be checked.")
                invoice_date = st.date_input("Invoice date (optional — defaults to today)", value=None)

            category = st.selectbox("Category", CATEGORY_OPTIONS, index=None,
                                     placeholder="Select a category...")
            base_amount = st.number_input("Base amount (₹)", min_value=0.0, step=100.0,
                                           value=None, placeholder="0.00")
            gst_rate_pct = st.number_input(
                "GST rate (%)", min_value=0.0, step=0.5, value=None, placeholder="e.g. 18",
                help="Enter as a whole percentage — e.g. 18 for 18%, not 0.18.",
            )

            gst_cgst = gst_sgst = gst_igst = gst_total = None
            if is_existing:
                if is_igst:
                    gst_igst = st.number_input("IGST (₹)", min_value=0.0, step=10.0,
                                                value=None, placeholder="0.00")
                else:
                    gst_cgst = st.number_input("CGST (₹)", min_value=0.0, step=10.0,
                                                value=None, placeholder="0.00")
                    gst_sgst = st.number_input("SGST (₹)", min_value=0.0, step=10.0,
                                                value=None, placeholder="0.00")
            else:
                gst_total = st.number_input(
                    "GST amount (₹)", min_value=0.0, step=10.0, value=None, placeholder="0.00",
                    help="Combined CGST+SGST or IGST, whichever you have — only the RATE this implies "
                         "is checked, not which split applies (that needs a real vendor/office pairing, "
                         "which doesn't exist yet for an unrecorded invoice).",
                )

            tds_amount = st.number_input("TDS amount (₹)", min_value=0.0, step=10.0,
                                          value=None, placeholder="0.00")
            net_payable_claimed = st.number_input("Net payable claimed (₹)", min_value=0.0, step=10.0,
                                                   value=None, placeholder="0.00")
            submitted = st.form_submit_button("Validate this advice", type="primary")

        if submitted:
            missing = []
            if is_existing and picked_label is None:
                missing.append("invoice")
            if category is None:
                missing.append("category")
            if base_amount is None:
                missing.append("base amount")
            if gst_rate_pct is None:
                missing.append("GST rate")
            if is_existing and gst_cgst is None and gst_igst is None:
                missing.append("GST amount (CGST+SGST or IGST)")
            if not is_existing and gst_total is None:
                missing.append("GST amount")
            if tds_amount is None:
                missing.append("TDS amount")
            if net_payable_claimed is None:
                missing.append("net payable claimed")

            if missing:
                st.error("Missing required field(s): " + ", ".join(missing) + ".")
            else:
                advice = {"base_amount": base_amount, "category": category, "gst_rate_pct": gst_rate_pct,
                          "tds_amount": tds_amount, "net_payable_claimed": net_payable_claimed}
                if is_existing:
                    if gst_igst is not None:
                        advice["gst_igst"] = gst_igst
                    else:
                        advice["gst_cgst"] = gst_cgst
                        advice["gst_sgst"] = gst_sgst
                    payload = {"invoice_id": inv_options[picked_label], "submitted_advice": advice}
                else:
                    # No split choice was offered (see the help text above) --
                    # divide evenly, well within the ±₹1 diff tolerance of
                    # compute.py's own even CGST/SGST split, so the RATE
                    # check this mode actually promises stays accurate.
                    advice["gst_cgst"] = gst_total / 2
                    advice["gst_sgst"] = gst_total / 2
                    payload = {"submitted_advice": advice}
                    if invoice_date:
                        payload["invoice_date"] = invoice_date.isoformat()

    elif mode == "Paste JSON or CSV row":
        example_csv = "invoice_id,base_amount,category,gst_rate_pct,gst_cgst,gst_sgst,tds_amount,net_payable_claimed\n17,110000,Software,18,9900,9900,11000,118800"
        pasted = st.text_area(
            "Paste JSON matching {invoice_id, submitted_advice: {...}}, or a CSV header + one data row "
            "(e.g. copied straight out of a spreadsheet). Omit invoice_id entirely for a draft advice on "
            "an invoice that isn't recorded yet — only the tax rate will be checked in that case.",
            height=200,
            placeholder=f"{json.dumps(FIXTURES['✅ Correct advice (INV-1)'], indent=2)}\n\n--- or ---\n\n{example_csv}",
        )
        if st.button("Validate this advice", type="primary"):
            stripped = pasted.strip()
            try:
                # Auto-detect rather than making the user pick a sub-mode --
                # JSON always starts with '{'; anything else is treated as a
                # CSV header + data row (the shape a spreadsheet paste takes).
                if stripped.startswith("{"):
                    payload = json.loads(stripped)
                else:
                    payload = _parse_csv_row(stripped)
            except json.JSONDecodeError as e:
                st.error(f"That's not valid JSON: {e}")
            except (ValueError, StopIteration) as e:
                st.error(f"Couldn't parse that as JSON or CSV: {e}")

    elif mode == "Upload a file":
        uploaded = st.file_uploader("Upload a .json or .csv file", type=["json", "csv"])
        st.caption("An invoice_id column/key is only required if this invoice already has a record — "
                   "omit it for a draft advice on something not yet entered anywhere.")
        if uploaded and st.button("Validate this advice", type="primary"):
            try:
                if uploaded.name.lower().endswith(".csv"):
                    payload = _parse_csv_row(uploaded.getvalue().decode("utf-8"))
                else:
                    payload = json.load(uploaded)
            except json.JSONDecodeError as e:
                st.error(f"That's not valid JSON: {e}")
            except (ValueError, StopIteration) as e:
                st.error(f"Couldn't parse that CSV: {e}")

    if payload:
        if "submitted_advice" not in payload:
            st.error("Missing required field: 'submitted_advice'.")
        else:
            with st.spinner("Independently reconstructing the correct figure, then comparing..."):
                try:
                    resp = requests.post(f"{N8N_BASE}/webhook/uc2-validate", json=payload, timeout=90)
                    result = resp.json()
                except Exception as e:
                    result = {"error": "request_failed", "message": str(e)}

            if result.get("error") == "not_found":
                st.error(result["message"])
                try:
                    sample = _load_invoice_options()[:6]
                except Exception:
                    sample = []
                if sample:
                    st.caption("Existing invoices you can validate against: " +
                               ", ".join(f"INV-{r['invoice_id']} ({r['vendor']})" for r in sample))
                st.caption("Validating a draft for something not in the system yet? Use Manual Form → "
                           "\"this is a draft, not yet recorded\", or omit invoice_id from JSON/CSV.")
            elif result.get("error"):
                st.error(f"Something went wrong: {result.get('message', result)}")
            else:
                diff = result.get("diff", {})
                is_category_only = result.get("mode") == "category_only"

                if is_category_only:
                    st.info("📝 " + result.get("note", "This invoice is not yet recorded in the system — "
                            "reduced check: only the tax rate was verified."))

                if diff.get("blocked"):
                    st.warning(diff.get("blocked_reason"))
                elif diff.get("overall_match"):
                    st.success("✅ " + result.get("verdict", "Advice matches the independently computed result."))
                else:
                    st.error("❌ Divergence found")
                    st.write(result.get("verdict", ""))

                # Deliberately separate from the numeric verdict above: whether
                # the numbers are correct is a different question from whether
                # this payment is actually clear to release. st.warning (not
                # st.error) is used specifically so this gets its own color
                # family via the CSS above -- a numeric mismatch and an
                # eligibility hold must never be visually confusable, even
                # when (as here) the numbers are otherwise completely correct.
                # Never shown for category_only -- eligibility genuinely
                # cannot be assessed without a real vendor/PO/invoice record,
                # so it's omitted entirely rather than defaulting to a claim
                # ("eligible: true") that was never actually verified.
                if not is_category_only and not diff.get("eligible", True):
                    st.divider()
                    st.warning("🚫 **NOT CLEAR TO PAY** — regardless of whether the numbers above match:\n\n"
                              + "\n".join(f"- {r}" for r in diff.get("eligibility_reasons", [])))

                fields = diff.get("fields", [])
                if is_category_only:
                    # Collapse cgst+sgst into one "GST amount" row -- the
                    # split itself was never checked (see the note above,
                    # and the Manual Form's "GST amount" help text) -- and
                    # drop base_amount entirely: it was never independently
                    # verified, just the accountant's own figure fed back
                    # to itself, so showing it as a "✅ match" row would be
                    # misleading rather than informative.
                    cgst_f = next((f for f in fields if f["field"] == "gst_cgst"), None)
                    sgst_f = next((f for f in fields if f["field"] == "gst_sgst"), None)
                    fields = [f for f in fields if f["field"] not in ("base_amount", "gst_cgst", "gst_sgst")]
                    if cgst_f and sgst_f:
                        both_match = cgst_f["match"] and sgst_f["match"]
                        fields.insert(0, {
                            "field": "gst_amount",
                            "claimed": (cgst_f["claimed"] or 0) + (sgst_f["claimed"] or 0),
                            "correct": (cgst_f["correct"] or 0) + (sgst_f["correct"] or 0),
                            "match": both_match,
                            "reason": "" if both_match else "GST amount does not match the applicable rate for this category and date.",
                        })

                if fields:
                    st.write("**Field-by-field comparison:**")
                    rows = []
                    for f in fields:
                        rows.append({
                            "Field": f["field"],
                            "Submitted": f["claimed"],
                            "Correct": f["correct"],
                            "Match?": "✅" if f["match"] else "❌",
                            "Why (if mismatched)": f["reason"],
                        })
                    df = pd.DataFrame(rows)

                    def _match_style(val):
                        if val == "✅":
                            return "background-color: #e8f3ea; font-weight: 600;"
                        if val == "❌":
                            return "background-color: #fbeaea; font-weight: 600;"
                        return ""

                    # st.table (not st.dataframe) deliberately -- st.dataframe
                    # renders to a <canvas> grid with no per-cell DOM, so CSS
                    # and this Styler-based formatting can't reach it at all.
                    # st.table renders real HTML, so both work.
                    styled = (
                        df.style
                        .map(_match_style, subset=["Match?"])
                        .set_properties(subset=["Submitted", "Correct"], **{
                            "font-family": "ui-monospace, SFMono-Regular, monospace",
                            "text-align": "right",
                        })
                    )
                    st.table(styled)

                if result.get("tax_evidence"):
                    with st.expander("Tax document sources for this reconstruction"):
                        for label, key in [("GST", "gst"), ("TDS", "tds")]:
                            sub = result["tax_evidence"].get(key)
                            if sub and sub.get("status") == "found":
                                st.markdown(f"**{label}** — *{sub['source_filename']}*")
                                st.markdown(f"> {sub['key_clause']}")

                with st.expander("Full raw response"):
                    st.json(result)


# ---------------------------------------------------------------------------
# TAB 3 — Browse the mock data
# ---------------------------------------------------------------------------

with tab_browse:
    st.write("Read-only view of the seeded mock data — a reviewer can see what's in the system "
             "before asking a question, without needing to inspect the source repo.")
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        st.subheader("Vendors")
        cur.execute("""
            SELECT v.vendor_id, v.legal_name, v.registered_state, v.gstin, v.payment_terms, v.status
            FROM vendor_master v ORDER BY v.vendor_id;
        """)
        st.dataframe([dict(r) for r in cur.fetchall()], use_container_width=True, hide_index=True)

        st.subheader("Invoices")
        cur.execute("""
            SELECT i.invoice_id, v.legal_name AS vendor, c.category_name AS category,
                   i.invoice_date, i.base_amount, i.status
            FROM invoice i
            JOIN vendor_master v ON i.vendor_id = v.vendor_id
            JOIN category c ON i.category_id = c.category_id
            ORDER BY i.invoice_date DESC;
        """)
        st.dataframe([dict(r) for r in cur.fetchall()], use_container_width=True, hide_index=True)

        st.subheader("Offices")
        cur.execute("SELECT office_id, name, city, state FROM office ORDER BY office_id;")
        st.dataframe([dict(r) for r in cur.fetchall()], use_container_width=True, hide_index=True)

        conn.close()
    except Exception as e:
        # Never interpolate the raw exception into a user-facing message --
        # a driver-level connection error (e.g. "invalid dsn") echoes back
        # the malformed connection string itself, which can contain a
        # misconfigured secret. Log server-side (Render's own log tab, not
        # public) for actual debugging; show only a generic message here.
        print(f"[browse-mock-data] DB connection failed: {e}")
        st.error("Could not reach the database. This is a configuration issue on the "
                 "backend, not something on your end -- the mock-data browser is "
                 "temporarily unavailable.")

    # Source C -- read directly from the repo checkout (not Postgres), so this
    # section still renders even if the DB connection above fails.
    st.subheader("Tax & Regulatory Documents (Source C)")
    st.caption("All documents below are **synthetic** -- written for this assignment to "
               "read like real Indian GST/TDS circulars, not real government issuances. "
               "See tax_docs/README.md in the repo for the full rationale behind each one.")
    tax_docs_dir = os.path.join(os.path.dirname(__file__), "..", "tax_docs")

    def _doc_category_tag(fname: str) -> str:
        if fname.startswith("gst_"):
            return "GST"
        if fname.startswith("tds_"):
            return "TDS"
        if fname.startswith("state_"):
            return "STATE"
        return "DOC"

    try:
        doc_files = sorted(f for f in os.listdir(tax_docs_dir)
                            if f.endswith(".md") and f != "README.md")
        for fname in doc_files:
            with open(os.path.join(tax_docs_dir, fname), encoding="utf-8") as f:
                body = f.read()
            title_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
            title = title_match.group(1).strip() if title_match else fname
            tag = _doc_category_tag(fname)
            with st.expander(f"[{tag}]  {title}  ·  `{fname}`"):
                st.markdown(body)
    except FileNotFoundError:
        st.warning("Tax document corpus not found alongside this deployment.")
