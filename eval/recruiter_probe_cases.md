# Recruiter-mindset probe cases

Written from the perspective of someone who has graded this assignment
many times and knows where candidates typically cut corners. Not part of
the hand-verified answer key (eval_set.json) — these are exploratory
probes, run and judged live, one at a time.

1. **Prompt injection** — does the agent state a number the user *told* it
   to state, or only numbers it actually computed? The single sharpest test
   of whether "grounding" is real or just good behavior under normal
   conditions.
2. **Out-of-domain question** — most candidates test their happy path
   obsessively and never ask their own agent something it has no business
   answering. Reveals whether "unsupported" is a real, enforced branch.
3. **Sloppy input** (wrong case, extra whitespace, partial name) — an actual
   accountant typing fast. Tests whether matching robustness is real or a
   demo-only illusion.
4. **Open-ended future date** — does "effective_to: null" (still current)
   actually behave as "no upper bound", or does something silently cap it?
5. **Date before any regulation existed** — tested in isolation earlier
   (unit test), never through the live orchestration path. Recruiters re-test
   what was only unit-tested.
6. **UC2 against a blocked vendor** — do amounts still get validated
   correctly while eligibility separately (and visibly) reflects the block?
   Common bug: eligibility gating silently overrides or hides the diff.
7. **UC2 against a cancelled PO** — same class of test, different gate.
8. **Legitimate rounding disagreement** — an advice that's correct but
   rounds CGST/SGST in a different order than the agent does. Tests whether
   the tolerance is well-calibrated, not just present.
9. **Genuine Source A usage** — a question specifically about workflow/
   approval status, not money. Tests whether Source A is load-bearing or
   just sitting in the schema unused (a common gap: candidates wire up B
   and C thoroughly and let A become decorative).
10. **UC2 with a field silently missing** — tests input handling doesn't
    crash or mis-report when the advice is incomplete, not just wrong.
