"""Trusted chronology comparisons shared by Stage A cutover transitions."""
from __future__ import annotations

from datetime import datetime, timezone

from .release_evidence import ReleaseAuthorityError


def _utc_comparable(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _require_at_or_after(
    value: datetime,
    floor: datetime,
    *,
    field: str,
    floor_field: str,
) -> None:
    if _utc_comparable(value) < _utc_comparable(floor):
        raise ReleaseAuthorityError(f"{field} must be at or after {floor_field}")
