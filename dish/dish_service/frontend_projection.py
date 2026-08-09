"""Candidate abnormal-only projection presentation for local Stage 4 detail."""
from __future__ import annotations

from datetime import datetime, timezone

from dish_pg.frontend_projection_query import ProjectionFact


def abnormal_projection(fact: ProjectionFact) -> dict[str, str] | None:
    candidates: tuple[tuple[str, str, datetime | None], ...] = (
        ("drifted", "The downstream Asana projection has open drift evidence.", fact.drifted_at),
        ("failed", "The downstream Asana projection has blocked live work.", fact.blocked_at),
        ("unknown", "The downstream Asana projection has uncertain live work.", fact.uncertain_at),
        ("delayed", "The downstream Asana projection has live work older than the local observation threshold.", fact.delayed_at),
    )
    for state, message, observed in candidates:
        if observed is not None:
            return {
                "state": state,
                "message": message,
                "observation_time": observed.astimezone(timezone.utc).isoformat(timespec="seconds"),
            }
    return None
