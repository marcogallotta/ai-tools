# Dish local operating contract (Steps 1–10)

## Authority and scope

The live Asana Cooking task is authoritative for title, body, workflow state, provenance, and cooking instructions. Agents access protocol-managed Cooking tasks only through `dish`; they do not read or write those tasks through the generic Asana CLI. The `ai-tools` checkout supplies that mediated interface, deterministic validation, persistence, and recovery state. Neither the tool nor its SQLite database replaces agent judgment or the governing protocol.

This document covers the single-agent local test path. It does **not** authorize concurrent or live multi-agent use. Step 11 supplies the shared-service access path, credentials, and GPT Action surface.

## Local invocation

Run from the `ai-tools` checkout with the bundled interpreter:

```text
bin/.venv/bin/python3 bin/dish <command> ...
bin/.venv/bin/python3 bin/dish-admin <command> ...
```

Required environment:

- `DISH_HONEST_PATH`: the compatible Honest rollout checkout containing `DISH_VERSION`, the task schema, migrations, and protocols.
- `DISH_DB_PATH`: optional local SQLite path; defaults to `~/ai-tools/var/dish-tool.db`.
- normal Asana CLI credentials used by the backend in local test mode.

Candidate files are ephemeral complete-text inputs. The live task is reread before mutation and after every write or move. Do not edit a candidate after recording the identity supplied to Verification.

## Agent commands

```text
dish sections --agent claude|gpt|codex
dish create --agent claude|gpt|codex --title TITLE
dish read TASK_GID --agent claude|gpt|codex
dish inspect OPERATION_ID --agent claude|gpt|codex

dish start TASK_GID --agent AGENT --kind planning|initial|change|verification \
  [--run-id ID] [--independence-attestation TEXT] \
  [--change-level small|large --change-reason TEXT]

dish prepare OPERATION_ID --agent AGENT --file PATH \
  [--exemption-revision TEXT] \
  [--material-classification material|non-material] \
  [--dish-name TEXT --recognition TEXT \
   (--role non-main | --no-role-tags) \
   (--blocker MARKER | --no-blockers)]

dish approve OPERATION_ID --agent AGENT --correction none|small \
  [--file PATH] [--reviewed-identity ID] \
  [--run-id ID | --independence-attestation TEXT] \
  --semantic-review-complete --provenance-complete \
  [title-declaration arguments as above]

dish reject OPERATION_ID --agent AGENT --reason TEXT \
  [--route large|evidence|human-review] [--file PATH] \
  [--run-id ID | --independence-attestation TEXT] \
  [--resume-status pending-verification|pending-research] \
  [--changed-since-prior TEXT] [--take-ownership]

dish submit OPERATION_ID --file PATH
```

`submit --file` remains part of the Step 10 parser contract even though submit is movement-only; the implementation uses the live signed task and never rewrites content during submission.

### Phase boundaries

- **Planning:** `start --kind planning`, perform protocol work, then `prepare` before Research handoff.
- **Research:** `start --kind initial` or `change`, perform protocol work and self-review, then `prepare`. The command writes and confirms the complete `pending-verification` task before any Research Queue → Verification Queue move.
- **Verification:** `start --kind verification`, perform semantic and provenance review, then `approve` or `reject`. After a successful approval, run the returned `submit` action in the same pass.
  The decision command must repeat the exact verifier run ID or attestation recorded by Verification start; an agent-family label alone is not authority.
- **Later edits:** begin a new `change` operation. Material edits invalidate prior Verification; explicitly non-material edits preserve it only when the protocol permits.

## Marco-only commands

```text
dish-admin migrate TASK_GID
dish-admin recover OPERATION_ID --outcome not-applied|applied --reason TEXT
dish-admin discard OPERATION_ID --reason TEXT
dish-admin unblock OPERATION_ID --reason TEXT
dish-admin reopen OPERATION_ID --category evidence|premise|method|scope \
  --before TEXT --after TEXT --editor TEXT --date DATE
```

- `migrate` is only for an individually encountered older-schema task after cutover. It writes, rereads, validates, and records the new schema only after confirmation.
- `recover` reconciles an interrupted write or movement against a fresh live Asana reread. It records `confirmed` only when the live title/notes identity or section exactly matches the persisted intended mutation, and records `not_applied` only when live evidence proves the persisted expected state remains. A contradictory requested outcome fails closed; the command never repeats the mutation.
- `reopen` is the only path out of the two-pass Human Review hold and requires a substantive reset recorded in `Material changes`.

## JSON response contract

Every invocation writes exactly one compact JSON object to stdout:

```json
{
  "ok": true,
  "command": "read",
  "code": "OK",
  "task_gid": "...",
  "submission_id": null,
  "state": null,
  "retryable": false,
  "allowed_actions": [],
  "data": {},
  "errors": []
}
```

- `task_gid` identifies the Asana task when known.
- `submission_id` is the operation identifier retained for CLI compatibility.
- `state` is tool operation state, not protocol readiness.
- `allowed_actions` is the bounded next tool action list.
- `data` contains command-specific exact identities, diagnostics, protocol text, or completion facts.
- `errors` contains structured findings with a `rule` and any supporting fields.

## Result codes and exit statuses

| Code | Exit | Meaning and handling |
|---|---:|---|
| `OK` | 0 | Deterministic command success. Continue the protocol’s semantic duty; a pass is not substantive approval. |
| `INVALID_ARGUMENT` | 2 | Fix command syntax or required arguments; rerun only after correction. |
| `NOT_FOUND` | 2 | Confirm the task/operation identifier. Do not create substitute state. |
| `UNMANAGED_TASK` | 2 | Task is outside the governed Cooking scope; do not force it through this workflow. |
| `VALIDATION_FAILED` | 2 | Agent-correctable only when the protocol makes the defect agent-owned. Correct the exact task/candidate, update provenance or `Material changes` where required, reread, and rerun. |
| `WRONG_STATE` | 3 | Inspect the live task and operation; take only a returned legal action. |
| `AGENT_MISMATCH` | 3 | The caller is not the recorded actor. Use the correct actor or a protocol-valid ownership route. |
| `VERIFIER_FAMILY_MISMATCH` | 3 | Legacy compatibility code; treat as a closed transition and inspect. Current Verification independence is identity/attestation based, not opposite-family routing. |
| `CONFLICT` | 3 | Stale identity, open-operation conflict, placement conflict, or another exact-state conflict. Preserve live content and restart/inspect as directed. |
| `HUMAN_ACTION_REQUIRED` | 3 | Stop normal agent workflow. This is valid only when the underlying protocol condition independently requires Marco; a tool message alone never creates Evidence or Human Review. |
| `BACKEND_REJECTED` | 4 | Backend proved non-application. Preserve state, diagnose, and rerun only when the reported cause is corrected. |
| `BACKEND_UNCERTAIN` | 5 | Outcome is ambiguous. Do not repeat the mutation. Preserve the task and use Marco-only recovery after a live reread. |
| `INTERNAL_ERROR` | 1 | Tooling failure. Preserve live task/content and report the command, identifiers, content identity, error, and diagnostics. |

The JSON `retryable` field is authoritative for mechanical retry advice. Even when true, correct the reported condition first. Never retry `BACKEND_UNCERTAIN` as a normal command.

## Interpreting outcomes

- **Tool pass:** deterministic conformance only. Continue the stage’s semantic work.
- **Agent-correctable finding:** fix the underlying protocol-owned defect, preserve required provenance, write/re-read through the tool, and rerun the same boundary check.
- **Possible Evidence or Human Review:** route there only when the underlying factual or judgment issue meets the protocol definition. Small/Large/Evidence/Human routing remains agent/protocol judgment.
- **Execution error or ambiguous result:** preserve task state and content. Report it as a tooling failure, not a dish blocker.
- **Tool/protocol disagreement:** fail closed, preserve the exact live task, stop the affected transition, and report the conformance defect. The protocol wins.

## Rerun rules

- Reread or inspect before deciding what to rerun.
- A stale baseline requires a new exact operation; never overwrite the live edit.
- A confirmed content write is naturally idempotent and must not be repeated.
- A confirmed content write, Verification signoff, and destination submission movement are independent completion facts. Recovery reconciles only interrupted backend attempts; it does not invent signoff or treat a Research/Verification handoff as destination submission.
- A successful `approve` returns `submit`; the verifier runs it in the same pass.
- If the task is already at its valid destination, `submit` records a confirmed no-op `destination_submission` movement attempt and then completes idempotently.
- Approval never implies final movement. Planning and Verification handoffs use their own movement purposes; only a confirmed `destination_submission` attempt satisfies final submission movement.
- Missing/invalid destination leaves the task `ready` with diagnostics and blocks movement only.

## Troubleshooting checklist

1. Save the complete JSON result and process exit status.
2. Run `dish read TASK_GID --agent AGENT` and, when an operation exists, `dish inspect OPERATION_ID --agent AGENT`.
3. Compare the reported live identity, reviewed/signed identity, placement, schema version, and legal actions.
4. For compatibility failure, confirm `DISH_HONEST_PATH`, `DISH_VERSION`, schema assets, and the exact supported protocol/schema pair.
5. For migration required, stop normal commands and ask Marco to run `dish-admin migrate`.
6. For a `started` or `uncertain` write/movement, do not retry the backend mutation. Use `dish-admin recover` after a live reread; recovery must match persisted expected/intended evidence and records the reconciliation outcome durably.
7. For tool/protocol disagreement, preserve the task unchanged and report both the protocol clause and tool rule.

The corpus migration and live cutover remain separately authorized Step 12 work. This local contract does not authorize live Cooking-task writes or multi-agent activation.
