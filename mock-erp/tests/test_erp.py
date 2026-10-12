from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/erp.db",
        api_token="test-token",
        faults_enabled=True,
        fault_sleep_seconds=0.01,
    )
    application = create_app(settings)
    with TestClient(application) as test_client:
        test_client.headers.update({"Authorization": "Bearer test-token"})
        yield test_client


def test_seed_chart_and_reject_missing_token(client: TestClient):
    bare = TestClient(client.app)
    assert bare.get("/api/accounts").status_code == 401
    response = client.get("/api/accounts")
    assert response.status_code == 200
    codes = {row["code"] for row in response.json()}
    assert codes == {"1100", "1200", "4000"}


def test_account_idempotency_and_code_conflict(client: TestClient):
    payload = {
        "code": "2100",
        "name": "Partner Payable",
        "type": "LIABILITY",
        "currency": "RWF",
        "external_reference": "GL-2100",
    }
    first = client.post("/api/accounts", json=payload)
    second = client.post("/api/accounts", json=payload)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()

    changed = dict(payload, name="Other")
    conflict = client.post("/api/accounts", json=changed)
    assert conflict.status_code == 409

    reused_code = dict(payload, external_reference="GL-2100-B")
    assert client.post("/api/accounts", json=reused_code).status_code == 409


def test_journal_must_balance_and_is_idempotent(client: TestClient):
    payload = {
        "external_reference": "JE-1",
        "lines": [
            {"account_code": "1200", "debit": "5000000", "credit": "0"},
            {"account_code": "1100", "debit": "0", "credit": "5000000"},
        ],
    }
    created = client.post("/api/journal-entries", json=payload)
    replay = client.post("/api/journal-entries", json=payload)
    assert created.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["external_reference"] == "JE-1"

    unbalanced = {
        "external_reference": "JE-2",
        "lines": [
            {"account_code": "1200", "debit": "10", "credit": "0"},
            {"account_code": "1100", "debit": "0", "credit": "9"},
        ],
    }
    rejected = client.post("/api/journal-entries", json=unbalanced)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "unbalanced"

    balance = client.get("/api/accounts/1200/balance")
    assert balance.status_code == 200
    assert Decimal(balance.json()["balance"]) == Decimal("5000000")


def test_timeout_fault_still_persists_one_journal(client: TestClient):
    payload = {
        "external_reference": "JE-TIMEOUT",
        "lines": [
            {"account_code": "1100", "debit": "100", "credit": "0"},
            {"account_code": "4000", "debit": "0", "credit": "100"},
        ],
    }
    response = client.post("/api/journal-entries", json=payload, headers={"X-Fault": "timeout_after_write"})
    assert response.status_code == 201
    again = client.post("/api/journal-entries", json=payload)
    assert again.status_code == 200
    fetched = client.get("/api/journal-entries/JE-TIMEOUT")
    assert fetched.status_code == 200
