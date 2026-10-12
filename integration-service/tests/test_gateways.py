from datetime import date
from decimal import Decimal

import httpx

from app.gateways import AccountSnapshot, HttpFineract
from app.settings import Settings


def test_fineract_client_uses_external_ids_for_loan_commands():
    calls: list[tuple[str, str, str | None]] = []
    loan_exists = False
    approved = False
    disbursed = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal loan_exists, approved, disbursed
        command = request.url.params.get("command")
        path = request.url.path
        calls.append((request.method, path, command))
        assert request.headers["Fineract-Platform-TenantId"] == "default"
        if path.endswith("/glaccounts") and request.method == "POST":
            body = request.read()
            assert b'"externalId":"GL-1200"' in body
            assert b'"currencyCode":"RWF"' in body
            return httpx.Response(200, json={"resourceId": 4})
        if path.endswith("/loans") and request.method == "POST":
            body = request.read()
            assert b'"externalId":"LN-1"' in body
            loan_exists = True
            return httpx.Response(200, json={"loanId": 15, "resourceId": 15})
        if path.endswith("/loans/external-id/LN-1") and command == "approve":
            approved = True
            return httpx.Response(200, json={"resourceId": 15})
        if path.endswith("/loans/external-id/LN-1") and request.method == "GET":
            if not loan_exists:
                return httpx.Response(404)
            status = "Approved" if approved else "Submitted and pending approval"
            return httpx.Response(200, json={"id": 15, "status": {"value": status}, "summary": {}})
        if path.endswith("/transactions/external-id/DISB-1") and request.method == "GET":
            if not disbursed:
                return httpx.Response(404)
            return httpx.Response(200, json={"amount": 5000000, "principalPortion": 5000000, "interestPortion": 0})
        if path.endswith("/loans/external-id/LN-1") and command == "disburse":
            body = request.read()
            assert b'"externalId":"DISB-1"' in body
            assert b'"actualDisbursementDate"' in body
            disbursed = True
            return httpx.Response(200, json={"resourceId": 90})
        return httpx.Response(404)

    settings = Settings(fineract_base_url="http://fineract/fineract-provider/api/v1", fineract_verify_ssl=False)
    gateway = HttpFineract(
        settings,
        client=httpx.Client(base_url=settings.fineract_base_url, transport=httpx.MockTransport(handler)),
    )

    assert gateway.find_loan("LN-1") is None
    gateway.create_loan("LN-1", 7, Decimal("5000000"), Decimal("18"), 12, date(2026, 10, 7))
    gateway.approve_loan("LN-1", date(2026, 10, 7))
    posted = gateway.disburse("LN-1", "DISB-1", Decimal("5000000"), date(2026, 10, 7))
    again = gateway.disburse("LN-1", "DISB-1", Decimal("5000000"), date(2026, 10, 7))
    assert posted.principal == Decimal("5000000")
    assert again.principal == posted.principal
    assert gateway.create_gl(AccountSnapshot("1200", "Loan Receivable", "ASSET", "RWF", "GL-1200")) == 4
    assert [call for call in calls if call[2] == "disburse"] == [
        ("POST", "/fineract-provider/api/v1/loans/external-id/LN-1", "disburse")
    ]
