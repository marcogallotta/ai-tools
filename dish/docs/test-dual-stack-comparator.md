# TEST dual-stack comparator qualification

This runbook qualifies the PostgreSQL/no-Asana TEST authority against a disposable legacy SQLite/Asana oracle. It is a pre-PROD comparison gate, not HA, failover, mirroring, or bidirectional synchronization.

## Authority topology

| Role | Private listener | Action listener/router path | State/effects |
|---|---|---|---|
| TEST authority | `127.0.0.1:8765` | `127.0.0.1:8766` via `/test` | PostgreSQL only; no Asana environment; external effects disabled |
| Legacy oracle | `127.0.0.1:8795` | `127.0.0.1:8796` via `/test-legacy` | separate SQLite root plus designated disposable TEST Asana project |
| Production | `127.0.0.1:8775` | `127.0.0.1:8776` at router root | unchanged by this qualification |

Caddy routes each path to exactly one upstream. There is no alternate upstream, load balancing, or automatic fallback from PostgreSQL to legacy. Ordinary TEST traffic uses `/test`; `/test-legacy` exists only for the curated comparison runner.

The old TEST dark-launch worker is incompatible with this topology because it copies legacy observations toward PostgreSQL. Keep `dish-shadow-worker-test.service` disabled and stopped for comparator qualification. Do not use shadow replay, request mirroring, or any other convergence mechanism to keep the two writable stacks aligned.

## Install and preflight

Install the reviewed `dish-service-test.service`, `dish-service-test-legacy.service`, router JSON/script, and owner-readable `test.env` / `test-legacy.env` derived from the checked-in examples. Use distinct Action tokens. The authority environment must contain no populated key whose name contains `ASANA`; the oracle must have `DISH_TEST_COMPARATOR_DISPOSABLE=1` and use only `/home/marco/.local/state/dish/test-legacy` for SQLite/backups.

Before starting the rig:

```sh
sudo systemctl disable --now dish-shadow-worker-test.service
sudo systemctl daemon-reload
sudo systemctl restart dish-postgres-test.service dish-service-test.service dish-service-test-legacy.service
sudo systemctl restart caddy
/home/marco/ai-tools/dish/deploy/caddy/dish-action-route status
```

The route status must show exactly production `127.0.0.1:8776`, TEST authority `127.0.0.1:8766`, and comparator `127.0.0.1:8796`. Verify `dish-service-test` health reports `backend=postgresql`, `profile=test`, and an empty `asana_environment_keys`; verify the legacy health endpoint is ready against the designated disposable project.

Run native PostgreSQL certification against the exact disposable TEST DSN/candidate before treating comparator evidence as cutover qualification. TEST evidence does not prove PROD.

## Compare

A read-only diagnostic run is useful but cannot satisfy the qualification gate:

```sh
cd /home/marco/ai-tools/dish
scripts/dish-pg-test-comparator
```

Run the full curated comparison only after the disposable project/state has been confirmed:

```sh
scripts/dish-pg-test-comparator --allow-mutating-scenarios
```

The runner performs route/runtime identity checks before any mutation, sends each curated scenario explicitly to both routes, normalizes declared UUID/timestamp/identity differences, and writes atomic evidence under `/home/marco/.local/state/dish/test/comparator-evidence/` including `latest.json`. Tokens are never written into the report.

Full qualification requires all of:

- `mismatch_count=0`
- `skipped_count=0`
- `full_qualification=true`
- `qualification_passed=true`

A mismatch is evidence, not authority. Fix every material mismatch or obtain Marco's explicit acceptance before PROD cutover.

## Reset the oracle instead of synchronizing

TEST drift is expected. When legacy state no longer provides a useful comparison corpus, stop `dish-service-test-legacy.service`, reset/recreate only the designated TEST Asana project using the established TEST reset procedure, discard/reseed the legacy SQLite/backups under `/home/marco/.local/state/dish/test-legacy`, then restart the oracle and rerun preflight. Do not copy PostgreSQL state into legacy, copy legacy state into PostgreSQL, or attempt to keep the authorities continuously converged.

Production state, routing, and cutover authority are outside this procedure.
