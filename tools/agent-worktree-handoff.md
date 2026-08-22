# Local-agent human handoff and PR durability

This is the single human-interaction and durable-state contract for Claude Code and Codex agents doing Dish repository Implementation/fix work. It changes presentation and handoff mechanics only; standing role authority, exact-head Review, Integration, TEST/PROD, and safety gates remain unchanged. Task-specific handoffs must not duplicate or weaken it.

When work can continue autonomously, continue. Do not narrate routine repository reads, internal checklists, tool limits, policy analysis, or intermediate reasoning to Marco. When Marco must act, send only current state plus the exact next action, with one short reason only when it changes what he must do. A genuine human-only decision gets one precise question. Do not request authorization that the current task or standing role contract already grants. Before declaring a blocker or routing routine work to Marco, apply the authorized-fallback gate from the standing role contract.

For an ordinary blocker, use exactly two lines: `Blocker:` naming one concrete failure or unavailable condition, then `Action:` giving one exact next action. Do not add an essay, alternatives, background dump, or template placeholders.

When a manual local-agent relay is unavoidable, apply the shared manual-handoff presentation
contract in `OPERATOR_CONTROL_PLANE.md`. Ordinary reconstructable work uses one fenced locator-only
block. A non-reconstructable payload is inline only at or below both shared limits; above either
limit, write the complete current handoff to a private temporary file and show only its exact
absolute path in one fenced block. Never duplicate a file preview or send an addendum that must be
combined with an older handoff.

If the remaining operation genuinely requires Marco's sudo privileges or another human-only local capability after authorized fallbacks are exhausted, use this contract for PostgreSQL bootstrap, package/service setup, and equivalent privileged local work:

1. Write a complete helper script to a concrete bounded path under `/tmp`, for example `/tmp/dish-pg-bootstrap.sh`.
2. Put every required command in the helper. Never hand Marco an ellipsis, placeholder, partial heredoc, or shell fragment he must complete.
3. Make shell helpers fail fast with `set -euo pipefail`, bounded to the named target, and idempotent where practical. Add an explicit refusal check when an accidental broader or remote target would be unsafe.
4. Persist every result needed for later diagnosis or verification to one concrete predictable file, preferably structured JSON when useful; for example `/tmp/dish-pg-bootstrap.json`. Persist failure diagnostics as well as success evidence. Do not ask Marco to copy terminal output when the agent can consume that file.
5. Before the command, explain in one short sentence what the helper reads or changes and why its scope is bounded.
6. Give exactly one runnable command, for example `sudo bash /tmp/dish-pg-bootstrap.sh`, then state the exact output-file path and one concise success signal.
7. After Marco reports completion, read the persisted output file when host/tooling permits and continue without re-requesting the same authorization or re-explaining prior context.

Never send the privileged handoff before the complete helper and result path exist.

Lineage identity is branch-incarnation scoped. One task may have multiple concurrently valid agent-worktree lineages only when they use different repository-wide admitted branches. Each admitted branch has one immutable `lineage_id`; the same branch or same PR cannot become two valid writers across hosts. Agent-worktree-managed branch names are single-use after terminal tombstoning, so cleanup/recreation never revives stale ownership. Task-only status may aggregate sibling lineages, but a task-only mutating operation with multiple lineages must fail `LINEAGE_AMBIGUOUS` rather than choosing one heuristically.

For a task-specific **exact-byte publication bundle** handoff, the handoff may explicitly authorize immediate start. When it does, do not pause to reconfirm routine execution. Treat the single named `~/Downloads/<bundle>` as the only required human-provided download: ignore unrelated files in `~/Downloads`, and do not stop merely because `.sha256`, manifest, checksum, or other sidecars are absent. Verify the bundle itself against the handoff's expected exact head/tree using the repository lifecycle helper. Only a missing/unreadable/invalid bundle or exact-tree mismatch blocks exact-byte continuation. The sending agent must have already attempted the normal GitHub connector publication path and must tell Marco the exact stop reason plus the concrete connector action(s) tried. Bundle handoff is not justified merely by size/file count. The sender must also deliver the one bundle through a working directly downloadable file/attachment surface, not the ChatGPT generated-file/artifact-card or sandbox-link/card form Marco has reported as non-working; if working bundle delivery itself is unavailable, that remains an unfinished publication blocker and the sender reports what delivery surfaces it tried.

Once an implementation PR exists, it is the durable candidate/handoff surface. Keep it current proactively; Marco does not need to ask for a PR update. Any implementation state change that affects candidate identity, evidence/readiness, blocker/recovery state, or the next owner/action must be durable on the PR before it is reported to Marco. State-changing examples include a new published head, focused evidence completion/failure, a publication/environment/local-capability blocker, blocker recovery, or review-ready transition. Update the PR description when canonical handoff fields changed; otherwise use one concise signed Implementation comment, and verify the write/readback. Avoid comments for routine progress that does not change state.

After successful head publication, the PR must identify the new exact head SHA, focused evidence, current implementation state, and one next action when unfinished. If head publication fails but the existing PR discussion surface is writable, post the blocker there before notifying Marco. Record exact current remote PR head SHA; exact local unpublished head SHA when one exists; blocker/failure class and current implementation state; publication action and authorized fallback attempted; persisted evidence/output-file path when relevant; and one exact next action. Branch/head publication and PR discussion are separate transports: failure of the former does not excuse skipping the latter. Never leave critical implementation or blocker state only in chat.

If the PR discussion surface itself cannot be written after authorized fallbacks, use the standing Implementation publication-blocker rules and tell Marco only the residual blocker plus action. Final human output stays control-plane terse; when the task handoff requires the compact form, return only PR number, exact current PR head SHA, PASS/FAIL, and next action. Substantive evidence and blocker detail belong on the PR.
