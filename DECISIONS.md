# Decisions

## What this submission is

This is an interview submission for connecting Apache Fineract to a mock ERP. Inkomoko's production ERP is Odoo. This repository does not use that Odoo, the public Odoo tree, or the Inkomoko Fineract fork. Those trees stay out of the solution so the submission can be reviewed on its own.

## Fineract UI

The browser UI is [apache/fineract-backoffice-ui](https://github.com/apache/fineract-backoffice-ui) at commit `2674193aa04359159db6b52472c1f74b0e4c46fa`, built from `deploy/Dockerfile` and tagged `fineract-backoffice-ui:2674193`. The mock ERP and the integration service stay REST-only. The UI's own `deploy/docker-compose.yml` starts PostgreSQL and `apache/fineract:latest`; that file is not used, because this submission needs MySQL and the patched `fineract:1.15.0` image. The UI container proxies `/api/` to `https://fineract:8443/fineract-provider/api` with `FINERACT_PROXY_SSL_VERIFY=off`.

Office, currency, the loan product, the least-privilege user, and sample clients are seeded in Fineract (UI or [postman/fineract-seed.json](postman/fineract-seed.json) / `scripts/seed_fineract.py`). The chart is synced first through `POST /api/accounts/sync` so 1100, 1200, and 4000 carry the patched external ids. Cash products also need an overpayment liability and a write-off expense; those use GL-2100 and GL-5000. The least-privilege user is `assessment_api` / `QwE!5rTy#9uP0` (Fineract password rules forbid consecutive repeating characters).

Disbursement is `POST /loans/external-id/{id}?command=disburse` with `actualDisbursementDate`. It is not a `/transactions?command=disburse` call; that query value is only valid on the template GET. Repayment stays on `/transactions?command=repayment`. Transaction dates must not be after the Fineract JVM calendar day.

## AI assistance

A Cursor agent (Grok) wrote the Fineract patch, the Python services, the tests, the simulation script, and these documents. The Java change is the `CREATE` path on `GLACCOUNT` (`CreateGLAccountCommandHandler` to `GLAccountWritePlatformServiceJpaRepositoryImpl`), plus changeset `0241`. The Python money mapping is `disbursement_entry` and `repayment_entry` in `integration-service/app/accounting.py`. Both are the parts a reviewer should expect to edit live.

## Fineract customization

The change is a patch against tag `1.15.0` (`d5636847ac556c30b437254c353f05526d172b97`), not a fork and not a new endpoint. `POST /v1/glaccounts` already exists and already requires `CREATE_GLACCOUNT`. The new fields `externalId` and `currencyCode` had to be added to `GLAccountCommand`, because the API re-serializes the command and the deserializer rejects unknown parameters.

A matching account returns the existing resource id. A different gl code, name, type, or currency raises `GLAccountExternalReferenceConflictException`. Fineract maps `AbstractPlatformDomainRuleException` to HTTP 403, so the conflict is 403 rather than 409. That is the equivalent of a conflict in this version. The unique key `acc_gl_external_id` is the race backstop. A request that loses that race gets the conflict as well, because the transaction is already rollback-only. A retry hits the pre-check and returns the row.

Blank `externalId` stays an ordinary create. MySQL allows multiple NULLs, so existing accounts without an external id are unaffected.

## Why not Odoo, and why not the FastAPI full-stack template

The brief asks for a Python mock with a fixed contract: `POST /api/accounts`, balances, and journals that are idempotent on `external_reference`, with the ERP itself rejecting duplicates and unbalanced lines. It also asks for failures that can be injected. A real Odoo would spend the time budget on its own API, and the follow-up exercise is a small edit to the Python mapping. `docker compose up` has to start the whole submission in one command.

The full-stack FastAPI template was declined for the same reason. The partner API needs a bearer token, two small services, and pytest. The template's frontend and auth stack are not part of the brief.

The Inkomoko Fineract tree was declined because it is a different line of development (Java 17, branch `inkomoko`). The required artifact is a patch against an official release tag, on an empty database, in an image built from that tag.

## MySQL

The plan started from PostgreSQL. The submission uses MySQL 8.4 because that was the database requested for this run, and Fineract 1.15.0 already ships `config/docker/env/fineract-mysql.env`. One server holds all four databases so the Fineract hostname stays `db`.

The official Jib image only embeds the PostgreSQL driver. MySQL Connector/J 9.7.0 is mounted at `/app/plugins`, which is the upstream extra classpath. The image was not rebuilt to hide the driver inside it.

## Sync, not a queue

Account sync is `POST /api/accounts/sync`. Money moves inside the request that asked for them. There is no Kafka, Celery, or Redis. An interrupted disbursement or repayment is recovered by the startup sweep and by `POST /api/admin/recover`. That is enough to finish or to mark `NEEDS_RECONCILIATION` after a restart, which is what the brief allows when there is no worker.

## Database constraints

| Constraint | What it protects |
| --- | --- |
| `acc_gl_external_id` | one Fineract GL per external id |
| ERP unique `external_reference` and unique account code | one account and one journal per reference |
| `idempotency_keys (operation_type, external_reference)` | one in-flight or stored result per partner operation |
| `loan_disbursements.loan_reference` | one disbursement per loan, even when the references differ |
| `clients.client_uid` | one client per partner uid |

Idempotency is the database row, not a map in memory.

## Idempotency, retries, partial failure

Same payload after `COMPLETED` returns 200 and the stored body. Same payload while the row is still between states returns 202. A different payload for the same reference returns 409. `FAILED` and `NEEDS_RECONCILIATION` are retried.

In-request backoff runs only for `fineract_down` (3 attempts, starting at 0.05 seconds). ERP failures are not retried inside the request. A timeout or a crash leaves the row in progress so the caller cannot blindly post again.

If Fineract has the transaction and the ERP write failed, the status is `NEEDS_RECONCILIATION`. The same payload, or the recovery sweep, looks up the journal by external reference and posts it only when it is absent.

## What happened to FIN-2026-000001?

From the logs, filter on `loan_reference`. Then read the loan and the transaction. Correlation ids change on every run. `make simulate` prints a fresh trail. A representative sequence for the duplicate disbursement is:

```json
{"event": "loan", "status": "COMPLETED", "external_reference": "LN-DUP", "client_uid": "CLT-DUP", "loan_reference": "FIN-2026-000001", "fineract_transaction_id": null, "erp_transaction_id": null}
{"event": "disbursement", "status": "COMPLETED", "external_reference": "FIN-2026-000001-DISB", "loan_reference": "FIN-2026-000001", "erp_transaction_id": "FIN-2026-000001-DISB"}
{"event": "disbursement", "status": "REPLAY", "external_reference": "FIN-2026-000001-DISB", "loan_reference": "FIN-2026-000001"}
```

The one API call for the money movement is `GET /api/transactions/FIN-2026-000001-DISB`. The loan is `GET /api/loans/FIN-2026-000001`. The ERP journal id in the log is the external reference, which is also the mock ERP's key. `fineract_transaction_id` is the partner `external_reference` (Fineract transaction `externalId`); the numeric Fineract resource id stays private. The startup sweep has an empty `correlation_id` because it does not run inside a request.

## Reversals and overpayment

A reversal is a new journal with the opposite lines and external reference `{original}:reversal`. Reconciliation does not post it and does not repair either system. An overpayment would credit a liability. That mapping is not implemented. Cash accounting was chosen because interest is recognized on repayment. Accrual was rejected for that reason.

## What was not built

- Part L: GL master-data reconciliation beyond the transaction report, fees, a demonstrated overpayment, metrics, webhooks.
- Posting of reversals.
- A queue.
- Regeneration of Fineract's OpenAPI client. The Java integration test speaks raw JSON; unit tests for the GL rule are recorded in `evidence/java-unit-tests.txt`. `make test-python` now runs `mypy` on both Python apps after pytest.

Tenant bootstrap (Kigali Main Office, RWF, cash product, least-privilege `assessment_api`, sample clients) and a live 5,000,000 RWF loan to `Closed (obligations met)` were run on the custom image; see `scripts/seed_fineract.py` and `evidence/live-loan.txt`.

## Fineract API user

Compose and default settings use `mifos` / `password` so a cold `docker compose up` can talk to Fineract before seed. After `scripts/seed_fineract.py`, switch the integration service to the least-privilege user by setting `FINERACT_USERNAME=assessment_api` and `FINERACT_PASSWORD=QwE!5rTy#9uP0` (and rebuild/recreate the container). Seed itself still authenticates as `mifos` to create that user.

## Known weaknesses

- The GL conflict is HTTP 403, because that is how this Fineract version maps a domain rule.
- The request that loses the unique-index race does not return the existing row. The retry does.
- The unauthenticated GL integration test assumes HTTP 401. That was not verified.
- The unit tests cover a matching create and a conflicting create. They do not cover the brand-new insert path, and they do not execute the read SQL that returns `externalId`.
- Python tests and `make simulate` use SQLite. Runtime is MySQL. The unique constraints are the part that has to hold in both.
- `create_all` does not migrate an existing MySQL `idempotency_keys` table. SQLite gets an `ALTER TABLE` for `loan_reference` when that column is missing.
- Recovery of a `FINERACT_PENDING` row whose transaction cannot be found marks the operation `FAILED`.
- I2/I3 concurrency and Part J failure evidence from `make simulate` use stand-in Fineract/ERP objects. Live Fineract evidence covers GL create/replay/conflict and the Part D loan lifecycle.
- Transaction dates follow the Fineract JVM calendar day. On this host that is UTC, so Africa/Kigali wall-clock dates can look like "tomorrow" and be rejected.
- `partner_transactions.external_reference` is unique across operation kinds; disbursement and repayment must not share one string.
