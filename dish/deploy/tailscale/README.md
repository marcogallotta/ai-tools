# Dish service Tailscale exposure

The production service has two separate loopback listeners:

- `127.0.0.1:8765` — private CLI/admin surface;
- `127.0.0.1:8766` — Action-only surface.

Use different Tailscale HTTPS ports so the private listener is not converted into a public Funnel endpoint:

```sh
tailscale serve --bg --https=443 http://127.0.0.1:8765
tailscale funnel --bg --https=8443 http://127.0.0.1:8766
```

Expected access paths:

- CLI/admin: `https://<node>.<tailnet>.ts.net/` over the tailnet;
- GPT Action: `https://<node>.<tailnet>.ts.net:8443/` over Funnel.

Before activation:

1. Confirm the installed Tailscale version's current `serve` and `funnel` syntax.
2. Inspect `tailscale serve status` and `tailscale funnel status`.
3. Confirm port 443 is tailnet-only and port 8443 is public.
4. Confirm the public endpoint returns 404 for `/v1/commands/*`, `/v1/admin/*`, `/health`, and backup/recovery routes.
5. Confirm the Action token can call only `/v1/action/*` and cannot call the private listener with Action scope.
6. Fetch `https://<node>.<tailnet>.ts.net:8443/openapi/action.json` and import that runtime schema, or replace the placeholder server in `openapi/dish-action.openapi.json` with the exact Funnel URL.
7. Validate the non-default HTTPS port in the GPT Action editor and Preview before activation.
8. Confirm the CLI/admin tokens are absent from the GPT Action configuration.

Do not point Funnel at the private listener. The same Tailscale HTTPS port cannot be both private Serve and public Funnel at once; the most recent configuration wins.
