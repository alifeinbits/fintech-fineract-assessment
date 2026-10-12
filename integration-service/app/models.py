from decimal import Decimal

from sqlalchemy import Numeric, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("operation_type", "external_reference", name="uq_operation_reference"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_type: Mapped[str] = mapped_column(String(64))
    external_reference: Mapped[str] = mapped_column(String(100))
    payload_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="RECEIVED")
    http_status: Mapped[int] = mapped_column(nullable=True)
    response_body: Mapped[str] = mapped_column(String(8000), nullable=True)
    loan_reference: Mapped[str] = mapped_column(String(100), nullable=True)


class AccountSync(Base):
    __tablename__ = "account_syncs"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    account_type: Mapped[str] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(3))
    external_reference: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="SYNC_PENDING")
    error_message: Mapped[str] = mapped_column(String(500), nullable=True)
    fineract_resource_id: Mapped[int] = mapped_column(nullable=True)


class ClientRecord(Base):
    __tablename__ = "clients"
    __table_args__ = (UniqueConstraint("client_uid", name="uq_client_uid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    external_reference: Mapped[str] = mapped_column(String(100), unique=True)
    client_uid: Mapped[str] = mapped_column(String(100))
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    fineract_client_id: Mapped[int] = mapped_column(nullable=True)


class LoanRecord(Base):
    __tablename__ = "loans"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_reference: Mapped[str] = mapped_column(String(100), unique=True)
    loan_reference: Mapped[str] = mapped_column(String(100), unique=True)
    client_uid: Mapped[str] = mapped_column(String(100))
    principal: Mapped[Decimal] = mapped_column(Numeric(19, 6))
    currency: Mapped[str] = mapped_column(String(3))
    interest_rate: Mapped[Decimal] = mapped_column(Numeric(9, 4))
    term_months: Mapped[int]
    submitted_on: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(32), default="SUBMITTED")
    fineract_status: Mapped[str] = mapped_column(String(80), default="")
    outstanding: Mapped[Decimal] = mapped_column(Numeric(19, 6), default=0)
    disbursement_reference: Mapped[str] = mapped_column(String(100), unique=True, nullable=True)
    fineract_loan_id: Mapped[int] = mapped_column(nullable=True)


class LoanDisbursement(Base):
    """One row per loan. The unique loan_reference is what stops a second disbursement."""

    __tablename__ = "loan_disbursements"

    id: Mapped[int] = mapped_column(primary_key=True)
    loan_reference: Mapped[str] = mapped_column(String(100), unique=True)
    external_reference: Mapped[str] = mapped_column(String(100), unique=True)


class PartnerTransaction(Base):
    __tablename__ = "partner_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_reference: Mapped[str] = mapped_column(String(100), unique=True)
    loan_reference: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(32))
    amount: Mapped[Decimal] = mapped_column(Numeric(19, 6))
    principal: Mapped[Decimal] = mapped_column(Numeric(19, 6), default=0)
    interest: Mapped[Decimal] = mapped_column(Numeric(19, 6), default=0)
    status: Mapped[str] = mapped_column(String(32), default="RECEIVED")
    loan_status: Mapped[str] = mapped_column(String(32), default="")
    fineract_status: Mapped[str] = mapped_column(String(80), default="")
    outstanding: Mapped[Decimal] = mapped_column(Numeric(19, 6), default=0)
