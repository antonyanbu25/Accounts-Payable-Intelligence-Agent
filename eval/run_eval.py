#!/usr/bin/env python3
"""
Runs the full eval set (Tier 1 + adversarial cases) against the LIVE running
system (n8n webhooks -> FastAPI service -> real Postgres + real tax docs).
This is not a unit test against isolated code -- it's the actual system,
end to end, exactly as a user would hit it.

Writes eval/results.json (machine-readable) and eval/results.md (human-
readable "results page", per the plan).
"""
import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
N8N_BASE = os.environ.get("N8N_BASE_URL", "http://localhost:5678")

with open(os.path.join(os.path.dirname(__file__), "eval_set.json")) as f:
    EVAL_SET = json.load(f)

TOL = 1.0  # ₹1 tolerance, matches the plan's rounding-noise policy

results = []


def close(a, b, tol=TOL):
    if a is None or b is None:
        return a == b
    return abs(float(a) - float(b)) <= tol


def check_tier1(case):
    exp = case["expected"]
    try:
        resp = requests.post(f"{N8N_BASE}/webhook/uc1-ask", json={"question": case["question"]}, timeout=60)
        data = resp.json()
    except Exception as e:
        return False, f"request failed: {e}"

    ev = data.get("evidence", {})
    problems = []

    if exp.get("refused") or "candidates" in exp:
        # no-rule-conflict case: gross/net must be null, pre_tax_ledger_position must match
        if not ev.get("tax_treatment_refused"):
            problems.append("expected tax_treatment_refused=true, got false")
        if not close(ev.get("pre_tax_ledger_position"), exp.get("pre_tax_ledger_position")):
            problems.append(f"pre_tax_ledger_position: expected {exp.get('pre_tax_ledger_position')}, got {ev.get('pre_tax_ledger_position')}")
    else:
        net_key = "net_disbursement_due" if "net_disbursement_due" in exp else "true_net_disbursement_due"
        if not close(ev.get("net_disbursement_due"), exp.get(net_key)):
            problems.append(f"net_disbursement_due: expected {exp.get(net_key)}, got {ev.get('net_disbursement_due')}")
        if not close(ev.get("tds_amount"), exp.get("tds_amount")):
            problems.append(f"tds_amount: expected {exp.get('tds_amount')}, got {ev.get('tds_amount')}")

    note = "" if data.get("guard") == "passed" else f" [info: narration fallback used, guard={data.get('guard')} -- numbers still verified correct]"
    return (len(problems) == 0), ("; ".join(problems) if problems else "OK") + note


def check_a1_nonexistent_vendor(case):
    try:
        resp = requests.post(f"{N8N_BASE}/webhook/uc1-ask", json={"question": case["question"]}, timeout=60)
        data = resp.json()
    except Exception as e:
        return False, f"request failed: {e}"
    ev = data.get("evidence", {})
    # A real balance figure must NOT appear -- either evidence is empty/null, or the
    # narration clearly declines. We check that no net_disbursement_due was fabricated.
    if ev.get("net_disbursement_due") not in (None, 0):
        return False, f"fabricated a balance ({ev.get('net_disbursement_due')}) for a nonexistent vendor"
    return True, "OK (no balance fabricated)"


def check_a2_pre_change_date(case):
    """This question has no vendor -- it takes the category-only ('Hypothetical
    Path') branch, which returns /tax-lookup's response shape (gst.rate_pct,
    tds.rate_pct), not /compute's shape (there's no invoice to compute a
    balance for). Checking the rate directly, not a net_disbursement_due."""
    try:
        resp = requests.post(f"{N8N_BASE}/webhook/uc1-ask", json={"question": case["question"]}, timeout=60)
        data = resp.json()
    except Exception as e:
        return False, f"request failed: {e}"
    ev = data.get("evidence", {})
    problems = []
    if not close(ev.get("gst", {}).get("rate_pct"), 18.0):
        problems.append(f"gst rate: expected 18.0 (pre-reform), got {ev.get('gst', {}).get('rate_pct')}")
    if not close(ev.get("tds", {}).get("rate_pct"), 10.0):
        problems.append(f"tds rate: expected 10.0, got {ev.get('tds', {}).get('rate_pct')}")
    note = "" if data.get("guard") == "passed" else f" [info: narration fallback used, guard={data.get('guard')} -- rates still verified correct]"
    return (len(problems) == 0), ("; ".join(problems) if problems else "OK") + note


def check_uc2(case, expect_match):
    payload = {"invoice_id": case["invoice_id"], "submitted_advice": case["submitted_advice"]}
    try:
        resp = requests.post(f"{N8N_BASE}/webhook/uc2-validate", json=payload, timeout=60)
        data = resp.json()
    except Exception as e:
        return False, f"request failed: {e}"

    diff = data.get("diff", {})
    problems = []
    if diff.get("overall_match") != expect_match:
        problems.append(f"expected overall_match={expect_match}, got {diff.get('overall_match')}")
    if not expect_match:
        mismatched = {f["field"] for f in diff.get("fields", []) if not f["match"]}
        expected_mismatches = {f["field"] for f in case["expected_diff"]}
        missing = expected_mismatches - mismatched
        if missing:
            problems.append(f"did not catch expected mismatch(es): {missing}")
    note = "" if data.get("guard") == "passed" else f" [info: narration fallback used, guard={data.get('guard')} -- diff still verified correct]"
    return (len(problems) == 0), ("; ".join(problems) if problems else "OK") + note


print("Running eval set against the live system...\n")

for case in EVAL_SET["tier1_cases"]:
    ok, detail = check_tier1(case)
    results.append({"id": case["id"], "type": "tier1", "passed": ok, "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {case['id']:<40} {detail}")

for case in EVAL_SET["adversarial_cases"]:
    if case["id"] == "A1-nonexistent-vendor":
        ok, detail = check_a1_nonexistent_vendor(case)
    elif case["id"] == "A2-pre-change-date":
        ok, detail = check_a2_pre_change_date(case)
    elif case["id"] == "A3-false-positive-check":
        ok, detail = check_uc2(case, expect_match=True)
    elif case["id"] == "A4-multi-error-advice":
        ok, detail = check_uc2(case, expect_match=False)
    else:
        ok, detail = False, "no runner for this case id"
    results.append({"id": case["id"], "type": "adversarial", "passed": ok, "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {case['id']:<40} {detail}")

n_pass = sum(1 for r in results if r["passed"])
n_total = len(results)
print(f"\n{n_pass}/{n_total} passed")

with open(os.path.join(os.path.dirname(__file__), "results.json"), "w") as f:
    json.dump({"pass_count": n_pass, "total_count": n_total, "results": results}, f, indent=2)

with open(os.path.join(os.path.dirname(__file__), "results.md"), "w") as f:
    f.write("# Eval Results\n\n")
    f.write(f"**{n_pass}/{n_total} passed**, run against the live hosted system, "
            f"not isolated unit tests.\n\n")
    f.write("| Case | Type | Result | Detail |\n|---|---|---|---|\n")
    for r in results:
        f.write(f"| {r['id']} | {r['type']} | {'✅ PASS' if r['passed'] else '❌ FAIL'} | {r['detail']} |\n")

print("\nWrote eval/results.json and eval/results.md")
sys.exit(0 if n_pass == n_total else 1)
