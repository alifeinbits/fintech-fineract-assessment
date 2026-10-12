from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class JournalLine:
    account_code: str
    debit: Decimal
    credit: Decimal


def disbursement_entry(amount: Decimal, bank_code: str, receivable_code: str) -> list[JournalLine]:
    """Cash disbursement: DR loan receivable, CR bank. Amount comes from Fineract."""
    if amount <= 0:
        raise ValueError("disbursement amount must be positive")
    return [
        JournalLine(receivable_code, amount, Decimal("0")),
        JournalLine(bank_code, Decimal("0"), amount),
    ]


def repayment_entry(
    principal: Decimal,
    interest: Decimal,
    bank_code: str,
    receivable_code: str,
    income_code: str,
) -> list[JournalLine]:
    """Cash repayment. Principal and interest are Fineract's transaction portions, not a local schedule.

    An overpayment portion would credit a liability account. A reversal would post the opposite
    lines with the original external reference plus a reversal suffix. Neither is posted here.
    """
    if principal < 0 or interest < 0:
        raise ValueError("principal and interest must be zero or positive")
    total = principal + interest
    if total <= 0:
        raise ValueError("repayment total must be positive")
    lines = [
        JournalLine(bank_code, total, Decimal("0")),
        JournalLine(receivable_code, Decimal("0"), principal),
    ]
    if interest > 0:
        lines.append(JournalLine(income_code, Decimal("0"), interest))
    return lines
