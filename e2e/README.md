# End-to-end test suite

The project has two complementary end-to-end suites:

- `service/tests/test_api_workflow_e2e.py` is the fast, deterministic suite. It calls the real FastAPI application, real tax corpus, `/tax-lookup`, `/compute`, and `/diff` endpoints in the same order as the workflow. It runs locally on every change and makes no network calls.
- `e2e/test_live_workflows.py` is the deployment smoke suite. It calls the same `/webhook/uc1-ask` + `/webhook/uc2-validate` routes as Streamlit -- now served directly by the FastAPI orchestrator -- so it validates the deployed path: orchestration -> Postgres -> compute/diff -> response contract. It only performs `SELECT`-style retrieval and must be explicitly enabled.

## Run before deployment

From the repository root:

```bash
python3 -m pytest service/tests -v
```

This runs unit tests plus the deterministic API workflow suite.

## Run after deployment

Set the deployed FastAPI service's public base URL, without a trailing slash. This URL is not a database credential and is safe to place in a local shell command.

```bash
RUN_LIVE_E2E=1 \
E2E_ORCHESTRATOR_BASE_URL=https://ap-agent-service.onrender.com \
python3 -m pytest e2e -v -m live_e2e
```

The live suite verifies:

1. UC1 recomputes a vendor's stale tax rate from Source C.
2. UC1 uses the invoice date when selecting an effective-dated rule.
3. UC2 accepts a fully correct advice rather than producing a false positive.
4. UC2 identifies multiple independent errors in one advice.
5. UC2 blocks validation when the PO and invoice categories conflict.
6. UC2 returns a clear not-found response for an unknown invoice.

## Recruiter probes

`service/tests/test_recruiter_probes_e2e.py` adds the deliberate follow-up
cases that are easy to miss in a polished happy-path demo:

1. A tax rule absent from the corpus must return `not_found`, never a plausible
   current rate.
2. An advice with perfect arithmetic must still show a payment-release hold if
   its PO is cancelled.
3. A submitted category that contradicts the invoice cannot be ignored merely
   because the monetary fields happen to match.
4. The narration guard must reject an unsupported currency amount even when
   the sentence otherwise sounds credible.

Never set `RUN_LIVE_E2E=1` against a production database containing real financial data. This assessment uses synthetic seeded data only.
