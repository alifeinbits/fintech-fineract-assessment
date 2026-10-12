from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.correlation import correlation_id_var
from app.db import make_session_factory, session_scope
from app.errors import ApiError
from app.gateways import HttpErp, HttpFineract
from app.lifecycle import (
    create_client,
    create_loan,
    disburse,
    get_client,
    get_loan,
    get_transaction,
    recover,
    repay,
    sync_accounts,
)
from app.reconciliation import reconcile_reference
from app.schemas import ClientCreate, DisbursementCreate, LoanCreate, ReconcileRequest, RepaymentCreate
from app.settings import Settings, get_settings


def create_app(
    settings: Settings | None = None,
    fineract: HttpFineract | None = None,
    erp: HttpErp | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    factory = make_session_factory(settings.database_url)
    app = FastAPI(title="Integration Service", version="0.1.0")
    app.state.settings = settings
    app.state.session_factory = factory
    app.state.fineract = fineract if fineract is not None else HttpFineract(settings)
    app.state.erp = erp if erp is not None else HttpErp(settings)
    with factory() as session:
        recover(session, settings, app.state.fineract, app.state.erp)

    faults = {
        "fineract_down",
        "fineract_timeout_after_send",
        "erp_down",
        "erp_timeout_after_write",
        "crash_before_erp",
    }

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
        return JSONResponse(status_code=exc.status_code, content=_error(exc.code, exc.message))

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        message = "request validation failed"
        if exc.errors():
            first = exc.errors()[0]
            loc = ".".join(str(part) for part in first.get("loc", []) if part != "body")
            message = f"{loc}: {first.get('msg')}" if loc else str(first.get("msg"))
        return JSONResponse(status_code=422, content=_error("validation_error", message))

    def db_session() -> Session:
        yield from session_scope(factory)

    def require_token(authorization: str | None = Header(default=None)) -> None:
        if authorization != f"Bearer {settings.api_token}":
            raise ApiError(401, "unauthorized", "bearer token is required")

    def active_fault(x_fault: str | None) -> str | None:
        if not x_fault or not settings.faults_enabled:
            return None
        if x_fault not in faults:
            raise ApiError(400, "unknown_fault", f"unknown fault {x_fault}")
        return x_fault

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/ready", dependencies=[Depends(require_token)])
    def ready() -> dict[str, str]:
        return {"status": "ready"}

    @app.post("/api/accounts/sync", dependencies=[Depends(require_token)])
    def post_sync(session: Session = Depends(db_session)) -> JSONResponse:
        body = sync_accounts(session, app.state.erp, app.state.fineract)
        return JSONResponse(status_code=200, content={"accounts": body})

    @app.post("/api/clients", dependencies=[Depends(require_token)])
    def post_client(body: ClientCreate, session: Session = Depends(db_session)) -> JSONResponse:
        status, payload = create_client(session, app.state.fineract, body)
        return JSONResponse(status_code=status, content=payload)

    @app.get("/api/clients/{client_uid}", dependencies=[Depends(require_token)])
    def read_client(client_uid: str, session: Session = Depends(db_session)) -> JSONResponse:
        return JSONResponse(content=get_client(session, client_uid))

    @app.post("/api/loans", dependencies=[Depends(require_token)])
    def post_loan(body: LoanCreate, session: Session = Depends(db_session)) -> JSONResponse:
        status, payload = create_loan(session, app.state.fineract, body)
        return JSONResponse(status_code=status, content=payload)

    @app.get("/api/loans/{loan_reference}", dependencies=[Depends(require_token)])
    def read_loan(loan_reference: str, session: Session = Depends(db_session)) -> JSONResponse:
        return JSONResponse(content=get_loan(session, loan_reference, app.state.fineract))

    @app.post("/api/loans/{loan_reference}/disburse", dependencies=[Depends(require_token)])
    def post_disburse(
        loan_reference: str,
        body: DisbursementCreate,
        session: Session = Depends(db_session),
        x_fault: str | None = Header(default=None),
    ) -> JSONResponse:
        status, payload = disburse(
            session, settings, app.state.fineract, app.state.erp, loan_reference, body, active_fault(x_fault)
        )
        return JSONResponse(status_code=status, content=payload)

    @app.post("/api/loans/{loan_reference}/repayments", dependencies=[Depends(require_token)])
    def post_repayment(
        loan_reference: str,
        body: RepaymentCreate,
        session: Session = Depends(db_session),
        x_fault: str | None = Header(default=None),
    ) -> JSONResponse:
        status, payload = repay(
            session, settings, app.state.fineract, app.state.erp, loan_reference, body, active_fault(x_fault)
        )
        return JSONResponse(status_code=status, content=payload)

    @app.get("/api/transactions/{external_reference}", dependencies=[Depends(require_token)])
    def read_transaction(external_reference: str, session: Session = Depends(db_session)) -> JSONResponse:
        return JSONResponse(content=get_transaction(session, external_reference))

    @app.post("/api/admin/recover", dependencies=[Depends(require_token)])
    def post_recover(session: Session = Depends(db_session)) -> dict:
        return {"recovered": recover(session, settings, app.state.fineract, app.state.erp)}

    @app.post("/api/reconciliation", dependencies=[Depends(require_token)])
    def post_reconciliation(body: ReconcileRequest) -> dict:
        return {
            "results": [
                reconcile_reference(app.state.fineract, app.state.erp, item.loan_reference, item.external_reference)
                for item in body.items
            ]
        }

    return app


def _error(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message, "correlation_id": correlation_id_var.get()}}


app = create_app()
