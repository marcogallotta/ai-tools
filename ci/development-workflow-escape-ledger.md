# Development Workflow escape ledger

The canonical escape-analysis surface is the owning Development Workflow task. A confirmed escape is appended as exactly one Asana story/comment whose first line is:

```text
<!-- dish-development-workflow-escape:v1 -->
```

The remainder of that comment is exactly one JSON object conforming to [`schemas/development-workflow-escape-v1.schema.json`](schemas/development-workflow-escape-v1.schema.json). Use `scripts/development_workflow_escape.py render` to produce the canonical comment and `fold` to read an exported story set. V1 never rewrites task notes/current context, creates a projection, or writes any lifecycle state.

## Identity and provenance

`escape_id` is `sha256:` plus the SHA-256 of canonical JSON containing only the exact repository-qualified `affected_change` identity and the sorted exact `discovery_evidence` identities. Identical exact evidence therefore identifies one escape; the fold de-duplicates byte-identical repeats and fails closed if the same identity carries conflicting record content. A recurrence needs different exact source evidence and therefore a different `escape_id` even when the root class is the same. GitHub identities include `owner/repository`; an unqualified PR, Review, run, or commit is not exact evidence.

Evidence stays source-correct. Repository-qualified GitHub PR/head, commit, formal Review, run, and comment identities describe repository facts; Asana task/story identities describe orchestration facts; runtime evidence remains runtime evidence. Lifecycle-economics telemetry is supporting operator-impact evidence only, referenced separately as `telemetry:<source>:<id>`; it is never valid `discovery_evidence` and therefore cannot enter `escape_id`. Missing evidence is the literal `UNKNOWN`, never zero or an inferred fact. Telemetry absence/degradation cannot change escape identity or truth.

Authenticated GitHub/Asana account attribution is not a Marco decision. `marco_approval.status=YES|NO` is valid only with an exact `asana:decision:<decision-id>@story:<story-gid>` that binds an explicit durable Marco decision. Actor/author/creator identity is not accepted as decision provenance.

The root class is exactly one member of the closed v1 set in the schema. The corrective owner is exactly `asana:task:<gid>` or `UNOWNED`; the ledger does not create that owner automatically. Adding/changing root classes or identity semantics is a reviewed schema change.

## Append/read boundary

Development Workflow and bounded Audit reconciliation may append a validated record when current authority confirms an escape. Dedupe exact evidence before appending. An `UNOWNED` material escape goes through ordinary Development Workflow triage/dedupe to an owning task; this helper does not prioritize, dispatch, schedule, Review, merge, close work, or relax a human gate. This change does not modify Coordinator authority; any Coordinator routing pointer is reconciled separately from this packet.

The fold is deterministic and read-only. For the same ordered canonical record set it emits byte-for-byte identical JSON with root-class recurrence, repeated safeguard failures, recent exact-duration high-impact examples, owned/UNOWNED counts, and evidence/operator-impact coverage. Corrective-owner task status is deliberately `UNKNOWN` because current Asana task state is not part of the immutable escape record. `canonical_input_digest` binds that canonical input set so a report/snapshot from a different set is detectable. Report fields explicitly remain diagnostic-only and `UNKNOWN` for eligibility/routing/priority/Review/merge/human-gate authority.

Example commands:

```sh
python3 scripts/development_workflow_escape.py validate --record-json /path/to/escape.json
python3 scripts/development_workflow_escape.py render --record-json /path/to/escape.json
python3 scripts/development_workflow_escape.py fold --stories-json /path/to/asana-stories.json
```
