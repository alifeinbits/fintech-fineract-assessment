from datetime import date
from decimal import Decimal
from threading import Barrier, Thread

from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings
from tests.fakes import FakeErp, FakeFineract

HEADERS = {"Authorization": "Bearer assessment-token"}
DISBURSE = {"external_reference": "DISB-1", "amount": "5000000", "transaction_date": "2026-10-07"}


def _stack(tmp_path, **settings):
    fineract = FakeFineract()
    erp = FakeErp()
    app = create_app(
        Settings(database_url=f"sqlite:///{tmp_path}/integration.db", retry_backoff_seconds=0, **settings),
        fineract=fineract,
        erp=erp,
    )
    with TestClient(app) as client:
        _create_client(client)
        _create_loan(client)
    return app, fineract, erp


def test_fineract_down_then_retry_posts_once(tmp_path):
    app, fineract, _erp = _stack(tmp_path)
    with TestClient(app) as client:
        down = client.post("/api/loans/LN-1/disburse", headers={**HEADERS, "X-Fault": "fineract_down"}, json=DISBURSE)
        assert down.status_code == 503
        assert down.json()["error"]["code"] == "fineract_down"
        assert fineract.disburse_posts == 0
        loan = client.get("/api/loans/LN-1", headers=HEADERS).json()
        assert loan["disbursement_reference"] is None
        other = client.post(
            "/api/loans/LN-1/disburse",
            headers=HEADERS,
            json={"external_reference": "DISB-OTHER", "amount": "5000000", "transaction_date": "2026-10-07"},
        )
        assert other.status_code == 201
    assert fineract.disburse_posts == 1


def test_transient_fineract_down_retries_with_backoff_inside_the_request(tmp_path):
    app, fineract, _erp = _stack(tmp_path)
    fineract.fail_disburse_remaining = 1
    with TestClient(app) as client:
        response = client.post("/api/loans/LN-1/disburse", headers=HEADERS, json=DISBURSE)
    assert response.status_code == 201
    assert fineract.disburse_posts == 1


def test_fineract_timeout_after_send_recovers_by_lookup(tmp_path):
    app, fineract, erp = _stack(tmp_path)
    with TestClient(app) as client:
        timed_out = client.post(
            "/api/loans/LN-1/disburse",
            headers={**HEADERS, "X-Fault": "fineract_timeout_after_send"},
            json=DISBURSE,
        )
        assert timed_out.status_code == 504
        assert fineract.disburse_posts == 1
        assert "DISB-1" not in erp.journals
        again = client.post("/api/loans/LN-1/disburse", headers=HEADERS, json=DISBURSE)
        assert again.status_code == 202
        recovered = client.post("/api/admin/recover", headers=HEADERS)
        assert recovered.status_code == 200
        assert recovered.json()["recovered"][0]["status"] == "COMPLETED"
    assert fineract.disburse_posts == 1
    assert list(erp.journals) == ["DISB-1"]


def test_erp_timeout_after_write_recovers_without_a_second_journal(tmp_path):
    app, fineract, erp = _stack(tmp_path)
    with TestClient(app) as client:
        timed_out = client.post(
            "/api/loans/LN-1/disburse",
            headers={**HEADERS, "X-Fault": "erp_timeout_after_write"},
            json=DISBURSE,
        )
        assert timed_out.status_code == 504
        assert list(erp.journals) == ["DISB-1"]
        client.post("/api/admin/recover", headers=HEADERS)
    assert fineract.disburse_posts == 1
    assert list(erp.journals) == ["DISB-1"]


def test_crash_before_erp_is_resumed_on_startup(tmp_path):
    app, fineract, erp = _stack(tmp_path)
    with TestClient(app) as client:
        crashed = client.post(
            "/api/loans/LN-1/disburse",
            headers={**HEADERS, "X-Fault": "crash_before_erp"},
            json=DISBURSE,
        )
        assert crashed.status_code == 500
    assert "DISB-1" not in erp.journals
    create_app(
        Settings(database_url=f"sqlite:///{tmp_path}/integration.db", retry_backoff_seconds=0),
        fineract=fineract,
        erp=erp,
    )
    assert fineract.disburse_posts == 1
    assert list(erp.journals) == ["DISB-1"]


def test_disabled_faults_are_ignored(tmp_path):
    app, fineract, _erp = _stack(tmp_path, faults_enabled=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/loans/LN-1/disburse",
            headers={**HEADERS, "X-Fault": "fineract_down"},
            json=DISBURSE,
        )
        unknown = client.post(
            "/api/loans/LN-1/repayments",
            headers={**HEADERS, "X-Fault": "not-a-fault"},
            json={"external_reference": "REP-1", "amount": "1", "transaction_date": "2026-11-07"},
        )
    assert response.status_code == 201
    assert fineract.disburse_posts == 1
    assert unknown.status_code != 400


def test_reconciliation_reports_four_outcomes_and_writes_nothing(tmp_path):
    app, fineract, erp = _stack(tmp_path)
    with TestClient(app) as client:
        assert client.post("/api/loans/LN-1/disburse", headers=HEADERS, json=DISBURSE).status_code == 201
    fineract.disburse("LN-1", "DISB-MISSING-ERP", Decimal("5000000"), date(2026, 10, 8))
    erp.create_journal("REP-MISSING-FIN", [("1100", Decimal("10"), Decimal("0")), ("1200", Decimal("0"), Decimal("10"))])
    erp.journals["DISB-1"] = [("1100", Decimal("1"), Decimal("0")), ("1200", Decimal("0"), Decimal("1"))]
    posts = fineract.disburse_posts
    journal_keys = set(erp.journals)
    with TestClient(app) as client:
        response = client.post(
            "/api/reconciliation",
            headers=HEADERS,
            json={
                "items": [
                    {"loan_reference": "LN-1", "external_reference": "DISB-MISSING-ERP"},
                    {"loan_reference": "LN-1", "external_reference": "REP-MISSING-FIN"},
                    {"loan_reference": "LN-1", "external_reference": "DISB-1"},
                ]
            },
        )
    assert response.status_code == 200
    outcomes = {row["external_reference"]: row["outcome"] for row in response.json()["results"]}
    assert outcomes == {
        "DISB-MISSING-ERP": "MISSING_IN_ERP",
        "REP-MISSING-FIN": "MISSING_IN_FINERACT",
        "DISB-1": "AMOUNT_MISMATCH",
    }
    assert set(erp.journals) == journal_keys
    assert fineract.disburse_posts == posts
    erp.journals["DISB-MATCH"] = [("1100", Decimal("5"), Decimal("0")), ("1200", Decimal("0"), Decimal("5"))]
    fineract.transactions[("LN-1", "DISB-MATCH")] = type(fineract.transactions[("LN-1", "DISB-1")])(
        Decimal("5"), Decimal("5"), Decimal("0")
    )
    with TestClient(app) as client:
        matched = client.post(
            "/api/reconciliation",
            headers=HEADERS,
            json={"items": [{"loan_reference": "LN-1", "external_reference": "DISB-MATCH"}]},
        )
    assert matched.json()["results"][0]["outcome"] == "MATCHED"


def test_ten_parallel_disbursements_of_one_reference_post_once(tmp_path):
    app, fineract, erp = _stack(tmp_path)
    statuses = _parallel(app, "/api/loans/LN-1/disburse", [DISBURSE] * 10)
    assert fineract.disburse_posts == 1
    assert list(erp.journals) == ["DISB-1"]
    assert set(statuses) <= {200, 201, 202}
    assert 201 in statuses or 200 in statuses


def test_ten_parallel_disbursements_with_different_references_post_once(tmp_path):
    app, fineract, erp = _stack(tmp_path)
    bodies = [
        {"external_reference": f"DISB-{index}", "amount": "5000000", "transaction_date": "2026-10-07"}
        for index in range(10)
    ]
    statuses = _parallel(app, "/api/loans/LN-1/disburse", bodies)
    assert fineract.disburse_posts == 1
    assert len(erp.journals) == 1
    assert statuses.count(201) == 1
    assert statuses.count(409) == 9


def test_parallel_repayments_match_fineract_outstanding(tmp_path):
    app, fineract, _erp = _stack(tmp_path)
    with TestClient(app) as client:
        assert client.post("/api/loans/LN-1/disburse", headers=HEADERS, json=DISBURSE).status_code == 201
    bodies = [
        {"external_reference": f"REP-{index}", "amount": "500000", "transaction_date": "2026-11-07"}
        for index in range(10)
    ]
    statuses = _parallel(app, "/api/loans/LN-1/repayments", bodies)
    assert set(statuses) <= {200, 201}
    assert fineract.repay_posts == 10
    with TestClient(app) as client:
        loan = client.get("/api/loans/LN-1", headers=HEADERS).json()
    assert Decimal(loan["outstanding"]) == fineract.loans["LN-1"].outstanding
    assert Decimal(loan["outstanding"]) == Decimal("0")
    assert loan["fineract_status"] == "Closed (obligations met)"


def _parallel(app, path: str, bodies: list[dict]) -> list[int]:
    barrier = Barrier(len(bodies))
    statuses: list[int] = []

    def worker(body: dict) -> None:
        barrier.wait()
        with TestClient(app) as client:
            response = client.post(path, headers=HEADERS, json=body)
        statuses.append(response.status_code)

    threads = [Thread(target=worker, args=(body,)) for body in bodies]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return statuses


def _create_client(client: TestClient) -> None:
    response = client.post(
        "/api/clients",
        headers=HEADERS,
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
        headers=HEADERS,
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
