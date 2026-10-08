"""End-to-end tests against a real Docker daemon. Run with: pytest -m docker

They build the two demo images from images/ (cached by Docker after the first build) and
execute real runs of environments/cpu-sum-demo with different fixture-agent modes.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from aeo.docker_backend import DockerBackend, DockerCLI, new_run_id
from aeo.result import validate_result
from aeo.spec import SPEC_FILENAME, load_spec

pytestmark = pytest.mark.docker

REPO = Path(__file__).resolve().parents[2]
DEMO_ENV = REPO / "environments" / "cpu-sum-demo"
AGENT_IMAGE = "aeo-demo-agent:0.1.0"
VERIFIER_IMAGE = "aeo-demo-verifier:0.1.0"


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


@pytest.fixture(scope="session", autouse=True)
def demo_images():
    if shutil.which("docker") is None or docker("version", check=False).returncode != 0:
        pytest.skip("docker daemon not available")
    subprocess.run([str(REPO / "scripts" / "build-demo-images.sh")], check=True)


@pytest.fixture
def make_env(tmp_path):
    """Copy the demo environment and patch its spec."""

    def make(agent_cmd=None, verifier_cmd=None, agent_timeout=None, verifier_timeout=None, **top):
        env = tmp_path / "env"
        shutil.copytree(DEMO_ENV, env)
        raw = json.loads((env / SPEC_FILENAME).read_text())
        if agent_cmd is not None:
            raw["agent"]["command"] = agent_cmd
        if verifier_cmd is not None:
            raw["verifier"]["command"] = verifier_cmd
        if agent_timeout is not None:
            raw["agent"]["timeout_seconds"] = agent_timeout
        if verifier_timeout is not None:
            raw["verifier"]["timeout_seconds"] = verifier_timeout
        raw.update(top)
        (env / SPEC_FILENAME).write_text(json.dumps(raw))
        return load_spec(env)

    return make


def fixture_agent(mode: str) -> list[str]:
    return ["python3", "/opt/aeo-agent/fixture_agent.py", mode]


def run(spec):
    run_id = new_run_id()
    outputs = DockerBackend(DockerCLI()).run(spec, run_id=run_id)
    validate_result(outputs.result)
    assert_no_leftovers(run_id)
    return outputs


def assert_no_leftovers(run_id: str) -> None:
    label = f"label=aeo.run_id={run_id}"
    assert docker("ps", "-a", "-q", "--filter", label).stdout.strip() == ""
    assert docker("volume", "ls", "-q", "--filter", label).stdout.strip() == ""


def test_solving_agent_gets_reward_one(make_env):
    out = run(make_env())
    r = out.result
    assert r["status"] == "completed", (r["error"], r["agent"])
    assert r["reward"] == 1.0
    assert r["verifier"]["details"]["reason"] == "correct"
    assert r["agent"]["exit_code"] == 0
    assert r["cleanup"]["ok"], r["cleanup"]
    assert r["agent"]["image_id"].startswith("sha256:")


def test_wrong_answer_is_completed_with_zero(make_env):
    r = run(make_env(agent_cmd=fixture_agent("wrong"))).result
    assert r["status"] == "completed"
    assert r["reward"] == 0.0
    assert r["verifier"]["details"]["reason"] == "wrong_answer"


def test_crashing_agent_is_scored_zero_not_error(make_env):
    r = run(make_env(agent_cmd=fixture_agent("crash"))).result
    assert r["status"] == "completed"
    assert r["reward"] == 0.0
    assert r["agent"]["exit_code"] == 3
    assert r["verifier"]["details"]["reason"] == "missing_answer"


def test_hanging_agent_is_killed_and_cleaned_up(make_env):
    r = run(make_env(agent_cmd=fixture_agent("hang"), agent_timeout=3)).result
    assert r["agent"]["timed_out"] is True
    assert r["agent"]["duration_seconds"] < 30
    assert r["status"] == "completed"
    assert r["reward"] == 0.0


def test_symlink_answer_can_not_trick_verifier(make_env):
    r = run(make_env(agent_cmd=fixture_agent("symlink"))).result
    assert r["status"] == "completed"
    assert r["reward"] == 0.0
    assert r["verifier"]["details"]["reason"] == "answer_not_regular_file"


def test_fifo_answer_can_not_stall_verifier_into_error(make_env):
    # A stalled verifier would be a null-reward "error" - i.e. an agent could turn a
    # failure into an excluded sample. It must be a plain 0.0 instead.
    r = run(make_env(agent_cmd=fixture_agent("fifo"))).result
    assert r["status"] == "completed", (r["error"], r["agent"])
    assert r["reward"] == 0.0
    assert r["verifier"]["details"]["reason"] == "answer_not_regular_file"


def test_non_canonical_answer_is_rejected(make_env):
    r = run(make_env(agent_cmd=fixture_agent("plus"))).result
    assert r["reward"] == 0.0
    assert r["verifier"]["details"]["reason"] == "answer_not_an_integer"


def test_agent_isolation_as_observed_from_inside(make_env):
    out = run(make_env(agent_cmd=fixture_agent("probe")))
    assert out.result["reward"] == 1.0  # probe mode also solves the task
    probe = json.loads(out.agent_stdout.decode().strip().splitlines()[-1])
    assert probe["uid"] == 10001 and probe["gid"] == 10001
    assert probe["interfaces_up"] == ["lo"]
    assert probe["ipv4_routes"] == 0
    assert probe["tcp_connect"].startswith("error")
    assert probe["docker_socket_present"] is False
    assert probe["verifier_code_visible"] is False
    assert probe["cap_eff"] == "0000000000000000"
    assert probe["no_new_privs"] == "1"
    assert probe["rootfs_write"].startswith("error")
    assert probe["tmp_exec"].startswith("error")
    assert probe["workspace_write"] == "ok"
    assert probe["memory_max"] == str(512 * 1024 * 1024)
    assert probe["pids_max"] == "128"
    assert set(probe["env_keys"]) <= {
        "HOME",
        "HOSTNAME",
        "PATH",
        "LANG",
        "GPG_KEY",
        "PYTHON_VERSION",
        "PYTHON_SHA256",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
    }
    # The only storage-backed mounts are the run-owned volume and Docker's per-container
    # hosts/hostname/resolv.conf files: no host paths, no docker.sock.
    mounts = dict(probe["storage_mounts"])
    assert set(mounts) == {"/workspace", "/etc/hosts", "/etc/hostname", "/etc/resolv.conf"}
    assert mounts["/workspace"].endswith(f"/volumes/{out.result['run_id']}-ws/_data")
    for name in ("/etc/hosts", "/etc/hostname", "/etc/resolv.conf"):
        assert "/containers/" in mounts[name]


def test_invalid_verifier_output_is_error_with_null_reward(make_env):
    r = run(make_env(verifier_cmd=["python3", "-c", "print('{\"reward\": NaN}')"])).result
    assert r["status"] == "error"
    assert r["reward"] is None
    assert r["error"]["kind"] == "verifier_output_invalid"


def test_out_of_range_verifier_reward_is_error(make_env):
    r = run(make_env(verifier_cmd=["python3", "-c", "print('{\"reward\": 1.5}')"])).result
    assert r["reward"] is None
    assert r["error"]["kind"] == "verifier_output_invalid"


def test_failing_verifier_is_error(make_env):
    r = run(make_env(verifier_cmd=["python3", "-c", "import sys; sys.exit(2)"])).result
    assert r["status"] == "error"
    assert r["reward"] is None
    assert r["error"]["kind"] == "verifier_failed"
    assert r["verifier"]["exit_code"] == 2


def test_hanging_verifier_is_error(make_env):
    spec = make_env(
        verifier_cmd=["python3", "-c", "import time; time.sleep(600)"], verifier_timeout=3
    )
    r = run(spec).result
    assert r["error"]["kind"] == "verifier_timeout"
    assert r["reward"] is None


def test_missing_image_is_error_and_never_pulled(make_env, tmp_path):
    spec = make_env()
    raw = json.loads((spec.spec_dir / SPEC_FILENAME).read_text())
    raw["agent"]["image"] = "aeo-image-that-does-not-exist:never"
    (spec.spec_dir / SPEC_FILENAME).write_text(json.dumps(raw))
    r = run(load_spec(spec.spec_dir)).result
    assert r["error"]["kind"] == "image_unavailable"
    assert r["reward"] is None


def test_cli_end_to_end(tmp_path):
    runs = tmp_path / "runs"
    proc = subprocess.run(
        [sys.executable, "-m", "aeo", "run", str(DEMO_ENV), "--runs-dir", str(runs)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["reward"] == 1.0
    shown = subprocess.run(
        [sys.executable, "-m", "aeo", "result", result["run_id"], "--runs-dir", str(runs)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(shown.stdout) == result
    assert_no_leftovers(result["run_id"])


VOLUME_IMAGE = "aeo-test-volume-agent:0.1.0"


@pytest.fixture
def volume_image():
    """The demo agent plus an image-declared ``VOLUME /scratch``; removed afterwards."""
    dockerfile = f"FROM {AGENT_IMAGE}\nVOLUME /scratch\n"
    subprocess.run(
        ["docker", "build", "--pull=false", "-q", "-t", VOLUME_IMAGE, "-"],
        input=dockerfile,
        text=True,
        capture_output=True,
        check=True,
    )
    yield VOLUME_IMAGE
    docker("image", "rm", "-f", VOLUME_IMAGE, check=False)


def volumes() -> set[str]:
    return set(docker("volume", "ls", "-q").stdout.split())


def test_docker_rm_without_v_leaks_image_declared_volume(volume_image):
    """Reproduces the reviewer's finding on real Docker: plain `rm -f` leaves an unlabelled
    anonymous volume behind, `rm -f -v` does not, and `-v` keeps named volumes."""
    named = f"{new_run_id()}-ws"
    docker("volume", "create", named)
    try:
        for rm_args, should_leak in ((["rm", "-f"], True), (["rm", "-f", "-v"], False)):
            before = volumes()
            cid = docker(
                "create", "--mount", f"type=volume,src={named},dst=/workspace", volume_image
            ).stdout.strip()
            created = volumes() - before
            assert len(created) == 1  # the anonymous /scratch volume
            docker(*rm_args, cid)
            leaked = created & volumes()
            assert bool(leaked) is should_leak, (rm_args, leaked)
            for v in leaked:
                docker("volume", "rm", "-f", v)
            assert named in volumes()  # `-v` never removes the named workspace volume
    finally:
        docker("volume", "rm", "-f", named, check=False)


def test_image_declared_volume_is_rejected_before_anything_is_created(make_env, volume_image):
    before = volumes()
    spec = make_env()
    spec = replace(spec, agent=replace(spec.agent, image=volume_image))
    r = run(spec).result
    assert r["status"] == "error", r
    assert r["reward"] is None
    assert r["error"]["kind"] == "image_unsupported"
    assert "/scratch" in r["error"]["message"]
    assert r["cleanup"]["ok"]
    assert volumes() - before == set()


def test_image_volume_is_still_removed_if_the_guard_is_bypassed(
    make_env, volume_image, monkeypatch
):
    # Defence in depth: even if an image with VOLUME got through, `rm -f -v` removes it.
    real = DockerBackend.image_info

    def no_volumes(self, image):
        info = real(self, image)
        return None if info is None else (info[0], [])

    monkeypatch.setattr(DockerBackend, "image_info", no_volumes)
    before = volumes()
    spec = make_env(agent_cmd=fixture_agent("solve"))
    spec = replace(spec, agent=replace(spec.agent, image=volume_image))
    r = run(spec).result
    assert r["status"] == "completed", (r["error"], r["agent"])
    assert r["reward"] == 1.0
    assert volumes() - before == set()
