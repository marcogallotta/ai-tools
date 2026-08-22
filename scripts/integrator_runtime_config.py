#!/usr/bin/env python3
"""Prepare the isolated Codex home used only by the persistent Integrator."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile


MCP_TOOLS = (
    "get_integrator_case",
    "get_exact_pr_evidence",
    "get_repair_owner",
    "get_prior_integrator_decisions",
    "get_nightly_health",
)


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _atomic_text(path: Path, text: str) -> None:
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def prepare_codex_home(
    *, codex_home: Path, source_codex_home: Path, repo: Path, state_dir: Path, python: Path
) -> Path:
    codex_home = codex_home.expanduser().resolve()
    source_codex_home = source_codex_home.expanduser().resolve()
    repo = repo.expanduser().resolve()
    state_dir = state_dir.expanduser().resolve()
    python = python.expanduser().resolve()
    if codex_home == source_codex_home:
        raise ValueError("Integrator Codex home must be isolated from the operator Codex home")
    for required in (repo / "scripts/integrator_mcp_server.py", python, source_codex_home / "auth.json"):
        if not required.exists():
            raise FileNotFoundError(required)

    codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    codex_home.chmod(0o700)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir.chmod(0o700)

    auth = codex_home / "auth.json"
    source_auth = source_codex_home / "auth.json"
    if auth.is_symlink():
        if auth.resolve() != source_auth:
            raise ValueError("Integrator auth link points at an unexpected credential source")
    elif auth.exists():
        raise ValueError("Integrator auth path exists but is not the expected symlink")
    else:
        auth.symlink_to(source_auth)

    enabled_tools = ", ".join(_toml_string(value) for value in MCP_TOOLS)
    config = f'''approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"
check_for_update_on_startup = false

[agents]
enabled = false

[features]
shell_tool = false
unified_exec = false
multi_agent = false
skill_mcp_dependency_install = false

[mcp_servers.dish_integrator]
command = {_toml_string(str(python))}
args = [{_toml_string(str(repo / "scripts/integrator_mcp_server.py"))}, "--state-dir", {_toml_string(str(state_dir))}, "--repository", "marcogallotta/ai-tools"]
cwd = {_toml_string(str(repo))}
required = true
enabled = true
enabled_tools = [{enabled_tools}]
default_tools_approval_mode = "auto"
startup_timeout_sec = 10
tool_timeout_sec = 30
'''
    _atomic_text(codex_home / "config.toml", config)
    return codex_home / "app-server-control/app-server-control.sock"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--source-codex-home", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    args = parser.parse_args()
    socket_path = prepare_codex_home(
        codex_home=args.codex_home,
        source_codex_home=args.source_codex_home,
        repo=args.repo,
        state_dir=args.state_dir,
        python=args.python,
    )
    print(socket_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
