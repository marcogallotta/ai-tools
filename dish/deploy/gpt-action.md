# GPT Action setup and operating contract

The GPT Action is a bounded client of the shared Dish service. It uses only the public Action
listener, the trimmed runtime OpenAPI document, and its dedicated bearer token. It never receives an
Asana, CLI, or admin credential.

## Preconditions

Before opening the GPT editor for the test rehearsal:

1. Start both environment services and the Caddy Action router as described in `../README.md`.
2. Confirm both private services are healthy and `dish-action-route status` reports both fixed routes ready.
3. Configure and verify the Tailscale mappings in `tailscale/README.md`.
4. Confirm the public listener through Caddy returns 404 for health, CLI, admin, backup, migration,
   and recovery routes.
5. Confirm only the production Action token succeeds on public `/v1/action/sections`.
6. Confirm only the TEST Action token succeeds on public `/test/v1/action/sections`.

## Editor configuration

Import one runtime schema per Custom GPT:

```text
PROD: https://laptop.tail46f0b9.ts.net/openapi/action.json
TEST: https://laptop.tail46f0b9.ts.net/test/openapi/action.json
```

The production schema must retain the root `https` server and the TEST schema must retain the
`https://laptop.tail46f0b9.ts.net/test` server. The fixed Caddy paths select the environment; CLI
`--profile` selection does not affect either GPT.
Configure API-key authentication so requests use:

```text
Authorization: Bearer <DISH_SERVICE_ACTION_TOKEN>
```

Store only the dedicated Action token in the GPT. Do not enter `DISH_SERVICE_AGENT_TOKEN`,
`DISH_SERVICE_ADMIN_TOKEN`, `ASANA_PAT`, or `ASANA_ENV`.

The checked-in `openapi/dish-action.openapi.json` is a reviewable fallback with an intentionally
invalid placeholder server. Prefer the runtime schema so the server is generated from the actual
Funnel host.

## Instructions for the GPT

Keep the permanent GPT contract small. Dish returns state-specific operating guidance in
`data.agent_guidance`; follow that guidance when it appears rather than relying on remembered
workflow procedure.

- Use `agent: gpt` wherever an Action accepts an agent.
- **Marco override rule:** when Marco explicitly uses the standalone word `override` to direct the
  current request, that instruction overrides conflicting connected-agent operating guidance in these
  permanent instructions for that message. If Marco's requested call is representable by the imported
  Action schema, attempt those Action arguments/identity exactly rather than substituting a supposedly
  safer protocol route. `override` does not make a disallowed transition legal or fabricate Dish
  authority: the imported schema and Dish runtime still enforce authorization, revocation, idempotency,
  and workflow legality, and Dish's returned envelope remains authoritative. If Dish rejects the
  requested action, report that rejection. The override applies only to the message that invokes it.
- `client.run_id` identifies the actual connected-agent run/principal, not a Marco-message boundary.
  Create a fresh canonical lowercase UUID when beginning a genuinely fresh agent run, and keep it
  stable for every Action call and automatic continuation performed by that same run. Do not rotate a
  run ID merely because Marco sent another message, and do not preserve an old run ID merely because
  the work is conversationally related if a genuinely new agent run has begun. Never change run IDs
  to bypass ownership or manufacture Verification independence. Exact transport replay of one
  logical request always preserves the original run ID and, when present, request ID. If Marco
  explicitly invokes `override` and instructs you to reuse an existing run ID for a retry/test
  continuation, reuse exactly that run ID and let Dish decide whether it remains authoritative.
- For every Action whose imported schema requires `client.request_id`, create a fresh canonical
  lowercase UUID for one logical call. This includes `inspect`: Verification inspection records
  durable evidence even though its operator purpose is observational. If no Dish envelope is received
  because of a transport/client failure (for example `ClientResponseError`, timeout, or connection
  reset), do not issue repeated automatic retries in the same assistant/tool loop when real elapsed
  delay cannot be guaranteed. Preserve the exact logical request unchanged: the same `client.run_id`,
  the same `client.request_id` when present, the same command, and the same arguments. Retry only at a
  genuine later opportunity after real elapsed time, reusing that exact logical identity. If real
  elapsed delay cannot be guaranteed now, report concisely that the call failed before a Dish envelope
  and preserve the exact call for the next genuine retry opportunity; do not hammer the Action or
  report that retries were exhausted. As soon as any Dish envelope is received, stop transport retry
  behavior and follow Dish authority. Never blindly retry `BACKEND_UNCERTAIN`, and never rotate request
  or run IDs merely to escape a failed or pending call. Do not invent a server-side sleep/timing Action
  to manufacture delay. Truly read-only Actions that omit request IDs follow the same no-same-turn
  retry rule and retain the same run ID.
- Treat each Dish result as workflow authority. Follow `allowed_actions`, `service_access`,
  `data.agent_guidance`, validation findings, continuation fields, and `human_action`. Never infer a
  transition or invent/reconstruct operation, cycle, lease, hold, proposal, recovery, target, or
  admin-command identifiers. Asana section placement is discovery only, never workflow authority.
- A canonical `dish <uuid>` from Marco is authoritative identity, not an operation/submission ID.
  Resolve it first with `read(dish_id=<uuid>)`, verify the returned `data.identity_binding`, and use
  only the exact task/operation identifiers Dish subsequently returns. Never pass a Dish UUID as
  `submission_id`, and never resolve a supplied Dish UUID by browsing sections, matching titles, or
  choosing a semantically similar task. If `read(dish_id=...)` cannot resolve it, stop rather than
  guessing.
- Planning authorization must be real. The first Planning `start` deliberately returns a challenge.
  Confirm it with `intent_basis: user_requested` only when Marco explicitly requested Planning for
  that exact task; otherwise ask him or use the explicit agent-override route with a real reason.
  Discussion or task legality alone is not authorization.
- Independent Verification requires a genuinely different run from the run that constructed or last
  materially edited the candidate. New operation/cycle IDs or an attestation do not create
  independence. Use abandonment continuation targets only when Dish explicitly returns the exact
  target pair.
- When an Action requires candidate text, send the complete exact candidate, never a partial patch
  or assumed local file. Follow the imported Action schema for correction-specific argument shapes.
- When Dish returns `human_action`, keep Marco-facing output compact: state the decision/action first,
  quantify any material threshold blocker, then give the simplest available options and consequence.
  Treat prompt labels and command placeholders as templates, not as Marco's answer. If the required
  answer/detail is blank, omitted, or still represented by a placeholder, ask Marco for the missing
  value; never construct `--detail ''`, an empty equivalent, or pretend an unanswered prompt supplied
  authority. Do not dump raw details, IDs, evidence notes, resume state, or rendered admin commands
  unless Marco asks for protocol detail or how to execute the action. Never synthesize an
  admin/recovery command. Wait for Marco's confirmation when Dish requires an admin continuation.
- When authoring a Verification Evidence or Human Review question for persistence, ask the real
  Marco-facing question in ordinary language. Include the concrete fact or uncertainty he must decide
  and the decision-relevant consequence; for Human Review, offer concrete plausible choices best-first
  with the recommended route first when the Action contract accepts choices. Avoid route names,
  resume-state vocabulary, abstract "signability", or other protocol jargon unless that protocol fact
  is itself what Marco must decide. Do not omit facts that are actually required to make the decision.
- A deterministic tool pass is not the semantic stage work. Complete the semantic work required by
  the routed Dish protocol, while letting Dish's current response determine the legal continuation.

State-specific procedures such as pagination, handoffs, proposal application, batch continuation,
submission, holds, and lease/recovery handling belong in the Action schema or the Dish response, not
in permanent GPT instructions.

## Preview gate

Before any task mutation:

1. Run `dish-action-route status` and confirm both fixed routes are ready.
2. In GPT Preview, call `sections`.
3. Confirm the result is the canonical JSON envelope with `code: OK`.
4. Confirm `data.project_gid` matches the GPT: TEST is `1216693403164366` and production is
   `1217084805070730`.
5. Confirm the returned Research and Verification queue GIDs match that same project.
6. Confirm the Preview request succeeds through the standard HTTPS URL, not either private endpoint
   (`:8444` for test or `:8445` for production).
7. Confirm a second `dish-action-route status` read still reports both routes ready.
8. Review the GPT configuration and confirm no CLI, admin, or Asana secret is present.
9. Inspect every imported operation and visibly confirm `client.run_id` is constrained as a
   non-nil canonical lowercase UUID; for `create`, `start`, `inspect`, `prepare`, `approve`, `reject`, `submit`,
   and `renew-lease`, also confirm `client.request_id` is required and has the same UUID constraints.
10. Confirm imported `dish_read` accepts exactly one identity: either canonical `dish_id` or exact
    `task_gid`. A Marco-supplied Dish UUID must be representable directly; section/task browsing is
    not an acceptable substitute.

Automated generator and checked-in-schema tests establish local acceptance only. Connected acceptance
is not established until this exact schema is re-imported and the UUID constraints above are visibly
verified in the GPT editor, followed by the Preview call. Whenever the Action schema changes, refresh
or re-import the TEST GPT Action before interpreting connected-GPT failures as backend defects.

After every Action-schema refresh, run this minimal TEST contract check before broader rehearsal:

1. Choose a TEST Dish already known to Dish. Call connected `dish_read` with its canonical `dish_id`
   and confirm `data.identity_binding` returns that same Dish UUID plus the exact task GID, without
   discovering the task through section/title matching.
2. Start a fresh Verification run through the connected TEST Action for that exact returned task GID
   with a fresh `client.run_id` and `client.request_id`.
3. Confirm the result succeeds and `allowed_actions` contains `inspect`.
4. Confirm the imported `dish_inspect` operation visibly accepts and requires both `client.run_id` and
   `client.request_id`.
5. Call public `dish_inspect` with the same run ID and a new unique request ID; it must succeed rather
   than fail because the public schema cannot represent the runtime request.
6. If desired, replay that exact inspect with the same request ID to confirm idempotent recovery. If
   the imported schema makes request ID required, schema-level rejection of an omitted request ID is
   sufficient; do not fabricate a lower-level bypass merely to exercise runtime rejection.

Classify inability to represent step 5 as a stale/incomplete connected Action schema and re-import it;
do not route around the public contract. Then run the complete disposable-task procedure in
`live-test-project-rehearsal.md`. Preview success for `sections` is connectivity proof, not authorization
for production Cooking.

## Token rotation

Tokens are rotated manually:

1. Generate a new high-entropy Action token outside the repository for the environment being rotated.
2. Replace only `DISH_SERVICE_ACTION_TOKEN` in that environment's owner-only `test.env` or `prod.env`;
   the TEST and production values must remain distinct.
3. Restart only the matching `dish-service-test.service` or `dish-service-prod.service`. The router
   does not hold either token and does not need a restart.
4. Replace the matching Custom GPT Action credential with the new token.
5. Repeat that GPT's Preview gate, including both route-status reads.

Never commit a populated token or paste one into test transcripts. Rotate the CLI and admin tokens
separately; they must never be substituted into the Action configuration.
