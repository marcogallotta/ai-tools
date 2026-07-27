# Dish service Tailscale exposure

The production service has two separate loopback listeners:

- `127.0.0.1:8765` — private CLI/admin surface;
- `127.0.0.1:8766` — Action-only surface.

Dish uses the standard public HTTPS port for its Action listener and a different, tailnet-only port
for its private listener:

```sh
tailscale serve --bg --https=8444 http://127.0.0.1:8765
tailscale funnel --bg --https=443 http://127.0.0.1:8766
```

Expected access paths:

- CLI/admin: `https://<node>.<tailnet>.ts.net:8444/` over the tailnet;
- GPT Action: `https://<node>.<tailnet>.ts.net/` over Funnel.

Before activation:

1. Confirm the installed Tailscale version's current `serve` and `funnel` syntax.
2. Save and inspect `tailscale serve status --json` before changing the configuration.
3. Confirm public port 443 is free or already points to Dish; do not overwrite an unrelated
   service. Add the two Dish mappings without using `serve reset` or `funnel reset`.
4. Inspect `tailscale serve status` and `tailscale funnel status`.
5. Confirm port 8444 is tailnet-only and port 443 is public.
6. Confirm the public endpoint returns 404 for `/v1/commands/*`, `/v1/admin/*`, `/health`, and
   backup/recovery routes.
7. Confirm the Action token can call only `/v1/action/*` and cannot call the private listener with
   Action scope.
8. Fetch `https://<node>.<tailnet>.ts.net/openapi/action.json` and import that runtime schema, or
   replace the placeholder server in `openapi/dish-action.openapi.json` with the exact Funnel URL.
9. Validate the standard HTTPS Funnel URL in the GPT Action editor and Preview before activation.
10. Confirm the CLI/admin tokens are absent from the GPT Action configuration.

Do not point Funnel at the private listener. The same Tailscale HTTPS port cannot be both private Serve and public Funnel at once; the most recent configuration wins.
