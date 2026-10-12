# API

Partner and mock ERP calls use `Authorization: Bearer assessment-token`. `GET /health` on both services is open. Every other response includes `X-Correlation-ID`. Error bodies have one shape:

```json
{"error": {"code": "conflict", "message": "payload does not match the stored request", "correlation_id": "..."}}
```

Validation errors use HTTP 422 and code `validation_error`. OpenAPI for the integration service is `GET /openapi.json`.

Fineract numeric ids are not in partner responses. `fineract_status` is the status string and is public.

## Idempotency

The key is `(operation_type, external_reference)`.

| Situation | HTTP |
| --- | --- |
| First success | 201, except account sync which is 200 |
| Same payload after `COMPLETED` | 200 and the stored body |
| Same payload while the row is still in progress | 202 `{"status","external_reference"}` |
| Same reference, different payload | 409 `conflict` |
| Same payload after `FAILED` or `NEEDS_RECONCILIATION` | the operation runs again |

## Faults

`X-Fault` is honored only when `FAULTS_ENABLED` is true (the default). Unknown values return 400 `unknown_fault`.

| Value | Effect |
| --- | --- |
| `fineract_down` | 503 before the Fineract command. The row is `FAILED` and the same payload can be retried. |
| `fineract_timeout_after_send` | 504 after Fineract has the transaction. The row stays `FINERACT_PENDING`. A caller retry returns 202. Recovery looks the transaction up. |
| `erp_down` | 503 `needs_reconciliation` after Fineract succeeded. The same payload posts the journal once. |
| `erp_timeout_after_write` | 504 after the journal exists. The row stays `ERP_PENDING`. Recovery completes it without a second journal. |
| `crash_before_erp` | 500 with the row at `FINERACT_DONE`. The next process start posts the journal. |

## Integration service (host port 8090)

### `GET /health`

`{"status":"ok"}`.

### `GET /api/ready`

`{"status":"ready"}`. Requires the bearer token.

### `POST /api/accounts/sync`

Reads the ERP chart and creates any GL that is not already `SYNCED`. The response is 200. A Fineract failure marks that account `FAILED` and leaves the others alone. The next sync retries the failed code. The body does not include the Fineract resource id.

```json
{"accounts": [{"code": "1100", "name": "Bank", "type": "ASSET", "status": "SYNCED"}]}
```

### `POST /api/clients`

```json
{
  "external_reference": "CL-1",
  "client_uid": "CLT-1",
  "first_name": "Amina",
  "last_name": "Uwase",
  "activation_date": "2026-10-07"
}
```

201 the first time, 200 on replay. A second `external_reference` for the same `client_uid` is 409.

### `GET /api/clients/{client_uid}`

Returns the same public client object. 404 if the client was never created here.

### `POST /api/loans`

```json
{
  "external_reference": "LN-OP-1",
  "loan_reference": "FIN-2026-000001",
  "client_uid": "CLT-1",
  "principal": "5000000",
  "currency": "RWF",
  "interest_rate": "18",
  "term_months": 12,
  "submitted_on": "2026-10-07"
}
```

Requires a client that already exists. 404 otherwise. The loan is submitted and approved. While Fineract still reports zero outstanding before disbursement, the service stores the principal. A second `external_reference` for the same `loan_reference` is 409.

### `GET /api/loans/{loan_reference}`

```json
{
  "external_reference": "LN-OP-1",
  "loan_reference": "FIN-2026-000001",
  "client_uid": "CLT-1",
  "principal": "5000000.000000",
  "currency": "RWF",
  "status": "APPROVED",
  "fineract_status": "Approved",
  "outstanding": "5000000.000000",
  "disbursement_reference": null
}
```

### `POST /api/loans/{loan_reference}/disburse`

```json
{"external_reference": "FIN-2026-000001-DISB", "amount": "5000000", "transaction_date": "2026-10-07"}
```

The amount must equal the principal (422 otherwise). A second reference for a loan that already has a disbursement is 409.

### `POST /api/loans/{loan_reference}/repayments`

```json
{"external_reference": "REP-1", "amount": "500000", "transaction_date": "2026-10-08"}
```

409 if the loan has not been disbursed. Many repayments are allowed, one per `external_reference`.

### `GET /api/transactions/{external_reference}`

```json
{
  "external_reference": "FIN-2026-000001-DISB",
  "loan_reference": "FIN-2026-000001",
  "kind": "DISBURSEMENT",
  "amount": "5000000.000000",
  "principal": "5000000.000000",
  "interest": "0.000000",
  "status": "COMPLETED",
  "loan_status": "ACTIVE",
  "fineract_status": "Active",
  "outstanding": "5000000.000000"
}
```

### `POST /api/admin/recover`

`{"recovered": [{"external_reference": "DISB-FTIME", "status": "COMPLETED"}]}`.

### `POST /api/reconciliation`

```json
{"items": [{"loan_reference": "FIN-2026-000001", "external_reference": "FIN-2026-000001-DISB"}]}
```

One to 100 items. The response is `{"results": [...]}` with `outcome`, `fineract_amount`, and `erp_amount`. This call does not post or repair.

## Mock ERP (host port 8001)

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/health` | open |
| POST | `/api/accounts` | 201, or 200 if the same `external_reference` and payload were already stored. 409 if the code or the payload clashes. |
| GET | `/api/accounts` | the chart, including the seeded 1100, 1200, and 4000 accounts |
| GET | `/api/accounts/{code}/balance` | debit-normal for asset and expense, credit-normal otherwise |
| POST | `/api/journal-entries` | balanced lines only. 200 on an exact replay, 409 on a clash, 422 if a line has both sides or the entry does not balance |
| GET | `/api/journal-entries/{external_reference}` | 404 when absent |

`X-Fault: down` returns 503. `X-Fault: timeout_after_write` commits the journal and then sleeps.

## Fineract (host port 8443)

The integration service calls `https://fineract:8443/fineract-provider/api/v1` with basic auth and `Fineract-Platform-TenantId: default`. Partners do not call Fineract directly.

GL create is `POST /v1/glaccounts` with `externalId` and `currencyCode`. An identical account returns the existing resource. A conflicting payload is HTTP 403 with `error.msg.glaccount.externalid.conflict`. Fineract maps domain-rule exceptions to 403. `POST /api/accounts/sync` still returns HTTP 200; that account row is marked `FAILED` with the conflict message and is retried on the next sync. The service does not create a second GL.

Loan commands used by the service: create, `command=approve`, `command=disburse`, `command=repayment`. Lookups use the external-id routes. A missing transaction is HTTP 404 from Fineract and means "not posted", which is how recovery avoids a blind retry.
