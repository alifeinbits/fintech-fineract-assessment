import json
import logging
from datetime import datetime, timezone

from app.correlation import correlation_id_var

logger = logging.getLogger("integration")


def log_event(event: str, status: str, **fields: object) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "status": status,
        "correlation_id": correlation_id_var.get(),
        "external_reference": fields.get("external_reference"),
        "client_uid": fields.get("client_uid"),
        "loan_reference": fields.get("loan_reference"),
        "fineract_transaction_id": fields.get("fineract_transaction_id"),
        "erp_transaction_id": fields.get("erp_transaction_id"),
    }
    logger.info(json.dumps(record))
