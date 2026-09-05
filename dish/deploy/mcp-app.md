# Dish MCP app setup and qualification

This is the additive Phase 1 transport for the PostgreSQL-authoritative Dish backend. It does not
change Dish command semantics, workflow authority, replay identity, or the existing loopback Action
listener. Keep the current GPT Action/Funnel available until every qualification item below passes.

The path is:

```text
ChatGPT Project -> custom Dish MCP app -> OpenAI Secure MCP Tunnel
  -> local stdio dish_service.mcp_server -> loopback Dish Action listener
```

The MCP adapter exposes exactly the current 18 PostgreSQL Action commands as `dish_<command>` tools.
It never exposes private admin commands or the retired `qualify-file-transport` route. Tool input
schemas and output envelopes are projected from `dish_pg.openapi.postgres_action_openapi()` at
runtime rather than copied into a second schema.

## Prerequisites

1. Use a current `tunnel-client` from Platform tunnel settings or the latest public
   `openai/tunnel-client` release. Do not pin this runbook to a historical binary URL.
2. Create a Secure MCP Tunnel and associate it with the Platform organization and ChatGPT workspace
   that will use it. The operator needs Tunnels Read + Use; tunnel management needs Read + Manage.
3. Keep the private Dish Action listener healthy on loopback. TEST is `http://127.0.0.1:8766` and
   PROD is `http://127.0.0.1:8776`.
4. Create an owner-readable environment file; never put either bearer in the tunnel profile, app
   description, Project instructions, repository, or ChatGPT conversation.

Start qualification against TEST. Create `/home/marco/.config/dish-service/mcp-tunnel.env` mode 0600:

```sh
CONTROL_PLANE_API_KEY=replace-with-tunnel-runtime-key
DISH_MCP_ACTION_URL=http://127.0.0.1:8766
DISH_MCP_ACTION_TOKEN=replace-with-test-action-token
```

The adapter rejects non-loopback Action URLs. `tunnel-client` and its stdio child inherit the three
variables above; ChatGPT never receives the Dish Action bearer.

## Create and verify the tunnel profile

Load the environment in the shell used for `init`/`doctor`, then create the named stdio profile:

```sh
set -a
. /home/marco/.config/dish-service/mcp-tunnel.env
set +a

/home/marco/.local/bin/tunnel-client init \
  --sample sample_mcp_stdio_local \
  --profile dish-mcp \
  --tunnel-id tunnel_REPLACE_ME \
  --mcp-command "/home/marco/ai-tools/dish/.venv/bin/python -m dish_service.mcp_server"

/home/marco/.local/bin/tunnel-client doctor --profile dish-mcp --explain
/home/marco/.local/bin/tunnel-client run --profile dish-mcp
```

`doctor` must pass before interpreting a ChatGPT discovery failure as a Dish defect. Secure MCP
Tunnel is outbound-only; do not add a public listener, reverse proxy, Funnel route, or OAuth layer for
this phase.

In ChatGPT developer mode, create a custom app, choose **Tunnel** as the connection, and select this
tunnel. The tunnel control-plane credential is not Dish authority; the adapter still authenticates
every backend request with the dedicated Action bearer.

## Connected qualification

Do not retire or alter the GPT Action route until all of these are true for the MCP app:

1. `tools/list` shows exactly these 18 tools: `dish_create`, `dish_sections`, `dish_section_tasks`,
   `dish_search`, `dish_cook_logs`, `dish_record_cook_log`, `dish_read`, `dish_proposals`,
   `dish_apply_proposal`, `dish_safe_reclaim`, `dish_inspect`, `dish_start`, `dish_prepare`,
   `dish_approve`, `dish_reject`, `dish_submit`, `dish_renew_lease`, and `dish_cooked`.
2. A regular Project performs a representative TEST read and receives the canonical Dish envelope.
3. A TEST replay-bound mutation preserves one stable `client.run_id`, one fresh `client.request_id`
   for the logical mutation, and the exact same pair on an intentional exact replay. The replay must
   return the authoritative stored envelope rather than perform a second mutation.
4. A regular Project completes one real continuation/challenge path using only identifiers,
   `allowed_actions`, guidance, and continuation data returned by Dish. A canonical `ok:false`
   response must remain a normal MCP tool result, not become a transport error.
5. A Pro live Project completes at least one approved Dish mutation through the MCP app. For that
   production check, change only `DISH_MCP_ACTION_URL` to `http://127.0.0.1:8776` and
   `DISH_MCP_ACTION_TOKEN` to the production Action token, then re-run `doctor` before the call.
   Treat the live account result as the gate: published plan documentation may lag runtime capability,
   but if the Project exposes only read/fetch tools then this step has failed and the old GPT Action stays.
6. Re-check connected-agent behavior: stable run identity, fresh mutation request identity, exact
   retry identity, canonical identifier discipline, `data.agent_guidance`/`human_action`, and
   independent Verification run separation.

Any mismatch in command inventory, request schema, result envelope, identity/replay behavior, or
continuation authority blocks retirement and returns to this additive adapter PR. Do not redesign the
backend or paper over a mismatch in Project instructions.

## Run as a production service

After the profile is qualified for production, install the checked-in unit and keep the environment
file on the production loopback URL/token:

```sh
sudo install -m 0644 deploy/systemd/dish-mcp-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dish-mcp-tunnel.service
/home/marco/.local/bin/tunnel-client doctor --profile dish-mcp --explain
systemctl status dish-mcp-tunnel.service --no-pager
```

The service runs alongside the existing Action transport. This Phase 1 change deliberately does not
disable the old GPT Action, Caddy/Tailscale routing, or Funnel. Retire those only in the separately
qualified cleanup step after all connected checks above remain green.
