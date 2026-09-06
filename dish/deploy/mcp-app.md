# Dish MCP app runbook

Dish is public at `https://laptop.tail46f0b9.ts.net/dish/mcp` through the existing Tailscale
Funnel and Caddy. The MCP process remains loopback-only. FastMCP implements OAuth; GitHub performs
the login, and Dish accepts only Marco's immutable GitHub user ID.

## GitHub OAuth App

Create one GitHub OAuth App with:

- Homepage: `https://laptop.tail46f0b9.ts.net/dish`
- Callback: `https://laptop.tail46f0b9.ts.net/dish/auth/callback`
- Wildcard matching: off
- Device flow: off
- Expiring user access tokens: on

## Private environment

`/home/marco/.config/dish-service/mcp.env` must be mode `0600`:

```sh
DISH_MCP_BIND_HOST=127.0.0.1
DISH_MCP_BIND_PORT=8787
DISH_MCP_RESOURCE_URL=https://laptop.tail46f0b9.ts.net/dish/mcp
DISH_MCP_GITHUB_CLIENT_ID=replace-with-github-client-id
DISH_MCP_GITHUB_CLIENT_SECRET=replace-with-github-client-secret
DISH_MCP_GITHUB_USER_ID=192548
DISH_MCP_ACTION_URL=http://127.0.0.1:8776
DISH_MCP_ACTION_TOKEN=replace-with-dish-action-token
```

Never put either secret in the repository, ChatGPT app settings, tool arguments, or logs. Do not
load `prod.env`: the public-facing MCP process needs only its dedicated Action token.

FastMCP keeps encrypted OAuth registrations and tokens in its user data directory. The service
must retain a stable GitHub client secret and write access to `/home/marco/.local/share/fastmcp` so
sessions survive restarts.

## Install and start

```sh
cd /home/marco/ai-tools/dish
.venv/bin/python -m pip install -r requirements.txt
install -Dm644 deploy/systemd/dish-mcp.service ~/.config/systemd/user/dish-mcp.service
systemctl --user daemon-reload
systemctl --user restart dish-mcp.service
```

Caddy must proxy these public paths to `127.0.0.1:8787`:

- `/dish/mcp`
- `/dish/authorize`, `/dish/token`, `/dish/register`
- `/dish/auth/callback`, `/dish/consent`
- `/.well-known/oauth-protected-resource/dish/mcp`
- `/.well-known/oauth-authorization-server/dish`

Reload the checked-in Caddy configuration with the existing `dish-action-router` service after
validating it.

## Verify

```sh
curl -i -X POST https://laptop.tail46f0b9.ts.net/dish/mcp
curl -sS https://laptop.tail46f0b9.ts.net/.well-known/oauth-protected-resource/dish/mcp
curl -sS https://laptop.tail46f0b9.ts.net/.well-known/oauth-authorization-server/dish
```

The MCP request must return `401` with a `WWW-Authenticate` resource-metadata URL. Protected
resource metadata must identify the exact `/dish/mcp` resource. Authorization-server metadata must
advertise the `/dish/authorize`, `/dish/token`, and `/dish/register` endpoints plus `S256` PKCE.

## ChatGPT app

Create the custom app with:

- MCP URL: `https://laptop.tail46f0b9.ts.net/dish/mcp`
- Authentication: `OAuth`

Do not select `Mixed` and do not enter the GitHub client secret in ChatGPT. ChatGPT discovers the
OAuth endpoints, registers itself, opens GitHub login, and returns through Dish's backend callback.

After connecting, verify that the app lists exactly the 18 `dish_*` tools. Exercise one read, one
replay-bound TEST mutation, one continuation flow, and one approved production mutation before
retiring the old GPT Action route. A failed Dish envelope remains a normal MCP tool result; OAuth
credentials never replace Dish `run_id` or `request_id`.
