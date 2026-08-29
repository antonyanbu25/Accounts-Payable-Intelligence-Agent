# Recruiter Evaluation — Context Sheet

Operational facts only. This file deliberately contains no functional
description of what the system does, what it's good at, or what to test —
that must come from the assignment sheet alone (see the prompt).

## Deployed system (test this, not the code)

- **Frontend (start here):** https://ap-agent-frontend-865q.onrender.com
- The frontend has three tabs: a conversational Q&A tab, a "validate a
  payment advice" tab, and a "browse mock data" tab. A recruiter reviewing
  a live demo would have access to all three — you may use the mock-data
  tab the same way a real reviewer would, to look up a real record before
  asking about it.
- The system reasons over three underlying data sources (a procurement
  system, a vendor/payments system, and a set of tax/regulatory documents).
  You do not need their schemas to test as a user — the assignment sheet
  and the live UI are your only required inputs for designing test cases.

## Repository (search this only after a failure, not before)

- Local path: `/Users/tony/Projects/Upskill Tracker/DevRev assignment`
- Remote: https://github.com/antonyanbu25/Accounts-Payable-Intelligence-Agent
- Current default branch: `main`

## Git workflow rules (hard constraints)

- Do not modify, commit, or push anything during evaluation. Read-only
  until the user explicitly asks for changes in a later message.
- If and only if the user later asks you to implement one or more of your
  recommendations:
  1. Create a new branch off the current `main` — do not work on `main`
     directly. Name it descriptively, e.g. `recruiter-eval-fixes`.
  2. Make the changes on that branch, with real commits.
  3. Do not push the branch, and do not merge it into `main`, unless the
     user separately asks you to.
  4. Report the branch name and a summary of what changed so the user can
     review it (e.g. `git diff main..<branch>`) before deciding to merge.

## Output expectations

- Produce a single written report (markdown), not a running commentary.
- Every test scenario needs: what you did, what you expected (per the
  assignment sheet), what actually happened (evidence — quote the UI
  response, or describe/screenshot what you saw), and pass/fail/partial.
- Cite the assignment sheet's own stated evaluation criteria for the score
  — do not invent a rubric. If the sheet doesn't state one explicitly,
  say so and use the most reasonable reading of what it emphasizes.
