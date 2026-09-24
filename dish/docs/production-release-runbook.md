# Production release and rollback

This runbook owns Dish application-code deployment and rollback. PostgreSQL migrations remain
owned by the [routine migration runbook](postgresql-routine-migration.md). A code rollback never
downgrades the database.

Production runs from the immutable release selected by
`/home/marco/.local/share/dish/prod-current`. The controller records the displaced exact release in
`prod-previous`; it never guesses from directory timestamps. Each release directory is named for
its full Git commit and contains a manifest binding that commit to the expected schema head, the
complete exported source tree, and the installed dependency set.

## One-time service installation

Install the reviewed per-user unit and reload the user manager:

```sh
install -Dm0644 deploy/systemd/dish-service-prod.service /home/marco/.config/systemd/user/dish-service-prod.service
systemctl --user daemon-reload
systemctl --user add-wants default.target dish-service-prod.service
```

Remove or disable any obsolete duplicate before enabling the per-user unit. The canonical
production unit is controlled only with `systemctl --user ... dish-service-prod.service`; never use
the system manager or `sudo` for it.

If `prod-current` already names a working release created before this controller, certify it once
before the first managed activation. Certification requires the directory name and Git `HEAD` to
be the same full commit, no modified or untracked files, a built virtual environment, the current
schema identity source, and matching source/dependency digests:

```sh
dish/scripts/dish-prod-release certify-existing <exact-40-character-commit>
```

This transition command rejects the mislabelled or modified directory instead of blessing it.

## Stage a release

Run from a clean repository that contains the reviewed commit. Staging exports committed bytes,
builds a private virtual environment, hashes the complete exported source tree and installed
package set, and publishes the directory only after all steps succeed.

```sh
dish/scripts/dish-prod-release stage \
  --repository /home/marco/ai-tools \
  --revision <exact-40-character-commit>
```

Staging does not change a service or pointer. Before a schema migration, stage and retain at least
one reviewed fallback release that expects the destination schema. A release expecting an older
schema is not a usable fallback after a forward-only migration.

## Activate

Keep a currently authorized MCP bearer token in
`/home/marco/.config/dish-service/mcp-conformance-token` with mode `0600`. This credential is read
only in process; it is never passed on the command line or written to the receipt.

```sh
dish/scripts/dish-prod-release activate <exact-40-character-commit>
```

Before changing the pointer, the controller verifies the complete exported source-tree digest and
uses the candidate's own runtime to prove that the configured database, schema, authority-generation
ID, and generation release all exactly match live PostgreSQL. It then atomically records
`prod-previous`, switches `prod-current`, performs one service restart, and requires all of these
within the bounded readiness interval:

- the per-user service is active;
- its main process working directory is the selected immutable release;
- private `/health` is ready;
- `/health.code_release` equals the selected Git commit.
- the existing public MCP endpoint accepts an authenticated initialize and a read-only
  `dish_sections` call.

The MCP checks are part of this existing readiness/recovery boundary, not a separate release gate.
Before the pointer switch, the controller also checks the unauthenticated challenge and exact
protected-resource metadata. After successful readiness it writes a mode-`0600` machine-readable
receipt under `/home/marco/.local/state/dish/release-receipts/`. The receipt binds the exact
immutable backend commit and schema, but truthfully records the MCP executable as an
`unbound_mutable_checkout`; `dish-mcp.service` does not execute from the selected release. Unit
evidence is collected through the user manager. Journal content is represented only by a bounded
line count and digest, so credentials and response bodies are not copied into the receipt.

The bare commands read the same environment split and precedence as the production unit:
`prod.env` first, then `postgres-prod.env`. For an explicit non-production rehearsal, repeat
`--env-file` in the desired precedence order before the subcommand.

The database generation identity remains `identity.dish_release`; it is intentionally distinct
from executable `code_release`.

If readiness fails, the controller restores both pointer values exactly, restarts the displaced
release, and verifies that recovery. A failure message explicitly distinguishes a recovered failed
activation from a failure where the prior release also could not be recovered.

## Roll back

```sh
dish/scripts/dish-prod-release rollback
```

Rollback targets only the exact `prod-previous` release and runs the same integrity, schema,
process-path, health, and MCP conformance checks as activation. If no compatible previous release
is recorded, it fails before stopping or restarting production. Never change `prod-current`
manually and never select a release merely because its directory is old.

Read pointer state without mutation:

```sh
dish/scripts/dish-prod-release status
```

For diagnosis, use:

```sh
systemctl --user status dish-service-prod.service
journalctl --user -u dish-service-prod.service
```
