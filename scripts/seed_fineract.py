#!/usr/bin/env python3
"""Seed Fineract for the assessment. Mirrors postman/fineract-seed.json.

Order: sync chart through the integration service, then office, RWF, cash product,
least-privilege user, and sample clients against Fineract.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

PARTNER = {"Authorization": "Bearer assessment-token"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fineract", default="https://localhost:8443/fineract-provider/api/v1")
    parser.add_argument("--integration", default="http://localhost:8090")
    parser.add_argument("--username", default="mifos")
    parser.add_argument("--password", default="password")
    parser.add_argument("--tenant", default="default")
    args = parser.parse_args()

    auth = (args.username, args.password)
    headers = {"Fineract-Platform-TenantId": args.tenant}

    with httpx.Client(verify=False, timeout=60.0) as client:
        print("sync chart")
        sync = client.post(f"{args.integration}/api/accounts/sync", headers=PARTNER)
        sync.raise_for_status()
        print(json.dumps(sync.json(), indent=2))

        print("rename office")
        office = client.put(
            f"{args.fineract}/offices/1",
            headers=headers,
            auth=auth,
            json={
                "name": "Kigali Main Office",
                "dateFormat": "yyyy-MM-dd",
                "locale": "en",
                "openingDate": "2009-01-01",
            },
        )
        _ok(office, allow=(200,))

        print("enable RWF")
        currency = client.put(
            f"{args.fineract}/currencies",
            headers=headers,
            auth=auth,
            json={"currencies": ["USD", "RWF"]},
        )
        _ok(currency, allow=(200,))

        gl_ids = _gl_ids(client, args.fineract, headers, auth)
        for code, name, account_type, external_id in (
            ("2100", "Loan Overpayment", 2, "GL-2100"),
            ("5000", "Loan Write Off", 5, "GL-5000"),
        ):
            if code in gl_ids:
                print(f"gl {code} already present as {gl_ids[code]}")
                continue
            print(f"create gl {code}")
            created = client.post(
                f"{args.fineract}/glaccounts",
                headers=headers,
                auth=auth,
                json={
                    "name": name,
                    "glCode": code,
                    "manualEntriesAllowed": True,
                    "type": str(account_type),
                    "usage": "1",
                    "description": name,
                    "externalId": external_id,
                    "currencyCode": "RWF",
                },
            )
            _ok(created, allow=(200,))
            gl_ids[code] = created.json()["resourceId"]

        product_id = _find_product(client, args.fineract, headers, auth, "SME Asset Finance")
        if product_id is None:
            print("create loan product SME Asset Finance")
            product = client.post(
                f"{args.fineract}/loanproducts",
                headers=headers,
                auth=auth,
                json={
                    "name": "SME Asset Finance",
                    "shortName": "SMEA",
                    "currencyCode": "RWF",
                    "digitsAfterDecimal": 0,
                    "inMultiplesOf": 1,
                    "principal": 5000000,
                    "numberOfRepayments": 12,
                    "repaymentEvery": 1,
                    "repaymentFrequencyType": 2,
                    "interestRatePerPeriod": 18,
                    "interestRateFrequencyType": 3,
                    "amortizationType": 1,
                    "interestType": 0,
                    "interestCalculationPeriodType": 1,
                    "transactionProcessingStrategyCode": "mifos-standard-strategy",
                    "accountingRule": 2,
                    "daysInYearType": 1,
                    "daysInMonthType": 1,
                    "isInterestRecalculationEnabled": False,
                    "multiDisburseLoan": False,
                    "fundSourceAccountId": gl_ids["1100"],
                    "loanPortfolioAccountId": gl_ids["1200"],
                    "transfersInSuspenseAccountId": gl_ids["1100"],
                    "interestOnLoanAccountId": gl_ids["4000"],
                    "incomeFromFeeAccountId": gl_ids["4000"],
                    "incomeFromPenaltyAccountId": gl_ids["4000"],
                    "incomeFromRecoveryAccountId": gl_ids["4000"],
                    "writeOffAccountId": gl_ids["5000"],
                    "overpaymentLiabilityAccountId": gl_ids["2100"],
                    "goodwillCreditAccountId": gl_ids["5000"],
                    "dateFormat": "yyyy-MM-dd",
                    "locale": "en",
                },
            )
            _ok(product, allow=(200,))
            product_id = product.json()["resourceId"]
        print(f"product_id={product_id}")

        role_id = _find_role(client, args.fineract, headers, auth, "Assessment API")
        if role_id is None:
            print("create role Assessment API")
            role = client.post(
                f"{args.fineract}/roles",
                headers=headers,
                auth=auth,
                json={"name": "Assessment API", "description": "Least privilege for the partner integration"},
            )
            _ok(role, allow=(200,))
            role_id = role.json()["resourceId"]
            permissions = client.put(
                f"{args.fineract}/roles/{role_id}/permissions",
                headers=headers,
                auth=auth,
                json={
                    "permissions": {
                        "READ_CLIENT": True,
                        "CREATE_CLIENT": True,
                        "READ_LOAN": True,
                        "CREATE_LOAN": True,
                        "APPROVE_LOAN": True,
                        "DISBURSE_LOAN": True,
                        "REPAYMENT_LOAN": True,
                        "READ_GLACCOUNT": True,
                        "CREATE_GLACCOUNT": True,
                    }
                },
            )
            _ok(permissions, allow=(200,))
        print(f"role_id={role_id}")

        if _find_user(client, args.fineract, headers, auth, "assessment_api") is None:
            print("create user assessment_api")
            user = client.post(
                f"{args.fineract}/users",
                headers=headers,
                auth=auth,
                json={
                    "username": "assessment_api",
                    "firstname": "Assessment",
                    "lastname": "API",
                    "email": "assessment@example.com",
                    "officeId": 1,
                    "roles": [role_id],
                    "sendPasswordToEmail": False,
                    "password": "QwE!5rTy#9uP0",
                    "repeatPassword": "QwE!5rTy#9uP0",
                },
            )
            _ok(user, allow=(200,))

        for first, last, external_id in (
            ("Amina", "Uwase", "CLT-AMINA"),
            ("Jean", "Nkurunziza", "CLT-JEAN"),
            ("Grace", "Mukamana", "CLT-GRACE"),
        ):
            if _find_client(client, args.fineract, headers, auth, external_id) is not None:
                print(f"client {external_id} already present")
                continue
            print(f"create client {external_id}")
            created = client.post(
                f"{args.fineract}/clients",
                headers=headers,
                auth=auth,
                json={
                    "officeId": 1,
                    "firstname": first,
                    "lastname": last,
                    "externalId": external_id,
                    "active": True,
                    "activationDate": "2026-10-07",
                    "legalFormId": 1,
                    "dateFormat": "yyyy-MM-dd",
                    "locale": "en",
                },
            )
            _ok(created, allow=(200,))

        print(
            json.dumps(
                {
                    "office_id": 1,
                    "office_name": "Kigali Main Office",
                    "product_id": product_id,
                    "role_id": role_id,
                    "gl_ids": gl_ids,
                    "user": "assessment_api",
                },
                indent=2,
            )
        )
    return 0


def _ok(response: httpx.Response, allow: tuple[int, ...] = (200, 201)) -> None:
    if response.status_code not in allow:
        print(response.status_code, response.text, file=sys.stderr)
        response.raise_for_status()


def _gl_ids(client: httpx.Client, base: str, headers: dict[str, str], auth: tuple[str, str]) -> dict[str, int]:
    response = client.get(f"{base}/glaccounts", headers=headers, auth=auth)
    _ok(response)
    return {str(item["glCode"]): int(item["id"]) for item in response.json()}


def _find_product(
    client: httpx.Client, base: str, headers: dict[str, str], auth: tuple[str, str], name: str
) -> int | None:
    response = client.get(f"{base}/loanproducts", headers=headers, auth=auth)
    _ok(response)
    for item in response.json():
        if item.get("name") == name:
            return int(item["id"])
    return None


def _find_role(
    client: httpx.Client, base: str, headers: dict[str, str], auth: tuple[str, str], name: str
) -> int | None:
    response = client.get(f"{base}/roles", headers=headers, auth=auth)
    _ok(response)
    for item in response.json():
        if item.get("name") == name:
            return int(item["id"])
    return None


def _find_user(
    client: httpx.Client, base: str, headers: dict[str, str], auth: tuple[str, str], username: str
) -> int | None:
    response = client.get(f"{base}/users", headers=headers, auth=auth)
    _ok(response)
    for item in response.json():
        if item.get("username") == username:
            return int(item["id"])
    return None


def _find_client(
    client: httpx.Client, base: str, headers: dict[str, str], auth: tuple[str, str], external_id: str
) -> Any:
    response = client.get(f"{base}/clients/external-id/{external_id}", headers=headers, auth=auth)
    if response.status_code == 404:
        return None
    _ok(response)
    return response.json()


if __name__ == "__main__":
    raise SystemExit(main())
