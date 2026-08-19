from __future__ import annotations

import hashlib
import uuid

from dish_pg import models
from dish_pg.database import session_scope
from dish_tool.content_versions import CONTENT_IDENTITY_SCHEME, content_identity
from tests.support.postgresql.command import _call, _port
from tests.support.postgresql.workflow import _next, _register_run, workflow_db


def _create_version(session, ids, context, *, title: str, body: str) -> models.ContentVersion:
    run_id = _next(ids)
    _register_run(
        session,
        generation_id=context["generation_id"],
        run_id=run_id,
    )
    result = _port(session, ids).execute(
        _call(
            "create",
            run_id=run_id,
            request_id=_next(ids),
            arguments={"title": title, "body": body},
        )
    )
    assert result.ok, (result.code, result.http_status, result.data)
    version = session.get(
        models.ContentVersion, uuid.UUID(result.data["content_version_id"])
    )
    assert version is not None
    return version


def test_warm_potato_salad_uses_canonical_source_content_identity(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    title = "Warm potato salad with yarrow"
    body = "Purpose: preserve the observed dark-launch content shape.\nServe warm.\n"

    with session_scope(factory) as session:
        version = _create_version(session, ids, context, title=title, body=body)

        assert version.title == title
        assert version.body == body
        assert version.identity_scheme == CONTENT_IDENTITY_SCHEME
        assert version.content_identity == content_identity(title, body)
        assert version.content_identity != hashlib.sha256(
            f"{title}\0{body}".encode("utf-8")
        ).hexdigest()


def test_postgresql_content_identity_uses_canonical_newline_normalization(
    workflow_db,
) -> None:
    factory, ids, context, _task_id = workflow_db
    title = "Warm potato salad with yarrow"
    body_lf = "First line\nSecond line\n"
    body_crlf = body_lf.replace("\n", "\r\n")

    with session_scope(factory) as session:
        lf_version = _create_version(
            session, ids, context, title=title, body=body_lf
        )
        crlf_version = _create_version(
            session, ids, context, title=title, body=body_crlf
        )

        assert (
            lf_version.identity_scheme
            == crlf_version.identity_scheme
            == CONTENT_IDENTITY_SCHEME
        )
        assert lf_version.content_identity == crlf_version.content_identity
        assert lf_version.content_identity == content_identity(title, body_lf)
        assert crlf_version.content_identity == content_identity(title, body_crlf)


def test_canonical_content_identity_preserves_field_order_and_real_differences() -> None:
    title = "Warm potato salad with yarrow"
    body = "Canonical body\n"

    baseline = content_identity(title, body)

    assert content_identity(body, title) != baseline
    assert content_identity(title, body + "Changed.\n") != baseline
    assert content_identity(title, "") != baseline
