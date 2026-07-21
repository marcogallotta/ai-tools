"""Asana SDK construction and transport failure mapping."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from .constants import ASANA_REQUEST_TIMEOUT
from .errors import BackendFailure, DishRuleError
from .models import RequestPhase, RequestPhaseTracker


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


class AsanaBackend:
    """Small SDK construction/call layer shared by both command surfaces."""

    def __init__(self, api_client: Any | None = None) -> None:
        self._client = api_client

    def client(self) -> Any:
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

    def call(
        self,
        function: Any,
        *args: Any,
        context: str | None = None,
        phase_tracker: RequestPhaseTracker | None = None,
        **kwargs: Any,
    ) -> Any:
        tracker = phase_tracker or RequestPhaseTracker()
        try:
            tracker.mark_send_started()
            response = function(*args, _request_timeout=ASANA_REQUEST_TIMEOUT, **kwargs)
            tracker.mark_response_received()
            if not isinstance(response, Mapping) or "data" not in response:
                raise ValueError("Asana response missing data envelope")
            return response["data"]
        except BackendFailure:
            raise
        except Exception as exc:
            raise map_backend_exception(
                exc, phase=tracker.phase, context=context
            ) from exc
