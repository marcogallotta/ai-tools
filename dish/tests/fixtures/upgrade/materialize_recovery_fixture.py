"""Materialize byte-exact historical recovery fixtures from tracked gzip assets."""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path


V17_NAME = "dish-tool-recovery-v17-legacy.sqlite"
V17_SHA256 = "74e9bb48daa5cc4524e40ae26d51e0d6e17d8301750350f1387e985cffb7edd2"


def materialize(source_dir: Path, fixture: str, destination: Path) -> Path:
    if fixture != V17_NAME:
        destination.write_bytes((source_dir / fixture).read_bytes())
        return destination

    payload = gzip.decompress((source_dir / f"{fixture}.gz").read_bytes())
    digest = hashlib.sha256(payload).hexdigest()
    if digest != V17_SHA256:
        raise ValueError(f"historical recovery fixture digest mismatch: {digest}")
    destination.write_bytes(payload)
    return destination
