from datetime import date
from decimal import Decimal
from threading import Lock

from app.errors import GatewayError
from app.gateways import AccountSnapshot, ClientSnapshot, LoanSnapshot, PostedTransaction

SEED = (
    AccountSnapshot("1100", "Bank", "ASSET", "RWF", "GL-1100"),
    AccountSnapshot("1200", "Loan Receivable", "ASSET", "RWF", "GL-1200"),
    AccountSnapshot("4000", "Interest Income", "INCOME", "RWF", "GL-4000"),
)


class FakeErp:
    def __init__(self) -> None:
        self.accounts = list(SEED)
        self.journals: dict[str, list[tuple[str, Decimal, Decimal]]] = {}
        self.fail_journal = False
        self._lock = Lock()

    def list_accounts(self) -> list[AccountSnapshot]:
        return list(self.accounts)

    def find_journal(self, external_reference: str) -> bool:
        return external_reference in self.journals

    def journal_total(self, external_reference: str) -> Decimal | None:
        lines = self.journals.get(external_reference)
        if lines is None:
            return None
        return sum((debit for _code, debit, _credit in lines), Decimal("0"))

    def create_journal(self, external_reference: str, lines: list[tuple[str, Decimal, Decimal]]) -> None:
        with self._lock:
            if self.fail_journal:
                self.fail_journal = False
                raise GatewayError("erp_down", "ERP request failed", 503)
            self.journals[external_reference] = lines


class FakeFineract:
    def __init__(self) -> None:
        self.fail_gl: set[str] = set()
        self.gl_calls: list[str] = []
        self.clients: dict[str, int] = {}
        self.loans: dict[str, LoanSnapshot] = {}
        self.transactions: dict[tuple[str, str], PostedTransaction] = {}
        self.interest_for: dict[str, Decimal] = {}
        self.client_creates = 0
        self.loan_creates = 0
        self.disburse_posts = 0
        self.repay_posts = 0
        self.fail_disburse_remaining = 0
        self._next_id = 4242
        self._lock = Lock()

    def create_gl(self, account: AccountSnapshot) -> int:
        self.gl_calls.append(account.external_reference)
        if account.code in self.fail_gl:
            raise GatewayError("fineract_down", "Fineract request failed", 503)
        return self._next_id

    def find_client(self, client_uid: str) -> ClientSnapshot | None:
        fineract_id = self.clients.get(client_uid)
        if fineract_id is None:
            return None
        return ClientSnapshot(fineract_id=fineract_id)

    def create_client(self, client_uid: str, first_name: str, last_name: str, activation_date: date) -> int:
        self.client_creates += 1
        if client_uid not in self.clients:
            self.clients[client_uid] = self._next_id
            self._next_id += 1
        return self.clients[client_uid]

    def find_loan(self, loan_reference: str) -> LoanSnapshot | None:
        return self.loans.get(loan_reference)

    def create_loan(
        self,
        loan_reference: str,
        client_id: int,
        principal: Decimal,
        interest_rate: Decimal,
        term_months: int,
        submitted_on: date,
    ) -> int:
        self.loan_creates += 1
        snapshot = LoanSnapshot(self._next_id, "Submitted and pending approval", Decimal("0"))
        self._next_id += 1
        self.loans[loan_reference] = snapshot
        return snapshot.fineract_id

    def approve_loan(self, loan_reference: str, approved_on: date) -> None:
        current = self.loans[loan_reference]
        if public_is_past_submit(current.status):
            return
        self.loans[loan_reference] = LoanSnapshot(current.fineract_id, "Approved", Decimal("0"))

    def find_transaction(self, loan_reference: str, external_reference: str) -> PostedTransaction | None:
        return self.transactions.get((loan_reference, external_reference))

    def disburse(self, loan_reference: str, external_reference: str, amount: Decimal, on: date) -> PostedTransaction:
        with self._lock:
            existing = self.find_transaction(loan_reference, external_reference)
            if existing is not None:
                return existing
            if self.fail_disburse_remaining > 0:
                self.fail_disburse_remaining -= 1
                raise GatewayError("fineract_down", "Fineract request failed", 503)
            self.disburse_posts += 1
            posted = PostedTransaction(amount, amount, Decimal("0"))
            self.transactions[(loan_reference, external_reference)] = posted
            current = self.loans[loan_reference]
            self.loans[loan_reference] = LoanSnapshot(current.fineract_id, "Active", amount)
            return posted

    def repay(self, loan_reference: str, external_reference: str, amount: Decimal, on: date) -> PostedTransaction:
        with self._lock:
            existing = self.find_transaction(loan_reference, external_reference)
            if existing is not None:
                return existing
            self.repay_posts += 1
            interest = self.interest_for.get(external_reference, Decimal("0"))
            principal = amount - interest
            current = self.loans[loan_reference]
            remaining = current.outstanding - principal
            if remaining < 0:
                raise GatewayError("fineract_error", "Fineract rejected the request")
            status = "Closed (obligations met)" if remaining == 0 else "Active"
            self.loans[loan_reference] = LoanSnapshot(current.fineract_id, status, remaining)
            posted = PostedTransaction(amount, principal, interest)
            self.transactions[(loan_reference, external_reference)] = posted
            return posted


def public_is_past_submit(status: str) -> bool:
    text = status.lower()
    return "approved" in text or "active" in text or "closed" in text
