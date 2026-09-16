from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    return path


def payload(cwd: Path) -> bytes:
    return json.dumps({"hook_event_name": "PreToolUse", "cwd": str(cwd)}).encode()


def configure(router, canonical: Path, switch_hook: Path) -> None:
    router.SWITCHSTAND = canonical
    router.SWITCHSTAND_HOOK = (sys.executable, str(switch_hook))


def test_canonical_main_uses_only_switchstand_hook(codex_hook_router, hooks_dir, tmp_path):
    canonical = repository(tmp_path / "switchstand")
    hook = tmp_path / "switch.py"
    hook.write_text("import sys; sys.stdin.buffer.read(); print('switch')\n")
    configure(codex_hook_router, canonical, hook)
    result = codex_hook_router.dispatch(payload(canonical), ("/bin/echo", "global"))
    assert result.stdout == b"switch\n"
    config = json.loads((hooks_dir.parent / "codex" / "hooks.json").read_text())
    commands = [configured["command"] for entries in config["hooks"].values()
                for entry in entries for configured in entry["hooks"]]
    assert len(commands) == 5
    assert all(command.startswith("/home/marco/.local/bin/codex-hook-router -- ")
               for command in commands)


def test_registered_linked_worktree_outside_prefix_uses_switchstand(codex_hook_router, tmp_path):
    canonical = repository(tmp_path / "switchstand")
    linked = tmp_path / "elsewhere" / "writer"
    linked.parent.mkdir()
    git(canonical, "worktree", "add", "-q", "-b", "writer", str(linked))
    hook = tmp_path / "switch.py"
    hook.write_text("import sys; sys.stdin.buffer.read(); print('switch')\n")
    configure(codex_hook_router, canonical, hook)
    result = codex_hook_router.dispatch(payload(linked), ("/bin/echo", "global"))
    assert result.stdout == b"switch\n"


def test_other_repo_preserves_global_hook_bytes(codex_hook_router, tmp_path):
    canonical = repository(tmp_path / "switchstand")
    other = repository(tmp_path / "other")
    hook = tmp_path / "switch.py"
    hook.write_text("raise SystemExit('wrong hook')\n")
    global_hook = tmp_path / "global.py"
    global_hook.write_text("import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(b'G'+data)\n")
    configure(codex_hook_router, canonical, hook)
    raw = payload(other)
    result = codex_hook_router.dispatch(raw, (sys.executable, str(global_hook)))
    assert result.stdout == b"G" + raw


def test_non_git_directory_preserves_global_fallback(codex_hook_router, tmp_path):
    canonical = repository(tmp_path / "switchstand")
    outside = tmp_path / "plain"
    outside.mkdir()
    hook = tmp_path / "switch.py"
    hook.write_text("raise SystemExit('wrong hook')\n")
    configure(codex_hook_router, canonical, hook)
    result = codex_hook_router.dispatch(payload(outside), ("/bin/echo", "global"))
    assert result.stdout == b"global\n"


def test_independent_switchstand_clone_preserves_global_fallback(codex_hook_router, tmp_path):
    canonical = repository(tmp_path / "switchstand")
    clone = tmp_path / "switchstand-clone"
    git(tmp_path, "clone", "-q", str(canonical), str(clone))
    hook = tmp_path / "switch.py"
    hook.write_text("raise SystemExit('wrong hook')\n")
    configure(codex_hook_router, canonical, hook)
    result = codex_hook_router.dispatch(payload(clone), ("/bin/echo", "global"))
    assert result.stdout == b"global\n"


def test_missing_git_runs_real_global_hook(codex_hook_router, tmp_path, monkeypatch):
    canonical = repository(tmp_path / "switchstand")
    hook = tmp_path / "switch.py"
    hook.write_text("raise SystemExit('wrong hook')\n")
    global_hook = tmp_path / "global"
    global_hook.write_text("#!/bin/sh\nprintf 'global-real\\n'\n")
    global_hook.chmod(0o755)
    configure(codex_hook_router, canonical, hook)
    monkeypatch.setenv("PATH", str(tmp_path))
    result = codex_hook_router.dispatch(payload(canonical), (str(global_hook),))
    assert result.stdout == b"global-real\n"
