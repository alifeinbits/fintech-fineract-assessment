# GL external reference patch

Apache Fineract `1.15.0` (`d5636847ac556c30b437254c353f05526d172b97`) stores no external reference on a GL account. This patch adds `externalId` and `currencyCode` to the existing `POST /v1/glaccounts` create command, so `CREATE_GLACCOUNT` still applies.

## Apply

From a Fineract clone next to this assessment repo:

```bash
git clone --branch 1.15.0 --depth 1 https://github.com/apache/fineract.git
cd fineract
git apply ../fintech-fineract-assessment/fineract-customization/gl-external-reference.patch
```

In this repository the working tree is `upstream/fineract` (gitignored). From the assessment root:

```bash
git -C upstream/fineract apply fineract-customization/gl-external-reference.patch
```

Do not commit the upstream clone into this repository.

## Behavior

- A blank `externalId` is an ordinary create.
- The same `externalId` with the same `glCode`, name, account type, and currency returns the existing account id and does not insert a second row. Currency comparison is case-insensitive.
- The same `externalId` with a different identity throws `GLAccountExternalReferenceConflictException`. Fineract maps domain-rule exceptions to HTTP 403. That is the equivalent of 409 used here.
- Liquibase changeset `0241` adds `acc_gl_account.external_id`, `currency_code`, and unique constraint `acc_gl_external_id`. MySQL allows multiple NULLs, so accounts created without an external id are unaffected.
- A concurrent duplicate that loses the unique-index race raises the same conflict. The transaction is already rollback-only, so the losing request does not return the existing row. A retry hits the pre-check and returns it.

## Tests

Unit tests (5 passed):

```bash
./gradlew :fineract-accounting:test \
  --tests org.apache.fineract.accounting.glaccount.service.GLAccountExternalReferenceSyncTest \
  --tests org.apache.fineract.accounting.glaccount.service.GLAccountWritePlatformServiceJpaRepositoryImplTest
```

`GLAccountExternalReferenceIntegrationTest` posts the same body twice and expects one resource id, then expects HTTP 403 for a changed name. It has not been run inside Gradle; it needs the integration-test harness.

Checked on the custom image against MySQL: the first `POST /v1/glaccounts` for `GL-1200` returned `resourceId` 1, the identical replay returned the same id, and a changed name returned HTTP 403 with `error.msg.glaccount.externalid.conflict`. `currencyCode` `rwf` was stored as `RWF`.
