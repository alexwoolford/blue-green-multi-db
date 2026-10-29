# Blue/green full refresh on Aura with multiple databases

A reference pattern, with a test harness that proves it, for rebuilding each tenant's graph on a schedule and swapping it in without the application noticing.

## The pattern

One Aura instance holds a database per tenant. Applications connect to a stable alias, such as `acme`, never to a physical database. Each refresh builds a new database next to the live one (`acme-r0042` beside `acme-r0041`), checks it, moves the alias with `ALTER ALIAS`, and deletes the old database. The application's configuration never changes, and there is no idle second environment to pay for.

Read the full write-up in [REFERENCE-ARCHITECTURE.md](REFERENCE-ARCHITECTURE.md). Each recommendation in it is labelled either **Verified** (ran in the live test) or **Design guidance** (from public documentation, not tested here).

## When to use it

Use it when:

- Tenant data is rebuilt in full from a source system on a schedule, and bulk loading with `neo4j-admin import` is acceptable.
- There are many tenants of similar size and load. Sharing an instance saves money when small tenants would otherwise each need their own Business Critical instance, which starts at 2GB. (AuraDB Professional does not support multiple databases.)
- Tenants can share one maintenance window, failure domain, and security boundary.

Do not use it when:

- Applications write data continuously and those writes must survive a refresh.
- There are one or two large or much busier tenants. They gain nothing from sharing, since one 8GB instance costs the same as two 4GB instances, and they become noisy neighbors.
- A tenant needs its own uptime, pause, upgrade schedule, keys, or network boundary. Give it its own instance.

Pausing the instance is not part of the pattern. Blue/green keeps each tenant's live database online, and a pause stops the whole instance, taking every tenant offline. Pausing does lower the bill, so it only suits tenants that do not need the data between refreshes, and those tenants do not need blue/green.

## Evidence

The pattern ran end to end on Aura Business Critical three times: twice on 2026-10-28, and once on 2026-10-29 with the current harness. During each alias switch, one client reading through the alias (about 25 reads per tenant over about 11 seconds) saw 0 failed reads. A sanitized copy of the results is in [docs/evidence/2026-10-28-aura-live-run.md](docs/evidence/2026-10-28-aura-live-run.md).

Those were small runs: three tenants of about 10MB each on a 2GB instance. Grants for a non-admin user, rollback, failure recovery, and the effect of uploads on other tenants were not tested. The reference lists what to test before adopting the pattern.

## Verify it yourself

The harness (the `bg` CLI in `orchestrator/`) runs the refresh against a local Neo4j Enterprise server and a temporary Aura instance. It answers four questions:

1. Does `neo4j-admin database upload --to-dbid` load a locally built dump into one specific database on an Aura instance with multiple databases, and how long does each step take?
2. Can a database alias switch a tenant to a new database on Aura without client changes, and what do clients see during the switch?
3. What does the same refresh look like on self-managed Enterprise Edition?
4. Which Aura API calls and payloads work today?

It records every step in `work/runs/<run_id>/ledger.jsonl` and writes `report.md` next to it. `work/` is not committed, because it holds the Aura instance password and the dumps.

### Cost and time

**A live run creates a billed Aura Business Critical instance.** With the defaults (2GB, three tenants) a run takes about 30 minutes, which is roughly 1 GB-hour of Business Critical at your region's rate.

The run deletes the instance at the end unless `AURA_KEEP_INSTANCE=true`. A crashed run can leave it up. `bg aura reap --yes` deletes instances whose names start with `AURA_INSTANCE_NAME_PREFIX` and that are older than `AURA_MAX_RUNTIME_MINUTES`.

### Requirements

- Docker Desktop (or another Compose v2 engine) with at least 6 GB of memory for `SCALE<=10`, and 12 GB for `SCALE>=50`.
- The Neo4j Enterprise image tag in `NEO4J_TAG` (default `2026.10.1`). The local server and the toolbox use the same tag. It must be 2026.07 or later, because uploading to a multi-database instance needs `neo4j-admin database upload --to-dbid`. Setting `NEO4J_ACCEPT_LICENSE_AGREEMENT=yes` accepts the [Neo4j Enterprise license](https://neo4j.com/licensing/). Use it only under terms you are entitled to, such as an evaluation or an existing subscription.
- Aura API credentials, an organization that can create Business Critical instances, and `AURA_REGION` set to a region where that is allowed.
- Browser and Bolt from the host use `http://localhost:17474` and `bolt://localhost:17687`. Those host ports avoid a Neo4j that already listens on 7474 and 7687. The toolbox reaches the server at `neo4j://neo4j-local:7687` on the compose network.

On macOS, files written into `./work` by uid 7474 are usable through Docker Desktop file sharing. On Linux, run `chmod -R a+rwX work` before the first command so the toolbox user can write the mounted work directory.

### Configure

```bash
cp .env.example .env
# fill AURA_CLIENT_ID, AURA_CLIENT_SECRET, and AURA_REGION
```

`AURA_MAX_RUNTIME_MINUTES` defaults to 180, which covers a full run. The watchdog deletes the instance once that time has passed since it was created.

### Commands

```bash
make up          # start neo4j-local and build the toolbox image
make doctor      # bg doctor inside the toolbox
make test-unit   # pytest, excluding the compose integration test
make test-local  # pytest -m local inside the toolbox
docker compose run --rm toolbox gen --tenant acme --cycle c0001
docker compose run --rm toolbox local cycle --tenant acme --cycle c0002 --watch
docker compose run --rm toolbox aura up --yes
docker compose run --rm toolbox aura cycle --tenant acme --cycle c0001
docker compose run --rm toolbox e2e --yes
docker compose run --rm toolbox aura down --yes
docker compose run --rm toolbox aura reap --yes
```

`bg doctor` writes `work/doctor/` (versions and `neo4j-admin` help). If `neo4j-admin` lacks a flag or credential variable the harness needs, it exits 5 and names it. It does not guess a replacement.

### Tests

Unit tests cover naming, the generator (determinism, churn, no dangling endpoints), the Aura client (token cache, 403 refresh, 429 `Retry-After`, redaction), and the report, including the cutover summary.

```bash
cd orchestrator && python3 -m pip install -c requirements.lock -e '.[dev]' && python3 -m pytest
```

The local integration test needs the compose stack:

```bash
docker compose run --rm --entrypoint pytest toolbox -m local
```

The live Aura check is `bg e2e --yes`. Its report is the artifact. There is no pytest marker for Aura.

## Repository layout

| Path | Contents |
| --- | --- |
| `REFERENCE-ARCHITECTURE.md` | The pattern: when to use it, the refresh steps, rollback, security, sizing, cost, and failure handling |
| `docs/evidence/` | Sanitized results of the live run |
| `orchestrator/` | The `bg` CLI and its tests |
| `toolbox/` | The container image that runs `bg` with the Neo4j Enterprise tools |
| `compose.yaml`, `Makefile` | The local Enterprise server and toolbox |

## License

Apache License 2.0. See [LICENSE](LICENSE). Neo4j Enterprise Edition and Aura are licensed separately by Neo4j.

## Security

Report security problems privately to the repository owner rather than in a public issue. Never paste `.env`, `work/`, or an Aura password into an issue or pull request.
