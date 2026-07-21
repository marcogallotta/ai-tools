# Dish research protocol — tool-aware beta

Use `dish` as the only interface for a protocol-managed Cooking task. Commands return JSON; follow
`allowed_actions` and keep every handoff as one complete candidate file.

## Start and bind the work

Read the task with `dish read <task-gid> --agent <agent>`, then start the correct submission:

- Initial construction:
  `dish start <task-gid> --agent <agent> --kind initial`
- Small change:
  `dish start <task-gid> --agent <agent> --kind change --change-level small --change-reason "<reason>"`
- Large change:
  `dish start <task-gid> --agent <agent> --kind change --change-level large --change-reason "<reason>"`

Read the complete frozen release returned by `start` before authoring. Its research protocol,
verification protocol, and complete-task manifest govern the submission until it ends. Do not reuse
a candidate authored before that binding.

## Produce the complete candidate

Build one complete task file matching the returned manifest. Preserve the Planning exemption set
unless Marco approved a revision. Initial and large-change candidates must include the required
self-review attribution in `Self-verified:`. A small change must preserve the existing
`Verification:` line byte-for-byte.

Run:

`dish prepare <submission-id> --agent <same-editor> --file <candidate-file>`

Add `--exemption-revision "<Marco decision, date, and reason>"` only when the command contract permits
an approved exemption change. On validation failure, correct the complete file and prepare again on
the same submission.

Small changes become `ready` after accepted preparation. Initial and large changes move
to `awaiting_verification` and require the opposite agent family. Preserve the exact candidate file
for that handoff. Use `dish inspect <submission-id> --agent <agent>` for the frozen bundle, routing,
state, and legal next actions.

When the submission is `ready`, run exactly once:

`dish submit <submission-id> --file <approved-complete-candidate-file>`

A Human-action or terminal result is a stop condition; report it rather than inventing a bypass.
