# GPT Action setup and operating contract

The GPT Action is a bounded client of the shared Dish service. It uses only the public Action
listener, the trimmed runtime OpenAPI document, and its dedicated bearer token. It never receives an
Asana, CLI, or admin credential.

## Preconditions

Before opening the GPT editor:

1. Start the service with the test-project configuration in `../README.md`.
2. Confirm private `GET /health` is healthy.
3. Configure and verify the Tailscale mappings in `tailscale/README.md`.
4. Confirm the public listener returns 404 for health, CLI, admin, backup, migration, and recovery
   routes.
5. Confirm only the Action token succeeds on public `/v1/action/sections`.

## Editor configuration

Import the runtime schema from:

```text
https://laptop.tail46f0b9.ts.net:8443/openapi/action.json
```

The imported schema must retain that exact `https` server, including port `8443`. Configure API-key
authentication so requests use:

```text
Authorization: Bearer <DISH_SERVICE_ACTION_TOKEN>
```

Store only the dedicated Action token in the GPT. Do not enter `DISH_SERVICE_AGENT_TOKEN`,
`DISH_SERVICE_ADMIN_TOKEN`, `ASANA_PAT`, or `ASANA_ENV`.

The checked-in `openapi/dish-action.openapi.json` is a reviewable fallback with an intentionally
invalid placeholder server. Prefer the runtime schema so the server is generated from the actual
Funnel host.

## Instructions for the GPT

Add an operating instruction with all of these requirements:

- Act as agent `gpt` and pass `agent: gpt` whenever the operation schema accepts an agent.
- Create one unique `client.run_id` for the current agent run and reuse it for every Action call and
  lease renewal in that run. A genuinely new run uses a new value.
- Treat `client.run_id` as service lease ownership, not as proof of independent Verification.
  Where `start` accepts a workflow `run_id`, supply the actual platform run identity when available;
  otherwise use the protocol's explicit independence attestation route. For `approve` and `reject`,
  this workflow proof is mandatory: send either the exact `arguments.run_id` recorded when
  Verification started or its exact `arguments.independence_attestation`. Reusing only
  `client.run_id` does not supply verifier proof.
- Follow only the returned `allowed_actions`. Do not reconstruct workflow transitions from
  conversation history.
- Treat `file_text` as the complete candidate. Never send a partial patch or assume that the service
  can read a local file.
- A tool pass proves deterministic conformance only; complete the semantic work required by the
  stage protocol returned by Dish.
- After successful Verification approval returns `submit`, call `submit` in the same pass.
- Never retry `BACKEND_UNCERTAIN`, steal an expired lease, call a private/admin route, or repair an
  Asana task directly. Stop and give Marco the complete result.

The canonical result meanings and retry rules remain in `../docs/runtime-contract.md`; do not copy a
second result-code policy into the GPT instructions.

## Lease handling

The default lease lasts 30 minutes. If work on an active operation approaches that limit, renew the
operation lease with the same `client.run_id`. A handoff may release the actor lease while leaving
the task operation active; follow the returned actions rather than renewing after handoff.

If a lease expires, stop. Only Marco may use `dish-admin recover-lease`; the GPT must not create a
replacement operation or change its run identity to bypass ownership.

## Preview gate

Before any task mutation:

1. In GPT Preview, call `sections`.
2. Confirm the result is the canonical JSON envelope with `code: OK`.
3. Confirm `data.project_gid` is the test project `1216693403164366`.
4. Confirm the returned Research and Verification queue GIDs match the live test project.
5. Confirm the Preview request succeeds through `:8443`, not the private `:8444` endpoint.
6. Review the GPT configuration and confirm no CLI, admin, or Asana secret is present.

Then run the complete disposable-task procedure in `live-test-project-smoke.md`. Preview success for
`sections` is connectivity proof, not authorization for production Cooking.

## Token rotation

Tokens are rotated manually:

1. Generate a new high-entropy Action token outside the repository.
2. Replace only `DISH_SERVICE_ACTION_TOKEN` in the owner-only service environment.
3. Restart `dish-service`; this immediately invalidates the old Action token.
4. Replace the GPT Action credential with the new token.
5. Repeat the Preview gate.

Never commit a populated token or paste one into test transcripts. Rotate the CLI and admin tokens
separately; they must never be substituted into the Action configuration.
