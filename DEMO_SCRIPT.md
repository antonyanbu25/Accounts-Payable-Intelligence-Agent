# Accounts Payable Intelligence Agent - Demo Script

**Target duration:** ~10 minutes (drafted to land at or under the assignment's stated 6-10 minute cap — timings below are estimates; read aloud once, timed, before recording, and trim per the "If time is tight" note at the end if needed)
**Audience:** DevRev Applied AI Strategist interview panel
**Live cases:** UC1 stale-rate detection, UC1 multi-turn (continuity + false-continuity), UC1 category-conflict refusal, UC2 multi-error validation, UC2 manual form, UC2 CSV paste

---

## Pre-recording checklist

- Open the deployed Streamlit app in a clean browser window, no prior chat history.
- Keep the **Validate a Payment Advice** tab ready in a second browser tab or easily reachable.
- Confirm both Render services are warm (hit the app once before recording so nothing spins up on camera).
- Walk every live moment below against the actual production app immediately before recording — not from memory: the loading captions, INV-9, INV-17 (stale-rate reveal), the Furniture false-continuity question, INV-16 (refusal), UC2's multi-error INV-4 example plus the step-by-step expander, the manual-form invoice picker, and a real CSV paste.
- Do not show passwords, API keys, Render environment variables, or raw database credentials.

---

## 0:00-0:50 — AP domain knowledge

**On screen:** app landing page, nothing clicked yet.

> To build this, I first had to learn the accounts payable domain end to end — procurement and finance sit outside my CX background, which is deliberately the point of this assignment. The core process is: requisition, approval, purchase order, goods or service receipt, invoice, a three-way match, tax determination, payment advice, approval, and payment. That's the decomposition I designed around — not an arbitrary set of screens, but a real process map with two identifiable failure points: information fragmented across three separate systems, and manual reconciliation errors slipping through before a payment is released. Those two failure points are exactly the two use cases.
>
> Two tax mechanics matter most for what I built. GST — Goods and Services Tax — has a rate that depends only on HSN or SAC classification and effective date, the same nationwide, but its *split* into CGST plus SGST for an intra-state transaction versus IGST for an inter-state one depends separately on the vendor's registered state relative to the purchasing office's state. TDS — Tax Deducted at Source — is withheld by the payer on categories like professional or technical services under Section 194J, calculated on the base amount *excluding* GST, per CBDT Circular 23 of 2017. And GSTIN, the GST identification number, encodes a vendor's registered state and is the legally authoritative source for tax jurisdiction — more authoritative than whatever a vendor typed into an onboarding form.

---

## 0:50-2:20 — Understanding the project: data model, scope, and assumptions

> The assignment asks for two things. First, a conversational agent that answers a natural-language AP question with a traceable, grounded answer — not an approximation. Second, a validator that independently reconstructs a payment figure and checks a human-prepared advice against it, rather than trusting the human's numbers.
>
> On the schema: Source A, the procurement portal, holds offices, requisitions, purchase orders, and goods receipts — a structured workflow from request to fulfillment. Source B, the SAP-style finance system, holds vendor master data, invoices, advances, payments, and credit notes. I deliberately modeled advances, payments, and credit notes as their own separate tables rather than one running balance, specifically so a partial payment or an applied advance is individually traceable, not folded into an opaque number — you'll see that live in a moment. Source C is a small corpus of synthetic regulatory documents, which I'll come back to in a minute.
>
> In scope: GST and TDS computation, the three-way match, payment eligibility, and two genuinely different kinds of source conflict — one with a defensible authority rule, one without.
>
> Deliberately out of scope, and documented as such: the reverse charge mechanism, approval-tier escalation, duplicate invoice detection, a fuller time-of-supply date model, aggregating a vendor's balance across multiple invoices, and ranking vendors against each other. None of these serve the two defined use cases directly — I'd rather state a clean boundary than half-build something.
>
> Key assumptions: three sample offices in states matching DevRev's own real footprint, five purchase categories chosen to cover both a taxed and an untaxed-for-TDS category, and invoice date as the sole effective-date key rather than a fuller time-of-supply model.

**On screen:** switch to Browse Mock Data briefly — point at Vendors, Invoices, and the Advances/Payments/Credit Notes tables specifically, since they were just named.

---

## 2:20-2:45 — Key highlights of the system built

> Two things worth highlighting before the live demo. UC2 never sees the accountant's submitted numbers until *after* it's already computed its own answer — a genuine blind reconstruction, not a review. And the system knows when to say no: a real source conflict with no defensible rule to resolve it gets refused, not guessed at.

---

## 2:45-4:05 — Key architectural decisions and why

> On architecture: Streamlit handles the UI, Postgres holds the two structured sources, and a Python FastAPI service owns everything that has to be deterministic — tax rate and split selection, all arithmetic, the field-by-field diff, and the numeral guard. Claude has exactly two roles: parsing a question into a fixed schema, and narrating a result. It never writes SQL, never picks a tax rate, never touches money.
>
> Second, citations aren't decoration here — they're a hard contract. Every answer separates its narrative from a structured evidence panel: source system, record, and for tax, the exact quoted clause. And that contract is enforced in code, not just asked for in a prompt — a numeral guard checks every number in the generated narrative against the computed result, and discards the narration if it doesn't match. In finance, a plausible-sounding wrong number is worse than no answer at all; a citation is what lets a human verify without redoing the work themselves.
>
> And third, the conflict-resolution policy itself is a deliberate design decision, not just a fallback. When two sources disagree and there's a defensible rule — GSTIN is always the authoritative source for a vendor's tax state, for instance — the system resolves it silently and just flags the losing value as a data-quality note. But when sources disagree and there's genuinely no rule to pick one — a purchase order and an invoice disagreeing on category, say — it refuses every tax-dependent figure together rather than guess, states only what's tax-independent, and names both candidates. Apply a rule when one exists, refuse when none does, never guess in either direction — that's the policy the whole system follows, and you'll see it live in a moment.

---

## 4:05-4:50 — How the tax documents were structured and made retrievable

> Source C deliberately isn't a clean rate table — it's 13 synthetic, prose regulatory documents, because that's what real tax circulars actually look like. Each one is tagged with metadata during ingestion: document type, HSN or SAC scope, state scope, and an effective-date window. That metadata is what makes retrieval deterministic rather than semantic — the system filters to an exact code match and a date window *before* anything resembling similarity search runs. Vector retrieval only ever surfaces the explanatory clause from a document that's already been identified this way; it never picks the rate itself.
>
> I deliberately seeded a few adversarial documents to stress-test ingestion, not retrieval: a pair of GST circulars spanning the actual 22 September 2025 rate change, near-duplicate distractors for adjacent categories and other states, and one circular written as a cross-referencing amendment with its scope and effective date buried mid-paragraph instead of in a header — the hardest one to parse correctly.

**On screen:** stay on Ask a Question, or briefly show a tax document if convenient — moving into the live demo next.

---

## 4:50-7:35 — Live demo 1: UC1

**On screen:** Browse Mock Data tab.

> Before asking anything, here's the real data behind every answer — vendors, invoices, purchase orders, tax documents. Nothing here is synthesized at answer time.

**Switch to Ask a Question. Enter:**
```text
How much is pending for TechNova Software Solutions on invoice INV-9?
```

> Watch the loading state — it names each source as it's checked: vendor details, then the purchase order, then tax treatment. That's not decorative; those are the three actual sources this answer draws from.

**While/after it renders:**

> The evidence panel shows the exact figures and the applied tax rate. Expand the tax document sources — this is the exact clause, from the exact document, using the deterministic lookup I just described.

**Multi-turn continuity, and the strongest cross-source reasoning moment. Enter:**
```text
What about invoice 17 for the same vendor — is it correctly taxed?
```

> Two things at once here. First, it resolves "the same vendor" from context, without me repeating the name. Second — look at the answer itself: TechNova's own invoice states an 18 percent GST rate, but the invoice date falls after the rate change, so the current rule is actually 12 percent. The agent doesn't trust the vendor-stated figure just because it's sitting right there in the source record — it traces the real rate from the tax circular, shows both numbers side by side, and explains that the vendor's invoice is internally consistent with itself but still wrong. That's genuine cross-source reconciliation — structured invoice data checked against unstructured regulatory text — not two sources being treated independently and hoping they agree.

**Multi-turn — the harder, more important continuity case. Enter:**
```text
What GST rate applies to Furniture purchases in general?
```

> This is a completely unrelated, vendor-less question asked right after a specific-vendor exchange. It does *not* drag TechNova into this answer just because a vendor exists in the conversation history — false continuity is worse than a missed one. This exact path had a real bug I found and fixed this week: the system was refusing this question outright when history was present, even though it correctly extracted everything else. It's fixed and verified now, including against the live deployment.

**Refusal case. Enter:**
```text
What tax treatment applies to Skyline Software Labs invoice INV-16?
```

> The purchase order and the invoice disagree on category here — Software versus Services — and there's no rule to say which one's authoritative. Rather than guess, it states only what's tax-independent, the pre-tax ledger position, and refuses GST, TDS, and net disbursement together, naming both candidates. A clearly explained refusal beats a confident wrong answer.

---

## 7:35-9:20 — Live demo 2: UC2

**Open:** Validate a Payment Advice → **Use an example** → pick `Multi-error advice (INV-4)` → **Validate this advice**

> The system never sees these submitted numbers until it's already independently computed its own. The field-by-field table shows exactly which value was claimed, which is correct, and why — this one has two simultaneous errors, and both are caught and named separately, not just a single "something's wrong."

**Expand Step-by-step calculation:**

> And this is new — every step of the arithmetic is shown, not just the final verdict: the rate applied, the formula, the split, the waterfall down to net disbursement. A reviewer doesn't have to trust a black box.

**Switch to Manual form. Search and pick an invoice from the picker:**

> A real accountant searches for an invoice by name or amount, not by typing a raw internal ID.

**Switch to Paste JSON or CSV row. Paste a one-row CSV:**

> And this — pasting a single CSV row — is closer to what a real payment-advice export out of an accounting system looks like than hand-typed JSON. Nothing here routes through an LLM to extract the numbers; both this and the manual form are deterministically parsed.

---

## 9:20-10:05 — What I'd do to scale this for production

> A few things I'd add before this goes anywhere near production. Authentication and per-route rate limiting — right now both services are fully open, which is fine for a reviewed demo but not for anything real, and I've already scoped exactly what that would take. Real aggregation — summing a vendor's balance across *all* open invoices, and eventually ranking vendors by what's owed — which the current architecture doesn't support yet, but is a natural, scoped extension of the two-invoice comparison already built. Production observability: latency, cost per query, escalation rate, and the percentage of advices validated without human review. And once real invoices arrive as PDFs rather than structured exports, a properly gated extraction step — deliberately excluded today, because it would touch the one guarantee UC2 exists to protect: nothing non-deterministic ever touches the submitted numbers before they're compared. Thank you.

---

## If time is tight

Cut the "Key highlights" section (2:20-2:45) — it's mostly re-stated by the live demos anyway. Keep the domain-knowledge, scope, architecture, and Source C sections short and tight rather than cutting them entirely — the Source C section in particular is one of the assignment's explicitly required walkthrough items, not optional color. Never cut the vendor-stated-rate / cross-source reconciliation moment or the false-continuity moment in the UC1 demo, or the refusal case — they're the clearest proof of grounding discipline and the two dimensions ("grounding & accuracy," "cross-source reasoning") most directly scored by a live demo rather than by narration alone.
