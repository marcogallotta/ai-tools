"""Shared fixtures for the hooks test suite.

Hook scripts live as extensionless executable files in the parent directory,
so they're loaded via importlib rather than a normal import.
"""
import importlib.util
import pathlib
from importlib.machinery import SourceFileLoader

import pytest

HOOKS_DIR = pathlib.Path(__file__).resolve().parent.parent


def load_hook_module(name):
    path = HOOKS_DIR / name
    loader = SourceFileLoader(f"{name}_under_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def no_compound_bash():
    return load_hook_module("no-compound-bash")
