from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

AccountSyncStatus = Literal["SYNC_PENDING", "SYNCED", "FAILED"]


class AccountSyncOut(BaseModel):
    code: str
    name: str
    type: str
    currency: str
    external_reference: str
    status: AccountSyncStatus
    error: str | None = None


class ClientCreate(BaseModel):
    external_reference: str = Field(min_length=1, max_length=100)
    client_uid: str = Field(min_length=1, max_length=100)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    activation_date: date


class ClientOut(BaseModel):
    external_reference: str
    client_uid: str
    first_name: str
    last_name: str


class LoanCreate(BaseModel):
    external_reference: str = Field(min_length=1, max_length=100)
    loan_reference: str = Field(min_length=1, max_length=100)
    client_uid: str = Field(min_length=1, max_length=100)
    principal: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3, default="RWF")
    interest_rate: Decimal = Field(default=Decimal("18"), ge=0)
    term_months: int = Field(default=12, ge=1, le=360)
    submitted_on: date


class LoanOut(BaseModel):
    external_reference: str
    loan_reference: str
    client_uid: str
    principal: Decimal
    currency: str
    interest_rate: Decimal
    term_months: int
    submitted_on: date
    status: str
    fineract_status: str
    outstanding: Decimal
    disbursement_reference: str | None = None


class DisbursementCreate(BaseModel):
    external_reference: str = Field(min_length=1, max_length=100)
    amount: Decimal = Field(gt=0)
    transaction_date: date


class RepaymentCreate(BaseModel):
    external_reference: str = Field(min_length=1, max_length=100)
    amount: Decimal = Field(gt=0)
    transaction_date: date


class TransactionOut(BaseModel):
    external_reference: str
    loan_reference: str
    kind: str
    amount: Decimal
    principal: Decimal
    interest: Decimal
    status: str
    loan_status: str
    fineract_status: str
    outstanding: Decimal


class ReconcileItem(BaseModel):
    loan_reference: str = Field(min_length=1, max_length=100)
    external_reference: str = Field(min_length=1, max_length=100)


class ReconcileRequest(BaseModel):
    items: list[ReconcileItem] = Field(min_length=1, max_length=100)
