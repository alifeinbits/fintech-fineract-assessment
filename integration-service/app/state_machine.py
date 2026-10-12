TERMINAL = {"COMPLETED", "FAILED", "NEEDS_RECONCILIATION"}

ALLOWED: dict[str, set[str]] = {
    "RECEIVED": {"FINERACT_PENDING", "FAILED"},
    "FINERACT_PENDING": {"FINERACT_DONE", "FAILED", "NEEDS_RECONCILIATION"},
    "FINERACT_DONE": {"ERP_PENDING"},
    "ERP_PENDING": {"COMPLETED", "FAILED", "NEEDS_RECONCILIATION"},
    "FAILED": {"FINERACT_PENDING", "ERP_PENDING", "NEEDS_RECONCILIATION"},
    "NEEDS_RECONCILIATION": {"ERP_PENDING", "COMPLETED"},
    "COMPLETED": set(),
}


def transition(current: str, target: str) -> str:
    if target not in ALLOWED.get(current, set()):
        raise ValueError(f"illegal transition {current} -> {target}")
    return target
