# Evidence: live run on Aura Business Critical, 2026-10-28

This is a sanitized copy of the harness report for one `bg e2e --yes` run with `PAUSE_ITERATIONS=1`. (Pause is now off by default.) Instance IDs, connection URLs, the organization and project IDs, and credentials have been removed. The instance was deleted at the end of the run.

The full, unsanitized output (ledger, watcher logs, upload logs) stayed in `work/runs/`, which is not committed.

## Setup

| Item | Value |
| --- | --- |
| Aura tier | Business Critical, multiple databases enabled |
| Size and cloud | 2GB, GCP |
| Neo4j tools (build and local EE) | 2026.10.1 Enterprise |
| Aura version string | `5.27-aura` (a client-compatibility label, not the calendar version) |
| Tenants | `acme`, `globex`, `initech` |
| Data per tenant per cycle | about 100k nodes and 355k relationships, dump about 10MB |
| Cycles | `c0001` (bootstrap), `c0002` (switch), and `c0003` for `acme` after pause and resume |

Database names in this run were `<tenant>-c<cycle>`. The reference architecture uses `<tenant>-r<sequence>` for the same purpose.

## Answers

| ID | Question | Result |
| --- | --- | --- |
| Q1 | Does `neo4j-admin database upload --to-dbid` load a dump into a specific database in an Aura MultiDB instance? | Yes. Every upload and the count checks after it passed. |
| Q2 | Can a local alias switch a tenant to a new database without client changes? | Yes. Every switch used `CREATE ALIAS` / `ALTER ALIAS` as the instance admin. |
| Q3 | Do pause and resume work for the whole instance, and do aliases and data survive? | Yes. Paused in 166.5s, running again in 137.6s. Aliases and data were intact for all three tenants. |
| Q4 | Does the same cycle work on self-managed Enterprise Edition? | Yes. Import into the server, then `CREATE DATABASE`, then alias switch and drop, for all three tenants. |
| Q5 | Which Aura API calls work? | Instance create with `type: business-critical` and `multi_database: true`. Database create, list, get, and delete on v2beta1. Pause and resume on v1 (`POST /v1/instances/{id}/pause` and `/resume`). |

## Aura refresh timings, in seconds

| Tenant | Cycle | Import | Dump | Create (API) | Database ready | Upload | Online after upload | Schema | Check | Switch | Old database deleted (API) | Total |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| acme | c0001 | 5.8 | 2.8 | 4.4 | 18.8 | 183.3 | 2.9 | 2.1 | 6.0 | 1.6 | | 228.9 |
| acme | c0002 | 5.6 | 2.4 | 4.6 | 13.0 | 161.1 | 1.2 | 3.0 | 6.3 | 1.1 | 4.6 | 211.3 |
| acme | c0003 | 5.9 | 2.6 | 7.0 | 22.7 | 165.8 | 2.4 | 2.6 | 6.6 | 1.4 | 5.3 | 226.8 |
| globex | c0001 | 5.9 | 2.5 | 4.2 | 25.0 | 183.0 | 2.5 | 1.5 | 5.1 | 1.3 | | 231.7 |
| globex | c0002 | 5.5 | 2.4 | 3.8 | 18.9 | 172.3 | 2.3 | 1.6 | 6.6 | 1.2 | 5.5 | 225.8 |
| initech | c0001 | 6.2 | 2.3 | 4.9 | 19.6 | 154.0 | 1.1 | 3.0 | 5.8 | 1.5 | | 199.0 |
| initech | c0002 | 5.7 | 2.4 | 6.8 | 22.1 | 185.3 | 1.1 | 2.9 | 5.7 | 1.2 | 6.5 | 245.2 |

"Switch" is the orchestrator's alias step, including its own checks. The alias change itself is a single statement.

## Self-managed Enterprise Edition timings, in seconds

| Tenant | Cycle | Import | Create database | Schema | Check | Switch | Old database dropped | Total |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| acme | c0001 | 6.0 | 0.5 | 0.7 | 0.1 | 0.01 | | 7.4 |
| acme | c0002 | 5.5 | 0.5 | 0.5 | 0.1 | 0.02 | 0.5 | 7.3 |
| globex | c0001 | 5.6 | 0.5 | 0.7 | 0.1 | 0.01 | | 7.0 |
| globex | c0002 | 5.3 | 0.5 | 0.3 | 0.1 | 0.01 | 0.5 | 6.9 |
| initech | c0001 | 6.0 | 0.5 | 0.5 | 0.1 | 0.02 | | 7.2 |
| initech | c0002 | 5.3 | 0.5 | 0.5 | 0.1 | 0.08 | 0.5 | 7.1 |

## Cutover as seen by clients

A watcher read each tenant's `Snapshot.cycle` through the alias during the `c0001` to `c0002` switch. It used two probes, alternating, about every 250ms: a managed transaction and a plain auto-commit query.

These numbers come from the raw watcher files. The report generated during the run had a bug in its cutover summary: it compared against the wrong cycle and showed no "last old" read. The bug is fixed, and regenerating the report from this run's ledger now gives the same numbers as this table.

| Target | Tenant | Reads | Failed reads | Last read of old data (UTC) | First read of new data (UTC) | Gap | Old data seen after new |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Aura | acme | 24 | 0 | 14:34:02.554 | 14:34:03.768 | 1.21s | 0 |
| Aura | globex | 26 | 0 | 14:41:46.373 | 14:41:47.045 | 0.67s | 0 |
| Aura | initech | 28 | 0 | 14:49:16.576 | 14:49:16.893 | 0.32s | 0 |
| Local EE | acme | 36 | 0 | 14:20:45.053 | 14:20:45.320 | 0.27s | 0 |
| Local EE | globex | 36 | 0 | 14:21:03.014 | 14:21:03.279 | 0.27s | 0 |
| Local EE | initech | 36 | 0 | 14:21:21.601 | 14:21:21.866 | 0.27s | 0 |

The gap is the time between two successive reads, so it is bounded by the polling interval plus query latency on Aura (about 0.2 to 1.2 seconds per read). It is not a period without service. No read failed.

## Pause and resume

| Databases on the instance | Total dump size | Time to paused | Time to running | Aliases intact | Data intact |
| --- | --- | --- | --- | --- | --- |
| 4 (three tenants plus `neo4j`) | 30.7MB | 166.5s | 137.6s | Yes | Yes |

Reads succeeded until the pause took effect, then failed until the instance was running again, as expected. After resume, each tenant's alias still pointed at its `c0002` database and returned `c0002` data. A further refresh (`acme` `c0003`) then ran normally.

## Not tested

- Whether the default `neo4j` database counts toward the database limit.
- Uploading a dump made with a newer Neo4j version than the Aura instance.
- Privileges for a non-admin tenant user across a switch. The run used the instance admin user.
- Rollback by pointing the alias back to the previous database.
- Any failure path: an interrupted upload, a failed check, or an orchestrator restart.
- The effect of an upload on queries for other tenants.
- Data larger than about 10MB per tenant, more than three tenants, other sizes, regions, or clouds.

This was one run. Treat the timings as an example, not a benchmark.
