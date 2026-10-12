from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import httpx

from app.account_mapping import map_account_type
from app.errors import GatewayError
from app.settings import Settings

DATE_FORMAT = "yyyy-MM-dd"


@dataclass(frozen=True)
class AccountSnapshot:
    code: str
    name: str
    type: str
    currency: str
    external_reference: str


@dataclass(frozen=True)
class ClientSnapshot:
    fineract_id: int


@dataclass(frozen=True)
class LoanSnapshot:
    fineract_id: int
    status: str
    outstanding: Decimal


@dataclass(frozen=True)
class PostedTransaction:
    amount: Decimal
    principal: Decimal
    interest: Decimal


def public_loan_status(fineract_status: str) -> str:
    text = fineract_status.lower()
    if "closed" in text:
        return "CLOSED"
    if "active" in text:
        return "ACTIVE"
    if "approved" in text:
        return "APPROVED"
    return "SUBMITTED"


class HttpErp:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            base_url=settings.erp_base_url,
            headers={"Authorization": f"Bearer {settings.erp_api_token}"},
            timeout=30.0,
        )

    def list_accounts(self) -> list[AccountSnapshot]:
        payload = self._json("GET", "/api/accounts")
        return [
            AccountSnapshot(
                code=item["code"],
                name=item["name"],
                type=item["type"],
                currency=item["currency"],
                external_reference=item["external_reference"],
            )
            for item in payload
        ]

    def find_journal(self, external_reference: str) -> bool:
        response = self._send("GET", f"/api/journal-entries/{external_reference}")
        if response.status_code == 404:
            return False
        self._raise(response)
        return True

    def journal_total(self, external_reference: str) -> Decimal | None:
        response = self._send("GET", f"/api/journal-entries/{external_reference}")
        if response.status_code == 404:
            return None
        self._raise(response)
        return sum((Decimal(str(line["debit"])) for line in response.json()["lines"]), Decimal("0"))

    def create_journal(self, external_reference: str, lines: list[tuple[str, Decimal, Decimal]]) -> None:
        self._json(
            "POST",
            "/api/journal-entries",
            {
                "external_reference": external_reference,
                "lines": [
                    {"account_code": code, "debit": str(debit), "credit": str(credit)} for code, debit, credit in lines
                ],
            },
        )

    def _json(self, method: str, path: str, body: dict | None = None) -> object:
        response = self._send(method, path, body)
        self._raise(response)
        return response.json()

    def _send(self, method: str, path: str, body: dict | None = None) -> httpx.Response:
        try:
            return self._client.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise GatewayError("erp_down", "ERP request failed", 503) from exc

    def _raise(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        if response.status_code == 409:
            raise GatewayError("conflict", "ERP rejected a duplicate reference", 409)
        if response.status_code == 422:
            raise GatewayError("unbalanced", "ERP rejected the journal", 422)
        raise GatewayError("erp_down", "ERP request failed", 503)


class HttpFineract:
    """Calls Fineract by external id. Numeric ids are kept inside snapshots and are not part of the partner API."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.Client(
            base_url=settings.fineract_base_url,
            auth=(settings.fineract_username, settings.fineract_password),
            headers={"Fineract-Platform-TenantId": settings.fineract_tenant},
            verify=settings.fineract_verify_ssl,
            timeout=30.0,
        )

    def create_gl(self, account: AccountSnapshot) -> int:
        payload = self._command(
            "POST",
            "/glaccounts",
            {
                "name": account.name,
                "glCode": account.code,
                "type": map_account_type(account.type),
                "usage": 1,
                "manualEntriesAllowed": True,
                "description": account.name,
                "externalId": account.external_reference,
                "currencyCode": account.currency,
            },
        )
        return int(payload["resourceId"])

    def find_client(self, client_uid: str) -> ClientSnapshot | None:
        payload = self._get(f"/clients/external-id/{client_uid}")
        if payload is None:
            return None
        return ClientSnapshot(fineract_id=int(payload["id"]))

    def create_client(self, client_uid: str, first_name: str, last_name: str, activation_date: date) -> int:
        payload = self._command(
            "POST",
            "/clients",
            {
                "officeId": self._settings.fineract_office_id,
                "firstname": first_name,
                "lastname": last_name,
                "externalId": client_uid,
                "active": True,
                "activationDate": activation_date.isoformat(),
                "legalFormId": self._settings.fineract_legal_form_id,
                **self._date_fields(),
            },
        )
        return int(payload.get("clientId") or payload["resourceId"])

    def find_loan(self, loan_reference: str) -> LoanSnapshot | None:
        payload = self._get(f"/loans/external-id/{loan_reference}")
        if payload is None:
            return None
        status = payload.get("status") or {}
        summary = payload.get("summary") or {}
        outstanding = summary.get("totalOutstanding", summary.get("principalOutstanding", 0))
        return LoanSnapshot(
            fineract_id=int(payload["id"]),
            status=str(status.get("value") or ""),
            outstanding=Decimal(str(outstanding)),
        )

    def create_loan(
        self,
        loan_reference: str,
        client_id: int,
        principal: Decimal,
        interest_rate: Decimal,
        term_months: int,
        submitted_on: date,
    ) -> int:
        payload = self._command(
            "POST",
            "/loans",
            {
                "clientId": client_id,
                "productId": self._settings.fineract_loan_product_id,
                "principal": str(principal),
                "loanTermFrequency": term_months,
                "loanTermFrequencyType": 2,
                "numberOfRepayments": term_months,
                "repaymentEvery": 1,
                "repaymentFrequencyType": 2,
                "interestRatePerPeriod": str(interest_rate),
                "interestRateFrequencyType": 3,
                "amortizationType": 1,
                "interestType": 0,
                "interestCalculationPeriodType": 1,
                "transactionProcessingStrategyCode": self._settings.fineract_strategy_code,
                "expectedDisbursementDate": submitted_on.isoformat(),
                "submittedOnDate": submitted_on.isoformat(),
                "externalId": loan_reference,
                "loanType": "individual",
                **self._date_fields(),
            },
        )
        return int(payload.get("loanId") or payload["resourceId"])

    def approve_loan(self, loan_reference: str, approved_on: date) -> None:
        current = self.find_loan(loan_reference)
        if current is not None and "approved" in current.status.lower():
            return
        if current is not None and public_loan_status(current.status) in {"ACTIVE", "CLOSED"}:
            return
        self._command(
            "POST",
            f"/loans/external-id/{loan_reference}",
            {"approvedOnDate": approved_on.isoformat(), **self._date_fields()},
            params={"command": "approve"},
        )

    def find_transaction(self, loan_reference: str, external_reference: str) -> PostedTransaction | None:
        payload = self._get(
            f"/loans/external-id/{loan_reference}/transactions/external-id/{external_reference}"
        )
        if payload is None:
            return None
        return _transaction_from_payload(payload)

    def disburse(self, loan_reference: str, external_reference: str, amount: Decimal, on: date) -> PostedTransaction:
        existing = self.find_transaction(loan_reference, external_reference)
        if existing is not None:
            return existing
        # Disbursement is a loan state transition, not a /transactions command.
        self._command(
            "POST",
            f"/loans/external-id/{loan_reference}",
            {
                "actualDisbursementDate": on.isoformat(),
                "transactionAmount": str(amount),
                "externalId": external_reference,
                "paymentTypeId": self._settings.fineract_payment_type_id,
                **self._date_fields(),
            },
            params={"command": "disburse"},
        )
        posted = self.find_transaction(loan_reference, external_reference)
        if posted is None:
            raise GatewayError("fineract_error", "Fineract disbursement was not visible after lookup")
        return posted

    def repay(self, loan_reference: str, external_reference: str, amount: Decimal, on: date) -> PostedTransaction:
        existing = self.find_transaction(loan_reference, external_reference)
        if existing is not None:
            return existing
        self._command(
            "POST",
            f"/loans/external-id/{loan_reference}/transactions",
            {
                "transactionDate": on.isoformat(),
                "transactionAmount": str(amount),
                "externalId": external_reference,
                "paymentTypeId": self._settings.fineract_payment_type_id,
                **self._date_fields(),
            },
            params={"command": "repayment"},
        )
        posted = self.find_transaction(loan_reference, external_reference)
        if posted is None:
            raise GatewayError("fineract_error", "Fineract repayment was not visible after lookup")
        return posted

    def _date_fields(self) -> dict[str, str]:
        return {"dateFormat": DATE_FORMAT, "locale": "en"}

    def _get(self, path: str) -> dict | None:
        response = self._send("GET", path)
        if response.status_code == 404:
            return None
        self._raise(response)
        return response.json()

    def _command(self, method: str, path: str, body: dict, params: dict | None = None) -> dict:
        response = self._send(method, path, body, params)
        self._raise(response)
        return response.json()

    def _send(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> httpx.Response:
        try:
            return self._client.request(
                method,
                path,
                json=body,
                params=params,
                headers={"Fineract-Platform-TenantId": self._settings.fineract_tenant},
            )
        except httpx.HTTPError as exc:
            raise GatewayError("fineract_down", "Fineract request failed", 503) from exc

    def _raise(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        text = response.text
        if response.status_code == 403 and "externalid.conflict" in text:
            raise GatewayError("conflict", "external reference already exists with a different payload", 409)
        if response.status_code in {502, 503, 504}:
            raise GatewayError("fineract_down", "Fineract request failed", 503)
        raise GatewayError("fineract_error", "Fineract rejected the request")


def _transaction_from_payload(payload: dict) -> PostedTransaction:
    amount = Decimal(str(payload.get("amount") or 0))
    principal = Decimal(str(payload.get("principalPortion") or 0))
    interest = Decimal(str(payload.get("interestPortion") or 0))
    if principal == 0 and interest == 0:
        principal = amount
    return PostedTransaction(amount=amount, principal=principal, interest=interest)
