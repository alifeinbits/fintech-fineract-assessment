import time

from app.errors import GatewayError
from app.settings import Settings

RETRYABLE = {"fineract_down"}


def call_with_backoff(settings: Settings, func):  # type: ignore[no-untyped-def]
    """Retry a Fineract call that already looks up by external id before it posts.

    ERP failures are not retried here. A blind retry could write a second journal.
    """
    delay = settings.retry_backoff_seconds
    for attempt in range(settings.retry_attempts):
        try:
            return func()
        except GatewayError as exc:
            if exc.code not in RETRYABLE or attempt == settings.retry_attempts - 1:
                raise
            if delay:
                time.sleep(delay)
            delay *= 2
    raise GatewayError("fineract_down", "Fineract request failed", 503)
