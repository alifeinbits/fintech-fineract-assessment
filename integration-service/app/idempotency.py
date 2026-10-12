import json
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.hashing import canonical_hash
from app.models import IdempotencyKey
from app.state_machine import transition

ClaimKind = Literal["new", "replay", "retry", "in_progress", "conflict"]


class IdempotencyConflict(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass
class Claim:
    kind: ClaimKind
    status_code: int
    body: dict[str, Any]
    row: IdempotencyKey | None = None


def claim(session: Session, operation_type: str, external_reference: str, payload: dict[str, Any]) -> Claim:
    digest = canonical_hash(payload)
    existing = _find(session, operation_type, external_reference)
    if existing is not None:
        return _from_existing(existing, digest)

    row = IdempotencyKey(
        operation_type=operation_type,
        external_reference=external_reference,
        payload_hash=digest,
        status="RECEIVED",
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raced = _find(session, operation_type, external_reference)
        if raced is None:
            raise
        return _from_existing(raced, digest)
    session.refresh(row)
    return Claim(kind="new", status_code=0, body={}, row=row)


def mark(session: Session, row: IdempotencyKey, target: str, http_status: int | None = None, body: dict | None = None) -> None:
    row.status = transition(row.status, target)
    if http_status is not None:
        row.http_status = http_status
    if body is not None:
        row.response_body = json.dumps(body)
    session.commit()


def _find(session: Session, operation_type: str, external_reference: str) -> IdempotencyKey | None:
    return session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.operation_type == operation_type,
            IdempotencyKey.external_reference == external_reference,
        )
    )


def _from_existing(row: IdempotencyKey, digest: str) -> Claim:
    if row.payload_hash != digest:
        raise IdempotencyConflict("external_reference already used with a different payload")
    if row.status == "COMPLETED":
        stored = json.loads(row.response_body or "{}")
        return Claim(kind="replay", status_code=row.http_status or 200, body=stored, row=row)
    if row.status in {"FAILED", "NEEDS_RECONCILIATION"}:
        return Claim(kind="retry", status_code=0, body={}, row=row)
    return Claim(
        kind="in_progress",
        status_code=202,
        body={"status": row.status, "external_reference": row.external_reference},
        row=row,
    )
