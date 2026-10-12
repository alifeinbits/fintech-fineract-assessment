"""Read-only comparison of a Fineract money transaction and the ERP journal that shares its external reference.

A reversal is a new journal with the opposite lines and external reference ``{original}:reversal``.
This comparison does not post that journal and does not change either system.
"""

from app.gateways import HttpErp, HttpFineract


def reconcile_reference(
    fineract: HttpFineract, erp: HttpErp, loan_reference: str, external_reference: str
) -> dict:
    posted = fineract.find_transaction(loan_reference, external_reference)
    erp_amount = erp.journal_total(external_reference)
    erp_kind = erp.journal_kind(external_reference)
    fineract_amount = None if posted is None else posted.amount
    fineract_kind = None if posted is None else posted.kind
    if posted is None:
        outcome = "MISSING_IN_FINERACT"
    elif erp_amount is None:
        outcome = "MISSING_IN_ERP"
    elif erp_amount != fineract_amount:
        outcome = "AMOUNT_MISMATCH"
    elif fineract_kind not in {None, "UNKNOWN"} and erp_kind not in {None, "UNKNOWN"} and fineract_kind != erp_kind:
        outcome = "AMOUNT_MISMATCH"
    else:
        outcome = "MATCHED"
    return {
        "loan_reference": loan_reference,
        "external_reference": external_reference,
        "outcome": outcome,
        "fineract_amount": None if fineract_amount is None else str(fineract_amount),
        "erp_amount": None if erp_amount is None else str(erp_amount),
        "fineract_type": fineract_kind,
        "erp_type": erp_kind,
    }
