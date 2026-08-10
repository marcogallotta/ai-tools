# Dish service Tailscale exposure

The test and production services have separate loopback listeners:

- test: `127.0.0.1:8765` private and `127.0.0.1:8766` Action;
- production: `127.0.0.1:8775` private and `127.0.0.1:8776` Action;
- router: `127.0.0.1:8786`, with production at root and TEST under `/test`.

Dish uses separate tailnet-only ports for the two private listeners. Public HTTPS points to the
static router, never directly to either service:

```sh
tailscale serve --bg --https=8444 http://127.0.0.1:8765
tailscale serve --bg --https=8445 http://127.0.0.1:8775
tailscale funnel --bg --https=443 http://127.0.0.1:8786
```

Expected access paths:

- test CLI/admin: `https://<node>.<tailnet>.ts.net:8444/` over the tailnet;
- production CLI/admin: `https://<node>.<tailnet>.ts.net:8445/` over the tailnet;
- production GPT Action: `https://<node>.<tailnet>.ts.net/` over Funnel;
- TEST GPT Action: `https://<node>.<tailnet>.ts.net/test/` over the same Funnel.

Inspect both fixed upstreams with `deploy/caddy/dish-action-route status`. Environment selection is
made by importing the corresponding root or `/test` schema, not by changing Tailscale or Caddy.

Before activation:

1. Confirm the installed Tailscale version's current `serve` and `funnel` syntax.
2. Save and inspect `tailscale serve status --json` before changing the configuration.
3. Confirm public port 443 is free or already points to Dish; do not overwrite an unrelated
   service. Add the three Dish mappings without using `serve reset` or `funnel reset`.
4. Inspect `tailscale serve status` and `tailscale funnel status`.
5. Confirm ports 8444 and 8445 are tailnet-only and port 443 is public.
6. Confirm the public endpoint returns 404 for root and `/test` CLI, admin, health, and
   backup/recovery routes.
7. Confirm each environment's Action token works only on its matching root or `/test` Action path.
8. Fetch `https://<node>.<tailnet>.ts.net/openapi/action.json` for production or
   `https://<node>.<tailnet>.ts.net/test/openapi/action.json` for TEST.
9. Validate the standard HTTPS Funnel URL in the GPT Action editor and Preview before activation.
10. Confirm the CLI/admin tokens are absent from the GPT Action configuration.
11. Confirm `dish-action-route status` reports both expected upstreams and that a Caddy restart
    preserves the fixed split from the autosaved native configuration.

Do not point Funnel at a private or direct Action listener. The same Tailscale HTTPS port cannot be
both private Serve and public Funnel at once; the most recent configuration wins.

## Future private browser frontend

The authenticated browser frontend is not currently exposed. Its contract requires a dedicated
private HTTPS hostname distinct from the Action/Funnel hostname; reusing the node hostname on port
8444 or 8445 is not sufficient because browser cookies are not port-scoped.

Before Stage 2 deployment, provision and document a tailnet-only hostname/certificate path that maps
to the existing environment's private listener without creating a third service listener. The initial
frontend implementation ignores forwarded authority and client-address headers, so do not depend on
`Forwarded` or `X-Forwarded-*` values for Host, scheme, origin, or throttling authority. Follow
`docs/frontend-test-deployment-readiness.md` and do not expose the fixture-only build as an
authenticated application.
