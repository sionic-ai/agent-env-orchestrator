"""Discover environment specs in a directory (one ``<name>/environment.json`` per env)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aeo.errors import ValidationError
from aeo.spec import SPEC_FILENAME, EnvironmentSpec, load_spec

MAX_CATALOG_ENTRIES = 256


@dataclass(frozen=True)
class CatalogEntry:
    path: Path
    spec: EnvironmentSpec | None
    error: str | None

    def as_dict(self) -> dict[str, object]:
        if self.spec is None:
            return {"path": str(self.path), "valid": False, "error": self.error}
        return {
            "path": str(self.path),
            "valid": True,
            "id": self.spec.id,
            "description": self.spec.description,
            "agent_image": self.spec.agent.image,
            "verifier_image": self.spec.verifier.image,
            "agent_timeout_seconds": self.spec.agent.timeout_seconds,
        }


def scan_catalog(root: Path | str) -> list[CatalogEntry]:
    root = Path(root)
    if not root.is_dir():
        raise ValidationError(f"catalog root is not a directory: {root}")
    entries: list[CatalogEntry] = []
    for child in sorted(root.iterdir()):
        if child.is_symlink() or not child.is_dir() or not (child / SPEC_FILENAME).exists():
            continue
        if len(entries) >= MAX_CATALOG_ENTRIES:
            raise ValidationError(f"more than {MAX_CATALOG_ENTRIES} environments under {root}")
        try:
            entries.append(CatalogEntry(child, load_spec(child), None))
        except ValidationError as exc:
            entries.append(CatalogEntry(child, None, str(exc)))
    return entries
