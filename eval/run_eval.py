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


def check_uc2_new_invoice(case):
    """No invoice_id in the payload -- the category-only branch for a draft
    advice on something not yet recorded. Checks the response is tagged
    mode=category_only (never silently falls through to the full-record
    shape), that overall_match matches expectation, and -- for the
    mismatch case -- that the correct rate it reconstructed is the one
    actually expected, not just that SOME mismatch was found."""
    payload = {"submitted_advice": case["submitted_advice"]}
    try:
        resp = requests.post(f"{N8N_BASE}/webhook/uc2-validate", json=payload, timeout=60)
        data = resp.json()
    except Exception as e:
        return False, f"request failed: {e}"

    exp = case["expected"]
    problems = []
    if data.get("mode") != exp["mode"]:
        problems.append(f"expected mode={exp['mode']!r}, got {data.get('mode')!r}")

    diff = data.get("diff", {})
    if diff.get("overall_match") != exp["overall_match"]:
        problems.append(f"expected overall_match={exp['overall_match']}, got {diff.get('overall_match')}")

    if "correct_gst_rate_pct" in exp:
        rate_field = next((f for f in diff.get("fields", []) if f["field"] == "gst_rate_pct"), None)
        if rate_field is None:
            problems.append("no gst_rate_pct field in diff")
        elif rate_field["correct"] != exp["correct_gst_rate_pct"]:
            problems.append(f"gst_rate_pct correct value: expected {exp['correct_gst_rate_pct']}, got {rate_field['correct']}")

    note = "" if data.get("guard") == "passed" else f" [info: narration fallback used, guard={data.get('guard')} -- diff still verified correct]"
    return (len(problems) == 0), ("; ".join(problems) if problems else "OK") + note


def check_vendor_lookup(case):
    """Same history-threading pattern as check_multi_turn, but asserting
    against the vendor_lookup response shape (legal_name/vendor_status/
    registered_state/onboarding_state/invoices) rather than the balance/tax
    compute shape -- these are structurally different responses, not just
    different expected numbers."""
    HISTORY_TURNS = 3
    history = []
    data = None
    try:
        for turn_question in case["turns"]:
            resp = requests.post(f"{N8N_BASE}/webhook/uc1-ask",
                                  json={"question": turn_question, "history": history[-(HISTORY_TURNS * 2):]},
                                  timeout=60)
            data = resp.json()
            history.append({"role": "user", "content": turn_question})
            history.append({"role": "assistant", "content": data.get("narrative", data.get("message", ""))})
    except Exception as e:
        return False, f"request failed: {e}"

    exp = case["expected_final"]
    ev = data.get("evidence", {})
    problems = []

    if "invoices" not in ev:
        problems.append(f"expected a vendor_lookup response (evidence.invoices present), got keys={list(ev.keys())} "
                         f"-- likely misclassified intent or fell through to 'unsupported'/refusal")
    else:
        for key in ("legal_name", "vendor_status", "registered_state", "onboarding_state"):
            if key not in exp:
                continue
            if ev.get(key) != exp[key]:
                problems.append(f"{key}: expected {exp[key]!r}, got {ev.get(key)!r}")
        if "invoice_count" in exp and len(ev.get("invoices") or []) != exp["invoice_count"]:
            problems.append(f"invoice_count: expected {exp['invoice_count']}, got {len(ev.get('invoices') or [])}")

    note = "" if data.get("guard") == "passed" else f" [info: narration fallback used, guard={data.get('guard')} -- data still verified correct]"
    return (len(problems) == 0), ("; ".join(problems) if problems else "OK") + note


def check_comparison(case):
    """A single question naming 2 invoices for the same vendor. Asserts
    against the {comparison: true, invoices: [...]} shape from the new
    comparison branch -- structurally different again from every other
    response shape, so this checks per-invoice fields by invoice_id rather
    than assuming array order matches the question's order."""
    try:
        resp = requests.post(f"{N8N_BASE}/webhook/uc1-ask", json={"question": case["question"]}, timeout=90)
        data = resp.json()
    except Exception as e:
        return False, f"request failed: {e}"

    exp = case["expected"]
    problems = []

    if "error" in exp:
        if data.get("error") != exp["error"]:
            problems.append(f"expected error={exp['error']!r}, got {data.get('error')!r}")
        msg = data.get("message", "")
        if exp.get("missing_contains") and exp["missing_contains"] not in msg:
            problems.append(f"expected message to mention {exp['missing_contains']!r}, got {msg!r}")
        return (len(problems) == 0), ("; ".join(problems) if problems else "OK")

    if not data.get("comparison"):
        return False, f"expected a comparison response (comparison=true), got keys={list(data.keys())}"

    by_id = {inv.get("invoice_id"): inv for inv in (data.get("invoices") or [])}
    rates = []
    for exp_inv in exp.get("invoices", []):
        iid = exp_inv["invoice_id"]
        inv = by_id.get(iid)
        if inv is None:
            problems.append(f"invoice {iid}: missing from response entirely")
            continue
        ev = inv.get("evidence", {})
        gst = ev.get("gst") or {}
        tax_ev = inv.get("tax_evidence", {})
        rates.append(tax_ev.get("gst", {}).get("rate_pct"))
        checks = [
            ("base_amount", ev.get("base_amount"), exp_inv.get("base_amount")),
            ("gst_amount", gst.get("gst_amount"), exp_inv.get("gst_amount")),
            ("cgst", gst.get("cgst"), exp_inv.get("cgst")),
            ("sgst", gst.get("sgst"), exp_inv.get("sgst")),
            ("tds_amount", ev.get("tds_amount"), exp_inv.get("tds_amount")),
            ("gross_liability", ev.get("gross_liability"), exp_inv.get("gross_liability")),
            ("net_disbursement_due", ev.get("net_disbursement_due"), exp_inv.get("net_disbursement_due")),
            ("gst_rate_pct", tax_ev.get("gst", {}).get("rate_pct"), exp_inv.get("gst_rate_pct")),
            ("tds_rate_pct", tax_ev.get("tds", {}).get("rate_pct"), exp_inv.get("tds_rate_pct")),
        ]
        for field_name, actual, expected in checks:
            if expected is None:
                continue
            if not close(actual, expected):
                problems.append(f"invoice {iid} {field_name}: expected {expected}, got {actual}")
        if exp_inv.get("payment_eligibility") and ev.get("eligibility") != exp_inv["payment_eligibility"]:
            problems.append(f"invoice {iid} eligibility: expected {exp_inv['payment_eligibility']!r}, got {ev.get('eligibility')!r}")

    if exp.get("rates_differ") and len(set(rates)) < 2:
        problems.append(f"expected GST rates to differ between invoices, got the same rate for both: {rates}")

    note = "" if data.get("guard") == "passed" else f" [info: narration fallback used, guard={data.get('guard')} -- data still verified correct]"
    return (len(problems) == 0), ("; ".join(problems) if problems else "OK") + note


def check_multi_turn(case):
    """Threads conversation history between calls exactly the way
    frontend/app.py does: each turn's REAL returned narrative (not a
    hand-authored stand-in) becomes the assistant's history entry for the
    next turn, capped to the same HISTORY_TURNS*2 window. Only the FINAL
    turn's result is asserted -- earlier turns exist purely to build
    realistic context, matching how a human would actually reach the
    final question."""
    HISTORY_TURNS = 3
    history = []
    data = None
    try:
        for turn_question in case["turns"]:
            resp = requests.post(f"{N8N_BASE}/webhook/uc1-ask",
                                  json={"question": turn_question, "history": history[-(HISTORY_TURNS * 2):]},
                                  timeout=60)
            data = resp.json()
            history.append({"role": "user", "content": turn_question})
            history.append({"role": "assistant", "content": data.get("narrative", data.get("message", ""))})
    except Exception as e:
        return False, f"request failed: {e}"

    exp = case["expected_final"]
    ev = data.get("evidence", {})
    problems = []

    if exp.get("category_answer"):
        # Category/hypothetical-path response shape (no vendor resolved) --
        # see check_a2_pre_change_date for the same shape elsewhere.
        if not close(ev.get("gst", {}).get("rate_pct"), exp.get("gst_rate_pct")):
            problems.append(f"gst rate: expected {exp.get('gst_rate_pct')}, got {ev.get('gst', {}).get('rate_pct')}")
        note_fragment = exp.get("note_contains")
        if note_fragment and note_fragment not in (data.get("note") or ""):
            problems.append(f"expected note to mention '{note_fragment}', got note={data.get('note')!r} -- "
                             f"a vendor/invoice got resolved when the question was actually vendor-less "
                             f"(false continuity from history)")
    else:
        for key in ("base_amount", "gst_amount", "tds_amount", "net_disbursement_due"):
            if key not in exp:
                continue
            actual = ev.get(key) if key != "gst_amount" else ev.get("gst", {}).get("gst_amount")
            if not close(actual, exp[key]):
                problems.append(f"{key}: expected {exp[key]}, got {actual}")

    note = "" if data.get("guard") == "passed" else f" [info: narration fallback used, guard={data.get('guard')} -- numbers still verified correct]"
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

for case in EVAL_SET.get("multi_turn_cases", []):
    ok, detail = check_multi_turn(case)
    results.append({"id": case["id"], "type": "multi_turn", "passed": ok, "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {case['id']:<40} {detail}")

for case in EVAL_SET.get("vendor_lookup_cases", []):
    ok, detail = check_vendor_lookup(case)
    results.append({"id": case["id"], "type": "vendor_lookup", "passed": ok, "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {case['id']:<40} {detail}")

for case in EVAL_SET.get("uc2_new_invoice_cases", []):
    ok, detail = check_uc2_new_invoice(case)
    results.append({"id": case["id"], "type": "uc2_new_invoice", "passed": ok, "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {case['id']:<40} {detail}")

for case in EVAL_SET.get("comparison_cases", []):
    ok, detail = check_comparison(case)
    results.append({"id": case["id"], "type": "comparison", "passed": ok, "detail": detail})
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
