# Fintech Fineract assessment

Interview take-home for connecting Apache Fineract to a mock ERP. The brief is in [docs/reference/Fintech_Engineer_Take_Home_Assessment.pdf](docs/reference/Fintech_Engineer_Take_Home_Assessment.pdf).

Tiers completed: core and reliability are implemented and covered by automated tests. The Java GL patch was checked on the custom image (create, replay, conflict). Fineract was seeded (office, RWF, cash product, least-privilege user, sample clients). A live 5,000,000 RWF loan was taken to `Closed (obligations met)` on that image; see [evidence/live-loan.txt](evidence/live-loan.txt). Stretch (Part L) is not started.

The scripted simulations in [SIMULATIONS.md](SIMULATIONS.md) run in-process against stand-ins. They do not start this stack. [ARCHITECTURE.md](ARCHITECTURE.md), [API.md](API.md), and [DECISIONS.md](DECISIONS.md) describe the design and the choices behind it.

## Pinned versions

| Piece | Version |
| --- | --- |
| Apache Fineract | tag `1.15.0`, commit `d5636847ac556c30b437254c353f05526d172b97` (checkout under `upstream/fineract`, not committed) |
| JDK | 21 (local Temurin 21.0.12.1; Fineract 1.15 toolchain is Java 21) |
| Gradle | wrapper shipped in the `1.15.0` tag |
| Python services | 3.12 in Docker images |
| MySQL | 8.4 |
| API framework | FastAPI 0.115.6 |

Fineract's own sample compose defaults to PostgreSQL. This submission uses MySQL because Fineract 1.15.0 ships `config/docker/env/fineract-mysql.env` and the assessment allows MySQL. One MySQL 8.4 server holds `fineract_tenants`, `fineract_default`, `mock_erp`, and `integration`.

## Start

```bash
docker compose up --build
```

That starts MySQL (host port 3307), the mock ERP on port 8001, the integration service on port 8090, Fineract on port 8443, and the Apache Fineract backoffice UI on port 8080. Host ports avoid 3306 and 8000, which are already in use on this machine.

Backoffice UI: `http://localhost:8080`, tenant `default`, login `mifos` / `password`. The page is built from [apache/fineract-backoffice-ui](https://github.com/apache/fineract-backoffice-ui) commit `2674193aa04359159db6b52472c1f74b0e4c46fa`. Its nginx proxies `/api/` to the custom Fineract image. That repository's own Postgres compose is not used.

Seed Fineract after the stack is healthy: sync the chart through the integration service, then office, RWF, the cash loan product, a least-privilege user, and sample clients. The same steps are in [postman/fineract-seed.json](postman/fineract-seed.json) and:

```bash
.venv/bin/python scripts/seed_fineract.py
```

Compose keeps `FINERACT_USERNAME=mifos` so a cold stack can talk to Fineract before seed. After seed, point the integration service at the least-privilege user (`assessment_api` / `QwE!5rTy#9uP0`) via env and recreate that container. See [DECISIONS.md](DECISIONS.md).

Build the custom Fineract image before the first `docker compose up`. Clone tag `1.15.0` into `upstream/fineract` (gitignored), apply the patch, then build:

```bash
git -C upstream/fineract apply fineract-customization/gl-external-reference.patch
cd upstream/fineract && ./gradlew :fineract-provider:jibDockerBuild
```

Jib tags the local image `fineract:1.15.0`. Compose does not build Fineract itself. The 1.15.0 image ships the PostgreSQL driver only. MySQL Connector/J is mounted at `/app/plugins`, which is on Fineract's extra classpath. The jar in `docker/fineract/plugins` is the one `./gradlew :fineract-provider:copyDatabaseDriverPlugins` resolves (`mysql-connector-j` 9.7.0).

Mock ERP token: `Authorization: Bearer assessment-token`.

## Tests

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r mock-erp/requirements-dev.txt -r integration-service/requirements-dev.txt
cd mock-erp && python -m pytest && cd ../integration-service && python -m pytest
```

`make test-python` runs the same pytest commands plus `mypy` on both Python apps (`make test-mypy` alone). `make simulate` reprints the nine-field evidence in [evidence/simulations.txt](evidence/simulations.txt). Pytest and simulate use stand-ins. They do not start Docker.

Java unit tests for the GL rule, from `upstream/fineract`:

```bash
./gradlew :fineract-accounting:test --tests org.apache.fineract.accounting.glaccount.service.GLAccountExternalReferenceSyncTest --tests org.apache.fineract.accounting.glaccount.service.GLAccountWritePlatformServiceJpaRepositoryImplTest
```

The last recorded result is in [fineract-customization/README.md](fineract-customization/README.md): five tests passed, and one live create/replay/conflict against the custom image.

## Layout

- `mock-erp/` — chart of accounts and balanced journals
- `integration-service/` — partner API for account sync, clients, loans, disbursement, and repayment. Faults, startup recovery, and read-only reconciliation live here. Fineract numeric ids stay in the service database.
- `fineract-customization/` — patch against tag 1.15.0
- `scripts/simulate.py` — re-runnable simulations
- `upstream/fineract/` — local clone of tag 1.15.0, gitignored
- `docs/reference/` — the assessment PDF
