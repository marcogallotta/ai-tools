# Dish verification protocol — tool-aware beta

Verification uses the frozen release and complete candidate attached to one `dish` submission. The
verifier must be from the family routed by the tool. Before reviewing, use
`dish inspect <submission-id> --agent <verifier>` to confirm state, required family, and the exact frozen bundle.

Review the entire candidate against the returned verification protocol and complete-task manifest.
Do not approve a fragment. Confirm the candidate's Destination and exemption set, and confirm that
its recorded self-review and process fields satisfy the frozen release.

## Approve

With no verifier edit:

`dish approve <submission-id> --agent <verifier> --file <complete-file> --correction none`

After a clear small correction made by the verifier:

`dish approve <submission-id> --agent <verifier> --file <corrected-complete-file> --correction small`

Approval reruns deterministic validation. Destination drift returns the submission for a new
Research preparation rather than silently changing the frozen handoff.

## Reject

Return the submission to Research with:

`dish reject <submission-id> --agent <verifier> --reason "<concrete reason>"`

On the second unsuccessful verification pass, also provide:

`--changed-since-prior "<what materially changed since the previous pass>"`

Use `--take-ownership` only when the verifier is taking responsibility for a material correction;
the next preparation is then attributed to that verifier and routes to the opposite family.

A second rejection enters Human Review. Stop and report the complete escalation result to Marco.
Do not attempt another agent workflow command until the tool reports a legal next action.
