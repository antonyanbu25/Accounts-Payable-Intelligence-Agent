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
/* ===========================================================================
   Dark-glow restyle -- design suggestions/validate-payment-advice-build-
   prompt.md + AP Agent Dark Glow Mockup.html. Visual/CSS layer only: no
   change to webhook calls, computed values, or which fields exist.
   Tokens match the approved mockup's palette exactly. --muted is #96969f
   specifically (not darker) -- an earlier pass was flagged as too
   low-contrast to read comfortably against #0a0a0d.

   Inter is loaded via @import (not separate <link> tags before this
   <style> block -- confirmed live that broke Streamlit's markdown
   sanitizer, causing the whole stylesheet to render as literal page text
   instead of being applied). */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

:root {
    --bg: #0a0a0d;
    --panel: #131316;
    --panel-border: #1c1c20;
    --card: #111114;
    --input-bg: #17171b;
    --input-border: #26262b;
    --text: #e8e8ea;
    --text-hero: #f2f2f4;
    --muted: #96969f;
    --accent: #5b8fd9;
    --success-bg: #10261c; --success-border: #1f4a34; --success-text: #6fbf8f;
    --error-bg: #2a1614;   --error-border: #4a2620;   --error-text: #d97a6c;
    --warn-bg: #241a0d;    --warn-border: #4a3416;    --warn-text: #eab876;
    --info-bg: #10202a;    --info-border: #1f3d4e;    --info-text: #7fb8d9;
}

html, body, [data-testid="stAppViewContainer"] {
    font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.mono, [data-testid="stMetricValue"] {
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    font-variant-numeric: tabular-nums;
}

/* layout="wide" removes Streamlit's own max-width; re-cap it here so
   content reads as a centered column on a wide viewer instead of running
   edge-to-edge. 1200px kept from an earlier live-tested decision -- Browse
   Mock Data's tables (6 of them now) need the room a narrower cap would
   take away. */
[data-testid="stMainBlockContainer"] {
    max-width: 1200px;
    margin: 0 auto;
}

[data-testid="stHeading"], [data-testid="stCaptionContainer"] {
    text-align: center;
}
[data-testid="stCaptionContainer"] { color: var(--muted); opacity: 1; }

/* Tab bar: full-contrast labels for BOTH states (mockup: only the colored
   underline should distinguish active, not a dimmed/undimmed contrast
   difference -- Streamlit's default already renders inactive tabs dimmed,
   so this overrides that). Visually reordered to Browse / Ask / Validate
   via flex `order` -- the underlying tab DOM order (and therefore which
   tab loads by default) stays Ask / Validate / Browse; Streamlit's
   st.tabs() has no separate "default" setting independent of list order,
   so reordering the Python call would make Browse load first. `order` on
   a flex child reorders the visual position only. */
[data-testid="stTabs"] [role="tablist"] {
    justify-content: center;
    border-bottom: 1px solid var(--panel-border);
    gap: 4px;
}
[data-testid="stTabs"] [role="tab"] {
    color: var(--text-hero) !important;
    font-weight: 600;
    opacity: 1 !important;
}
[data-testid="stTabs"] [role="tab"]:nth-child(1) { order: 2; } /* Ask a Question */
[data-testid="stTabs"] [role="tab"]:nth-child(2) { order: 3; } /* Validate a Payment Advice */
[data-testid="stTabs"] [role="tab"]:nth-child(3) { order: 1; } /* Browse Mock Data */
[data-testid="stTabs"] [role="tab"][aria-selected="true"] {
    border-bottom-color: var(--accent) !important;
}

/* Flat panels everywhere -- the glow is reserved for actionable surfaces
   (chat input, the Validate button) and must never appear on read-only or
   compliance-critical content; box-shadow "elevation" (the light-theme
   treatment) is replaced with a hairline border on a slightly-lifted flat
   fill instead. stMetric is deliberately NOT in this group -- the mockup
   wants the evidence-panel metrics borderless (label above value, one
   hairline divider above the whole row -- see the explicit divider in
   render_evidence()), not boxed cards. */
[data-testid="stAlertContainer"],
[data-testid="stExpander"], [data-testid="stTableStyledTable"],
[data-testid="stForm"] {
    background-color: var(--panel) !important;
    border: 1px solid var(--panel-border) !important;
    box-shadow: none !important;
    border-radius: 8px;
}
[data-testid="stMetric"] { background: transparent !important; border: none !important; box-shadow: none !important; padding: 0 !important; overflow: visible !important; }
[data-testid="stMetricLabel"] { color: var(--muted) !important; font-size: 12px !important; }
/* Streamlit's default metric-value font-size truncates with an ellipsis in
   a 4-column row this narrow (confirmed live: "₹200,0…") -- sized down and
   no-wrap disabled so the full rupee figure is always legible, matching
   the mockup's smaller (~17px) metric values. */
[data-testid="stMetricValue"] {
    color: var(--text-hero) !important;
    font-size: 17px !important;
    white-space: normal !important;
    overflow: visible !important;
    text-overflow: clip !important;
}

/* Suggestion chips: lightweight -- no fill, thin border, muted text. The
   card-shadow treatment (metrics/alerts/tables above) reads as "act here";
   these are examples to skim, not actions, so they get the lightest
   possible treatment in the whole system. */
[data-testid="stBaseButton-secondary"] {
    border-radius: 999px !important;
    border: 1px solid var(--panel-border) !important;
    box-shadow: none !important;
    background-color: transparent !important;
    color: var(--muted) !important;
}
[data-testid="stBaseButton-secondary"]:hover {
    border-color: var(--accent) !important;
    color: var(--text) !important;
}

/* Chat input: pill with a soft radial glow behind it. The mockup demotes
   this to a plain, glow-free bar once a conversation starts -- deliberately
   NOT replicated: st.chat_input() is called once, standalone, outside the
   st.container(key="chat_landing") the rest of the landing state lives in,
   so there's no clean CSS hook to condition its own styling on landing-vs-
   chat state without a larger restructure. Kept simple: the glow stays
   present in both states, a minor aesthetic gap from the mockup, not a
   functional one. Sticky
   positioning + the double-border structural fix are unchanged from the
   light-theme version -- only colors and the glow are new. */
[data-testid="stChatInput"] {
    background:
      radial-gradient(circle at 50% 0%, color-mix(in oklab, var(--accent) 35%, transparent) 0%, color-mix(in oklab, var(--accent) 12%, transparent) 35%, transparent 70%),
      var(--input-bg) !important;
    border: 1px solid var(--input-border) !important;
    border-radius: 28px;
    min-height: 60px;
    position: sticky;
    bottom: 0;
    z-index: 10;
    margin-top: 12px;
}
[data-testid="stChatInputTextArea"] {
    font-size: 1.05rem;
    padding-top: 16px;
    padding-bottom: 16px;
    background-color: transparent !important;
    color: var(--text) !important;
    /* Explicit cap, not just min-height on the wrapper -- older Streamlit
       versions (confirmed on 1.50.0, not present on the deployed 1.62.0)
       auto-expand this textarea to ~260px with no cap otherwise. */
    max-height: 60px !important;
}
[data-testid="stChatInputTextArea"]::placeholder { color: var(--muted) !important; }
/* Double-border fix: stChatInput renders a middle, un-testid'd wrapper div
   between the outer container above and the textarea below, carrying
   Streamlit's own native border + :focus-within rule pointing at
   theme.colors.primary. No data-testid exists on it, so this targets it
   structurally (direct child). */
[data-testid="stChatInput"] > div,
[data-testid="stChatInput"] > div:focus-within {
    border: none !important;
    box-shadow: none !important;
    outline: none !important;
}

/* Landing state (no conversation yet): vertically centered in the space
   below the tab bar, so the glowing input is the one obvious thing to do
   -- set only while st.session_state.messages is empty (see the Python
   st.container(key="chat_landing") block), and harmless once a real
   conversation exists (that container is simply never rendered again). */
.st-key-chat_landing { min-height: 56vh; display: flex; flex-direction: column; justify-content: center; }

/* Chat messages: user right-aligned as a bubble, agent left-aligned as
   plain text -- deliberately asymmetric, not two bubbles, so the agent's
   longer grounded answers read as the primary content and the user's
   short questions read as prompts. No avatar icons either role. */
[data-testid="stChatMessageAvatarUser"], [data-testid="stChatMessageAvatarAssistant"] { display: none; }
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
    margin-left: auto;
    max-width: 78%;
}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stChatMessageContent"] {
    background: var(--panel);
    border: 1px solid var(--panel-border);
    border-radius: 14px;
    padding: 10px 16px;
}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"]) {
    max-width: 82%;
}

/* Restrained, harmonized alert palette for the dark ground. */
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentSuccess"]) {
    background-color: var(--success-bg) !important; border: 1px solid var(--success-border) !important;
}
[data-testid="stAlertContentSuccess"] { color: var(--success-text) !important; }

[data-testid="stAlertContainer"]:has([data-testid="stAlertContentError"]) {
    background-color: var(--error-bg) !important; border: 1px solid var(--error-border) !important;
}
[data-testid="stAlertContentError"] { color: var(--error-text) !important; }

/* Info = "held for review" (category conflict / tax treatment refused) --
   deliberately calm and blue, NOT error-red or warning-amber, so a
   genuine data conflict with no defensible resolution reads as correct,
   confident behavior rather than a broken state. */
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentInfo"]) {
    background-color: var(--info-bg) !important; border: 1px solid var(--info-border) !important;
}
[data-testid="stAlertContentInfo"] { color: var(--info-text) !important; }

/* Warning = the "not eligible to pay" / unapplied-advance family --
   deliberately its own color, distinct from both error (numeric mismatch)
   and info (held for review). Never visually confusable with either. */
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentWarning"]) {
    background-color: var(--warn-bg) !important; border: 1px solid var(--warn-border) !important;
    border-left: 4px solid var(--warn-text) !important;
}
[data-testid="stAlertContentWarning"] { color: var(--warn-text) !important; font-weight: 600; }

/* The Validate button gets the same radial-glow treatment as the chat
   input -- the one primary action on this tab. Every other button
   (form-submit buttons inside expanders, etc.) is left alone; grep
   confirms this selector only ever matches type="primary" buttons, and
   "Validate this advice" is the only one in regular use on this tab. */
[data-testid="stBaseButton-primary"], [data-testid="stFormSubmitButton"] button {
    background:
      radial-gradient(circle at 50% 100%, color-mix(in oklab, var(--accent) 32%, transparent) 0%, color-mix(in oklab, var(--accent) 10%, transparent) 40%, transparent 72%),
      var(--input-bg) !important;
    border: 1px solid var(--input-border) !important;
    color: var(--text-hero) !important;
    font-weight: 600;
}

/* Comparison table (UC2 field-by-field diff): fixed column widths so
   Submitted/Correct/Match don't stretch full-bleed with large dead gaps,
   and those three columns center-aligned against their (also centered)
   headers -- the mismatch between a left-aligned header and a right-
   aligned value column was a real readability bug caught while building
   this mockup. st.table (not st.dataframe) renders real HTML, so this is
   reachable via CSS at all. */
[data-testid="stTable"] table { table-layout: fixed; max-width: 640px; }
[data-testid="stTable"] th:not(:first-child), [data-testid="stTable"] td:not(:first-child) {
    text-align: center !important;
}
/* pandas Styler's .hide(axis="index") has no effect when rendered through
   st.table() -- confirmed live, the row-number column (.row_heading) and
   its blank corner header (.blank) still render regardless. CSS fallback
   since the Styler-level directive doesn't reach the actual output here. */
[data-testid="stTable"] th.row_heading, [data-testid="stTable"] th.blank {
    display: none;
}

/* Browse Mock Data: st.dataframe already provides its own scroll/sticky-
   header behavior -- only the palette needs to follow the theme here. */
[data-testid="stDataFrame"] { background-color: var(--panel); }
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
        # A genuine, no-defensible-rule source conflict is correct, confident
        # behavior -- not a broken state -- so this deliberately uses st.info
        # (calm blue) rather than st.warning/st.error. It's a separate
        # elif branch from the normal eligibility display below, so this
        # never collides with or dilutes the "not eligible to pay" warning.
        st.info(
            f"**⏸ Held for review — {evidence.get('eligibility', '')}**\n\n"
            f"Pre-tax ledger position (base − advances − credits − payments): "
            f"₹{evidence.get('pre_tax_ledger_position'):,.2f}"
        )
        cc = evidence.get("category_conflict")
        if cc:
            st.write(f"PO says **{cc['po_category']}**, invoice says **{cc['invoice_category']}** — "
                     f"no rule to resolve which is correct, so tax treatment is withheld pending review. "
                     f"This is a genuine conflict in the source data that needs a human decision, not an error in the system.")
    else:
        gst = evidence.get("gst") or {}
        # A single 4-column row, not two 2x2 rows -- matches the mockup's
        # "4 metrics in one borderless row" layout. The divider is an
        # explicit element (not a CSS rule targeting the column row itself)
        # since Streamlit gives st.columns() no stable hook to distinguish
        # THIS row from every other st.columns() call elsewhere in the app.
        st.markdown('<div style="border-top: 1px solid var(--panel-border); margin: 18px 0 4px;"></div>',
                    unsafe_allow_html=True)
        c1, c2, c3, c4 = st.columns(4)
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


def render_comparison_evidence(invoices: list):
    """Comparison response ({comparison: true, invoices: [...]}) -- side-by-
    side columns, each rendered with the existing single-invoice
    render_evidence() so the money-metric layout and guard-fallback styling
    stay in exactly one place rather than being reimplemented here."""
    cols = st.columns(len(invoices)) if invoices else []
    for col, inv in zip(cols, invoices):
        with col:
            st.markdown(f"**Invoice {inv.get('invoice_id', '—')}** ({inv.get('invoice_date', '—')})")
            render_evidence(inv.get("evidence") or {}, inv.get("tax_evidence"))


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
        # Vertically centers the hero+chips block in the space below the tab
        # bar, so the glow (on the input, pinned at the bottom -- see the
        # note on why it can't move into this same flex group) is the
        # obvious focal point rather than one control among several stacked
        # at the top. Harmless and never rendered again once a real
        # conversation exists. st.container(key=...) (not two separate
        # st.markdown('<div>')/('</div>') calls) -- confirmed live that a
        # raw opening tag from one st.markdown call does NOT actually wrap
        # widgets rendered by later Streamlit calls (each becomes its own
        # sibling element), so the "wrapper" div was empty and the intended
        # min-height just showed up as a big blank gap above the real
        # content. st.container(key="chat_landing") emits a real DOM
        # container Streamlit itself manages, taggable as .st-key-chat_landing.
        with st.container(key="chat_landing"):
            st.markdown('<p style="text-align:center; font-weight:300; font-size:32px; '
                        'color:var(--text-hero); letter-spacing:-0.01em; margin:0 0 8px;">'
                        'Ask about a vendor, invoice, or tax treatment</p>', unsafe_allow_html=True)
            st.write("**Try one of these, or type your own question below:**")
            cols = st.columns(len(suggestions))
            for i, s in enumerate(suggestions):
                cols[i].button(s, key=f"sugg_{i}", use_container_width=True,
                                on_click=_use_suggestion, args=(s,))

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])
            if msg.get("comparison"):
                render_comparison_evidence(msg.get("invoices") or [])
            elif msg.get("evidence"):
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
            "comparison": data.get("comparison", False), "invoices": data.get("invoices"),
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

                # Same advisory UC1 already shows for this invoice -- an
                # advance never gets netted into overall_match/eligible, so
                # a numerically "correct" advice must never read as
                # unconditionally clear to release in full when one exists.
                # Never present for category_only (no real advance data
                # exists for an invoice that isn't recorded yet).
                if not is_category_only and diff.get("unapplied_advance_advisory"):
                    st.info(f"⚠️ Advisory: an unapplied advance of ₹{diff['unapplied_advance_advisory']:,.2f} "
                            f"exists against this vendor/PO and has NOT been netted here — a possible overpayment risk if missed.")

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
                        # pandas Styler renders these as inline style="..."
                        # attributes, which beat any external stylesheet rule
                        # on specificity alone -- confirmed live (the CSS
                        # dark-theme pass had zero effect here until these
                        # literal colors were fixed at the source). var()
                        # references still resolve correctly in an inline
                        # attribute since :root is an ancestor of the table.
                        if val == "✅":
                            return "background-color: var(--success-bg); color: var(--success-text); font-weight: 600;"
                        if val == "❌":
                            return "background-color: var(--error-bg); color: var(--error-text); font-weight: 600;"
                        return ""

                    # st.table (not st.dataframe) deliberately -- st.dataframe
                    # renders to a <canvas> grid with no per-cell DOM, so CSS
                    # and this Styler-based formatting can't reach it at all.
                    # st.table renders real HTML, so both work. Same reasoning
                    # applies to every property below: set explicitly here,
                    # not left to inheritance from the page's dark palette.
                    styled = (
                        df.style
                        .hide(axis="index")
                        # Broad defaults FIRST, then .map()'s per-cell
                        # background LAST -- pandas Styler concatenates
                        # style rules in call order into one inline style="
                        # ..." attribute, and CSS's own "last property wins"
                        # rule then applies. Confirmed live: with .map()
                        # before this broad set_properties(), the flat
                        # background-color here was silently clobbering the
                        # conditional success/error colors on every match/
                        # mismatch cell.
                        .set_properties(**{"background-color": "var(--panel)", "color": "var(--text)",
                                            "border-color": "var(--panel-border)"})
                        .set_properties(subset=["Submitted", "Correct", "Match?"], **{
                            "font-family": "ui-monospace, SFMono-Regular, monospace",
                            # Center-aligned against a centered header, not
                            # right-aligned against a left-aligned one -- a
                            # real readability mismatch caught while building
                            # this mockup.
                            "text-align": "center",
                        })
                        .set_table_styles([
                            {"selector": "th", "props": "background-color: var(--card); color: var(--muted); "
                                                          "border-color: var(--panel-border); text-align: center; "
                                                          "font-weight: 500; font-size: 12px;"},
                            {"selector": "th:first-child, td:first-child", "props": "text-align: left;"},
                        ])
                        .map(_match_style, subset=["Match?"])
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

def _section_header(title: str, source_label: str):
    """Labels each table with which of the three sources it belongs to --
    per the domain model, Vendors/Invoices are Source B (SAP-style Vendor &
    Payments DB); Offices/Requisitions/Purchase Orders/Goods Receipts are
    Source A (Procurement Portal). The reference mockup had these two
    swapped -- corrected here to match the actual data model, not copied
    from the mockup verbatim."""
    st.markdown(f'<div style="display:flex; align-items:baseline; gap:10px; margin:8px 0 14px;">'
                f'<h3 style="margin:0; font-size:20px; color:var(--text-hero);">{title}</h3>'
                f'<span style="font-size:11px; color:var(--accent); border:1px solid '
                f'color-mix(in oklab, var(--accent) 45%, transparent); border-radius:4px; '
                f'padding:2px 7px;">{source_label}</span></div>', unsafe_allow_html=True)


with tab_browse:
    st.write("Read-only view of the seeded mock data — a reviewer can see what's in the system "
             "before asking a question, without needing to inspect the source repo.")
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        _section_header("Vendors", "Source B · Postgres")
        cur.execute("""
            SELECT v.vendor_id, v.legal_name, v.registered_state, v.gstin, v.payment_terms, v.status
            FROM vendor_master v ORDER BY v.vendor_id;
        """)
        st.dataframe([dict(r) for r in cur.fetchall()], use_container_width=True, hide_index=True)

        _section_header("Invoices", "Source B · Postgres")
        cur.execute("""
            SELECT i.invoice_id, v.legal_name AS vendor, c.category_name AS category,
                   i.invoice_date, i.base_amount, i.status
            FROM invoice i
            JOIN vendor_master v ON i.vendor_id = v.vendor_id
            JOIN category c ON i.category_id = c.category_id
            ORDER BY i.invoice_date DESC;
        """)
        st.dataframe([dict(r) for r in cur.fetchall()], use_container_width=True, hide_index=True)

        _section_header("Offices", "Source A · Postgres")
        cur.execute("SELECT office_id, name, city, state FROM office ORDER BY office_id;")
        st.dataframe([dict(r) for r in cur.fetchall()], use_container_width=True, hide_index=True)

        # Source A's own "structured workflow from request to fulfillment" --
        # previously queried nowhere in the frontend, even though it's the
        # direct input to every three-way-match and cancelled-PO/category-
        # conflict eligibility computation this system performs. Found by
        # recruiter-mindset testing: that reasoning is real and correct, but
        # was undiscoverable to anyone restricted to the live app. Read-only,
        # same pattern as Vendors/Invoices/Offices above -- no new tables,
        # nothing built, only surfaced.
        _section_header("Requisitions", "Source A · Postgres")
        cur.execute("""
            SELECT r.requisition_id, o.name AS office, r.requester, r.department,
                   c.category_name AS category, r.estimated_amount, r.status, r.created_date
            FROM requisition r
            JOIN office o ON r.office_id = o.office_id
            JOIN category c ON r.category_id = c.category_id
            ORDER BY r.requisition_id;
        """)
        st.dataframe([dict(r) for r in cur.fetchall()], use_container_width=True, hide_index=True)

        _section_header("Purchase Orders", "Source A · Postgres")
        cur.execute("""
            SELECT po.po_id, v.legal_name AS vendor, c.category_name AS category,
                   po.po_amount, po.issued_date, po.status
            FROM purchase_order po
            JOIN vendor_master v ON po.vendor_id = v.vendor_id
            JOIN category c ON po.category_id = c.category_id
            ORDER BY po.po_id;
        """)
        st.dataframe([dict(r) for r in cur.fetchall()], use_container_width=True, hide_index=True)

        _section_header("Goods Receipts", "Source A · Postgres")
        cur.execute("SELECT receipt_id, po_id, received_date, received_amount, status FROM receipt ORDER BY receipt_id;")
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
    _section_header("Tax & Regulatory Documents", "Source C · Unstructured files")
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
