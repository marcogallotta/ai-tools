"""Shared fixtures for the asana CLI test suite.

The CLI lives at ../asana (no .py extension, executable script), so it is
loaded via importlib rather than a normal import. Each test gets a fresh
module instance (loaded, not cached in sys.modules) so the script's module-
level globals (_CLIENT, _PAT) never leak state between tests.
"""
import importlib.util
import pathlib
import sys
from importlib.machinery import SourceFileLoader

import pytest

CLI_PATH = pathlib.Path(__file__).resolve().parent.parent / "asana"


def _load_cli_module():
    loader = SourceFileLoader("asana_cli_under_test", str(CLI_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def cli(monkeypatch):
    """A freshly loaded asana CLI module with ASANA_PAT set."""
    monkeypatch.setenv("ASANA_PAT", "test-pat-token")
    monkeypatch.delenv("ASANA_ENV", raising=False)
    return _load_cli_module()


def run_cli(module, argv, monkeypatch):
    """Invoke module.main() as if invoked from the shell with the given args."""
    monkeypatch.setattr(sys, "argv", ["asana"] + argv)
    module.main()


@pytest.fixture
def run(monkeypatch):
    def _run(module, argv):
        run_cli(module, argv, monkeypatch)
    return _run
