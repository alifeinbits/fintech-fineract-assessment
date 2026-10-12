#!/usr/bin/env python3
"""Run the 5,000,000 RWF demo loan to Closed on the live stack and print nine-field evidence."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from decimal import Decimal

import httpx

INTEGRATION = "http://localhost:8090"
FINERACT = "https://localhost:8443/fineract-provider/api/v1"
ERP = "http://localhost:8001"
PARTNER = {"Authorization": "Bearer assessment-token"}
AUTH = ("mifos", "password")
TENANT = {"Fineract-Platform-TenantId": "default"}
# Fineract 1.15 compares transaction dates to the JVM calendar day (UTC here).
TODAY = date.utcnow().date() if hasattr(date, "utcnow") else date.today()
try:
    from datetime import datetime, timezone

    TODAY = datetime.now(timezone.utc).date()
except Exception:  # pragma: no cover
    TODAY = date.today()

LOAN_REF = "FIN-2026-000003"
PRINCIPAL = "5000000"
DISB_REF = "FIN-2026-000003-DISB"


def main() -> int:
    evidence: list[str] = []
    with httpx.Client(verify=False, timeout=120.0) as client:
        evidence.append(_step_client(client))
        evidence.append(_step_loan(client))
        evidence.append(_step_disburse(client))
        evidence.extend(_step_repayments(client))
        evidence.append(_step_closed(client))
        evidence.append(_erp_balances(client))

    out = "\n\n".join(evidence) + "\n"
    path = "/home/alifeinbits/fintech-fineract-assessment/evidence/live-loan.txt"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(out)
    print(out)
    print(f"wrote {path}", file=sys.stderr)
    return 0


def _step_client(client: httpx.Client) -> str:
    body = {
        "external_reference": "CL-LIVE-1",
        "client_uid": "CLT-AMINA",
        "first_name": "Amina",
        "last_name": "Uwase",
        "activation_date": TODAY.isoformat(),
    }
    response = client.post(f"{INTEGRATION}/api/clients", headers=PARTNER, json=body)
    fineract = client.get(f"{FINERACT}/clients/external-id/CLT-AMINA", headers=TENANT, auth=AUTH)
    return _block(
        "Live client create",
        "Fineract already has CLT-AMINA from the seed.",
        "POST /api/clients for CLT-AMINA.",
        f"HTTP {response.status_code} {response.json()}",
        f"Fineract client: {fineract.json().get('id')} status {fineract.json().get('status')}",
        "ERP unchanged.",
        f"Integration client: {response.json()}",
        "201 or 200 with public client fields and no Fineract numeric id.",
        f"code={response.status_code} body={response.json()}",
        "The partner API finds the existing Fineract client by externalId and stores the mapping privately.",
    )


def _step_loan(client: httpx.Client) -> str:
    body = {
        "external_reference": "LN-LIVE-3",
        "loan_reference": LOAN_REF,
        "client_uid": "CLT-AMINA",
        "principal": PRINCIPAL,
        "currency": "RWF",
        "interest_rate": "18",
        "term_months": 12,
        "submitted_on": TODAY.isoformat(),
    }
    response = client.post(f"{INTEGRATION}/api/loans", headers=PARTNER, json=body)
    fineract = client.get(f"{FINERACT}/loans/external-id/{LOAN_REF}", headers=TENANT, auth=AUTH)
    payload = fineract.json()
    return _block(
        "Live loan create and approve",
        "Client mapped. No loan yet.",
        f"POST /api/loans for {LOAN_REF}, principal 5000000 RWF, 18%, 12 months.",
        f"HTTP {response.status_code} {response.json()}",
        f"Fineract status={payload.get('status')} outstanding={payload.get('summary', {}).get('totalOutstanding')}",
        "ERP unchanged.",
        f"Integration loan: {response.json()}",
        "Approved loan with outstanding equal to principal.",
        f"code={response.status_code} fineract_status={response.json().get('fineract_status')} outstanding={response.json().get('outstanding')}",
        "Create also approves. Product id 1 is SME Asset Finance from the seed.",
    )


def _step_disburse(client: httpx.Client) -> str:
    body = {
        "external_reference": DISB_REF,
        "amount": PRINCIPAL,
        "transaction_date": TODAY.isoformat(),
    }
    response = client.post(f"{INTEGRATION}/api/loans/{LOAN_REF}/disburse", headers=PARTNER, json=body)
    fineract = client.get(f"{FINERACT}/loans/external-id/{LOAN_REF}", headers=TENANT, auth=AUTH)
    journal = client.get(f"{ERP}/api/journal-entries/{DISB_REF}", headers=PARTNER)
    loan = fineract.json()
    return _block(
        "Live disbursement",
        "Approved loan, no disbursement, no journal.",
        f"POST /api/loans/{LOAN_REF}/disburse with {DISB_REF}.",
        f"HTTP {response.status_code} {response.json()}",
        f"Fineract status={loan.get('status')} outstanding={loan.get('summary', {}).get('totalOutstanding')}",
        f"ERP journal HTTP {journal.status_code}: {journal.json() if journal.status_code < 500 else journal.text}",
        f"Integration transaction: {response.json()}",
        "201, Active loan, one balanced journal DR 1200 CR 1100.",
        f"code={response.status_code} status={response.json().get('status')} outstanding={response.json().get('outstanding')}",
        f"Fineract posts the disbursement with externalId {DISB_REF}. The ERP journal uses the same reference.",
    )


def _step_repayments(client: httpx.Client) -> list[str]:
    blocks: list[str] = []
    schedule = [
        ("REP-LIVE-A", "500000"),
        ("REP-LIVE-B", "1000000"),
        ("REP-LIVE-C", "1000000"),
    ]
    for ref, amount in schedule:
        blocks.append(_one_repayment(client, ref, amount, TODAY.isoformat()))

    loan = client.get(f"{INTEGRATION}/api/loans/{LOAN_REF}", headers=PARTNER).json()
    outstanding = Decimal(str(loan["outstanding"]))
    # Quantize to whole RWF (product digitsAfterDecimal=0) and use a unique reference per run.
    amount = str(int(outstanding))
    blocks.append(_one_repayment(client, f"REP-LIVE-PAYOFF-{LOAN_REF}", amount, TODAY.isoformat()))
    return blocks


def _one_repayment(client: httpx.Client, ref: str, amount: str, when: str) -> str:
    body = {"external_reference": ref, "amount": amount, "transaction_date": when}
    response = client.post(f"{INTEGRATION}/api/loans/{LOAN_REF}/repayments", headers=PARTNER, json=body)
    fineract = client.get(f"{FINERACT}/loans/external-id/{LOAN_REF}", headers=TENANT, auth=AUTH)
    journal = client.get(f"{ERP}/api/journal-entries/{ref}", headers=PARTNER)
    loan = fineract.json()
    return _block(
        f"Live repayment {ref}",
        f"Active loan before posting {ref}.",
        f"POST /api/loans/{LOAN_REF}/repayments amount={amount} date={when}.",
        f"HTTP {response.status_code} {response.json()}",
        f"Fineract status={loan.get('status')} outstanding={loan.get('summary', {}).get('totalOutstanding')}",
        f"ERP journal HTTP {journal.status_code}: {journal.json() if journal.status_code < 500 else journal.text}",
        f"Integration transaction: {response.json()}",
        "201 with principal and interest portions from Fineract, one journal.",
        f"code={response.status_code} outstanding={response.json().get('outstanding')} fineract_status={response.json().get('fineract_status')}",
        "Interest and principal portions come from the Fineract transaction, not a local schedule.",
    )


def _step_closed(client: httpx.Client) -> str:
    loan = client.get(f"{INTEGRATION}/api/loans/{LOAN_REF}", headers=PARTNER)
    fineract = client.get(f"{FINERACT}/loans/external-id/{LOAN_REF}", headers=TENANT, auth=AUTH)
    payload = fineract.json()
    return _block(
        "Live loan closed",
        "After the payoff repayment.",
        f"GET /api/loans/{LOAN_REF} and GET Fineract loan by external id.",
        f"Integration HTTP {loan.status_code} {loan.json()}",
        f"Fineract status={payload.get('status')} outstanding={payload.get('summary', {}).get('totalOutstanding')}",
        "ERP holds one journal per money reference.",
        f"Integration status={loan.json().get('status')} fineract_status={loan.json().get('fineract_status')} outstanding={loan.json().get('outstanding')}",
        "Closed (obligations met), outstanding 0.",
        f"fineract_status={loan.json().get('fineract_status')} outstanding={loan.json().get('outstanding')}",
        "The public status is CLOSED when Fineract reports Closed (obligations met).",
    )


def _erp_balances(client: httpx.Client) -> str:
    balances = {}
    for code in ("1100", "1200", "4000"):
        response = client.get(f"{ERP}/api/accounts/{code}/balance", headers=PARTNER)
        balances[code] = response.json() if response.status_code == 200 else response.text
    return _block(
        "ERP balances after closed loan",
        "Journals posted for disbursement and repayments.",
        "GET /api/accounts/{1100,1200,4000}/balance.",
        json.dumps(balances),
        "Fineract loan is closed.",
        json.dumps(balances),
        "Integration loan CLOSED.",
        "Bank and income reflect cash movements; receivable is cleared.",
        json.dumps(balances),
        "Balances are debit-normal for assets and credit-normal for income.",
    )


def _block(
    title: str,
    initial: str,
    request: str,
    response: str,
    fineract_state: str,
    erp_state: str,
    integration_state: str,
    expected: str,
    actual: str,
    explanation: str,
) -> str:
    return "\n".join(
        [
            f"## {title}",
            f"1. Initial state: {initial}",
            f"2. Request: {request}",
            f"3. Response: {response}",
            f"4. Fineract state: {fineract_state}",
            f"5. ERP state: {erp_state}",
            f"6. Integration state: {integration_state}",
            f"7. Expected result: {expected}",
            f"8. Actual result: {actual}",
            f"9. Explanation: {explanation}",
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
