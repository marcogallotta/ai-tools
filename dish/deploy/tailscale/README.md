# Dish service Tailscale exposure

The test and production services have separate loopback listeners:

- test: `127.0.0.1:8765` private and `127.0.0.1:8766` Action;
- production: `127.0.0.1:8775` private and `127.0.0.1:8776` Action;
- TEST legacy comparator oracle: `127.0.0.1:8795` private and `127.0.0.1:8796` Action;
- router: `127.0.0.1:8786`, with production at root, PostgreSQL-authoritative TEST under `/test`, and the qualification-only legacy oracle under `/test-legacy`.

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
- TEST GPT Action: `https://<node>.<tailnet>.ts.net/test/` over the same Funnel;
- TEST comparator oracle: `https://<node>.<tailnet>.ts.net/test-legacy/` over the same Funnel, for the curated comparator only. Never import this schema as an ordinary GPT Action or use it as failover.

Inspect all three fixed upstreams with `deploy/caddy/dish-action-route status`. Environment selection is
made by importing the corresponding root or `/test` schema; `/test-legacy` is reserved for explicit comparator qualification, not by changing Tailscale or Caddy.

Before activation:

1. Confirm the installed Tailscale version's current `serve` and `funnel` syntax.
2. Save and inspect `tailscale serve status --json` before changing the configuration.
3. Confirm public port 443 is free or already points to Dish; do not overwrite an unrelated
   service. Add the three Dish mappings without using `serve reset` or `funnel reset`.
4. Inspect `tailscale serve status` and `tailscale funnel status`.
5. Confirm ports 8444 and 8445 are tailnet-only and port 443 is public.
6. Confirm the public endpoint returns 404 for root, `/test`, and `/test-legacy` CLI, admin, health, and
   backup/recovery routes.
7. Confirm each environment's Action token works only on its matching root, `/test`, or comparator-only `/test-legacy` Action path.
8. Fetch `https://<node>.<tailnet>.ts.net/openapi/action.json` for production or
   `https://<node>.<tailnet>.ts.net/test/openapi/action.json` for TEST.
9. Validate the standard HTTPS Funnel URL in the GPT Action editor and Preview before activation.
10. Confirm the CLI/admin tokens are absent from the GPT Action configuration.
11. Confirm `dish-action-route status` reports all three expected upstreams and that a Caddy restart
    preserves the fixed split from the autosaved native configuration.

Do not point Funnel at a private or direct Action listener. The same Tailscale HTTPS port cannot be
both private Serve and public Funnel at once; the most recent configuration wins.

## PostgreSQL TEST browser frontend

The PostgreSQL-authoritative TEST service can expose the production-shaped authenticated frontend
through a dedicated private HTTPS hostname distinct from the Action/Funnel hostname. Reusing the
node Action hostname on port 8444 or 8445 is not sufficient because browser cookies are not
port-scoped.

Provision a private hostname and certificate, populate
`deploy/systemd/frontend-test-caddy.env.example`, and run
`dish-frontend-test-caddy.service`. Its Caddy configuration terminates HTTPS/HSTS and proxies only to
the existing TEST private listener at `127.0.0.1:8765`; it does not create another Dish application
listener and never routes to Action port 8766. Bind Caddy to the intended private interface rather
than all interfaces. The frontend ignores forwarded authority and client-address headers, so do not
depend on `Forwarded` or `X-Forwarded-*` values for Host, scheme, origin, or throttling authority.

This TEST origin is rehearsal-only. It does not change the production frontend activation decision.
Follow `docs/frontend-deployment-runbook.md` and deploy only the ordinary production build, never
`review-dist/`.
