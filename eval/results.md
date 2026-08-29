# Eval Results

**24/25 passed**, run against the live hosted system, not isolated unit tests.

| Case | Type | Result | Detail |
|---|---|---|---|
| T1-partial-payment | tier1 | ✅ PASS | OK |
| T1-applied-advance | tier1 | ✅ PASS | OK |
| T1-credit-note | tier1 | ✅ PASS | OK |
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
| M1-invoice-pronoun-continuity | multi_turn | ✅ PASS | OK |
| M2-topic-change-no-false-continuity | multi_turn | ❌ FAIL | gst rate: expected 18.0, got None; expected note to mention 'category-level answer', got note=None -- a vendor/invoice got resolved when the question was actually vendor-less (false continuity from history) [info: narration fallback used, guard=None -- numbers still verified correct] |
| M3-recency-over-depth | multi_turn | ✅ PASS | OK |
| M4-invoice-continuity-same-vendor | multi_turn | ✅ PASS | OK |
| V1-vendor-details-basic | vendor_lookup | ✅ PASS | OK |
| V2-vendor-details-state-discrepancy | vendor_lookup | ✅ PASS | OK |
| N1-draft-correct-rate | uc2_new_invoice | ✅ PASS | OK |
| N2-draft-stale-rate | uc2_new_invoice | ✅ PASS | OK |
| C1-skyline-tax-rate-change | comparison | ✅ PASS | OK |
| C2-comparison-one-invoice-missing | comparison | ✅ PASS | OK |
