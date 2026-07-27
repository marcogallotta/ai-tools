# Dish service Tailscale exposure

The production service has two separate loopback listeners:

- `127.0.0.1:8765` — private CLI/admin surface;
- `127.0.0.1:8766` — Action-only surface.

This laptop already uses public Funnel port 443 for an unrelated service at
`127.0.0.1:8001`. That mapping must remain unchanged. Dish uses two different HTTPS ports so the
private listener is not converted into a public Funnel endpoint:

```sh
tailscale serve --bg --https=8444 http://127.0.0.1:8765
tailscale funnel --bg --https=8443 http://127.0.0.1:8766
```

Expected access paths:

- CLI/admin: `https://<node>.<tailnet>.ts.net:8444/` over the tailnet;
- GPT Action: `https://<node>.<tailnet>.ts.net:8443/` over Funnel.

Before activation:

1. Confirm the installed Tailscale version's current `serve` and `funnel` syntax.
2. Save and inspect `tailscale serve status --json` before changing the configuration.
3. Add the two Dish mappings without using `serve reset` or `funnel reset`.
4. Inspect `tailscale serve status` and `tailscale funnel status`.
5. Confirm port 8444 is tailnet-only, port 8443 is public, and the existing public port 443 mapping
   is unchanged.
6. Confirm the public endpoint returns 404 for `/v1/commands/*`, `/v1/admin/*`, `/health`, and
   backup/recovery routes.
7. Confirm the Action token can call only `/v1/action/*` and cannot call the private listener with
   Action scope.
8. Fetch `https://<node>.<tailnet>.ts.net:8443/openapi/action.json` and import that runtime schema,
   or replace the placeholder server in `openapi/dish-action.openapi.json` with the exact Funnel
   URL.
9. Validate the non-default HTTPS port in the GPT Action editor and Preview before activation.
10. Confirm the CLI/admin tokens are absent from the GPT Action configuration.

Do not point Funnel at the private listener. The same Tailscale HTTPS port cannot be both private Serve and public Funnel at once; the most recent configuration wins.
