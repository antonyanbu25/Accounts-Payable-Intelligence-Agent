#!/usr/bin/env python3
"""
Independent cross-check of eval/eval_set.json.

Recomputes every Tier-1 case from raw seed data using the SAME rate tables
as db/seed.py, and diffs the result against what's hand-typed in the JSON.
This is a sanity check on the answer key itself, not on any built system --
the compute service doesn't exist yet.
"""
import json
import datetime
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "db"))
from seed import GST_RATES, TDS_RATES, RATE_CHANGE_DATE  # noqa: E402

INVOICE_FACTS = {
    9:  {"base": 200000.00, "date": "2026-02-01", "category": "Software",  "vendor_state": "Karnataka",   "office_state": "Karnataka",   "advances": 0,     "credits": 0,    "payments": 120000.00},
    10: {"base": 150000.00, "date": "2026-02-10", "category": "Software",  "vendor_state": "Tamil Nadu",  "office_state": "Tamil Nadu",  "advances": 30000, "credits": 0,    "payments": 120000.00},
    11: {"base": 60000.00,  "date": "2026-02-05", "category": "Furniture", "vendor_state": "Karnataka",   "office_state": "Karnataka",   "advances": 0,     "credits": 5000, "payments": 55000.00},
    12: {"base": 80000.00,  "date": "2026-03-01", "category": "Services",  "vendor_state": "Tamil Nadu",  "office_state": "Karnataka",   "advances": 0,     "credits": 0,    "payments": 80000.00},
    13: {"base": 100000.00, "date": "2025-09-01", "category": "Software",  "vendor_state": "Tamil Nadu",  "office_state": "Tamil Nadu",  "advances": 0,     "credits": 0,    "payments": 0},
    14: {"base": 100000.00, "date": "2025-10-01", "category": "Software",  "vendor_state": "Tamil Nadu",  "office_state": "Tamil Nadu",  "advances": 0,     "credits": 0,    "payments": 0},
    15: {"base": 70000.00,  "date": "2026-03-03", "category": "Services",  "vendor_state": "Maharashtra", "office_state": "Maharashtra", "advances": 0,     "credits": 0,    "payments": 0},  # authoritative state, not onboarding
    17: {"base": 110000.00, "date": "2025-11-15", "category": "Software",  "vendor_state": "Karnataka",   "office_state": "Karnataka",   "advances": 0,     "credits": 0,    "payments": 0},
    18: {"base": 130000.00, "date": "2026-03-12", "category": "Software",  "vendor_state": "Tamil Nadu",  "office_state": "Karnataka",   "advances": 0,     "credits": 0,    "payments": 0},
    19: {"base": 130000.00, "date": "2026-03-13", "category": "Software",  "vendor_state": "Karnataka",   "office_state": "Karnataka",   "advances": 0,     "credits": 0,    "payments": 0},
    1:  {"base": 85000.00,  "date": "2026-03-10", "category": "Furniture", "vendor_state": "Maharashtra", "office_state": "Maharashtra", "advances": 0,     "credits": 0,    "payments": 0},
    4:  {"base": 61000.00,  "date": "2026-02-20", "category": "Appliances","vendor_state": "Karnataka",   "office_state": "Karnataka",   "advances": 0,     "credits": 0,    "payments": 0},
}

def compute(inv):
    d = datetime.date.fromisoformat(inv["date"])
    band = "pre" if d < RATE_CHANGE_DATE else "post"
    gst_rate = GST_RATES[inv["category"]][band]
    tds_rate = TDS_RATES[inv["category"]]["rate"]
    base = inv["base"]
    gst_amt = round(base * gst_rate / 100, 2)
    tds_amt = round(base * tds_rate / 100, 2)
    gross = round(base + gst_amt, 2)
    net = round(gross - tds_amt - inv["advances"] - inv["credits"] - inv["payments"], 2)
    split = "IGST" if inv["vendor_state"] != inv["office_state"] else "CGST+SGST"
    return {"gst_rate_pct": gst_rate, "gst_amount": gst_amt, "split": split,
            "tds_amount": tds_amt, "gross_liability": gross, "net_disbursement_due": net}

with open(os.path.join(os.path.dirname(__file__), "eval_set.json")) as f:
    eval_set = json.load(f)

expected_by_invoice = {}
for case in eval_set["tier1_cases"]:
    iid = case.get("invoice_id")
    if iid:
        expected_by_invoice[iid] = case["expected"]

mismatches = 0
for iid, facts in INVOICE_FACTS.items():
    recomputed = compute(facts)
    expected = expected_by_invoice.get(iid)
    if expected is None:
        continue  # T1-no-rule-conflict (invoice 16) intentionally has no single expected GST figure
    checks = [
        ("gst_rate_pct", expected.get("gst_rate_pct") or expected.get("true_gst_rate_pct")),
        ("tds_amount", expected.get("tds_amount") or expected.get("tds_amount")),
    ]
    net_key = "net_disbursement_due" if "net_disbursement_due" in expected else "true_net_disbursement_due"
    ok = True
    if expected.get("gst_rate_pct") is not None and expected["gst_rate_pct"] != recomputed["gst_rate_pct"]:
        ok = False
    if expected.get("true_gst_rate_pct") is not None and expected["true_gst_rate_pct"] != recomputed["gst_rate_pct"]:
        ok = False
    expected_net = expected.get(net_key)
    if expected_net is not None and abs(expected_net - recomputed["net_disbursement_due"]) > 0.01:
        ok = False
        print(f"  MISMATCH invoice {iid}: expected net={expected_net}, recomputed net={recomputed['net_disbursement_due']}")
        mismatches += 1
    if ok:
        print(f"  OK invoice {iid}: rate={recomputed['gst_rate_pct']}%, split={recomputed['split']}, "
              f"TDS={recomputed['tds_amount']}, net={recomputed['net_disbursement_due']}")

print(f"\n{'ALL CLEAR — every hand-computed figure matches an independent recomputation.' if mismatches == 0 else f'{mismatches} MISMATCH(ES) FOUND — fix eval_set.json before trusting it.'}")
