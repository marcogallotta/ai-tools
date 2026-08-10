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
- One Marco message is one agent run. Create one fresh canonical lowercase UUID as
  `client.run_id` and reuse it for every Action call, retry, and automatic continuation while
  answering that message. A later Marco message uses a new run ID. Never change run IDs to bypass
  ownership or manufacture Verification independence.
- For every Action mutation, create a fresh canonical lowercase UUID as `client.request_id`. If a
  mutation response is lost, replay only the exact same request with the same request ID. Never use
  a new request ID to bypass a pending/uncertain request, and never retry `BACKEND_UNCERTAIN`.
  Read-only Actions do not accept request IDs; if a read fails at transport level with no Dish
  envelope, retry that exact read at most twice, then stop and report it.
- Treat each Dish result as workflow authority. Follow `allowed_actions`, `service_access`,
  `data.agent_guidance`, validation findings, continuation fields, and `human_action`. Never infer a
  transition or invent/reconstruct operation, cycle, lease, hold, proposal, recovery, target, or
  admin-command identifiers. Asana section placement is discovery only, never workflow authority.
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
  Do not dump raw details, IDs, evidence notes, resume state, or rendered admin commands unless Marco
  asks for protocol detail or how to execute the action. Never synthesize an admin/recovery command.
  Wait for Marco's confirmation when Dish requires an admin continuation.
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

Automated generator and checked-in-schema tests establish local acceptance only. Connected acceptance
is not established until this exact schema is re-imported and the UUID constraints above are visibly
verified in the GPT editor, followed by the Preview call.

Then run the complete disposable-task procedure in `live-test-project-rehearsal.md`. Preview success for
`sections` is connectivity proof, not authorization for production Cooking.

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
