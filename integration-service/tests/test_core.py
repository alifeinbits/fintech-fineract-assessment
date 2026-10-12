from decimal import Decimal

import pytest
from app.account_mapping import map_account_type
from app.accounting import disbursement_entry, repayment_entry
from app.db import make_session_factory
from app.idempotency import IdempotencyConflict, claim, mark
from app.state_machine import transition


def test_account_type_mapping():
    assert map_account_type("ASSET") == 1
    assert map_account_type("INCOME") == 4
    with pytest.raises(ValueError):
        map_account_type("CASH")


def test_disbursement_and_repayment_balance():
    disbursement = disbursement_entry(Decimal("5000000"), "1100", "1200")
    assert sum(line.debit for line in disbursement) == sum(line.credit for line in disbursement)
    assert disbursement[0].account_code == "1200"

    repayment = repayment_entry(Decimal("400000"), Decimal("75000"), "1100", "1200", "4000")
    assert sum(line.debit for line in repayment) == sum(line.credit for line in repayment)
    assert repayment[0].debit == Decimal("475000")
    assert repayment[2].account_code == "4000"


def test_state_machine_rejects_skip():
    assert transition("RECEIVED", "FINERACT_PENDING") == "FINERACT_PENDING"
    with pytest.raises(ValueError):
        transition("RECEIVED", "COMPLETED")


def test_idempotency_replay_conflict_and_in_progress(tmp_path):
    factory = make_session_factory(f"sqlite:///{tmp_path}/integration.db")
    payload = {"amount": "10"}

    with factory() as session:
        first = claim(session, "DISBURSEMENT", "REF-1", payload)
        assert first.kind == "new"
        assert first.row is not None
        mark(session, first.row, "FINERACT_PENDING")

    with factory() as session:
        again = claim(session, "DISBURSEMENT", "REF-1", payload)
        assert again.kind == "in_progress"
        assert again.status_code == 202

    with factory() as session:
        row = claim(session, "DISBURSEMENT", "REF-1", payload).row
        assert row is not None
        mark(session, row, "FINERACT_DONE")
        mark(session, row, "ERP_PENDING")
        mark(session, row, "COMPLETED", http_status=200, body={"loan_reference": "LN-1"})

    with factory() as session:
        replay = claim(session, "DISBURSEMENT", "REF-1", payload)
        assert replay.kind == "replay"
        assert replay.status_code == 200
        assert replay.body["loan_reference"] == "LN-1"
        with pytest.raises(IdempotencyConflict):
            claim(session, "DISBURSEMENT", "REF-1", {"amount": "11"})

    with factory() as session:
        failed = claim(session, "REPAYMENT", "REF-9", payload)
        assert failed.row is not None
        mark(session, failed.row, "FAILED", http_status=503, body={"error": "down"})

    with factory() as session:
        retry = claim(session, "REPAYMENT", "REF-9", payload)
        assert retry.kind == "retry"
