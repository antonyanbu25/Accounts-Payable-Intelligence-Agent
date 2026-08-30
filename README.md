# Accounts Payable Intelligence Agent

A working prototype for DevRev's Applied AI Strategist take-home assignment: an
agent that reasons across three siloed mock data sources — a structured
Procurement Portal, an SAP-style Vendor & Payments DB, and unstructured
tax/regulatory documents — to (1) answer natural-language AP questions with a
traceable, evidence-backed answer, and (2) independently reconstruct and
validate a human-prepared payment advice before it's paid.

**Live app:** https://ap-agent-frontend-865q.onrender.com
(two services — Streamlit, FastAPI — first request after idle may
take a few seconds while the instance wakes; see [Hosting](#hosting--running-it-yourself))

This document is the written assumption/decision log the assignment asks
for. The video recording covers the *what* and *why* at a walkthrough level;
this covers every specific judgment call in enough depth to defend under
follow-up questioning.

---

## The two use cases

- **UC1 — conversational lookup.** A natural-language question about a
  vendor balance or tax treatment. Answered by resolving records across
  Source A and B, determining the applicable tax rule from Source C, and
  computing the result — never by an LLM guessing at a number.
- **UC2 — payment advice validation.** An accountant submits a structured
  payment advice (invoice reference, GST/TDS breakdown, net payable). The
  system computes the correct answer *from scratch, without ever reading the
  submitted numbers first* (a genuine blind reconstruction, not a review of
  the human's math), then diffs field by field and states exactly where and
  why it diverges.

Both use cases share one grounding contract: every number in the narration
is checked, in code, against the structured computed result before it's
shown (the "numeral guard," see below). If a generated sentence contains a
number the compute engine didn't produce, the narration is discarded and a
templated response is shown instead. This is enforced by a regex pass over
the LLM's output, not a prompt instruction the model could ignore.

---

## Architecture

```
Streamlit  →  FastAPI (native orchestration, /webhook/uc1-ask + /webhook/uc2-validate)
                 ├─ Claude: parse question → fixed intent schema (never SQL, never a number)
                 ├─ Postgres: vendor resolution (pg_trgm) + raw record retrieval
                 ├─ conflict/authority resolution (before tax lookup — see below)
                 ├─ /tax-lookup: deterministic HSN/SAC + jurisdiction + effective-date rule selection
                 ├─ /compute (or /diff for UC2): all arithmetic, in Python, Decimal-rounded
                 ├─ Claude: narrate the structured result (narration only, never introduces a value)
                 └─ numeral guard: every number in the narrative must exist in the structured result,
                    or the narration is discarded and a templated fallback is shown instead
```

Money math and rate selection live in a pure-Python service
([service/compute.py](service/compute.py), [service/tax_lookup.py](service/tax_lookup.py)) with 77
unit/integration tests — not in a prompt. `/tax-lookup`, `/compute`, `/diff`, and the numeral guard
are plain FastAPI routes, called as direct in-process Python function calls by the orchestration
layer (`service/uc1_orchestration.py`, `service/uc2_orchestration.py`) — no network hop between them.

**This wasn't the original design — it's the result of removing a real component that stopped
earning its complexity.** The first working version orchestrated both use cases through a
self-hosted n8n workflow engine, chosen specifically to give a non-technical AP team a visible,
editable canvas separate from the Python codebase. In practice, across the life of this project,
every single change to either workflow — including two real production bug fixes — was made by
editing a Python script that deletes and rebuilds the whole n8n workflow via its REST API, never by
opening the n8n editor itself. That's strictly worse than writing the same logic directly: no type
checking, no unit tests, JS expressions embedded in Python string literals, and n8n's own
multi-item execution model (`.first()`/`.all()`/`.item` pairing) fighting the problem rather than
solving it. It also caused two real deploy-time bugs this session, because n8n workflows live in
n8n's own storage — invisible to a normal `git push` — so a merged, working fix could sit silently
un-deployed. The editable-canvas rationale never actually got used in practice, so n8n was removed
in favor of the architecture above; the two use cases' actual reasoning (intent parsing, retrieval,
narration, the numeral guard) is unchanged, only the hop between them is gone. The n8n-based version
is preserved on the `n8n-based` git branch as a fallback, not deleted outright.

**Why not a knowledge graph.** DevRev's own product thesis centers on a
knowledge graph unifying structured and unstructured data. At 3 sources with
fixed, known relationship paths, relational joins + metadata-filtered
retrieval *are* the correct implementation of that same idea — a graph
becomes the right tool once relationship paths aren't known in advance and
cross-system entity resolution (not lookup) is the hard problem. That's a
scale boundary, not a rejection of the idea.

---

## Data model

**Source A — Procurement Portal** (Postgres, queried natively by the FastAPI service):
`office`, `vendor_onboarding`, `requisition`, `purchase_order`, `receipt`.

**Source B — SAP-style Vendor & Payments DB** (Postgres): `vendor_master`
(GSTIN, registered state, status), `invoice` (base amount, *stated* GST —
evidence only, status), `advance`, `payment`, `credit_note`.

**Source C — Tax & Regulatory Docs**: 13 synthetic, explicitly-labeled
mock GST/TDS circulars (see [tax_docs/README.md](tax_docs/README.md)) —
prose documents, not a clean rate table, deliberately including an
effective-dated pair spanning a real rate-change date, near-duplicate
distractors, and one document written as a cross-referencing amendment with
its scope and date buried mid-prose rather than in a header.

Full DDL: [db/schema.sql](db/schema.sql). Seed generator (15 vendors, 26
invoices, majority clean/unremarkable, edge cases concentrated in a curated
subset): [db/seed.py](db/seed.py).

**Why outstanding balance is never a stored field.** It's computed live from
invoice + tax + advances + credits + payments every time, so it can never go
stale and every component is individually traceable in the evidence panel.

**Why HSN/SAC, not a free-text category label, drives tax lookup.** A
free-text label ("software services") is exactly the kind of thing an LLM
could paraphrase inconsistently. HSN/SAC codes are the actual real-world
legal basis for GST rates in India, and using them as the lookup key makes
retrieval a deterministic exact-match problem instead of a semantic one.

---

## India GST/TDS context (for a non-specialist reader)

- **GST** (Goods and Services Tax) is charged on top of the base amount.
  Its *rate* depends only on the HSN/SAC classification of what was
  purchased and the effective date — it is the same rate nationwide. Its
  *split* into CGST+SGST (intra-state) vs. IGST (inter-state) depends
  separately on whether the vendor's registered state matches the
  purchasing office's state (place-of-supply rule) — rate and split are two
  independent questions, both wired through to `/compute` and the UC2 diff.
- **TDS** (Tax Deducted at Source) is withheld by the payer on certain
  categories of spend (e.g. professional/technical services under Section
  194J), calculated on the base amount **excluding** GST — this is a real
  rule (CBDT Circular 23/2017), not a project-specific choice.
- **GSTIN** (GST Identification Number) encodes the vendor's registered
  state in its first two digits, and is the legally authoritative source
  for a vendor's tax jurisdiction — more authoritative than whatever a
  vendor typed into an onboarding form.

---

## Assumption & Decision Log

Every entry below is a real judgment call made during this build, with the
reasoning, not just the choice.

**Sample-office scope (3 offices, 3 states).** Karnataka (Bangalore),
Maharashtra (Mumbai), Tamil Nadu (Chennai) — chosen to match DevRev's own
real India office footprint (verified via web search, not guessed), and
because 3 distinct states is the minimum needed to demonstrate both the
intra-state (CGST+SGST) and inter-state (IGST) split paths plus a
vendor-state authority conflict.

**Category scope (5 categories).** Furniture, Software, Services, Food,
Appliances — chosen to cover: a good with TDS (none, per rule) vs. a
service with TDS; a category whose rate changed on 22-Sep-2025 (Software,
Food) vs. one that didn't (Furniture, Appliances, Services); and enough
spread to make the category-conflict and effective-date cases meaningful
rather than contrived.

**3-way-match tolerance: greater of ₹100 or 2% of PO amount, tax-exclusive
only.** Chosen (not given) — a flat rupee tolerance alone would be too
strict on large POs and too loose on small ones; a percentage-only
tolerance would flag trivial rounding differences on small POs. Comparing
`PO.po_amount` / `Receipt.received_amount` / `Invoice.base_amount` — never a
GST-inclusive figure — matters because comparing tax-inclusive amounts
would flag a false mismatch any time GST computation legitimately differs
from what a vendor's invoice assumed (exactly the stale-rate scenario this
system is built to catch). Implementation: [service/compute.py](service/compute.py) `check_three_way_match()`.

**`invoice_date` as the sole effective-date key.** A fuller model would use
a separate time-of-supply date (which can legally differ from invoice date
under specific triggering events). Documented simplification, not an
oversight — see Out of Scope below.

**TDS on base amount excluding GST.** Real rule (CBDT Circular 23/2017), not
a project choice — implemented once, in [service/compute.py](service/compute.py) `compute_tds()`,
and the underlying circular is itself one of the ingestible Source C
documents ([tax_docs/tds_gst_exclusion_rule.md](tax_docs/tds_gst_exclusion_rule.md)) rather than hardcoded prose.

**GSTIN as the authority rule for vendor-state conflicts; PO-vs-invoice
category as the deliberate no-rule counter-example.** These two conflict
cases were seeded specifically to be different in kind, not just different
in content:
- Vendor state (Source A's onboarding-submitted state vs. Source B's
  GSTIN-derived state) has a real, defensible legal answer — GSTIN always
  wins — so the system resolves it silently and flags the losing value as a
  data-quality note, never refuses ([T1-authority-conflict](eval/eval_set.json)).
- PO category vs. invoice category has no such rule — nothing makes one
  more authoritative than the other — so the system refuses every
  tax-dependent figure together (GST, TDS, gross liability, net
  disbursement), states only the tax-independent "pre-tax ledger position,"
  and names both candidate outcomes rather than picking one
  ([T1-no-rule-conflict](eval/eval_set.json)). This distinction — apply a rule when one exists,
  refuse when none does, never guess in either direction — is the conflict
  policy the whole system follows.

**`gst_rate_stated` (the vendor's own invoice figure) is evidence-only,
never a `/compute` input.** The seeded three-way-tension case
([T1-vendor-stated-superseded-rate](eval/eval_set.json)) exists specifically to prove this: an invoice
dated after the 22-Sep-2025 rate change where the vendor's own invoice still
states the old 18% rate. The correct answer (12%) traces to the current
circular, not to what the vendor billed — and the system explicitly shows
both, so a reviewer can see the vendor's invoice is internally consistent
with itself but still wrong.

**The numeral guard is a code-level check, not a prompt instruction.**
Every number appearing in LLM-generated narration is extracted by regex and
checked against the structured computed result; a mismatch discards the
narration and falls back to a templated response built directly from the
structured fields. This was deliberately tested against real narration text
(not just designed): live eval runs against the hosted deployment have
directly observed it triggering and recovering correctly (e.g. on
`A4-multi-error-advice`, when Claude's phrasing that run happened to
include a number outside the structured result) — real fallback behavior,
not just an untested happy-path claim. Whether it fires on any given run
depends on the LLM's exact phrasing, so it isn't guaranteed on every pass,
but the mechanism has been proven live, not just designed and assumed.

**UC2's three input modes** (pick a pre-baked example / manual structured
form / paste JSON / upload a JSON or one-row CSV file). No mode routes
through an LLM to extract numbers — all are deterministically parsed. The
file-upload path accepts a one-row CSV specifically because that's closer
to what a real payment-advice export looks like than hand-typed JSON.

**Tiering — what's demoed live vs. modeled but not demoed.** Both tiers are
fully built and covered by the automated test suite; the split below is
purely about what's *shown live* in the ~9-minute recording, and is a
deliberate scope choice, not an omission:
- Demoed live: the stale-vendor-rate case (grounding under a plausible-but-
  wrong invoice), the no-rule category conflict (refusal discipline), and
  UC2's blind-reconstruction diff.
- Modeled, tested, mentioned but not demoed live: unapplied-advance
  advisory, non-PO/maverick spend, a price-mismatch 3-way-match failure,
  vendor-name disambiguation, blocked-vendor/cancelled-PO eligibility
  gating, and the retrieval distractor documents (including the
  hard-to-ingest amendment). Time in a ~9-minute recording is the
  constraint, not confidence in these paths — see [eval/eval_set.json](eval/eval_set.json) and
  [eval/recruiter_probe_cases.md](eval/recruiter_probe_cases.md) for how each is actually verified.

**The Day-3-fallback / Day-4-extension triggers, and why neither fired.**
The original plan set two honest bail-out points: if n8n wasn't working
end-to-end by end of Day 3, fall back to a direct Streamlit→FastAPI path
(skipping n8n) rather than fake a demo; if UC1+UC2 still weren't working
end-to-end by end of Day 4 even via that fallback, request a real extension
rather than ship a shallow build. Recorded here because a plan that
pre-commits to visible failure conditions is itself part of the signal this
process was built to produce, whether or not either one is ever hit — in
this build, neither was: n8n reached both use cases working end-to-end
within the original timeline.

**Out of scope, explicitly, and why:**
- *Reverse charge mechanism* — a real GST mechanism where the buyer
  remits tax instead of the vendor, but not triggered by anything in this
  system's category scope; including it would add a rule path with no
  seeded case to justify it.
- *Approval-tier escalation logic* — doesn't serve either defined use case
  (UC1 answers questions, UC2 validates advices already produced by a
  human); adding it would be scope creep toward a different problem
  (workflow automation) than the one asked for.
- *Duplicate invoice detection* — a real and valuable AP control, but a
  distinct problem from cross-source reasoning and tax correctness, which
  is what this assignment specifically tests.
- *A fuller time-of-supply date model* — real GST law has specific rules
  for when time-of-supply differs from invoice date; using invoice_date as
  the sole effective-date key is a documented simplification, not a claim
  that it's always legally correct.
- *Multi-invoice aggregation* — "what's our total outstanding across all
  of TechNova's open invoices" is not answerable today. When no specific
  invoice is named, the system resolves exactly one (the vendor's most
  recent) and explicitly discloses that it's not the total, rather than
  silently under-reporting — but it genuinely cannot sum across invoices.
  UC1's comparison feature is deliberately scoped to exactly 2 named
  invoices (for detecting a tax-rate difference between them), not an
  aggregate over an arbitrary set; naming a 3rd invoice for comparison is
  silently dropped with a note, not summed in. A real fix is a new,
  generalized fan-out over N invoices per vendor — the same shape of work
  as the 2-invoice comparison feature, just uncapped and summed instead of
  diffed.
- *Cross-vendor questions* ("which vendor owes us the most") — every
  question resolves to exactly one vendor; there's no ranking or
  comparison across vendors. This depends on the multi-invoice
  aggregation above as a prerequisite, plus a new ranked-list response
  shape nothing in the current UI has a precedent for.

---

## Verification

- **Unit/integration tests:** 77 passing (`python3 -m pytest service/tests/ -q`) —
  compute engine, tax lookup, diff engine, narration guard, UC1/UC2
  orchestration, and full pipeline end-to-end, run against the
  deterministic Python layer directly.
- **Live eval suite, against the hosted deployment (not local, not mocked):**

  **25/25 passed** — [eval/results.md](eval/results.md), generated by [eval/run_eval.py](eval/run_eval.py)
  run with `ORCHESTRATOR_BASE_URL` pointed at the live FastAPI service.
  Covers 11 Tier-1 cases (partial payment, applied advance, credit note,
  TDS alongside GST, both sides of the effective-date change, both
  conflict types, the stale-vendor-rate case, and both GST split types),
  4 adversarial cases (nonexistent vendor, pre-change-date question, a
  false-positive check where the accountant's advice is entirely correct,
  and a multi-error advice), 4 multi-turn conversational cases (pronoun
  continuity, topic-change with no false continuity, recency-over-depth,
  and invoice continuity for the same vendor), 2 vendor-lookup cases,
  2 draft/unrecorded-invoice cases, and 2 multi-invoice comparison cases.

  Two of those are worth being explicit about rather than glossing over:
  - The narration guard has been directly observed triggering its
    fallback in this suite's own testing — a generated sentence included
    a number not present in the structured result, the guard caught it,
    discarded the narration, and served the templated response instead.
    The underlying computed result (independent of any LLM) was still
    verified correct in every case. This is the safety net working as
    designed, not a bug.
  - `M2-topic-change-no-false-continuity` — the suite's own most
    important multi-turn case, by design (its own note: *"false
    continuity is worse than missed continuity"*) — failed on two
    separate live runs before being fixed. A vendor-less follow-up
    question asked right after a specific-invoice exchange was correctly
    extracting an empty vendor and the right category (no false
    continuity — the anti-carryover instructions were working), but the
    top-level intent classification flipped to "unsupported" only when
    history was present, refusing an answerable question outright. Root
    cause: the intent field's own description restricted it to "a
    specific invoice or transaction," which excluded a category-general
    question by its own wording. Fixed by correcting that description
    and adding a code-level backstop (a correctly-populated category
    signal now overrides a wrongly-classified intent, rather than
    depending on prompt compliance alone) — verified 25/25 twice locally
    and again against the live deployment after the fix shipped.
- **Live end-to-end contract tests, against the hosted deployment:**
  6/6 passed (`RUN_LIVE_E2E=1 E2E_ORCHESTRATOR_BASE_URL=https://ap-agent-service.onrender.com
  pytest e2e -v -m live_e2e`) — [e2e/test_live_workflows.py](e2e/test_live_workflows.py). Complements the golden-value
  eval suite above with response-contract checks against the real deployed
  Postgres/FastAPI stack: the stale-vendor-rate recomputation, the
  effective-date boundary, a correct UC2 advice being accepted, a
  multi-error advice reporting all of its errors, the category-conflict
  refusal blocking validation, and an unknown invoice reference producing a
  clean "not found" rather than an error.
- **Recruiter-style adversarial probes:** 10 additional cases designed
  from an interviewer's-eye view of where candidates typically cut corners
  ([eval/recruiter_probe_cases.md](eval/recruiter_probe_cases.md)) — including the most serious bug this
  process actually found: UC2 initially reported only numeric correctness,
  never payment eligibility, so a numerically-perfect advice for a blocked
  vendor produced zero warning. Fixed (see `service/diff.py`'s `eligible`/
  `eligibility_reasons` fields, deliberately independent of `overall_match`)
  and covered by a regression test
  (`test_blocked_vendor_flagged_even_when_numbers_match`).

---

## Hosting / running it yourself

Two Render services, deployed from [render.yaml](render.yaml) (a Render Blueprint): the FastAPI
compute/RAG/orchestration service (`ap-agent-service`, Starter tier — 512MB is proven sufficient
once the tax-document index is pre-built and committed rather than rebuilt on every deploy), and
Streamlit (`ap-agent-frontend`, Starter tier). Both are paid Starter tier specifically to avoid Free
tier's 15-minute inactivity spin-down, not because more compute is needed. (A third service, a
self-hosted n8n container, existed earlier in this project's life and required the larger Standard
tier — its baseline memory footprint genuinely exceeded 512MB even at idle, confirmed via
production crash logs. It's been removed; see the Architecture section above for why.)

To run locally: see `service/requirements.txt` (runtime) vs.
`service/requirements-ingest.txt` (one-time local tooling for rebuilding
the tax-document search index after editing `tax_docs/`), and
`db/seed.py` to regenerate the mock data.
