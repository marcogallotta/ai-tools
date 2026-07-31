# SUPERSEDED DESIGN — DO NOT CONTINUE OR IMPLEMENT

> **Human decision, 31 July 2026:** Development of the trusted connected-session and
> operation-authority-assignment model in Part II has been stopped. Sections 1–11 are an
> incomplete, superseded design exploration and are not an approved basis for implementation,
> further refinement, or follow-on design work.
>
> Do not resume, repair, extend, review, or implement Part II unless Marco explicitly overrides
> this instruction and asks for this specific design to be reopened.
>
> The inability of the current GPT Action to provide an authenticated per-chat identity is
> acknowledged and accepted for now. A cooperative one-time agent/session identifier may be used
> to reduce accidental identity changes and support same-session lease reclaim, but it is not
> trusted security identity and must not be represented as such.
>
> **Current direction:**
>
> 1. Part I remains the supported recovery mechanism while Asana is the authoritative task backend.
> 2. The database-backend migration takes priority over further work on Part II.
> 3. Future recovery design must be reconsidered after the database backend exists.
> 4. Intermediate Planning, Research, and Verification-round work should be journaled without
>    changing canonical task content.
> 5. Canonical task content should advance only when the relevant Planning stage, Research stage,
>    or complete Verification round reaches its committed boundary.
> 6. A later agent should recover from the last committed checkpoint and journaled evidence rather
>    than requiring transfer of unfinished trusted authority.
>
> Sections 1–11 are retained only as historical context showing an explored approach and its
> unresolved concerns. They are not requirements.

# Abandoned agent runs: pre-release recovery and long-term ownership redesign

This is Revision 19. Part I records the implemented pre-release recovery model, its first
post-implementation hardening pass, and the prepared-successor live-drift correction. Part II
incorporates the external long-term design review but remains intentionally unapproved.

| Field | Value |
|---|---|
| Revision | 19 |
| Date | 31 July 2026 |
| Part I status | **Implemented, with the prepared-successor drift correction awaiting merge at the time of this revision.** Core abandonment and the first corrective patch are complete. The remaining correction closes the live content/placement drift loop for unclaimed Planning/Research successors. Current runtime behavior must be sourced from `docs/architecture.md` and `docs/runtime-contract.md` after that patch lands. |
| Part II status | **Superseded and abandoned by human decision on 31 July 2026. Do not implement, continue, or review unless Marco explicitly reopens this exact design. Database-backend work takes priority; recovery architecture will be reconsidered after that migration.** |
| Supersedes | Revision 18 |
| Source basis | Revision 18, shipped Part I implementation, current architecture/runtime contract, `future.md`, and a code-grounded review of the deployed GPT Action, request replay, leases, workflow routes, and Verification provenance. |

> **Decision.** Part I remains the release solution for permanently lost runs. Part II may later
> allow a safe replacement chat session to continue the same operation at narrowly approved
> frontiers. Part II does not replace Part I: abandonment and successor creation remain the
> fallback whenever same-operation session replacement is unsafe, ambiguous, or incompatible.

## Reading rule

- Part I is a historical and release-readiness summary. The current executable contract lives in
  `docs/architecture.md` and `docs/runtime-contract.md`.
- Part II is a design draft. It may be reviewed and revised, but it must not be implemented until
  its fixed approval gate is satisfied.
- No Part II concept adds a requirement to Part I retroactively.

# Part I — implemented: permanent run abandonment

> **Shipped principle.** `recover-lease` means the same run is returning.
> `abandon-operation` means that run is permanently gone. Dish never transfers the abandoned
> run's identity to a replacement agent.

## Summary

- The characterization suite proved that removing an expired lease did not remove durable
  ownership: Planning and Research remained bound through `operations.run_id`, while Verification
  remained bound through verifier actor facts and cycle identity.
- Marco chooses only between same-run recovery and permanent abandonment. Code determines the
  stage-specific result from durable and live evidence.
- Clean, unchanged, effect-free attempts may restart through a fresh same-stage successor.
- Already committed work is recovered or preserved.
- Partial, uncertain, contradictory, or unsupported states fail closed and require explicit
  reconciliation.
- The targeted lease must be the latest eligible actor attempt and must be expired or
  administratively released.
- The exact abandoned owner/run cannot claim the successor or continuation.
- Pre-construction Research Evidence/Human-Review rejection is stage-actor authority. The original
  Research run may reacquire a missing lease there without acquiring Verification-cycle authority.

## Part I corrective hardening

The post-implementation review identified four defects. The corrective patch must be merged before
release:

1. **Task fence.** Operation creation and connected starts must be blocked while an abandonment is
   active, except for the exact prepared successor or exact Verification continuation.
2. **Crash/replay convergence.** A crash after the abandonment workflow transition commits must not
   leave its Marco execution or private request permanently pending. Reconciliation must reclaim and
   settle the exact original execution without repeating external effects or succession.
3. **Prepared successor schema adoption.** An unowned Planning/Research successor created before a
   schema deployment must be able to adopt the governed current schema in the same transaction as
   its first claim. Claimed, Verification, terminal, and ordinary operations retain immutable
   schema identity.
4. **Hold relay.** Agent-facing hold responses must contain the generated admin command or clearly
   marked command template, tell the agent to wait for success, and then refresh the authoritative
   Dish action.
5. **Prepared-successor live drift.** A Planning/Research prepared claim that observes changed
   task content or placement must atomically move the abandonment from `awaiting_successor_claim`
   to `blocked_manual_reconciliation`. `reconcile-abandonment` restores the immutable
   successor-owned baseline and expected section through journaled successor effects, then
   republishes the same exact prepared start. The succession and baseline are never rebased in
   place; corrupt bindings or contradictory effects remain blocked.

## Part I acceptance

Part I is release-ready when the corrective patch is merged and the complete suite remains green:

- a permanently lost chat run cannot strand Planning, Research, or Verification;
- `recover-lease` remains same-run recovery and never transfers ownership;
- only the exact authorized successor or continuation can pass an active abandonment fence;
- the abandoned owner/run cannot reclaim the replacement;
- committed work is preserved truthfully;
- only clean baseline-matching states restart;
- partial, uncertain, or contradictory states remain fenced and replayable;
- execution and private-request state converge after crash recovery;
- prepared stage successors do not become stranded solely because the governed schema advanced;
- prepared Planning/Research successors do not loop forever after live content or placement drift;
- drift repair is successor-owned, journaled, replay-safe, and restores rather than mutates the
  immutable succession baseline;
- private relay text gives the agent the exact next administrative instruction.

# Part II — draft: trusted connected sessions and operation authority assignments

> **SUPERSEDED — DO NOT IMPLEMENT OR CONTINUE.** Sections 1–11 describe an abandoned design
> direction. They remain only as historical context. The current decision is to retain Part I
> during the Asana-backed period, prioritize the database backend, and reconsider recovery
> afterward using journaled intermediate work and committed canonical checkpoints. Only Marco may
> explicitly reopen this design.

## 1. Scope, client prerequisite, and decisions

The current deployed Action authenticates every Custom GPT request as the shared owner
`gpt-action`. The GPT itself generates `client.run_id` and sends it in the model-authored request
body. The service validates its UUID syntax but does not independently authenticate it.

Current-code evidence:

- `deploy/gpt-action.md:44–47` instructs the GPT to generate and reuse `client.run_id`;
- `dish_service/command_spec.py:377–410` accepts that body field;
- `dish_service/http.py:297–322` authenticates the shared bearer token and constructs the principal
  from the body-supplied run ID.

Therefore the current service cannot distinguish:

- the same chat returning;
- a different chat copying the old run UUID;
- a different chat requesting replacement.

`client.run_id` remains useful historical provenance, but it is not a trusted transport identity.
No design may treat it as authenticated same-chat evidence after this finding.

Part II now has one prerequisite and three separate tracks:

| Track | Problem | Status against the current GPT Action |
|---|---|---|
| **P — trusted client session prerequisite** | Supply a server-verifiable per-chat identity that the model cannot choose or rewrite. | Required before A1 or B. Not present today. |
| **A1 — quiescent same-session lease reclaim** | The same trusted connected session returns after idle lease expiry, with no interrupted mutation. | Disabled until P exists. |
| **A2 — interrupted-request replay/recovery** | A command may have started, completed externally, or lost its response. | Existing request/execution recovery problem; remains separate from A1. |
| **B — different-session same-operation replacement** | Marco authorizes a different trusted connected session to continue the same operation. | Disabled until P exists. |

A stateful HTTP proxy is not sufficient unless its upstream receives a stable, authenticated
per-conversation identifier. The prerequisite must be one of:

- an upstream-signed conversation/session identifier supplied outside model-authored JSON; or
- a custom stateful client or broker that owns the conversation session and injects a signed
  per-session credential or header that Dish can verify.

The model must not be able to select, copy, or edit that identity. Until such a client exists:

- same-chat automatic reclaim remains unavailable on the GPT Action;
- different-chat same-operation replacement remains unavailable;
- exact request replay, `recover-lease`, and Part I abandonment remain the supported paths.

Explicitly out of scope:

- intentional dish revision;
- re-Verification and return-to-Planning workflows;
- transfer of a bound verifier review;
- historical release execution;
- replacement at partial, uncertain, or contradictory effect frontiers;
- replacing Part I abandonment and successor creation.

## 2. Corrected problem statement

A Dish operation is not always one Planning, Research, or Verification stage attempt.

- A Planning operation normally ends at Research handoff.
- An Initial or Change operation begins under Research authority, may own several Verification
  cycles, may pass through Small or Large correction routes, may enter Evidence/Human holds, and
  may finally own submission.
- Verification authority is cycle-specific and may change several times inside one operation.

The durable design must distinguish:

1. **trusted transport session identity** — which authenticated conversation/client session is
   calling;
2. **connected session record** — Dish's durable record for that trusted transport session;
3. **connected operation authority** — which session may mutate one operation in one exact
   role/cycle/route context now;
4. **service lease** — short-lived liveness for that authority;
5. **operation execution claim** — process-level mutation/recovery fencing, including private admin
   and system recovery;
6. **workflow actor lineage** — durable authorship/reviewer lineage that cannot be laundered through
   session replacement;
7. **effect provenance** — which authority, session, actor lineage, or private execution caused each
   durable effect.

Connected workflow authority and private admin execution authority are separate systems. A private
admin command may repair or advance workflow state, but it never becomes Planning, Research, or
Verification authority merely because it executed the mutation.

A connected session may hold authority over several operations. Replacing its authority for one
operation must not globally supersede that session on other operations.

## 3. Chosen conceptual model

```text
trusted transport session
  -> connected_session
       ├── operation_authority A on operation 1
       ├── operation_authority B on operation 2
       └── observed client.run_id provenance only

operation
  ├── authority generation 1: Research
  ├── authority generation 2: Verification cycle 1
  ├── authority generation 3: Verification cycle 2
  └── terminal result

workflow_actor_lineage
  ├── one durable author/editor/verifier lineage
  ├── inherited by replacement sessions continuing that lineage
  └── linked to exact effect-producing sessions

private execution authority
  └── existing admin/recovery execution subsystem, separate from operation_authorities
```

The existing `operations` row remains the durable workflow operation. No parallel
`workflow_attempt` table is introduced in the first implementation.

## 4. Trusted connected-session identity

### 4.1 Client prerequisite

Dish must receive a `transport_session_id` through a trusted adapter or broker boundary.

Required properties:

- opaque and high-entropy or cryptographically signed;
- stable for one conversation/client session;
- different for a genuinely new conversation;
- injected outside model-authored command arguments;
- authenticated to the transport owner;
- impossible for the model to override with `client.run_id`;
- available before request journaling and authority validation;
- suitable for binding an intended replacement claimant.

The service must ignore `client.run_id` for authority decisions after cutover. It may retain it as
untrusted diagnostic provenance.

### 4.2 `connected_sessions`

Suggested fields:

| Field | Purpose |
|---|---|
| `connected_session_id` | Server-issued durable identifier. |
| `owner_id` | Authenticated service principal. |
| `transport_session_id` | Trusted adapter-injected conversation/session identity. |
| `observed_client_run_id` | Model/client-supplied run UUID retained as non-authoritative provenance. |
| `state` | `active` or `closed`. |
| `created_at`, `last_seen_at`, `closed_at` | Lifecycle evidence. |

Required rules:

- global uniqueness for `(owner_id, transport_session_id)`, not only while active;
- a closed transport session is never recreated as a new `connected_session_id`;
- a request presenting a previously closed transport session resolves to the historical closed row
  and is rejected for mutation;
- session identity, owner, and trusted transport identity are immutable;
- lease expiry does not close a connected session;
- durable holds do not close a connected session;
- a session closes only through explicit trusted-client termination, permanent session
  abandonment, or administrative retirement after no operation authority remains;
- closing a connected session does not rewrite operation or effect history;
- a connected session is not assigned a workflow role globally.

These rules prevent a fenced session from escaping exclusion by closing and reopening under the
same transport identity.

## 5. Connected operation authority

### 5.1 Operation fields

Add:

| Field | Purpose |
|---|---|
| `current_connected_authority_id` | Designated connected authority for the current workflow frontier, nullable only at unbound and terminal frontiers. |
| `authority_generation` | Monotonic generation of activated connected authority assignments. |
| `authority_model` | `legacy_run` or `authority_assignment`; one operation must never read both as authority. |

`current_connected_authority_id` is the sole source of connected mutation authority after cutover.
It may point to an `active`, `suspended`, or `replacement_pending` authority. Only `active` may
mutate, and only with the required lease.

Private admin commands and system recovery remain authorized through private routes and the
existing operation-execution subsystem. They are not rows in `operation_authorities`.

### 5.2 `operation_authorities`

Suggested fields:

| Field | Purpose |
|---|---|
| `authority_id` | Server-issued connected assignment identity. |
| `operation_id` | Exact governed operation. |
| `generation` | Activated operation authority generation. |
| `connected_session_id` | Exact trusted connected session. |
| `actor_lineage_id` | Exact workflow actor lineage continued by this authority. |
| `role` | `planning`, `research`, or `verification`. |
| `context_cycle_id` | Exact Verification cycle, nullable outside Verification authority. |
| `origin_route_id` | Exact rejection/hold route that created a Research continuation, nullable otherwise. |
| `state` | `active`, `suspended`, `replacement_pending`, or `ended`. |
| `predecessor_authority_id` | Previous connected authority assignment, nullable. |
| `activation_kind` | `operation_start`, `normal_handoff`, `replacement`, or `migration`. |
| `ended_reason` | Stable reason when ended. |
| lifecycle timestamps | Activation, suspension, replacement-pending, resume, and end evidence. |

Submission is not a separate role. The exact verifier authority that approved continues through
submission unless the workflow route ends or suspends it.

Required constraints:

- only one designated connected authority per operation;
- only the designated authority may be `active`, `suspended`, or `replacement_pending`;
- generation is unique and monotonic per operation;
- same-authority lease renewal, self-reclaim, or hold resume does not increment generation;
- `replacement_pending` cannot mutate or resume, but remains the durable source for another grant
  or Part I fallback;
- ended authority never becomes active, suspended, or replacement-pending again;
- predecessor lineage is append-only and acyclic;
- role, actor lineage, and cycle/route context are immutable;
- Verification authority always has exact `context_cycle_id`;
- terminal operations have no designated connected authority.

### 5.3 Generation semantics

`operations.authority_generation` changes only when a new connected authority is successfully
activated.

A replacement grant reserves `next_generation = operations.authority_generation + 1`. Grant
creation does not update the operation generation. Claiming the grant atomically:

1. proves the reserved generation is still next;
2. ends the `replacement_pending` source authority;
3. creates the new authority with that generation;
4. sets `current_connected_authority_id`;
5. advances `operations.authority_generation`;
6. consumes the grant.

Generation does not change when:

- the same authority renews or reacquires a lease;
- the same authority resumes after an authorized hold;
- a private admin command executes recovery without creating a connected authority.

Every connected mutation validates exact `authority_id` and generation. Generation alone is
insufficient.

## 6. Workflow actor lineage and exact provenance

Execution authority and semantic authorship are different facts.

### 6.1 Actor-lineage identity

Introduce a server-issued `actor_lineage_id` representing one durable author, material-editor, or
verifier lineage inside the workflow.

It is not a model name, owner account, agent profile, session ID, or caller attestation.

Rules:

- create a new actor lineage when a genuinely new Planning, Research, or Verification actor begins;
- same-session resume preserves it;
- same-operation replacement that continues the same actor's unfinished work inherits it;
- a normal handoff to a new role or a fresh Verification cycle creates a new actor lineage;
- a verifier performing a Small or Large correction also contributes that verifier lineage as the
  material editor of the corrected candidate;
- private admin recovery may execute an effect attributed to an existing actor lineage without
  becoming that actor;
- lineages are immutable and append-only.

This avoids the invalid fallback of treating every chat under the shared `gpt-action` owner as one
actor. Until trusted transport sessions exist and this model cuts over, the current task-wide run
lineage remains the executable Verification-independence rule
(`dish_tool/step5.py:218–261`, `docs/architecture.md:120–129`).

### 6.2 Verification independence

After cutover, a fresh verifier must satisfy both:

1. its `actor_lineage_id` is absent from all task-wide constructor and material-editor actor facts;
2. its trusted `connected_session_id` is absent from the effect-producing session provenance of
   those facts.

A replacement Research session inherits the Research actor lineage, so replacement cannot launder
candidate authorship. A session that actually wrote or materially corrected the candidate remains
independence-disqualified even if it later requests a fresh verifier lineage.

### 6.3 Required provenance

| Record | Required provenance |
|---|---|
| Service lease | Exact connected authority ID, generation, trusted connected session, lease kind, and cycle context. |
| Service request | Trusted connected session immediately; exact authority binding when known. |
| Operation execution | Exact executing connected authority when connected, or exact private execution identity when administrative. |
| Write/movement attempt | Executing authority or private execution, plus exact actor lineage attribution where different. |
| Actor fact | Stable `actor_lineage_id`, originating connected authority/private execution, and effect-producing connected session. |
| Verification review/decision | Exact verifier authority/generation, verifier actor lineage, trusted connected session, cycle, confirmed reviewed content-version ID and identity, attestation, verifier actor fact, Verification Queue placement, and exact post-read inspection fact. |

The initial disqualifying-lineage query must preserve current semantics: any task-wide constructor or
material-editor lineage disqualifies that actor/session from Verification. The current runtime also
requires exact `reviewed_content_version_id`, actor fact, inspection fact, attestation, and queue
placement; reviewed identity alone is insufficient
(`dish_tool/application_service.py:77–106`, `docs/architecture.md:120–139`).

`operations.run_id` and `verification_cycles.run_id` may remain historical projections during
migration, but must not remain independent mutation-authority sources after cutover.

## 7. Normal authority transitions

The redesign must model every currently supported workflow route before replacement is enabled.

| Workflow frontier | Designated connected authority | Normal transition |
|---|---|---|
| Planning `prepare_required` | Active Planning authority | Planning handoff ends authority and normally terminalizes the Planning operation. |
| Initial/Change Research `prepare_required` | Active Research authority | Material candidate handoff ends Research authority; operation remains open in `await_verification` with no designated authority. |
| Non-material Change check-in | Active Research authority | The Research authority writes the checked-in candidate, inherits the exact prior signoff, terminalizes the Change operation, and ends. No new Verification authority is created (`dish_tool/step6.py:414–450, 482–490`). |
| Pre-construction Research hold | Suspended Research authority | Hold resolution may resume only this exact authority when the route names the same operation, role, and generation. A dead session uses Part I. |
| Verification cycle unbound | None | Exact-cycle Verification start creates a fresh verifier authority, actor lineage, and generation. |
| Verification review bound | Exact active verifier authority | Same authority continues through inspection and decision. The binding includes the exact confirmed reviewed version, identity, actor fact, attestation, queue placement, and inspection fact. |
| Small correction | Same verifier authority | Authority remains verifier; the verifier actor lineage is also recorded as material editor for the corrected candidate while the reviewed version remains immutable. |
| First-pass Large rejection | Same verifier authority through route commit | The verifier actor lineage becomes material editor of the corrected candidate; the rejected cycle completes; verifier authority ends; the new cycle is unbound (`dish_tool/step8.py:873–915, 940–947`). |
| Two-pass Large rejection | Same verifier authority through route commit | The verifier actor lineage becomes material editor; the cycle completes into the governed Human hold; verifier authority ends. The existing private two-pass reopen installs the authorized reset and creates a fresh unbound Verification cycle in the same operation (`dish_tool/step8.py:850–900, 1080–1187`). |
| Verifier-originated Evidence/Human hold | Verifier authority through route commit, then ended | Private resolution never turns admin execution into connected authority. A `pending-research` resolution terminalizes the held operation and the next ordinary Research start creates a new operation/authority; a `pending-verification` resolution creates a fresh unbound cycle in the same operation (`dish_tool/step8.py:1192–1212, 1390–1452`). |
| Approved, submission pending | Exact verifier authority | Same verifier authority submits. Terminalization ends it. |
| `ready_move_failed` destination repair | Suspended exact verifier authority | Private `repair-destination` records its own execution/effect provenance and preserves approval; the same verifier authority resumes `submit` after repair. A dead verifier uses Part I (`dish_tool/application_service.py:127–153`, `dish_tool/step9.py:802–980`). |
| Terminal operation | None | No connected mutation. |

Research continuations created by rejection or hold resolution record their origin route/cycle
separately from Verification `context_cycle_id`; Research authority remains role `research`.

Private admin execution does not silently become workflow actor lineage or connected Research or
Verification authority.

## 8. Track A — same-session continuation

### 8.1 Availability gate

Track A1 is disabled for the current GPT Action because the body-supplied `client.run_id` is not a
trusted session identity. It becomes available only after Section 4's client prerequisite is met.

### 8.2 A1: quiescent lease self-reclaim

The connected command is journaled before lease handling in the current service
(`dish_service/request_replay.py:23–49`, `dish_service/application.py:1736–1745`). Therefore
quiescence means no **prior** pending/uncertain work; it does not reject the exact reclaim request
currently being executed.

The next connected mutation may atomically reclaim liveness when all are true:

- trusted owner and `transport_session_id` exactly match the designated authority's session;
- authority is `active`, or is `suspended` and the exact continuation authorizes resume;
- exact operation, role, cycle/route context, authority ID, actor lineage, and generation match;
- no active Part I abandonment, replacement grant, successor, or later authority conflicts;
- no live operation-execution claim exists for another process;
- no prior pending, uncertain, or unresolved request, execution, workflow step, or external effect
  exists;
- command is valid in the current phase and release contract.

Lease terminology is exact:

- an **unreleased** lease is any row with `released_at IS NULL`, even if `expires_at` is in the past;
- an expired-but-unreleased row still occupies the operation/task uniqueness constraints and cannot
  be bypassed (`dish_service/leases.py:55–73, 165–241`,
  `dish_tool/database_schema.py:1182–1201`).

A1's authority transaction must:

1. revalidate the exact expired actor lease when one exists;
2. release that exact expired row;
3. insert a new actor lease for the same authority/session/generation;
4. complete the exact current request result.

If the old lease is already released or missing, the transaction inserts the new lease after all
same-authority checks. It does not create a new authority or actor lineage.

### 8.3 A2: interrupted-request replay or recovery

If the returning session has an exact prior request or any execution/effect may be incomplete:

- exact request replay remains authoritative when its request ID is supplied;
- operation-execution recovery determines whether effects applied, did not apply, or remain
  contradictory;
- a new command must not bypass the unresolved prior request merely because the lease expired;
- A1 is forbidden until the prior request/execution state is settled.

A1 and A2 remain separate under both `legacy_run` and future `authority_assignment` operations.

## 9. Track B — Marco-authorized different-session replacement

### 9.1 Availability and claimant binding

Track B is disabled for the current GPT Action until a trusted transport session exists.

The intended replacement conversation must be registered with the trusted client/broker before
Marco authorizes replacement. The grant binds to:

- exact `intended_connected_session_id`;
- exact intended owner;
- exact operation, role, generation, cycle/route context, and actor lineage;
- exact source authority and source transport session.

The grant ID is an identifier, not claimant authority. Any other session is rejected even if it
knows the grant ID.

### 9.2 Grant lifecycle

Use append-only `operation_authority_replacement_grants`.

Allowed transitions:

```text
prepared -> claimed
prepared -> expired
prepared -> cancelled
prepared -> blocked
blocked  -> prepared   (only after explicit private reconciliation)
```

`claimed`, `expired`, and `cancelled` are terminal grant states. The source authority remains
`replacement_pending` after blocked, expired, or cancelled grant outcomes. It never becomes active
again, but it remains the exact durable target for:

- reconciliation of the same blocked grant;
- a new grant after expiry/cancellation;
- Part I abandonment when same-operation replacement is no longer suitable.

This avoids a state with neither a valid current authority target nor a valid Part I fallback.

### 9.3 Phase 1 — authorize and fence

Marco runs a private command against the exact source authority and intended replacement session.
The command's own pending service request, `admin_request` lease, and operation-execution claim are
expected and are excluded by exact ID from quiescence; every other request, lease, and execution
must satisfy Section 10.

In one writer transaction Dish must:

1. validate the current source authority, intended claimant, and exact clean frontier;
2. prove quiescence;
3. release the exact expired-but-unreleased source actor lease, if present;
4. move the source authority to `replacement_pending` without clearing the operation pointer;
5. reserve `next_generation = operations.authority_generation + 1` in the grant without advancing
   operation generation;
6. create the exact one-time grant bound to the intended claimant;
7. permanently fence the source session for that operation;
8. record audit evidence and the exact claim action.

A live, unexpired actor lease is not replaceable in the initial design.

### 9.4 Phase 2 — exact replacement claim

Only the grant's exact intended connected session may claim it.

In one authority transaction Dish must:

1. revalidate the grant, intended session, source authority, operation, frontier, release, and live
   task state;
2. prove the intended transport session is active, globally unique, and not the fenced source
   session;
3. preserve the source `actor_lineage_id` because this is continuation of the same unfinished actor
   work;
4. end the `replacement_pending` source authority;
5. create the new authority with the reserved generation and inherited actor lineage;
6. set it as designated and active;
7. advance `operations.authority_generation`;
8. acquire its actor lease;
9. consume the grant;
10. complete the exact claim request result.

A new semantic actor is not created merely because a different session continues the same
unfinished Planning or Research work.

### 9.5 Grant fields

| Field | Purpose |
|---|---|
| `replacement_grant_id` | Exact one-time grant identifier. |
| `operation_id` | Governed operation. |
| `source_authority_id` | Authority moved to `replacement_pending`. |
| `source_connected_session_id` | Exact fenced source session. |
| `intended_connected_session_id` / `intended_owner_id` | Only permitted claimant. |
| `actor_lineage_id` | Exact actor lineage inherited by the claimant. |
| `reserved_generation` | Next generation, not yet active. |
| `role`, `context_cycle_id`, `origin_route_id` | Exact permitted authority context. |
| `frontier_kind` | Exact clean-frontier classification. |
| `status` | `prepared`, `blocked`, `claimed`, `expired`, or `cancelled`. |
| `created_by_request_id`, `reason`, timestamps | Authorization and audit evidence. |

## 10. Quiescence and safe-frontier classifier

Same-operation replacement is legal only when all earlier mutation authority and external effects
are quiescent.

The classifier is evaluated inside the exact private authorization command. It must distinguish the
command's own authority from prior workflow work:

- the current Marco request, its exact admin execution, and its exact `admin_request` lease are
  expected and excluded by ID;
- no other live operation-execution claim may exist;
- no prior pending or uncertain service request may exist for the source authority;
- no unreleased actor lease may remain after the transaction's exact expired-lease release step;
- a live unexpired actor lease blocks replacement.

Additional required checks:

- source operation is the current actionable operation and authority-model leaf;
- exact designated authority remains the source authority;
- source authority is `active` or `suspended`, not already replacement-pending or ended;
- no active Part I abandonment or prepared successor exists;
- no unresolved prior execution exists;
- no pending, started, uncertain, or contradictory write/movement/decision effect exists;
- no declared-but-unresolved workflow step could still authorize an effect;
- live task content and placement equal the exact frontier baseline;
- operation release fingerprint matches the executing deployment;
- role, operation kind, actor lineage, and Verification cycle or Research route context are exact;
- intended claimant is a different trusted transport session;
- no later connected authority or replacement grant exists.

Fail closed on ambiguity. The classifier returns a durable evidence record containing the exact
baseline identity, placement, relevant steps/effects, source authority, actor lineage, generation,
operation kind, role, intended claimant, and cycle/route context used for the decision.

## 11. Approved replacement frontiers

Track B remains disabled until the trusted-client prerequisite exists. After that prerequisite, the
initial design permits only these same-operation replacement frontiers:

| Frontier | Exact clean predicate | Policy |
|---|---|---|
| Planning clean start | `prepare_required`; exact pre-Planning baseline and placement; no declared workflow step; no prior request/execution/effect beyond successful start and authority activation | Marco-authorized replacement allowed. |
| Initial Research clean start | `kind=initial`; `prepare_required`; exact Research baseline and placement; no pre-construction hold, candidate, validation, cycle, movement, or unresolved step/effect; immutable operation initialization may exist | Marco-authorized replacement allowed. |
| Change Research clean start | `kind=change`; `prepare_required`; exact Change baseline and placement; completed immutable `change_intent` is allowed; no candidate/hold/validation/cycle/movement or unresolved effect after intent | Marco-authorized replacement allowed; replacement inherits exact Change intent and Research actor lineage. |
| Pre-construction Research hold | Hold exists and Research authority is suspended | No different-session replacement initially. Same-authority resume when permitted; otherwise Part I. |
| Unbound Verification cycle | No verifier/review binding | Normal fresh verifier claim, not replacement. |
| Bound Verification review | Reviewed version/identity or verifier authority bound | Never replace that review. Same-authority recovery or Part I fresh Verification attempt. |
| Non-material Change already started; any correction, rejection, hold route, signoff, submission, `ready_move_failed`, committed route, partial effect, or uncertainty | Any such evidence exists | No same-operation replacement; use existing recovery or Part I. |
| Terminal operation | Terminal | No replacement. |

Grant outcome rules:

- `blocked` may return to `prepared` only through exact private reconciliation;
- `expired` or `cancelled` requires a new grant or Part I;
- source authority remains `replacement_pending` and can never resume mutation;
- Part I remains available because the source authority, session, actor lineage, and lease history
  remain durably identifiable.

The frontier set may expand only through a new design decision backed by code characterization and
fault evidence.

## 12. Request and replay contract

### 12.1 Request binding

A service request exists before a new operation or authority may exist.

Therefore:

- every request binds immediately to `connected_session_id` and client provenance;
- when an operation authority becomes known, append an exact
  `service_request_authority_binding` containing authority ID and generation;
- failed pre-operation starts may legitimately have no authority binding;
- bindings are immutable and request replay never infers them from timestamps.

### 12.2 Historical result and current authority

The stored command result remains immutable.

For session-aware API clients, use a versioned replay envelope:

```json
{
  "historical_result": { "...": "exact stored result" },
  "current_authority": {
    "operation_id": "...",
    "authority_id": "... or null",
    "generation": 4,
    "session_state": "current|suspended|ended",
    "allowed_actions": [],
    "observed_at": "..."
  }
}
```

Rules:

- `historical_result` is deterministic exact replay;
- `current_authority` is an explicitly live advisory snapshot, not part of idempotent command
  history;
- stale historical `allowed_actions` never grant mutation;
- every mutation still validates exact authority ID and generation;
- legacy clients may retain the existing stored-envelope contract until they migrate to the
  versioned envelope;
- pending and uncertain requests settle against the exact authority that issued them.

## 13. Release and schema compatibility

Same-operation authority replacement must preserve the operation's execution contract.

Persist a canonical operation release fingerprint containing at least:

- role protocol release and content hash;
- schema version and schema hash;
- required manifest identities/hashes;
- required adapter identity;
- downstream Verification protocol identity for Research operations;
- workflow compatibility version.

First implementation policy:

- current deployment must exactly match the operation fingerprint;
- same-session resume and new-run replacement do not upgrade the operation;
- no historical mutation engine is introduced;
- mismatch blocks same-operation continuation;
- use explicit operation migration or Part I successor under the current release.

Compatibility broader than exact fingerprint equality is deferred.

## 14. Migration and cutover

Migration must not fabricate session or authority history.

Recommended policy:

1. Add session and authority tables without changing existing authority.
2. New operations use `authority_model=authority_assignment`.
3. Legacy operations remain `authority_model=legacy_run` until individually migrated or completed.
4. Migrate an open legacy operation only when exact owner/run/lease/actor/cycle evidence proves one
   unambiguous current connected authority and no unresolved effect exists.
5. Write one migration authority assignment with exact evidence; do not invent prior assignments.
6. Ambiguous legacy operations remain legacy and use Part I.
7. During dual-write, legacy projections may be maintained for compatibility, but mutation reads
   exactly one authority model selected by the operation row.
8. Completed legacy operations remain unchanged.

Rollback:

- disabling replacement does not erase session or authority records;
- migrated operations remain on the authority-assignment read path;
- rollback may disable new replacement grants while retaining same-session and normal handoff
  authority behavior;
- authority generations never decrease and ended authorities never reactivate.

## 15. Security and trust model

Initial deployment assumptions:

- single-owner personal system;
- authenticated Action owner remains shared across the user's connected chats;
- connected run IDs and opaque session IDs are visible to the model and are not treated as secrets;
- agents are not assumed malicious, but accidental stale or cross-operation claims must fail closed;
- new-run replacement requires Marco authorization.

Required controls:

- unguessable IDs even though IDs are not sole credentials;
- exact owner/run/session/authority/generation checks;
- old-authority exclusion on replacement grants;
- one-current-authority database constraints;
- exact role/cycle targeting;
- rate limiting and telemetry for repeated invalid claims;
- no authority ID or grant permits mutation after end/consumption;
- replacement cannot bypass verifier independence;
- private admin commands remain outside the public Action surface.

If multi-user isolation, adversarial agents, or hidden per-chat credentials become requirements, a
stateful client/broker must be designed before enabling replacement under that threat model.

## 16. Observability and operator controls

Expose:

- connected session history by owner/run;
- current and historical operation authorities;
- authority generation and role/cycle context;
- active lease, requests, executions, and effects by authority;
- replacement-grant state and safe-frontier evidence;
- release fingerprint status;
- exact reason for resume, replacement, refusal, or Part I fallback.

Audit events should include:

- connected session created/closed;
- operation authority activated/suspended/resumed/ended;
- generation reserved/advanced;
- replacement grant prepared/claimed/expired/cancelled/blocked;
- stale authority rejected;
- same-run lease self-reclaimed;
- Part I fallback selected.

Metrics should distinguish Track A resume, Track B replacement, Part I fallback, stale-generation
rejects, invalid grant claims, and manual blocks.

## 17. Relationship to Part I and other future workflows

| Existing/future mechanism | Relationship |
|---|---|
| Part I permanent abandonment | Universal fallback for unsafe, ambiguous, incompatible, or bound-review loss. |
| Part I successor operations | Retained whenever a new operation/cycle is required. |
| Same-run `recover-lease` | May become automatic Track A self-reclaim under strict quiescence. |
| Re-Verification | Separate workflow intent; may reuse exact authority/cycle targeting but is not session replacement. |
| Intentional revision / return to Planning | Separate workflow intent; may reuse successor primitives but is not session replacement. |
| Private hold resolution | Uses private admin execution authority and emits exact continuation authority. |

Part II does not replace or broaden these workflow semantics.

## 18. Tentative implementation sequence

1. **Trusted client session prerequisite.** Supply a `transport_session_id` that the model cannot
   set or override (Section 4.1). Track A1 and Track B remain disabled until this exists.
2. **Track A1 characterization and same-session self-reclaim.** Depends on step 1; no additional
   session model beyond the prerequisite is required.
3. **Session and authority schema foundation.** Add connected sessions, operation authorities,
   generations, constraints, and audits without enabling replacement.
4. **Normal authority handoffs.** Model Research-to-Verification, repeated cycles, holds, and
   submission before any replacement feature.
5. **Exact authority bindings.** Bind leases, requests, executions, effects, and actor provenance;
   add request-authority binding.
6. **Replay v2 and migration path.** Introduce historical/live separation and per-operation cutover.
7. **Shadow replacement classifier.** Evaluate clean Planning/Research frontiers without allowing
   replacement.
8. **Marco-authorized replacement grants.** Enable one role/frontier at a time.
9. **Verification remains Part I-only after review binding.** Reconsider only with separate evidence
   and design.

## 19. Fixed approval gate

Part II is ready for implementation review only when all are explicit and code-backed:

1. Track A and Track B remain separate;
2. connected-session identity works with the actual GPT Action client;
3. normal operation authority handoffs are completely specified;
4. `current_connected_authority_id` is the sole connected mutation source;
5. authority ID and generation rules are database constrained;
6. request, lease, execution, effect, and cycle bindings are exact;
7. semantic authorship is separate from execution authority;
8. verifier-independence uses a stable semantic identity rule;
9. same-run quiescence and self-reclaim algorithm is complete;
10. replacement uses a two-phase exact grant/claim contract;
11. safe-frontier evidence and fault matrix are complete;
12. replay v2 has no deterministic/live ambiguity;
13. release fingerprint and mismatch behavior are exact;
14. migration reads one authority model per operation and fails closed;
15. security assumptions match the actual single-owner Action deployment;
16. Part I remains the fallback for every unsupported state;
17. concurrency and crash tests cover old-authority races, grant claim, normal handoffs, and admin
    recovery.

## 20. Remaining decisions

Before implementation approval, decide:

- whether Track A should replace manual `recover-lease` automatically on the next valid mutation;
- whether the first Track B Planning/Research replacement always requires Marco authorization;
- exact replacement-grant lifetime and cancellation behavior;
- canonical release fingerprint serialization;
- whether replay v2 is introduced through a new API version or an opt-in response field;
- whether a future hidden stateful client/broker is worth building beyond the single-owner threat
  model.

## Status

> **DRAFT — AUTHORITY MODEL REWRITTEN — DO NOT IMPLEMENT.** The design now distinguishes connected
> sessions, operation authority assignments, lease liveness, execution claims, and semantic actor
> provenance. The next step is a fixed-boundary review of this model, not another patch to the old
> operation-scoped session design.

# Appendix A — source basis

- Revision 16 and the shipped Part I lineage.
- Current Dish architecture and runtime contracts for operations, leases, request replay,
  operation execution, Research/Verification cycles, holds, submission, abandonment, and prepared
  successors.
- Current GPT Action authentication and `client.run_id` behavior.
- `docs/future.md` distinctions between abandonment, re-Verification, intentional revision, and
  return-to-Planning.
- Full Part II review findings requiring:
  - separate Track A same-run resume and Track B new-run replacement;
  - connected session, operation authority, and semantic actor separation;
  - normal authority handoffs before replacement frontiers;
  - exact quiescence, replay, migration, release, and security rules.
