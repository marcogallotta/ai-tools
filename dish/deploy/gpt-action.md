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
https://laptop.tail46f0b9.ts.net/openapi/action.json
```

The imported schema must retain that exact `https` server with no non-default port. Configure
API-key authentication so requests use:

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

- Use the machine identifier `agent: gpt` whenever the operation schema accepts an agent; Dish
  renders that identifier as the human-readable actor name `Custom GPT`.
- Create one non-nil canonical lowercase UUID as `client.run_id` for the current agent run and reuse
  it for every Action call and lease renewal in that run. A genuinely new run uses a new UUID.
- Before every mutation—`create`, `start`, `prepare`, `approve`, `reject`, `submit`, and lease
  renewal—create a new non-nil canonical lowercase UUID as `client.request_id` and preserve it with
  the attempted call. Read-only `sections`, `read`, and `inspect` do not accept a request ID. Dish
  binds the first authoritative success or expected failure to the exact command, canonical
  arguments, authenticated owner, and run. If the response is lost, repeat only that exact call with
  the same UUID; a completed replay returns the stored result with `data.request_replayed: true`.
  Reusing the UUID for different work conflicts. A matching pending or uncertain request is not
  executed again, so never generate a new UUID merely to bypass that outcome.
- If read-only `sections`, `read`, or `inspect` returns no Dish JSON envelope because of a
  transport-level client error, retry the exact same read up to two times. If it still fails, stop
  and report the error. This bounded read retry does not apply to mutations; after a lost mutation
  response, replay only the exact call with its original `client.request_id`.
- The authenticated `client.run_id` is both lease ownership and the durable agent-run identity. The
  service applies it to `start`, `prepare`, `approve`, and `reject`; do not invent a separate
  workflow run ID. A redundant `arguments.run_id`, when supplied, must match it exactly.
- Independent Verification requires that run ID to differ from the run that constructed or last
  materially edited the candidate. A new operation ID, cycle ID, actor/model identity, or
  `independence_attestation` does not establish independence. A non-blank attestation is required as
  supplementary audit context only on Verification start, and it cannot replace `client.run_id`.
  Approval and every rejection route — including Large — inherit the exact persisted start
  attestation automatically and do not accept the field; never send `independence_attestation` on
  `reject`.
- Follow only the returned `allowed_actions`. A completed cross-stage handoff names `start` plus
  `data.required_start_kind`; pass that exact value as `arguments.kind` and do not reopen the terminal
  prior operation. In particular, Planning → Research returns `required_start_kind: initial`: call
  `start` with `kind: initial` to begin Research, and never start another Planning operation.
  After Verification `start`, call `inspect` before making an approval or rejection decision. Do not
  reconstruct workflow transitions from conversation history.
- Treat `file_text` as the complete candidate. Never send a partial patch or assume that the service
  can read a local file. For approval with `correction: none`, omit `file_text`: Dish signs the exact
  inspected candidate. For `correction: small`, supply the complete corrected candidate as
  `file_text`. `approve` never accepts `correction: large`. A Large correction is never sent through
  `approve`: call `reject` with `route: large`, `file_text` (the complete corrected candidate), and
  `reason` instead.
- A tool pass proves deterministic conformance only; complete the semantic work required by the
  stage protocol returned by Dish.
- After successful Verification approval returns `submit`, call `submit` in the same pass.
- Never retry `BACKEND_UNCERTAIN`, steal an expired lease, call a private/admin route, or repair an
  Asana task directly. An exact transport replay with the original `client.request_id` is allowed only
  when no result was received; once Dish returns `BACKEND_UNCERTAIN`, stop and give Marco the complete
  result.

The canonical result meanings and retry rules remain in `../docs/runtime-contract.md`; do not copy a
second result-code policy into the GPT instructions.

## Lease handling

The default lease lasts 30 minutes. If work on an active operation approaches that limit, call
`dish_renew_lease` with the operation UUID in `arguments.operation_id`, the same `client.run_id`, and
a fresh `client.request_id`. Do not supply the operation UUID as a top-level or path parameter. A
handoff may release the actor lease while leaving
the task operation active; follow the returned actions rather than renewing after handoff.

If a lease expires, stop. Only Marco may use `dish-admin recover-lease`; the GPT must not create a
replacement operation or change its run identity to bypass ownership.

## Preview gate

Before any task mutation:

1. In GPT Preview, call `sections`.
2. Confirm the result is the canonical JSON envelope with `code: OK`.
3. Confirm `data.project_gid` is the test project `1216693403164366`.
4. Confirm the returned Research and Verification queue GIDs match the live test project.
5. Confirm the Preview request succeeds through the standard HTTPS URL, not the private `:8444`
   endpoint.
6. Review the GPT configuration and confirm no CLI, admin, or Asana secret is present.
7. Inspect every imported operation and visibly confirm `client.run_id` is constrained as a
   non-nil canonical lowercase UUID; for `create`, `start`, `prepare`, `approve`, `reject`, `submit`,
   and `renew-lease`, also confirm `client.request_id` is required and has the same UUID constraints.

Automated generator and checked-in-schema tests establish local acceptance only. Connected acceptance
is not established until this exact schema is re-imported and the UUID constraints above are visibly
verified in the GPT editor, followed by the Preview call.

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
