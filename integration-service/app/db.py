from collections.abc import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.models import Base


def make_session_factory(database_url: str) -> sessionmaker[Session]:
    connect_args = {"check_same_thread": False, "timeout": 30} if database_url.startswith("sqlite") else {}
    engine_kwargs: dict = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        engine_kwargs["poolclass"] = NullPool
    engine = create_engine(database_url, connect_args=connect_args, **engine_kwargs)
    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    Base.metadata.create_all(engine)
    _add_loan_reference_column(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _add_loan_reference_column(engine) -> None:  # type: ignore[no-untyped-def]
    inspector = inspect(engine)
    if "idempotency_keys" not in inspector.get_table_names():
        return
    names = {column["name"] for column in inspector.get_columns("idempotency_keys")}
    if "loan_reference" in names:
        return
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE idempotency_keys ADD COLUMN loan_reference VARCHAR(100)"))


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
    finally:
        session.close()
