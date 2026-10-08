"""Small, strict validators shared by the spec, result, asset and CLI layers."""

from __future__ import annotations

import json
import re
from typing import Any

from aeo.errors import ValidationError

ENV_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
RUN_ID_RE = re.compile(r"^aeo-[0-9a-f]{16}$")

# A conservative subset of the Docker reference grammar. It never starts with "-", so an
# image can not be mistaken for a docker CLI option, and it only contains ASCII.
_COMPONENT = r"[a-z0-9]+(?:(?:\.|_|__|-+)[a-z0-9]+)*"
# Like Docker, only treat the first component as a registry host when it contains a "." or a
# port, or is "localhost"; otherwise "UPPER/case" would sneak uppercase through as a "host".
_LABEL = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?"
_DOMAIN = (
    rf"(?:(?:{_LABEL}(?:\.{_LABEL})+(?::[0-9]{{1,5}})?"
    rf"|{_LABEL}:[0-9]{{1,5}}"
    r"|localhost)/)"
)
IMAGE_REF_RE = re.compile(
    rf"^(?:{_DOMAIN})?{_COMPONENT}(?:/{_COMPONENT})*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
    r"(?:@sha256:[a-f0-9]{64})?$"
)
MAX_IMAGE_REF_LEN = 255

MAX_ARGV_ITEMS = 64
MAX_ARG_LEN = 4096

MAX_JSON_DEPTH = 32


def validate_env_id(value: object) -> str:
    if not isinstance(value, str) or not ENV_ID_RE.fullmatch(value):
        raise ValidationError(
            "environment id must match ^[a-z0-9][a-z0-9._-]{0,63}$ (lowercase slug)"
        )
    return value


def validate_run_id(value: object) -> str:
    if not isinstance(value, str) or not RUN_ID_RE.fullmatch(value):
        raise ValidationError("run id must look like aeo-<16 lowercase hex digits>")
    return value


def validate_image_ref(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_IMAGE_REF_LEN
        or not IMAGE_REF_RE.fullmatch(value)
    ):
        raise ValidationError(
            "image must be a plain Docker image reference (name[:tag][@sha256:...])"
        )
    return value


def validate_argv(value: object, *, field: str = "command") -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValidationError(f"{field} must be a non-empty JSON array of strings")
    if len(value) > MAX_ARGV_ITEMS:
        raise ValidationError(f"{field} has more than {MAX_ARGV_ITEMS} items")
    for item in value:
        if not isinstance(item, str):
            raise ValidationError(f"{field} items must be strings")
        if len(item) > MAX_ARG_LEN:
            raise ValidationError(f"{field} item longer than {MAX_ARG_LEN} characters")
        if "\x00" in item:
            raise ValidationError(f"{field} items must not contain NUL bytes")
    return tuple(value)


def _reject_constant(name: str) -> Any:
    raise ValidationError(f"JSON constant {name} is not allowed")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in pairs:
        if key in out:
            raise ValidationError("JSON object has a duplicate key")
        out[key] = val
    return out


def _depth(value: Any, limit: int) -> None:
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > limit:
            raise ValidationError(f"JSON nests deeper than {limit} levels")
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)


def load_json_bytes(data: bytes, *, max_bytes: int, max_depth: int = MAX_JSON_DEPTH) -> Any:
    """Parse untrusted JSON with size, depth, NaN/Infinity and duplicate-key checks."""
    if len(data) > max_bytes:
        raise ValidationError(f"JSON document too large (limit {max_bytes} bytes)")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError("JSON document is not valid UTF-8") from None
    try:
        value = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_no_duplicates)
    except RecursionError:
        raise ValidationError(f"JSON nests deeper than {max_depth} levels") from None
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid JSON at line {exc.lineno} column {exc.colno}") from None
    except ValueError:
        # e.g. an integer longer than Python's int-digit limit
        raise ValidationError("JSON document contains an unparseable number") from None
    _depth(value, max_depth)
    return value
