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
MAX_CONTEXT_CHARS = 150_000
BLOCK_ROW = re.compile(r"^\|\s*\[\x60([^\x60]+\.md)\x60\]\(([^)]+\.md)\)\s*\|", re.M)
DISH_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


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


def _format_receipt(
    *, scope: str, target: str, files: list[dict[str, str]],
    parent_path: str | None, cooked: list[dict[str, Any]],
    logs: list[dict[str, Any]], focus: list[str],
    canonical: Mapping[str, Any] | None,
) -> str:
    """Show exact delivered docs and the actual log coverage, not just totals."""
    titles = {str(row["dish_id"]): str(row.get("title") or row["dish_id"])
              for row in cooked}
    if canonical and canonical.get("dish_id") and canonical.get("title"):
        titles[str(canonical["dish_id"])] = str(canonical["title"])

    def title(dish_id: str) -> str:
        return titles.get(dish_id, dish_id)

    def name(path: str) -> str:
        label = path.removesuffix(".md")
        if label.startswith("planning/blocks/"):
            label = "blocks/" + label.removeprefix("planning/blocks/")
        if path == parent_path:
            label += " (parent)"
        return label

    counts: dict[str, int] = {}
    for log in logs:
        dish_id = str(log["dish_id"])
        counts[dish_id] = counts.get(dish_id, 0) + 1
    log_details = ", ".join(
        f"{title(dish_id)} ({count})"
        for dish_id, count in sorted(counts.items(), key=lambda x: (title(x[0]).casefold(), x[0]))
    ) or "none"
    finalists = ", ".join(
        title(dish_id) for dish_id in sorted(set(focus), key=lambda x: (title(x).casefold(), x))
    ) if focus else "none yet"
    return "\n".join((
        f"Context loaded ({scope}: {target or 'all'})",
        "Docs: " + ", ".join(name(doc["path"]) for doc in files),
        f"Cook history: {len(cooked)} dishes cooked in total",
        "Logs read: " + log_details,
        "Finalists checked: " + finalists,
    ))


def assemble_planning_context(
    adapter: Any, caller: Mapping[str, str | None], checkout: Path,
    read_files: Callable[[Path, list[str]], Mapping[str, Any]], *,
    run_id: str, agent: str, scope: str, target: str = "",
    focus_dish_ids: list[str] | None = None,
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
    focus = focus_dish_ids or []
    if len(focus) > 16 or any(
        not isinstance(x, str) or not DISH_UUID.fullmatch(x) for x in focus
    ):
        raise PlanningContextError("focus_dish_ids must be at most 16 canonical Dish UUIDs")
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
    parent_path = None
    if scope == "block":
        index = next(f["text"] for f in files if f["path"] == "planning/blocks/index.md")
        chosen, index_row = _block(index, target)
        sha2, detail = _read(read_files, checkout, [chosen])
        if sha2 != sha:
            raise PlanningContextError("Honest Pantry main changed while resolving the block")
        parent = _parent(detail[0]["text"])
        if parent and parent != chosen:
            parent_path = parent
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
    # Cooked inventory is complete; log detail is bounded to the relevant subset.
    # Full finalist/alias history is fetched when focus_dish_ids names that subset.
    relevant_ids = {row["dish_id"] for row in recent} | set(focus)
    if scope == "block":
        relevant_ids.update(
            match.group(0).lower()
            for file in files if file["path"].startswith("planning/blocks/")
            for match in DISH_UUID.finditer(file["text"])
        )
    if scope == "dish":
        relevant_ids.update(
            row["dish_id"] for row in cooked
            if row["dish_id"] == selected_id or _key(str(row.get("title") or "")) == _key(target)
        )
        if selected_id:
            relevant_ids.add(selected_id)
    # A programme file may refer to a planned or archival Dish that has no
    # cooked record. Such references are not evidence of completed cooking.
    if scope == "block":
        cooked_ids = {row["dish_id"] for row in cooked}
        relevant_ids = (relevant_ids & cooked_ids) | set(focus)
    logs: list[dict[str, Any]] = []
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
    # The complete inventory is a compact projection; only cook logs establish
    # actual cooking dates when imported state contains migration timestamps.
    compact_cooked = [
        {
            "title": row.get("title"),
            "dish_id": row["dish_id"],
            "last_cooked": row.get("current_cooked_at"),
        }
        for row in cooked
    ]
    payload: dict[str, Any] = {
        "ok": True, "code": "OK", "repository_sha": sha,
        "scope": scope, "target": target or "all", "files": files,
        "block_index_row": index_row,
        "canonical_dish_id": selected_id, "canonical_dish": canonical,
        "history": {
            "recent_updates": recent, "recent_logged_cooks": recent_logged,
            "all_cooked": compact_cooked, "cook_logs": logs,
            "logged_dish_ids": sorted(relevant_ids),
            "focus_dish_ids": sorted(set(focus)),
            "coverage": (
                "Full cooked inventory; log detail covers recent updates, block references, "
                "the selected dish, and requested focus IDs. To compare finalists and plausible "
                "aliases not in logged_dish_ids, reload with their Dish UUIDs in focus_dish_ids."
            ),
        },
        "counts": {
            "docs": len(files), "cooked": len(cooked), "logs": len(logs),
            "logged_dishes": len(relevant_ids),
        },
    }
    if len(json.dumps(payload, ensure_ascii=False)) > MAX_CONTEXT_CHARS:
        raise PlanningContextError("Context exceeds the configured budget; no receipt was issued")
    payload["receipt"] = _format_receipt(
        scope=scope, target=target, files=files, parent_path=parent_path,
        cooked=compact_cooked, logs=logs, focus=focus, canonical=canonical,
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

    def require(self, *, owner: str, run_id: str) -> None:
        """A successful context read admits this authenticated agent run, without expiry."""
        if not self.path.exists():
            raise PlanningContextError("No planning-context receipt exists for this MCP service")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM context_receipts WHERE owner = ? AND run_id = ?",
                (owner, canonical_run_id(run_id)),
            ).fetchone()
        if row is None:
            raise PlanningContextError("Call dish_planning_context with this client.run_id first")
