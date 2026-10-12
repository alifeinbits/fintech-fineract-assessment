import json
from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings
from tests.fakes import FakeErp, FakeFineract

FORBIDDEN_KEYS = {
    "fineract_client_id",
    "fineract_loan_id",
    "fineract_resource_id",
    "resourceId",
    "clientId",
    "loanId",
    "officeId",
}


def _client(tmp_path, fineract: FakeFineract | None = None, erp: FakeErp | None = None):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/integration.db", retry_backoff_seconds=0)
    fineract = fineract or FakeFineract()
    erp = erp or FakeErp()
    app = create_app(settings, fineract=fineract, erp=erp)
    return TestClient(app), fineract, erp


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer assessment-token"}


def _assert_no_private_ids(payload: object) -> None:
    if isinstance(payload, dict):
        assert FORBIDDEN_KEYS.isdisjoint(payload)
        for value in payload.values():
            _assert_no_private_ids(value)
    elif isinstance(payload, list):
        for item in payload:
            _assert_no_private_ids(item)


def test_health_is_open_and_api_requires_token(tmp_path):
    client, _, _ = _client(tmp_path)
    assert client.get("/health").json() == {"status": "ok"}
    denied = client.post("/api/accounts/sync")
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "unauthorized"
    assert denied.headers["X-Correlation-ID"]
    assert denied.json()["error"]["correlation_id"] == denied.headers["X-Correlation-ID"]


def test_account_sync_is_create_only(tmp_path):
    client, fineract, _ = _client(tmp_path)
    first = client.post("/api/accounts/sync", headers=_headers())
    assert first.status_code == 200
    assert [row["external_reference"] for row in first.json()["accounts"]] == ["GL-1100", "GL-1200", "GL-4000"]
    assert {row["status"] for row in first.json()["accounts"]} == {"SYNCED"}
    for _ in range(3):
        again = client.post("/api/accounts/sync", headers=_headers())
        assert again.status_code == 200
        assert again.json() == first.json()
    assert fineract.gl_calls == ["GL-1100", "GL-1200", "GL-4000"]
    _assert_no_private_ids(first.json())


def test_account_sync_retries_a_failed_gl(tmp_path):
    fineract = FakeFineract()
    fineract.fail_gl.add("1200")
    client, fineract, _ = _client(tmp_path, fineract)
    failed = client.post("/api/accounts/sync", headers=_headers())
    by_code = {row["code"]: row["status"] for row in failed.json()["accounts"]}
    assert by_code == {"1100": "SYNCED", "1200": "FAILED", "4000": "SYNCED"}
    fineract.fail_gl.clear()
    retried = client.post("/api/accounts/sync", headers=_headers())
    assert {row["status"] for row in retried.json()["accounts"]} == {"SYNCED"}
    assert fineract.gl_calls.count("GL-1200") == 2
    assert fineract.gl_calls.count("GL-1100") == 1


def test_client_idempotency_and_uid_conflict(tmp_path):
    client, fineract, _ = _client(tmp_path)
    body = {
        "external_reference": "CL-OP-1",
        "client_uid": "CLT-1",
        "first_name": "Amina",
        "last_name": "Uwase",
        "activation_date": "2026-10-07",
    }
    created = client.post("/api/clients", headers=_headers(), json=body)
    assert created.status_code == 201
    _assert_no_private_ids(created.json())
    replay = client.post("/api/clients", headers=_headers(), json=body)
    assert replay.status_code == 200
    assert replay.json() == created.json()
    assert fineract.client_creates == 1
    conflict = client.post("/api/clients", headers=_headers(), json={**body, "first_name": "Other"})
    assert conflict.status_code == 409
    other_ref = client.post(
        "/api/clients",
        headers=_headers(),
        json={**body, "external_reference": "CL-OP-2"},
    )
    assert other_ref.status_code == 409
    assert other_ref.json()["error"]["code"] == "conflict"
    fetched = client.get("/api/clients/CLT-1", headers=_headers())
    assert fetched.status_code == 200
    assert fetched.json()["client_uid"] == "CLT-1"
    assert "4242" not in json.dumps(fetched.json())


def test_loan_requires_client_and_replays(tmp_path):
    client, fineract, _ = _client(tmp_path)
    loan = {
        "external_reference": "LN-OP-1",
        "loan_reference": "LN-1",
        "client_uid": "CLT-1",
        "principal": "5000000",
        "currency": "RWF",
        "interest_rate": "18",
        "term_months": 12,
        "submitted_on": "2026-10-07",
    }
    missing = client.post("/api/loans", headers=_headers(), json=loan)
    assert missing.status_code == 404
    _create_client(client)
    created = client.post("/api/loans", headers=_headers(), json=loan)
    assert created.status_code == 201
    assert created.json()["status"] == "APPROVED"
    assert created.json()["loan_reference"] == "LN-1"
    assert Decimal(created.json()["outstanding"]) == Decimal("5000000")
    _assert_no_private_ids(created.json())
    replay = client.post("/api/loans", headers=_headers(), json=loan)
    assert replay.status_code == 200
    assert replay.json() == created.json()
    assert fineract.loan_creates == 1
    duplicate = client.post("/api/loans", headers=_headers(), json={**loan, "external_reference": "LN-OP-2"})
    assert duplicate.status_code == 409
    fetched = client.get("/api/loans/LN-1", headers=_headers())
    assert fetched.json()["fineract_status"] == "Approved"
    assert "4242" not in json.dumps(fetched.json())


def test_disbursement_is_once_per_loan_and_posts_one_journal(tmp_path):
    client, fineract, erp = _client(tmp_path)
    _create_client(client)
    _create_loan(client)
    body = {"external_reference": "DISB-1", "amount": "5000000", "transaction_date": "2026-10-07"}
    first = client.post("/api/loans/LN-1/disburse", headers=_headers(), json=body)
    assert first.status_code == 201
    assert first.json()["loan_status"] == "ACTIVE"
    assert Decimal(first.json()["outstanding"]) == Decimal("5000000")
    _assert_no_private_ids(first.json())
    for _ in range(3):
        replay = client.post("/api/loans/LN-1/disburse", headers=_headers(), json=body)
        assert replay.status_code == 200
        assert replay.json() == first.json()
    assert fineract.disburse_posts == 1
    assert list(erp.journals) == ["DISB-1"]
    lines = erp.journals["DISB-1"]
    assert sum(debit for _, debit, _ in lines) == sum(credit for _, _, credit in lines)
    other = client.post(
        "/api/loans/LN-1/disburse",
        headers=_headers(),
        json={**body, "external_reference": "DISB-2"},
    )
    assert other.status_code == 409
    fetched = client.get("/api/transactions/DISB-1", headers=_headers())
    assert fetched.status_code == 200
    assert fetched.json()["kind"] == "DISBURSEMENT"


def test_repayments_are_unique_per_reference_and_can_close_the_loan(tmp_path):
    client, fineract, erp = _client(tmp_path)
    _create_client(client)
    _create_loan(client)
    client.post(
        "/api/loans/LN-1/disburse",
        headers=_headers(),
        json={"external_reference": "DISB-1", "amount": "5000000", "transaction_date": "2026-10-07"},
    )
    fineract.interest_for["REP-1"] = Decimal("75000")
    first = client.post(
        "/api/loans/LN-1/repayments",
        headers=_headers(),
        json={"external_reference": "REP-1", "amount": "475000", "transaction_date": "2026-11-07"},
    )
    assert first.status_code == 201
    assert Decimal(first.json()["principal"]) == Decimal("400000")
    assert Decimal(first.json()["interest"]) == Decimal("75000")
    income = [line for line in erp.journals["REP-1"] if line[0] == "4000"]
    assert income[0][2] == Decimal("75000")
    replay = client.post(
        "/api/loans/LN-1/repayments",
        headers=_headers(),
        json={"external_reference": "REP-1", "amount": "475000", "transaction_date": "2026-11-07"},
    )
    assert replay.status_code == 200
    assert fineract.repay_posts == 1
    for reference, amount, on in (
        ("REP-2", "1000000", "2026-12-07"),
        ("REP-3", "1000000", "2027-01-07"),
        ("REP-4", "2600000", "2027-02-07"),
    ):
        paid = client.post(
            "/api/loans/LN-1/repayments",
            headers=_headers(),
            json={"external_reference": reference, "amount": amount, "transaction_date": on},
        )
        assert paid.status_code == 201
    loan = client.get("/api/loans/LN-1", headers=_headers())
    assert loan.json()["fineract_status"] == "Closed (obligations met)"
    assert loan.json()["status"] == "CLOSED"
    assert Decimal(loan.json()["outstanding"]) == Decimal("0")
    assert fineract.repay_posts == 4


def test_erp_outage_after_disbursement_is_reconciled_without_a_second_post(tmp_path):
    client, fineract, erp = _client(tmp_path)
    _create_client(client)
    _create_loan(client)
    erp.fail_journal = True
    body = {"external_reference": "DISB-1", "amount": "5000000", "transaction_date": "2026-10-07"}
    failed = client.post("/api/loans/LN-1/disburse", headers=_headers(), json=body)
    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "needs_reconciliation"
    assert fineract.disburse_posts == 1
    recovered = client.post("/api/loans/LN-1/disburse", headers=_headers(), json=body)
    assert recovered.status_code == 201
    assert fineract.disburse_posts == 1
    assert "DISB-1" in erp.journals


def test_validation_uses_the_error_shape(tmp_path):
    client, _, _ = _client(tmp_path)
    response = client.post("/api/clients", headers=_headers(), json={"client_uid": "CLT-1"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_openapi_lists_the_partner_paths(tmp_path):
    client, _, _ = _client(tmp_path)
    paths = client.get("/openapi.json").json()["paths"]
    for path in (
        "/api/accounts/sync",
        "/api/clients",
        "/api/clients/{client_uid}",
        "/api/loans",
        "/api/loans/{loan_reference}",
        "/api/loans/{loan_reference}/disburse",
        "/api/loans/{loan_reference}/repayments",
        "/api/transactions/{external_reference}",
        "/api/admin/recover",
        "/api/reconciliation",
    ):
        assert path in paths


def _create_client(client: TestClient) -> None:
    response = client.post(
        "/api/clients",
        headers=_headers(),
        json={
            "external_reference": "CL-OP-1",
            "client_uid": "CLT-1",
            "first_name": "Amina",
            "last_name": "Uwase",
            "activation_date": "2026-10-07",
        },
    )
    assert response.status_code == 201


def _create_loan(client: TestClient) -> None:
    response = client.post(
        "/api/loans",
        headers=_headers(),
        json={
            "external_reference": "LN-OP-1",
            "loan_reference": "LN-1",
            "client_uid": "CLT-1",
            "principal": "5000000",
            "currency": "RWF",
            "interest_rate": "18",
            "term_months": 12,
            "submitted_on": "2026-10-07",
        },
    )
    assert response.status_code == 201
