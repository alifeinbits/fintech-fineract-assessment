# Simulations

`make simulate` reruns every scenario below and writes [evidence/simulations.txt](evidence/simulations.txt). The script is `scripts/simulate.py`. It starts the integration service in-process with stand-in Fineract and ERP objects. It does not start Docker, and it does not call the running Fineract image.

The loan in every scripted scenario is `FIN-2026-000001`, principal 5,000,000 RWF. The stand-in reports outstanding from its own transaction breakdown. Closing that loan in scenario I3 is against the stand-in, not against the custom Fineract image.

The GL scenario is the one live check that was made against the custom image. It is not part of `make simulate`.

## GL create, replay, and conflict (custom image)

1. Initial state: empty tenant database, changeset `0241` applied, no row for `GL-1200`.
2. Request: `POST /v1/glaccounts` for Loan Receivable, gl code 1200, `externalId` `GL-1200`, `currencyCode` `rwf`. Then the same body. Then the same external id with a different name.
3. Response: HTTP 200 `{"resourceId":1}`, then the same body again, then HTTP 403 `error.msg.glaccount.externalid.conflict`.
4. Fineract state: one `acc_gl_account` row, `1 1200 Loan Receivable GL-1200 RWF`.
5. ERP state: not involved.
6. Integration state: not involved.
7. Expected result: one account, replay returns it, a changed name is rejected.
8. Actual result: resource id 1 both times, currency stored as `RWF`, conflict on the changed name.
9. Explanation: the create command compares gl code, name, type, and currency. Domain-rule exceptions are HTTP 403 in this Fineract version. Details are in [fineract-customization/README.md](fineract-customization/README.md).

Java unit tests for that rule: 5 passed, command in the same note. `GLAccountExternalReferenceIntegrationTest` has not been executed by Gradle.

## I1 sequential duplicate disbursement

1. Initial state: approved loan, no disbursement, no journal.
2. Request: `POST /api/loans/FIN-2026-000001/disburse` four times with `external_reference` `FIN-2026-000001-DISB`.
3. Response: HTTP 201, then 200, 200, 200.
4. Fineract state: 1 disbursement post. Status `Active`.
5. ERP state: one journal, `FIN-2026-000001-DISB`.
6. Integration state: transaction `COMPLETED`.
7. Expected result: 4 requests, 1 Fineract disbursement, 1 ERP journal, 1 integration transaction.
8. Actual result: requests 4, posts 1, journals 1, codes `[201, 200, 200, 200]`.
9. Explanation: the first call completes. The next three match the stored payload hash and return the stored body.

## I2a ten parallel disbursements, one reference

1. Initial state: approved loan, no disbursement.
2. Request: ten simultaneous posts of `DISB-SAME`.
3. Response: HTTP `[201, 202, 202, 202, 202, 202, 202, 202, 202, 202]`.
4. Fineract state: 1 disbursement post.
5. ERP state: one journal for `DISB-SAME`.
6. Integration state: one disbursement slot on the loan.
7. Expected result: one disbursement. The winner returns 201. The others return 202 while it is still running, or 200 if it has already finished.
8. Actual result: posts 1, one 201 and nine 202 responses.
9. Explanation: one idempotency row. Losers see the in-progress state and do not post.

## I2b ten parallel disbursements, different references

1. Initial state: approved loan, no disbursement.
2. Request: ten simultaneous posts, `DISB-0` through `DISB-9`.
3. Response: HTTP `[201, 409, 409, 409, 409, 409, 409, 409, 409, 409]`.
4. Fineract state: 1 disbursement post.
5. ERP state: one journal, for the reference that won the slot.
6. Integration state: one row in `loan_disbursements`.
7. Expected result: one disbursement, one 201, nine 409s.
8. Actual result: posts 1, codes as above.
9. Explanation: the idempotency keys do not collide. The unique `loan_reference` on `loan_disbursements` rejects the other nine. Fineract would also refuse to disburse an already active loan if that row were bypassed. The unique loan reference is the last line of defence in this service.

## I3 ten parallel repayments

1. Initial state: active loan, outstanding 5,000,000.
2. Request: ten simultaneous repayments of 500,000, `REP-0` through `REP-9`.
3. Response: ten HTTP 201s.
4. Fineract state: 10 repayment posts. Outstanding 0. Status `Closed (obligations met)`.
5. ERP state: ten journals, one per reference.
6. Integration state: outstanding `0.000000`, `fineract_status` `Closed (obligations met)`.
7. Expected result: each reference once, outstanding 0, closed.
8. Actual result: posts 10, outstanding 0, status `Closed (obligations met)`.
9. Explanation: outstanding is the balance read back from the stand-in after each post. A second post of one of these references would replay and would not add a journal.

## J1 ERP down after Fineract success

1. Initial state: approved loan.
2. Request: disbursement `DISB-ERPDOWN` with `X-Fault: erp_down`, then the same body with no fault.
3. Response: 503 `needs_reconciliation`, then 201.
4. Fineract state: 1 disbursement post.
5. ERP state: one journal, `DISB-ERPDOWN`, written by the retry.
6. Integration state: transaction `COMPLETED`.
7. Expected result: the retry posts the journal once and does not disburse again.
8. Actual result: posts 1, journals 1.
9. Explanation: `NEEDS_RECONCILIATION` is retryable. The ERP lookup by external reference prevents a second journal.

## J2 Fineract down

1. Initial state: approved loan, no idempotency row.
2. Request: `DISB-DOWN` with `X-Fault: fineract_down`, then the same body with no fault.
3. Response: 503 `fineract_down`, then 201.
4. Fineract state: 1 disbursement post, on the retry only.
5. ERP state: one journal, `DISB-DOWN`.
6. Integration state: transaction `COMPLETED`.
7. Expected result: 503, a `FAILED` row, and a safe retry that posts once.
8. Actual result: posts 1, journals 1, second code 201.
9. Explanation: the failed attempt did not send a command, so the retry is a new disbursement.

## J3 Fineract timeout after send

1. Initial state: approved loan.
2. Request: `DISB-FTIME` with `X-Fault: fineract_timeout_after_send`, then the same post, then `POST /api/admin/recover`.
3. Response: 504, then 202, then `{"recovered":[{"external_reference":"DISB-FTIME","status":"COMPLETED"}]}`.
4. Fineract state: 1 disbursement post. The transaction is found by external id.
5. ERP state: one journal, `DISB-FTIME`.
6. Integration state: transaction `COMPLETED`.
7. Expected result: the caller retry does not post again. Recovery completes the journal.
8. Actual result: posts 1, journals 1, second code 202.
9. Explanation: the row stays `FINERACT_PENDING`, so the second call is "in progress". Recovery looks up `DISB-FTIME` and continues at the ERP step.

## J4 ERP timeout after the journal was written

1. Initial state: approved loan.
2. Request: `DISB-ETIME` with `X-Fault: erp_timeout_after_write`, then `POST /api/admin/recover`.
3. Response: 504, then recovered `COMPLETED`.
4. Fineract state: 1 disbursement post.
5. ERP state: one journal, `DISB-ETIME`.
6. Integration state: transaction `COMPLETED`.
7. Expected result: one journal. Recovery does not write another.
8. Actual result: posts 1, journals 1, status `COMPLETED`.
9. Explanation: the row stays `ERP_PENDING`. Recovery finds the journal by external reference.

## J5 crash between Fineract success and the ERP call

1. Initial state: approved loan.
2. Request: `DISB-CRASH` with `X-Fault: crash_before_erp`. Then the application is constructed again on the same database.
3. Response: 500. After restart the transaction is `COMPLETED`.
4. Fineract state: 1 disbursement post. Loan status `Active`.
5. ERP state: one journal, `DISB-CRASH`, written by the startup sweep.
6. Integration state: transaction `COMPLETED`.
7. Expected result: the row is left at `FINERACT_DONE` and the next process start posts one journal.
8. Actual result: posts 1, journals 1, status `COMPLETED`.
9. Explanation: `create_app` runs the sweep before it serves traffic. The sweep finds the Fineract transaction by external id.

## K reconciliation

1. Initial state: one matched disbursement `DISB-MATCH`, then three discrepancies seeded on the stand-ins.
2. Request: `POST /api/reconciliation` for `DISB-MATCH`, `DISB-MISSING-ERP`, `JRN-ORPHAN`, and `DISB-BAD`.
3. Response: those four outcomes, with amounts `5000000`/`5000000`, `5000000`/null, null/`5000000`, and `5000000`/`1`.
4. Fineract state: has the matched, missing-ERP, and mismatched transactions. Does not have `JRN-ORPHAN`.
5. ERP state: has the matched journal, the orphan, and `DISB-BAD` with debit 1. Does not have `DISB-MISSING-ERP`.
6. Integration state: the report writes nothing.
7. Expected result: `MATCHED`, `MISSING_IN_ERP`, `MISSING_IN_FINERACT`, `AMOUNT_MISMATCH`, and no repair.
8. Actual result: those four outcomes, journal keys unchanged apart from the seeded rows, disbursement posts still 1.
9. Explanation: the comparison is read-only. A reversal would be a new journal `{original}:reversal` with the opposite lines. This report does not post one.

## Live loan on the custom image

Captured in [evidence/live-loan.txt](evidence/live-loan.txt). Loan `FIN-2026-000003`, principal 5,000,000 RWF, product SME Asset Finance, three partial repayments then payoff. Fineract status ended `Closed (obligations met)` with outstanding 0. Interest portions came from Fineract (total interest collected 500,799 RWF). Disbursement uses `POST /loans/external-id/{id}?command=disburse`. Transaction dates follow the Fineract JVM calendar day (UTC on this host).

Seed used for that run: `scripts/seed_fineract.py` and [postman/fineract-seed.json](postman/fineract-seed.json). Backoffice UI: `http://localhost:8080` from apache/fineract-backoffice-ui commit `2674193`.

## What these runs do not show

Part L (fees, overpayment demo, metrics, webhooks) is not started.
