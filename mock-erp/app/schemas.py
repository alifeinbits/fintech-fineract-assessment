from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

AccountType = Literal["ASSET", "LIABILITY", "EQUITY", "INCOME", "EXPENSE"]


class AccountCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    type: AccountType
    currency: str = Field(min_length=3, max_length=3)
    external_reference: str = Field(min_length=1, max_length=100)


class AccountOut(BaseModel):
    code: str
    name: str
    type: AccountType
    currency: str
    external_reference: str


class JournalLineIn(BaseModel):
    account_code: str
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")


class JournalEntryCreate(BaseModel):
    external_reference: str = Field(min_length=1, max_length=100)
    lines: list[JournalLineIn] = Field(min_length=2)


class JournalLineOut(BaseModel):
    account_code: str
    debit: Decimal
    credit: Decimal


class JournalEntryOut(BaseModel):
    external_reference: str
    lines: list[JournalLineOut]


class BalanceOut(BaseModel):
    code: str
    currency: str
    balance: Decimal
