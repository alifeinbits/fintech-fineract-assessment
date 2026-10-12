import hashlib
import json
from decimal import Decimal
from typing import Any


def _normalize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: _normalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(_normalize(payload), separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()
