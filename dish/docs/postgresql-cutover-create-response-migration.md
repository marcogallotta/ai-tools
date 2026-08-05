# Stage A create-response migration

## Status and scope

**Implementation contract for the approved PostgreSQL Stage A design — 5 August 2026.**

This document defines the canonical `create` response, compatibility rules, complete known consumer
inventory, and deployment order. It does not implement runtime code. It is subordinate to the
settled create behavior in `dish/docs/postgresql-cutover.md` and its acceptance requirements in
`dish/docs/postgresql-cutover-imp.md`.

Received base identity:

- archive: `ai-tools-venv(20260805-195845).tgz`;
- archive SHA-256: `09a32bd6f42496de9a6a77b556a8d806a310a9de98e781b26b516e2a7a73377d`;
- the archive contained no Git metadata;
- synthetic local baseline commit used only to produce reviewable patches:
  `618ea622b150b4b2a5e367909dd13201a45ab206`.

## Normative create result

A successful PostgreSQL-authoritative `create` command returns the ordinary command envelope and the
following command-specific `data` object:

```json
{
  "dish_id": "<canonical lowercase Dish UUID>",
  "url": "<configured Dish frontend URL or null>",
  "asana_task_gid": "<decimal Asana task GID or null>"
}
```

The fields have these exact meanings:

| Field | Presence | Authority and validation | Replay meaning |
|---|---|---|---|
| `data.dish_id` | required on every successful create and replay | Canonical `DishTask.task_id`; non-nil canonical lowercase UUID; identifies the Dish independently of every projection | The same exact request always returns the same value. |
| `data.url` | optional or null | Convenience URL produced only by configured Dish URL authority; it must resolve to the same `dish_id`; it is not an Asana URL and is not proof that Asana projection succeeded | The replayed first outcome retains the value originally committed. If no URL was available then, replay does not enrich it later. |
| `data.asana_task_gid` | optional or null | Secondary projection identity; decimal Asana GID; present only when the canonical mapping to the same Dish is already durably confirmed | The replayed first outcome retains the original value. Later projection success is learned by a fresh read/resolution operation. |

The top-level common-envelope field `task_gid` remains an **Asana-only compatibility field** wherever
it is retained. It must never contain `dish_id`, any other Dish UUID, or a Dish URL. For a canonical
create that does not already have a durably confirmed Asana projection, top-level `task_gid` is null
or absent according to the final envelope schema. If both `task_gid` and `data.asana_task_gid` are
present, they must be identical decimal Asana GIDs.

The current generic result envelope always emits top-level `task_gid`; therefore Stage A may keep the
field as nullable during migration. Removing it from the generic envelope is not required for the
create migration and must not be coupled to cutover.

## Transaction and failure contract

1. The canonical Dish row, first content version and activation, command execution, immutable request
   outcome, audit evidence, and required projection intent commit in one PostgreSQL transaction.
2. Successful create does not wait for an Asana API call, an Asana task identity, frontend
   availability, or frontend URL resolution.
3. Missing Dish URL configuration produces `url: null` or omission; it is not create failure.
4. A projection delay, retryable projection error, ambiguous Asana attempt, reconciliation item, or
   terminal projection failure does not roll back or invalidate canonical create.
5. A transport failure after canonical commit follows committed-success-stays-success. Exact replay
   returns the stored successful outcome and the same `dish_id`.
6. The first authoritative outcome is immutable. Projection or URL facts discovered later do not
   rewrite it. A fresh canonical read, identifier-resolution command, or projection-status read must
   expose later metadata.
7. A failure before canonical commit creates no Dish. Its durable request outcome or uncertainty
   state follows the Stage A request/replay contract; callers must not infer success from a projection
   artifact alone.
8. The service must reject any serialization path that attempts to assign a Dish UUID to a field
   named `task_gid`.

## Identifier acceptance after migration

Agent and client surfaces may accept either a canonical Dish UUID or a configured Dish URL only after
one shared resolver can prove both identify the same canonical Dish. An Asana URL or Asana GID remains
an external projection identifier and resolves through the projection mapping, not through UUID
reinterpretation.

Compatibility parsing must be field-directed:

- `dish_id` is parsed only as a Dish UUID;
- `url` is parsed only by the configured Dish URL grammar and resolver;
- `asana_task_gid` and compatibility `task_gid` are parsed only as decimal Asana GIDs;
- no parser may guess an identifier type from shape and silently move it into another field;
- legacy responses may be recognized by the absence of `dish_id`, but only before PostgreSQL
  authority is activated and only with `task_gid` retaining its historical Asana meaning.

## Complete known consumer inventory

“Consumer” includes producers, schemas, adapters, instructions, scripts, tests, and frontend code
whose assumptions determine whether the response can be deployed safely. Files that merely use
`task_gid` for unrelated commands are not create-response consumers and are intentionally excluded.

### Runtime producers and adapters

| Repository location | Current dependency | Required migration | Deployment gate |
|---|---|---|---|
| `dish/dish_tool/commands.py` (`_step5_create`) | Legacy authority creates Asana first and returns top-level and `data.task_gid`. | Preserve only for legacy authority before cutover. Do not copy its create ordering or identity semantics into PostgreSQL. Add compatibility tests proving legacy `task_gid` stays an Asana GID. | Must remain unchanged until legacy writer retirement; PostgreSQL route must not call it. |
| `dish/dish_pg/command_port.py` (`_create`) | Creates the canonical UUID correctly but returns `data.task_id`, content version, and projection event ID. | Rename the public canonical field to required `dish_id`; keep internal `task_id` naming private. Add optional `url` and `asana_task_gid` according to this contract. Never surface the UUID through `task_gid`. | Required before the exact first live PostgreSQL create. |
| `dish/dish_pg/protocol.py` | Serializes `CommandResult` with `dataclasses.asdict`, so command-port field names become public without command-specific validation. | Validate/serialize the create result against the approved schema before returning it; do not expose an internal `task_id` dialect or synthesize `task_gid`. | Required before the PostgreSQL port is placed behind a live route. |
| `dish/dish_tool/results.py` | Generic envelope always emits nullable `task_gid`. | Preserve its Asana-only meaning or introduce a PostgreSQL result adapter that emits null. Add a serialization assertion/test forbidding a UUID in `task_gid`. | Required before PostgreSQL create is exposed through the service. |
| `dish/dish_service/application.py` | Owns service result finalization, durable replay, and legacy/shadow dispatch. | Ensure PostgreSQL create result is normalized once before outcome commit; replay must return stored `dish_id` and must not enrich later projection metadata. | Required before service cutover. |
| `dish/dish_service/http.py` | Publishes the current service application's command result as the Action HTTP response. | Keep transport field-neutral, but route the PostgreSQL create result through the command-specific serializer/schema and preserve exact replay bytes/semantics. | Required before the service route switches authority. |
| `dish/dish_service/request_replay.py` and request coordinators | Store/replay common result envelopes built around legacy command results. | Treat the whole canonical create result as the first authoritative outcome; include `dish_id` in equality/durability tests and retain permanent request reservation. | Required before service cutover. |
| `dish/dish_service/shadow_capture.py` | Captures legacy response/state containing Asana task identities. | Add explicit source/target identity roles. Do not compare legacy `task_gid` directly with target `dish_id`; correlate through approved create-correlation evidence or classify create capture-only. | Required before create is used as parity evidence. |
| `dish/dish_pg/shadow_worker.py` | `_source_task_reference` accepts `task_gid` or `task_id` from source arguments/snapshots. | Keep legacy source identity and target canonical identity distinct; prohibit a fallback that treats source Asana GID as target Dish UUID. | Required before executing create in shadow; current policy correctly keeps create capture-only. |
| `dish/dish_shadow/policy.py` | Marks `create` capture-only because lost-response correlation is unqualified. | Retain capture-only until a testable correlation contract exists. Metadata must not declare create executable merely because the response schema changed. | No create shadow execution before qualification. |

### Command schemas, OpenAPI, and generated artifacts

| Repository location | Current dependency | Required migration | Deployment gate |
|---|---|---|---|
| `dish/dish_service/command_spec.py` | Defines create arguments and replay classification, but no command-specific result schema. | Keep create consequential/replay-bound. Add or reference a command-specific response contract rather than relying only on an untyped `data` object. | Before publishing the new Action schema. |
| `dish/dish_service/openapi.py` | Generic result schema exposes nullable top-level `task_gid`; `data` is open. | Define create success with required `data.dish_id`, optional nullable `url`, optional nullable `asana_task_gid`, and an explicit prohibition/example showing `task_gid` is not the Dish ID. | Before client/GPT deployment. |
| `dish/dish_pg/openapi.py` | Generates the PostgreSQL Action document from current PG command definitions. | Emit the same create schema and envelope semantics as the authoritative service OpenAPI; avoid a PG-only `task_id` response dialect. | Before exact first request. |
| `dish/openapi/dish-action.openapi.json` | Checked-in legacy Action schema with generic `task_gid`. | Regenerate after source schema changes; schema-oracle tests must prove exact synchronization. | Deploy before or with consumers that understand the new fields. |
| `dish/openapi/dish-postgresql-action.openapi.json` | Checked-in PG schema with generic result data. | Regenerate with required create contract and verify no Dish UUID example or description appears under `task_gid`. | Required before exact first request. |
| `dish/frontend/openapi/frontend.openapi.json` and generated frontend contract modules | No create response or canonical URL resolver is currently defined. | Add only the frontend identifier-resolution/read contract needed for configured `url`; do not duplicate command create authority in the browser API. | Required before the service is configured to emit non-null `url`. |

### Clients, command-line surfaces, and instructions

| Repository location | Current dependency | Required migration | Deployment gate |
|---|---|---|---|
| `dish/dish_service/client.py` | Strictly validates the generic envelope and exposes `task_gid`; generates request IDs for create. | Accept and validate command-specific `data.dish_id`; allow nullable URL/projection metadata; keep `task_gid` Asana-only. Return the unchanged result rather than synthesizing aliases. Also reconcile the two request-ID command sets while modifying this client. | Client must be forward-compatible before server switch. |
| `dish/dish_tool/cli.py` | Prints/passes through service command results and carries task context based on legacy identifiers. | Human and JSON output must label canonical `dish_id` distinctly and must not seed later commands by assigning it to `task_gid`. Follow-up commands need the new Dish identifier argument/resolver contract. | Before operators use PG create. |
| `dish/deploy/gpt-action.md` | Tells the GPT to find and use `task_gid`; reflects legacy create and currently calls `inspect` read-only. | Replace successful-create handling with `dish_id`/configured `url`; treat `asana_task_gid` as optional projection metadata; forbid using it as canonical identity; update inspect replay instructions consistently with the command contract. | Update with schema before the first live request. |
| external `~/honest-pantry/dish-custom-gpt-instructions.md` named by `CLAUDE.md` | Live custom-GPT instructions are outside the supplied archive and could still consume legacy `task_gid`. | Make the corresponding external-repository change, commit it there, paste the exact updated instructions into the GPT editor, and record the deployed revision. Exact contents are unresolved because the file was not supplied. | Hard deployment gate; repository-only edits cannot prove completion. |
| GPT Action editor configuration | Deployed schema/instructions are not repository state. | Import the regenerated schema, update instructions, save, and execute create/replay acceptance tests against the exact deployed Action. | Hard deployment gate before general admission. |
| `dish/README.md`, `dish/docs/runtime-contract.md`, and `dish/docs/architecture.md` | Operator/client guidance and current authority descriptions are centered on Asana task identity and legacy envelope semantics. | Document canonical Dish identity, optional URL, secondary projection identity, replay immutability, and the authority boundary. Update architecture in the implementation commit that actually changes authority. | Before production cutover documentation sign-off. |
| `dish/docs/gpt-natural-interaction-design.md` | Existing-task flows are built around `task_gid`; URL work discussed there is Asana URL parsing and partly deferred. | Separate canonical Dish URL resolution from the old Asana `task_url` discussion. Update create-success and existing-Dish decision trees without reviving deferred generic Asana URL parsing. | Before GPT instruction deployment. |

No generated standalone SDK was found. `DishServiceClient` is the known in-repository programmatic
client. Out-of-repository callers cannot be proven from this archive and must be checked through
release inventory and production access/telemetry before admission opens.

### Scripts and operational workflows

| Repository location | Current dependency | Required migration | Deployment gate |
|---|---|---|---|
| `dish/scripts/dish-pg-certify-shadow-worker-restart` | Exercises captured commands and shadow restart evidence; create remains subject to shadow policy. | Assert create remains capture-only unless correlation is qualified; when fixtures include results, distinguish source Asana GID from target Dish UUID. | Before changing create's dark-launch treatment. |
| `dish/scripts/dish-pg-host-capture-rehearsal` | Rehearses legacy capture/spool behavior whose snapshots use task GIDs. | Record identity role in evidence and prohibit target UUID substitution into legacy fields. | Before using rehearsal output as create migration evidence. |
| `dish/scripts/dish-pg-release` | First-admission planning accepts internal `task_id`; release evidence binds request/command/target. | For create, allow no pre-existing target and verify the committed result contains the new `dish_id`. Keep internal UUID column naming separate from public response naming. | Before reserving a create as first request. |
| release/cutover runbooks under `dish/docs/` | Existing steps certify schema, dark launch, first request, projection, and rollback gates but do not enumerate create consumers. | Add the ordered migration and exact deployed schema/instruction revisions to release evidence. | Before first-admission reservation. |

### Tests that consume or freeze the old response

The implementation change must update or add assertions in every applicable group below. Tests that
only pass `task_gid` into non-create commands need no rename unless their setup obtains that value from
create.

| Test group | Known files | Required assertions |
|---|---|---|
| Legacy create and placement | `dish/tests/test_dish_tool_step5_commands.py`, `dish/tests/test_asana_placement_lifecycle.py` | Legacy authority still returns a real Asana GID; no UUID reinterpretation; retirement boundary explicit. |
| End-to-end Action create | `dish/tests/test_action_full_lifecycle.py`, `dish/tests/test_action_surface_identifier_contract.py`, `dish/tests/test_action_surface.py` | Successful PG create yields required `dish_id`; clients use it or configured URL for subsequent canonical resolution; `task_gid` is null/Asana-only. |
| Service envelope and request identity | `dish/tests/test_service_foundations.py`, `dish/tests/test_request_identity.py`, `dish/tests/test_service_run_identity.py`, `dish/tests/test_request_replay_and_restore_durability.py`, `dish/tests/test_committed_success_boundaries.py` | Strict validation accepts new fields; exact replay returns same Dish UUID and original metadata after restart/restore/response loss; mismatched request reuse conflicts. |
| OpenAPI synchronization | `dish/tests/test_action_surface_openapi.py`, `dish/tests/postgresql/test_postgresql_action_openapi_oracle.py` | Checked-in schemas equal generators; create schema requires `dish_id`; optional fields are typed; no schema permits a Dish UUID as `task_gid`. |
| PostgreSQL create/transition | `dish/tests/postgresql/test_stage4_command_port.py`, `dish/tests/postgresql/test_stage5_transition_projection.py`, `dish/tests/postgresql/test_stage5_projection_recovery.py`, `dish/tests/postgresql/test_fail_closed_admission_outbox.py` | Internal task UUID is serialized as `dish_id`; canonical commit succeeds independently of projection; projection intent/attempt links back to the same Dish. |
| Native projection and reconciliation | PostgreSQL native projection-worker and reconciliation tests under `dish/tests/postgresql/native/` | Later successful projection creates a confirmed Asana mapping without mutating the original create outcome; ambiguous/failed attempts preserve canonical success. |
| Dark launch and translation | `dish/tests/postgresql/test_dark_launch_shadow_translation.py`, `dish/tests/postgresql/test_dark_launch_policy.py`, `dish/tests/postgresql/test_dark_launch_evidence.py`, `dish/tests/test_shadow_capture.py`, `dish/tests/test_shadow_spool.py` | Legacy `task_gid` and target `dish_id` retain distinct roles; create stays capture-only until correlation proof is accepted. |
| Service client transport | `dish/tests/test_service_clients_auth.py`, `dish/tests/test_transport_contract_resilience.py`, `dish/tests/test_dish_cli_transport_errors.py` | New create result passes strict client validation; malformed/missing `dish_id` fails closed; no compatibility alias is synthesized. |
| Frontend URL resolution | Existing/new frontend route, bootstrap, and contract tests | Configured URL resolves to the same Dish UUID; wrong environment/type, stale alias, malformed URL, or missing Dish fails closed; no raw UUID is silently treated as Asana GID. |

## Frontend Dish URL contract

The supplied frontend is fixture-oriented and currently routes protected detail views as
`GET /task/{task_id}`. The Gate B source map explicitly says the current read model has no
browser-safe task/section route-identity authority. Therefore the service must return `url: null`
until all of the following exist and are deployed:

1. one configured canonical frontend origin per environment;
2. a browser-safe route-identity codec or alias mapping backed by the canonical Dish UUID;
3. a protected route that resolves that identity to exactly one `dish_id` in the same environment;
4. normalization and negative tests for malformed, cross-environment, stale, and unknown identities;
5. an application URL builder that uses that same route grammar rather than string concatenation in
   command code;
6. a production configuration and deployment check proving the emitted URL resolves to the created
   Dish.

The exact route grammar and configuration variable are unresolved repository facts. Stage A must not
invent them in the create handler. An Asana task URL is not an acceptable fallback for `data.url`.

Known frontend locations requiring review when the resolver is implemented include
`dish/frontend/src/js/features/routing/routes.js`, `dish/frontend/src/js/boot.js`, the review catalog
and detail modules under `dish/frontend/src/js/review/`, `dish/docs/frontend.md`,
`dish/docs/frontend-imp.md`, `dish/docs/frontend-stage2-runtime-decisions.md`, and
`dish/docs/frontend-gate-b-source-map.md`.

## Ordered migration and deployment

The order prevents either response dialect from silently changing the meaning of an existing field.

1. **Land this contract and failing/characterization tests.** Freeze the legacy response and target
   result separately. Record all in-repository and known external consumers.
2. **Make consumers forward-compatible.** Update service client, CLI presentation/context, GPT
   instructions, scripts, and test helpers to recognize `dish_id`, `url`, and `asana_task_gid` while
   still accepting a legacy response with an Asana-only `task_gid` before cutover. No consumer may
   assign `dish_id` to a `task_gid` variable or argument.
3. **Publish compatible schemas before switching the producer.** Regenerate checked-in Action
   schemas, deploy the GPT Action schema/instructions, and verify strict clients accept both the
   legacy envelope and the new command-specific create data. Generic top-level `task_gid` remains
   nullable and Asana-only.
4. **Implement the PostgreSQL producer.** Serialize internal `DishTask.task_id` as required
   `data.dish_id`; commit the result with canonical rows and projection intent; emit URL and Asana GID
   only when their independent authorities have already confirmed them.
5. **Implement and deploy canonical URL resolution.** Until it passes environment/route tests and is
   configured, return null/omit `url`. Enable non-null URL only after a deployed URL resolves to the
   same Dish UUID.
6. **Verify projection independence.** Exercise delayed, failed, ambiguous, recovered, and reconciled
   Asana projection. Prove none changes canonical create success or the stored replay result.
7. **Run dark-launch and schema/contract certification.** Keep create capture-only unless exact
   source/target correlation is separately qualified. Record source release, OpenAPI release, GPT
   instruction revision, client revision, and frontend revision.
8. **Reserve and execute the exact first PostgreSQL request.** A create may be the first request only
   if the release tool supports a targetless create plan and every preceding consumer/deployment gate
   is satisfied. Verify exact replay, canonical reread, projection settlement, and independent Asana
   observation before opening admission.
9. **Retire legacy create only after general-admission evidence.** Do not remove legacy response code,
   aliases, or tests before the writer fence and rollback limits say legacy mutation is permanently
   retired. Then remove obsolete compatibility handling in a separately reviewable cleanup.

There is no permitted deployment step that temporarily sets `task_gid` to the Dish UUID.

## Exact unresolved implementation facts

1. No concrete public PostgreSQL service adapter currently maps `data.task_id` to approved
   `data.dish_id`.
2. The final common-envelope decision—always-present nullable `task_gid` versus command-specific
   omission—is not settled. Either is legal only while `task_gid` remains Asana-only.
3. The canonical Dish URL origin, route grammar, resolver table/codec, and configuration variable do
   not exist in the supplied repository state.
4. The supplied frontend has no accepted canonical Dish route-identity authority; Gate B records this
   as open.
5. The live external custom-GPT instruction file and editor configuration were not supplied, so their
   exact legacy references and deployed revision cannot be verified here.
6. No repository-wide manifest of out-of-repository HTTP clients exists. Access/telemetry or an
   operator-owned client inventory is required before general admission.
7. Create remains capture-only in dark launch because exact lost-response/source-target correlation
   is not qualified. The required correlation mechanism is unresolved.
8. The current PostgreSQL result includes `content_version_id` and `projection_event_id`. Whether
   these remain additional public fields or become private diagnostics is unresolved; they may not
   weaken or replace the three approved fields.
9. The command argument/identifier migration needed for follow-up commands to accept `dish_id` or a
   configured Dish URL is broader than response serialization and lacks a complete current target
   API mapping.
10. The release script's internal `task_id` field is legitimate database terminology, but the exact
    create-first-request input/output evidence format has not been implemented.

## Contradictions found in current code or documentation

1. Legacy `_step5_create` makes Asana creation authoritative and returns `task_gid`; approved Stage A
   makes PostgreSQL Dish creation authoritative and Asana secondary.
2. PostgreSQL `_create` returns public `task_id`, not the approved required field `dish_id`.
3. The generic OpenAPI schemas do not require any create-specific identity field and therefore permit
   a nominally successful create with no canonical Dish identifier.
4. Current GPT deployment instructions tell agents to find and use `task_gid`, with no canonical
   `dish_id` create-success path.
5. Current frontend routes use an opaque fixture/task identity while the frontend source map says no
   browser-safe canonical route-identity authority exists; returning a configured Dish URL now would
   be unproven.
6. Shadow translation accepts source `task_gid` or `task_id` as a generic task reference. Without an
   explicit identity role this can collapse Asana and Dish identities.
7. The PostgreSQL cutover registry treats create as retained while dark-launch policy correctly keeps
   it capture-only; command retention does not itself prove response correlation or shadow safety.
8. The service client has two differing request-ID command sets, and one omits `apply-proposal`; that
   unrelated-but-adjacent contradiction must not be copied into create migration logic.
9. Existing documentation about `task_url` primarily concerns Asana URL parsing. It cannot be reused
   silently as the configured canonical Dish URL contract.

## Repository searches used

The consumer inventory was derived with the following repository searches and direct inspections:

```text
rg -n "create|task_gid|task_id|dish_id|asana_task_gid" \
  dish/dish_tool dish/dish_service dish/dish_pg dish/dish_shadow
rg -n "create|task_gid|task_id|dish_id|url" \
  dish/openapi dish/frontend dish/deploy dish/docs dish/scripts dish/tests
rg -n "task_gid" dish --glob '*.py' --glob '*.md' --glob '*.json' --glob '*.sh'
rg -n "def _step5_create|def _create" dish/dish_tool/commands.py dish/dish_pg/command_port.py
rg -n "task_url|task_urls|frontend.*url|base_url" dish
```

Direct code inspection included the legacy and PostgreSQL create handlers, result envelope,
service/client replay paths, both OpenAPI generators and checked-in documents, shadow policy and
worker, release/capture scripts, frontend route decisions/source map, and create/replay/projection
contract tests. Search results were filtered to remove files that only use `task_gid` for unrelated
non-create commands.

## Acceptance checklist

- [x] `dish_id` is required, canonical, and replay-stable.
- [x] `url` is optional, configured, and required to resolve to the same Dish UUID.
- [x] `asana_task_gid` is optional secondary projection metadata.
- [x] `task_gid` is never allowed to contain a Dish UUID or silently change meaning.
- [x] Canonical create does not wait for or fail because of Asana projection.
- [x] Later projection success does not rewrite the first authoritative create outcome.
- [x] Every known runtime producer, adapter, client, schema, instruction, script, test group, and
  frontend consumer is inventoried with a required migration and gate.
- [x] External consumers that cannot be inspected from the archive are named as unresolved deployment
  gates rather than assumed compatible.
- [x] Deployment order keeps old and new meanings distinguishable and contains no UUID-in-`task_gid`
  bridge.
- [x] No production code or new workflow state is introduced by this document.

## Self-review

- **Field meaning:** every field has one identity domain; no compatibility field silently changes
  semantics.
- **Replay:** create replay preserves the original canonical outcome and cannot be enriched by later
  projection.
- **Failure paths:** URL absence, projection delay/failure/ambiguity, response loss, pre-commit failure,
  and malformed serialization are covered.
- **Inventory:** production code, schemas, deployed instructions, clients, scripts, tests, docs,
  frontend, and external gaps are included; unrelated task-GID users are excluded deliberately.
- **Deployment:** consumer compatibility precedes producer switch; frontend URL emission and general
  admission remain gated by mechanical evidence.
- **Cross-document consistency:** canonical authority, request permanence, projection independence,
  and committed-success behavior match the behavioral and command contracts.
