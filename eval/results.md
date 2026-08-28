# Eval Results

**14/15 passed**, run against the live hosted system, not isolated unit tests.

| Case | Type | Result | Detail |
|---|---|---|---|
| T1-partial-payment | tier1 | ✅ PASS | OK |
| T1-applied-advance | tier1 | ✅ PASS | OK |
| T1-credit-note | tier1 | ❌ FAIL | request failed: Expecting value: line 1 column 1 (char 0) |
| T1-tds-with-gst-igst | tier1 | ✅ PASS | OK |
| T1-effective-date-pre | tier1 | ✅ PASS | OK |
| T1-effective-date-post | tier1 | ✅ PASS | OK |
| T1-authority-conflict | tier1 | ✅ PASS | OK |
| T1-no-rule-conflict | tier1 | ✅ PASS | OK |
| T1-vendor-stated-superseded-rate | tier1 | ✅ PASS | OK |
| T1-igst-split-demo | tier1 | ✅ PASS | OK |
| T1-cgst-sgst-split-demo | tier1 | ✅ PASS | OK |
| A1-nonexistent-vendor | adversarial | ✅ PASS | OK (no balance fabricated) |
| A2-pre-change-date | adversarial | ✅ PASS | OK |
| A3-false-positive-check | adversarial | ✅ PASS | OK |
| A4-multi-error-advice | adversarial | ✅ PASS | OK |
