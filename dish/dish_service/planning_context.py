"""Deterministic MCP Planning bundle and run-bound context evidence.

This is a connected-agent presentation/read boundary, not a second Dish
workflow authority. No planning write is authorized by receiving this bundle.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import uuid
from typing import Any, Callable, Mapping

BROAD_DOCS = (
    "dish-planning-protocol.md",
    "planning/index.md",
    "planning/roadmap.md",
    "cuisine-map.md",
    "dish-classes.md",
    "planning/blocks/index.md",
)
BLOCK_BASE_DOCS = (
    "dish-planning-protocol.md",
    "cuisine-map.md",
    "dish-classes.md",
    "planning/blocks/index.md",
)
DISH_DOCS = ("dish-planning-protocol.md",)
EPOCH = "1970-01-01T00:00:00Z"
MAX_PAGES = 100
MAX_CONTEXT_CHARS = 900_000
RECEIPT_TTL_SECONDS = 90 * 60
BLOCK_ROW = re.compile(r"^\|\s*\[\x60([^\x60]+\.md)\x60\]\(([^)]+\.md)\)\s*\|", re.M)


class PlanningContextError(ValueError):
    """No incomplete or ambiguous bundle is eligible for a context receipt."""


def canonical_run_id(run_id: str) -> str:
    try:
        parsed = str(uuid.UUID(run_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise PlanningContextError("client.run_id must be a canonical lowercase UUID") from exc
    if parsed != run_id or parsed == "00000000-0000-0000-0000-000000000000":
        raise PlanningContextError("client.run_id must be a canonical nonzero lowercase UUID")
    return parsed


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _call(
    adapter: Any, caller: Mapping[str, str | None], run_id: str,
    agent: str, tool: str, **arguments: Any,
) -> Mapping[str, Any]:
    result = adapter.call(
        tool,
        {"client": {"run_id": run_id}, "arguments": {"agent": agent, **arguments}},
        caller=caller,
    )
    if not isinstance(result, Mapping) or result.get("ok") is not True:
        raise PlanningContextError(
            f"{tool} failed: {result.get('code', 'unavailable') if isinstance(result, Mapping) else 'unavailable'}"
        )
    data = result.get("data")
    if not isinstance(data, Mapping):
        raise PlanningContextError(f"{tool} returned no authoritative data")
    return data


def _pages(
    adapter: Any, caller: Mapping[str, str | None], run_id: str,
    agent: str, tool: str, rows_key: str, **arguments: Any,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    generation: str | None = None
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        args = {**arguments, "page_size": 100}
        if generation and tool == "dish_cooked_updates":
            args["generation_id"] = generation
        if cursor:
            args["cursor"] = cursor
        data = _call(adapter, caller, run_id, agent, tool, **args)
        if tool == "dish_cooked_updates":
            if generation is None:
                generation = str(data.get("generation_id") or "")
            elif data.get("generation_id") != generation:
                raise PlanningContextError("Cooked-history generation changed during context assembly")
        values = data.get(rows_key)
        if not isinstance(values, list):
            raise PlanningContextError(f"{tool} returned no {rows_key} list")
        records.extend(dict(x) for x in values if isinstance(x, Mapping))
        cursor = data.get("next_cursor")
        if cursor is None:
            return records
        if not isinstance(cursor, str) or cursor in seen:
            raise PlanningContextError(f"{tool} returned an invalid or repeated cursor")
        seen.add(cursor)
    raise PlanningContextError(f"{tool} exceeded {MAX_PAGES} history pages")


def _read(read_files: Callable[[Path, list[str]], Mapping[str, Any]],
          checkout: Path, paths: list[str]) -> tuple[str, list[dict[str, str]]]:
    payload = read_files(checkout, paths)
    files = payload.get("files")
    sha = payload.get("sha")
    if not isinstance(sha, str) or not isinstance(files, list) or len(files) != len(paths):
        raise PlanningContextError("Honest Pantry context read was incomplete")
    return sha, [dict(entry) for entry in files]


def _block(index: str, target: str) -> tuple[str, str]:
    if ".." in target or "\\" in target or (
        "/" in target and not target.startswith("planning/blocks/")
    ):
        raise PlanningContextError("Block target must be a registered name or path")
    found = [(label, path) for label, path in BLOCK_ROW.findall(index)]
    if not found:
        raise PlanningContextError("No registered programme blocks were found")
    normalized = _key(target.removeprefix("planning/blocks/").removesuffix(".md"))
    matches = [
        (label, path) for label, path in found
        if normalized in {_key(path.removesuffix(".md")), _key(label.removesuffix(".md"))}
    ]
    if len(matches) != 1:
        available = ", ".join(path.removesuffix(".md") for _, path in found)
        raise PlanningContextError(
            f"Block must match exactly one registered file: {target!r}; available: {available}"
        )
    label, name = matches[0]
    if "/" in name or name.startswith("."):
        raise PlanningContextError("Block registry contains an unsafe file path")
    row = next(
        line for line in index.splitlines()
        if f"]({name})" in line and line.lstrip().startswith("|")
    )
    return f"planning/blocks/{name}", row


def _parent(block_text: str) -> str | None:
    # Only parent relationships stated near the start of the selected detail.
    match = re.search(
        r"child of [^\n]*?\]\(([A-Za-z0-9_-]+\.md)\)",
        block_text[:800], re.IGNORECASE,
    )
    if match:
        return f"planning/blocks/{match.group(1)}"
    return None


def _dish_identity(
    adapter: Any, caller: Mapping[str, str | None], run_id: str,
    agent: str, target: str,
) -> tuple[str | None, Mapping[str, Any] | None]:
    try:
        dish_id = str(uuid.UUID(target))
    except ValueError:
        matches = _pages(
            adapter, caller, run_id, agent, "dish_search", "results", query=target,
        )
        exact = [row for row in matches if _key(str(row.get("title") or "")) == _key(target)]
        if len(exact) > 1:
            raise PlanningContextError("Named dish matched several canonical records; supply its UUID")
        if matches and not exact:
            raise PlanningContextError(
                "Dish search returned possible matches; supply an exact title or Dish UUID"
            )
        dish_id = str(exact[0]["dish_id"]) if exact else ""
    if not dish_id:
        return None, None  # genuinely new named dish
    return dish_id, _call(adapter, caller, run_id, agent, "dish_read", dish_id=dish_id)


def assemble_planning_context(
    adapter: Any, caller: Mapping[str, str | None], checkout: Path,
    read_files: Callable[[Path, list[str]], Mapping[str, Any]], *,
    run_id: str, agent: str, scope: str, target: str = "",
) -> dict[str, Any]:
    canonical_run_id(run_id)
    if agent not in {"gpt", "claude", "codex"}:
        raise PlanningContextError("agent must be gpt, claude, or codex")
    if scope not in {"broad", "block", "dish"}:
        raise PlanningContextError("scope must be broad, block, or dish")
    if scope != "broad" and not target.strip():
        raise PlanningContextError("a named block or dish requires a target")
    if scope == "broad" and target.strip():
        raise PlanningContextError("broad scope cannot silently consume a named target")
    owner = caller.get("subject")
    if not isinstance(owner, str) or not owner:
        raise PlanningContextError("authenticated MCP caller identity required")

    # Resolve a named dish through Dish before any repository documents.
    selected_id = None
    canonical = None
    if scope == "dish":
        selected_id, canonical = _dish_identity(adapter, caller, run_id, agent, target)

    paths = list(BROAD_DOCS if scope == "broad" else
                 BLOCK_BASE_DOCS if scope == "block" else DISH_DOCS)
    sha, files = _read(read_files, checkout, paths)
    index_row = None
    if scope == "block":
        index = next(f["text"] for f in files if f["path"] == "planning/blocks/index.md")
        chosen, index_row = _block(index, target)
        sha2, detail = _read(read_files, checkout, [chosen])
        if sha2 != sha:
            raise PlanningContextError("Honest Pantry main changed while resolving the block")
        parent = _parent(detail[0]["text"])
        if parent and parent != chosen:
            sha3, parent_file = _read(read_files, checkout, [parent])
            if sha3 != sha:
                raise PlanningContextError("Honest Pantry main changed while resolving the block parent")
            detail.extend(parent_file)
        files = [f for f in files if f["path"] != "planning/blocks/index.md"] + detail
        target = chosen.removeprefix("planning/blocks/").removesuffix(".md")

    recent = []
    if scope != "dish":
        since = (datetime.now(timezone.utc) - timedelta(days=21)).isoformat()
        recent = _pages(
            adapter, caller, run_id, agent, "dish_cooked_updates", "updates", since=since,
        )
    cooked = _pages(
        adapter, caller, run_id, agent, "dish_cooked_updates", "updates", since=EPOCH,
    )
    logs: list[dict[str, Any]] = []
    relevant_ids = {row["dish_id"] for row in cooked}
    if scope == "dish":
        relevant_ids = {
            row["dish_id"] for row in cooked
            if row["dish_id"] == selected_id or _key(str(row.get("title") or "")) == _key(target)
        }
    for dish_id in sorted(relevant_ids):
        for log in _pages(
            adapter, caller, run_id, agent, "dish_cook_logs", "logs", dish_id=dish_id,
        ):
            logs.append({"dish_id": dish_id, **log})
    recent_logged: list[dict[str, Any]] = []
    recent_cutoff = datetime.now(timezone.utc) - timedelta(days=21)
    for row in recent:
        if any(
            log["dish_id"] == row.get("dish_id")
            and datetime.fromisoformat(
                str(log.get("recorded_at") or "1970-01-01T00:00:00Z").replace("Z", "+00:00")
            ) >= recent_cutoff
            for log in logs
        ):
            recent_logged.append(row)
    payload: dict[str, Any] = {
        "ok": True, "code": "OK", "repository_sha": sha,
        "scope": scope, "target": target or "all", "files": files,
        "block_index_row": index_row,
        "canonical_dish_id": selected_id, "canonical_dish": canonical,
        "history": {
            "recent_updates": recent, "recent_logged_cooks": recent_logged,
            "all_cooked": cooked, "cook_logs": logs,
        },
        "counts": {"docs": len(files), "cooked": len(cooked), "logs": len(logs)},
    }
    if len(json.dumps(payload, ensure_ascii=False)) > MAX_CONTEXT_CHARS:
        raise PlanningContextError("Context exceeds the configured budget; no receipt was issued")
    payload["receipt"] = (
        f"Context: {scope} {target or 'all'}, {len(files)} docs, {len(logs)} cook logs "
        f"(Honest {sha[:12]})"
    )
    return payload


class PlanningReceiptStore:
    """MCP-owned evidence that an authenticated run received a complete bundle."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=5)
        if self.path.exists():
            os.chmod(self.path, 0o600)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS context_receipts ("
            "owner TEXT NOT NULL, run_id TEXT NOT NULL, sha TEXT NOT NULL,"
            "scope TEXT NOT NULL, target TEXT NOT NULL, created_at REAL NOT NULL,"
            "PRIMARY KEY (owner, run_id))"
        )
        return connection

    def record(self, *, owner: str, run_id: str, sha: str,
               scope: str, target: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO context_receipts "
                "(owner, run_id, sha, scope, target, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (owner, canonical_run_id(run_id), sha, scope, target,
                 datetime.now(timezone.utc).timestamp()),
            )

    def require(self, *, owner: str, run_id: str, checkout: Path,
                dish_id: str = "") -> None:
        if not self.path.exists():
            raise PlanningContextError("No planning-context receipt exists for this MCP service")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT sha, scope, target, created_at FROM context_receipts "
                "WHERE owner = ? AND run_id = ?",
                (owner, canonical_run_id(run_id)),
            ).fetchone()
        if row is None:
            raise PlanningContextError("Call dish_planning_context with this client.run_id first")
        sha, scope, target, created = row
        if datetime.now(timezone.utc).timestamp() - created > RECEIPT_TTL_SECONDS:
            raise PlanningContextError("Planning context expired; reload it")
        current = subprocess.run(
            ["/usr/bin/git", "rev-parse", "HEAD"], cwd=checkout,
            capture_output=True, timeout=10, check=False,
        )
        if current.returncode or current.stdout.decode().strip() != sha:
            raise PlanningContextError("Honest Pantry main changed; reload Planning context")
        if dish_id and scope == "dish" and target != dish_id:
            raise PlanningContextError("Planning context belongs to another dish")
