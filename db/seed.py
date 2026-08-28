#!/usr/bin/env python3
"""
Seed data generator for the Accounts Payable Intelligence Agent mock.

Produces db/seed_data.sql — a set of INSERT statements that can be run
against the Postgres schema in db/schema.sql.

Design notes (see plan v4 for full rationale):
  - 15 vendors, ~28 invoices, MAJORITY clean/boring — edge cases are
    concentrated in a curated, clearly-commented subset below.
  - Tax rates/TDS sections here are illustrative/synthetic and must stay in
    sync with the tax circulars authored in tax_docs/ — they are the same
    "ground truth" the mock is built around.
  - All monetary amounts are tax-EXCLUSIVE base values unless a field name
    says otherwise (gst_amount_stated, etc.) — matches the 3-way-match
    tax-exclusive-comparison rule in the compute contract.
"""

import datetime
import os

# -------------------------------------------------------------------------
# Illustrative tax-rate tables (kept in sync with tax_docs/*.md)
# Format: category_name -> {"pre": rate_before_22-Sep-2025, "post": rate_after}
# -------------------------------------------------------------------------
GST_RATES = {
    "Furniture":  {"pre": 18.0, "post": 18.0},   # unaffected by the reform
    "Software":   {"pre": 18.0, "post": 12.0},   # rate reduced by the reform (illustrative)
    "Services":   {"pre": 18.0, "post": 18.0},   # unaffected
    "Food":       {"pre": 18.0, "post": 5.0},    # rate reduced by the reform (illustrative)
    "Appliances": {"pre": 18.0, "post": 18.0},   # unaffected
}
RATE_CHANGE_DATE = datetime.date(2025, 9, 22)

TDS_RATES = {   # illustrative Section-based rates, applied to base_amount (excl. GST)
    "Furniture":  {"rate": 0.0,  "section": None},
    "Software":   {"rate": 10.0, "section": "194J"},
    "Services":   {"rate": 10.0, "section": "194J"},
    "Food":       {"rate": 2.0,  "section": "194C"},
    "Appliances": {"rate": 0.0,  "section": None},
}

def gst_rate_for(category: str, on_date: datetime.date) -> float:
    band = "pre" if on_date < RATE_CHANGE_DATE else "post"
    return GST_RATES[category][band]

def tds_rate_for(category: str) -> float:
    return TDS_RATES[category]["rate"]

# -------------------------------------------------------------------------
# Reference data
# -------------------------------------------------------------------------

CATEGORIES = [
    # (id, name, hsn_or_sac, code_type)
    (1, "Furniture",  "9403",   "HSN"),
    (2, "Software",   "998313", "SAC"),
    (3, "Services",   "998311", "SAC"),
    (4, "Food",       "996331", "SAC"),
    (5, "Appliances", "8516",   "HSN"),
]

OFFICES = [
    # (id, name, city, state)
    (1, "DevRev-style Bangalore Office", "Bangalore",     "Karnataka"),
    (2, "DevRev-style Mumbai Office",    "Navi Mumbai",   "Maharashtra"),
    (3, "DevRev-style Chennai Office",   "Chennai",       "Tamil Nadu"),
]
# Sample offices modeled on a realistic 3-state Indian tech-company footprint
# (see assumption log — not attributed to any real company in the deliverable).

STATE_CODE = {"Karnataka": "29", "Maharashtra": "27", "Tamil Nadu": "33"}

def make_gstin(state: str, seq: int) -> str:
    """Synthetic but well-formed-looking GSTIN: 2-digit state code + PAN-like + entity + Z + checksum."""
    sc = STATE_CODE[state]
    pan = f"AP{seq:03d}Q"          # fake PAN-shaped string, 10 chars total with prefix below
    return f"{sc}{pan}0000F1Z{seq % 10}"

# (id, legal_name, state, category_hint, status)
VENDORS = [
    (1,  "TechNova Software Solutions Pvt Ltd", "Karnataka",    "Software",   "active"),
    (2,  "Bright Office Furnishings Pvt Ltd",    "Maharashtra",  "Furniture",  "active"),
    (3,  "Chennai Business Consultants LLP",     "Tamil Nadu",   "Services",   "active"),
    (4,  "SpiceRoute Catering Services",         "Karnataka",    "Food",       "active"),
    (5,  "ApplianceWorld Distributors Pvt Ltd",  "Maharashtra",  "Appliances", "active"),
    (6,  "CodeCraft IT Services Pvt Ltd",        "Tamil Nadu",   "Software",   "active"),
    (7,  "Metro Furniture Traders",              "Karnataka",    "Furniture",  "active"),
    (8,  "Prime Consulting Group",               "Maharashtra",  "Services",   "active"),
    (9,  "FreshBite Foods Pvt Ltd",               "Tamil Nadu",   "Food",       "active"),
    (10, "ElectroMax Appliances Pvt Ltd",        "Karnataka",    "Appliances", "active"),
    (11, "Acme Furnishings Pvt Ltd",             "Maharashtra",  "Furniture",  "active"),  # name-ambiguity pair
    (12, "Acme Traders Pvt Ltd",                 "Maharashtra",  "Furniture",  "active"),  # name-ambiguity pair
    (13, "Skyline Software Labs",                "Tamil Nadu",   "Software",   "active"),
    (14, "Coastal Facility Services",            "Karnataka",    "Services",   "blocked"), # blocked-vendor case
    (15, "Urban Appliance Hub",                  "Maharashtra",  "Appliances", "active"),
]

VENDOR_MASTER_SQL_ROWS = []
VENDOR_ONBOARDING_SQL_ROWS = []
for vid, name, state, _cat, status in VENDORS:
    gstin = make_gstin(state, vid)
    VENDOR_MASTER_SQL_ROWS.append(
        (vid, name, gstin, state, f"PAN{vid:04d}Q", "Net 30", status)
    )
    # Vendor 8 (Prime Consulting Group) is the seeded authority-resolved conflict:
    # onboarding captured the WRONG state; GSTIN (Maharashtra) is what's actually correct.
    submitted_state = "Karnataka" if vid == 8 else state
    VENDOR_ONBOARDING_SQL_ROWS.append(
        (vid, "approved", submitted_state, datetime.date(2024, 1, 1) + datetime.timedelta(days=vid))
    )

CATEGORY_BY_NAME = {name: cid for (cid, name, _hsn, _t) in CATEGORIES}
VENDOR_BY_ID = {v[0]: v for v in VENDORS}

# -------------------------------------------------------------------------
# Invoices and their upstream chain (requisition -> PO -> receipt -> invoice
# -> advance/payment/credit note as applicable).
# Each entry below is a small "scenario spec"; build_invoice() expands it
# into the full relational chain. Keeping this declarative make each case
# easy to review and hand-verify against the eval set later.
# -------------------------------------------------------------------------

requisition_id_seq = iter(range(1, 1000))
po_id_seq = iter(range(1, 1000))
receipt_id_seq = iter(range(1, 1000))
invoice_id_seq = iter(range(1, 1000))
advance_id_seq = iter(range(1, 1000))
payment_id_seq = iter(range(1, 1000))
credit_id_seq = iter(range(1, 1000))

requisitions, purchase_orders, receipts, invoices = [], [], [], []
advances, payments, credit_notes = [], [], []

def d(y, m, day):
    return datetime.date(y, m, day)

def add_full_chain(vendor_id, office_id, category_name, base_amount, invoice_date,
                    *, gst_rate_stated_override=None, po_amount_override=None,
                    receipt_amount_override=None, po_category_override=None,
                    invoice_category_override=None, no_po=False,
                    po_status="issued", invoice_status="open", note=""):
    """Builds requisition->PO->receipt->invoice for one scenario, returns invoice_id."""
    cat_id = CATEGORY_BY_NAME[category_name]
    po_cat_id = CATEGORY_BY_NAME[po_category_override] if po_category_override else cat_id
    inv_cat_id = CATEGORY_BY_NAME[invoice_category_override] if invoice_category_override else cat_id

    req_id = next(requisition_id_seq)
    requisitions.append((req_id, office_id, "A. Kumar", "Operations", po_cat_id,
                          base_amount, "approved", invoice_date - datetime.timedelta(days=20)))

    po_id = None
    if not no_po:
        po_id = next(po_id_seq)
        po_amt = po_amount_override if po_amount_override is not None else base_amount
        purchase_orders.append((po_id, req_id, vendor_id, po_cat_id, po_amt,
                                 invoice_date - datetime.timedelta(days=15), po_status))

        rec_id = next(receipt_id_seq)
        rec_amt = receipt_amount_override if receipt_amount_override is not None else base_amount
        rec_status = "full" if rec_amt >= base_amount else "partial"
        receipts.append((rec_id, po_id, invoice_date - datetime.timedelta(days=5), rec_amt, rec_status))

    inv_id = next(invoice_id_seq)
    rate = gst_rate_for(category_name, invoice_date) if gst_rate_stated_override is None else gst_rate_stated_override
    gst_amt_stated = round(base_amount * rate / 100, 2)
    invoices.append((inv_id, po_id, vendor_id, invoice_date, inv_cat_id, base_amount,
                      rate, gst_amt_stated, invoice_status, note))
    return inv_id, po_id

# ---- Clean / boring baseline invoices (majority of the ledger) ----------

add_full_chain(2,  1, "Furniture",  85000.00,  d(2026, 3, 10), note="clean")
add_full_chain(5,  2, "Appliances", 42000.00,  d(2026, 3, 12), note="clean")
add_full_chain(9,  3, "Food",       15000.00,  d(2026, 3, 15), note="clean")
add_full_chain(10, 1, "Appliances", 61000.00,  d(2026, 2, 20), note="clean")
add_full_chain(13, 3, "Software",   120000.00, d(2026, 3, 5),  note="clean")
add_full_chain(3,  2, "Services",   95000.00,  d(2026, 2, 18), note="clean intra?")  # V3 is TN, office MH -> inter-state IGST
add_full_chain(4,  1, "Food",       22000.00,  d(2026, 3, 1),  note="clean intra")
add_full_chain(5,  2, "Appliances", 30000.00,  d(2026, 1, 15), note="clean")

# ---- Tier 1: core, fully built + demoable + in eval set ------------------

# 1. Partial payment
inv_partial, _ = add_full_chain(1, 1, "Software", 200000.00, d(2026, 2, 1), note="TIER1 partial-payment")
payments.append((next(payment_id_seq), inv_partial, 120000.00, d(2026, 2, 15), "partial"))

# 2. Applied advance
inv_advance, po_advance = add_full_chain(6, 3, "Software", 150000.00, d(2026, 2, 10), note="TIER1 applied-advance")
advances.append((next(advance_id_seq), 6, po_advance, 30000.00, d(2026, 1, 20), inv_advance))
payments.append((next(payment_id_seq), inv_advance, 120000.00, d(2026, 2, 20), "full"))  # remaining after advance

# 3. Credit note
inv_credit, _ = add_full_chain(7, 1, "Furniture", 60000.00, d(2026, 2, 5), note="TIER1 credit-note")
credit_notes.append((next(credit_id_seq), 7, inv_credit, 5000.00, "Damaged item returned", d(2026, 2, 12)))
payments.append((next(payment_id_seq), inv_credit, 55000.00, d(2026, 2, 20), "full"))

# 4. TDS alongside GST (Services vendor, TN->KA inter-state, TDS u/s 194J)
inv_tds, _ = add_full_chain(3, 1, "Services", 80000.00, d(2026, 3, 1), note="TIER1 tds-with-gst")
payments.append((next(payment_id_seq), inv_tds, 80000.00, d(2026, 3, 20), "full"))

# 5. Effective-dated tax rules: one invoice each side of 22-Sep-2025
inv_pre_change, _ = add_full_chain(13, 3, "Software", 100000.00, d(2025, 9, 1), note="TIER1 pre-rate-change (18%)")
inv_post_change, _ = add_full_chain(13, 3, "Software", 100000.00, d(2025, 10, 1), note="TIER1 post-rate-change (12%)")

# 6. Source conflict, authority-resolved: vendor 8's onboarding state (KA) vs GSTIN state (MH)
inv_authority_conflict, _ = add_full_chain(8, 2, "Services", 70000.00, d(2026, 3, 3),
                                            note="TIER1 authority-conflict (vendor 8: onboarding KA vs GSTIN MH)")

# 7. Source conflict, no rule -> refuse: PO says Software, Invoice says Services (vendor 13)
inv_no_rule_conflict, _ = add_full_chain(13, 3, "Software", 90000.00, d(2026, 3, 8),
                                          po_category_override="Software",
                                          invoice_category_override="Services",
                                          note="TIER1 no-rule-conflict (PO=Software, Invoice=Services)")

# 8. Vendor's own invoice states a superseded rate (three-way tension)
#    Dated AFTER the change (true rate = 12%), but vendor's invoice claims the OLD 18% rate.
inv_stale_rate, _ = add_full_chain(1, 1, "Software", 110000.00, d(2025, 11, 15),
                                    gst_rate_stated_override=18.0,
                                    note="TIER1 vendor-stated-superseded-rate (invoice claims old 18%, true rate is 12%)")

# 9. CGST+SGST vs IGST split — already naturally covered by intra-state vendor/office
#    pairs above (e.g. V1/office1, V2/office2) vs inter-state pairs (V3/office2 above,
#    and this dedicated one for clarity):
inv_igst_demo, _ = add_full_chain(6, 1, "Software", 130000.00, d(2026, 3, 12),
                                   note="TIER1 igst-demo (vendor TN, office KA -> inter-state IGST)")
inv_cgst_sgst_demo, _ = add_full_chain(1, 1, "Software", 130000.00, d(2026, 3, 13),
                                        note="TIER1 cgst-sgst-demo (vendor KA, office KA -> intra-state)")

# ---- Adversarial / robustness (nonexistent vendor needs no seed row) -----
# "Question dated before the slab change" is covered by inv_pre_change above.
# "False-positive" and "multi-error" UC2 checks use clean invoices as targets —
# reusing the first clean invoice above (Bright Office Furnishings) is fine.

# ---- Tier 2: modeled in schema/data, documented, not demoed live ---------

# Unapplied advance (sits against vendor 2, no invoice claimed yet)
advances.append((next(advance_id_seq), 2, None, 15000.00, d(2026, 2, 1), None))

# Non-PO invoice (maverick spend)
inv_no_po, _ = add_full_chain(9, 3, "Food", 8000.00, d(2026, 3, 6), no_po=True,
                               note="TIER2 non-po-maverick-spend")

# Price/amount 3-way-match failure (PO 50000, receipt 50000, invoice 58000 -> beyond tolerance)
inv_mismatch, _ = add_full_chain(10, 1, "Appliances", 58000.00, d(2026, 3, 9),
                                  po_amount_override=50000.00, receipt_amount_override=50000.00,
                                  note="TIER2 price-mismatch (PO/receipt 50000 vs invoice base 58000)")

# Vendor name ambiguity pair (Acme Furnishings vs Acme Traders)
inv_acme1, _ = add_full_chain(11, 2, "Furniture", 40000.00, d(2026, 3, 2), note="TIER2 name-ambiguity (Acme Furnishings)")
inv_acme2, _ = add_full_chain(12, 2, "Furniture", 45000.00, d(2026, 3, 4), note="TIER2 name-ambiguity (Acme Traders)")

# Blocked vendor (vendor 14 already status='blocked' in VENDORS table above)
inv_blocked, _ = add_full_chain(14, 1, "Services", 30000.00, d(2026, 3, 7), note="TIER2 blocked-vendor")

# Cancelled PO
inv_cancelled_po, po_cancelled = add_full_chain(15, 2, "Appliances", 25000.00, d(2026, 3, 11),
                                                 po_status="cancelled", note="TIER2 cancelled-po")

print(f"Generated: {len(VENDORS)} vendors, {len(invoices)} invoices, "
      f"{len(purchase_orders)} POs, {len(advances)} advances, "
      f"{len(payments)} payments, {len(credit_notes)} credit notes.")

# -------------------------------------------------------------------------
# Emit SQL
# -------------------------------------------------------------------------

def sql_str(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, datetime.date):
        return f"'{v.isoformat()}'"
    return "'" + str(v).replace("'", "''") + "'"

def emit(table, columns, rows):
    lines = []
    for r in rows:
        vals = ", ".join(sql_str(v) for v in r)
        lines.append(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({vals});")
    return "\n".join(lines)

out = []
out.append("-- Auto-generated by db/seed.py — do not hand-edit, edit the generator instead.\n")

out.append(emit("category", ["category_id", "category_name", "hsn_or_sac_code", "code_type"], CATEGORIES))
out.append(emit("office", ["office_id", "name", "city", "state"], OFFICES))
out.append(emit("vendor_master",
                 ["vendor_id", "legal_name", "gstin", "registered_state", "pan", "payment_terms", "status"],
                 VENDOR_MASTER_SQL_ROWS))
out.append(emit("vendor_onboarding",
                 ["vendor_id", "onboarding_status", "submitted_state", "onboarding_date"],
                 VENDOR_ONBOARDING_SQL_ROWS))
out.append(emit("requisition",
                 ["requisition_id", "office_id", "requester", "department", "category_id",
                  "estimated_amount", "status", "created_date"], requisitions))
out.append(emit("purchase_order",
                 ["po_id", "requisition_id", "vendor_id", "category_id", "po_amount",
                  "issued_date", "status"], purchase_orders))
out.append(emit("receipt",
                 ["receipt_id", "po_id", "received_date", "received_amount", "status"], receipts))
out.append(emit("invoice",
                 ["invoice_id", "po_id", "vendor_id", "invoice_date", "category_id", "base_amount",
                  "gst_rate_stated", "gst_amount_stated", "status", "note_internal_only"],
                 # NOTE: note_internal_only is NOT a real schema column — stripped below.
                 [row[:-1] for row in invoices]))
# Rebuild invoice INSERTs without the trailing dev-only note column:
out[-1] = emit("invoice",
               ["invoice_id", "po_id", "vendor_id", "invoice_date", "category_id", "base_amount",
                "gst_rate_stated", "gst_amount_stated", "status"],
               [row[:-1] for row in invoices])
out.append(emit("advance",
                 ["advance_id", "vendor_id", "po_id", "amount", "advance_date",
                  "applied_against_invoice_id"], advances))
out.append(emit("payment",
                 ["payment_id", "invoice_id", "amount", "payment_date", "type"], payments))
out.append(emit("credit_note",
                 ["credit_id", "vendor_id", "invoice_id", "amount", "reason", "credit_date"], credit_notes))

_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "seed_data.sql"), "w") as f:
    f.write("\n\n".join(out) + "\n")

print("Wrote db/seed_data.sql")

# Also dump a human-readable scenario index so it's easy to hand-verify later
with open(os.path.join(_HERE, "scenario_index.txt"), "w") as f:
    f.write("Invoice ID -> scenario note (for hand-verification / eval-set writing)\n")
    f.write("=" * 70 + "\n")
    for row in invoices:
        f.write(f"invoice_id={row[0]:<4} vendor_id={row[2]:<3} date={row[3]}  {row[-1]}\n")

print("Wrote db/scenario_index.txt")
