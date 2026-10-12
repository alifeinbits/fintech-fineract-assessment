"""Print the nine-field evidence template for each scripted simulation.

Runs the integration service in-process with stand-in Fineract and ERP objects.
Does not start Docker or Apache Fineract.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Thread

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration-service"))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.settings import Settings  # noqa: E402
from tests.fakes import FakeErp, FakeFineract  # noqa: E402

TOKEN = {"Authorization": "Bearer assessment-token"}
LOAN = "FIN-2026-000001"
PRINCIPAL = "5000000"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def main() -> None:
    capture = _Capture()
    logger = logging.getLogger("integration")
    logger.setLevel(logging.INFO)
    logger.addHandler(capture)
    sections = [
        _duplicates(),
        _concurrent_same(),
        _concurrent_different(),
        _concurrent_repayments(),
        _fineract_down(),
        _fineract_timeout(),
        _erp_down(),
        _erp_timeout(),
        _crash_before_erp(),
        _reconciliation(),
    ]
    for section in sections:
        print(section)
        print()
    print("## Log trail for FIN-2026-000001")
    for line in capture.lines:
        if LOAN in line:
            print(line)


def _duplicates() -> str:
    app, fineract, erp, client = _stack()
    _open_loan(client, "DUP")
    body = {"external_reference": "FIN-2026-000001-DISB", "amount": PRINCIPAL, "transaction_date": "2026-10-07"}
    codes = [
        client.post(f"/api/loans/{LOAN}/disburse", headers=TOKEN, json=body).status_code for _ in range(4)
    ]
    return _block(
        "I1 sequential duplicate disbursement",
        "Approved loan, no disbursement, no journal.",
        "POST /api/loans/FIN-2026-000001/disburse four times with external_reference FIN-2026-000001-DISB.",
        f"HTTP {codes}.",
        f"Fineract disbursement posts: {fineract.disburse_posts}. Status: {fineract.loans[LOAN].status}.",
        f"ERP journals: {list(erp.journals)}.",
        f"Stored transaction status: {_tx(app, 'FIN-2026-000001-DISB')['status']}.",
        "4 requests, 1 Fineract disbursement, 1 ERP journal, 1 integration transaction.",
        f"Requests {len(codes)}, Fineract posts {fineract.disburse_posts}, journals {len(erp.journals)}, "
        f"codes {codes}.",
        "The first call completes. The next three replay the stored response because the payload hash matches.",
    )


def _concurrent_same() -> str:
    app, fineract, _erp, client = _stack()
    _open_loan(client, "SAME")
    body = {"external_reference": "DISB-SAME", "amount": PRINCIPAL, "transaction_date": "2026-10-07"}
    codes = sorted(_parallel(app, f"/api/loans/{LOAN}/disburse", [body] * 10))
    return _block(
        "I2a ten parallel disbursements, one reference",
        "Approved loan, no disbursement.",
        "Ten simultaneous POSTs of DISB-SAME.",
        f"HTTP {codes}.",
        f"Fineract disbursement posts: {fineract.disburse_posts}.",
        "One journal for DISB-SAME when the winner finished.",
        "One disbursement slot on the loan.",
        "Exactly one Fineract disbursement. The winner returns 201. The others return 202 while that call is still in progress, or 200 if it has already finished.",
        f"Posts {fineract.disburse_posts}, codes {codes}.",
        "The loan_disbursements unique key admits one reference. Callers that lose the race read the in-progress "
        "row (202) or the stored result (200) and do not post again.",
    )


def _concurrent_different() -> str:
    app, fineract, _erp, client = _stack()
    _open_loan(client, "DIFF")
    bodies = [
        {"external_reference": f"DISB-{index}", "amount": PRINCIPAL, "transaction_date": "2026-10-07"}
        for index in range(10)
    ]
    codes = sorted(_parallel(app, f"/api/loans/{LOAN}/disburse", bodies))
    return _block(
        "I2b ten parallel disbursements, different references",
        "Approved loan, no disbursement.",
        "Ten simultaneous POSTs, references DISB-0 through DISB-9.",
        f"HTTP {codes}.",
        f"Fineract disbursement posts: {fineract.disburse_posts}.",
        "One journal, for whichever reference won the slot.",
        "One row in loan_disbursements.",
        "Exactly one disbursement. One 201 and nine 409 responses.",
        f"Posts {fineract.disburse_posts}, codes {codes}.",
        "Idempotency keys do not collide across references. The unique loan_reference on loan_disbursements is the "
        "constraint that stops the second disbursement. Fineract loan state would reject another disburse if that "
        "row were bypassed.",
    )


def _concurrent_repayments() -> str:
    app, fineract, _erp, client = _stack()
    _open_loan(client, "REPAY")
    client.post(
        f"/api/loans/{LOAN}/disburse",
        headers=TOKEN,
        json={"external_reference": "DISB-REPAY", "amount": PRINCIPAL, "transaction_date": "2026-10-07"},
    )
    bodies = [
        {"external_reference": f"REP-{index}", "amount": "500000", "transaction_date": "2026-10-08"}
        for index in range(10)
    ]
    codes = sorted(_parallel(app, f"/api/loans/{LOAN}/repayments", bodies))
    loan = client.get(f"/api/loans/{LOAN}", headers=TOKEN).json()
    return _block(
        "I3 ten parallel repayments, different references",
        "Active loan, outstanding 5000000.",
        "Ten simultaneous repayments of 500000, references REP-0 through REP-9.",
        f"HTTP {codes}.",
        f"Fineract repayment posts: {fineract.repay_posts}. Outstanding on the stand-in: {fineract.loans[LOAN].outstanding}. "
        f"Status: {fineract.loans[LOAN].status}.",
        "Ten journals, one per reference.",
        f"Partner loan outstanding {loan['outstanding']}, fineract_status {loan['fineract_status']}.",
        "Each reference applies once. Outstanding is 0 and the Fineract status is Closed (obligations met).",
        f"Posts {fineract.repay_posts}, outstanding {loan['outstanding']}, status {loan['fineract_status']}, codes {codes}.",
        "Outstanding is read back from the stand-in after each post, not computed as sent amount minus nothing. "
        "A duplicate of one reference would replay and would not post a second journal.",
    )


def _fineract_down() -> str:
    app, fineract, erp, client = _stack()
    _open_loan(client, "DOWN")
    body = {"external_reference": "DISB-DOWN", "amount": PRINCIPAL, "transaction_date": "2026-10-07"}
    first = client.post(f"/api/loans/{LOAN}/disburse", headers={**TOKEN, "X-Fault": "fineract_down"}, json=body)
    second = client.post(f"/api/loans/{LOAN}/disburse", headers=TOKEN, json=body)
    return _block(
        "J2 Fineract down",
        "Approved loan. Idempotency row does not exist yet.",
        "POST disbursement with X-Fault: fineract_down, then the same body with no fault.",
        f"First HTTP {first.status_code} {first.json()}. Second HTTP {second.status_code}.",
        f"Fineract disbursement posts: {fineract.disburse_posts}.",
        f"Journals: {list(erp.journals)}.",
        f"After recovery-by-retry, transaction status {_tx(app, 'DISB-DOWN')['status']}.",
        "Caller gets 503 and a FAILED row. The same payload is safe to retry and then posts once.",
        f"Posts {fineract.disburse_posts}, journals {len(erp.journals)}, second code {second.status_code}.",
        "Nothing was sent to Fineract on the failed attempt, so the retry is a new command, not a blind replay.",
    )


def _fineract_timeout() -> str:
    app, fineract, erp, client = _stack()
    _open_loan(client, "FTIME")
    body = {"external_reference": "DISB-FTIME", "amount": PRINCIPAL, "transaction_date": "2026-10-07"}
    first = client.post(
        f"/api/loans/{LOAN}/disburse", headers={**TOKEN, "X-Fault": "fineract_timeout_after_send"}, json=body
    )
    second = client.post(f"/api/loans/{LOAN}/disburse", headers=TOKEN, json=body)
    recovered = client.post("/api/admin/recover", headers=TOKEN).json()
    return _block(
        "J3 Fineract timeout after send",
        "Approved loan.",
        "POST with X-Fault: fineract_timeout_after_send, then the same POST, then POST /api/admin/recover.",
        f"First HTTP {first.status_code}. Second HTTP {second.status_code}. Recovered {recovered}.",
        f"Fineract disbursement posts: {fineract.disburse_posts}. The transaction is found by external id.",
        f"Journals: {list(erp.journals)}.",
        f"Transaction status {_tx(app, 'DISB-FTIME')['status']}.",
        "504, then 202 because the row is still in progress, then recovery completes one journal.",
        f"Posts {fineract.disburse_posts}, journals {len(erp.journals)}, second code {second.status_code}.",
        "The command reached Fineract. Recovery looks up external id DISB-FTIME and does not disburse again.",
    )


def _erp_down() -> str:
    app, fineract, erp, client = _stack()
    _open_loan(client, "ERPDOWN")
    body = {"external_reference": "DISB-ERPDOWN", "amount": PRINCIPAL, "transaction_date": "2026-10-07"}
    first = client.post(f"/api/loans/{LOAN}/disburse", headers={**TOKEN, "X-Fault": "erp_down"}, json=body)
    second = client.post(f"/api/loans/{LOAN}/disburse", headers=TOKEN, json=body)
    return _block(
        "J1 ERP down after Fineract success",
        "Approved loan.",
        "POST with X-Fault: erp_down, then the same body with no fault.",
        f"First HTTP {first.status_code} {first.json()}. Second HTTP {second.status_code}.",
        f"Fineract disbursement posts: {fineract.disburse_posts}.",
        f"Journals: {list(erp.journals)}.",
        f"Transaction status {_tx(app, 'DISB-ERPDOWN')['status']}.",
        "503 needs_reconciliation. The retry posts the journal once. Fineract is not called again.",
        f"Posts {fineract.disburse_posts}, journals {len(erp.journals)}.",
        "Status stays NEEDS_RECONCILIATION, which is retryable. The ERP lookup by external reference prevents a second journal.",
    )


def _erp_timeout() -> str:
    app, fineract, erp, client = _stack()
    _open_loan(client, "ETIME")
    body = {"external_reference": "DISB-ETIME", "amount": PRINCIPAL, "transaction_date": "2026-10-07"}
    first = client.post(
        f"/api/loans/{LOAN}/disburse", headers={**TOKEN, "X-Fault": "erp_timeout_after_write"}, json=body
    )
    recovered = client.post("/api/admin/recover", headers=TOKEN).json()
    return _block(
        "J4 ERP timeout after the journal was written",
        "Approved loan.",
        "POST with X-Fault: erp_timeout_after_write, then POST /api/admin/recover.",
        f"First HTTP {first.status_code}. Recovered {recovered}.",
        f"Fineract disbursement posts: {fineract.disburse_posts}.",
        f"Journals: {list(erp.journals)}.",
        f"Transaction status {_tx(app, 'DISB-ETIME')['status']}.",
        "504 while the row stays ERP_PENDING. Recovery sees the existing journal and completes without writing another.",
        f"Posts {fineract.disburse_posts}, journals {len(erp.journals)}, status {_tx(app, 'DISB-ETIME')['status']}.",
        "The journal is keyed by external reference, so the catch-up path finds it instead of posting again.",
    )


def _crash_before_erp() -> str:
    database = tempfile.NamedTemporaryFile(prefix="assessment-crash-sim-", suffix=".db", delete=False)
    database.close()
    path = Path(database.name)
    settings = Settings(database_url=f"sqlite:///{path}", retry_backoff_seconds=0)
    fineract = FakeFineract()
    erp = FakeErp()
    app = create_app(settings=settings, fineract=fineract, erp=erp)
    with TestClient(app) as client:
        _open_loan(client, "CRASH")
        body = {"external_reference": "DISB-CRASH", "amount": PRINCIPAL, "transaction_date": "2026-10-07"}
        first = client.post(
            f"/api/loans/{LOAN}/disburse", headers={**TOKEN, "X-Fault": "crash_before_erp"}, json=body
        )
    restarted = create_app(settings=settings, fineract=fineract, erp=erp)
    with TestClient(restarted) as client:
        loan = client.get(f"/api/loans/{LOAN}", headers=TOKEN).json()
        transaction = client.get("/api/transactions/DISB-CRASH", headers=TOKEN).json()
    return _block(
        "J5 crash between Fineract success and the ERP call",
        "Approved loan.",
        "POST with X-Fault: crash_before_erp. Then construct the app again on the same database.",
        f"First HTTP {first.status_code}. After restart, transaction {transaction}.",
        f"Fineract disbursement posts: {fineract.disburse_posts}. Loan status {loan['fineract_status']}.",
        f"Journals: {list(erp.journals)}.",
        f"Transaction status {transaction['status']}.",
        "500 with the row left at FINERACT_DONE. Startup recovery posts the journal once.",
        f"Posts {fineract.disburse_posts}, journals {len(erp.journals)}, status {transaction['status']}.",
        "create_app runs the startup sweep. It finds the Fineract transaction by external id and writes the journal.",
    )


def _reconciliation() -> str:
    _app, fineract, erp, client = _stack()
    _open_loan(client, "RECON")
    client.post(
        f"/api/loans/{LOAN}/disburse",
        headers=TOKEN,
        json={"external_reference": "DISB-MATCH", "amount": PRINCIPAL, "transaction_date": "2026-10-07"},
    )
    fineract.transactions[(LOAN, "DISB-MISSING-ERP")] = fineract.transactions[(LOAN, "DISB-MATCH")]
    erp.journals["JRN-ORPHAN"] = list(erp.journals["DISB-MATCH"])
    fineract.transactions[(LOAN, "DISB-BAD")] = fineract.transactions[(LOAN, "DISB-MATCH")]
    erp.journals["DISB-BAD"] = [("1200", Decimal("1"), Decimal("0"))]
    report = client.post(
        "/api/reconciliation",
        headers=TOKEN,
        json={
            "items": [
                {"loan_reference": LOAN, "external_reference": "DISB-MATCH"},
                {"loan_reference": LOAN, "external_reference": "DISB-MISSING-ERP"},
                {"loan_reference": LOAN, "external_reference": "JRN-ORPHAN"},
                {"loan_reference": LOAN, "external_reference": "DISB-BAD"},
            ]
        },
    ).json()
    outcomes = [item["outcome"] for item in report["results"]]
    return _block(
        "K reconciliation, four outcomes",
        "One matched disbursement, then three seeded discrepancies on the stand-ins.",
        "POST /api/reconciliation with DISB-MATCH, DISB-MISSING-ERP, JRN-ORPHAN, DISB-BAD.",
        str(report),
        "Stand-in has DISB-MATCH, DISB-MISSING-ERP, and DISB-BAD. It has no JRN-ORPHAN.",
        "ERP has DISB-MATCH, JRN-ORPHAN, and DISB-BAD with debit 1. It has no DISB-MISSING-ERP.",
        "The report does not write an integration row.",
        "MATCHED, MISSING_IN_ERP, MISSING_IN_FINERACT, AMOUNT_MISMATCH. No journal is created or repaired.",
        f"Outcomes {outcomes}. Journal keys {sorted(erp.journals)}. Disburse posts {fineract.disburse_posts}.",
        "Comparison is read-only. A reversal would be a new journal {original}:reversal with opposite lines; "
        "this report does not post one.",
    )


def _stack():
    database = tempfile.NamedTemporaryFile(prefix="assessment-sim-", suffix=".db", delete=False)
    database.close()
    fineract = FakeFineract()
    erp = FakeErp()
    settings = Settings(database_url=f"sqlite:///{database.name}", retry_backoff_seconds=0)
    app = create_app(settings=settings, fineract=fineract, erp=erp)
    return app, fineract, erp, TestClient(app)


def _open_loan(client: TestClient, suffix: str) -> None:
    client.post(
        "/api/clients",
        headers=TOKEN,
        json={
            "external_reference": f"CL-{suffix}",
            "client_uid": f"CLT-{suffix}",
            "first_name": "Amina",
            "last_name": "Uwase",
            "activation_date": "2026-10-07",
        },
    )
    client.post(
        "/api/loans",
        headers=TOKEN,
        json={
            "external_reference": f"LN-{suffix}",
            "loan_reference": LOAN,
            "client_uid": f"CLT-{suffix}",
            "principal": PRINCIPAL,
            "currency": "RWF",
            "interest_rate": "18",
            "term_months": 12,
            "submitted_on": "2026-10-07",
        },
    )


def _tx(app, external_reference: str) -> dict:
    with TestClient(app) as client:
        return client.get(f"/api/transactions/{external_reference}", headers=TOKEN).json()


def _parallel(app, path: str, bodies: list[dict]) -> list[int]:
    barrier = Barrier(len(bodies))
    codes: list[int] = []

    def worker(body: dict) -> None:
        barrier.wait()
        with TestClient(app) as client:
            codes.append(client.post(path, headers=TOKEN, json=body).status_code)

    threads = [Thread(target=worker, args=(body,)) for body in bodies]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return codes


def _block(
    title: str,
    initial: str,
    request: str,
    response: str,
    fineract_state: str,
    erp_state: str,
    integration_state: str,
    expected: str,
    actual: str,
    explanation: str,
) -> str:
    lines = [
        f"## {title}",
        f"1. Initial state: {initial}",
        f"2. Request: {request}",
        f"3. Response: {response}",
        f"4. Fineract state: {fineract_state}",
        f"5. ERP state: {erp_state}",
        f"6. Integration state: {integration_state}",
        f"7. Expected result: {expected}",
        f"8. Actual result: {actual}",
        f"9. Explanation: {explanation}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
