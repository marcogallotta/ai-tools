"""Flaky-test classification and quarantine policy shared by pytest and tooling."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Mapping


CANDIDATE_MARKER = "flake_candidate"
QUARANTINE_MARKER = "quarantined"
MAX_QUARANTINE_DAYS = 7

_CANDIDATE_REQUIRED = {
    "issue",
    "owner",
    "first_seen",
    "signature",
}
_QUARANTINE_REQUIRED = _CANDIDATE_REQUIRED | {
    "quarantined_on",
    "expires",
}


def _parse_iso_date(value: object, *, field: str) -> tuple[date | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, f"{field} must be a non-blank ISO date"
    try:
        return date.fromisoformat(value), None
    except ValueError:
        return None, f"{field} must use YYYY-MM-DD"


def validate_marker_metadata(
    marker_name: str,
    metadata: Mapping[str, object],
    *,
    today: date | None = None,
    launch_critical: bool = False,
) -> list[str]:
    """Return policy violations for one flaky-test marker."""
    today = date.today() if today is None else today
    if marker_name == CANDIDATE_MARKER:
        required = _CANDIDATE_REQUIRED
    elif marker_name == QUARANTINE_MARKER:
        required = _QUARANTINE_REQUIRED
    else:
        return [f"unknown flaky-test marker: {marker_name}"]

    errors: list[str] = []
    missing = sorted(
        field
        for field in required
        if field not in metadata
        or not isinstance(metadata[field], str)
        or not str(metadata[field]).strip()
    )
    if missing:
        errors.append("missing non-blank metadata: " + ", ".join(missing))

    first_seen, error = _parse_iso_date(metadata.get("first_seen"), field="first_seen")
    if error:
        errors.append(error)
    elif first_seen and first_seen > today:
        errors.append("first_seen cannot be in the future")

    if marker_name == QUARANTINE_MARKER:
        quarantined_on, error = _parse_iso_date(
            metadata.get("quarantined_on"), field="quarantined_on"
        )
        if error:
            errors.append(error)
        expires, error = _parse_iso_date(metadata.get("expires"), field="expires")
        if error:
            errors.append(error)

        if first_seen and quarantined_on and quarantined_on < first_seen:
            errors.append("quarantined_on cannot precede first_seen")
        if quarantined_on and quarantined_on > today:
            errors.append("quarantined_on cannot be in the future")
        if expires and expires <= today:
            errors.append("quarantine has expired")
        if quarantined_on and expires:
            maximum = quarantined_on + timedelta(days=MAX_QUARANTINE_DAYS)
            if expires > maximum:
                errors.append(
                    f"quarantine may last at most {MAX_QUARANTINE_DAYS} days"
                )
        if launch_critical and not str(metadata.get("waiver", "")).strip():
            errors.append(
                "launch-critical smoke/invariant tests require an explicit waiver"
            )

    return errors
