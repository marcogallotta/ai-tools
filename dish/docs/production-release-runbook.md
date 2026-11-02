# Production release and rollback

## Commands from the repository root

```sh
dish/scripts/dish-prod-release deploy latest
dish/scripts/dish-prod-release status
dish/scripts/dish-prod-release rollback
```

These commands target **production** and release backend and MCP together. For the separately
installed **TEST rehearsal**, use the explicit profile:

```sh
dish/scripts/dish-prod-release --profile test deploy latest
dish/scripts/dish-prod-release --profile test status
dish/scripts/dish-prod-release --profile test rollback
dish/scripts/dish-prod-release --profile test recover
```

Run the requested command and consume its result. It scripts its own checks; no manual SHA,
health curl, process inspection or token copying is needed. `--json` before the command returns
the same verdict and machine-readable evidence. Exit 0 means verified success; 1 means status
unhealthy/unknown; 2 means usage/precondition failure; 3 means failed deployment with verified
recovery; 4 means recovery failed. A failed deploy remains a failure after successful recovery.

An interrupted operation blocks deploy, pinned activate and rollback. Status names its identity
and exact recovery command. `recover` restores the recorded compatible recovery release, without
resolving latest. A matching terminal receipt makes retry observational. External pointer,
configuration, unit-binding or schema drift is refused before recovery effects; restore the
recorded state before retrying rather than guessing.

## Preparation and pinned operations

`latest` resolves fixed canonical `marcogallotta/ai-tools` GitHub `refs/heads/main` once and prints
the full SHA immediately. Main moving later does not retarget it. Fetching uses the object store
without changing checkout/index. This command does not merge code.

```sh
dish/scripts/dish-prod-release prepare latest
dish/scripts/dish-prod-release stage --repository /home/marco/ai-tools --revision <exact-40-character-commit>
dish/scripts/dish-prod-release activate <exact-40-character-commit>
```

`prepare latest` builds and qualifies without changing operational pointers/processes. Cold deploy
includes preparation; warm deploy reuses a staged, exact-qualified artifact. Results separate
preparation and activation durations. Activation and rollback never install dependencies or fetch.
Staging requires configured Python 3.12 at
`/home/marco/.local/share/dish/python-3.12.13/bin/python3.12`; invalid explicit `--python` fails.
Owner-only `~/.config/dish-service/release-prod.json` may set `python` and exact `recovery_release`.

The manifest binds commit, complete source digest, dependencies, actual interpreter and checked-in
`deploy/hot-deploy-contract.json`. Routine eligibility requires `paired-hot-v1`, transport generation
1 and config generation 1. Both mixed pairs and candidate/candidate must pass real authenticated
read-only qualification using the configured MCP backend transport. For Action HTTP, MCP calls
the opposite release's actual Action listener using isolated credentials; successful backend replies
bind the serving code/PID. Candidate/candidate evidence permits later same-target stale-MCP repair
without a second prepare. Native PostgreSQL-only MCP reads do not certify the Action interface.
Evidence binds candidate and retained/recovery SHAs, source digests,
schema, config and contract generations. Missing/stale evidence refuses disruption and names
`prepare latest`. Missing declaration or incompatible transport/config changes require the existing
rollout plan rather than a compatibility guess.

Production environments retain `prod.env` then `postgres-prod.env` precedence. Differing ad-hoc
target flags are explicitly refused for control commands: use installed `--profile test`, whose
`env_files` preserve declared order. Pinned stage/activate command forms remain supported. The
controller never silently ignores a target override and operates production instead.

## Readiness, credentials and recovery

Before effects, the controller verifies source/dependency integrity, live PostgreSQL schema/database/
authority identity through the candidate runtime, effective paired unit bindings, endpoint identity,
credential permissions and authenticated reads, exact pair evidence, and an integrity-checked compatible
recovery release. Current is normally recovery. If a forward-only migration made it incompatible,
configure an explicitly prepared compatible fallback. Missing fallback refuses disruption.
[Routine migrations](postgresql-routine-migration.md) retain schema authority; rollback never downgrades
any database.

MCP bearer file `~/.config/dish-service/mcp-conformance-token` requires mode `0600`. Deploy preflight
can refresh using existing `mcp-conformance-refresh.json`; it never opens a browser or creates
credential authority. If interactive renewal is required, the supported action is:

```sh
dish/scripts/dish-release-credential sign-in
```

The helper prints the login URL and stores credentials privately. Explicit silent renewal is
`dish/scripts/dish-release-credential renew`. Results/receipts exclude tokens, response bodies and
OAuth contents.

Deployment durably records exact prior pointer values and recovery identity before switching. It
sets previous to the displaced release, changes current, restarts backend then MCP and verifies both
within one readiness deadline. This is recoverable sequential deployment, with a brief mixed-version
interval. Failed activation has a separate bounded recovery deadline. Compatible prior recovery
restores both exact pointer strings; fallback recovery reports actual current/previous values and
never calls a never-running fallback previous. Unproven recovery remains pending and reports failure.

Status proves live versions through PID/start identity, immutable cwd/executable/arguments, listener
ownership, backend health and authenticated MCP process-start identity. Missing evidence is unknown.
Previous incompatibility reports `rollback_available=false` without making healthy current processes
unhealthy. Persistent receipts describe historical operations only.

## One-time installation and bootstrap

Routine commands refuse uncommissioned units/releases. Installation and bootstrap require separately
authorized work. Preserve prod/postgres/MCP environments, public ports/routes, OAuth storage and
Honest store/write paths. Install both reviewed user units:

```sh
install -Dm0644 dish/deploy/systemd/dish-service-prod.service ~/.config/systemd/user/dish-service-prod.service
install -Dm0644 dish/deploy/systemd/dish-mcp.service ~/.config/systemd/user/dish-mcp.service
```

The installed host may have `dish-mcp.service.d/10-merged-source.conf` pinning an older release.
During that separately authorized installation, retain a persistent copy and remove only its
superseded WorkingDirectory/ExecStart overrides. Preserve unrelated overrides/secrets. Then run
`systemctl --user daemon-reload` and verify effective ExecStart/WorkingDirectory for both units
select `prod-current/dish`. Do not restart production during repository implementation or TEST
qualification. Retain `KillMode=control-group`.

For pre-controller releases, `certify-existing <exact SHA>` checks clean exact Git identity and
source/dependency bytes. It does not invent hot-deploy declaration or pair evidence. Bootstrap must
establish a declared, qualified current/recovery pair before routine use.

## Isolated qualification and timing fixture

The implementation/test owner installs real persistent resources once from a committed candidate
using the repository-local test environment:

```sh
dish/.venv/bin/python dish/scripts/dish-release-rehearsal --repository /home/marco/ai-tools --candidate <exact candidate SHA>
```

Setup creates `~/.local/state/dish/release-rehearsal/` with native PG17, real user units, private
ports/env/database, separate signed bearer trust/storage, Pantry snapshot, release/pointer/receipt
state and deterministic local upstream. It installs fixed `~/.config/dish-service/release-test.json`
and `release-qualification.json`. Existing fixtures are preserved. TEST admission refuses incomplete
or overlapping configurations. Separate qualification resources exercise both exact mixed pairs
without changing operator rehearsal pointers. `setup-probe.json` must pass before timed agents launch.
The separate TEST bootstrap launcher imports application modules/dependencies from each immutable
release's working directory. This permits retained artifacts to use the same isolated auth/listener
setup without editing their bytes. Its digest is bound into TEST configuration evidence. Each backend
hosts actual private-health and Action HTTP listeners; MCP selects its real configured adapter. An
append-only observer records only Action path/status/success and process identity, never credentials
or response content. A negative control disabling the Action route must fail pair qualification while
backend health and operator services remain intact.
If the first build fails, retain its logs and use the same command with `--resume-build` and the
corrected committed candidate. This rebuilds only incomplete TEST fixtures and preserves earlier
immutable releases, databases and logs; it refuses a setup that already passed.

TEST uses real cryptographically verified bearer authentication and production HTTP/FastMCP apps.
It does not commission live GitHub OAuth, canonical GitHub fetch or production activation. Check
production-default resolution read-only until separately authorized commissioning. The synthetic
retained fixture has identical transport source with a distinct immutable commit; retain that limit.
No fake shell, production credentials or hidden agent environment injection may replace execution.
After independent implementation Review passes, timing agents receive only checkout entry point
and a goal explicitly naming TEST.
