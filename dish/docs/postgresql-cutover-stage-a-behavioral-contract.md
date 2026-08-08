# Stage A behavioral contract

## Status and authority

**Implementation contract for the approved PostgreSQL Stage A design — 5 August 2026.**

This document specifies behavior, not production code. It is subordinate to Marco's explicit
product decisions in `dish/docs/postgresql-cutover.md` and sequencing in
`dish/docs/postgresql-cutover-imp.md`. Where the current repository cannot prove a concrete target
schema mapping, this document marks the fact unresolved instead of assigning an invented table or
state.

Received base identity:

- archive: `ai-tools-venv(20260805-195845).tgz`;
- archive SHA-256: `09a32bd6f42496de9a6a77b556a8d806a310a9de98e781b26b516e2a7a73377d`;
- the archive contained no Git metadata;
- synthetic local baseline commit used only to produce reviewable patches:
  `618ea622b150b4b2a5e367909dd13201a45ab206`.

## Contract vocabulary

The contract uses existing durable concepts rather than defining a workflow DSL.

| Term | Mechanical meaning |
|---|---|
| canonical transaction | One PostgreSQL transaction containing the command execution, canonical mutation, audit, authoritative outcome, and required projection intent. |
| consequential command | A command that may change durable state, legal actions, an external effect, recovery state, or immutable evidence. It requires a request ID. `inspect` is consequential. |
| exact request match | Stored principal, run ID, command name, canonical argument digest, target identity, and semantic payload digest all equal the incoming values. |
| terminal external settlement | Every external attempt reachable from the operation or request is durably `applied` or `not_applied`; none is pending, running, or uncertain. |
| no incomplete execution | No reachable command execution, proposal application, settlement, or projection attempt is pending, claimed, running, or uncertain. |
| safe reclaim predicate | `no incomplete execution` AND `terminal external settlement` AND no live lease/claim by another owner AND no incomplete proposal/application/settlement step. The replacement insert and old-owner fence occur atomically. |
| semantic digest | Digest over the canonical semantic fields covered by review. Formatting-only or unrelated metadata is outside the digest only when the schema explicitly classifies it so. |
| mechanical fence | A database constraint, row lock, generation/epoch check, ownership token, or authenticated external-writer fence. Agent concern is never a mechanical fence. |
| authoritative success | The first committed `service_request_outcomes` success for the exact request, even when later response delivery, projection, or observation fails. |

Current table names used where already present include `authority_generations`, `authority_activations`,
`service_requests`, `service_request_outcomes`, `command_executions`, `workflow_operations`,
`service_leases`, `operation_execution_fences`, `planning_intent_challenges`,
`human_review_requirements`, `human_review_decisions`, `abandonment_attempts`,
`operation_succession_edges`, `governed_audit_events`, `projection_outbox_events`,
`projection_attempts`, `projection_observations`, `projection_adjudications`,
`projection_drift_events`, `projection_reconciliation_runs`, `projection_reconciliation_items`,
`first_admission_plans`, and `mutation_admission_controls`.

PostgreSQL does not currently contain the approved semantic-proposal approval model, a safe-reclaim
predicate/command, direct Marco override evidence, or whole-version rollback. Their required rows are
specified logically below; the target-schema document must map them to concrete tables without
changing these transitions.

## Shared admission and execution rules

1. Only the active PostgreSQL authority generation may accept live commands.
2. Dark-launch envelopes use a separate shadow entry point and never satisfy live admission.
3. Admission is `closed`, `exact_request`, or `open`; the default is `closed`.
4. `exact_request` admits only the reserved request ID, principal/run binding, command, target, and
   canonical payload digest recorded by the first-admission plan.
5. Every consequential command reserves its request ID before execution. Request IDs are never
   deleted or reusable.
6. A duplicate request ID with a non-exact binding fails before command dispatch.
7. A matching completed request returns the stored first authoritative outcome byte-for-byte except
   for transport-added replay metadata.
8. A matching pending or uncertain request is not executed again. Recovery must first establish a
   terminal authoritative outcome or a specific, evidence-backed continuation.
9. Agent warnings may be recorded as findings. They cannot remove a legal action or block a command
   unless a database integrity or recovery predicate fails.
10. Planning challenge or planning override authorizes only the exact planning start. It never
    creates Human Review approval, semantic-change approval, governed mutation authority, or rollback
    authority.

## Transition catalogue

Each transition is normative. “Rows” names the minimum durable facts; implementations may normalize
storage, but may not weaken immutability, binding, or atomicity.

### A. Authority and admission

| ID | Source state | Action and actor | Authority/request requirements | Mechanically checkable preconditions | Rows created or changed; immutable evidence | External effects and settlement | Replay, errors, next legal actions, required tests |
|---|---|---|---|---|---|---|---|
| A1 | no active PostgreSQL authority generation | bootstrap generation; deployment administrator | private control-plane principal; no live command request | clean target; schema head installed; one complete import; no other active generation | create pending `authority_generations`, binding, registry, import evidence; append bootstrap audit | none | duplicate generation identity is rejected; next: validate/activate. Test fresh database, duplicate bootstrap, incomplete import. |
| A2 | validated pending generation; live legacy authority | activate PostgreSQL authority; Marco-approved cutover operator | authenticated admin request ID permanently reserved | verified backup and clean restore; final import/reconciliation complete; writer fence verified; admission currently closed; no other active generation | append `authority_activations`; mark generation active; create/confirm `mutation_admission_controls=closed`; immutable artifact/source digests and activation actor | legacy writer fence is already engaged and verified; no Dish mutation yet | exact replay returns activation result; mismatch conflicts. Authority is irreversible after first accepted PostgreSQL mutation. Next: reserve first request. Test ordering constraints and concurrent activation. |
| A3 | active generation; admission `closed` | reserve first request and switch to `exact_request`; cutover operator | admin request ID plus reserved live request identity | first-admission plan absent; command is retained and consequential; exact principal/run/target/payload digest known | create immutable `first_admission_plans`; update admission row to `exact_request`; append audit | none | exact replay returns reservation. Any different live request receives admission-closed error. Next: execute only reserved request. Test digest/identity mismatch and database constraint against open admission. |
| A4 | `exact_request`; reserved request unconsumed | execute first live command; reserved principal | exact reserved request ID and binding | all ordinary command preconditions; first plan matches; no previous request outcome | ordinary command transaction plus first-request consumption evidence | command-specific effects | normal exact replay. Failure before canonical commit leaves request pending/not-applied as proven; failure after canonical commit follows committed-success rule. Next: verify replay, audit, projection, and reread. Test all crash points. |
| A5 | reserved request has authoritative success, exact replay succeeds, projection settled applied, independent Asana reread matches | mark first admission verified; cutover operator | consequential admin request ID | all checks refer to same request, generation, command execution, projection event/attempt, and observed external identity | append verification checkpoint; transition cutover run to first-admission-verified | no new external mutation | replay stable. Next: open general admission. Test mismatched evidence cannot verify. |
| A6 | first admission verified | set admission `open`; cutover operator | consequential admin request ID | cutover state first-admission-verified; active generation unchanged; writer fence still verified | update `mutation_admission_controls=open`; append audit | none | exact replay stable; next: retained live commands. Test DB rejects open before A5. |
| A7 | any active generation during maintenance/recovery | set admission `closed`; authorized operator or automatic fail-closed startup | request ID for operator action; automatic startup closure uses durable recovery event identity | restore fault, unresolved first request, writer-fence failure, or operator maintenance action | update admission to closed; append reason/evidence | none | closing is idempotent. Next depends on specific recovery. Test that closure cannot erase committed success or request reservations. |

### B. Request identity and replay

| ID | Source state | Action and actor | Authority/request requirements | Preconditions | Rows/evidence | External effects | Replay, errors, next actions, tests |
|---|---|---|---|---|---|---|---|
| R1 | request ID absent | reserve consequential request; authenticated caller | canonical UUID; principal, run, command, target, canonical arguments, semantic payload digest | admission permits request; active run/principal; command exists and is retained | insert immutable `service_requests` binding and pending `command_executions`; append claim/audit evidence | none before reservation | conflict on reused ID; next: claim/execute. Test concurrent identical and mismatched reservations. |
| R2 | exact pending execution, unclaimed or stale claim safely takeable | claim and execute; eligible service worker | same request binding | claim fence succeeds; target/operation fences match; no unresolved prior external effect | claim event, execution status/fence update | command-specific | transport loss never authorizes a new ID retry. Test stale worker rejection and takeover. |
| R3 | command transaction reaches authoritative success | commit result; service | same request ID | all canonical rows, audit, outcome, and required projection intents valid in one transaction | insert immutable success `service_request_outcomes`; terminalize execution | projection runs later | any later exact replay returns this success even if projection fails. Test response loss immediately after commit. |
| R4 | deterministic expected failure before any effect | commit expected failure; service | same request ID | failure classification is deterministic and no external/canonical effect occurred | immutable failure outcome and audit | none | exact replay returns same failure. Corrected input requires a fresh request ID. Test validation and authorization failures. |
| R5 | effect may have occurred but terminal settlement absent | mark uncertain/fail closed; service/recovery | original request only | durable attempt exists; observation cannot prove applied or not-applied | uncertainty resolution row or unresolved execution state with exact attempt/evidence | no repeat | matching replay returns pending/uncertain response; mismatched ID conflicts. Next: command-specific recovery/reconciliation. Test lost response and ambiguous observation. |
| R6 | exact request has terminal outcome | replay; any caller with same principal/run binding | exact match | stored outcome exists | no canonical change; append optional replay observation only | none | return first outcome plus `data.request_replayed=true` and request ID. Test byte-stable payload and restart durability. |
| R7 | request ID exists with different binding | reject reuse; service | any caller | one or more binding fields differ | append security/audit rejection without changing original request | none | `service_request_identity_conflict`; no next mutation under that ID. Test each binding dimension. |
| R8 | old bulky details eligible for archival | archive non-authoritative request detail; operator job | no command authority | terminal outcome exists; core identity/outcome/audit retained | move only bulky payload/response/diagnostic material; retain permanent request tombstone and digests | none | replay must still return an authoritative response or stable archival pointer contract. Concrete archival mechanism is deferred. Test ID remains reserved. |

### C. Human Review and semantic proposals

The approved model requires distinct finding, proposal, Marco decision, and application records.
Current PostgreSQL has Human Review requirement/decision tables but no semantic proposal authority;
Stage A must add a canonical equivalent of the current immutable `semantic_proposals` and
`semantic_proposal_changes` behavior plus exact approval bindings.

| ID | Source state | Action/actor | Authority/request requirements | Preconditions | Rows/evidence | External effects | Replay, errors, next actions, tests |
|---|---|---|---|---|---|---|---|
| H1 | canonical candidate under inspection | record finding/evidence; eligible agent | consequential `inspect` request ID | exact task/version/cycle; independent verifier requirements; no unresolved effect | immutable inspection occurrence and finding/evidence rows; confidence/source recorded separately from proposal | none | replay returns same occurrence/result. Next: approve, reject route, or propose correction as legal. Test inspect changes legal actions and is replay-bound. |
| H2 | finding exists; exact baseline/candidate known | create semantic proposal/open question; eligible agent | consequential reject/proposal request ID | proposal semantic digest and candidate-version digest computed; no active conflicting proposal | immutable proposal core, ordered field changes, explanation/rationale, linked finding, proposer/run, baseline and candidate IDs/digests; open Human Review requirement when Marco input is needed | none | exact replay returns same proposal. Changed content is a different proposal and request. Next: review-inspect/approve/reject/revision. |
| H3 | pending proposal/requirement | answer, reject, request revision, or approve; Marco via admin surface | consequential admin request ID; actor must be Marco-authorized principal | displayed proposal/requirement identity still current; submitted exact words non-blank | immutable decision stores Marco's exact words, normalized decision, proposal ID/digest, candidate-version ID/digest, actor, surface, request ID, timestamp; agent interpretation/rationale separate | none | exact replay stable. Stale digest -> conflict, no decision. Approval creates no canonical edit. Next: claim application, submit revision, or close. Test exact words and digest mismatch. |
| H4 | approved proposal unclaimed | claim application; later eligible agent | `apply-proposal` request ID and fresh run | approval normalized `approve`; exact proposal and candidate digests still match; baseline is still the approved baseline; no competing claim | claim/fence row tied to proposal, applying run, request, and operation | none before canonical transaction | concurrent claim loser conflicts. Next: apply exact proposal. Test later-run eligibility and original-run independence. |
| H5 | approved, claimed exact proposal | apply; claiming agent/service | original apply request ID | claim owned; approval and digests unchanged; current canonical baseline matches; all normal mechanical fences pass | new immutable content version/activation, exact approved diff, consumed approval/application evidence, audit, outcome, projection intent; proposal terminal `applied` | projection update; settled asynchronously | exact replay returns success; changed candidate cannot apply. Next: fresh verification cycle as required by current workflow. Test atomicity and no duplicate version. |
| H6 | pending proposal | request revision; Marco | admin request ID | exact proposal displayed | decision `request_revision`; original proposal remains immutable/terminal for application; finding remains open unless separately closed | none | revised semantics require a new proposal and new approval. Test old approval cannot migrate. |
| H7 | pending proposal | reject; Marco | admin request ID | exact proposal displayed | rejection decision; proposal terminal rejected; underlying finding remains independently open/closed according to its own evidence | none | same semantic bundle cannot be silently requeued unchanged under current policy. Next: different proposal, finding closure, or no change. Test rejection does not erase finding. |
| H8 | open finding/requirement after decision or application | close finding/review item; authorized workflow/admin action | consequential request ID | closure reason and linked terminal decision/application exist | append closure event; do not mutate historical proposal/decision | none | replay stable. Test closure cannot imply application. |

### D. Lease expiry and safe reclaim

| ID | Source state | Action/actor | Requirements | Preconditions | Rows/evidence | External effects | Replay, errors, next actions, tests |
|---|---|---|---|---|---|---|---|
| L1 | eligible unleased operation | acquire lease/claim; eligible agent run | request/run identity where command is consequential | operation open; no active conflicting abandonment; ownership fence current | `service_leases` plus lease event and operation/actor fact | none | duplicate same-owner acquisition idempotent only when exact; other owner conflict. |
| L2 | active lease | renew; same owner/run | `renew-lease` request ID | exact live lease and operation fence | lease event/expiry update; outcome | none | exact replay stable; late/stale owner rejected. |
| L3 | active lease | expire naturally or explicit release/termination; clock/service or admin | admin termination is consequential; natural expiry is derived from committed time/row | exact lease identity; termination does not alter workflow facts | terminal lease event | none | does not itself transfer ownership or abandon work. Next: evaluate safe reclaim predicate. |
| L4 | inactive owner; safe reclaim predicate true | safe reclaim; new eligible agent/admin-mediated service | consequential request ID, new run, exact source operation | one locked transaction proves every clause of safe reclaim predicate and source fence version | create linked replacement `workflow_operation`; append `operation_succession_edges`; atomically terminalize/fence source ownership; transfer still-valid exact approvals only where their bound proposal/candidate digests remain unchanged; audit old/new owner and reason | none | replay returns same replacement. Any late source-owner write fails fence. Next: restart research/verification/planning step on replacement. Tests for every false predicate and race. |
| L5 | inactive owner; safe reclaim predicate false due pending/uncertain/incomplete effect | request reclaim | same as L4 | predicate false | no replacement; append diagnostic audit only | none | error enumerates the failed database clauses; next: recover/settle or formal abandonment. Test one reason per clause. |
| L6 | genuine recovery risk | formal abandonment; Marco/admin | consequential admin request ID | exact source operation; unsafe frontier classified from durable evidence | `abandonment_attempts`, immutable frontier/evidence, source fence | command-specific recovery only; no blind repeat | replay stable. Next: reconcile abandonment. |
| L7 | abandonment ready for settlement | formal succession/reconciliation; admin/service | consequential request ID | exact abandonment authority, terminal external settlement or explicit uncertain disposition, no competing successor | terminal abandonment state, linked successor edge when continuation exists, old-owner fence | no duplicate effects | replay stable; late writes rejected. Tests crash/restart and single successor. |

The target schema must expose one callable/SQL predicate for L4/L5. Duplicating its clauses in service,
`dish-admin`, and tests is prohibited.

### E. Marco override

| ID | Source state | Action/actor | Requirements | Preconditions | Rows/evidence | External effects | Replay, errors, next actions, tests |
|---|---|---|---|---|---|---|---|
| O1 | concern or `needs verification` state; no mechanical recovery block | direct override through `dish-admin`; Marco | consequential admin request ID; exact displayed concern/task/version | concern identity and candidate version current; one concise consequence description available; no pending/uncertain effect or integrity failure | immutable override evidence: Marco exact words, normalized override scope, concern identity, task/version/candidate digests, actor, surface, request ID, timestamp; separate agent warning/interpretation | canonical transition only if command explicitly requests it; projection intent atomic with transition | replay stable. Next legal actions are derived from resulting authoritative snapshot. Test override cannot bypass DB fences. |
| O2 | same | override communicated through agent; Marco is speaker, agent transports | consequential request ID; exact Marco words included; agent principal cannot self-assert Marco | explicit words unambiguously identify override and scope; agent has issued at most the required concise warning; same mechanical checks as O1 | same override evidence plus transporting agent/run; exact words separate from normalization | same as O1 | silence, urgency, continued conversation, or agent paraphrase fails `authorization_required`. Test each non-authorization input. |
| O3 | concern previously overridden | raise concern again | agent finding request if evidence is recorded | materially new evidence digest or Marco explicit reopen request | new finding linked to prior override; prior override immutable | none | repeated same evidence cannot hard-block or reopen. Test semantic equality and new evidence. |

Override ends repeated challenge for its recorded concern. It never grants mutation authority outside
the explicitly selected override transition and never converts a planning challenge/override into
Human Review or semantic-change approval.

### F. Whole-version rollback

| ID | Source state | Action/actor | Requirements | Preconditions | Rows/evidence | External effects | Replay, errors, next actions, tests |
|---|---|---|---|---|---|---|---|
| B1 | canonical task has current version and selectable prior full snapshot | preview rollback; admin query | Marco/admin principal; no request ID required if strictly query | selected prior version belongs to same task and is immutable | no mutation; return current/prior IDs, exact forward diff that would be applied, approvals/history retained, and data lost from current view | none | no replay record. Invalid version -> not found/conflict. Next: confirmed rollback. |
| B2 | valid preview | confirm rollback; Marco/admin | consequential request ID plus explicit confirmation binding preview/current/prior/diff digest and rationale | current version still equals preview source; selected snapshot digest unchanged; no pending/uncertain task effect; authority/admission and fences pass | create new full `task_content_versions` row copied from selected snapshot, parent=current version, exact rollback diff/rationale/approving authority, new activation/head revision, audit/outcome/projection intent; never update/delete old versions | projection update | exact replay returns same new version. Stale preview conflicts. Next: normal verification/submission state defined by rollback command contract. Test no duplicate version and history immutability. |
| B3 | rollback canonical commit succeeded; projection failed/uncertain | recover projection | admin recovery command/request | authoritative success exists; exact projection event/attempt | only projection settlement/observation changes | retry idempotently only after `not_applied`; never repeat canonical rollback | replay of rollback remains success. Test committed-success boundary. |

Concrete command name and exact post-rollback workflow phase are unresolved repository facts. Stage A
must add an admin preview and explicit confirmation surface before cutover; it must not overload
`reopen` because current `reopen` means verification reset, not whole-version rollback.

### G. Projection and reconciliation

| ID | Source state | Action/actor | Requirements | Preconditions | Rows/evidence | External effects | Replay, errors, next actions, tests |
|---|---|---|---|---|---|---|---|
| P1 | canonical mutation planned | commit canonical mutation and projection intent; service | command request ID | command-specific preconditions | canonical rows, audit/outcome, ordered `projection_outbox_events` in same transaction; generation and epoch recorded | none before commit | rollback of transaction leaves neither canonical mutation nor projection intent. Test atomic failure. |
| P2 | pending projection event | claim attempt; projection worker | worker identity; no user request ID | live-origin event, active generation/epoch, no terminal applied attempt | create durable `projection_attempts` and claim/fence | none yet | stale worker rejected. Shadow-origin events cannot be claimed for external mutation. |
| P3 | claimed attempt | perform Asana effect then observe; projection worker | same attempt/fence | exact mapping and expected before/after identity | record pre-effect intent, external response metadata, independent reread `projection_observations` | Asana create/update/move/completion effect | settle `applied`, `not_applied`, or `uncertain`; never infer success from HTTP response alone. Test response loss and reread mismatch. |
| P4 | attempt `not_applied` | retry delivery; worker | same projection event, new attempt identity | observation proves effect absent and epoch current | new attempt linked to event | idempotent effect | canonical command is not rerun. Test repeated retries. |
| P5 | attempt `uncertain` | adjudicate/reconcile; admin/worker | recovery request ID when consequential | exact expected/observed identity and attempt chain | adjudication and final settlement; drift event if mismatch | read first; repair only through explicit command | no blind retry. Next: repair, mark applied, or remain blocked. |
| P6 | canonical Dish created; no Asana mapping yet | continue normal canonical success | none beyond create request | create canonical transaction committed | mapping absent; projection event pending/failed | Asana task may not exist yet | `asana_task_gid` remains absent; create replay remains success. Next: projection/reconciliation. Test creation independent of Asana outage. |
| P7 | scheduled/manual reconciliation | compare canonical state to Asana; reconciliation worker/admin | run identity; request ID only if repair is included | complete scoped fetch; generation/epoch fixed | reconciliation run/items, observations, drift events, completion evidence | reads only for compare | partial fetch cannot report complete. Next: no action or explicit repair command. Test missing/duplicate/unknown/mismatch. |
| P8 | drift proven and repair authorized | repair projection; admin/worker | consequential repair request ID | drift item current; expected canonical version unchanged; no unresolved attempt | repair projection event/attempt/observation/adjudication | idempotent Asana effect | exact replay stable; canonical content unchanged. Test stale drift rejection. |

### H. Committed success stays success

| ID | Source state | Action/actor | Requirements | Preconditions | Rows/evidence | External effects | Replay, errors, next actions, tests |
|---|---|---|---|---|---|---|---|
| S1 | canonical transaction committed success; response not delivered | retry/replay; original caller | exact original request ID/binding | success outcome exists | no canonical change | none | return success with replay marker. Never return failure because transport delivery failed. |
| S2 | canonical success; projection pending/failed/uncertain | read/replay command | exact request or normal query | success outcome exists | expose projection follow-up state separately | projection recovery may remain | command result remains success; `retryable` applies only to projection follow-up, not command rerun. |
| S3 | canonical success; later reconciliation detects drift | report drift | query/reconciliation identity | drift evidence current | drift/reconciliation rows | optional explicit repair later | do not rewrite first outcome. Next: repair projection or Marco decision. |
| S4 | expected failure committed before effects | exact replay | original request | failure outcome exists and no later authoritative success | no change | none | failure remains first outcome. A corrected command uses a new request ID. |

Every result envelope must distinguish `command_outcome` from `projection_status`/`required_admin_action`
so clients cannot turn a successful canonical mutation into an unsafe retry.

### I. Cutover first request and failure recovery

| ID | Source state | Action/actor | Requirements | Preconditions | Rows/evidence | External effects | Replay, errors, next actions, tests |
|---|---|---|---|---|---|---|---|
| C1 | authority active; admission exact request; first request not committed | first request fails before canonical commit and all effects proven not applied | service/recovery | reserved request ID | terminal not-applied/expected failure evidence or safe same-request continuation | none/proven absent | remain maintenance/exact-request. Repair cause and retry same ID only when contract says continuation; never open admission. |
| C2 | first request canonical success; projection pending or failed | verify/recover | cutover operator/worker | exact success outcome | preserve success; projection recovery evidence | projection only | remain maintenance/exact-request until projection applied and reread matches. Replay same request returns success. |
| C3 | first request effect uncertain | investigate/adjudicate | admin recovery request plus original request identity | exact attempt and expected identity | uncertainty resolution/adjudication | read before any repair | remain maintenance. No new first request and no general admission. |
| C4 | authority activated but PostgreSQL health/restore fault occurs | close admission and recover PostgreSQL | operator/startup | durable fault/health evidence | maintenance/fault events; restore/recovery evidence | no authority reversion to SQLite | after first accepted PG mutation, repair/restore/forward-fix PostgreSQL only. Test legacy writer remains fenced. |
| C5 | first request fully verified | open admission | operator | A5 satisfied | A6 rows | none | general live operation begins. |

## Error taxonomy required by this contract

Exact public codes may reuse the current envelope, but these distinctions must remain machine-readable:

- admission closed or wrong exact request;
- request identity conflict;
- request pending;
- request/external outcome uncertain;
- stale owner, lease, claim, generation, epoch, version, proposal, approval, or preview;
- authorization required;
- mechanical integrity/recovery fence;
- external effect not applied versus uncertain;
- projection drift/reconciliation incomplete;
- command succeeded with projection follow-up required.

Agent concern alone is not an error category that may remove a legal action.

## Required test matrix

At minimum, each transition table row requires:

- happy path and exact replay;
- duplicate request ID with each mismatched binding dimension;
- crash before reservation, after reservation, before canonical commit, after canonical commit, before
  external effect, after effect before observation, and after observation before settlement;
- concurrent claims and stale-owner writes;
- active versus stale generation/epoch;
- semantic digest mutation after approval;
- safe-reclaim predicate false for each individual clause;
- projection applied/not-applied/uncertain outcomes;
- first-admission ordering constraints;
- agent warning versus database fence separation.

The applicable repository lanes are source-contract/document checks plus schema/contract tests selected
by `dish/scripts/dish-test-plan`; native PostgreSQL is mandatory when implementation later changes
locks, triggers, row-level concurrency, admission ordering, projection claiming, or migrations.

## Repository searches used

Run from the extracted repository or `dish/` as appropriate:

```text
find . -type d -name .git -prune -print
sha256sum /mnt/data/ai-tools-venv(20260805-195845).tgz
find docs dish/docs -type f | grep -Ei 'postgres|cutover|decision|implement|plan|testing|runtime'
rg -n 'COMMAND|CommandSpec|ADMIN_COMMAND_SPECS|COMMAND_DEFINITIONS|ACTION_COMMANDS'
rg -n 'add_parser\(' dish_service/cli.py dish_service/admin_cli.py
rg -n 'command ==|command in \{' dish_service dish_pg
rg -n 'planning-intent-settlement|review-(queue|inspect|approve|reject)|apply-proposal'
rg -n 'backup-(create|restore)|backup_create|backup_restore'
rg -n 'dish_id|asana_task_gid|task_gid|create' dish_tool dish_service dish_pg openapi deploy frontend scripts tests docs
rg -n 'legal_actions|phase_candidate_actions|allowed_actions' dish_tool dish_pg
rg -n 'CREATE TABLE semantic_|semantic_proposal_' dish_tool/database_schema.py dish_tool/semantic_proposals.py
```

## Exact unresolved implementation facts

1. The target reduced PostgreSQL schema has not been approved, so semantic proposal, approval,
   override, safe-reclaim, and rollback table names are not yet concrete.
2. No retained command currently implements safe reclaim as the approved linked replacement operation.
3. No current command is a whole-version rollback preview/confirm command; current `reopen` has a
   different verification-reset meaning.
4. The exact post-rollback workflow phase and required new verification treatment are not settled in
   code or approved docs.
5. The current PostgreSQL Human Review decision row does not store the complete approved binding:
   Marco exact words plus normalized decision plus proposal and candidate semantic digests.
6. The canonical boundary between formatting-only changes and semantic changes needs a schema-owned
   digest definition; current docs approve the rule but code does not implement it in PostgreSQL.
7. The mechanism that proves an agent-transported statement is Marco's exact words is not implemented.
8. Current first-admission/release machinery is larger than the approved minimal model; which existing
   tables survive schema reduction is a separate target-schema decision.

## Contradictions found in current code or docs

- `dish_service/command_spec.py` classifies `inspect` outside `REPLAY_SAFE_COMMANDS`, while the approved
  contract and PostgreSQL registry classify it consequential and replay-required.
- `dish/docs/runtime-contract.md` still states `inspect` is read-only and does not accept request
  identity, contradicting the approved cutover design.
- PostgreSQL has Human Review requirement/decision rows but no semantic proposal/review/application
  authority; dark-launch policy therefore capture-only/excludes those commands.
- Current abandonment/succession is not the approved normal safe-reclaim path and must not be reused
  merely because a lease ended.
- Existing planning `agent_override` is planning-intent settlement, not Marco mutation override; it
  must not satisfy O1/O2 or H3.

## Acceptance checklist

- [x] Authority and admission transitions are database-ordered and mechanically checkable.
- [x] Every consequential transition requires permanent request identity.
- [x] Approval binds exact proposal and candidate semantic digests and cannot follow changed content.
- [x] Safe reclaim has one explicit database predicate and creates a linked replacement operation.
- [x] Agent warnings are separated from database execution fences.
- [x] Planning challenge/override cannot grant mutation authority.
- [x] Whole-version rollback creates a new immutable canonical version.
- [x] Projection failure cannot reverse canonical success.
- [x] First-request failure remains in maintenance and reuses the same request identity.
- [x] Deferred light-verification and proceed-now work is not reopened.

## Self-review

- Removed duplicate state narration by centralizing shared predicates.
- Used current table/state names only where repository evidence exists.
- Marked missing PostgreSQL capabilities and unresolved mappings rather than guessing.
- Checked every transition for actor, request identity, preconditions, rows, evidence, external
  settlement, replay, errors, next actions, and tests.
- Checked that no agent judgment is a database precondition or hard block.
