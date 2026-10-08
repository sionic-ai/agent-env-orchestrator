"""Environment spec contract (``environment.json``, schema ``aeo.environment/v1``).

A spec only chooses *what* to run (images, argv, timeouts, bounded resources, a trusted asset
directory). *How* containers are isolated (no network, dropped capabilities, read-only rootfs,
non-root user, no host mounts) is fixed by the backend and cannot be configured here: unknown
keys are rejected, so fields such as ``privileged``, ``env``, ``mounts`` or ``cap_add`` fail.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from aeo.errors import ValidationError
from aeo.safefs import ensure_no_symlink_components, read_regular_file
from aeo.validation import load_json_bytes, validate_argv, validate_env_id, validate_image_ref

SPEC_SCHEMA_VERSION = "aeo.environment/v1"
SPEC_FILENAME = "environment.json"
MAX_SPEC_BYTES = 64 * 1024
MAX_DESCRIPTION_LEN = 1000

AGENT_MAX_TIMEOUT_S = 3600
VERIFIER_MAX_TIMEOUT_S = 600
MAX_CPUS = 8.0
MIN_CPUS = 0.1
MIN_MEMORY_MB = 64
MAX_MEMORY_MB = 16384
MIN_PIDS = 16
MAX_PIDS = 4096

_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

_TOP_KEYS = {"schema_version", "id", "description", "assets", "network", "agent", "verifier"}
_ROLE_KEYS = {"image", "command", "timeout_seconds", "resources"}
_RESOURCE_KEYS = {"cpus", "memory_mb", "pids"}


@dataclass(frozen=True)
class Resources:
    cpus: float = 1.0
    memory_mb: int = 512
    pids: int = 128


@dataclass(frozen=True)
class ContainerRole:
    image: str
    command: tuple[str, ...]
    timeout_seconds: int
    resources: Resources


@dataclass(frozen=True)
class EnvironmentSpec:
    id: str
    description: str
    network: str
    agent: ContainerRole
    verifier: ContainerRole
    assets: str | None
    # Filled in by load_spec(); parse_spec() alone leaves them empty.
    spec_dir: Path | None = None
    assets_dir: Path | None = None
    sha256: str = ""


def _require_object(value: Any, where: str, allowed: set[str], required: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ValidationError(f"{where} must be a JSON object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValidationError(f"{where} has unsupported field(s): {', '.join(unknown)}")
    missing = sorted(required - set(value))
    if missing:
        raise ValidationError(f"{where} is missing field(s): {', '.join(missing)}")
    return value


def _int_in_range(value: Any, where: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValidationError(f"{where} must be an integer in [{lo}, {hi}]")
    return value


def _parse_resources(value: Any, where: str) -> Resources:
    if value is None:
        return Resources()
    obj = _require_object(value, where, _RESOURCE_KEYS, set())
    defaults = Resources()
    cpus = obj.get("cpus", defaults.cpus)
    # The range check comes first: it compares ints exactly (no float conversion, so a huge
    # integer can not raise OverflowError) and is False for NaN and infinities.
    if (
        isinstance(cpus, bool)
        or not isinstance(cpus, int | float)
        or not MIN_CPUS <= cpus <= MAX_CPUS
        or not math.isfinite(cpus)
    ):
        raise ValidationError(f"{where}.cpus must be a number in [{MIN_CPUS}, {MAX_CPUS}]")
    memory = _int_in_range(
        obj.get("memory_mb", defaults.memory_mb), f"{where}.memory_mb", MIN_MEMORY_MB, MAX_MEMORY_MB
    )
    pids = _int_in_range(obj.get("pids", defaults.pids), f"{where}.pids", MIN_PIDS, MAX_PIDS)
    return Resources(cpus=float(cpus), memory_mb=memory, pids=pids)


def _parse_role(value: Any, where: str, max_timeout: int) -> ContainerRole:
    obj = _require_object(value, where, _ROLE_KEYS, {"image", "command", "timeout_seconds"})
    return ContainerRole(
        image=validate_image_ref(obj["image"]),
        command=validate_argv(obj["command"], field=f"{where}.command"),
        timeout_seconds=_int_in_range(
            obj["timeout_seconds"], f"{where}.timeout_seconds", 1, max_timeout
        ),
        resources=_parse_resources(obj.get("resources"), f"{where}.resources"),
    )


def _parse_assets(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValidationError("assets must be a relative directory path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or "\\" in value:
        raise ValidationError("assets must be a relative POSIX path inside the environment dir")
    for part in pure.parts:
        if part in (".", "..") or not _PATH_COMPONENT_RE.fullmatch(part):
            raise ValidationError("assets path components must be plain names (no '.' or '..')")
    return str(pure)


def parse_spec(raw: Any) -> EnvironmentSpec:
    obj = _require_object(raw, "spec", _TOP_KEYS, {"schema_version", "id", "agent", "verifier"})
    if obj["schema_version"] != SPEC_SCHEMA_VERSION:
        raise ValidationError(f"schema_version must be {SPEC_SCHEMA_VERSION!r}")
    description = obj.get("description", "")
    if not isinstance(description, str) or len(description) > MAX_DESCRIPTION_LEN:
        raise ValidationError(f"description must be a string of at most {MAX_DESCRIPTION_LEN}")
    network = obj.get("network", "none")
    if network != "none":
        raise ValidationError("network must be 'none' (networked environments are not supported)")
    return EnvironmentSpec(
        id=validate_env_id(obj["id"]),
        description=description,
        network=network,
        agent=_parse_role(obj["agent"], "agent", AGENT_MAX_TIMEOUT_S),
        verifier=_parse_role(obj["verifier"], "verifier", VERIFIER_MAX_TIMEOUT_S),
        assets=_parse_assets(obj.get("assets")),
    )


def load_spec(path: Path | str) -> EnvironmentSpec:
    """Load ``environment.json`` from a file path or an environment directory."""
    path = Path(path)
    spec_file = path / SPEC_FILENAME if path.is_dir() and not path.is_symlink() else path
    data = read_regular_file(spec_file, max_bytes=MAX_SPEC_BYTES, what="environment spec")
    spec = parse_spec(load_json_bytes(data, max_bytes=MAX_SPEC_BYTES))
    spec_dir = spec_file.parent.resolve()
    assets_dir = None
    if spec.assets is not None:
        candidate = spec_dir / spec.assets
        ensure_no_symlink_components(candidate, root=spec_dir, what="assets directory")
        if not candidate.is_dir():
            raise ValidationError(f"assets directory is not a directory: {candidate}")
        assets_dir = candidate
    return replace(
        spec, spec_dir=spec_dir, assets_dir=assets_dir, sha256=hashlib.sha256(data).hexdigest()
    )
