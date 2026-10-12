from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("code", name="uq_accounts_code"),
        UniqueConstraint("external_reference", name="uq_accounts_external_reference"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(3))
    external_reference: Mapped[str] = mapped_column(String(100))
    payload_hash: Mapped[str] = mapped_column(String(64))

    lines: Mapped[list["JournalLine"]] = relationship(back_populates="account")


class JournalEntry(Base):
    __tablename__ = "journal_entries"
    __table_args__ = (UniqueConstraint("external_reference", name="uq_journal_external_reference"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    external_reference: Mapped[str] = mapped_column(String(100))
    payload_hash: Mapped[str] = mapped_column(String(64))
    lines: Mapped[list["JournalLine"]] = relationship(back_populates="entry", cascade="all, delete-orphan")


class JournalLine(Base):
    __tablename__ = "journal_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    debit: Mapped[Decimal] = mapped_column(Numeric(19, 6))
    credit: Mapped[Decimal] = mapped_column(Numeric(19, 6))

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship(back_populates="lines")
