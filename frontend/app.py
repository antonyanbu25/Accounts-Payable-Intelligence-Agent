"""
Streamlit frontend for the Accounts Payable Intelligence Agent.

This is a thin client: all reasoning happens in n8n + the FastAPI service.
This app only renders the chat, the UC2 input surface, and a read-only view
of the mock data -- it never computes anything itself.
"""
import json
import os
from typing import Optional

import psycopg2
import psycopg2.extras
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

N8N_BASE = os.environ.get("N8N_BASE_URL", "http://localhost:5678")
DATABASE_URL = os.environ["DATABASE_URL"]

st.set_page_config(page_title="AP Intelligence Agent", page_icon="📒", layout="wide")

st.title("📒 Accounts Payable Intelligence Agent")
st.caption(
    "A working prototype reasoning across a Procurement Portal, an SAP-style Vendor & Payments DB, "
    "and synthetic tax/regulatory documents. Every number shown traces to a source record or a quoted "
    "regulatory clause — see the Evidence panel under each answer."
)

tab_ask, tab_validate, tab_browse = st.tabs(
    ["💬 Ask a Question", "✅ Validate a Payment Advice", "📊 Browse Mock Data"]
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
        with st.expander("📜 Tax document sources (exact clause quoted)"):
            for label, key in [("GST", "gst"), ("TDS", "tds")]:
                sub = tax_evidence.get(key)
                if not sub:
                    continue
                if sub.get("status") == "found":
                    st.markdown(f"**{label}** — *{sub['source_filename']}*")
                    st.markdown(f"> {sub['key_clause']}")
                elif sub.get("status") == "not_found":
                    st.markdown(f"**{label}** — no applicable rule found in the corpus.")

    with st.expander("🔍 Full structured evidence (raw)"):
        st.json(evidence)


# ---------------------------------------------------------------------------
# TAB 1 — UC1: Ask a question
# ---------------------------------------------------------------------------

with tab_ask:
    st.write("**Try one of these, or type your own question below:**")
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

    cols = st.columns(len(suggestions))
    for i, s in enumerate(suggestions):
        if cols[i].button(s, key=f"sugg_{i}", use_container_width=True):
            st.session_state.pending_question = s

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])
            if msg.get("evidence"):
                render_evidence(msg["evidence"], msg.get("tax_evidence"))

    typed = st.chat_input("Ask about a vendor balance or tax treatment...")
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
                    st.table(rows)

                if result.get("tax_evidence"):
                    with st.expander("📜 Tax document sources for this reconstruction"):
                        for label, key in [("GST", "gst"), ("TDS", "tds")]:
                            sub = result["tax_evidence"].get(key)
                            if sub and sub.get("status") == "found":
                                st.markdown(f"**{label}** — *{sub['source_filename']}*")
                                st.markdown(f"> {sub['key_clause']}")

                with st.expander("🔍 Full raw response"):
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
        st.error(f"Could not reach the database: {e}")
