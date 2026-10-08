"""Verifier output contract and run result contract (schema ``aeo.result/v1``).

Reward semantics:

* ``status == "completed"``: the trusted verifier ran to completion and returned a valid
  reward in ``[0.0, 1.0]``. A reward of ``0.0`` is a real score ("the task was not solved").
* ``status == "error"``: the environment, Docker, or the verifier failed. ``reward`` is
  ``null`` - never ``0.0`` - so infrastructure failures can not be mistaken for task failures.
"""

from __future__ import annotations

import math
import re
from typing import Any

from aeo.errors import ValidationError
from aeo.validation import (
    ENV_ID_RE,
    IMAGE_REF_RE,
    RUN_ID_RE,
    load_json_bytes,
)

RESULT_SCHEMA_VERSION = "aeo.result/v1"
MAX_VERIFIER_STDOUT_BYTES = 16 * 1024
MAX_DETAIL_KEYS = 32
MAX_DETAIL_STRING = 256
MAX_ERROR_MESSAGE = 500
_DETAIL_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

ERROR_KINDS = frozenset(
    {
        "image_unavailable",  # an image is not present locally (aeo never pulls)
        "image_unsupported",  # an image declares VOLUMEs (anonymous volumes would escape cleanup)
        "workspace_setup_failed",  # volume creation or asset copy failed
        "agent_start_failed",  # docker could not create/start the agent container
        "verifier_failed",  # verifier exited non-zero
        "verifier_timeout",  # verifier exceeded its timeout and was killed
        "verifier_output_invalid",  # stdout was not a valid verdict
        "docker_error",  # any other docker CLI/daemon failure
        "interrupted",  # aeo itself was interrupted (e.g. Ctrl-C)
        "internal_error",  # bug in aeo
    }
)


class VerifierVerdict:
    __slots__ = ("details", "reward")

    def __init__(self, reward: float, details: dict[str, Any] | None) -> None:
        self.reward = reward
        self.details = details


def check_reward(value: Any) -> float:
    """Return ``value`` as a float if it is a finite number in [0, 1]; refuse anything else."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValidationError("reward must be a JSON number (booleans are not rewards)")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationError("reward must be finite")
    if not 0 <= value <= 1:
        raise ValidationError("reward must be within [0, 1]")
    return float(value)


def _check_details(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError("details must be a flat JSON object")
    if len(value) > MAX_DETAIL_KEYS:
        raise ValidationError(f"details has more than {MAX_DETAIL_KEYS} keys")
    for key, item in value.items():
        if not _DETAIL_KEY_RE.fullmatch(key):
            raise ValidationError("details keys must match [A-Za-z0-9_.-]{1,64}")
        if isinstance(item, str):
            if len(item) > MAX_DETAIL_STRING:
                raise ValidationError(f"details strings are limited to {MAX_DETAIL_STRING} chars")
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValidationError("details numbers must be finite")
        elif item is not None and not isinstance(item, bool | int):
            raise ValidationError("details values must be strings, numbers, booleans or null")
    return dict(value)


def parse_verifier_output(stdout: bytes, *, truncated: bool = False) -> VerifierVerdict:
    """Parse the verifier's stdout: exactly one JSON object ``{"reward": x, "details": {...}}``."""
    if truncated:
        raise ValidationError(f"verifier output too large (limit {MAX_VERIFIER_STDOUT_BYTES} B)")
    obj = load_json_bytes(stdout.strip(), max_bytes=MAX_VERIFIER_STDOUT_BYTES, max_depth=3)
    if not isinstance(obj, dict):
        raise ValidationError("verifier output must be a JSON object")
    unknown = set(obj) - {"reward", "details"}
    if unknown or "reward" not in obj:
        raise ValidationError("verifier output must have 'reward' and optional 'details' only")
    return VerifierVerdict(check_reward(obj["reward"]), _check_details(obj.get("details")))


# --- result document validation ---------------------------------------------------------


def _obj(value: Any, where: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{where} must be an object")
    if set(value) != keys:
        raise ValidationError(f"{where} must have exactly the keys {sorted(keys)}")
    return value


def _str(value: Any, where: str, pattern: re.Pattern[str] | None = None, max_len: int = 200):
    if not isinstance(value, str) or len(value) > max_len:
        raise ValidationError(f"{where} must be a string")
    if pattern is not None and not pattern.fullmatch(value):
        raise ValidationError(f"{where} has an invalid format")


def _bool(value: Any, where: str) -> None:
    if not isinstance(value, bool):
        raise ValidationError(f"{where} must be a boolean")


def _num(value: Any, where: str, *, nullable: bool = False, integer: bool = False) -> None:
    if value is None and nullable:
        return
    kinds = int if integer else int | float
    if isinstance(value, bool) or not isinstance(value, kinds):
        raise ValidationError(f"{where} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationError(f"{where} must be finite")


_AGENT_KEYS = {
    "image",
    "image_id",
    "exit_code",
    "timed_out",
    "oom_killed",
    "duration_seconds",
    "stdout_bytes",
    "stderr_bytes",
    "output_truncated",
}
_VERIFIER_KEYS = {"image", "image_id", "exit_code", "timed_out", "duration_seconds", "details"}
_RESULT_KEYS = {
    "schema_version",
    "run_id",
    "environment_id",
    "spec_sha256",
    "status",
    "reward",
    "error",
    "agent",
    "verifier",
    "cleanup",
    "started_at",
    "finished_at",
    "aeo_version",
}


def _check_container(value: Any, where: str, keys: set[str]) -> None:
    if value is None:
        return
    obj = _obj(value, where, keys)
    _str(obj["image"], f"{where}.image", IMAGE_REF_RE, 255)
    if obj["image_id"] is not None:
        _str(obj["image_id"], f"{where}.image_id", _IMAGE_ID_RE)
    _num(obj["exit_code"], f"{where}.exit_code", nullable=True, integer=True)
    _bool(obj["timed_out"], f"{where}.timed_out")
    _num(obj["duration_seconds"], f"{where}.duration_seconds")
    if "oom_killed" in keys:
        _bool(obj["oom_killed"], f"{where}.oom_killed")
        _num(obj["stdout_bytes"], f"{where}.stdout_bytes", integer=True)
        _num(obj["stderr_bytes"], f"{where}.stderr_bytes", integer=True)
        _bool(obj["output_truncated"], f"{where}.output_truncated")
    if "details" in keys:
        _check_details(obj["details"])


def validate_result(value: Any) -> dict[str, Any]:
    """Validate a result document (e.g. read back from disk) and return it unchanged."""
    obj = _obj(value, "result", _RESULT_KEYS)
    if obj["schema_version"] != RESULT_SCHEMA_VERSION:
        raise ValidationError(f"schema_version must be {RESULT_SCHEMA_VERSION!r}")
    _str(obj["run_id"], "run_id", RUN_ID_RE)
    _str(obj["environment_id"], "environment_id", ENV_ID_RE)
    _str(obj["spec_sha256"], "spec_sha256", _SHA256_RE)
    _str(obj["started_at"], "started_at")
    _str(obj["finished_at"], "finished_at")
    _str(obj["aeo_version"], "aeo_version")
    if obj["status"] == "completed":
        check_reward(obj["reward"])
        if obj["error"] is not None:
            raise ValidationError("completed results must have error = null")
    elif obj["status"] == "error":
        if obj["reward"] is not None:
            raise ValidationError("error results must have reward = null")
        err = _obj(obj["error"], "error", {"kind", "message"})
        if err["kind"] not in ERROR_KINDS:
            raise ValidationError("error.kind is not a known error kind")
        _str(err["message"], "error.message", max_len=MAX_ERROR_MESSAGE)
    else:
        raise ValidationError("status must be 'completed' or 'error'")
    _check_container(obj["agent"], "agent", _AGENT_KEYS)
    _check_container(obj["verifier"], "verifier", _VERIFIER_KEYS)
    cleanup = _obj(obj["cleanup"], "cleanup", {"ok", "errors"})
    _bool(cleanup["ok"], "cleanup.ok")
    if not isinstance(cleanup["errors"], list) or len(cleanup["errors"]) > 20:
        raise ValidationError("cleanup.errors must be a short list")
    for item in cleanup["errors"]:
        _str(item, "cleanup.errors[]", max_len=MAX_ERROR_MESSAGE)
    return obj
