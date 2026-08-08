# Stage A command and authority contract

## Status and scope

**Implementation contract for retained Stage A commands — 5 August 2026.**

This inventory reconciles the current agent registry, private admin registry, PostgreSQL registry,
HTTP routes, CLIs, OpenAPI, clients, workflow handlers, tests, and approved product decisions. It is
not executable workflow metadata. Legal-state predicates remain owned by the authoritative workflow
and recovery services; registries may describe command dimensions but may not encode mutation logic.

Base archive SHA-256: `09a32bd6f42496de9a6a77b556a8d806a310a9de98e781b26b516e2a7a73377d`.
Synthetic baseline commit: `618ea622b150b4b2a5e367909dd13201a45ab206`.

## Shared command rules

### Classification

- **Query**: no durable workflow, audit-obligation, legal-action, recovery, or external-effect change.
- **Consequential**: any durable change, including evidence that changes legal actions. Every
  consequential command requires a permanently reserved request ID.
- `inspect` on the agent/verification surface is consequential because it appends inspection
  evidence and changes available actions.
- Human-readable `dish-admin` rendering is a transport view of the same canonical result; it is not a
  second command contract.

### Principal and run rules

| Principal | Requirement |
|---|---|
| reader/agent | authenticated agent family and service owner; a canonical run ID for every consequential command and every operation claim |
| verifier | agent principal plus independent verifier/run conditions from the authoritative snapshot |
| Marco/admin | authenticated private admin principal; commands representing Marco decisions must store Marco's exact words and normalized decision |
| worker | authenticated internal projection/reconciliation worker, generation/epoch fenced; never accepted from the Action surface |
| cutover operator | authenticated private control-plane principal with explicit Marco authorization for activation/admission actions |

Discussion, clarification, urgency, silence, continued conversation, an agent warning, or a planning
challenge/override is not Marco/admin authorization.

### Legal-state predicates

The table below references these predicates; they are service/database checks, not registry fields.

| Predicate | Exact test |
|---|---|
| `ADMITTED` | active authority generation and admission mode permits this exact request |
| `ACTION(x)` | `x` occurs in legal actions derived from one authoritative snapshot after all identity, placement, hold, effect, migration, lease, and fence checks |
| `NO_UNSETTLED_EFFECT` | all reachable external attempts are terminal `applied` or `not_applied` |
| `EXACT_OWNER` | operation/lease/claim/fence is owned by the authenticated principal and run |
| `EXACT_REVIEW` | displayed proposal/requirement ID and all bound proposal/candidate semantic digests still match |
| `SAFE_RECLAIM` | the single predicate defined in `stage-a-behavioral-contract.md` |
| `RESTORE_EXCLUSIVE` | maintenance/restore lock excludes ordinary requests and exact restore journal authority is current |
| `ROLLBACK_PREVIEW_CURRENT` | current version and selected prior snapshot/diff digest equal the confirmed preview |

### Replay and envelope

Every consequential command binds request ID to principal, run, command, target, canonical arguments,
and semantic payload digest. Exact completed replay returns the first authoritative result. Matching
pending/uncertain execution is not repeated. Mismatched reuse returns request identity conflict.

Except for the approved `create` migration, the stable JSON envelope remains:

```text
ok, command, code, task_gid, submission_id, state, retryable,
allowed_actions, data, errors
```

`task_gid` continues to mean an Asana task GID only. It must never contain a Dish UUID. New canonical
identity fields belong in `data` or an explicitly approved top-level envelope revision. A successful
canonical command remains successful when projection follow-up is pending or failed; projection
status and required next action are separate data.

## Agent and Action command inventory

| Command | Class; principal/run/request | Legal state | Exact canonical effect | External effect | Replay/result | Errors; next actions | Exposed surfaces | Stage A disposition |
|---|---|---|---|---|---|---|---|---|
| `create` | consequential; agent; run + request required | `ADMITTED`; active Research Queue registry | create canonical Dish, initial full content version/activation, authority head, project membership, Research Queue placement, not-completed event, audit/outcome, ordered projection intent | asynchronous Asana task creation; mapping may arrive later | required `dish_id`; optional configured `url`; optional `asana_task_gid`; never Dish UUID in `task_gid`; exact replay cannot duplicate Dish | title/registry/admission/request conflict; next `start` planning regardless of projection lag | CLI, private HTTP, GPT Action/OpenAPI | retain; intentionally migrate response |
| `sections` | query; reader; no request ID | active registry readable | return canonical/compatible Cooking section list and external aliases | none | normal envelope; no replay record | registry unavailable; next `section-tasks`/`start` | CLI, private HTTP, Action | retain compatibility query; target source must be PostgreSQL, not live Asana authority |
| `section-tasks` | query; reader; no request ID | section alias resolves; bounded cursor valid | return paginated canonical membership/placement projection for section | none | stable opaque cursor; no replay record | bad section/cursor; next page/read/start | CLI, private HTTP, Action | retain compatibility query; exact ordering/cursor source unresolved |
| `read` | query; reader; no request ID | Dish resolves by supported identity | return canonical current version, placement, completion, active operation, legal actions, projection/drift status | no mutation; reconciliation reads only when explicitly separate | normal envelope | not found, ambiguous alias, recovery required; next from authoritative actions | CLI, private HTTP, Action | retain; input identity migration must support canonical Dish URL/UUID without redefining `task_gid` |
| `proposals` | query; eligible agent; no request ID | approved unclaimed proposals visible to principal | list exact approved semantic bundles claimable by a later run | none | no replay record | queue unavailable; next `apply-proposal` | CLI, private HTTP, Action | retain; missing PostgreSQL implementation |
| `apply-proposal` | consequential; eligible agent; fresh run + request required | `ADMITTED`, `ACTION(apply-proposal)`, exact approved unclaimed proposal | atomically claim, verify baseline/digests, create exact approved canonical version/activation, consume application authority, close/reopen required verification state, audit/outcome/projection intent | Asana document projection | exact replay returns same version; no duplicate claim/version | stale/changed proposal, claim conflict, baseline drift, fence; next verify/recover | CLI, private HTTP, Action | retain; client must generate request ID; missing PostgreSQL semantic authority |
| `inspect` (agent) | **consequential**; verifier; run + request required | `ADMITTED`, `ACTION(inspect)`, exact cycle/operation and independent verifier | append immutable inspection occurrence/evidence and advance legal actions | none | exact replay returns same evidence/result | stale identity/cycle, independence, request conflict; next approve/reject | CLI, private HTTP, Action | retain and reclassify; current service/OpenAPI/runtime docs are wrong |
| `start` | consequential; agent; run + request required | `ADMITTED`; planning first call or `ACTION(start-kind)`; no conflicting operation/abandonment | planning first call records challenge only; confirmed planning or other kind opens exact operation, actor fact, lease/fence | none | exact replay stable | invalid kind, challenge mismatch, planning intent, active operation, stale successor; next prepare/inspect as returned | CLI, private HTTP, Action | retain; planning `agent_override` remains planning-only |
| `prepare` | consequential; exact owner; run + request required | `ADMITTED`, `ACTION(prepare)`, `EXACT_OWNER` | validate candidate, create/activate version, operation step/cycle/phase, audit/outcome, projection intents | Asana content update and queue move | exact replay; canonical success survives projection failure | validation, identity drift, lease/fence, uncertain projection; next inspect/recover | CLI, private HTTP, Action | retain |
| `approve` | consequential; independent verifier; run + request required | `ADMITTED`, `ACTION(approve)`, current inspection and cycle | record signoff; optional small correction creates exact corrected version; advance to submission; audit/outcome/projection intent | Asana content projection for signoff/correction | exact replay, no duplicate signoff/version | stale inspection/cycle, semantic/provenance incomplete, lease/fence; next submit/recover | CLI, private HTTP, Action | retain; not a Marco semantic-proposal approval command |
| `reject` | consequential; verifier; run + request required | `ADMITTED`, `ACTION(reject)` | route exact correction/evidence/Human Review; create correction version/hold/proposal as applicable; audit/outcome/projection intent | Asana content projection when candidate changes | exact replay | invalid route/reason/blocker evidence, stale cycle, fence; next new verification, Human Review, or recovery | CLI, private HTTP, Action | retain; Human Review route must use exact proposal/decision model |
| `submit` | consequential; exact owner; run + request required | `ADMITTED`, `ACTION(submit)`, signoff bound, destination valid | commit logical destination and operation completion; audit/outcome/projection intent | Asana move | exact replay; confirmed move never repeated | destination drift/failure/uncertain; next repair-destination/recover | CLI, private HTTP, Action | retain |
| `renew-lease` | consequential; exact owner; run + request required | `ADMITTED`, live lease, `EXACT_OWNER` | append lease renewal event/update expiry | none | exact replay | stale/terminal lease, wrong owner; next continue/inspect | Action/private HTTP client; no current CLI subparser | retain; surface inventory must remain explicit |

## Private admin command inventory

| Command | Class; principal/run/request | Legal state | Exact effect | External effect | Replay/result | Errors; next actions | Surfaces | Stage A disposition |
|---|---|---|---|---|---|---|---|---|
| `attention` | query; admin; no request | database readable | aggregate persisted operations, leases, abandonments, unresolved executions/recovery | none | human or JSON envelope | item-level unsafe diagnostics; next `inspect` | CLI, private HTTP | retain |
| `holds` | query; admin; no request | database readable | list exact open Evidence/Human Review holds and bound identities | none | no replay | none/stale display; next displayed resolution command | CLI, private HTTP | retain |
| `review-queue` | query; admin; no request | review store readable | list pending semantic proposals and Human Review requirements | none | no replay | none; next `review-inspect` | CLI, private HTTP | retain; missing PG registry/handler |
| `review-inspect` | query; admin; no request | exact review identity resolves | return immutable proposal/changes/evidence/question and current digests | none | no replay | not found/stale queue number; next approve/reject/revision | CLI, private HTTP | retain; missing PG registry/handler |
| `review-approve` | consequential Marco decision; admin request required | `EXACT_REVIEW`; proposal/requirement pending | store Marco exact words, normalized approve, proposal and candidate digests, actor/surface/request; no application | none | exact replay | stale digest/status, missing exact words; next `apply-proposal` or close hold | CLI, private HTTP | retain; PostgreSQL implementation required |
| `review-reject` | consequential Marco decision; admin request required | `EXACT_REVIEW`; pending | immutable reject or request-revision decision; no canonical edit; finding independent | none | exact replay | stale/status; next revised proposal/closure | CLI, private HTTP | retain; PostgreSQL implementation required |
| `inspect` (admin) | query; admin; no request | task/operation resolves | return current authority, ownership, holds, effects, exact safe actions/command templates | read-only Asana access only if separately specified; PostgreSQL target should not require live Asana for authority | no replay | ambiguity/recovery; next rendered command | CLI, private HTTP | retain; distinct from consequential agent `inspect` |
| `recover` | consequential; admin run + request | unresolved exact projection/effect and command-specific recovery predicate | observe/adjudicate/settle existing attempt; never repeat confirmed effect | read and possibly idempotent repair only after not-applied proof | exact replay | ambiguous/uncertain remains fail-closed; next retry projection or Marco | CLI, private HTTP | retain |
| `repair-destination` | consequential; admin run + request | exact failed destination evidence; content/signoff already committed | update only logical destination repair identity and projection intent; preserve approved content | Asana destination move | exact replay | destination invalid/stale, uncertain prior move; next submit/recover | CLI, private HTTP | retain |
| `discard` | consequential; admin request | operation provably unapplied and cancelable | terminalize operation as provably not applied; audit | none | exact replay | any applied/uncertain effect blocks; next none/reclaim | CLI, private HTTP | retain |
| `abandon-operation` | consequential; admin request | genuine recovery risk; exact source operation | create formal abandonment/frontier and fence source | recovery-specific only | exact replay | safe-reclaim alone is not reason; next reconcile-abandonment | CLI, private HTTP | retain for unsafe recovery, not normal lease expiry |
| `reconcile-abandonment` | consequential; admin request | exact active abandonment authority | settle frontier and create at most one linked successor; preserve/transfer eligible exact evidence | no duplicate effect | exact replay | ambiguous attempts/claims; next successor action | CLI, private HTTP | retain |
| `migrate` | consequential; admin request | task requires supported protocol/schema migration and no unsafe effect | create canonical migrated version/activation/operation evidence/projection intent | Asana document/move as specified | exact replay | incompatible release, drift, fence; next verify/recover | CLI, private HTTP | retain until disposition proves obsolete |
| `reopen-planning` | consequential; admin request | exact completed task/placement and no unsafe effect | clear completion for planning, open required planning state, projection intent | Asana completion/placement effect | exact replay with current recovery journal behavior | uncertain external effect; next recover/start | CLI, private HTTP | retain |
| `reopen` | consequential; admin request | exact held/resettable verification operation | reset verification cycle and activate resumed version | Asana document projection | exact replay | stale hold/cycle/effect; next verify | CLI, private HTTP | retain; must not be renamed semantically into whole-version rollback |
| `supply-evidence` | consequential Marco/admin input; request | exact open Evidence hold and bound identity | store supplied evidence, release hold, activate resumed version/state | Asana document projection if status/body changes | exact replay | stale hold/identity, blank detail; next prepare/verify | CLI, private HTTP | retain |
| `record-human-decision` | consequential Marco decision; request | exact open ordinary Human Review requirement | store exact decision words/normalization, release hold, activate resumed state; does not grant unrelated mutation authority | Asana document projection if status changes | exact replay | stale hold/identity; next workflow action | CLI, private HTTP | retain; align with shared approval evidence model |
| `resolved` | consequential; admin request | exact automatic two-pass verification hold | release hold and resume verification without signoff/approval | Asana document projection | exact replay | wrong hold kind/stale binding; next verify | CLI, private HTTP | retain |
| `authorize-governed-change` | consequential Marco authorization; admin run + request | exact operation/task/before/after semantic values | create scoped immutable authorization grant; no edit | none | exact replay | stale values, duplicate semantic identity, missing reason; next governed command | CLI, private HTTP | retain; must bind exact words/digests where used for semantic proposals |
| `recover-lease` | consequential; same durable run/admin request | exact lease belongs to same continuing run and recoverable execution | restore/release exact lease authority as current behavior specifies; no ownership transfer | none | exact replay | different run forbidden; next resume command | CLI, private HTTP | retain narrowly |
| `expire-lease` | consequential; admin request | exact lease/task resolves; expiry allowed | terminal lease event only; no workflow mutation or transfer | none | exact replay | ambiguity/stale lease; next safe-reclaim eligibility check | CLI, private HTTP | retain; cannot substitute for safe reclaim |
| `backup-create` | consequential operational admin; request required | healthy or supported diagnostic mode; destination reservation available | reserve backup identity, create validated PostgreSQL backup, record digest/size/schema/off-device-copy status | database/storage effect, not Asana | exact replay returns same backup; uncertain file durability blocks new destination | validation/storage/uncertain; next verify/copy | CLI, private backup HTTP | **retain**; current PG registry's historical/retired status contradicts approved requirements |
| `backup-restore` | consequential operational admin; request required | `RESTORE_EXCLUSIVE`; managed backup verifies | journal, restore into clean target, migrate/validate, atomically install, record result/fault recovery | database replacement; no Asana mutation | exact replay/restart resumes same restore; authority remains PostgreSQL | invalid backup, rollback unproven, restore fault; next repair/retry same request | CLI, private backup HTTP | **retain**; current PG registry's historical/retired status is wrong |

## Required Stage A admin additions

These product behaviors cannot be represented by an existing command without silently changing its
meaning. Exact public names remain an implementation naming decision; the command roles are required.

| Required role | Class/authority | Exact effect and state | Surface/disposition |
|---|---|---|---|
| safe-reclaim eligibility | query; admin | evaluate the single `SAFE_RECLAIM` predicate and return each failed clause, source owner, and proposed replacement lineage; no mutation | add to `dish-admin` inspect or a dedicated query before cutover |
| safe reclaim execution | consequential; eligible new agent/admin-mediated request | atomically create linked replacement operation, fence old owner, record succession/reason, transfer only still-valid exact approvals | add before cutover; must not call formal abandonment when safe |
| Marco verification override | consequential Marco decision | store exact words/scope/digests and move out of `needs verification` only when mechanical fences pass | add direct admin path and agent-transported path before cutover |
| whole-version rollback preview | query; Marco/admin | return selected prior full snapshot, current version, exact applied diff, retained history, and loss | add before cutover |
| whole-version rollback confirmation | consequential Marco/admin request | create new canonical full version from selected snapshot, parent=current, audit approval/rationale/diff, projection intent | add before cutover; exact command name unresolved |

## Non-surface/internal and retired inventory

| Name | Current evidence | Stage A disposition |
|---|---|---|
| `planning-intent-settlement` | retained in `dish_pg/command_contract.py`, command port, dark-launch policy, baseline tests; absent from current admin CLI/registry/HTTP | do not expose as a retained public command. Preserve planning challenge settlement as an internal effect of the exact `start` protocol, or prove a concrete production caller before retaining a private command. |
| `backup-create`, `backup-restore` as `retained=False` historical commands | PostgreSQL registry only | reject that disposition; operational backup/restore is a Stage A requirement. |
| topic commands `planning`, `research`, `verification` | CLI help walkthroughs only | not commands; no request, authority, or PostgreSQL handler. Keep as documentation aliases or remove independently. |

## Registry and surface derivation contract

One canonical inventory may generate names and static dimensions for:

- Action/OpenAPI path list and input schemas;
- private admin path list;
- CLI parser command existence;
- request-ID requirement checks;
- query versus consequential classification;
- principal class and exposed surfaces.

It must **not** contain legal-state transitions, mutation steps, SQL, projection settlement, or next
workflow actions. Those remain in service/use-case code and the behavioral contract. Tests must use
an independent literal expected inventory so changing generator and registry together cannot hide a
contract break.

## Complete current inventory reconciliation

| Source | Commands present but missing elsewhere |
|---|---|
| `dish_service/command_spec.py` Action registry | includes `proposals`; excludes admin; classifies replay only for create/start/prepare/approve/reject/submit/apply-proposal/renew-lease, incorrectly excluding `inspect` |
| `dish_tool/admin_command_spec.py` | contains review commands and backup commands omitted or contradicted by PostgreSQL registry |
| `dish_pg/command_contract.py` | omits `proposals`, all review commands, and admin inspect; includes internal `planning-intent-settlement`; marks backup commands retired |
| `dish_pg/command_port.py` | has no semantic proposal/review handlers; has internal planning settlement |
| `dish_shadow/policy.py` | explicitly records semantic proposal/review gap as excluded or capture-only |
| agent CLI | no `renew-lease` subparser although Action/client exposes it |

## Exact unresolved implementation facts

1. Exact names and route shapes for safe reclaim, Marco override, and rollback commands are not set.
2. Whether `sections`/`section-tasks` remain long-term or only cutover compatibility queries is not
   decided; Stage A must retain current semantics through general admission.
3. Canonical `read`/`start` identity acceptance for Dish UUID and configured URL is not implemented.
4. The PostgreSQL result-envelope adapter from command-port `task_id` to public `dish_id` is not present.
5. `renew-lease` CLI exposure is inconsistent; product requirement for a CLI subcommand is not stated.
6. The exact PostgreSQL backup implementation and off-device-copy command boundary are not present in
   the current PostgreSQL command port.
7. Current admin commands do not uniformly require request IDs at their direct application boundary;
   Stage A transport/service must enforce the consequential classification above.
8. Exact legal post-rollback action/phase remains unresolved and must be approved with target schema.

## Contradictions found

- Agent `inspect` is durable but current service/runtime contract treats it as read-only.
- The bundled client generates request IDs for create/start/prepare/approve/reject/submit and
  renew-lease, but not `apply-proposal` in one client check, despite the approved replay requirement.
- PostgreSQL retained inventory cannot be complete while semantic proposal/review commands are absent.
- PostgreSQL marks backup commands retired while current service exposes and heavily tests them and
  the approved cutover requires verified backup/restore.
- `planning-intent-settlement` is represented as a retained admin command without a current external
  admin producer, risking command metadata becoming a workflow DSL.
- Current `reopen` semantics are verification reset and cannot silently become rollback.

## Repository searches used

```text
rg -n 'AGENT_MUTATION_COMMANDS|ACTION_COMMANDS|REPLAY_SAFE_COMMANDS' dish_service/command_spec.py
rg -n 'ADMIN_COMMAND_SPECS|AdminCommandSpec' dish_tool/admin_command_spec.py
rg -n 'COMMAND_DEFINITIONS|RETAINED_COMMANDS|RETIRED_COMMANDS' dish_pg/command_contract.py
rg -n 'add_parser\(' dish_service/cli.py dish_service/admin_cli.py
rg -n 'command ==|command in \{' dish_service/application.py dish_service/http.py
rg -n 'handlers =|COMMAND_NOT_PORTED' dish_pg/command_port.py
rg -n 'legal_actions|ACTION\(' dish_tool/workflow_policy.py dish_pg
rg -n 'review-(queue|inspect|approve|reject)|proposals|apply-proposal' .
rg -n 'backup-(create|restore)|planning-intent-settlement' .
rg -n '/v1/action|/v1/admin' dish_service/http_routing.py openapi
```

## Acceptance checklist

- [x] Every current agent, admin, internal-target, and backup command is accounted for.
- [x] Every retained consequential command has principal, run, request, replay, effect, error, surface,
  and next-action treatment.
- [x] Query versus consequential classification is explicit; agent `inspect` is consequential.
- [x] Semantic approval and application remain separate.
- [x] Planning challenge/override has no mutation-authority effect.
- [x] Command metadata is limited to static dimensions and is not a workflow DSL.
- [x] Existing command meanings are preserved; `reopen` is not silently changed to rollback.
- [x] Backup/restore and missing Human Review commands are not lost through registry consolidation.

## Self-review

- Reconciled all three current registries rather than treating one as authoritative.
- Kept legal-state mechanics in named predicates and the behavioral contract.
- Marked exact new command names unresolved while proving why new command roles are required.
- Checked replay behavior for every consequential command and no-replay behavior for every query.
- Checked all external effects preserve intent-before-effect and applied/not-applied/uncertain settlement.
