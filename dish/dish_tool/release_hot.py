"""Paired release operations. Configuration and receipts contain no credentials."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import prod_release as release

CANONICAL_REMOTE = "https://github.com/marcogallotta/ai-tools.git"
TEST_CONFIG = Path.home() / ".config/dish-service/release-test.json"
PROD_CONFIG = Path.home() / ".config/dish-service/release-prod.json"
CONTRACT = {
    "release_contract": "paired-hot-v1",
    "transport_generation": 1,
    "config_generation": 1,
}


class OperationError(release.ReleaseError):
    def __init__(self, code, message, *, exit_code=2, observed=None):
        super().__init__(message)
        self.code, self.exit_code, self.observed = code, exit_code, observed


class Budget:
    def __init__(self, seconds):
        self.end = time.monotonic() + seconds

    def left(self):
        value = self.end - time.monotonic()
        if value <= 0:
            raise OperationError("TIMEOUT", "total operation deadline expired")
        return value


def atomic_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass
class Target:
    profile: str
    values: dict

    def path(self, key):
        return Path(self.values[key])

    @property
    def pending(self):
        return self.path("state_root") / "pending.json"

    @property
    def recover_command(self):
        profile = " --profile test" if self.profile == "test" else ""
        return f"dish/scripts/dish-prod-release{profile} recover"

    def fingerprint(self):
        # Bind configuration bytes including secret-file digests, never their contents.
        files = [*self.values["env_files"], self.values["mcp_env_file"]]
        payload = {
            "target": self.values,
            "files": {
                p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in files
            },
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def target_config(profile):
    values = {
        "release_root": str(release.DEFAULT_RELEASE_ROOT),
        "current": str(release.DEFAULT_CURRENT),
        "previous": str(release.DEFAULT_PREVIOUS),
        "state_root": str(Path.home() / ".local/state/dish/paired-release"),
        "env_files": list(map(str, release.DEFAULT_ENV_FILES)),
        "mcp_env_file": str(Path.home() / ".config/dish-service/mcp.env"),
        "backend_unit": release.DEFAULT_UNIT,
        "mcp_unit": "dish-mcp.service",
        "health_url": release.DEFAULT_HEALTH_URL,
        "mcp_url": "https://laptop.tail46f0b9.ts.net/dish/mcp",
        "mcp_local_url": "http://127.0.0.1:8787/mcp",
        "token_file": str(Path.home() / ".config/dish-service/mcp-conformance-token"),
        "refresh_file": str(
            Path.home() / ".config/dish-service/mcp-conformance-refresh.json"
        ),
        "python": str(release.DEFAULT_PYTHON),
        "repository": str(Path(__file__).resolve().parents[2]),
        "upstream": CANONICAL_REMOTE,
        "oauth_root": str(Path.home() / ".local/share/fastmcp"),
        "honest_store": str(
            Path.home() / ".local/share/dish/honest-mcp/prod/repository.git"
        ),
        "timeout": 15,
        "recovery_timeout": 15,
        "status_timeout": 3,
    }
    if profile == "test":
        if not TEST_CONFIG.is_file():
            raise OperationError(
                "TEST_NOT_INSTALLED",
                f"isolated TEST target missing: {TEST_CONFIG}; run the rehearsal setup first",
            )
        values = json.loads(TEST_CONFIG.read_text())
        required = {
            "release_root",
            "current",
            "previous",
            "state_root",
            "env_files",
            "mcp_env_file",
            "backend_unit",
            "mcp_unit",
            "health_url",
            "mcp_url",
            "mcp_local_url",
            "token_file",
            "python",
            "repository",
            "upstream",
            "oauth_root",
            "honest_store",
            "isolation_root",
            "database_url",
        }
        if required - values.keys():
            raise OperationError(
                "TEST_ISOLATION", "incomplete isolated TEST configuration"
            )
        root = Path(values["isolation_root"]).resolve()
        if (
            root == Path.home()
            or root == Path("/")
            or ".local/state/dish" not in str(root)
        ):
            raise OperationError(
                "TEST_ISOLATION", "TEST isolation root must be persistent Dish state"
            )
        for name in (
            "release_root",
            "current",
            "previous",
            "state_root",
            "token_file",
            "mcp_env_file",
            "oauth_root",
            "honest_store",
            *[],
        ):
            # Pointers themselves must be inside root; follow their target separately.
            path = Path(values[name]).absolute()
            if not path.is_relative_to(
                root
            ) or not path.parent.resolve().is_relative_to(root):
                raise OperationError(
                    "TEST_ISOLATION", f"TEST {name} escapes isolation root"
                )
            if path.exists() and not path.resolve().is_relative_to(root):
                raise OperationError(
                    "TEST_ISOLATION", f"TEST {name} resolves outside isolation root"
                )
        for p in values["env_files"]:
            if not Path(p).resolve().is_relative_to(root):
                raise OperationError(
                    "TEST_ISOLATION", "TEST environment escapes isolation root"
                )
        for name in ("backend_unit", "mcp_unit"):
            if not values[name].startswith("dish-release-test-") or not values[
                name
            ].endswith(".service"):
                raise OperationError(
                    "TEST_ISOLATION", "TEST requires dedicated rehearsal user units"
                )
        for name in ("health_url", "mcp_url", "mcp_local_url"):
            url = urllib.parse.urlsplit(values[name])
            if (
                url.hostname != "127.0.0.1"
                or url.scheme != "http"
                or url.port in (None, 8765, 8766, 8775, 8776, 8780, 8781, 8787)
            ):
                raise OperationError(
                    "TEST_ISOLATION", "TEST endpoints require separate loopback ports"
                )
        db = urllib.parse.urlsplit(values["database_url"])
        if (
            db.hostname != "127.0.0.1"
            or db.port in (None, 5432, 55432, 55433)
            or not db.path.startswith("/dish_release_test")
        ):
            raise OperationError(
                "TEST_ISOLATION", "TEST database requires an isolated server and name"
            )
        env = release._read_env(tuple(map(Path, values["env_files"])))
        if env.get("DISH_PG_DATABASE_URL") != values["database_url"]:
            raise OperationError(
                "TEST_ISOLATION",
                "TEST database configuration differs from isolation declaration",
            )
        for key in ("DISH_STATE_DIR", "DISH_DB_PATH"):
            if env.get(key) and not Path(env[key]).resolve().is_relative_to(root):
                raise OperationError(
                    "TEST_ISOLATION", "TEST backend store escapes isolation root"
                )
    elif PROD_CONFIG.exists():
        extra = json.loads(PROD_CONFIG.read_text())
        if set(extra) - {"python", "recovery_release"}:
            raise OperationError(
                "PRODUCTION_CONFIG",
                "production permits only interpreter and exact compatible recovery configuration",
            )
        values.update(extra)
    return Target(profile, values)


def run(command, budget, **kwargs):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=budget.left(),
            check=False,
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OperationError(
            "SUBPROCESS_FAILED",
            "bounded subprocess failed; inspect the named operation",
        ) from exc
    if result.returncode:
        raise OperationError(
            "SUBPROCESS_FAILED",
            f"{Path(command[0]).name} operation failed (exit {result.returncode})",
        )
    return result.stdout.strip()


def unit_values(unit, budget):
    raw = run(
        [
            "/usr/bin/systemctl",
            "--user",
            "show",
            unit,
            "--property=ActiveState,MainPID,ExecStart,WorkingDirectory,EnvironmentFiles,Environment,KillMode,FragmentPath,DropInPaths",
        ],
        budget,
    )
    return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)


def unit_bindings(target, budget):
    result = {}
    expected = str(target.path("current") / "dish")
    for component in ("backend", "mcp"):
        unit = target.values[f"{component}_unit"]
        values = unit_values(unit, budget)
        if values.get(
            "WorkingDirectory"
        ) != expected or expected + "/" not in values.get("ExecStart", ""):
            raise OperationError(
                "UNIT_BINDING",
                f"{unit} is not bound to the paired current pointer; install the reviewed paired units",
            )
        if component == "mcp" and values.get("KillMode") != "control-group":
            raise OperationError(
                "UNIT_BINDING", "MCP KillMode must preserve control-group teardown"
            )
        # Capture effective bindings, not volatile PID/status; reload/drift is detectable.
        result[component] = {
            k: v
            for k, v in values.items()
            if k not in ("MainPID", "ActiveState", "ExecStart")
        }
        result[component]["Environment"] = hashlib.sha256(
            values.get("Environment", "").encode()
        ).hexdigest()
        # systemctl ExecStart contains runtime timestamps; retain command only.
        result[component]["command"] = values["ExecStart"].split(
            " ; ignore_errors=", 1
        )[0]
    return result


def process_witness(pid):
    base = Path(f"/proc/{pid}")
    return {
        "pid": int(pid),
        "start_ticks": (base / "stat").read_text().rsplit(")", 1)[1].split()[19],
        "cwd": str((base / "cwd").resolve()),
        "executable": str((base / "exe").resolve()),
        "arguments": (base / "cmdline")
        .read_bytes()
        .replace(b"\0", b" ")
        .decode()
        .strip(),
    }


def owns_listener(pid, url):
    port = urllib.parse.urlsplit(url).port
    if port is None:
        return False
    sockets = set()
    for fd in Path(f"/proc/{pid}/fd").iterdir():
        try:
            link = os.readlink(fd)
            if link.startswith("socket:["):
                sockets.add(link[8:-1])
        except OSError:
            continue
    for table in ("tcp", "tcp6"):
        for row in Path(f"/proc/{pid}/net/{table}").read_text().splitlines()[1:]:
            fields = row.split()
            if (
                fields[3] == "0A"
                and int(fields[1].split(":")[1], 16) == port
                and fields[9] in sockets
            ):
                return True
    return False


def request_json(url, budget, *, token=None, payload=None):
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
        headers["MCP-Protocol-Version"] = "2025-06-18"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=budget.left()) as response:
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ValueError("oversized response")
            result = json.loads(body)
    except urllib.error.HTTPError as exc:
        raise OperationError(
            "AUTH_REQUIRED" if exc.code == 401 else "ENDPOINT_FAILED",
            "authenticated endpoint rejected request; renew the release credential"
            if exc.code == 401
            else "readiness endpoint failed",
        ) from exc
    except (OSError, ValueError) as exc:
        raise OperationError(
            "ENDPOINT_UNKNOWN", "readiness endpoint unavailable or malformed"
        ) from exc
    if not isinstance(result, dict) or result.get("error"):
        raise OperationError("RPC_FAILED", "read-only endpoint contract failed")
    return result


def private_token(target):
    path = target.path("token_file")
    if path.stat().st_mode & 0o077:
        raise OperationError("TOKEN_PERMISSIONS", "release token requires mode 0600")
    token = path.read_text().strip()
    if not token or "\n" in token or "\r" in token:
        raise OperationError(
            "AUTH_REQUIRED",
            "release token missing; run dish/scripts/dish-release-credential renew",
        )
    return token


def rpc(url, token, method, params, budget):
    return request_json(
        url,
        budget,
        token=token,
        payload={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )


def runtime_read(url, token, budget):
    result = rpc(
        url,
        token,
        "tools/call",
        {"name": "dish_runtime_identity", "arguments": {}},
        budget,
    ).get("result", {})
    value = result.get("structuredContent")
    if value is None:
        for block in result.get("content", []):
            if block.get("type") == "text":
                value = json.loads(block["text"])
                break
    if result.get("isError") or not isinstance(value, dict):
        raise OperationError(
            "IDENTITY_UNKNOWN", "MCP process-start code identity unavailable"
        )
    return value


def resource_identity(target, budget):
    url = target.values["mcp_url"]
    request = urllib.request.Request(
        url, data=b"{}", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=budget.left()):
            raise OperationError(
                "ENDPOINT_IDENTITY", "MCP endpoint did not require authentication"
            )
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise OperationError(
                "ENDPOINT_IDENTITY", "MCP authentication discovery failed"
            ) from exc
        from .release_mcp_conformance import _RESOURCE_METADATA

        match = _RESOURCE_METADATA.search(exc.headers.get("WWW-Authenticate", ""))
    if match is None:
        raise OperationError(
            "ENDPOINT_IDENTITY", "MCP challenge omitted resource metadata"
        )
    metadata_url = match.group(1)
    parsed = urllib.parse.urlsplit(metadata_url)
    expected = urllib.parse.urlsplit(url)
    if (
        (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc)
        or parsed.username
        or parsed.password
    ):
        raise OperationError(
            "ENDPOINT_IDENTITY", "MCP resource metadata escaped endpoint origin"
        )
    if request_json(metadata_url, budget).get("resource") != url:
        raise OperationError(
            "ENDPOINT_IDENTITY", "MCP resource metadata identifies a different endpoint"
        )


def authenticated_read(target, budget, *, refresh=False):
    resource_identity(target, budget)
    token = private_token(target)
    try:
        rpc(
            target.values["mcp_url"],
            token,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "dish-release", "version": "1"},
            },
            budget,
        )
        result = rpc(
            target.values["mcp_url"],
            token,
            "tools/call",
            {
                "name": "dish_sections",
                "arguments": {
                    "client": {"run_id": str(uuid.uuid4())},
                    "arguments": {"agent": "gpt"},
                },
            },
            budget,
        ).get("result")
        if not isinstance(result, dict) or result.get("isError"):
            raise OperationError(
                "CONFORMANCE_FAILED", "authenticated sections read failed"
            )
        structured = result.get("structuredContent")
        if structured is None:
            structured = next(
                (
                    json.loads(c["text"])
                    for c in result.get("content", [])
                    if c.get("type") == "text"
                ),
                None,
            )
        # Transport success alone is not successful backend execution.
        if not isinstance(structured, dict) or structured.get("ok") is not True:
            raise OperationError(
                "CONFORMANCE_FAILED",
                "sections reply does not prove successful backend read",
            )
        return token
    except OperationError as exc:
        if not refresh or exc.code != "AUTH_REQUIRED" or target.profile != "prod":
            raise
        from .release_credential import refresh_token

        refresh_token(target, budget)
        return authenticated_read(target, budget)


def observe_component(target, component, expected, budget):
    try:
        values = unit_values(target.values[f"{component}_unit"], budget)
        if values.get("ActiveState") != "active":
            raise OperationError("UNHEALTHY", f"{component} unit is not active")
        pid = int(values["MainPID"])
        before = process_witness(pid)
        if before["cwd"] != str(expected.dish_root.resolve()):
            raise OperationError(
                "VERSION_MISMATCH",
                f"{component} process directory differs from selected release",
                observed=before,
            )
        python = str((expected.dish_root / ".venv/bin/python").resolve())
        import shlex

        argv = shlex.split(before["arguments"])
        if (
            before["executable"] != python
            or not argv
            or Path(argv[0]).resolve() != Path(python)
            or Path(argv[0]).parent.resolve()
            != (expected.dish_root / ".venv/bin").resolve()
        ):
            raise OperationError(
                "PROCESS_UNKNOWN",
                f"{component} executable/arguments are not release-bound",
                observed=before,
            )
        local = target.values[
            "health_url" if component == "backend" else "mcp_local_url"
        ]
        if not owns_listener(pid, local):
            raise OperationError(
                "LISTENER_UNKNOWN",
                f"{component} process does not own expected listener",
                observed=before,
            )
        if component == "backend":
            identity = request_json(local, budget)
            if identity.get("ok") is not True:
                raise OperationError("UNHEALTHY", "backend readiness failed")
        else:
            token = authenticated_read(target, budget)
            identity = runtime_read(local, token, budget)
            public = runtime_read(target.values["mcp_url"], token, budget)
            if public != identity:
                raise OperationError(
                    "ENDPOINT_IDENTITY",
                    "public MCP and local process witnesses disagree",
                )
        if (
            identity.get("code_release") != expected.source_commit
            or identity.get("pid") != pid
            or str(identity.get("start_ticks")) != before["start_ticks"]
        ):
            raise OperationError(
                "VERSION_MISMATCH",
                f"{component} process-start identity differs from selected release",
                observed={"process": before, "identity": identity},
            )
        if process_witness(pid) != before:
            raise OperationError(
                "PROCESS_CHANGED", f"{component} process changed during observation"
            )
        return {"ok": True, "code_release": identity["code_release"], "process": before}
    except (OSError, ValueError, KeyError, OperationError) as exc:
        return {
            "ok": False,
            "state": getattr(exc, "code", "UNKNOWN"),
            "error": str(exc),
            "observed": getattr(exc, "observed", None),
        }


def selected(target, key="current", *, integrity=True):
    pointer = target.path(key)
    raw = release._pointer_value(pointer)
    if raw is None:
        raise OperationError(
            "NO_RELEASE", f"no {key} release; use the one-time bootstrap runbook"
        )
    path = pointer.resolve()
    if path.parent != target.path("release_root").resolve():
        raise OperationError(
            "POINTER_INVALID", f"{key} release escapes configured root"
        )
    return read_release(target, path.name, integrity=integrity)


def read_release(target, sha, *, integrity=True):
    if integrity:
        return release.load_release(target.path("release_root"), sha)
    if release._SHA.fullmatch(sha) is None:
        raise OperationError("RELEASE_INVALID", "release must be an exact SHA")
    root = target.path("release_root") / sha
    manifest = json.loads((root / release.MANIFEST_NAME).read_text())
    if manifest.get("source_commit") != sha or root.is_symlink():
        raise OperationError("RELEASE_INVALID", "manifest identity mismatch")
    return release.Release(root, sha, manifest["schema_head"], manifest["hashes"])


def database_check(target, candidate, budget):
    env = release._read_env(tuple(map(Path, target.values["env_files"])))
    if env.get("DISH_PG_EXPECTED_SCHEMA_HEAD") != candidate.schema_head:
        raise OperationError(
            "SCHEMA_INCOMPATIBLE",
            "release expects a different schema; use the routine migration runbook",
        )
    run(
        [
            str(candidate.dish_root / ".venv/bin/python"),
            "-m",
            "dish_pg.release_preflight",
        ],
        budget,
        cwd=candidate.dish_root,
        env={**os.environ, **env},
    )


def snapshot(target, budget=None):
    budget = budget or Budget(target.values.get("status_timeout", 3))
    current = selected(target, integrity=False)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            c: pool.submit(observe_component, target, c, current, budget)
            for c in ("backend", "mcp")
        }
        components = {c: f.result() for c, f in futures.items()}
    rollback = False
    previous_sha = None
    try:
        previous = selected(target, "previous", integrity=False)
        previous_sha = previous.source_commit
        rollback = previous.schema_head == current.schema_head and pair_valid(
            target, current, previous, check_only=True
        )
    except (OSError, ValueError, KeyError, release.ReleaseError):
        pass
    pending = (
        json.loads(target.pending.read_text()) if target.pending.exists() else None
    )
    ok = all(c["ok"] for c in components.values()) and pending is None
    return {
        "ok": ok,
        "profile": target.profile,
        "current": current.source_commit,
        "previous": previous_sha,
        "rollback_available": rollback,
        "components": components,
        "observed_at": time.time(),
        "pending_operation": pending,
        "next_action": target.recover_command
        if pending
        else (
            "none"
            if ok
            else "dish/scripts/dish-prod-release"
            + (" --profile test" if target.profile == "test" else "")
            + " deploy latest"
        ),
    }


def require_no_pending(target):
    if target.pending.exists():
        pending = json.loads(target.pending.read_text())
        raise OperationError(
            "PENDING_OPERATION",
            f"operation {pending['id']} is unresolved; run {target.recover_command}",
            observed={"pending_operation": pending},
        )


def pair_path(target, a, b):
    return (
        target.path("state_root")
        / "pairs"
        / f"{a.source_commit}-{b.source_commit}.json"
    )


def pair_binding(target, a, b):
    manifests = [
        json.loads((c.root / release.MANIFEST_NAME).read_text()) for c in (a, b)
    ]
    for m in manifests:
        if m.get("hot_deploy_contract") != CONTRACT:
            raise OperationError(
                "UNSUPPORTED_CONTRACT",
                "release lacks paired-hot-v1; use the one-time bootstrap or migration/config rollout runbook",
            )
    return {
        "candidate": a.source_commit,
        "retained": b.source_commit,
        "schema": a.schema_head,
        "source_digests": [m["source_tree_sha256"] for m in manifests],
        "config": target.fingerprint(),
        "contract": CONTRACT,
    }


def pair_valid(target, a, b, *, check_only=False):
    try:
        payload = json.loads(pair_path(target, a, b).read_text())
        valid = payload.get("binding") == pair_binding(target, a, b) and payload.get(
            "mixed_pairs"
        ) == [True, True]
    except (OSError, ValueError, release.ReleaseError):
        valid = False
    if not valid and not check_only:
        raise OperationError(
            "PREPARE_REQUIRED",
            "exact candidate/retained pair evidence missing or stale; run dish/scripts/dish-prod-release"
            + (" --profile test" if target.profile == "test" else "")
            + " prepare latest",
        )
    return valid


def resolve_latest(target, budget):
    remote = target.values["upstream"]
    if target.profile == "prod" and remote != CANONICAL_REMOTE:
        raise OperationError(
            "UPSTREAM_INVALID", "production latest requires fixed canonical GitHub main"
        )
    output = run(["git", "ls-remote", "--exit-code", remote, "refs/heads/main"], budget)
    sha, ref = output.split()
    if release._SHA.fullmatch(sha) is None or ref != "refs/heads/main":
        raise OperationError(
            "UPSTREAM_INVALID", "canonical main did not resolve exactly"
        )
    print(f"Resolved latest: {sha}", flush=True)
    repository = target.path("repository")
    # Fetch only the pinned object into the object store; no checkout/index changes.
    run(
        [
            "git",
            "-C",
            str(repository),
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            remote,
            sha,
        ],
        budget,
    )
    return sha


def prepare(target, sha, python=None):
    start = time.monotonic()
    candidate = release.stage_release(
        target.path("repository"),
        sha,
        target.path("release_root"),
        python or target.path("python"),
    )
    retained = selected(target)
    fallback = target.values.get("recovery_release")
    if retained.schema_head != candidate.schema_head and fallback:
        retained = read_release(target, fallback)
    pair_binding(target, candidate, retained)
    if not pair_valid(target, candidate, retained, check_only=True):
        from .release_qualification import qualify_pair

        proof = qualify_pair(target, candidate, retained)
        atomic_json(
            pair_path(target, candidate, retained),
            {
                "binding": pair_binding(target, candidate, retained),
                "mixed_pairs": [True, True],
                "proof": proof,
            },
        )
        # Same two mixed pairs certify the reverse direction as well.
        atomic_json(
            pair_path(target, retained, candidate),
            {
                "binding": pair_binding(target, retained, candidate),
                "mixed_pairs": [True, True],
                "proof": proof,
            },
        )
    return candidate, time.monotonic() - start


def pointer_states(target):
    return {
        name: release._pointer_value(target.path(name))
        for name in ("current", "previous")
    }


def set_pointers(target, values):
    for name in ("previous", "current"):
        release._set_pointer(target.path(name), values[name])
        sync_directory(target.path(name).parent)


def checkpoint(target, record, phase):
    record["phase"] = phase
    atomic_json(target.pending, record)


def restart_pair(target, record, budget, *, recovering=False):
    for component in ("backend", "mcp"):
        checkpoint(
            target, record, ("recover-" if recovering else "") + "restart-" + component
        )
        run(
            [
                "/usr/bin/systemctl",
                "--user",
                "restart",
                target.values[f"{component}_unit"],
            ],
            budget,
        )
        checkpoint(
            target,
            record,
            ("recover-" if recovering else "") + component + "-restarted",
        )


def verify_pair(target, candidate, budget):
    while True:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda c: observe_component(target, c, candidate, budget),
                    ("backend", "mcp"),
                )
            )
        if all(item["ok"] for item in results):
            return dict(zip(("backend", "mcp"), results))
        if budget.left() < 0.15:
            raise OperationError(
                "READINESS_FAILED",
                "paired readiness could not be verified",
                observed=results,
            )
        time.sleep(min(0.1, budget.left()))


def terminal(target, record, state, components):
    payload = {
        "id": record["id"],
        "state": state,
        "target": record["target"],
        "pointers": pointer_states(target),
        "components": components,
        "record": record,
        "at": time.time(),
    }
    atomic_json(
        target.path("state_root") / "receipts" / (record["id"] + ".json"), payload
    )
    atomic_json(target.path("state_root") / "last-operation.json", payload)
    target.pending.unlink(missing_ok=True)
    sync_directory(target.pending.parent)
    return payload


def reconcile_record(target, record, budget):
    allowed = record["pointer_states"]
    observed = pointer_states(target)
    if (
        observed not in allowed
        or target.fingerprint() != record["config"]
        or unit_bindings(target, budget) != record["units"]
    ):
        raise OperationError(
            "EXTERNAL_STATE_CHANGED",
            "pointers, effective units or configuration moved outside recorded operation states; restore the recorded configuration before recover",
            observed=observed,
        )
    recovery = read_release(target, record["recovery"])
    database_check(target, recovery, budget)
    for component in ("backend", "mcp"):
        values = unit_values(target.values[f"{component}_unit"], budget)
        pid = int(values.get("MainPID", "0"))
        if not pid:
            continue  # a restart interrupted before process creation is recoverable
        witness = process_witness(pid)
        code_root = Path(witness["cwd"]).parent
        if (
            code_root.parent != target.path("release_root").resolve()
            or code_root.name not in record["expected_versions"]
        ):
            raise OperationError(
                "EXTERNAL_STATE_CHANGED",
                f"{component} process is outside recorded release identities",
                observed=witness,
            )
        actual = read_release(target, code_root.name, integrity=False)
        observed_component = observe_component(target, component, actual, budget)
        if not observed_component["ok"] and observed_component.get("state") in {
            "VERSION_MISMATCH",
            "PROCESS_UNKNOWN",
            "ENDPOINT_IDENTITY",
        }:
            raise OperationError(
                "EXTERNAL_STATE_CHANGED",
                f"{component} runtime identity changed outside recorded pre/post states",
                observed=observed_component,
            )
    return recovery


def recover(target):
    budget = Budget(target.values.get("recovery_timeout", 15))
    if not target.pending.exists():
        last = target.path("state_root") / "last-operation.json"
        if not last.exists():
            raise OperationError(
                "NO_PENDING", "no pending or completed operation to recover"
            )
        receipt = json.loads(last.read_text())
        candidate = reconcile_record(target, receipt["record"], budget)
        current = selected(target)
        del candidate
        if pointer_states(target) != receipt["pointers"]:
            raise OperationError(
                "EXTERNAL_STATE_CHANGED",
                "completed operation pointers changed; recover will not restart",
            )
        receipt["components"] = verify_pair(target, current, budget)
        return {"ok": True, "state": "ALREADY_TERMINAL", "receipt": receipt}
    record = json.loads(target.pending.read_text())
    receipt_path = target.path("state_root") / "receipts" / (record["id"] + ".json")
    recovery = reconcile_record(target, record, budget)
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if (
            receipt["record"]["id"] != record["id"]
            or pointer_states(target) != receipt["pointers"]
        ):
            raise OperationError(
                "EXTERNAL_STATE_CHANGED",
                "terminal operation evidence disagrees with actual pointers",
            )
        current = selected(target)
        components = verify_pair(target, current, budget)
        terminal(target, record, receipt["state"], components)
        return {"ok": True, "state": "ALREADY_TERMINAL", "components": components}
    checkpoint(target, record, "recover-pointers")
    set_pointers(target, record["recovery_pointers"])
    checkpoint(target, record, "recovery-pointers-set")
    restart_pair(target, record, budget, recovering=True)
    components = verify_pair(target, recovery, budget)
    state = (
        "RECOVERED_TO_PRIOR" if record["prior_compatible"] else "RECOVERED_TO_FALLBACK"
    )
    receipt = terminal(target, record, state, components)
    return {"ok": True, "state": state, "receipt": receipt}


def activate(target, candidate):
    start = time.monotonic()
    budget = Budget(target.values.get("timeout", 15))
    prior = selected(target)
    database_check(target, candidate, budget)
    bindings = unit_bindings(target, budget)
    authenticated_read(target, budget, refresh=True)
    recovery = prior
    prior_compatible = True
    try:
        database_check(target, prior, budget)
    except release.ReleaseError:
        prior_compatible = False
        fallback = target.values.get("recovery_release")
        if not fallback:
            raise OperationError(
                "RECOVERY_REQUIRED",
                "current release incompatible; prepare an explicit compatible recovery release before deploy",
            )
        recovery = read_release(target, fallback)
        database_check(target, recovery, budget)
    pair_valid(target, candidate, recovery)
    old = pointer_states(target)
    new = {"current": str(candidate.root), "previous": old["current"]}
    if candidate.root == prior.root:
        new = old.copy()
    recovery_pointers = (
        old
        if prior_compatible
        else {"current": str(recovery.root), "previous": old["previous"]}
    )
    transitional = {"current": old["current"], "previous": new["previous"]}
    recovery_transitional = {
        "current": new["current"],
        "previous": recovery_pointers["previous"],
    }
    record = {
        "id": uuid.uuid4().hex,
        "target": candidate.source_commit,
        "recovery": recovery.source_commit,
        "prior_compatible": prior_compatible,
        "prior_pointers": old,
        "target_pointers": new,
        "recovery_pointers": recovery_pointers,
        "pointer_states": [
            old,
            new,
            transitional,
            recovery_pointers,
            recovery_transitional,
        ],
        "units": bindings,
        "config": target.fingerprint(),
        "expected_versions": [
            prior.source_commit,
            candidate.source_commit,
            recovery.source_commit,
        ],
    }
    checkpoint(target, record, "admitted")
    try:
        checkpoint(target, record, "switch-pointers")
        set_pointers(target, new)
        checkpoint(target, record, "pointers-set")
        restart_pair(target, record, budget)
        components = verify_pair(target, candidate, budget)
        checkpoint(target, record, "candidate-verified")
        receipt = terminal(target, record, "DEPLOYED", components)
        return {
            "ok": True,
            "state": "DEPLOYED",
            "receipt": receipt,
            "activation_seconds": time.monotonic() - start,
        }
    except Exception as exc:
        failure = {
            "code": getattr(exc, "code", "ACTIVATION_FAILED"),
            "error": str(exc),
            "observed": getattr(exc, "observed", None),
        }
        record["failure"] = failure
        checkpoint(target, record, "activation-failed")
        try:
            result = recover(target)
        except Exception as recovery_error:
            raise OperationError(
                "RECOVERY_FAILED",
                "activation and bounded recovery failed; run " + target.recover_command,
                exit_code=4,
                observed={
                    "failure": failure,
                    "recovery_error": str(recovery_error),
                    "pointers": pointer_states(target),
                },
            ) from recovery_error
        raise OperationError(
            result["state"],
            "deployment failed; exact compatible recovery verified",
            exit_code=3,
            observed={"failure": failure, "recovery": result},
        ) from exc


def execute(args):
    defaults = {
        "release_root": release.DEFAULT_RELEASE_ROOT,
        "current": release.DEFAULT_CURRENT,
        "previous": release.DEFAULT_PREVIOUS,
        "unit": release.DEFAULT_UNIT,
        "health_url": release.DEFAULT_HEALTH_URL,
        "mcp_url": "https://laptop.tail46f0b9.ts.net/dish/mcp",
        "mcp_unit": "dish-mcp.service",
        "mcp_token_file": Path.home() / ".config/dish-service/mcp-conformance-token",
        "mcp_receipt_root": Path.home() / ".local/state/dish/release-receipts",
    }
    if any(
        getattr(args, name, default) != default for name, default in defaults.items()
    ):
        raise OperationError(
            "TARGET_OVERRIDE_REJECTED",
            "control commands require the fixed TEST profile/configuration for rehearsal; target overrides are not silently applied to production",
        )
    if (
        getattr(args, "env_files", None)
        and tuple(args.env_files) != release.DEFAULT_ENV_FILES
    ):
        raise OperationError(
            "TARGET_OVERRIDE_REJECTED",
            "custom environments require the fixed TEST target configuration; env_files there retain their declared precedence",
        )
    target = target_config(args.profile)
    command = args.command
    with release._release_lock(target.path("release_root")):
        if command == "status":
            result = snapshot(target)
            return result, 0 if result["ok"] else 1
        if command == "recover":
            return recover(target), 0
        if command in ("deploy", "activate", "rollback"):
            require_no_pending(target)
        preparation = 0
        if command in ("prepare", "deploy"):
            sha = (
                resolve_latest(target, Budget(60))
                if args.target == "latest"
                else args.target
            )
            if command == "prepare" or not (target.path("release_root") / sha).exists():
                candidate, preparation = prepare(target, sha, args.python)
            else:
                candidate = read_release(target, sha)
            if command == "prepare":
                return {
                    "ok": True,
                    "state": "PREPARED",
                    "release": sha,
                    "preparation_seconds": preparation,
                }, 0
        elif command == "rollback":
            candidate = selected(target, "previous")
        else:
            candidate = read_release(target, args.release)
        result = activate(target, candidate)
        result["preparation_seconds"] = preparation
        return result, 0
