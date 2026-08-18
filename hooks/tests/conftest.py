"""Shared fixtures for the hooks test suite.

Hook scripts live as extensionless executable files in the parent directory,
so they're loaded via importlib rather than a normal import.
"""
import importlib.util
import pathlib
import subprocess
from importlib.machinery import SourceFileLoader

import pytest

HOOKS_DIR = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def hooks_dir():
    return HOOKS_DIR


def load_hook_module(name):
    path = HOOKS_DIR / name
    loader = SourceFileLoader(f"{name}_under_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def destructive_op_guard():
    return load_hook_module("destructive-op-guard")


@pytest.fixture
def codex_protected_checkout():
    return load_hook_module("codex-protected-checkout")


@pytest.fixture
def classifier_module(hooks_dir):
    import sys

    sys.path.insert(0, str(hooks_dir))
    return importlib.import_module("protected_checkout")


@pytest.fixture
def asana_write_guard():
    return load_hook_module("asana-write-guard")


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def protected_repo(tmp_path, destructive_op_guard, monkeypatch):
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "main")
    _git(primary, "config", "user.email", "test@example.com")
    _git(primary, "config", "user.name", "Test")
    (primary / "README.md").write_text("x\n")
    _git(primary, "add", "README.md")
    _git(primary, "commit", "-q", "-m", "initial")

    linked = tmp_path / "linked"
    _git(primary, "worktree", "add", "-q", "-b", "agent/existing", str(linked), "main")

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    _git(unrelated, "init", "-q", "-b", "main")
    _git(unrelated, "config", "user.email", "test@example.com")
    _git(unrelated, "config", "user.name", "Test")
    (unrelated / "README.md").write_text("x\n")
    _git(unrelated, "add", "README.md")
    _git(unrelated, "commit", "-q", "-m", "initial")

    root = str(primary.resolve())
    monkeypatch.setattr(destructive_op_guard, "PROTECTED_CHECKOUT_ROOT", root)
    monkeypatch.setattr(
        destructive_op_guard.protected_checkout, "DEFAULT_PROTECTED_CHECKOUT_ROOT", root
    )
    return {"primary": primary, "linked": linked, "unrelated": unrelated}


@pytest.fixture
def agent_reground():
    return load_hook_module("agent-reground")
