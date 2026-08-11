from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "dependency_bundle.py"


def _module():
    spec = importlib.util.spec_from_file_location("dependency_bundle", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "ci").mkdir(parents=True)
    (root / "dish").mkdir()
    (root / "tools").mkdir()
    for relative in (
        "dish/requirements.txt",
        "dish/requirements-test.txt",
        "dish/requirements-flake.txt",
        "tools/requirements.txt",
    ):
        (root / relative).write_text("fixturedep==1.0.0\n", encoding="utf-8")
    target = {
        "schema_version": 1,
        "python_implementation": "CPython",
        "python_version": "3.13.5",
        "platform_system": "Linux",
        "platform_architecture": "x86_64",
        "sysconfig_platform": "linux-x86_64",
        "libc_name": "glibc",
        "libc_version": "2.39",
        "github_runner": "ubuntu-24.04",
        "compatibility_manifests": [
            "dish/requirements.txt",
            "dish/requirements-test.txt",
            "dish/requirements-flake.txt",
            "tools/requirements.txt",
        ],
        "environments": {
            "dish": {
                "requirements": "dish/requirements-test.txt",
                "venv": "dish/.venv",
                "install_by_default": True,
            },
            "tools": {
                "requirements": "tools/requirements.txt",
                "venv": "tools/.venv",
                "install_by_default": True,
            },
            "flake": {
                "requirements": "dish/requirements-flake.txt",
                "venv": "dish/.venv-flake",
                "install_by_default": False,
            },
        },
    }
    (root / "ci" / "dependency-bundle-target.json").write_text(
        json.dumps(target), encoding="utf-8"
    )
    return root


def test_bundle_identity_changes_when_dependency_manifest_changes(tmp_path: Path) -> None:
    module = _module()
    root = _fake_repo(tmp_path)
    before = module.expected_metadata(root)
    (root / "tools" / "requirements.txt").write_text("fixturedep==2.0.0\n", encoding="utf-8")
    after = module.expected_metadata(root)
    assert before["bundle_id"] != after["bundle_id"]
    assert before["compatibility_sha256"] != after["compatibility_sha256"]


def test_bundle_verification_fails_closed_on_checkout_manifest_drift(tmp_path: Path) -> None:
    module = _module()
    root = _fake_repo(tmp_path)
    expected = module.expected_metadata(root)
    bundle = tmp_path / "bundle"
    (bundle / "wheelhouse").mkdir(parents=True)
    (bundle / "resolved").mkdir()
    fake_wheel = bundle / "wheelhouse" / "fixturedep-1.0.0-py3-none-any.whl"
    fake_wheel.write_bytes(b"not-a-real-wheel")
    for name in expected["target"]["environments"]:
        (bundle / "resolved" / f"{name}.txt").write_text("", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "bundle_id": expected["bundle_id"],
        "compatibility_sha256": expected["compatibility_sha256"],
        "dependency_manifest_sha256": expected["dependency_manifest_sha256"],
        "target": expected["target"],
        "builder": {},
        "wheels": [{"path": "wheelhouse/fixturedep-1.0.0-py3-none-any.whl", "sha256": module._sha256_file(fake_wheel)}],
        "resolved_lock_sha256": {
            name: module._sha256_file(bundle / "resolved" / f"{name}.txt")
            for name in expected["target"]["environments"]
        },
    }
    (bundle / "bundle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "dish" / "requirements.txt").write_text("fixturedep==9.9.9\n", encoding="utf-8")
    with pytest.raises(module.BundleError, match="bundle identity mismatch"):
        module._verify_bundle_tree(root, bundle)


def test_bundle_runtime_fails_closed_on_runner_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    root = _fake_repo(tmp_path)
    target = module.expected_metadata(root)["target"]
    monkeypatch.setenv("AI_TOOLS_GITHUB_RUNNER", "ubuntu-22.04")
    with pytest.raises(module.BundleError, match="GitHub runner compatibility mismatch"):
        module._verify_runtime(target)


def test_bundle_runtime_fails_closed_on_libc_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    root = _fake_repo(tmp_path)
    target = module.expected_metadata(root)["target"]
    facts = {
        "python_implementation": target["python_implementation"],
        "python_version": target["python_version"],
        "platform_system": target["platform_system"],
        "platform_architecture": target["platform_architecture"],
        "sysconfig_platform": target["sysconfig_platform"],
        "libc_name": target["libc_name"],
        "libc_version": "9.99",
    }
    monkeypatch.delenv("AI_TOOLS_GITHUB_RUNNER", raising=False)
    monkeypatch.setattr(module, "_runtime_facts", lambda: facts)
    with pytest.raises(module.BundleError, match="runtime compatibility mismatch for libc_version"):
        module._verify_runtime(target)
