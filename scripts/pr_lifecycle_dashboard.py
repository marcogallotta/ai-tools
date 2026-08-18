#!/usr/bin/env python3
"""Private, read-only HTTP dashboard for the lifecycle controller projection."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from pr_lifecycle_projection import read_projection

DEFAULT_PROJECTION = Path.home() / ".local" / "state" / "dish" / "pr-lifecycle-controller" / "lifecycle.json"
STALE_AFTER_SECONDS = 120
JSON_PATH = "/api/dish-lifecycle.json"
PAGE_PATH = "/dish-desk.html"


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def coordinator_handoff(projection: Mapping[str, Any]) -> str | None:
    actions = [item for item in projection.get("coordinator_actions", []) if isinstance(item, Mapping)]
    drift = [item for item in projection.get("state_drift", []) if isinstance(item, Mapping)]
    if not actions and not drift:
        return None
    lines = [
        "COORDINATOR HANDOFF — lifecycle snapshot may be stale; re-read live GitHub and Asana authority before acting."
    ]
    for item in actions:
        lines.append(f"- PR #{item.get('pr')}: {item.get('action')} (observed head {item.get('head')})")
    for item in drift:
        target = f"PR #{item.get('pr')}" if item.get("pr") else f"task {item.get('task')}"
        lines.append(f"- {target}: {item.get('conflict')} — repair owner: {item.get('repair_owner')}")
    return "\n".join(lines)


def dashboard_snapshot(
    projection: Mapping[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    reconciled = _instant(projection.get("reconciled_at"))
    age = max(0, int((now - reconciled).total_seconds())) if reconciled else None
    controller = dict(projection.get("controller") or {})
    controller_status = str(controller.get("status") or "unknown")
    stale = age is None or age > STALE_AFTER_SECONDS or controller_status in {"failed", "stale", "stopped"}
    value = dict(projection)
    value["dashboard"] = {
        "served_at": now.isoformat(),
        "snapshot_age_seconds": age,
        "stale": stale,
        "controller_status": controller_status,
        "coordinator_handoff": coordinator_handoff(projection),
        "read_only": True,
    }
    return value


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dish lifecycle desk</title><style>
:root{color-scheme:dark;--bg:#111827;--panel:#1f2937;--line:#374151;--muted:#9ca3af;--hot:#f59e0b;--bad:#fb7185;--good:#34d399}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#f9fafb;font:14px/1.45 system-ui,sans-serif}main{max-width:1500px;margin:auto;padding:24px}
h1{margin:0;font-size:24px}.meta{color:var(--muted);margin:6px 0 20px}.stale{color:var(--bad);font-weight:700}.fresh{color:var(--good)}
.queues{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}.queue,.health,.handoff{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.queue h2{font-size:15px;margin:0 0 10px}.card{border-top:1px solid var(--line);padding:9px 0}.card:first-of-type{border:0}.muted{color:var(--muted)}
.health{display:flex;gap:22px;flex-wrap:wrap;margin-bottom:12px}.handoff{white-space:pre-wrap;margin-bottom:12px;border-color:var(--hot)}button{margin-top:8px;padding:7px 10px}
</style></head><body><main><h1>Dish lifecycle desk</h1><div id="meta" class="meta">Loading read-only projection…</div><section id="health" class="health"></section><section id="handoff"></section><section id="queues" class="queues"></section></main>
<script>
const api='/api/dish-lifecycle.json';
function txt(tag,value,klass){const n=document.createElement(tag);n.textContent=value??'';if(klass)n.className=klass;return n}
function render(v){const d=v.dashboard||{}, prs=new Map((v.pull_requests||[]).map(p=>[Number(p.number),p]));
 const meta=document.querySelector('#meta');meta.textContent=`Last upstream reconciliation: ${v.reconciled_at||'unknown'} · snapshot age: ${d.snapshot_age_seconds??'unknown'}s · controller: ${d.controller_status}`;meta.className=d.stale?'meta stale':'meta fresh';
 const health=document.querySelector('#health');health.replaceChildren();health.append(txt('div',`Full regression: ${(v.full_regression||{}).conclusion||(v.full_regression||{}).status||'unknown'}`),txt('div',`Drift: ${(v.state_drift||[]).length}`),txt('div',`Corrective owners: ${(v.current_main_corrective_owners||[]).length}`));
 const hand=document.querySelector('#handoff');hand.replaceChildren();if(d.coordinator_handoff){const box=txt('div',d.coordinator_handoff,'handoff'),b=txt('button','Copy coordinator handoff');b.onclick=()=>navigator.clipboard.writeText(d.coordinator_handoff);box.append(b);hand.append(box)}
 const queues=document.querySelector('#queues');queues.replaceChildren();for(const [name,numbers] of Object.entries(v.queues||{})){const q=txt('article','', 'queue');q.append(txt('h2',`${name} · ${numbers.length}`));for(const number of numbers){const p=prs.get(Number(number))||{}, c=txt('div','', 'card');c.append(txt('div',`#${number} ${p.title||''}`),txt('div',`${p.state_label||p.state||''} · ${(p.head||'').slice(0,10)}`,'muted'),txt('div',p.residual_reason||p.human_action||'','muted'));q.append(c)}queues.append(q)}}
async function refresh(){try{const r=await fetch(api,{cache:'no-store'}),v=await r.json();if(!r.ok)throw Error(v.error||r.statusText);render(v)}catch(e){const m=document.querySelector('#meta');m.textContent=`Dashboard unavailable: ${e.message}`;m.className='meta stale'}}
refresh();setInterval(refresh,10000);
</script></body></html>"""


def handler(projection_path: Path) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "DishLifecycleDashboard/1"

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/":
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", PAGE_PATH)
                self.end_headers()
                return
            if path == PAGE_PATH:
                self._send(HTTPStatus.OK, "text/html; charset=utf-8", HTML.encode())
                return
            if path == JSON_PATH:
                try:
                    payload = dashboard_snapshot(read_projection(projection_path))
                    body = json.dumps(payload, sort_keys=True).encode()
                    self._send(HTTPStatus.OK, "application/json; charset=utf-8", body)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    body = json.dumps({"error": f"lifecycle projection unavailable: {exc}"}).encode()
                    self._send(HTTPStatus.SERVICE_UNAVAILABLE, "application/json; charset=utf-8", body)
                return
            self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found\n")

        do_HEAD = do_GET

        def do_POST(self) -> None:
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, "text/plain; charset=utf-8", b"read-only\n")

        def log_message(self, format: str, *args: Any) -> None:
            return

    return DashboardHandler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projection-path", type=Path, default=DEFAULT_PROJECTION)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)
    if args.bind not in {"127.0.0.1", "::1", "localhost"}:
        parser.error("--bind must remain loopback-only; private HTTPS exposure belongs to the existing proxy")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    server = ThreadingHTTPServer((args.bind, args.port), handler(args.projection_path.expanduser()))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
