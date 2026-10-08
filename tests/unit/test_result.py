import json
import math

import pytest

from aeo.errors import ValidationError
from aeo.result import (
    ERROR_KINDS,
    MAX_VERIFIER_STDOUT_BYTES,
    RESULT_SCHEMA_VERSION,
    check_reward,
    parse_verifier_output,
    validate_result,
)


@pytest.mark.parametrize("reward", [0, 0.0, 1, 1.0, 0.25, 1e-9])
def test_check_reward_accepts_unit_interval(reward):
    assert check_reward(reward) == float(reward)
    assert isinstance(check_reward(reward), float)


@pytest.mark.parametrize(
    "reward",
    [True, False, -0.01, 1.0000001, math.nan, math.inf, -math.inf, "1", None, [1], 10**400],
)
def test_check_reward_refuses_invalid(reward):
    with pytest.raises(ValidationError):
        check_reward(reward)


def test_parse_verifier_output_zero_reward_is_a_valid_score():
    verdict = parse_verifier_output(b'{"reward": 0, "details": {"reason": "wrong answer"}}\n')
    assert verdict.reward == 0.0
    assert verdict.details == {"reason": "wrong answer"}


def test_parse_verifier_output_details_optional():
    assert parse_verifier_output(b'{"reward": 1.0}').details is None


@pytest.mark.parametrize(
    "stdout",
    [
        b"",
        b"1.0",
        b"[1.0]",
        b'{"reward": NaN}',
        b'{"reward": Infinity}',
        b'{"reward": true}',
        b'{"reward": 2}',
        b'{"reward": -1}',
        b'{"reward": "1"}',
        b'{"reward": null}',
        b'{"score": 1}',
        b'{"reward": 1, "extra": 1}',
        b'{"reward": 1}\n{"reward": 0}',
        b'log line\n{"reward": 1}',
        b'{"reward": 1, "details": []}',
        b'{"reward": 1, "details": {"nested": {"a": 1}}}',
        b'{"reward": 1, "details": {"bad key!": 1}}',
        b'{"reward": 1, "details": {"x": "' + b"a" * 300 + b'"}}',
        b'{"reward": 1, "details": {' + b",".join(b'"k%d": 1' % i for i in range(40)) + b"}}",
        b'{"reward": 1, "reward": 0}',
        b'{"reward": ' + b"1" * 5000 + b"}",
    ],
)
def test_parse_verifier_output_rejects_malformed(stdout):
    with pytest.raises(ValidationError):
        parse_verifier_output(stdout)


def test_parse_verifier_output_rejects_truncated_or_oversize():
    with pytest.raises(ValidationError, match="too large"):
        parse_verifier_output(b'{"reward": 1}', truncated=True)
    big = b'{"reward": 1, "details": {"a": "' + b"x" * MAX_VERIFIER_STDOUT_BYTES + b'"}}'
    with pytest.raises(ValidationError):
        parse_verifier_output(big)


def completed(**overrides):
    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "run_id": "aeo-0123456789abcdef",
        "environment_id": "cpu-sum-demo",
        "spec_sha256": "0" * 64,
        "status": "completed",
        "reward": 0.0,
        "error": None,
        "agent": {
            "image": "aeo-demo-agent:0.1.0",
            "image_id": "sha256:" + "1" * 64,
            "exit_code": 0,
            "timed_out": False,
            "oom_killed": False,
            "duration_seconds": 0.5,
            "stdout_bytes": 10,
            "stderr_bytes": 0,
            "output_truncated": False,
        },
        "verifier": {
            "image": "aeo-demo-verifier:0.1.0",
            "image_id": "sha256:" + "2" * 64,
            "exit_code": 0,
            "timed_out": False,
            "duration_seconds": 0.2,
            "details": {"reason": "wrong"},
        },
        "cleanup": {"ok": True, "errors": []},
        "started_at": "2026-10-08T00:00:00Z",
        "finished_at": "2026-10-08T00:00:01Z",
        "aeo_version": "0.1.0",
    }
    result.update(overrides)
    return result


def test_validate_result_accepts_completed_zero_and_error_null():
    assert validate_result(completed())["reward"] == 0.0
    err = completed(
        status="error",
        reward=None,
        error={"kind": "verifier_output_invalid", "message": "reward must be finite"},
    )
    assert validate_result(err)["reward"] is None
    assert "verifier_output_invalid" in ERROR_KINDS


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "completed", "reward": None},
        {"status": "completed", "reward": True},
        {"status": "completed", "reward": 1.5},
        {"status": "completed", "error": {"kind": "docker_error", "message": "x"}},
        {"status": "error", "reward": 0.0, "error": {"kind": "docker_error", "message": "x"}},
        {"status": "error", "reward": None, "error": None},
        {"status": "error", "reward": None, "error": {"kind": "made_up", "message": "x"}},
        {"status": "weird"},
        {"schema_version": "other"},
        {"run_id": "../../etc"},
        {"unexpected": 1},
    ],
)
def test_validate_result_rejects_inconsistent(overrides):
    with pytest.raises(ValidationError):
        validate_result(completed(**overrides))


def test_result_round_trips_through_json():
    data = json.loads(json.dumps(completed()))
    assert validate_result(data) == completed()
