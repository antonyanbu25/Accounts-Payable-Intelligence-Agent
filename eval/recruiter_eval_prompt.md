# Prompt: Independent Recruiter-Style Evaluation

Copy everything below into a fresh session of an agentic tool that has both
(a) live browser/UI automation and (b) repo/filesystem access — the same
class of tool as a Claude Code session with browser tools. A plain
chat-only LLM cannot execute this, since Phase 1 requires actually
operating a live web app, not just reasoning about it.

Attach the assignment PDF (`DevRev_Applied-AI-Strategist_Assignment_Antony-Sagayaraj(1).PDF`,
in the repo root) to the session before sending this prompt.

---

You are acting as a technical recruiter/interviewer evaluating a candidate's
take-home submission. You will do this in four strict phases, in order.
Do not skip ahead or blend phases.

First, read `eval/recruiter_eval_context.md` in this repository for the
operational facts you need (deployed URL, repo location, git rules, output
format). It is deliberately free of any description of what the system
does or what to test — that comes only from the assignment sheet.

## Phase 1 — Derive your test plan from the assignment sheet alone

Read the attached assignment PDF and nothing else in this repository yet.
Do not open the README, any file under `.claude/plans/`, `DEMO_SCRIPT.md`,
or any other design/planning document — those are the team's own internal
notes and would bias an "independent" evaluation into just checking the
things they already know they built.

From the assignment sheet alone, work out:
- The use cases and requirements it actually asks for, explicitly and
  implicitly.
- The evaluation criteria / scoring dimensions it states or clearly implies.
- A realistic recruiter's test plan: not just the happy path, but the kind
  of skeptical, adversarial, and edge-case probing an experienced reviewer
  who has graded many of these actually does — ambiguous inputs, boundary
  conditions, questions the system should refuse to answer, requests that
  don't fit the obvious categories, and anything the sheet emphasizes but
  a rushed build commonly shortcuts.

Write this test plan down before you touch the live app.

## Phase 2 — Execute the test plan against the live, deployed UI

Using real browser interaction (not API calls, not reading the code) against
the deployed frontend URL in the context sheet, work through every scenario
in your Phase 1 plan exactly as a recruiter would: read what's on screen,
click through the actual tabs and controls, type real questions, try the
edge cases. For each scenario, record what you expected (per the assignment
sheet) versus what actually happened, with evidence (quote the response
text, describe or capture what rendered), and mark it pass / fail / partial.

Be an honest, skeptical tester. The goal is to surface real gaps, not to
confirm the build is good.

## Phase 3 — Root-cause every failure or shortfall

Only now — after Phase 2 is complete — search the codebase to understand
*why* each failed or partial scenario behaved the way it did. Read the
actual code/config responsible; don't guess. For each one, write a specific,
concrete recommended change (what file/behavior, and roughly how), not a
vague "improve X."

## Phase 4 — Score and report

Produce one written report with:
1. **Current score** — using the assignment sheet's own stated evaluation
   criteria as your rubric (quote what it says it's scored on), with a
   short justification per dimension and a headline score/grade.
2. **Full results table** — every scenario from Phase 1/2, pass/fail/
   partial, and the evidence.
3. **Findings and recommendations** — for each failure/gap: root cause
   (from Phase 3) and the specific recommended fix, ranked by how much it
   would move the score.
4. **Projected post-fix score** — your best estimate of the score if all
   recommendations were implemented, and why. This is an estimate only —
   do not implement anything to verify it.

## Hard constraints

- Do not modify, commit, or push any code during Phases 1–4. This is a
  read-only evaluation.
- Stop after Phase 4 and wait. Do not propose next steps as if you're about
  to start making changes.
- If, and only if, the user later replies asking you to implement one or
  more recommendations: follow the git workflow rules in
  `eval/recruiter_eval_context.md` exactly — new branch off `main`, real
  commits there, never work on `main` directly, don't push or merge unless
  separately asked.
