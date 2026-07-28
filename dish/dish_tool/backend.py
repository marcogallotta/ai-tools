"""Asana SDK construction and transport failure mapping."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Mapping

from .constants import ASANA_REQUEST_TIMEOUT, COOKING_PROJECT_GID
from .errors import BackendFailure, DishRuleError
from .models import RequestPhase, RequestPhaseTracker

LOG = logging.getLogger("dish.backend")


def load_asana_pat() -> str:
    pat = os.environ.get("ASANA_PAT")
    if pat:
        return pat
    env_path = Path(
        os.environ.get("ASANA_ENV", "~/.config/asana-cli/.env")
    ).expanduser()
    try:
        for raw_line in env_path.read_text().splitlines():
            line = raw_line.strip()
            if line.startswith("ASANA_PAT="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value
    except FileNotFoundError:
        pass
    raise DishRuleError(
        "INTERNAL_ERROR",
        f"ASANA_PAT not found (set ASANA_PAT or add it to {env_path})",
        rule="asana_auth_missing",
    )


def asana_error_detail(error: Exception, context: str | None = None) -> str:
    status = getattr(error, "status", None)
    body = getattr(error, "body", None)
    reason = getattr(error, "reason", None)
    if isinstance(body, (bytes, bytearray)):
        body = body.decode(errors="replace")
    detail = str(body or reason or error)[:800]
    where = f" [{context}]" if context else ""
    if status == 401:
        return f"Asana auth error (401){where}: {detail}"
    if status == 404:
        return f"Asana resource not found (404){where}: {detail}"
    if status == 429:
        return f"Asana rate limit (429){where}: {detail}"
    if status is not None and status >= 500:
        return f"Asana server error ({status}){where}: {detail}"
    return f"Asana API error ({status}){where}: {detail}"


def map_backend_exception(
    error: Exception,
    *,
    phase: RequestPhase,
    context: str | None = None,
) -> BackendFailure:
    status = getattr(error, "status", None)
    LOG.warning(
        "asana_request_failed phase=%s status=%s context=%s error_type=%s detail=%s",
        phase.value,
        status,
        context,
        type(error).__name__,
        asana_error_detail(error, context),
    )
    if status is not None:
        message = asana_error_detail(error, context)
        if status == 408 or status >= 500:
            return BackendFailure(
                "BACKEND_UNCERTAIN",
                message,
                status=status,
                phase=phase.value,
                retryable=False,
            )
        if status == 403:
            return BackendFailure(
                "BACKEND_REJECTED",
                message,
                rule="backend_access_denied",
                status=status,
                phase=phase.value,
                retryable=False,
            )
        return BackendFailure(
            "BACKEND_REJECTED",
            message,
            status=status,
            phase=phase.value,
            retryable=True,
        )
    if phase == RequestPhase.PRE_SEND:
        return BackendFailure(
            "BACKEND_REJECTED",
            f"backend request failed before transmission: {error}",
            phase=phase.value,
            retryable=True,
        )
    return BackendFailure(
        "BACKEND_UNCERTAIN",
        f"backend request may have been transmitted: {error}",
        phase=phase.value,
        retryable=False,
    )


def close_asana_sdk_client(api_client: Any) -> None:
    """Deterministically release a python-asana client's worker pool.

    ``multiprocessing.pool.ThreadPool.close`` and ``join`` stop the workers but
    leave the pool's registered finalizer active.  On interpreter shutdown that
    retained finalizer can stall an otherwise completed process.  Cancel the
    now-redundant finalizer after the graceful join so shutdown has no stale pool
    callback to execute.
    """

    pool = getattr(api_client, "pool", None)
    if pool is None:
        return
    pool.close()
    pool.join()
    finalizer = getattr(pool, "_terminate", None)
    if finalizer is not None and finalizer.still_active():
        finalizer.cancel()
    # The generated ApiClient destructor blindly closes and joins ``pool``
    # again during interpreter teardown.  Remove the already-closed pool so
    # that late destructor execution is a no-op rather than a shutdown hazard.
    delattr(api_client, "pool")


class AsanaBackend:
    """Small SDK construction/call layer shared by both command surfaces."""

    def __init__(self, api_client: Any | None = None) -> None:
        self._client = api_client
        self._owns_client = api_client is None
        self._closed = False

    def client(self) -> Any:
        if self._closed:
            raise DishRuleError(
                "INTERNAL_ERROR",
                "Asana backend is closed",
                rule="asana_backend_closed",
            )
        if self._client is None:
            try:
                import asana
                from urllib3.util import Retry
            except ImportError as exc:
                raise DishRuleError(
                    "INTERNAL_ERROR",
                    "python-asana is not installed",
                    rule="asana_sdk_missing",
                ) from exc
            config = asana.Configuration()
            config.access_token = load_asana_pat()
            config.return_page_iterator = False
            config.retry_strategy = Retry(total=0, connect=0, read=0, redirect=0)
            self._client = asana.ApiClient(config)
        return self._client

    def close(self) -> None:
        """Release only an SDK client created by this backend.

        python-asana 5.2.5 exposes no public close method or context manager,
        but every ``ApiClient`` creates a ``multiprocessing.pool.ThreadPool``.
        The generated destructor closes that pool nondeterministically.  Owners
        must instead close and join it explicitly.  An injected client remains
        caller-owned and is never touched here.
        """

        if self._closed:
            return
        self._closed = True
        client = self._client
        if not self._owns_client or client is None:
            return
        close_asana_sdk_client(client)
        self._client = None

    def __enter__(self) -> "AsanaBackend":
        if self._closed:
            raise DishRuleError(
                "INTERNAL_ERROR",
                "Asana backend is closed",
                rule="asana_backend_closed",
            )
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def call_envelope(
        self,
        function: Any,
        *args: Any,
        context: str | None = None,
        phase_tracker: RequestPhaseTracker | None = None,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        tracker = phase_tracker or RequestPhaseTracker()
        try:
            tracker.mark_send_started()
            response = function(*args, _request_timeout=ASANA_REQUEST_TIMEOUT, **kwargs)
            tracker.mark_response_received()
            if not isinstance(response, Mapping) or "data" not in response:
                raise ValueError("Asana response missing data envelope")
            return response
        except BackendFailure:
            raise
        except (Exception, asyncio.CancelledError) as exc:
            raise map_backend_exception(
                exc, phase=tracker.phase, context=context
            ) from exc

    def call(
        self,
        function: Any,
        *args: Any,
        context: str | None = None,
        phase_tracker: RequestPhaseTracker | None = None,
        **kwargs: Any,
    ) -> Any:
        return self.call_envelope(
            function,
            *args,
            context=context,
            phase_tracker=phase_tracker,
            **kwargs,
        )["data"]

    def list_sections(self, project_gid: str) -> list[dict[str, Any]]:
        import asana

        function = asana.SectionsApi(self.client()).get_sections_for_project
        options: dict[str, Any] = {"opt_fields": "gid,name", "limit": 100}
        sections: list[dict[str, Any]] = []
        seen_offsets: set[str] = set()
        while True:
            envelope = self.call_envelope(
                function,
                project_gid,
                options,
                context=f"Cooking project {project_gid} sections",
            )
            data = envelope["data"]
            if not isinstance(data, list) or not all(isinstance(item, Mapping) for item in data):
                raise DishRuleError(
                    "INTERNAL_ERROR",
                    "Asana returned malformed section data",
                    rule="backend_response_malformed",
                )
            sections.extend(dict(item) for item in data)
            next_page = envelope.get("next_page")
            if next_page is None:
                break
            if not isinstance(next_page, Mapping):
                raise DishRuleError(
                    "INTERNAL_ERROR",
                    "Asana returned malformed section pagination data",
                    rule="backend_response_malformed",
                )
            offset = str(next_page.get("offset") or "").strip()
            if not offset:
                break
            if offset in seen_offsets:
                raise DishRuleError(
                    "INTERNAL_ERROR",
                    "Asana repeated a section pagination offset",
                    rule="backend_pagination_loop",
                )
            seen_offsets.add(offset)
            options = {"opt_fields": "gid,name", "limit": 100, "offset": offset}
        return sections

    def read_task(self, task_gid: str) -> dict[str, Any]:
        import asana

        opt_fields = ",".join(
            (
                "gid",
                "name",
                "notes",
                "html_notes",
                "completed",
                "modified_at",
                "permalink_url",
                "projects.gid",
                "projects.name",
                "memberships.project.gid",
                "memberships.project.name",
                "memberships.section.gid",
                "memberships.section.name",
            )
        )
        try:
            data = self.call(
                asana.TasksApi(self.client()).get_task,
                task_gid,
                {"opt_fields": opt_fields},
                context=f"task {task_gid}",
            )
        except BackendFailure as exc:
            if exc.status == 404:
                raise DishRuleError(
                    "NOT_FOUND",
                    f"task not found: {task_gid}",
                    rule="task_not_found",
                ) from exc
            raise
        if not isinstance(data, Mapping):
            raise DishRuleError(
                "INTERNAL_ERROR",
                "Asana returned malformed task data",
                rule="backend_response_malformed",
            )
        return dict(data)

    @staticmethod
    def _section_for_project(task: Mapping[str, Any], project_gid: str | None = None) -> str | None:
        for membership in task.get("memberships") or []:
            project = membership.get("project") or {}
            section = membership.get("section") or {}
            if project_gid is None or str(project.get("gid") or "") == str(project_gid):
                gid = str(section.get("gid") or "").strip()
                if gid:
                    return gid
        return None

    def create_bare_task(
        self, *, title: str, project_gid: str, section_gid: str
    ) -> dict[str, Any]:
        """Create without notes, then place the confirmed task in Research Queue.

        Any failure after task creation is ambiguous for the overall command: a
        retry could duplicate the task, so it is never reported as safely retryable.
        """

        import asana

        created = self.call(
            asana.TasksApi(self.client()).create_task,
            {"data": {"name": title, "projects": [project_gid]}},
            {"opt_fields": "gid,name,notes"},
            context=f"Cooking project {project_gid}",
        )
        if not isinstance(created, Mapping) or not str(created.get("gid") or "").strip():
            raise BackendFailure(
                "BACKEND_UNCERTAIN",
                "task creation response did not identify the created task",
                retryable=False,
            )
        task = dict(created)
        task_gid = str(task["gid"])
        try:
            self.call(
                asana.SectionsApi(self.client()).add_task_for_section,
                section_gid,
                {"body": {"data": {"task": task_gid}}},
                context=f"Research Queue {section_gid}",
            )
        except DishRuleError as exc:
            raise BackendFailure(
                "BACKEND_UNCERTAIN",
                f"task {task_gid} was created but Research Queue placement was not confirmed: {exc}",
                status=getattr(exc, "status", None),
                phase=getattr(exc, "phase", None),
                retryable=False,
                details={
                    "task_gid": task_gid,
                    "partial_application": "task_created",
                },
            ) from exc
        confirmed = self.read_task(task_gid)
        if self._section_for_project(confirmed, project_gid) != section_gid:
            raise BackendFailure(
                "BACKEND_UNCERTAIN",
                "task creation succeeded but Research Queue placement was not confirmed",
                retryable=False,
                details={"task_gid": task_gid, "expected_section_gid": section_gid},
            )
        if str(confirmed.get("name") or "") != title or str(confirmed.get("notes") or "") not in {"", str(task.get("notes") or "")}:
            raise BackendFailure(
                "BACKEND_UNCERTAIN",
                "task changed unexpectedly during creation placement",
                retryable=False,
                details={"task_gid": task_gid},
            )
        return confirmed


    def update_task_content(
        self, *, task_gid: str, title: str, notes: str
    ) -> None:
        """Replace the canonical title and complete notes in one mutation."""

        try:
            import asana

            tasks_api = asana.TasksApi(self.client())
        except BackendFailure:
            raise
        except (Exception, asyncio.CancelledError) as exc:
            raise map_backend_exception(
                exc,
                phase=RequestPhase.PRE_SEND,
                context=f"task {task_gid} content",
            ) from exc

        data = self.call(
            tasks_api.update_task,
            {"data": {"name": title, "notes": notes}},
            task_gid,
            {"opt_fields": "gid"},
            context=f"task {task_gid} content",
        )
        response_gid = (
            str(data.get("gid") or "").strip()
            if isinstance(data, Mapping)
            else ""
        )
        if response_gid != task_gid:
            raise BackendFailure(
                "BACKEND_UNCERTAIN",
                "Asana returned malformed data after the title-and-notes write",
                phase=RequestPhase.RESPONSE_RECEIVED.value,
                retryable=False,
                details={"expected_task_gid": task_gid, "actual_task_gid": response_gid},
            )

    def update_task_completed(self, *, task_gid: str, completed: bool) -> None:
        """Set only the Asana completion flag and verify the response identity."""
        try:
            import asana

            tasks_api = asana.TasksApi(self.client())
        except BackendFailure:
            raise
        except (Exception, asyncio.CancelledError) as exc:
            raise map_backend_exception(
                exc,
                phase=RequestPhase.PRE_SEND,
                context=f"task {task_gid} completion",
            ) from exc

        data = self.call(
            tasks_api.update_task,
            {"data": {"completed": bool(completed)}},
            task_gid,
            {"opt_fields": "gid"},
            context=f"task {task_gid} completion",
        )
        response_gid = (
            str(data.get("gid") or "").strip()
            if isinstance(data, Mapping)
            else ""
        )
        if response_gid != task_gid:
            raise BackendFailure(
                "BACKEND_UNCERTAIN",
                "Asana returned malformed data after the completion-state write",
                phase=RequestPhase.RESPONSE_RECEIVED.value,
                retryable=False,
                details={"expected_task_gid": task_gid, "actual_task_gid": response_gid},
            )

    def move_task_to_section(self, *, task_gid: str, section_gid: str) -> None:
        """Place a task in a section after the caller resolves live state."""

        import asana

        before = self.read_task(task_gid)
        self.call(
            asana.SectionsApi(self.client()).add_task_for_section,
            section_gid,
            {"body": {"data": {"task": task_gid}}},
            context=f"section {section_gid}",
        )
        after = self.read_task(task_gid)
        if self._section_for_project(after, COOKING_PROJECT_GID) != section_gid:
            raise BackendFailure(
                "BACKEND_UNCERTAIN",
                "section placement was not confirmed by exact reread",
                retryable=False,
                details={"task_gid": task_gid, "expected_section_gid": section_gid},
            )
        if str(after.get("name") or "") != str(before.get("name") or "") or str(after.get("notes") or "") != str(before.get("notes") or ""):
            raise BackendFailure(
                "BACKEND_UNCERTAIN",
                "task content changed during section placement",
                retryable=False,
                details={"task_gid": task_gid, "expected_section_gid": section_gid},
            )
