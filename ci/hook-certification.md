# Claude/Codex hook certification

Use this runbook for pre-Review exact-head testing of the repository-owned Claude/Codex hook surface. It is an execution surface for the existing installed-host certificate; it does not add a lifecycle state, Review authority, Integration authority, dispatch mechanism, or installed activation authority.

## Normal command

Run the command from the existing local Implementation continuation created with a fresh `tools/agent-worktree claim --require-launch-provenance` identity:

```sh
tools/dish-hook-certify --pr <pr-number> --head <exact-40-char-pr-head>
```

The command re-reads the live PR/branch/head, rejects dirty or moved candidates, derives active hook ownership from `.claude/settings.json` and `codex/hooks.json`, runs Tier A/B evidence, preflights every required Tier-C host before child launch, launches only the required host adapters, validates the exact candidate/config/hook bytes, restores or isolates host state, and posts then re-reads the canonical exact-head certificate comment.

`--plan-only` prints the mechanically selected active surface and A/B/C plan without launching hosts. `--no-post` retains validated local evidence without writing the PR comment and is for diagnosis only; it does not satisfy the durable gate.

Evidence is retained under `~/.local/state/dish/hook-certification/runs/`.

## Evidence selection

Tier A is deterministic component/config/core evidence and is always run by the normal command. Tier B is hermetic protocol/entrypoint evidence using isolated state. Tier C is selected only when the changed boundary actually depends on a host adapter/config/install/certification-harness boundary or a known host-specific regression.

The active surface is not a second maintained manifest. Direct adapters and their shared Python hook components are derived from the authoritative Claude/Codex configuration. Dormant scripts in `hooks/` do not create a Tier-C gate merely by existing. A host-neutral active core change with unchanged host contract remains A+B unless another selected boundary requires Tier C.

## One-time Codex certificate identity

Codex uses a dedicated certification home, never the live operator Codex home:

```sh
CODEX_HOME="$HOME/.local/state/dish/hook-certification/codex-home" codex
```

Complete normal browser authentication in that dedicated home once. Routine certification reuses only that test identity, gives each attempt fresh Dish child state, and temporarily writes an exact-candidate rebased `hooks.json` inside the certification home. The prior certification-home hook config is restored after the run. Never copy or symlink live `~/.codex` auth/config into the certificate home.

The Codex golden sequence is: harmless governed action -> `/compact` -> exact candidate re-ground ready before the next action -> second harmless action -> deliberate `git switch main` conflict denied before Git runs -> clean exit/readback.

## One-time Claude isolated environment

The Claude container path remains proof-before-process until an exact candidate has passed it end to end. Building or merging this harness does not make the route operational, relax the temporary hook freeze, or make installed activation complete.

Initialize a pinned certificate image and dedicated Claude auth/settings volume once:

```sh
tools/dish-hook-certify setup-claude --version <pinned-approved-claude-code-version>
```

Authentication occurs inside the dedicated certification volume. Do not mount or copy live `~/.claude` credentials. Normal runs materialize the exact PR candidate into isolated container state, run Claude as non-root, restrict network egress to the inference/auth telemetry endpoints required by the harness, and mount protected host-probe state read-only. The real interactive Claude CLI drives the compact lifecycle; noninteractive `claude -p` is not substituted when actual hook delivery is the evidence boundary.

The first successful exact-head Claude proof is evidence about the harness/host boundary only. Any policy decision to make that route mandatory or to supersede the temporary hook freeze remains separate authority.

## Preflight and failure classes

All required host prerequisites are checked before the first Tier-C child starts. In particular, if an exact candidate still references a worktree-local `tools/.venv/bin/python`, the command fails before host launch when that interpreter is missing; this prevents the historical post-`/compact` failure from being rediscovered inside Codex.

Failures should be read as one of these bounded classes:

- **candidate** — exact candidate hook/config logic failed, exact byte/path binding failed, or the candidate mutated the isolated workspace;
- **host** — installed CLI/version/loader/PTY behavior failed at the boundary being certified;
- **auth/setup** — the dedicated Codex or Claude certificate identity is absent/expired; perform only the stated one-time setup/login action;
- **harness/preflight** — required local tooling, launch provenance, isolation, or durable publication/readback failed before the candidate can be trusted.

A moved PR head invalidates the attempt. Re-run on the new exact head; do not reuse the previous certificate.

## Exact-path and protected-host readback

A passing certificate does not trust producer-supplied booleans alone. The validator requires candidate file evidence and verifies required effective config sources and active hook targets against the exact candidate paths, Git blob IDs, and SHA-256 values. Stale, unrelated, or wrong-checkout config/hook targets fail mechanically even when the rest of a certificate says PASS.

Normal Tier C uses isolated host state and must leave live Claude/Codex config and repository-installed hook files unchanged. A source merge is not installed activation. Installed activation, rollback, Review, and Integration remain separately governed operations.
