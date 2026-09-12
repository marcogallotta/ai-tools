"""Environment-bound routine PostgreSQL migration with durable release evidence."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .native_catalog_runtime_finalizer import finalize_native_catalog_runtime_authority
from .schema_identity import ALEMBIC_HEAD

DISH_ROOT = Path(__file__).resolve().parents[1]
_SYSTEM_DATABASES = frozenset({"postgres", "template0", "template1"})
_MAX_DETAIL = 400
_NATIVE_PLACEMENT_STAGING_REVISION = "0051_native_dish_state_placement"


class RoutineMigrationError(RuntimeError):
    """A routine migration safety invariant failed."""

    def __init__(
        self,
        rule: str,
        message: str,
        *,
        retryable: bool = False,
        next_action: str = "Stop promotion and investigate before any service restart.",
    ) -> None:
        super().__init__(message)
        self.rule = rule
        self.retryable = retryable
        self.next_action = next_action


@dataclass
class EvidenceJournal:
    path: Path
    fd: int

    @classmethod
    def create(cls, path: Path) -> "EvidenceJournal":
        path = path.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return cls(path=path, fd=fd)

    def write(self, payload: dict[str, Any]) -> None:
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        os.write(self.fd, encoded)
        os.fsync(self.fd)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, choices=("test", "production"))
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--expected-database-name", required=True)
    parser.add_argument(
        "--source-commit",
        required=True,
        help="release/source commit; must resolve to the exact checked-out HEAD",
    )
    parser.add_argument("--evidence-file", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="preflight only; never mutate")
    mode.add_argument("--apply", action="store_true", help="apply to exact ALEMBIC_HEAD")
    mode.add_argument(
        "--apply-native-placement",
        action="store_true",
        help="apply 0051, establish and verify the native runtime root, then apply 0052",
    )
    parser.add_argument(
        "--confirm-database-name",
        help="production apply only: must exactly equal --expected-database-name",
    )
    return parser


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_source_commit(value: str) -> str:
    def git(*args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(DISH_ROOT), *args],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RoutineMigrationError(
                "source_commit_unresolvable",
                "source commit cannot be resolved in this repository checkout",
            )
        return completed.stdout.strip()

    resolved = git("rev-parse", "--verify", f"{value}^{{commit}}")
    head = git("rev-parse", "--verify", "HEAD")
    if resolved != head:
        raise RoutineMigrationError(
            "source_commit_checkout_mismatch",
            "source commit does not match the exact checked-out code used for migration",
            next_action="Check out the reviewed release commit, then rerun preflight.",
        )
    if not re.fullmatch(r"[0-9a-f]{40}", resolved):
        raise RoutineMigrationError(
            "source_commit_not_exact",
            "resolved source commit is not an exact 40-character Git object ID",
        )
    return resolved


def _alembic_config(database_url: str | None = None) -> Config:
    cfg = Config(str(DISH_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(DISH_ROOT / "dish_pg/migrations"))
    if database_url is not None:
        cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _repository_script() -> ScriptDirectory:
    script = ScriptDirectory.from_config(_alembic_config())
    heads = tuple(sorted(script.get_heads()))
    if heads != (ALEMBIC_HEAD,):
        raise RoutineMigrationError(
            "repository_head_inconsistency",
            "repository migration heads do not exactly match dish_pg.schema_identity.ALEMBIC_HEAD",
            next_action="Fix/review repository migration authority before touching any database.",
        )
    return script


def _validate_target(
    *,
    environment: str,
    database_url: str,
    expected_database_name: str,
    apply: bool,
    confirmation: str | None,
) -> None:
    try:
        selected = make_url(database_url)
    except Exception as exc:
        raise RoutineMigrationError("database_url_invalid", "database URL is invalid") from exc
    if selected.get_backend_name() != "postgresql":
        raise RoutineMigrationError("database_backend_wrong", "database URL must use PostgreSQL")
    if selected.database != expected_database_name:
        raise RoutineMigrationError(
            "database_url_identity_mismatch",
            "database URL identity does not exactly match the expected database name",
        )
    if expected_database_name in _SYSTEM_DATABASES:
        raise RoutineMigrationError(
            "database_identity_system_database",
            "routine migration refuses PostgreSQL system databases",
        )
    lowered = expected_database_name.lower()
    if environment == "test":
        if (
            not expected_database_name.startswith("dish_")
            or not expected_database_name.endswith("_test")
            or "prod" in lowered
            or "production" in lowered
        ):
            raise RoutineMigrationError(
                "test_database_identity_not_disposable",
                "TEST migration requires a disposable dish_*_test database identity with no production marker",
            )
    elif expected_database_name.endswith("_test"):
        raise RoutineMigrationError(
            "production_database_identity_is_test",
            "production migration refuses a database name ending in '_test'",
        )
    if environment == "production" and apply and confirmation != expected_database_name:
        raise RoutineMigrationError(
            "production_confirmation_mismatch",
            "production apply requires --confirm-database-name to exactly match the expected database name",
            next_action="Marco must rerun the reviewed command with the exact production database confirmation.",
        )


def _read_database_state(database_url: str) -> tuple[str, tuple[str, ...]]:
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            if connection.dialect.name == "postgresql":
                database_name = str(connection.scalar(text("SELECT current_database()")))
            else:
                database_name = str(make_url(database_url).database or "")
            heads = tuple(sorted(MigrationContext.configure(connection).get_current_heads()))
        return database_name, heads
    finally:
        engine.dispose()


def _validate_current_revision(script: ScriptDirectory, heads: tuple[str, ...]) -> str:
    if not heads:
        raise RoutineMigrationError(
            "database_revision_missing",
            "database has no Alembic revision; routine migration is not a bootstrap path",
        )
    if len(heads) != 1:
        raise RoutineMigrationError(
            "database_multiple_heads",
            "database has multiple Alembic heads; routine migration refuses branch ambiguity",
        )
    current = heads[0]
    if current == ALEMBIC_HEAD:
        return "current"
    expected_ancestors = {
        revision.revision for revision in script.iterate_revisions(ALEMBIC_HEAD, "base")
    }
    if current in expected_ancestors:
        return "behind"
    known = {revision.revision for revision in script.walk_revisions()}
    if current in known:
        raise RoutineMigrationError(
            "database_revision_divergent",
            "database revision is known to this repository but is not an ancestor of the expected head",
        )
    raise RoutineMigrationError(
        "database_revision_ahead_or_unexpected",
        "database revision is not in the expected repository history; it may be ahead, divergent, or foreign",
    )


def _redacted_detail(exc: BaseException, database_url: str) -> str:
    detail = str(exc).replace("\n", " ").strip()
    try:
        selected = make_url(database_url)
        password = selected.password
        if password:
            detail = detail.replace(str(password), "***")
        detail = detail.replace(database_url, selected.render_as_string(hide_password=True))
    except Exception:
        pass
    # Scrub conventional URL userinfo even if a downstream error reconstructed it.
    detail = re.sub(r"(postgres(?:ql)?(?:\+[^:]+)?://[^:/@\s]+:)[^@\s]+@", r"\1***@", detail)
    detail = re.sub(
        r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*[^,;\s]+",
        lambda match: f"{match.group(1)}=<redacted>",
        detail,
    )
    return detail[:_MAX_DETAIL]


def _base_evidence(args: argparse.Namespace) -> dict[str, Any]:
    native_placement = bool(getattr(args, "apply_native_placement", False))
    return {
        "schema": "dish-pg-routine-migration-evidence-v1",
        "recorded_at": _now(),
        "environment": args.environment,
        "database_identity": {
            "expected": args.expected_database_name,
            "observed": None,
        },
        "source_commit": None,
        "before_revisions": None,
        "expected_revision": ALEMBIC_HEAD,
        "final_revisions": None,
        "mode": (
            "apply_native_placement"
            if native_placement
            else ("apply" if args.apply else "check")
        ),
        "phases": [],
        "mutation_attempted": False,
        "mutation_occurred": False,
        "result": "preflight_started",
        "error": None,
        "next_action": None,
    }


def _finalize_native_placement(database_url: str, source_commit: str) -> None:
    engine = create_engine(database_url, future=True)
    try:
        with Session(engine) as session, session.begin():
            finalize_native_catalog_runtime_authority(
                session,
                source_commit_sha=source_commit,
            )
    finally:
        engine.dispose()


def _read_native_placement_state(database_url: str) -> dict[str, int]:
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            root_count = int(
                connection.scalar(
                    text(
                        "SELECT count(*) FROM current_native_catalog_runtimes r "
                        "JOIN authority_generations g USING (generation_id) "
                        "WHERE g.status='active'"
                    )
                )
            )
            null_count = int(
                connection.scalar(
                    text("SELECT count(*) FROM dish_states WHERE section_id IS NULL")
                )
            )
        return {"runtime_root_count": root_count, "null_section_count": null_count}
    finally:
        engine.dispose()


def _require_database_revision(
    database_url: str,
    *,
    expected_database_name: str,
    expected_revision: str,
) -> None:
    observed, revisions = _read_database_state(database_url)
    if observed != expected_database_name or revisions != (expected_revision,):
        raise RoutineMigrationError(
            "phase_revision_mismatch",
            f"migration phase did not verify exact revision {expected_revision}",
            next_action=(
                "Do not restart/promote. Preserve the evidence and diagnose the "
                "recorded phase before retrying."
            ),
        )


def _plain_apply_eligible(script: ScriptDirectory, current_revision: str) -> bool:
    # Head-first descending order, e.g. [ALEMBIC_HEAD, ..., staging, ..., base].
    ordered = [revision.revision for revision in script.iterate_revisions(ALEMBIC_HEAD, "base")]
    if current_revision not in ordered or _NATIVE_PLACEMENT_STAGING_REVISION not in ordered:
        return False
    return ordered.index(current_revision) <= ordered.index(_NATIVE_PLACEMENT_STAGING_REVISION)


def _apply_final_revision(
    cfg: Config, args: argparse.Namespace, phases: list[dict[str, Any]]
) -> None:
    command.upgrade(cfg, ALEMBIC_HEAD)
    _require_database_revision(
        args.database_url,
        expected_database_name=args.expected_database_name,
        expected_revision=ALEMBIC_HEAD,
    )
    phases.append({"phase": "final_revision", "result": "verified", "revision": ALEMBIC_HEAD})


def _run_native_placement_sequence(
    args: argparse.Namespace,
    evidence: dict[str, Any],
    journal: EvidenceJournal,
    source_commit: str,
) -> None:
    cfg = _alembic_config(args.database_url)
    phases = evidence["phases"]

    command.upgrade(cfg, _NATIVE_PLACEMENT_STAGING_REVISION)
    _require_database_revision(
        args.database_url,
        expected_database_name=args.expected_database_name,
        expected_revision=_NATIVE_PLACEMENT_STAGING_REVISION,
    )
    phases.append(
        {
            "phase": "staging_revision",
            "result": "verified",
            "revision": _NATIVE_PLACEMENT_STAGING_REVISION,
        }
    )
    evidence["recorded_at"] = _now()
    journal.write(evidence)

    _finalize_native_placement(args.database_url, source_commit)
    placement = _read_native_placement_state(args.database_url)
    if placement != {"runtime_root_count": 1, "null_section_count": 0}:
        raise RoutineMigrationError(
            "native_placement_readback_failed",
            "native placement finalizer did not establish exactly one runtime root "
            "with zero NULL placements",
            next_action=(
                "Do not apply 0052 or restart/promote. Preserve the evidence and "
                "diagnose native placement state."
            ),
        )
    phases.append(
        {"phase": "native_runtime_finalizer", "result": "verified", **placement}
    )
    evidence["recorded_at"] = _now()
    journal.write(evidence)

    _apply_final_revision(cfg, args, phases)


def run(args: argparse.Namespace, journal: EvidenceJournal) -> tuple[dict[str, Any], int]:
    evidence = _base_evidence(args)
    journal.write(evidence)
    try:
        source_commit = _resolve_source_commit(args.source_commit)
        evidence["source_commit"] = source_commit
        script = _repository_script()
        native_placement = bool(getattr(args, "apply_native_placement", False))
        apply_requested = bool(args.apply or native_placement)
        _validate_target(
            environment=args.environment,
            database_url=args.database_url,
            expected_database_name=args.expected_database_name,
            apply=apply_requested,
            confirmation=args.confirm_database_name,
        )
        try:
            observed_database, before = _read_database_state(args.database_url)
        except SQLAlchemyError as exc:
            raise RoutineMigrationError(
                "database_unavailable",
                "PostgreSQL target is temporarily unavailable during migration preflight",
                retryable=True,
                next_action="Do not restart/promote; restore database availability and rerun preflight.",
            ) from exc
        evidence["database_identity"]["observed"] = observed_database
        evidence["before_revisions"] = list(before)
        evidence["final_revisions"] = list(before)
        if observed_database != args.expected_database_name:
            raise RoutineMigrationError(
                "connected_database_identity_mismatch",
                "connected PostgreSQL database does not match the expected database identity",
            )
        state = _validate_current_revision(script, before)
        if state == "current":
            evidence["result"] = "already_current"
            evidence["next_action"] = "Schema gate passed; service restart/promotion may proceed only under its separate authority."
            evidence["recorded_at"] = _now()
            journal.write(evidence)
            return evidence, 0
        if args.check:
            evidence["result"] = "pending"
            evidence["next_action"] = "Run the reviewed/authorized --apply-native-placement command for this exact release, then rerun --check before any service restart."
            evidence["recorded_at"] = _now()
            journal.write(evidence)
            return evidence, 0

        plain_apply = False
        if args.apply and not native_placement:
            if _plain_apply_eligible(script, before[0]):
                plain_apply = True
            else:
                raise RoutineMigrationError(
                    "native_placement_sequence_required",
                    "this release requires the governed native-placement sequence before "
                    "the final schema revision",
                    next_action=(
                        "Run the reviewed/authorized --apply-native-placement command for "
                        "this exact release, then rerun --check before any service restart."
                    ),
                )

        evidence["mutation_attempted"] = True
        evidence["mutation_occurred"] = None
        evidence["result"] = "apply_in_progress"
        evidence["recorded_at"] = _now()
        journal.write(evidence)
        try:
            if plain_apply:
                _apply_final_revision(_alembic_config(args.database_url), args, evidence["phases"])
            else:
                _run_native_placement_sequence(args, evidence, journal, source_commit)
        except BaseException as exc:
            final: tuple[str, ...] | None = None
            try:
                observed_after, final = _read_database_state(args.database_url)
                evidence["database_identity"]["observed"] = observed_after
                evidence["final_revisions"] = list(final)
                evidence["mutation_occurred"] = True if final != before else None
            except BaseException:
                evidence["final_revisions"] = None
                evidence["mutation_occurred"] = None
            if isinstance(exc, RoutineMigrationError):
                raise
            raise RoutineMigrationError(
                "migration_execution_failed",
                f"Alembic upgrade failed ({type(exc).__name__})",
                next_action="Do not restart/promote or downgrade automatically. Preserve this evidence and diagnose the exact current head before recovery.",
            ) from exc

        try:
            observed_after, final = _read_database_state(args.database_url)
        except SQLAlchemyError as exc:
            evidence["final_revisions"] = None
            evidence["mutation_occurred"] = None
            raise RoutineMigrationError(
                "post_apply_verification_unavailable",
                "migration returned but exact post-apply schema verification could not read the database",
                retryable=True,
                next_action="Do not restart/promote; recover connectivity and verify the exact schema head manually with --check.",
            ) from exc
        evidence["database_identity"]["observed"] = observed_after
        evidence["final_revisions"] = list(final)
        if observed_after != args.expected_database_name or final != (ALEMBIC_HEAD,):
            evidence["mutation_occurred"] = True if final != before else None
            raise RoutineMigrationError(
                "post_apply_revision_mismatch",
                "post-apply verification did not observe the exact expected database identity and Alembic head",
                next_action="Do not restart/promote or downgrade automatically. Preserve before/final evidence for diagnosis.",
            )
        evidence["mutation_occurred"] = True
        evidence["result"] = "applied"
        evidence["next_action"] = "Exact schema head verified; service restart/promotion may proceed only under its separate authority."
        evidence["recorded_at"] = _now()
        journal.write(evidence)
        return evidence, 0
    except RoutineMigrationError as exc:
        evidence["result"] = "failed"
        evidence["error"] = {
            "rule": exc.rule,
            "type": type(exc).__name__,
            "message": _redacted_detail(exc, args.database_url),
            "retryable": exc.retryable,
        }
        evidence["next_action"] = exc.next_action
        evidence["recorded_at"] = _now()
        journal.write(evidence)
        return evidence, 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        journal = EvidenceJournal.create(args.evidence_file)
    except OSError as exc:
        sys.stderr.write(
            f"dish-pg-migrate: cannot create exclusive evidence file ({type(exc).__name__})\n"
        )
        return 1
    try:
        evidence, status = run(args, journal)
    finally:
        journal.close()
    stream = sys.stdout if status == 0 else sys.stderr
    stream.write(json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
