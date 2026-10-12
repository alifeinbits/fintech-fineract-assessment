from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import ApiError
from app.hashing import canonical_hash
from app.models import Account, JournalEntry, JournalLine
from app.schemas import AccountCreate, AccountOut, BalanceOut, JournalEntryCreate, JournalEntryOut, JournalLineOut

DEBIT_NORMAL = {"ASSET", "EXPENSE"}
SEED_ACCOUNTS = (
    AccountCreate(code="1100", name="Bank", type="ASSET", currency="RWF", external_reference="GL-1100"),
    AccountCreate(
        code="1200",
        name="Loan Receivable",
        type="ASSET",
        currency="RWF",
        external_reference="GL-1200",
    ),
    AccountCreate(
        code="4000",
        name="Interest Income",
        type="INCOME",
        currency="RWF",
        external_reference="GL-4000",
    ),
)


def to_account_out(account: Account) -> AccountOut:
    return AccountOut(
        code=account.code,
        name=account.name,
        type=account.type,  # type: ignore[arg-type]
        currency=account.currency,
        external_reference=account.external_reference,
    )


def to_journal_out(entry: JournalEntry) -> JournalEntryOut:
    return JournalEntryOut(
        external_reference=entry.external_reference,
        lines=[
            JournalLineOut(account_code=line.account.code, debit=line.debit, credit=line.credit) for line in entry.lines
        ],
    )


def seed_chart(session: Session) -> None:
    existing = session.scalar(select(Account.id).limit(1))
    if existing is not None:
        return
    for account in SEED_ACCOUNTS:
        create_account(session, account)


def create_account(session: Session, body: AccountCreate) -> tuple[AccountOut, bool]:
    body = body.model_copy(update={"currency": body.currency.upper()})
    payload = body.model_dump(mode="json")
    digest = canonical_hash(payload)
    existing = session.scalar(select(Account).where(Account.external_reference == body.external_reference))
    if existing is not None:
        if existing.payload_hash != digest:
            raise ApiError(409, "conflict", "external_reference already used with a different payload")
        return to_account_out(existing), False

    by_code = session.scalar(select(Account).where(Account.code == body.code))
    if by_code is not None:
        raise ApiError(409, "conflict", "account code already exists")

    account = Account(
        code=body.code,
        name=body.name,
        type=body.type,
        currency=body.currency.upper(),
        external_reference=body.external_reference,
        payload_hash=digest,
    )
    session.add(account)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raced = session.scalar(select(Account).where(Account.external_reference == body.external_reference))
        if raced is not None and raced.payload_hash == digest:
            return to_account_out(raced), False
        raise ApiError(409, "conflict", "account already exists") from None
    session.refresh(account)
    return to_account_out(account), True


def list_accounts(session: Session) -> list[AccountOut]:
    rows = session.scalars(select(Account).order_by(Account.code)).all()
    return [to_account_out(row) for row in rows]


def _require_account(session: Session, code: str) -> Account:
    account = session.scalar(select(Account).where(Account.code == code))
    if account is None:
        raise ApiError(404, "not_found", f"account {code} does not exist")
    return account


def account_balance(session: Session, code: str) -> BalanceOut:
    account = _require_account(session, code)
    lines = session.scalars(select(JournalLine).where(JournalLine.account_id == account.id)).all()
    debits = sum((line.debit for line in lines), Decimal("0"))
    credits = sum((line.credit for line in lines), Decimal("0"))
    balance = debits - credits if account.type in DEBIT_NORMAL else credits - debits
    return BalanceOut(code=account.code, currency=account.currency, balance=balance)


def _validate_lines(session: Session, body: JournalEntryCreate) -> list[tuple[Account, Decimal, Decimal]]:
    parsed: list[tuple[Account, Decimal, Decimal]] = []
    total_debit = Decimal("0")
    total_credit = Decimal("0")
    for line in body.lines:
        debit = Decimal(line.debit)
        credit = Decimal(line.credit)
        if debit < 0 or credit < 0:
            raise ApiError(422, "validation_error", "debit and credit must be zero or positive")
        if (debit > 0 and credit > 0) or (debit == 0 and credit == 0):
            raise ApiError(422, "validation_error", "each line must have either a debit or a credit")
        account = _require_account(session, line.account_code)
        parsed.append((account, debit, credit))
        total_debit += debit
        total_credit += credit
    if total_debit != total_credit:
        raise ApiError(422, "unbalanced", "journal entry debits must equal credits")
    return parsed


def create_journal_entry(session: Session, body: JournalEntryCreate) -> tuple[JournalEntryOut, bool]:
    payload = body.model_dump(mode="json")
    digest = canonical_hash(payload)
    existing = session.scalar(select(JournalEntry).where(JournalEntry.external_reference == body.external_reference))
    if existing is not None:
        if existing.payload_hash != digest:
            raise ApiError(409, "conflict", "external_reference already used with a different payload")
        return to_journal_out(existing), False

    parsed = _validate_lines(session, body)
    entry = JournalEntry(external_reference=body.external_reference, payload_hash=digest)
    for account, debit, credit in parsed:
        entry.lines.append(JournalLine(account=account, debit=debit, credit=credit))
    session.add(entry)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raced = session.scalar(select(JournalEntry).where(JournalEntry.external_reference == body.external_reference))
        if raced is not None and raced.payload_hash == digest:
            return to_journal_out(raced), False
        raise ApiError(409, "conflict", "journal entry already exists") from None
    session.refresh(entry)
    return to_journal_out(entry), True


def get_journal_entry(session: Session, external_reference: str) -> JournalEntryOut:
    entry = session.scalar(select(JournalEntry).where(JournalEntry.external_reference == external_reference))
    if entry is None:
        raise ApiError(404, "not_found", "journal entry does not exist")
    return to_journal_out(entry)
