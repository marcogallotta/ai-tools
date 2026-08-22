# Implementation Action Gate A — real bidirectional file transport

Gate A qualifies the actual Dish GPT Action file seam before any Asana/GitHub provider parity or provider credentials are added. It exercises the same Dish Action HTTP/auth/OpenAPI/request-replay stack intended for the Implementation Action pilot; a standalone hash service does not count.

## Preconditions

- Gate A runs from the dedicated `Dish — Implementation` Custom GPT. GPT Actions are configured on the Custom GPT, not in ChatGPT Project settings. Each acting Dish role therefore needs its own Custom GPT Action configuration; do not expect Project tool discovery to install or expose this Action. Keep using the existing generated Implementation role kernel (`implementation.md`) as the instruction authority.
- Configure the Dish Action deployment used for this gate with `DISH_ACTION_CLIENT_ID=implementation-action` and its own dedicated Action bearer, separate from any deployment serving the default connected GPT. The service enforces this at the command boundary: `qualify-file-transport` rejects any other `action_client_id` with `AGENT_MISMATCH`/`action_client_not_authorized`. Do not configure Asana or GitHub provider credentials for this gate.
- Import the generated Development Workflow Implementation Action schema. It must contain exactly one operation for Gate A: `qualify-file-transport`. If any ordinary Dish workflow operation appears, the deployment is misconfigured; stop before importing or testing it.
- The `Dish — Implementation` Custom GPT must have Code Interpreter and its imported Action available together. Native GitHub/Asana Apps are not part of this gate.

## Fixture and call

Create a deterministic binary file in Code Interpreter and record its exact byte count and SHA-256. Start near 512 KiB. If that succeeds with useful latency headroom, repeat near 5 MiB. Do not infer a production ceiling from ChatGPT's general file-upload limit.

For each fixture, call `qualify-file-transport` with:

- one fresh stable `client.run_id` for the Gate A execution;
- one fresh `client.request_id` for the logical qualification request;
- `arguments.expected_sha256` equal to the locally computed lowercase SHA-256;
- `arguments.expected_bytes` equal to the exact local byte count;
- exactly one selected conversation/Code Interpreter file through `openaiFileIdRefs`.

The service treats the supplied temporary `download_link` as transient transport only. Its signed URL must not enter durable replay identity, logs, receipts, or returned errors. Durable request identity binds the stable OpenAI file id/name/MIME type plus expected digest and size.

## Required observations

A successful first call must prove all of the following:

1. Dish receives the selected file through the real Action route and fetches exactly the expected bytes.
2. Returned qualification metadata reports the same file id, digest, and byte count.
3. Dish returns `dish-action-gate-a-receipt.json` through `openaiFileResponse`.
4. Code Interpreter can read that returned file and independently verify its SHA-256 and JSON contents.
5. The stored Dish request is owned by the `implementation-action` principal/run identity.
6. No provider backend is required for the command.

Record observed end-to-end latency and fixture size. The repository command enforces a 10,000,000-byte Gate A ceiling and a bounded server-side fetch time; those are safety bounds, not evidence that every size below the ceiling is operationally suitable.

## Replay / response-loss proof

For an exact replay, reuse the same request id, run id, stable file identity, digest, and size. The platform may supply a newly signed `download_link`; that is expected. Dish must return the stored authoritative result and receipt without fetching the file again.

Repeat the exact replay after a Dish service restart. Changed digest, size, stable file identity, run identity, or owner under the same request UUID must conflict before another file fetch.

If a transport response is lost or the service cannot establish a safe result, do not rotate request identity merely to force another effect. Preserve the exact request for reconciliation under the existing Dish replay contract.

## Gate decision

**GO** requires reliable byte-identical input and returned-file output on the actual `Dish — Implementation` Custom GPT Action path, exact replay across a rotated signed URL and service restart, coexistence with Code Interpreter and the role instructions, and measured latency/headroom sufficient for the intended publication-bundle range.

**NO-GO** if either file direction is absent/unreliable, exact bytes cannot be proven, replay depends on re-fetching the ephemeral URL, the intended Custom GPT cannot combine the required capabilities, or practical transfer latency makes the seam unsuitable.

Do not work around a NO-GO by adding an async queue, model-mediated blob/chunk/base64 transport, Connector/local fallback inside the Action run, or provider credentials. Stop the Action pilot and return the transport question to research/operator decision. Gate B/C provider work remains blocked until Gate A is a GO.
