import json
import logging
import time
from contextvars import ContextVar
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db import make_session_factory, session_scope
from app.errors import ApiError
from app.schemas import AccountCreate, JournalEntryCreate
from app.service import (
    account_balance,
    create_account,
    create_journal_entry,
    get_journal_entry,
    list_accounts,
    seed_chart,
)
from app.settings import Settings, get_settings

logger = logging.getLogger("mock-erp")
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    factory = make_session_factory(settings.database_url)
    with factory() as session:
        seed_chart(session)

    app = FastAPI(title="Mock ERP", version="0.1.0")
    app.state.session_factory = factory
    app.state.settings = settings

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        correlation_id = request.headers.get("x-correlation-id") or str(uuid4())
        token = correlation_id_var.set(correlation_id)
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        response.headers["X-Correlation-ID"] = correlation_id
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "correlation_id": correlation_id_var.get(),
                }
            },
        )

    def db_session() -> Session:
        yield from session_scope(factory)

    def require_token(authorization: str | None = Header(default=None)) -> None:
        expected = f"Bearer {settings.api_token}"
        if authorization != expected:
            raise ApiError(401, "unauthorized", "bearer token is required")

    def apply_fault(x_fault: str | None) -> None:
        if not x_fault or not settings.faults_enabled:
            return
        if x_fault == "down":
            raise ApiError(503, "erp_down", "injected ERP outage")
        if x_fault == "timeout_after_write":
            return
        raise ApiError(400, "unknown_fault", f"unknown fault {x_fault}")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/accounts", dependencies=[Depends(require_token)])
    def post_account(
        body: AccountCreate,
        session: Session = Depends(db_session),
        x_fault: str | None = Header(default=None),
    ):
        apply_fault(x_fault)
        account, created = create_account(session, body)
        _log("account_upsert", external_reference=body.external_reference, status="created" if created else "replayed")
        return JSONResponse(status_code=201 if created else 200, content=account.model_dump())

    @app.get("/api/accounts", dependencies=[Depends(require_token)])
    def get_accounts(session: Session = Depends(db_session)):
        return [item.model_dump() for item in list_accounts(session)]

    @app.get("/api/accounts/{code}/balance", dependencies=[Depends(require_token)])
    def get_balance(code: str, session: Session = Depends(db_session)):
        return account_balance(session, code).model_dump(mode="json")

    @app.post("/api/journal-entries", dependencies=[Depends(require_token)])
    def post_journal(
        body: JournalEntryCreate,
        session: Session = Depends(db_session),
        x_fault: str | None = Header(default=None),
    ):
        apply_fault(x_fault)
        entry, created = create_journal_entry(session, body)
        if x_fault == "timeout_after_write" and settings.faults_enabled:
            _log("journal_timeout_after_write", external_reference=body.external_reference, status="written")
            time.sleep(settings.fault_sleep_seconds)
        _log(
            "journal_upsert",
            external_reference=body.external_reference,
            erp_transaction_id=body.external_reference,
            status="created" if created else "replayed",
        )
        return JSONResponse(status_code=201 if created else 200, content=entry.model_dump(mode="json"))

    @app.get("/api/journal-entries/{external_reference}", dependencies=[Depends(require_token)])
    def get_journal(external_reference: str, session: Session = Depends(db_session)):
        return get_journal_entry(session, external_reference).model_dump(mode="json")

    return app


def _log(event: str, **fields: object) -> None:
    logger.info(
        json.dumps(
            {
                "event": event,
                "correlation_id": correlation_id_var.get(),
                **fields,
            }
        )
    )


app = create_app()
