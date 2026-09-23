from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

DirectiveValues = Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class UnitFile:
    sections: Mapping[str, DirectiveValues]

    def values(self, section: str, directive: str) -> tuple[str, ...]:
        return self.sections.get(section, {}).get(directive, ())


def parse_unit(text: str) -> UnitFile:
    """Parse the directive subset needed by shipped systemd owner contracts."""
    sections: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    current_section: str | None = None
    logical_lines: list[str] = []
    pending = ""

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if pending:
            stripped = pending + stripped
            pending = ""
        if stripped.endswith("\\"):
            pending = stripped[:-1]
            continue
        logical_lines.append(stripped)
    if pending:
        raise ValueError("unterminated systemd line continuation")

    for line in logical_lines:
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1]
            if not current_section:
                raise ValueError("empty systemd section")
            continue
        if current_section is None:
            raise ValueError(f"directive outside a section: {line}")
        key, separator, value = line.partition("=")
        if not separator or not key:
            raise ValueError(f"invalid systemd directive: {line}")
        sections[current_section][key].append(value)

    return UnitFile(
        MappingProxyType(
            {
                section: MappingProxyType(
                    {key: tuple(values) for key, values in directives.items()}
                )
                for section, directives in sections.items()
            }
        )
    )


def load_unit(path: Path) -> UnitFile:
    return parse_unit(path.read_text(encoding="utf-8"))


def assert_directives(
    unit: UnitFile,
    expected: Mapping[str, Mapping[str, tuple[str, ...]]],
) -> None:
    for section, directives in expected.items():
        for directive, values in directives.items():
            actual = unit.values(section, directive)
            assert actual == values, (
                f"[{section}] {directive}: expected {values!r}, got {actual!r}"
            )


USER_MANAGER_FORBIDDEN_DIRECTIVES = frozenset(
    {
        "AmbientCapabilities",
        "CapabilityBoundingSet",
        "Group",
        "PrivateDevices",
        "ProtectKernelModules",
        "User",
    }
)


def assert_user_manager_compatible(unit: UnitFile) -> None:
    service = unit.sections.get("Service", {})
    present = USER_MANAGER_FORBIDDEN_DIRECTIVES.intersection(service)
    assert not present, f"system-manager-only directives present: {sorted(present)}"
    for section in unit.sections.values():
        for values in section.values():
            for value in values:
                assert "multi-user.target" not in value
                assert "docker.service" not in value
                assert "tailscaled.service" not in value
