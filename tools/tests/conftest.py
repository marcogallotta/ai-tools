"""Shared fixtures for the tools/ test suite.

``tools/asana`` is an extensionless executable script (loaded via importlib,
same as the hook scripts), and it imports the real python-asana SDK plus
``dish_tool`` from ``dish/``. The SDK is stubbed out here so these tests run
against a plain venv with only pytest installed -- no python-asana, no real
Asana network/DB access. ``dish_tool`` itself has only stdlib dependencies,
so it's imported for real.

Note: the script does a filesystem check for ``tools/.venv`` at import time
(it re-execs into that venv when run for real), so ``tools/.venv`` must exist
on disk for these tests to import the module at all -- see tools/README.md.
"""
import importlib.util
import pathlib
import sys
import types
from importlib.machinery import SourceFileLoader
from unittest.mock import MagicMock

import pytest

TOOLS_DIR = pathlib.Path(__file__).resolve().parent.parent


class FakeApiException(Exception):
    """Stand-in for asana.rest.ApiException."""

    def __init__(self, status=None, reason=None, body=None):
        super().__init__(reason or body or status)
        self.status = status
        self.reason = reason
        self.body = body


def _install_fake_asana_sdk():
    """Install a minimal stub 'asana' package into sys.modules, if not already present."""
    existing = sys.modules.get("asana")
    if existing is not None and getattr(existing, "_FAKE_ASANA_SDK", False):
        return existing

    rest_module = types.ModuleType("asana.rest")
    rest_module.ApiException = FakeApiException

    asana_module = types.ModuleType("asana")
    asana_module._FAKE_ASANA_SDK = True
    asana_module.rest = rest_module
    for name in (
        "Configuration", "ApiClient", "TasksApi", "SectionsApi",
        "StoriesApi", "TypeaheadApi", "ProjectsApi",
    ):
        setattr(asana_module, name, MagicMock(name=name))

    sys.modules["asana"] = asana_module
    sys.modules["asana.rest"] = rest_module
    return asana_module


def load_asana_cli():
    _install_fake_asana_sdk()
    path = TOOLS_DIR / "asana"
    loader = SourceFileLoader("asana_cli_under_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class NoopAdvisoryGuard:
    """Records before_* calls without touching a real DB or the network."""

    def __init__(self):
        self.calls = []

    def before_task_content(self, *args, **kwargs):
        self.calls.append(("before_task_content", args, kwargs))

    def before_create_task(self, *args, **kwargs):
        self.calls.append(("before_create_task", args, kwargs))

    def before_create_subtask(self, *args, **kwargs):
        self.calls.append(("before_create_subtask", args, kwargs))

    def before_raw(self, *args, **kwargs):
        self.calls.append(("before_raw", args, kwargs))


@pytest.fixture
def cli(monkeypatch):
    """A loaded tools/asana module with the advisory guard replaced by a no-op recorder."""
    module = load_asana_cli()
    guard = NoopAdvisoryGuard()
    monkeypatch.setattr(module, "advisory_guard", lambda: guard)
    module._test_guard = guard
    return module
