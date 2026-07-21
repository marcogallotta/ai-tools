# Dish planning protocol — tool-aware beta

Use `dish` as the only interface for a protocol-managed Cooking task. Commands return JSON; follow
`allowed_actions` and do not infer a next step from prose.

## Start and bind the work

1. Create a bare task when needed:
   `dish create --agent claude|gpt|codex --title "<working title>"`.
2. Read the current task with `dish read <task-gid> --agent <agent>`.
3. Start planning with
   `dish start <task-gid> --agent <agent> --kind planning`.
4. Read the complete frozen release returned by `start` before authoring. The returned planning
   protocol and manifest govern this submission until it ends.

Planning starts only from empty notes. Its free working title is preserved unchanged; do not pass
structured-title arguments during planning. Produce one complete candidate file matching the returned
planning manifest. It must contain the complete Planning brief, including one `Destination section:`
and one `Exemptions:` field. Do not submit a patch or fragment.

## Validate and submit

Run:

`dish prepare <submission-id> --agent <same-editor> --file <candidate-file>`

Planning uses deterministic validation only. On `VALIDATION_FAILED`, correct the complete file and
run `prepare` again on the same submission. When the result state is `ready`, run exactly once:

`dish submit <submission-id> --file <same-complete-candidate-file>`

Use `dish inspect <submission-id> --agent <agent>` whenever state or legal next actions are unclear.
A terminal or Human-action result is a stop condition; report it rather than inventing a bypass.
