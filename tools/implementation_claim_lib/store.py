from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import ClaimError

CLAIM_SCHEMA_VERSION = 2
ROLE = "Implementation"
ACTIVE_STATES = {"claimed", "publishing", "review-ready"}
WRITABLE_STATES = {"claimed", "publishing"}
TERMINAL_STATES = {"released", "superseded"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def require_task_gid(value: str) -> str:
    value = str(value)
    if not value.isdigit() or len(value) < 3:
        raise ClaimError("INVALID_TASK", "task_gid must be an Asana numeric GID", 400)
    return value


def require_repository(value: str) -> str:
    value = str(value or "").strip()
    if not value or "/" not in value or len(value) > 200:
        raise ClaimError("INVALID_REPOSITORY", "repository must be owner/name", 400)
    return value


def require_sha(value: str | None, label: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    text = str(value or "").lower()
    if len(text) != 40 or any(c not in "0123456789abcdef" for c in text):
        raise ClaimError("INVALID_SHA", f"{label} must be a full 40-character Git SHA", 400)
    return text


def require_branch(value: str) -> str:
    branch = str(value or "")
    if not branch.startswith("agent/") or branch.endswith("/") or ".." in branch or branch == "agent/":
        raise ClaimError("INVALID_BRANCH", "Implementation branch must be under agent/*", 400)
    return branch


def require_text(value: str, label: str, *, limit: int = 500) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise ClaimError("INVALID_PROVENANCE", f"{label} must be non-empty and <= {limit} characters", 400)
    return text


def request_digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class ClaimStore:
    """SQLite-backed CAS authority intended to be owned by one shared claim service."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._init_lock = threading.Lock()
        self._initialized = False
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def initialize(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS implementation_claims (
                        repository TEXT NOT NULL,
                        task_gid TEXT NOT NULL,
                        schema_version INTEGER NOT NULL,
                        role TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        claim_id TEXT NOT NULL,
                        writer_capability_hash TEXT,
                        previous_claim_id TEXT,
                        owner TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        host TEXT NOT NULL,
                        authoring_base_sha TEXT NOT NULL,
                        state TEXT NOT NULL,
                        branch TEXT,
                        branch_head TEXT,
                        pr_number INTEGER,
                        pr_head TEXT,
                        claimed_at TEXT NOT NULL,
                        last_renewed_at TEXT NOT NULL,
                        released_at TEXT,
                        superseded_at TEXT,
                        transition_reason TEXT,
                        liveness_evidence TEXT,
                        asana_sync_state TEXT NOT NULL,
                        asana_synced_at TEXT,
                        last_event TEXT NOT NULL,
                        PRIMARY KEY (repository, task_gid),
                        UNIQUE (claim_id)
                    );

                    CREATE TABLE IF NOT EXISTS implementation_claim_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        repository TEXT NOT NULL,
                        task_gid TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        claim_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        snapshot_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS implementation_claim_events_task
                    ON implementation_claim_events(repository, task_gid, event_id);

                    CREATE TABLE IF NOT EXISTS implementation_branch_lineage (
                        repository TEXT NOT NULL,
                        branch TEXT NOT NULL,
                        task_gid TEXT NOT NULL,
                        first_claim_id TEXT NOT NULL,
                        bound_at TEXT NOT NULL,
                        PRIMARY KEY (repository, branch)
                    );

                    CREATE TABLE IF NOT EXISTS implementation_pr_lineage (
                        repository TEXT NOT NULL,
                        pr_number INTEGER NOT NULL,
                        task_gid TEXT NOT NULL,
                        branch TEXT NOT NULL,
                        first_claim_id TEXT NOT NULL,
                        bound_at TEXT NOT NULL,
                        PRIMARY KEY (repository, pr_number)
                    );

                    CREATE TABLE IF NOT EXISTS implementation_publications (
                        repository TEXT NOT NULL,
                        task_gid TEXT NOT NULL,
                        request_id TEXT NOT NULL,
                        request_digest TEXT NOT NULL,
                        claim_id TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        branch TEXT NOT NULL,
                        expected_head TEXT,
                        proposed_head TEXT NOT NULL,
                        state TEXT NOT NULL,
                        result_head TEXT,
                        pr_number INTEGER,
                        created_at TEXT NOT NULL,
                        completed_at TEXT,
                        PRIMARY KEY (repository, task_gid, request_id)
                    );
                    """
                )
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(implementation_claims)")}
                if "writer_capability_hash" not in columns:
                    # Pre-v2 rows are intentionally left without writer authority. They can
                    # only be recovered through an explicitly authorized takeover, which mints
                    # a fresh capability for a fresh generation.
                    conn.execute("ALTER TABLE implementation_claims ADD COLUMN writer_capability_hash TEXT")
            finally:
                conn.close()
            self._initialized = True

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    @staticmethod
    def _row_to_claim(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        claim = dict(row)
        # Writer authority is intentionally not part of the public claim projection.
        # Neither the credential nor its hash may escape through status/conflict/Asana views.
        claim.pop("writer_capability_hash", None)
        claim["generation"] = int(claim["generation"])
        claim["pr_number"] = int(claim["pr_number"]) if claim["pr_number"] is not None else None
        claim["writable"] = claim["state"] in WRITABLE_STATES and claim["asana_sync_state"] == "synced"
        # Any existing durable generation must be explicitly continued or replaced by
        # exact-generation CAS. Ordinary dispatch is legal only when no claim lineage
        # exists at all; terminal generations are not an ABA-safe implicit unlock.
        claim["dispatch_blocked"] = True
        return claim

    def _select(self, conn: sqlite3.Connection, repository: str, task_gid: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM implementation_claims WHERE repository=? AND task_gid=?",
            (repository, task_gid),
        ).fetchone()
        return self._row_to_claim(row)

    def read(self, repository: str, task_gid: str) -> dict[str, Any] | None:
        repository = require_repository(repository)
        task_gid = require_task_gid(task_gid)
        conn = self._connect()
        try:
            return self._select(conn, repository, task_gid)
        finally:
            conn.close()

    def events(self, repository: str, task_gid: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM implementation_claim_events WHERE repository=? AND task_gid=? ORDER BY event_id",
                (require_repository(repository), require_task_gid(task_gid)),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def publication(self, repository: str, task_gid: str, request_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM implementation_publications WHERE repository=? AND task_gid=? AND request_id=?",
                (require_repository(repository), require_task_gid(task_gid), request_id),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def _append_event(self, conn: sqlite3.Connection, claim: dict[str, Any], event_type: str) -> None:
        snapshot = {k: v for k, v in claim.items() if k not in {"writable", "dispatch_blocked"}}
        conn.execute(
            """INSERT INTO implementation_claim_events
               (repository,task_gid,generation,claim_id,event_type,created_at,snapshot_json)
               VALUES (?,?,?,?,?,?,?)""",
            (
                claim["repository"], claim["task_gid"], claim["generation"], claim["claim_id"], event_type,
                utc_now(), json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
            ),
        )

    def _current_or_error(self, conn: sqlite3.Connection, repository: str, task_gid: str) -> dict[str, Any]:
        current = self._select(conn, repository, task_gid)
        if current is None:
            raise ClaimError("CLAIM_MISSING", "no durable Implementation claim exists for this task", 404)
        return current

    @staticmethod
    def _expect_claim(current: dict[str, Any], claim_id: str) -> None:
        if current["claim_id"] != claim_id:
            raise ClaimError("OWNERSHIP_CONFLICT", "claim_id is stale or belongs to another generation", 409, current=current)

    @staticmethod
    def _capability_hash(writer_capability: str) -> str:
        value = require_text(writer_capability, "writer capability", limit=512)
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _expect_writer(
        self, conn: sqlite3.Connection, current: dict[str, Any], claim_id: str, writer_capability: str
    ) -> None:
        self._expect_claim(current, claim_id)
        row = conn.execute(
            "SELECT writer_capability_hash FROM implementation_claims WHERE repository=? AND task_gid=? AND claim_id=?",
            (current["repository"], current["task_gid"], claim_id),
        ).fetchone()
        stored = row["writer_capability_hash"] if row is not None else None
        supplied = self._capability_hash(writer_capability)
        if not stored or not hmac.compare_digest(str(stored), supplied):
            raise ClaimError(
                "WRITER_AUTHORITY_DENIED",
                "writer capability does not authorize the current claim generation",
                403,
                current=current,
            )

    def verify_writer(
        self, repository: str, task_gid: str, claim_id: str, writer_capability: str
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            current = self._current_or_error(conn, require_repository(repository), require_task_gid(task_gid))
            self._expect_writer(conn, current, claim_id, writer_capability)
            return current
        finally:
            conn.close()

    @staticmethod
    def _assert_writable(current: dict[str, Any]) -> None:
        if current["state"] not in WRITABLE_STATES:
            raise ClaimError("CLAIM_NOT_WRITABLE", f"claim state {current['state']!r} is not writable", 409, current=current)
        if current["asana_sync_state"] != "synced":
            raise ClaimError(
                "ORCHESTRATION_SYNC_PENDING",
                "claim is fenced until its exact generation is durably mirrored to Asana",
                503,
                current=current,
            )

    def _bind_branch_tx(self, conn: sqlite3.Connection, current: dict[str, Any], branch: str) -> str:
        branch = require_branch(branch)
        row = conn.execute(
            "SELECT task_gid FROM implementation_branch_lineage WHERE repository=? AND branch=?",
            (current["repository"], branch),
        ).fetchone()
        if row is not None and row["task_gid"] != current["task_gid"]:
            raise ClaimError("LINEAGE_CONFLICT", f"branch {branch!r} is already bound to task {row['task_gid']}", 409)
        if current["branch"] not in (None, branch):
            raise ClaimError("LINEAGE_CONFLICT", f"task is already bound to branch {current['branch']!r}", 409, current=current)
        if row is None:
            conn.execute(
                "INSERT INTO implementation_branch_lineage(repository,branch,task_gid,first_claim_id,bound_at) VALUES (?,?,?,?,?)",
                (current["repository"], branch, current["task_gid"], current["claim_id"], utc_now()),
            )
        return branch

    def acquire(self, *, repository: str, task_gid: str, owner: str, session_id: str, host: str,
                authoring_base_sha: str, writer_capability: str, branch: str | None = None) -> dict[str, Any]:
        repository = require_repository(repository)
        task_gid = require_task_gid(task_gid)
        owner = require_text(owner, "owner", limit=200)
        session_id = require_text(session_id, "session_id", limit=200)
        host = require_text(host, "host", limit=200)
        authoring_base_sha = str(require_sha(authoring_base_sha, "authoring_base_sha"))
        writer_capability_hash = self._capability_hash(writer_capability)
        with self._write() as conn:
            existing = self._select(conn, repository, task_gid)
            if existing is not None:
                raise ClaimError(
                    "OWNERSHIP_CONFLICT",
                    "a durable claim generation already exists; continuation or exact-generation takeover is required",
                    409,
                    current=existing,
                )
            now = utc_now()
            claim_id = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO implementation_claims
                   (repository,task_gid,schema_version,role,generation,claim_id,writer_capability_hash,previous_claim_id,owner,session_id,host,
                    authoring_base_sha,state,branch,branch_head,pr_number,pr_head,claimed_at,last_renewed_at,released_at,
                    superseded_at,transition_reason,liveness_evidence,asana_sync_state,asana_synced_at,last_event)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    repository, task_gid, CLAIM_SCHEMA_VERSION, ROLE, 1, claim_id, writer_capability_hash, None, owner, session_id, host,
                    authoring_base_sha, "claimed", None, None, None, None, now, now, None, None, None, None,
                    "pending", None, "acquired",
                ),
            )
            current = self._select(conn, repository, task_gid)
            assert current is not None
            if branch is not None:
                bound = self._bind_branch_tx(conn, current, branch)
                conn.execute(
                    "UPDATE implementation_claims SET branch=? WHERE repository=? AND task_gid=? AND claim_id=?",
                    (bound, repository, task_gid, claim_id),
                )
                current = self._select(conn, repository, task_gid)
                assert current is not None
            self._append_event(conn, current, "acquired")
            return current

    def takeover(self, *, repository: str, task_gid: str, expected_claim_id: str, owner: str, session_id: str,
                 host: str, authoring_base_sha: str, reason: str, liveness_evidence: str,
                 writer_capability: str, recovery_authorized: bool) -> dict[str, Any]:
        repository = require_repository(repository)
        task_gid = require_task_gid(task_gid)
        owner = require_text(owner, "owner", limit=200)
        session_id = require_text(session_id, "session_id", limit=200)
        host = require_text(host, "host", limit=200)
        reason = require_text(reason, "takeover reason")
        liveness_evidence = require_text(liveness_evidence, "liveness evidence", limit=2000)
        authoring_base_sha = str(require_sha(authoring_base_sha, "authoring_base_sha"))
        writer_capability_hash = self._capability_hash(writer_capability)
        if not recovery_authorized:
            raise ClaimError(
                "RECOVERY_AUTHORITY_REQUIRED",
                "takeover requires distinct recovery/orchestration authority",
                403,
            )
        with self._write() as conn:
            current = self._current_or_error(conn, repository, task_gid)
            self._expect_claim(current, expected_claim_id)
            pending = conn.execute(
                "SELECT request_id FROM implementation_publications WHERE repository=? AND task_gid=? AND state='pending' LIMIT 1",
                (repository, task_gid),
            ).fetchone()
            if pending is not None:
                raise ClaimError(
                    "PUBLICATION_PENDING",
                    f"publication {pending['request_id']} is unresolved; reconcile or abort it before takeover",
                    409,
                    current=current,
                )
            if current["authoring_base_sha"] != authoring_base_sha:
                raise ClaimError("BASE_MISMATCH", "takeover must preserve the authoritative authoring base SHA", 409, current=current)
            old_id = current["claim_id"]
            new_id = uuid.uuid4().hex
            now = utc_now()
            cursor = conn.execute(
                """UPDATE implementation_claims
                   SET schema_version=?,generation=?,claim_id=?,writer_capability_hash=?,previous_claim_id=?,owner=?,session_id=?,host=?,state='claimed',
                       claimed_at=?,last_renewed_at=?,released_at=NULL,superseded_at=NULL,transition_reason=?,
                       liveness_evidence=?,asana_sync_state='pending',asana_synced_at=NULL,last_event='takeover'
                   WHERE repository=? AND task_gid=? AND claim_id=?""",
                (
                    CLAIM_SCHEMA_VERSION, current["generation"] + 1, new_id, writer_capability_hash, old_id, owner, session_id, host, now, now, reason,
                    liveness_evidence, repository, task_gid, old_id,
                ),
            )
            if cursor.rowcount != 1:
                latest = self._select(conn, repository, task_gid)
                raise ClaimError("OWNERSHIP_CONFLICT", "claim generation changed during takeover", 409, current=latest)
            current = self._select(conn, repository, task_gid)
            assert current is not None
            self._append_event(conn, current, "takeover")
            return current

    def mark_asana_synced(self, repository: str, task_gid: str, claim_id: str) -> dict[str, Any]:
        with self._write() as conn:
            current = self._current_or_error(conn, require_repository(repository), require_task_gid(task_gid))
            self._expect_claim(current, claim_id)
            now = utc_now()
            conn.execute(
                "UPDATE implementation_claims SET asana_sync_state='synced',asana_synced_at=? WHERE repository=? AND task_gid=? AND claim_id=?",
                (now, repository, task_gid, claim_id),
            )
            current = self._select(conn, repository, task_gid)
            assert current is not None
            return current

    def authorize(self, repository: str, task_gid: str, claim_id: str, writer_capability: str, *, branch: str | None = None) -> dict[str, Any]:
        conn = self._connect()
        try:
            current = self._current_or_error(conn, require_repository(repository), require_task_gid(task_gid))
            self._expect_writer(conn, current, claim_id, writer_capability)
            self._assert_writable(current)
            if branch is not None:
                branch = require_branch(branch)
                if current["branch"] != branch:
                    raise ClaimError("LINEAGE_CONFLICT", "claim is not bound to the requested branch", 409, current=current)
            return current
        finally:
            conn.close()

    def renew(self, repository: str, task_gid: str, claim_id: str, writer_capability: str) -> dict[str, Any]:
        with self._write() as conn:
            current = self._current_or_error(conn, require_repository(repository), require_task_gid(task_gid))
            self._expect_writer(conn, current, claim_id, writer_capability)
            self._assert_writable(current)
            conn.execute(
                "UPDATE implementation_claims SET last_renewed_at=?,last_event='renewed' WHERE repository=? AND task_gid=? AND claim_id=?",
                (utc_now(), repository, task_gid, claim_id),
            )
            current = self._select(conn, repository, task_gid)
            assert current is not None
            self._append_event(conn, current, "renewed")
            return current

    def bind_branch(self, repository: str, task_gid: str, claim_id: str, writer_capability: str, branch: str) -> dict[str, Any]:
        with self._write() as conn:
            current = self._current_or_error(conn, require_repository(repository), require_task_gid(task_gid))
            self._expect_writer(conn, current, claim_id, writer_capability)
            self._assert_writable(current)
            bound = self._bind_branch_tx(conn, current, branch)
            conn.execute(
                """UPDATE implementation_claims SET branch=?,asana_sync_state='pending',asana_synced_at=NULL,last_event='branch-bound'
                   WHERE repository=? AND task_gid=? AND claim_id=?""",
                (bound, repository, task_gid, claim_id),
            )
            current = self._select(conn, repository, task_gid)
            assert current is not None
            self._append_event(conn, current, "branch-bound")
            return current

    def bind_pr(self, repository: str, task_gid: str, claim_id: str, writer_capability: str, *, pr_number: int, pr_head: str) -> dict[str, Any]:
        pr_head = str(require_sha(pr_head, "pr_head"))
        if not isinstance(pr_number, int) or pr_number <= 0:
            raise ClaimError("INVALID_PR", "pr_number must be a positive integer", 400)
        with self._write() as conn:
            current = self._current_or_error(conn, require_repository(repository), require_task_gid(task_gid))
            self._expect_writer(conn, current, claim_id, writer_capability)
            self._assert_writable(current)
            branch = current["branch"]
            if branch is None:
                raise ClaimError("BRANCH_REQUIRED", "bind the authoritative branch before binding a PR", 409, current=current)
            existing = conn.execute(
                "SELECT task_gid,branch FROM implementation_pr_lineage WHERE repository=? AND pr_number=?",
                (repository, pr_number),
            ).fetchone()
            if existing is not None and (existing["task_gid"] != task_gid or existing["branch"] != branch):
                raise ClaimError("LINEAGE_CONFLICT", f"PR #{pr_number} is already bound to another lineage", 409)
            if current["pr_number"] not in (None, pr_number):
                raise ClaimError("LINEAGE_CONFLICT", f"task is already bound to PR #{current['pr_number']}", 409, current=current)
            if existing is None:
                conn.execute(
                    "INSERT INTO implementation_pr_lineage(repository,pr_number,task_gid,branch,first_claim_id,bound_at) VALUES (?,?,?,?,?,?)",
                    (repository, pr_number, task_gid, branch, claim_id, utc_now()),
                )
            conn.execute(
                """UPDATE implementation_claims
                   SET pr_number=?,pr_head=?,branch_head=?,asana_sync_state='pending',asana_synced_at=NULL,last_event='pr-bound'
                   WHERE repository=? AND task_gid=? AND claim_id=?""",
                (pr_number, pr_head, pr_head, repository, task_gid, claim_id),
            )
            current = self._select(conn, repository, task_gid)
            assert current is not None
            self._append_event(conn, current, "pr-bound")
            return current

    def begin_publication(self, repository: str, task_gid: str, claim_id: str, writer_capability: str, *, branch: str,
                          expected_head: str | None, proposed_head: str, request_id: str) -> tuple[dict[str, Any], dict[str, Any], bool]:
        repository = require_repository(repository)
        task_gid = require_task_gid(task_gid)
        branch = require_branch(branch)
        expected_head = require_sha(expected_head, "expected_head", allow_none=True)
        proposed_head = str(require_sha(proposed_head, "proposed_head"))
        request_id = require_text(request_id, "request_id", limit=200)
        digest = request_digest(
            {"repository": repository, "task_gid": task_gid, "claim_id": claim_id, "branch": branch,
             "expected_head": expected_head, "proposed_head": proposed_head}
        )
        with self._write() as conn:
            current = self._current_or_error(conn, repository, task_gid)
            self._expect_writer(conn, current, claim_id, writer_capability)
            self._assert_writable(current)
            if current["branch"] != branch:
                raise ClaimError("LINEAGE_CONFLICT", "publication branch does not match the authoritative claim lineage", 409, current=current)
            if current["branch_head"] != expected_head:
                raise ClaimError("HEAD_MOVED", f"claim records branch head {current['branch_head']!r}, not expected {expected_head!r}", 409, current=current)
            existing = conn.execute(
                "SELECT * FROM implementation_publications WHERE repository=? AND task_gid=? AND request_id=?",
                (repository, task_gid, request_id),
            ).fetchone()
            if existing is not None:
                publication = dict(existing)
                if publication["request_digest"] != digest:
                    raise ClaimError("IDEMPOTENCY_CONFLICT", "request_id was already used with a different publication identity", 409)
                return current, publication, True
            pending = conn.execute(
                "SELECT request_id FROM implementation_publications WHERE repository=? AND task_gid=? AND state='pending' LIMIT 1",
                (repository, task_gid),
            ).fetchone()
            if pending is not None:
                raise ClaimError(
                    "PUBLICATION_PENDING",
                    f"publication {pending['request_id']} is already unresolved for this claim lineage",
                    409,
                    current=current,
                )
            conn.execute(
                """INSERT INTO implementation_publications
                   (repository,task_gid,request_id,request_digest,claim_id,generation,branch,expected_head,proposed_head,state,
                    result_head,pr_number,created_at,completed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,'pending',NULL,NULL,?,NULL)""",
                (repository, task_gid, request_id, digest, claim_id, current["generation"], branch, expected_head, proposed_head, utc_now()),
            )
            conn.execute(
                """UPDATE implementation_claims
                   SET state='publishing',asana_sync_state='pending',asana_synced_at=NULL,last_event='publication-begin'
                   WHERE repository=? AND task_gid=? AND claim_id=?""",
                (repository, task_gid, claim_id),
            )
            current = self._select(conn, repository, task_gid)
            publication = dict(conn.execute(
                "SELECT * FROM implementation_publications WHERE repository=? AND task_gid=? AND request_id=?",
                (repository, task_gid, request_id),
            ).fetchone())
            assert current is not None
            self._append_event(conn, current, "publication-begin")
            return current, publication, False

    def complete_publication(self, repository: str, task_gid: str, claim_id: str, writer_capability: str, *, request_id: str,
                             result_head: str, pr_number: int | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        result_head = str(require_sha(result_head, "result_head"))
        with self._write() as conn:
            current = self._current_or_error(conn, require_repository(repository), require_task_gid(task_gid))
            self._expect_writer(conn, current, claim_id, writer_capability)
            row = conn.execute(
                "SELECT * FROM implementation_publications WHERE repository=? AND task_gid=? AND request_id=?",
                (repository, task_gid, request_id),
            ).fetchone()
            if row is None:
                raise ClaimError("PUBLICATION_MISSING", "publication intent does not exist", 404)
            publication = dict(row)
            if publication["claim_id"] != claim_id:
                raise ClaimError("OWNERSHIP_CONFLICT", "publication belongs to a stale claim generation", 409, current=current)
            if publication["proposed_head"] != result_head:
                raise ClaimError("CONTENT_IDENTITY_MISMATCH", "publication result does not equal the authorized proposed head", 409)
            if publication["state"] == "completed":
                if publication["result_head"] != result_head or publication["pr_number"] != pr_number:
                    raise ClaimError("IDEMPOTENCY_CONFLICT", "completed publication result differs from replay", 409)
                return current, publication
            if publication["state"] != "pending":
                raise ClaimError("PUBLICATION_NOT_PENDING", f"publication is {publication['state']!r}", 409)
            if pr_number is not None:
                if not isinstance(pr_number, int) or pr_number <= 0:
                    raise ClaimError("INVALID_PR", "pr_number must be a positive integer", 400)
                branch = current["branch"]
                assert branch is not None
                existing = conn.execute(
                    "SELECT task_gid,branch FROM implementation_pr_lineage WHERE repository=? AND pr_number=?",
                    (repository, pr_number),
                ).fetchone()
                if existing is not None and (existing["task_gid"] != task_gid or existing["branch"] != branch):
                    raise ClaimError("LINEAGE_CONFLICT", f"PR #{pr_number} belongs to another lineage", 409)
                if current["pr_number"] not in (None, pr_number):
                    raise ClaimError("LINEAGE_CONFLICT", f"task already owns PR #{current['pr_number']}", 409)
                if existing is None:
                    conn.execute(
                        "INSERT INTO implementation_pr_lineage(repository,pr_number,task_gid,branch,first_claim_id,bound_at) VALUES (?,?,?,?,?,?)",
                        (repository, pr_number, task_gid, branch, claim_id, utc_now()),
                    )
            conn.execute(
                """UPDATE implementation_publications SET state='completed',result_head=?,pr_number=?,completed_at=?
                   WHERE repository=? AND task_gid=? AND request_id=? AND state='pending'""",
                (result_head, pr_number, utc_now(), repository, task_gid, request_id),
            )
            conn.execute(
                """UPDATE implementation_claims
                   SET state='publishing',branch_head=?,pr_number=COALESCE(?,pr_number),
                       pr_head=CASE WHEN ? IS NULL THEN pr_head ELSE ? END,
                       asana_sync_state='pending',asana_synced_at=NULL,last_event='publication-complete'
                   WHERE repository=? AND task_gid=? AND claim_id=?""",
                (result_head, pr_number, pr_number, result_head, repository, task_gid, claim_id),
            )
            current = self._select(conn, repository, task_gid)
            publication = dict(conn.execute(
                "SELECT * FROM implementation_publications WHERE repository=? AND task_gid=? AND request_id=?",
                (repository, task_gid, request_id),
            ).fetchone())
            assert current is not None
            self._append_event(conn, current, "publication-complete")
            return current, publication

    def abort_publication(self, repository: str, task_gid: str, claim_id: str, writer_capability: str, *, request_id: str) -> dict[str, Any]:
        with self._write() as conn:
            current = self._current_or_error(conn, require_repository(repository), require_task_gid(task_gid))
            self._expect_writer(conn, current, claim_id, writer_capability)
            row = conn.execute(
                "SELECT * FROM implementation_publications WHERE repository=? AND task_gid=? AND request_id=?",
                (repository, task_gid, request_id),
            ).fetchone()
            if row is None:
                raise ClaimError("PUBLICATION_MISSING", "publication intent does not exist", 404)
            publication = dict(row)
            if publication["claim_id"] != claim_id or publication["state"] != "pending":
                raise ClaimError("PUBLICATION_NOT_PENDING", "only the current claim may abort its pending publication", 409)
            conn.execute(
                "UPDATE implementation_publications SET state='aborted',completed_at=? WHERE repository=? AND task_gid=? AND request_id=?",
                (utc_now(), repository, task_gid, request_id),
            )
            conn.execute(
                """UPDATE implementation_claims
                   SET state='claimed',asana_sync_state='pending',asana_synced_at=NULL,last_event='publication-abort'
                   WHERE repository=? AND task_gid=? AND claim_id=?""",
                (repository, task_gid, claim_id),
            )
            current = self._select(conn, repository, task_gid)
            assert current is not None
            self._append_event(conn, current, "publication-abort")
            return current

    def review_ready(self, repository: str, task_gid: str, claim_id: str, writer_capability: str, *, pr_number: int, pr_head: str) -> dict[str, Any]:
        pr_head = str(require_sha(pr_head, "pr_head"))
        with self._write() as conn:
            current = self._current_or_error(conn, require_repository(repository), require_task_gid(task_gid))
            self._expect_writer(conn, current, claim_id, writer_capability)
            self._assert_writable(current)
            if current["pr_number"] != pr_number or current["pr_head"] != pr_head or current["branch_head"] != pr_head:
                raise ClaimError("PR_LINEAGE_MISMATCH", "review-ready identity must equal the bound exact PR/head lineage", 409, current=current)
            pending = conn.execute(
                "SELECT request_id FROM implementation_publications WHERE repository=? AND task_gid=? AND state='pending' LIMIT 1",
                (repository, task_gid),
            ).fetchone()
            if pending is not None:
                raise ClaimError("PUBLICATION_PENDING", f"publication {pending['request_id']} is unresolved", 409)
            conn.execute(
                """UPDATE implementation_claims
                   SET state='review-ready',asana_sync_state='pending',asana_synced_at=NULL,last_event='review-ready'
                   WHERE repository=? AND task_gid=? AND claim_id=?""",
                (repository, task_gid, claim_id),
            )
            current = self._select(conn, repository, task_gid)
            assert current is not None
            self._append_event(conn, current, "review-ready")
            return current

    def release(self, repository: str, task_gid: str, claim_id: str, writer_capability: str, *, reason: str) -> dict[str, Any]:
        reason = require_text(reason, "release reason")
        with self._write() as conn:
            current = self._current_or_error(conn, require_repository(repository), require_task_gid(task_gid))
            self._expect_writer(conn, current, claim_id, writer_capability)
            if current["state"] == "released":
                return current
            if current["state"] == "superseded":
                raise ClaimError("OWNERSHIP_CONFLICT", "superseded claim cannot be released by its old owner", 409, current=current)
            pending = conn.execute(
                "SELECT request_id FROM implementation_publications WHERE repository=? AND task_gid=? AND state='pending' LIMIT 1",
                (repository, task_gid),
            ).fetchone()
            if pending is not None:
                raise ClaimError("PUBLICATION_PENDING", f"publication {pending['request_id']} is unresolved", 409, current=current)
            now = utc_now()
            conn.execute(
                """UPDATE implementation_claims
                   SET state='released',released_at=?,asana_sync_state='pending',asana_synced_at=NULL,last_event='released',transition_reason=?
                   WHERE repository=? AND task_gid=? AND claim_id=?""",
                (now, reason, repository, task_gid, claim_id),
            )
            current = self._select(conn, repository, task_gid)
            assert current is not None
            self._append_event(conn, current, "released")
            return current

    def supersede(self, repository: str, task_gid: str, claim_id: str, writer_capability: str, *, reason: str) -> dict[str, Any]:
        reason = require_text(reason, "supersede reason")
        with self._write() as conn:
            current = self._current_or_error(conn, require_repository(repository), require_task_gid(task_gid))
            self._expect_writer(conn, current, claim_id, writer_capability)
            pending = conn.execute(
                "SELECT request_id FROM implementation_publications WHERE repository=? AND task_gid=? AND state='pending' LIMIT 1",
                (repository, task_gid),
            ).fetchone()
            if pending is not None:
                raise ClaimError("PUBLICATION_PENDING", f"publication {pending['request_id']} is unresolved", 409, current=current)
            now = utc_now()
            conn.execute(
                """UPDATE implementation_claims
                   SET state='superseded',superseded_at=?,asana_sync_state='pending',asana_synced_at=NULL,last_event='superseded',transition_reason=?
                   WHERE repository=? AND task_gid=? AND claim_id=?""",
                (now, reason, repository, task_gid, claim_id),
            )
            current = self._select(conn, repository, task_gid)
            assert current is not None
            self._append_event(conn, current, "superseded")
            return current
