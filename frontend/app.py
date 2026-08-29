"""
Streamlit frontend for the Accounts Payable Intelligence Agent.

This is a thin client: all reasoning happens in n8n + the FastAPI service.
This app only renders the chat, the UC2 input surface, and a read-only view
of the mock data -- it never computes anything itself.
"""
import json
import os
import re
from typing import Optional

import pandas as pd
import psycopg2
import psycopg2.extras
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

N8N_BASE = os.environ.get("N8N_BASE_URL", "http://localhost:5678")
DATABASE_URL = os.environ["DATABASE_URL"]

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
   edge-to-edge (which scans as "left-aligned" once the window is wide).
   Narrower than a first pass (1200px) -- this app's widest real content
   (the 2-up metric rows) doesn't need more than ~1000px, and a tighter
   column reads more intentional. Tabs/buttons stay left-aligned *within*
   this centered column -- centering each individually would look worse
   than a centered column of left-aligned content. */
[data-testid="stMainBlockContainer"] {
    max-width: 1000px;
    margin: 0 auto;
}

/* Currency/ID figures read as a ledger, not chat prose */
[data-testid="stMetricValue"] {
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}

/* Elevation against the off-white page background (see config.toml) --
   strong enough to actually read as "raised," not just a hairline. */
[data-testid="stMetric"], [data-testid="stAlertContainer"],
[data-testid="stExpander"], [data-testid="stBaseButton-secondary"],
[data-testid="stTableStyledTable"] {
    box-shadow: 0 1px 4px rgba(0,0,0,0.08), 0 2px 8px rgba(0,0,0,0.05);
    border: 1px solid #e4e7e7 !important;
}
[data-testid="stExpander"], [data-testid="stBaseButton-secondary"] {
    border-radius: 8px;
}

/* The chat input is the primary interaction surface for a conversational
   agent, and is deliberately the first thing rendered in the tab (see
   tab_ask below) -- it needs real visual weight: taller, an accent-tinted
   fill (not just a border) so it unmistakably reads as "the" input rather
   than "an" input among several controls. */
[data-testid="stChatInput"] {
    background-color: #eef4f4;
    box-shadow: 0 2px 12px rgba(61,107,111,0.16), 0 1px 3px rgba(0,0,0,0.06);
    border: 2px solid #3d6b6f;
    border-radius: 12px;
    min-height: 60px;
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
    if evidence.get("tax_treatment_refused"):
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

    # Input first -- this is the primary action for a conversational tool,
    # so it should be the first thing the eye lands on. Suggestions are a
    # secondary "or try one of these" affordance below it, not a sibling
    # beside it. (Verified this chat_input renders in normal document flow
    # here, not pinned to the viewport bottom, so this ordering actually
    # takes effect -- that's not true of every Streamlit layout context.)
    typed = st.chat_input("Ask about a vendor balance or tax treatment...")

    st.write("**Or try one of these:**")
    cols = st.columns(len(suggestions))
    for i, s in enumerate(suggestions):
        if cols[i].button(s, key=f"sugg_{i}", use_container_width=True):
            st.session_state.pending_question = s

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])
            if msg.get("evidence"):
                render_evidence(msg["evidence"], msg.get("tax_evidence"))

    question = typed or st.session_state.pending_question
    st.session_state.pending_question = None

    if question:
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.write(question)
        with st.chat_message("assistant"):
            with st.spinner("Looking this up across all three sources..."):
                try:
                    resp = requests.post(f"{N8N_BASE}/webhook/uc1-ask", json={"question": question}, timeout=90)
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
            st.write(data.get("narrative", "(no answer returned)"))
            if data.get("evidence"):
                render_evidence(data["evidence"], data.get("tax_evidence"))
        st.session_state.messages.append({
            "role": "assistant", "content": data.get("narrative", ""),
            "evidence": data.get("evidence"), "tax_evidence": data.get("tax_evidence"),
        })


# ---------------------------------------------------------------------------
# TAB 2 — UC2: Validate a payment advice
# ---------------------------------------------------------------------------

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
}

with tab_validate:
    st.write("An accountant's payment advice is uploaded, pasted, or entered — the agent independently "
             "reconstructs the correct figure first, *then* compares.")

    mode = st.radio("Input method", ["Use an example", "Manual form", "Paste JSON", "Upload a file"], horizontal=True)

    payload = None

    if mode == "Use an example":
        choice = st.selectbox("Pick a scenario", list(FIXTURES.keys()))
        st.json(FIXTURES[choice])
        if st.button("Validate this advice", type="primary"):
            payload = FIXTURES[choice]

    elif mode == "Manual form":
        with st.form("manual_advice_form"):
            invoice_id = st.number_input("Invoice ID", min_value=1, step=1)
            category = st.text_input("Category (as you understand it)", "")
            base_amount = st.number_input("Base amount (₹)", min_value=0.0, step=100.0)
            gst_rate_pct = st.number_input("GST rate (%)", min_value=0.0, step=0.5)
            split = st.radio("GST split", ["CGST + SGST (intra-state)", "IGST (inter-state)"])
            if split.startswith("CGST"):
                gst_cgst = st.number_input("CGST (₹)", min_value=0.0, step=10.0)
                gst_sgst = st.number_input("SGST (₹)", min_value=0.0, step=10.0)
                gst_igst = None
            else:
                gst_igst = st.number_input("IGST (₹)", min_value=0.0, step=10.0)
                gst_cgst = gst_sgst = None
            tds_amount = st.number_input("TDS amount (₹)", min_value=0.0, step=10.0)
            net_payable_claimed = st.number_input("Net payable claimed (₹)", min_value=0.0, step=10.0)
            submitted = st.form_submit_button("Validate this advice", type="primary")
        if submitted:
            advice = {"base_amount": base_amount, "category": category, "gst_rate_pct": gst_rate_pct,
                      "tds_amount": tds_amount, "net_payable_claimed": net_payable_claimed}
            if gst_igst is not None:
                advice["gst_igst"] = gst_igst
            else:
                advice["gst_cgst"] = gst_cgst
                advice["gst_sgst"] = gst_sgst
            payload = {"invoice_id": int(invoice_id), "submitted_advice": advice}

    elif mode == "Paste JSON":
        pasted = st.text_area(
            "Paste JSON matching {invoice_id, submitted_advice: {...}}", height=200,
            placeholder=json.dumps(FIXTURES["✅ Correct advice (INV-1)"], indent=2),
        )
        if st.button("Validate this advice", type="primary"):
            try:
                payload = json.loads(pasted)
            except json.JSONDecodeError as e:
                st.error(f"That's not valid JSON: {e}")

    elif mode == "Upload a file":
        uploaded = st.file_uploader("Upload a .json file", type=["json"])
        if uploaded and st.button("Validate this advice", type="primary"):
            try:
                payload = json.load(uploaded)
            except json.JSONDecodeError as e:
                st.error(f"That's not valid JSON: {e}")

    if payload:
        if "invoice_id" not in payload or "submitted_advice" not in payload:
            st.error("Missing required field(s): expected both 'invoice_id' and 'submitted_advice'.")
        else:
            with st.spinner("Independently reconstructing the correct figure, then comparing..."):
                try:
                    resp = requests.post(f"{N8N_BASE}/webhook/uc2-validate", json=payload, timeout=90)
                    result = resp.json()
                except Exception as e:
                    result = {"error": "request_failed", "message": str(e)}

            if result.get("error") == "not_found":
                st.error(result["message"])
            elif result.get("error"):
                st.error(f"Something went wrong: {result.get('message', result)}")
            else:
                diff = result.get("diff", {})
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
                if not diff.get("eligible", True):
                    st.divider()
                    st.warning("🚫 **NOT CLEAR TO PAY** — regardless of whether the numbers above match:\n\n"
                              + "\n".join(f"- {r}" for r in diff.get("eligibility_reasons", [])))

                if diff.get("fields"):
                    st.write("**Field-by-field comparison:**")
                    rows = []
                    for f in diff["fields"]:
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
