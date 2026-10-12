FINERACT_GL_TYPES: dict[str, int] = {
    "ASSET": 1,
    "LIABILITY": 2,
    "EQUITY": 3,
    "INCOME": 4,
    "EXPENSE": 5,
}


def map_account_type(erp_type: str) -> int:
    try:
        return FINERACT_GL_TYPES[erp_type]
    except KeyError as exc:
        raise ValueError(f"unsupported ERP account type {erp_type}") from exc
