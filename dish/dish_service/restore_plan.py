"""Typed mutable state for one checkpointed database restore."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, MutableMapping
from typing import Any

from dish_tool.errors import DishRuleError


class RestorePlan(MutableMapping[str, Any]):
    """Explicit restore state whose serialized form remains journal-compatible."""

    _FIELDS = {
        "backup_id",
        "source",
        "source_schema_version",
        "restored_schema_version",
        "candidate",
        "live_at_start",
        "pre_restore_target",
        "pre_restore_backup",
        "pre_restore_unavailable",
        "live_before",
        "restore_error_type",
        "rollback_candidate",
        "rolled_back",
        "installed",
        "restored",
        "result",
    }

    def __init__(self, *, backup_id: str, **values: Any) -> None:
        self.backup_id = str(backup_id)
        self.source: dict[str, Any] | None = None
        self.source_schema_version: int | None = None
        self.restored_schema_version: int | None = None
        self.candidate: dict[str, Any] | None = None
        self.live_at_start: dict[str, Any] | None = None
        self.pre_restore_target: dict[str, Any] | None = None
        self.pre_restore_backup: dict[str, Any] | None = None
        self.pre_restore_unavailable: dict[str, Any] | None = None
        self.live_before: dict[str, Any] | None = None
        self.restore_error_type: str | None = None
        self.rollback_candidate: dict[str, Any] | None = None
        self.rolled_back: dict[str, Any] | None = None
        self.installed: dict[str, Any] | None = None
        self.restored: dict[str, Any] | None = None
        self.result: dict[str, Any] | None = None
        self._present = {"backup_id"}
        for key, value in values.items():
            self[key] = value

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RestorePlan":
        unknown = set(value) - cls._FIELDS
        backup_id = value.get("backup_id")
        if unknown or not isinstance(backup_id, str) or not backup_id:
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "restore recovery checkpoint has an invalid plan shape",
                rule="backup_restore_recovery_checkpoint_invalid",
                retryable=False,
                details={
                    "database_retained": False,
                    "unknown_fields": sorted(unknown),
                },
            )
        return cls(
            backup_id=backup_id,
            **{key: value[key] for key in value if key != "backup_id"},
        )

    def as_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps({key: self[key] for key in self}))

    def __getitem__(self, key: str) -> Any:
        if key not in self._FIELDS or key not in self._present:
            raise KeyError(key)
        return getattr(self, key)

    def __setitem__(self, key: str, value: Any) -> None:
        if key not in self._FIELDS:
            raise KeyError(f"unknown restore-plan field: {key}")
        if key == "backup_id":
            value = str(value)
            if not value:
                raise ValueError("restore backup_id must not be empty")
        setattr(self, key, value)
        self._present.add(key)

    def __delitem__(self, key: str) -> None:
        if key == "backup_id":
            raise KeyError("restore backup_id is required")
        if key not in self._present:
            raise KeyError(key)
        self._present.remove(key)
        setattr(self, key, None)

    def __iter__(self) -> Iterator[str]:
        for key in self._FIELDS:
            if key in self._present:
                yield key

    def __len__(self) -> int:
        return len(self._present)
