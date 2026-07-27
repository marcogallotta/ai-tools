# Dish service Tailscale exposure

The service listens on loopback. Keep CLI/admin access on the tailnet and expose the
scoped GPT Action through Funnel. Tokens remain mandatory on both paths.

Example target:

```sh
tailscale serve --bg http://127.0.0.1:8765
tailscale funnel --bg http://127.0.0.1:8765
```

Before activation, confirm the installed Tailscale version's current `serve` and
`funnel` syntax, inspect `tailscale serve status` / `tailscale funnel status`, and
verify that the Funnel credential can call only `/v1/action/*`. Never place the
CLI/admin bearer token in the GPT Action configuration.
