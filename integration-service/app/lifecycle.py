from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.accounting import disbursement_entry, repayment_entry
from app.errors import ApiError, GatewayError
from app.gateways import (
    AccountSnapshot,
    HttpErp,
    HttpFineract,
    PostedTransaction,
    public_loan_status,
)
from app.idempotency import IdempotencyConflict, claim, mark
from app.models import AccountSync, ClientRecord, IdempotencyKey, LoanDisbursement, LoanRecord, PartnerTransaction
from app.observe import log_event
from app.retry import call_with_backoff
from app.schemas import (
    AccountSyncOut,
    ClientCreate,
    ClientOut,
    DisbursementCreate,
    LoanCreate,
    LoanOut,
    RepaymentCreate,
    TransactionOut,
)
from app.settings import Settings


def sync_accounts(session: Session, erp: HttpErp, fineract: HttpFineract) -> list[dict]:
    results: list[dict] = []
    for account in erp.list_accounts():
        results.append(_sync_one(session, fineract, account).model_dump(mode="json"))
    return results


def _sync_one(session: Session, fineract: HttpFineract, account: AccountSnapshot) -> AccountSyncOut:
    row = session.scalar(select(AccountSync).where(AccountSync.external_reference == account.external_reference))
    if row is not None and row.status == "SYNCED":
        return _account_out(row)
    if row is None:
        row = AccountSync(
            code=account.code,
            name=account.name,
            account_type=account.type,
            currency=account.currency,
            external_reference=account.external_reference,
            status="SYNC_PENDING",
        )
        session.add(row)
        session.commit()
    try:
        resource_id = fineract.create_gl(account)
    except GatewayError as exc:
        row.status = "FAILED"
        row.error_message = exc.message
        session.commit()
        return _account_out(row)
    row.fineract_resource_id = resource_id
    row.status = "SYNCED"
    row.error_message = None
    session.commit()
    return _account_out(row)


def _account_out(row: AccountSync) -> AccountSyncOut:
    return AccountSyncOut(
        code=row.code,
        name=row.name,
        type=row.account_type,
        currency=row.currency,
        external_reference=row.external_reference,
        status=row.status,  # type: ignore[arg-type]
        error=row.error_message,
    )


def create_client(session: Session, fineract: HttpFineract, body: ClientCreate) -> tuple[int, dict]:
    payload = body.model_dump(mode="json")
    row = _begin(session, "CLIENT", body.external_reference, payload)
    if isinstance(row, tuple):
        return row
    taken = session.scalar(select(ClientRecord).where(ClientRecord.client_uid == body.client_uid))
    if taken is not None and taken.external_reference != body.external_reference:
        _fail(session, row, "client_uid already exists")
        raise ApiError(409, "conflict", "client_uid already exists")
    record = taken or ClientRecord(
        external_reference=body.external_reference,
        client_uid=body.client_uid,
        first_name=body.first_name,
        last_name=body.last_name,
    )
    if taken is None:
        session.add(record)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            _fail(session, row, "client_uid already exists")
            raise ApiError(409, "conflict", "client_uid already exists") from None
    try:
        found = fineract.find_client(body.client_uid)
        if found is None:
            fineract.create_client(body.client_uid, body.first_name, body.last_name, body.activation_date)
            found = fineract.find_client(body.client_uid)
        if found is None:
            raise GatewayError("fineract_error", "Fineract client was not visible after lookup")
    except GatewayError as exc:
        _fail(session, row, exc.message)
        raise ApiError(exc.status_code, exc.code, exc.message) from exc
    record.fineract_client_id = found.fineract_id
    session.commit()
    public = ClientOut(
        external_reference=record.external_reference,
        client_uid=record.client_uid,
        first_name=record.first_name,
        last_name=record.last_name,
    ).model_dump(mode="json")
    log_event("client", "COMPLETED", external_reference=body.external_reference, client_uid=body.client_uid)
    return _finish(session, row, public)


def get_client(session: Session, client_uid: str) -> dict:
    record = session.scalar(select(ClientRecord).where(ClientRecord.client_uid == client_uid))
    if record is None:
        raise ApiError(404, "not_found", "client does not exist")
    return ClientOut(
        external_reference=record.external_reference,
        client_uid=record.client_uid,
        first_name=record.first_name,
        last_name=record.last_name,
    ).model_dump(mode="json")


def create_loan(session: Session, fineract: HttpFineract, body: LoanCreate) -> tuple[int, dict]:
    payload = body.model_dump(mode="json")
    row = _begin(session, "LOAN", body.external_reference, payload)
    if isinstance(row, tuple):
        return row
    client = session.scalar(select(ClientRecord).where(ClientRecord.client_uid == body.client_uid))
    if client is None or client.fineract_client_id is None:
        _fail(session, row, "client does not exist")
        raise ApiError(404, "not_found", "client does not exist")
    other = session.scalar(select(LoanRecord).where(LoanRecord.loan_reference == body.loan_reference))
    if other is not None and other.external_reference != body.external_reference:
        _fail(session, row, "loan_reference already exists")
        raise ApiError(409, "conflict", "loan_reference already exists")
    record = other or LoanRecord(
        external_reference=body.external_reference,
        loan_reference=body.loan_reference,
        client_uid=body.client_uid,
        principal=body.principal,
        currency=body.currency.upper(),
        interest_rate=body.interest_rate,
        term_months=body.term_months,
        submitted_on=body.submitted_on.isoformat(),
        outstanding=body.principal,
    )
    if other is None:
        session.add(record)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            _fail(session, row, "loan_reference already exists")
            raise ApiError(409, "conflict", "loan_reference already exists") from None
    try:
        found = fineract.find_loan(body.loan_reference)
        if found is None:
            fineract.create_loan(
                body.loan_reference,
                client.fineract_client_id,
                body.principal,
                body.interest_rate,
                body.term_months,
                body.submitted_on,
            )
        fineract.approve_loan(body.loan_reference, body.submitted_on)
        found = fineract.find_loan(body.loan_reference)
        if found is None:
            raise GatewayError("fineract_error", "Fineract loan was not visible after lookup")
    except GatewayError as exc:
        _fail(session, row, exc.message)
        raise ApiError(exc.status_code, exc.code, exc.message) from exc
    record.fineract_loan_id = found.fineract_id
    record.fineract_status = found.status
    record.status = public_loan_status(found.status)
    record.outstanding = body.principal if record.status == "APPROVED" else found.outstanding
    session.commit()
    log_event(
        "loan",
        "COMPLETED",
        external_reference=body.external_reference,
        client_uid=body.client_uid,
        loan_reference=body.loan_reference,
    )
    return _finish(session, row, _loan_out(record).model_dump(mode="json"))


def get_loan(session: Session, loan_reference: str) -> dict:
    record = session.scalar(select(LoanRecord).where(LoanRecord.loan_reference == loan_reference))
    if record is None:
        raise ApiError(404, "not_found", "loan does not exist")
    return _loan_out(record).model_dump(mode="json")


def disburse(
    session: Session,
    settings: Settings,
    fineract: HttpFineract,
    erp: HttpErp,
    loan_reference: str,
    body: DisbursementCreate,
    fault: str | None = None,
) -> tuple[int, dict]:
    loan = _require_loan(session, loan_reference)
    if body.amount != Decimal(str(loan.principal)):
        raise ApiError(422, "validation_error", "disbursement amount must equal the loan principal")
    payload = body.model_dump(mode="json")
    row = _begin(session, "DISBURSEMENT", body.external_reference, payload)
    if isinstance(row, tuple):
        log_event(
            "disbursement",
            "REPLAY" if row[0] == 200 else "IN_PROGRESS",
            external_reference=body.external_reference,
            loan_reference=loan_reference,
        )
        return row
    _remember_loan_reference(session, row, loan.loan_reference)
    _claim_disbursement_slot(session, loan, row, body.external_reference)
    if row.status in {"NEEDS_RECONCILIATION", "ERP_PENDING", "FINERACT_DONE"}:
        return _complete_erp(
            session, settings, erp, loan, row, body.external_reference, "DISBURSEMENT", body.amount, fault=fault
        )
    if fault == "fineract_down":
        _fail(session, row, "Fineract is down")
        log_event("disbursement", "FAILED", external_reference=body.external_reference, loan_reference=loan_reference)
        raise ApiError(503, "fineract_down", "Fineract is down")
    if row.status == "RECEIVED":
        mark(session, row, "FINERACT_PENDING")
    elif row.status == "FAILED":
        mark(session, row, "FINERACT_PENDING")
    try:
        posted = call_with_backoff(
            settings,
            lambda: fineract.disburse(loan.loan_reference, body.external_reference, body.amount, body.transaction_date),
        )
        view = fineract.find_loan(loan.loan_reference)
        if view is None:
            raise GatewayError("fineract_error", "Fineract loan was not visible after disbursement")
        if fault == "fineract_timeout_after_send":
            log_event(
                "disbursement",
                "FINERACT_PENDING",
                external_reference=body.external_reference,
                loan_reference=loan_reference,
            )
            raise ApiError(504, "fineract_timeout", "Fineract timed out after the disbursement was sent")
    except ApiError:
        raise
    except GatewayError as exc:
        _fail(session, row, exc.message)
        raise ApiError(exc.status_code, exc.code, exc.message) from exc
    _remember_loan(session, loan, view.status, view.outstanding)
    mark(session, row, "FINERACT_DONE")
    _save_transaction(session, loan, body.external_reference, "DISBURSEMENT", posted, view.status, view.outstanding)
    if fault == "crash_before_erp":
        log_event(
            "disbursement",
            "FINERACT_DONE",
            external_reference=body.external_reference,
            loan_reference=loan_reference,
        )
        raise ApiError(500, "crashed", "process stopped before the ERP journal")
    return _complete_erp(
        session, settings, erp, loan, row, body.external_reference, "DISBURSEMENT", body.amount, posted, fault
    )


def repay(
    session: Session,
    settings: Settings,
    fineract: HttpFineract,
    erp: HttpErp,
    loan_reference: str,
    body: RepaymentCreate,
    fault: str | None = None,
) -> tuple[int, dict]:
    loan = _require_loan(session, loan_reference)
    if loan.disbursement_reference is None:
        raise ApiError(409, "conflict", "loan has not been disbursed")
    payload = body.model_dump(mode="json")
    row = _begin(session, "REPAYMENT", body.external_reference, payload)
    if isinstance(row, tuple):
        log_event(
            "repayment",
            "REPLAY" if row[0] == 200 else "IN_PROGRESS",
            external_reference=body.external_reference,
            loan_reference=loan_reference,
        )
        return row
    _remember_loan_reference(session, row, loan.loan_reference)
    if row.status in {"NEEDS_RECONCILIATION", "ERP_PENDING", "FINERACT_DONE"}:
        return _complete_erp(
            session, settings, erp, loan, row, body.external_reference, "REPAYMENT", body.amount, fault=fault
        )
    if fault == "fineract_down":
        _fail(session, row, "Fineract is down")
        log_event("repayment", "FAILED", external_reference=body.external_reference, loan_reference=loan_reference)
        raise ApiError(503, "fineract_down", "Fineract is down")
    if row.status in {"RECEIVED", "FAILED"}:
        mark(session, row, "FINERACT_PENDING")
    try:
        posted = call_with_backoff(
            settings,
            lambda: fineract.repay(loan.loan_reference, body.external_reference, body.amount, body.transaction_date),
        )
        view = fineract.find_loan(loan.loan_reference)
        if view is None:
            raise GatewayError("fineract_error", "Fineract loan was not visible after repayment")
        if fault == "fineract_timeout_after_send":
            log_event(
                "repayment",
                "FINERACT_PENDING",
                external_reference=body.external_reference,
                loan_reference=loan_reference,
            )
            raise ApiError(504, "fineract_timeout", "Fineract timed out after the repayment was sent")
    except ApiError:
        raise
    except GatewayError as exc:
        _fail(session, row, exc.message)
        raise ApiError(exc.status_code, exc.code, exc.message) from exc
    _remember_loan(session, loan, view.status, view.outstanding)
    mark(session, row, "FINERACT_DONE")
    _save_transaction(session, loan, body.external_reference, "REPAYMENT", posted, view.status, view.outstanding)
    if fault == "crash_before_erp":
        log_event(
            "repayment",
            "FINERACT_DONE",
            external_reference=body.external_reference,
            loan_reference=loan_reference,
        )
        raise ApiError(500, "crashed", "process stopped before the ERP journal")
    return _complete_erp(
        session, settings, erp, loan, row, body.external_reference, "REPAYMENT", body.amount, posted, fault
    )


def recover(session: Session, settings: Settings, fineract: HttpFineract, erp: HttpErp) -> list[dict]:
    """Resume money operations left in progress. Lookup by external id happens before any new post."""
    rows = session.scalars(
        select(IdempotencyKey).where(
            IdempotencyKey.status.in_(("FINERACT_PENDING", "FINERACT_DONE", "ERP_PENDING", "NEEDS_RECONCILIATION")),
            IdempotencyKey.operation_type.in_(("DISBURSEMENT", "REPAYMENT")),
        )
    ).all()
    results = []
    for row in rows:
        results.append(_resume(session, settings, fineract, erp, row))
        log_event(
            "recover",
            results[-1]["status"],
            external_reference=row.external_reference,
            loan_reference=row.loan_reference,
            erp_transaction_id=row.external_reference if results[-1]["status"] == "COMPLETED" else None,
        )
    return results


def _resume(
    session: Session, settings: Settings, fineract: HttpFineract, erp: HttpErp, row: IdempotencyKey
) -> dict:
    if not row.loan_reference:
        return {"external_reference": row.external_reference, "status": row.status}
    loan = _require_loan(session, row.loan_reference)
    kind = row.operation_type
    if row.status == "FINERACT_PENDING":
        posted = fineract.find_transaction(loan.loan_reference, row.external_reference)
        if posted is None:
            _fail(session, row, "Fineract has no transaction for this reference")
            return {"external_reference": row.external_reference, "status": "FAILED"}
        view = fineract.find_loan(loan.loan_reference)
        if view is None:
            raise ApiError(502, "fineract_error", "Fineract loan was not visible during recovery")
        _remember_loan(session, loan, view.status, view.outstanding)
        mark(session, row, "FINERACT_DONE")
        _save_transaction(session, loan, row.external_reference, kind, posted, view.status, view.outstanding)
    transaction = session.scalar(
        select(PartnerTransaction).where(PartnerTransaction.external_reference == row.external_reference)
    )
    amount = Decimal(str(transaction.amount)) if transaction is not None else Decimal("0")
    _status, _body = _complete_erp(session, settings, erp, loan, row, row.external_reference, kind, amount)
    return {"external_reference": row.external_reference, "status": "COMPLETED"}


def _remember_loan_reference(session: Session, row: IdempotencyKey, loan_reference: str) -> None:
    if row.loan_reference == loan_reference:
        return
    row.loan_reference = loan_reference
    session.commit()


def _claim_disbursement_slot(session: Session, loan: LoanRecord, row: IdempotencyKey, external_reference: str) -> None:
    existing = session.scalar(select(LoanDisbursement).where(LoanDisbursement.loan_reference == loan.loan_reference))
    if existing is not None:
        if existing.external_reference == external_reference:
            loan.disbursement_reference = external_reference
            session.commit()
            return
        _fail(session, row, "loan already has a disbursement")
        raise ApiError(409, "conflict", "loan already has a disbursement")
    session.add(LoanDisbursement(loan_reference=loan.loan_reference, external_reference=external_reference))
    loan.disbursement_reference = external_reference
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        winner = session.scalar(select(LoanDisbursement).where(LoanDisbursement.loan_reference == loan.loan_reference))
        if winner is not None and winner.external_reference == external_reference:
            return
        _fail(session, row, "loan already has a disbursement")
        raise ApiError(409, "conflict", "loan already has a disbursement") from None


def get_transaction(session: Session, external_reference: str) -> dict:
    row = session.scalar(select(PartnerTransaction).where(PartnerTransaction.external_reference == external_reference))
    if row is None:
        raise ApiError(404, "not_found", "transaction does not exist")
    return _transaction_out(row).model_dump(mode="json")


def _complete_erp(
    session: Session,
    settings: Settings,
    erp: HttpErp,
    loan: LoanRecord,
    row: IdempotencyKey,
    external_reference: str,
    kind: str,
    amount: Decimal,
    posted: PostedTransaction | None = None,
    fault: str | None = None,
) -> tuple[int, dict]:
    transaction = session.scalar(
        select(PartnerTransaction).where(PartnerTransaction.external_reference == external_reference)
    )
    if transaction is None:
        raise ApiError(409, "needs_reconciliation", "Fineract succeeded but the local transaction row is missing")
    principal = Decimal(str(transaction.principal if posted is None else posted.principal))
    interest = Decimal(str(transaction.interest if posted is None else posted.interest))
    if row.status == "FINERACT_DONE":
        mark(session, row, "ERP_PENDING")
    elif row.status == "NEEDS_RECONCILIATION":
        mark(session, row, "ERP_PENDING")
    lines = _journal_lines(settings, kind, amount, principal, interest)
    try:
        if fault == "erp_down":
            raise GatewayError("erp_down", "ERP request failed", 503)
        if not erp.find_journal(external_reference):
            erp.create_journal(external_reference, lines)
        if fault == "erp_timeout_after_write":
            log_event(
                "journal",
                "ERP_PENDING",
                external_reference=external_reference,
                loan_reference=loan.loan_reference,
                erp_transaction_id=external_reference,
            )
            raise ApiError(504, "erp_timeout", "ERP timed out after the journal was written")
    except ApiError:
        raise
    except GatewayError as exc:
        mark(session, row, "NEEDS_RECONCILIATION", http_status=503, body={"status": "NEEDS_RECONCILIATION"})
        transaction.status = "NEEDS_RECONCILIATION"
        session.commit()
        log_event(
            "journal",
            "NEEDS_RECONCILIATION",
            external_reference=external_reference,
            loan_reference=loan.loan_reference,
        )
        raise ApiError(503, "needs_reconciliation", "Fineract succeeded and the ERP journal still needs posting") from exc
    transaction.status = "COMPLETED"
    session.commit()
    public = _transaction_out(transaction).model_dump(mode="json")
    log_event(
        kind.lower(),
        "COMPLETED",
        external_reference=external_reference,
        loan_reference=loan.loan_reference,
        erp_transaction_id=external_reference,
    )
    return _finish(session, row, public)


def _journal_lines(
    settings: Settings, kind: str, amount: Decimal, principal: Decimal, interest: Decimal
) -> list[tuple[str, Decimal, Decimal]]:
    if kind == "DISBURSEMENT":
        lines = disbursement_entry(amount, settings.bank_code, settings.receivable_code)
    else:
        lines = repayment_entry(principal, interest, settings.bank_code, settings.receivable_code, settings.income_code)
    return [(line.account_code, line.debit, line.credit) for line in lines]


def _save_transaction(
    session: Session,
    loan: LoanRecord,
    external_reference: str,
    kind: str,
    posted: PostedTransaction,
    fineract_status: str,
    outstanding: Decimal,
) -> None:
    existing = session.scalar(
        select(PartnerTransaction).where(PartnerTransaction.external_reference == external_reference)
    )
    if existing is None:
        existing = PartnerTransaction(external_reference=external_reference, loan_reference=loan.loan_reference, kind=kind, amount=posted.amount)
        session.add(existing)
    existing.principal = posted.principal
    existing.interest = posted.interest
    existing.amount = posted.amount
    existing.loan_status = public_loan_status(fineract_status)
    existing.fineract_status = fineract_status
    existing.outstanding = outstanding
    existing.status = "FINERACT_DONE"
    session.commit()


def _remember_loan(session: Session, loan: LoanRecord, fineract_status: str, outstanding: Decimal) -> None:
    loan.fineract_status = fineract_status
    loan.status = public_loan_status(fineract_status)
    loan.outstanding = outstanding
    session.commit()


def _require_loan(session: Session, loan_reference: str) -> LoanRecord:
    loan = session.scalar(select(LoanRecord).where(LoanRecord.loan_reference == loan_reference))
    if loan is None:
        raise ApiError(404, "not_found", "loan does not exist")
    return loan


def _loan_out(record: LoanRecord) -> LoanOut:
    return LoanOut(
        external_reference=record.external_reference,
        loan_reference=record.loan_reference,
        client_uid=record.client_uid,
        principal=Decimal(str(record.principal)),
        currency=record.currency,
        interest_rate=Decimal(str(record.interest_rate)),
        term_months=record.term_months,
        submitted_on=record.submitted_on,  # type: ignore[arg-type]
        status=record.status,
        fineract_status=record.fineract_status,
        outstanding=Decimal(str(record.outstanding)),
        disbursement_reference=record.disbursement_reference,
    )


def _transaction_out(row: PartnerTransaction) -> TransactionOut:
    return TransactionOut(
        external_reference=row.external_reference,
        loan_reference=row.loan_reference,
        kind=row.kind,
        amount=Decimal(str(row.amount)),
        principal=Decimal(str(row.principal)),
        interest=Decimal(str(row.interest)),
        status=row.status,
        loan_status=row.loan_status,
        fineract_status=row.fineract_status,
        outstanding=Decimal(str(row.outstanding)),
    )


def _begin(session: Session, operation: str, external_reference: str, payload: dict) -> IdempotencyKey | tuple[int, dict]:
    try:
        claimed = claim(session, operation, external_reference, payload)
    except IdempotencyConflict as exc:
        raise ApiError(409, "conflict", exc.message) from exc
    if claimed.kind == "replay":
        return claimed.status_code, claimed.body
    if claimed.kind == "in_progress":
        return claimed.status_code, claimed.body
    if claimed.row is None:
        raise ApiError(500, "internal", "idempotency row was not stored")
    return claimed.row


def _finish(session: Session, row: IdempotencyKey, public: dict) -> tuple[int, dict]:
    if row.status in {"RECEIVED", "FAILED"}:
        mark(session, row, "FINERACT_PENDING")
    if row.status == "FINERACT_PENDING":
        mark(session, row, "FINERACT_DONE")
    if row.status == "FINERACT_DONE":
        mark(session, row, "ERP_PENDING")
    mark(session, row, "COMPLETED", http_status=200, body=public)
    return 201, public


def _fail(session: Session, row: IdempotencyKey, message: str) -> None:
    if row.status == "RECEIVED":
        mark(session, row, "FAILED", http_status=409, body={"error": message})
        return
    if row.status == "FINERACT_PENDING":
        mark(session, row, "FAILED", http_status=502, body={"error": message})
