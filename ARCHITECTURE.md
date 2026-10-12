# Architecture

A partner calls the integration service. That service calls Apache Fineract 1.15.0 (custom image) and the mock ERP. All three store data in one MySQL 8.4 server: `fineract_tenants`, `fineract_default`, `mock_erp`, and `integration`. There is no queue, broker, or worker. A request runs until it finishes, fails, or leaves a persisted state that startup or `POST /api/admin/recover` can resume.

```
partner  ->  integration service  ->  Fineract 1.15.0 (GL, clients, loans, transactions)
                                 ->  mock ERP (accounts, journals, balances)
```

## Chart of accounts

Sync is an explicit `POST /api/accounts/sync`. The integration service reads the ERP chart and creates each GL in Fineract through the existing `CREATE` command on `GLACCOUNT`. The chart used by the money mapping is:

| Code | Name | Type | Fineract external id |
| --- | --- | --- | --- |
| 1100 | Bank | ASSET | GL-1100 |
| 1200 | Loan Receivable | ASSET | GL-1200 |
| 4000 | Interest Income | INCOME | GL-4000 |

Currency is RWF. Usage is DETAIL. The same external id sent again with the same gl code, name, type, and currency returns the existing account. A different name, code, type, or currency is rejected inside Fineract.

## Cash journals

Interest is recognized when cash is collected.

- Disbursement: debit 1200, credit 1100, for the principal.
- Repayment: debit 1100 for principal plus interest, credit 1200 for the principal portion, credit 4000 for the interest portion when it is greater than zero.

Portions come from the Fineract transaction (`principalPortion`, `interestPortion`), not from a schedule kept in the integration service. If both portions are zero, the whole amount is treated as principal.

## Identifiers

The partner never receives a Fineract numeric id. The service stores `fineract_client_id`, `fineract_loan_id`, and the GL `resourceId` in its own tables.

| Partner field | Fineract field | ERP field |
| --- | --- | --- |
| `client_uid` | client `externalId` | — |
| `loan_reference` | loan `externalId` | — |
| money `external_reference` | transaction `externalId` | journal `external_reference` |
| GL `external_reference` | GL `externalId` | account `external_reference` |

`fineract_status` on a loan or transaction is the status string from Fineract (`Approved`, `Active`, `Closed (obligations met)`). The public loan `status` is `CLOSED`, `ACTIVE`, `APPROVED`, or `SUBMITTED`.

## Data model (integration database)

- `idempotency_keys` — unique `(operation_type, external_reference)`. Holds the payload hash, state, HTTP status, and response body.
- `account_syncs` — one row per ERP account code. Status is `SYNC_PENDING`, `SYNCED`, or `FAILED`.
- `clients` — unique `client_uid` and unique `external_reference`.
- `loans` — unique `loan_reference` and unique `external_reference`.
- `loan_disbursements` — unique `loan_reference` and unique `external_reference`. This is what makes one disbursement per loan hold under concurrency.
- `partner_transactions` — the public view of a disbursement or repayment.

## State machine

Money operations (disbursement and repayment):

```
RECEIVED -> FINERACT_PENDING -> FINERACT_DONE -> ERP_PENDING -> COMPLETED
```

`FAILED` and `NEEDS_RECONCILIATION` are also states. Allowed moves:

| From | To |
| --- | --- |
| RECEIVED | FINERACT_PENDING, FAILED |
| FINERACT_PENDING | FINERACT_DONE, FAILED, NEEDS_RECONCILIATION |
| FINERACT_DONE | ERP_PENDING |
| ERP_PENDING | COMPLETED, FAILED, NEEDS_RECONCILIATION |
| FAILED | FINERACT_PENDING, ERP_PENDING, NEEDS_RECONCILIATION |
| NEEDS_RECONCILIATION | ERP_PENDING, COMPLETED |
| COMPLETED | (none) |

`COMPLETED` is replayed. `FAILED` and `NEEDS_RECONCILIATION` are retried with the same payload. Anything still in the middle returns HTTP 202 and is not posted again by the caller. Client and loan creation do not post journals. They walk the same machine to `COMPLETED` so a finished operation has one terminal state.

## Idempotency layers for disbursement

1. The idempotency row stops a repeat of the same reference from calling Fineract again.
2. `loan_disbursements.loan_reference` stops a second reference from disbursing the same loan.
3. Fineract stores the transaction `externalId`, so recovery can look the command up.
4. Fineract will not disburse a loan that is already active.

The last line of defence in this service is the unique `loan_reference` on `loan_disbursements`. The Fineract loan state is the backstop if that row were bypassed. Either layer on its own is not enough: the idempotency key does not cover two different references, and the Fineract loan state is only visible after a command has been accepted.

## Recovery

`create_app` sweeps rows left in `FINERACT_PENDING`, `FINERACT_DONE`, `ERP_PENDING`, or `NEEDS_RECONCILIATION`. `POST /api/admin/recover` runs the same sweep. Recovery looks up the Fineract transaction by external id before it posts anything. A missing transaction becomes `FAILED`. A found transaction continues at the ERP step, and the ERP is also looked up by external reference before a journal is created.

In-request retries, with backoff, apply only when Fineract is down (`retry_attempts` 3, `retry_backoff_seconds` 0.05). An ERP failure is not retried inside the request, because a blind retry could write a second journal.

## Reconciliation

`POST /api/reconciliation` compares one Fineract transaction with the ERP journal that carries the same `external_reference`. It does not write. Outcomes are `MATCHED`, `MISSING_IN_ERP`, `MISSING_IN_FINERACT`, and `AMOUNT_MISMATCH`. The amount compared is the Fineract transaction amount against the sum of the journal debits.

A reversal is a new journal with the opposite lines and external reference `{original}:reversal`. The report does not post it.
