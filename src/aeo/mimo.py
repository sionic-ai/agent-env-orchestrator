"""First-pass MiMo adapter: normalise *local* MiMo rows into catalog metadata.

Input (both files are supplied by the operator; aeo downloads nothing):

* rows: ``.jsonl`` / ``.json`` exports of dataset rows, or ``.parquet`` (needs the optional
  ``pyarrow`` extra). Each row has ``extra_info.instance_json``, a JSON *string* holding
  ``docker_image``, ``cwd``, ``instance_id`` and ``dataset_type``. Some domains (e.g. music)
  have no ``docker_image``.
* mapping: ``.jsonl`` lines ``{"dataset_image": ..., "dockerhub_image": ...}``.

Output is metadata only. Prompts, solutions and other row fields are never copied (for
Parquet only the ``extra_info`` column is read). No MiMo task can be executed by aeo yet:
every entry is ``executable: false`` with a reason, and ``require_executable`` always raises.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from aeo.errors import UnsupportedExecutionError, ValidationError
from aeo.safefs import open_regular_file, read_regular_file
from aeo.validation import load_json_bytes, validate_image_ref

MIMO_CATALOG_SCHEMA = "aeo.mimo-catalog/v1"

_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,255}$")
_DATASET_TYPE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_DATASET_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,511}$")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

REASON_NO_IMAGE = (
    "no docker image in instance metadata (e.g. the music domain); "
    "aeo has no runtime for this task type"
)
REASON_UNMAPPED = "dataset image has no dockerhub_image mapping"
REASON_NOT_IMPLEMENTED = (
    "MiMo task execution (agent harness, tests and scoring protocol) is not implemented "
    "in aeo; metadata only"
)


@dataclass(frozen=True)
class MimoLimits:
    max_rows: int = 200_000
    max_line_bytes: int = 16 * 1024 * 1024
    max_json_file_bytes: int = 256 * 1024 * 1024
    max_instance_json_bytes: int = 64 * 1024
    max_mapping_lines: int = 100_000
    max_mapping_bytes: int = 64 * 1024 * 1024


class _SkipRow(Exception):
    pass


def _check_not_symlink(path: Path, what: str) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise ValidationError(f"{what} not found: {path}") from None
    if stat.S_ISLNK(st.st_mode):
        raise ValidationError(f"{what} must not be a symlink: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise ValidationError(f"{what} is not a regular file: {path}")


def _iter_jsonl(path: Path, max_line: int, what: str) -> Iterator[Any]:
    _check_not_symlink(path, what)
    fd = open_regular_file(path, what=what)  # re-checked on the descriptor, never blocks
    with os.fdopen(fd, "rb") as fh:
        lineno = 0
        while line := fh.readline(max_line + 1):
            lineno += 1
            if len(line) > max_line:
                raise ValidationError(f"{what} line {lineno} longer than {max_line} bytes")
            if line.strip():
                try:
                    yield load_json_bytes(line, max_bytes=max_line)
                except ValidationError as exc:
                    raise ValidationError(f"{what} line {lineno}: {exc}") from None


def _iter_rows(path: Path, limits: MimoLimits) -> Iterator[Any]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        yield from _iter_jsonl(path, limits.max_line_bytes, "rows file")
    elif suffix == ".json":
        data = read_regular_file(path, max_bytes=limits.max_json_file_bytes, what="rows file")
        rows = load_json_bytes(data, max_bytes=limits.max_json_file_bytes)
        if not isinstance(rows, list):
            raise ValidationError("rows .json file must contain a JSON array of rows")
        yield from rows
    elif suffix == ".parquet":
        _check_not_symlink(path, "rows file")
        try:
            import pyarrow.parquet as pq
        except ImportError:
            raise ValidationError(
                "reading .parquet needs pyarrow: pip install 'agent-env-orchestrator[parquet]'"
            ) from None
        parquet = pq.ParquetFile(path)
        if "extra_info" not in parquet.schema_arrow.names:
            raise ValidationError("parquet rows have no extra_info column")
        for batch in parquet.iter_batches(columns=["extra_info"], batch_size=1024):
            for extra in batch.column(0).to_pylist():
                yield {"extra_info": extra}
    else:
        raise ValidationError("unsupported rows format (use .jsonl, .json or .parquet)")


def load_image_mapping(path: Path | str, limits: MimoLimits | None = None) -> dict[str, str]:
    limits = limits or MimoLimits()
    path = Path(path)
    _check_not_symlink(path, "mapping file")
    if os.lstat(path).st_size > limits.max_mapping_bytes:
        raise ValidationError(f"mapping file too large (limit {limits.max_mapping_bytes} bytes)")
    mapping: dict[str, str] = {}
    for n, item in enumerate(_iter_jsonl(path, 64 * 1024, "mapping file"), start=1):
        if n > limits.max_mapping_lines:
            raise ValidationError(f"mapping file has more than {limits.max_mapping_lines} lines")
        if not isinstance(item, dict) or set(item) != {"dataset_image", "dockerhub_image"}:
            raise ValidationError(
                f"mapping line {n} must have exactly dataset_image and dockerhub_image"
            )
        source, target = item["dataset_image"], item["dockerhub_image"]
        if not isinstance(source, str) or not _DATASET_IMAGE_RE.fullmatch(source):
            raise ValidationError(f"mapping line {n}: invalid dataset_image")
        try:
            validate_image_ref(target)
        except ValidationError:
            raise ValidationError(f"mapping line {n}: invalid dockerhub_image") from None
        if mapping.get(source, target) != target:
            raise ValidationError(f"mapping line {n}: conflicting mapping for {source}")
        mapping[source] = target
    return mapping


def _instance(row: Any, limits: MimoLimits) -> dict[str, Any]:
    if not isinstance(row, dict) or "extra_info" not in row:
        raise _SkipRow("row has no extra_info")
    extra = row["extra_info"]
    if isinstance(extra, str):
        try:
            extra = load_json_bytes(extra.encode(), max_bytes=limits.max_line_bytes)
        except ValidationError as exc:
            raise _SkipRow(f"extra_info string is not valid JSON: {exc}") from None
    if not isinstance(extra, dict) or not isinstance(extra.get("instance_json"), str):
        raise _SkipRow("extra_info.instance_json missing or not a string")
    raw = extra["instance_json"].encode()
    try:
        instance = load_json_bytes(raw, max_bytes=limits.max_instance_json_bytes, max_depth=8)
    except ValidationError as exc:
        raise _SkipRow(f"instance_json: {exc}") from None
    if not isinstance(instance, dict):
        raise _SkipRow("instance_json must decode to an object")
    return instance


def _cwd(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 1024 or "\x00" in value:
        raise _SkipRow("cwd must be a string")
    pure = PurePosixPath(value)
    if not pure.is_absolute() or ".." in pure.parts:
        raise _SkipRow("cwd must be an absolute path without '..'")
    return str(pure)


def normalize_row(index: int, row: Any, mapping: dict[str, str], limits: MimoLimits) -> dict:
    inst = _instance(row, limits)
    instance_id = inst.get("instance_id")
    if (
        not isinstance(instance_id, str)
        or not _INSTANCE_ID_RE.fullmatch(instance_id)
        or (".." in instance_id.split("/"))
    ):
        raise _SkipRow("instance_id missing or not a safe identifier")
    dataset_type = inst.get("dataset_type")
    if dataset_type is not None and (
        not isinstance(dataset_type, str) or not _DATASET_TYPE_RE.fullmatch(dataset_type)
    ):
        raise _SkipRow("dataset_type is not a safe identifier")
    dataset_image = inst.get("docker_image")
    if dataset_image in (None, ""):
        dataset_image = None
    elif not isinstance(dataset_image, str) or not _DATASET_IMAGE_RE.fullmatch(dataset_image):
        raise _SkipRow("docker_image is not a plausible image reference")
    image = mapping.get(dataset_image) if dataset_image else None
    if dataset_image is None:
        reason = REASON_NO_IMAGE
    elif image is None:
        reason = REASON_UNMAPPED
    else:
        reason = REASON_NOT_IMPLEMENTED
    return {
        "row_index": index,
        "instance_id": instance_id,
        "dataset_type": dataset_type,
        "cwd": _cwd(inst.get("cwd")),
        "dataset_image": dataset_image,
        "image": image,
        "executable": False,
        "unsupported_reason": reason,
    }


def build_catalog(
    rows_path: Path | str, mapping_path: Path | str, limits: MimoLimits | None = None
) -> dict[str, Any]:
    limits = limits or MimoLimits()
    rows_path, mapping_path = Path(rows_path), Path(mapping_path)
    mapping = load_image_mapping(mapping_path, limits)
    entries: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(_iter_rows(rows_path, limits)):
        if index >= limits.max_rows:
            raise ValidationError(f"more than {limits.max_rows} rows; split the input")
        try:
            entry = normalize_row(index, row, mapping, limits)
            if entry["instance_id"] in seen:
                raise _SkipRow("duplicate instance_id")
        except _SkipRow as exc:
            skipped.append({"row_index": index, "reason": str(exc)[:300]})
            continue
        seen.add(entry["instance_id"])
        entries.append(entry)
    return {
        "schema_version": MIMO_CATALOG_SCHEMA,
        "source": {"rows_file": rows_path.name, "mapping_file": mapping_path.name},
        "entries": entries,
        "skipped": skipped,
    }


def require_executable(entry: dict[str, Any]) -> None:
    """Gate for future execution support. Today every MiMo entry is metadata only."""
    raise UnsupportedExecutionError(
        f"MiMo instance {entry.get('instance_id')!r} is metadata only: "
        f"{entry.get('unsupported_reason') or REASON_NOT_IMPLEMENTED}"
    )


def load_catalog(path: Path | str) -> dict[str, Any]:
    data = read_regular_file(Path(path), max_bytes=512 * 1024 * 1024, what="MiMo catalog")
    catalog = load_json_bytes(data, max_bytes=512 * 1024 * 1024)
    if not isinstance(catalog, dict) or catalog.get("schema_version") != MIMO_CATALOG_SCHEMA:
        raise ValidationError(f"not a {MIMO_CATALOG_SCHEMA} file: {path}")
    entries = catalog.get("entries")
    if not isinstance(entries, list):
        raise ValidationError("MiMo catalog has no entries list")
    for index, entry in enumerate(entries):
        _check_catalog_entry(index, entry)
    return catalog


_CATALOG_STR_FIELDS = ("dataset_type", "cwd", "dataset_image", "image", "unsupported_reason")


def _check_catalog_entry(index: int, entry: Any) -> None:
    """A catalog may have been edited by hand: re-check the shape ``build_catalog`` writes."""
    where = f"MiMo catalog entry {index}"
    if not isinstance(entry, dict):
        raise ValidationError(f"{where} must be a JSON object")
    instance_id = entry.get("instance_id")
    if not isinstance(instance_id, str) or not _INSTANCE_ID_RE.fullmatch(instance_id):
        raise ValidationError(f"{where}: instance_id missing or not a safe identifier")
    if entry.get("executable") is not False:
        raise ValidationError(f"{where}: executable must be false (MiMo execution is unsupported)")
    for key in _CATALOG_STR_FIELDS:
        value = entry.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 1024):
            raise ValidationError(f"{where}: {key} must be a string or null")


def is_mimo_catalog(path: Path) -> bool:
    """Cheap sniff used by ``aeo run`` to give a clear 'unsupported' error."""
    if not path.is_file() or path.is_symlink():
        return False
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    return MIMO_CATALOG_SCHEMA.encode() in head


def write_catalog(path: Path | str, catalog: dict[str, Any]) -> None:
    fd = os.open(Path(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        # Insertion order keeps schema_version first, which is_mimo_catalog() relies on.
        json.dump(catalog, fh, indent=2, allow_nan=False)
        fh.write("\n")
