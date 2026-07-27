"""Shared fixtures for the asana CLI test suite.

The CLI lives at ../asana (no .py extension, executable script), so it is
loaded via importlib rather than a normal import. Each test gets a fresh
module instance (loaded, not cached in sys.modules) so the script's module-
level globals (_CLIENT, _PAT) never leak state between tests.
"""
import importlib.util
import os
import pathlib
import sqlite3
import sys
from importlib.machinery import SourceFileLoader

import pytest

CLI_PATH = pathlib.Path(__file__).resolve().parent.parent.parent / "tools" / "asana"


def _load_cli_module():
    loader = SourceFileLoader("asana_cli_under_test", str(CLI_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def cli(monkeypatch):
    """A freshly loaded asana CLI module with ASANA_PAT set.

    The production script re-execs into its pinned virtualenv when imported
    outside that environment. Tests intentionally load the module in-process,
    so suppress only that re-exec boundary; the installed SDK contract remains
    real and is covered separately.
    """
    monkeypatch.setenv("ASANA_PAT", "test-pat-token")
    monkeypatch.delenv("ASANA_ENV", raising=False)
    monkeypatch.setattr(os, "execv", lambda *_args, **_kwargs: None)
    module = _load_cli_module()

    class PermissiveCookingGuard:
        def before_task_mutation(self, *args, **kwargs):
            return None

        def before_create_task(self, *args, **kwargs):
            return None

        def before_create_subtask(self, *args, **kwargs):
            return None

        def before_move(self, *args, **kwargs):
            return None

        def before_raw(self, *args, **kwargs):
            return None

    module._COOKING_GUARD = PermissiveCookingGuard()
    try:
        yield module
    finally:
        module.close_client()


@pytest.fixture(autouse=True)
def close_sqlite_connections(monkeypatch):
    """Make every test own and deterministically close its SQLite handles."""
    real_connect = sqlite3.connect
    opened = []

    def tracked_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    yield
    for conn in reversed(opened):
        try:
            conn.close()
        except sqlite3.Error:
            pass


def run_cli(module, argv, monkeypatch):
    """Invoke module.main() as if invoked from the shell with the given args."""
    monkeypatch.setattr(sys, "argv", ["asana"] + argv)
    module.main()


@pytest.fixture
def run(monkeypatch):
    def _run(module, argv):
        run_cli(module, argv, monkeypatch)
    return _run
