# Dish MCP app setup and qualification

This is the additive authenticated MCP transport for the PostgreSQL-authoritative Dish backend. It
adds a ChatGPT-facing OAuth resource-server boundary to the existing MCP app; it does not change
Dish command semantics, workflow authority, replay identity, or the existing loopback Action
listener. Keep the current GPT Action/Funnel available until every qualification item below passes.

The path is:

```text
ChatGPT Project -> custom Dish MCP app -> OpenAI Secure MCP Tunnel
  -> private loopback Streamable HTTP dish_service.mcp_server
  -> loopback Dish Action listener
```

Dish is only an OAuth 2.1/OIDC **resource server** in this path. The external authorization server
signs users in, issues access/refresh tokens, and publishes its browser/discovery endpoints. Dish
never stores passwords, mints OAuth tokens, or becomes a second identity provider.

The MCP server exposes exactly the current 18 PostgreSQL Action commands as `dish_<command>` tools.
It projects tool schemas and canonical result envelopes from
`dish_pg.openapi.postgres_action_openapi()` and does not add auth fields to any tool input. The
incoming OAuth bearer is consumed only by the MCP HTTP auth layer and is never forwarded to
`/v1/action`. `DISH_MCP_ACTION_TOKEN` remains the independent server-side bearer for that private
loopback hop.

## External authorization-server requirements

Choose the real external OAuth/OIDC provider before qualification. A provider/configuration is not
acceptable for this transport unless all of these hold:

- its issuer, authorization/token endpoints, and JWKS are externally reachable over HTTPS by the
  ChatGPT OAuth flow;
- it issues signed JWT access tokens with `exp`, `iss`, a stable OAuth client identity in `client_id`
  or `azp`, and a standard `scope` string (or provider-style `scp`);
- the token is targeted at this MCP resource by either an exact `resource` claim or `aud` containing
  the MCP resource identifier/configured audience;
- the ChatGPT client can obtain `dish:connected` and the provider supports the refresh/reconnect flow
  used for the app (including refresh/offline access when the provider requires that scope);
- its JWKS and issuer semantics are stable enough to fail closed. Do not disable issuer, signature,
  expiry/not-before, target, or scope checks to accommodate a provider mismatch.

`DISH_MCP_RESOURCE_URL` is the exact HTTPS OAuth resource identifier for the ChatGPT-visible MCP
resource and must end in `/mcp`. It is an identifier advertised in Protected Resource Metadata; it
is **not** a public Dish listener. The process still binds only to loopback. If the selected
ChatGPT/tunnel setup does not provide a stable HTTPS resource identifier that the OAuth client can
use, qualification is blocked rather than falling back to unauthenticated MCP.

## Private runtime configuration

Use a current `tunnel-client` from Platform tunnel settings or the latest public
`openai/tunnel-client` release. Start qualification against TEST. Create
`/home/marco/.config/dish-service/mcp-tunnel.env` mode 0600:

```sh
CONTROL_PLANE_API_KEY=replace-with-tunnel-runtime-key

DISH_MCP_BIND_HOST=127.0.0.1
DISH_MCP_BIND_PORT=8765
DISH_MCP_RESOURCE_URL=https://replace-with-exact-chatgpt-resource.example/mcp
DISH_MCP_OAUTH_ISSUER=https://replace-with-external-issuer.example
DISH_MCP_OAUTH_JWKS_URL=https://replace-with-external-issuer.example/.well-known/jwks.json
# Set only when the provider's access-token audience is not the exact resource URL.
# DISH_MCP_OAUTH_AUDIENCE=provider-specific-resource-audience

DISH_MCP_ACTION_URL=http://127.0.0.1:8766
DISH_MCP_ACTION_TOKEN=replace-with-test-action-token
```

The MCP process rejects non-loopback bind hosts and non-loopback Action URLs. `CONTROL_PLANE_API_KEY`
only authorizes the Secure MCP Tunnel control plane and cannot satisfy the Dish MCP bearer check.
Neither the control-plane key, OAuth bearer, nor Action bearer belongs in the tunnel profile, app
description, Project instructions, repository, tool arguments, result envelopes, or support logs.

Install/update the Python environment through the repository's normal dependency path; the only new
direct runtime dependency for this boundary is the official `mcp` Python SDK pinned in
`requirements.txt`.

## Start the private MCP server and create the tunnel profile

Load the environment, start the private MCP resource server, then create the tunnel profile using the
HTTP/OAuth-aware sample. Secure MCP Tunnel targets the loopback URL; it no longer launches Dish as a
stdio child.

```sh
set -a
. /home/marco/.config/dish-service/mcp-tunnel.env
set +a

cd /home/marco/ai-tools/dish
/home/marco/ai-tools/dish/.venv/bin/python -m dish_service.mcp_server
```

In a second shell with the same environment loaded:

```sh
/home/marco/.local/bin/tunnel-client init \
  --sample sample_mcp_with_dcr \
  --profile dish-mcp \
  --tunnel-id tunnel_REPLACE_ME \
  --mcp-server-url "http://127.0.0.1:8765/mcp"

/home/marco/.local/bin/tunnel-client doctor --profile dish-mcp --explain
/home/marco/.local/bin/tunnel-client run --profile dish-mcp
```

For this authenticated server, `doctor` reaching `/mcp` and observing the normal OAuth protected-
resource challenge is success, not an auth failure. It must also resolve the Protected Resource
Metadata/discovery path. A 401 without a standards-compliant `WWW-Authenticate` pointer, malformed
Protected Resource Metadata, unreachable external authorization-server metadata/JWKS, or unexpected
5xx is a configuration/provider failure and blocks connected qualification.

The expected unauthenticated shape is:

```text
HTTP 401
WWW-Authenticate: Bearer ... resource_metadata="https://.../.well-known/oauth-protected-resource/mcp"
```

and the metadata must advertise the exact MCP resource, the configured external issuer, and
`dish:connected`. A token with a valid signature but without `dish:connected` must get HTTP 403.
Invalid signature, expiry/not-before, issuer, or resource/audience must get HTTP 401 before an MCP
tool executes.

## Configure the ChatGPT app

In the ChatGPT app/tunnel setup, select the Secure MCP Tunnel and configure OAuth against the same
external authorization server and exact MCP resource identifier used above. Use a provider-issued
OAuth client registration/credentials appropriate for ChatGPT; do not create a Dish authorization
server or reuse `CONTROL_PLANE_API_KEY`/`DISH_MCP_ACTION_TOKEN` as an OAuth credential.

The authorization request must be able to obtain `dish:connected` plus any provider-specific scope
needed for refresh/offline access. Complete a browser sign-in, disconnect/reconnect, and an actual
access-token expiry/refresh cycle. If the provider cannot complete that flow with the ChatGPT app,
change provider/client configuration; do not weaken Dish token validation.

## Connected qualification

Do not retire or alter the GPT Action route until all of these are true for the authenticated MCP
app:

1. An unauthenticated `initialize`, `tools/list`, and `tools/call` each fail at HTTP with 401 plus the
   Protected Resource Metadata `WWW-Authenticate` pointer before tool execution. A valid token
   missing `dish:connected` fails with 403.
2. Invalid signature, expired/not-yet-valid token, wrong issuer, and wrong resource/audience each
   fail with 401. Possessing only `CONTROL_PLANE_API_KEY` cannot authorize an MCP request.
3. After browser OAuth, `tools/list` shows exactly these 18 tools: `dish_create`, `dish_sections`,
   `dish_section_tasks`, `dish_search`, `dish_cook_logs`, `dish_record_cook_log`, `dish_read`,
   `dish_proposals`, `dish_apply_proposal`, `dish_safe_reclaim`, `dish_inspect`, `dish_start`,
   `dish_prepare`, `dish_approve`, `dish_reject`, `dish_submit`, `dish_renew_lease`, and
   `dish_cooked`.
4. A regular Project performs a representative authenticated TEST read and receives the canonical
   Dish envelope. Confirm the incoming OAuth bearer is absent from tool inputs, structured content,
   result text, MCP/application logs, and captured support evidence.
5. A TEST replay-bound mutation preserves one stable `client.run_id`, one fresh
   `client.request_id` for the logical mutation, and the exact same pair on an intentional exact
   replay. The replay must return the authoritative stored envelope rather than perform a second
   mutation. The OAuth caller identity must not replace either Dish product-protocol identity.
6. A regular Project completes one real continuation/challenge path using only identifiers,
   `allowed_actions`, guidance, and continuation data returned by Dish. A canonical `ok:false`
   response remains a normal MCP tool result, not a transport/auth error.
7. Let an access token expire and prove refresh/reconnect obtains a fresh valid token without
   changing the Dish run/request replay contract.
8. A Pro live Project completes at least one approved Dish mutation through the authenticated MCP
   app. For that production check, change only `DISH_MCP_ACTION_URL` to
   `http://127.0.0.1:8776` and `DISH_MCP_ACTION_TOKEN` to the production Action token, restart the
   private MCP service, and re-run `doctor` before the call. Keep the OAuth issuer/resource/scope
   boundary unchanged.
9. Re-check connected-agent behavior: stable run identity, fresh mutation request identity, exact
   retry identity, canonical identifier discipline, `data.agent_guidance`/`human_action`, and
   independent Verification run separation.

Any mismatch in auth enforcement/discovery, command inventory, request schema, result envelope,
identity/replay behavior, continuation authority, or refresh/reconnect blocks retirement and returns
to this bounded MCP-auth PR. Do not redesign the backend or paper over a mismatch in Project
instructions.

## Run as production services

After authenticated MCP qualification is green, install both checked-in units. The MCP process owns
the loopback HTTP listener; the tunnel depends on it and forwards the private target to ChatGPT.

```sh
sudo install -m 0644 deploy/systemd/dish-mcp.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/dish-mcp-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dish-mcp.service dish-mcp-tunnel.service
/home/marco/.local/bin/tunnel-client doctor --profile dish-mcp --explain
systemctl status dish-mcp.service dish-mcp-tunnel.service --no-pager
```

The services run alongside the existing Action transport. This auth change deliberately does not
disable the old GPT Action, Caddy/Tailscale routing, or Funnel. Retire those only in the separately
qualified cleanup step after every connected check above remains green. MCP-native v2 consumes this
authenticated shell later; it must not redesign or weaken this OAuth boundary.
