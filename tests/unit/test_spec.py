import json
import os

import pytest

from aeo.errors import ValidationError
from aeo.spec import SPEC_FILENAME, SPEC_SCHEMA_VERSION, load_spec, parse_spec


def base_spec(**overrides):
    spec = {
        "schema_version": SPEC_SCHEMA_VERSION,
        "id": "cpu-sum-demo",
        "description": "Sum integers from numbers.txt into answer.txt.",
        "assets": "assets",
        "network": "none",
        "agent": {
            "image": "aeo-demo-agent:0.1.0",
            "command": ["python3", "/opt/aeo-agent/fixture_agent.py", "solve"],
            "timeout_seconds": 60,
            "resources": {"cpus": 1, "memory_mb": 256, "pids": 64},
        },
        "verifier": {
            "image": "aeo-demo-verifier:0.1.0",
            "command": ["python3", "/opt/aeo-verifier/verify.py"],
            "timeout_seconds": 30,
        },
    }
    spec.update(overrides)
    return spec


def write_env(tmp_path, spec, name="env"):
    env = tmp_path / name
    (env / "assets").mkdir(parents=True)
    (env / "assets" / "task.md").write_text("task\n")
    (env / SPEC_FILENAME).write_text(json.dumps(spec))
    return env


def test_parse_valid_spec_applies_defaults():
    spec = parse_spec(base_spec())
    assert spec.id == "cpu-sum-demo"
    assert spec.agent.command[-1] == "solve"
    assert spec.agent.resources.memory_mb == 256
    # verifier resources fall back to conservative defaults
    assert spec.verifier.resources.cpus == 1.0
    assert spec.verifier.resources.memory_mb == 512
    assert spec.verifier.resources.pids == 128
    assert spec.network == "none"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(schema_version="aeo.environment/v0"),
        lambda s: s.update(id="../escape"),
        lambda s: s.update(network="bridge"),
        lambda s: s.update(network="host"),
        lambda s: s.update(privileged=True),
        lambda s: s.update(volumes=["/:/host"]),
        lambda s: s["agent"].update(env={"OPENAI_API_KEY": "x"}),
        lambda s: s["agent"].update(cap_add=["SYS_ADMIN"]),
        lambda s: s["agent"].update(mounts=["/var/run/docker.sock:/var/run/docker.sock"]),
        lambda s: s["agent"].update(user="root"),
        lambda s: s["agent"].update(image="--privileged"),
        lambda s: s["agent"].update(command="python3 agent.py; curl evil"),
        lambda s: s["agent"].update(timeout_seconds=0),
        lambda s: s["agent"].update(timeout_seconds=3601),
        lambda s: s["agent"].update(timeout_seconds=True),
        lambda s: s["agent"].update(timeout_seconds=1.5),
        lambda s: s["verifier"].update(timeout_seconds=601),
        lambda s: s["agent"]["resources"].update(cpus=64),
        lambda s: s["agent"]["resources"].update(cpus=0),
        lambda s: s["agent"]["resources"].update(memory_mb=1 << 20),
        lambda s: s["agent"]["resources"].update(pids=-1),
        lambda s: s["agent"]["resources"].update(swap_mb=100),
        lambda s: s.update(description="x" * 1001),
        lambda s: s.update(assets="/etc"),
        lambda s: s.update(assets="../secrets"),
        lambda s: s.update(assets="assets/../../x"),
        lambda s: s.pop("verifier"),
    ],
)
def test_parse_rejects_unsafe_or_out_of_bounds_specs(mutate):
    spec = base_spec()
    mutate(spec)
    with pytest.raises(ValidationError):
        parse_spec(spec)


def test_spec_must_be_an_object():
    with pytest.raises(ValidationError):
        parse_spec(["not", "an", "object"])


def test_load_spec_from_directory_resolves_assets_inside_env(tmp_path):
    env = write_env(tmp_path, base_spec())
    spec = load_spec(env)
    assert spec.assets_dir == (env / "assets").resolve()
    assert len(spec.sha256) == 64
    assert load_spec(env / SPEC_FILENAME).sha256 == spec.sha256


def test_load_spec_without_assets_has_no_assets_dir(tmp_path):
    raw = base_spec()
    raw.pop("assets")
    env = write_env(tmp_path, raw)
    assert load_spec(env).assets_dir is None


def test_load_spec_rejects_symlinked_assets_dir(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    raw = base_spec(assets="linked")
    env = write_env(tmp_path, raw)
    os.symlink(outside, env / "linked")
    with pytest.raises(ValidationError, match="symlink"):
        load_spec(env)


def test_load_spec_rejects_symlinked_spec_file(tmp_path):
    env = write_env(tmp_path, base_spec())
    real = env / SPEC_FILENAME
    link = tmp_path / "link.json"
    os.symlink(real, link)
    with pytest.raises(ValidationError, match="symlink"):
        load_spec(link)


def test_load_spec_rejects_oversized_file(tmp_path):
    env = tmp_path / "env"
    env.mkdir()
    (env / SPEC_FILENAME).write_text(" " * (70 * 1024) + "{}")
    with pytest.raises(ValidationError, match="too large"):
        load_spec(env)


def test_load_spec_missing_file_is_validation_error(tmp_path):
    with pytest.raises(ValidationError, match="not found"):
        load_spec(tmp_path / "nope")


@pytest.mark.parametrize("cpus", [10**400, -(10**400)])
def test_huge_integer_cpus_is_validation_error_not_overflow(cpus):
    raw = base_spec()
    raw["agent"]["resources"] = {"cpus": cpus}
    with pytest.raises(ValidationError, match="cpus"):
        parse_spec(raw)


def test_load_spec_refuses_fifo_without_blocking(tmp_path):
    import subprocess
    import sys

    env = tmp_path / "env"
    env.mkdir()
    os.mkfifo(env / "environment.json")
    # In a subprocess: a blocking open of a FIFO with no writer would hang the test runner.
    code = (
        "import sys\nfrom aeo.errors import ValidationError\nfrom aeo.spec import load_spec\n"
        "try:\n    load_spec(sys.argv[1])\nexcept ValidationError as e:\n    print(e)\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", code, str(env)], capture_output=True, timeout=20, check=False
    )
    assert b"not a regular file" in res.stdout, res
