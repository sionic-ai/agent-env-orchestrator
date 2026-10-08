import json

import pytest

from aeo.errors import ValidationError
from aeo.validation import (
    load_json_bytes,
    validate_argv,
    validate_env_id,
    validate_image_ref,
    validate_run_id,
)


@pytest.mark.parametrize("value", ["cpu-sum-demo", "a", "env.v1", "x_y-1"])
def test_env_id_accepts_simple_slugs(value):
    assert validate_env_id(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "-leading-dash",
        ".hidden",
        "../etc",
        "a/b",
        "UPPER",
        "a" * 65,
        "sp ace",
        "nul\x00",
        3,
        None,
    ],
)
def test_env_id_rejects_unsafe_values(value):
    with pytest.raises(ValidationError):
        validate_env_id(value)


def test_run_id_format_is_strict():
    assert validate_run_id("aeo-0123456789abcdef") == "aeo-0123456789abcdef"
    for bad in ["aeo-../../x", "aeo-0123", "../aeo-0123456789abcdef", "aeo-0123456789ABCDEF"]:
        with pytest.raises(ValidationError):
            validate_run_id(bad)


@pytest.mark.parametrize(
    "ref",
    [
        "aeo-demo-agent:0.1.0",
        "python:3.12-slim",
        "ghcr.io/example/agent:1.2",
        "localhost:5000/team/img",
        "registry.example.com/a/b@sha256:" + "a" * 64,
        "img:tag@sha256:" + "0" * 64,
    ],
)
def test_image_ref_accepts_docker_references(ref):
    assert validate_image_ref(ref) == ref


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "--privileged",
        "-v/:/host",
        "UPPER/case",
        "img:tag with space",
        "img;rm -rf /",
        "img@sha256:short",
        "a" * 300,
        "img\n",
        ["list"],
    ],
)
def test_image_ref_rejects_option_injection_and_garbage(ref):
    with pytest.raises(ValidationError):
        validate_image_ref(ref)


def test_argv_must_be_bounded_list_of_strings():
    assert validate_argv(["python3", "/opt/agent/agent.py", "solve"]) == (
        "python3",
        "/opt/agent/agent.py",
        "solve",
    )
    for bad in [
        [],
        "python3 agent.py",
        ["ok", 1],
        ["nul\x00byte"],
        ["x"] * 65,
        ["y" * 4097],
    ]:
        with pytest.raises(ValidationError):
            validate_argv(bad)


def test_load_json_bytes_rejects_oversize_nan_and_duplicate_keys():
    assert load_json_bytes(b'{"a": 1}', max_bytes=100) == {"a": 1}
    with pytest.raises(ValidationError, match="too large"):
        load_json_bytes(b'{"a": "' + b"x" * 200 + b'"}', max_bytes=100)
    with pytest.raises(ValidationError, match="NaN"):
        load_json_bytes(b'{"a": NaN}', max_bytes=100)
    with pytest.raises(ValidationError, match="Infinity"):
        load_json_bytes(b'{"a": -Infinity}', max_bytes=100)
    with pytest.raises(ValidationError, match="duplicate"):
        load_json_bytes(b'{"a": 1, "a": 2}', max_bytes=100)
    with pytest.raises(ValidationError, match="invalid JSON"):
        load_json_bytes(b"{not json", max_bytes=100)
    with pytest.raises(ValidationError, match="UTF-8"):
        load_json_bytes(b'{"a": "\xff"}', max_bytes=100)


def test_load_json_bytes_rejects_deep_nesting():
    deep = ("[" * 200 + "]" * 200).encode()
    with pytest.raises(ValidationError, match="nest"):
        load_json_bytes(deep, max_bytes=10_000)
    assert json.dumps(load_json_bytes(b"[[[1]]]", max_bytes=100)) == "[[[1]]]"


def test_load_json_bytes_huge_integer_is_validation_error():
    # Python's int-digit limit raises a plain ValueError inside json.loads
    with pytest.raises(ValidationError):
        load_json_bytes(b'{"reward": ' + b"1" * 5000 + b"}", max_bytes=10_000)
