import io
import json
import tarfile
from dataclasses import replace

import pytest

from aeo.docker_backend import AGENT_UID, DockerBackend, docker_env, new_run_id
from aeo.errors import DockerUnavailableError
from aeo.proc import ProcResult
from aeo.result import validate_result
from aeo.spec import load_spec

from .test_spec import base_spec, write_env

RUN_ID = "aeo-00112233445566aa"
IMAGE_IDS = {
    "aeo-demo-agent:0.1.0": "sha256:" + "a" * 64,
    "aeo-demo-verifier:0.1.0": "sha256:" + "b" * 64,
}


def ok(stdout=b"", returncode=0, stderr=b"", timed_out=False, total=None):
    return ProcResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        stdout_total=len(stdout) if total is None else total,
        stderr_total=len(stderr),
        stdout_truncated=total is not None and total > len(stdout),
        stderr_truncated=False,
        timed_out=timed_out,
        duration_seconds=0.1,
    )


STARTED = "2026-10-08T00:00:00.1Z"


def state(exit_code=0, oom=False, error="", status="exited", started=STARTED):
    return json.dumps(
        {
            "Status": status,
            "Running": status == "running",
            "ExitCode": exit_code,
            "OOMKilled": oom,
            "Error": error,
            "StartedAt": started,
        }
    )


STATE_OK = state()


class FakeDocker:
    """Scriptable stand-in for the docker CLI. Records every argv."""

    def __init__(self):
        self.calls = []
        self.stdin = {}
        self.missing_images = set()
        self.image_configs = {}  # image -> JSON of .Config (default: no Volumes key at all)
        self.fail = {}  # key -> ProcResult
        self.agent_result = ok(b"agent says hi\n")
        self.agent_state = STATE_OK
        self.verifier_result = ok(b'{"reward": 1.0, "details": {"reason": "correct"}}\n')
        self.verifier_state = STATE_OK
        self.raise_on = {}
        self.leftover_containers = b""
        self.leftover_volumes = b""

    def key(self, args):
        if args[0] in ("image", "volume", "container"):
            return f"{args[0]} {args[1]}"
        return args[0]

    def run(self, args, *, timeout, stdin=None, max_stdout=0, max_stderr=0, on_timeout=None):
        assert all(isinstance(a, str) for a in args)
        self.calls.append(list(args))
        key = self.key(args)
        if stdin is not None:
            self.stdin[key] = stdin
        if key in self.raise_on:
            raise self.raise_on[key]
        if key in self.fail:
            return self.fail[key]
        if key == "image inspect":
            if args[-1] in self.missing_images:
                return ok(returncode=1, stderr=b"Error: No such image")
            config = self.image_configs.get(args[-1], '{"Cmd": ["true"]}')
            return ok(f"{IMAGE_IDS[args[-1]]} {config}\n".encode())
        if key == "start":
            if self.agent_result.timed_out and on_timeout:
                on_timeout()
            return self.agent_result
        if key == "run":
            if self.verifier_result.timed_out and on_timeout:
                on_timeout()
            return self.verifier_result
        if key == "inspect":
            name = args[-1]
            return ok(
                (self.agent_state if name.endswith("-agent") else self.verifier_state).encode()
            )
        if key == "ps":
            return ok(self.leftover_containers)
        if key == "volume ls":
            return ok(self.leftover_volumes)
        return ok()

    def cmds(self, key):
        return [c for c in self.calls if self.key(c) == key]

    def index(self, pred):
        for i, c in enumerate(self.calls):
            if pred(c):
                return i
        raise AssertionError("call not found")


@pytest.fixture
def spec(tmp_path):
    env = write_env(tmp_path, base_spec())
    (env / "assets" / "numbers.txt").write_text("1\n2\n")
    return load_spec(env)


def run(spec, fake):
    backend = DockerBackend(fake)
    return backend.run(spec, run_id=RUN_ID)


def flag_value(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def test_new_run_ids_are_unique_and_valid():
    ids = {new_run_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(i.startswith("aeo-") and len(i) == 20 for i in ids)


def test_happy_path_completed_with_reward(spec):
    fake = FakeDocker()
    out = run(spec, fake)
    result = validate_result(out.result)
    assert result["status"] == "completed"
    assert result["reward"] == 1.0
    assert result["error"] is None
    assert result["verifier"]["details"] == {"reason": "correct"}
    assert result["agent"]["exit_code"] == 0
    assert result["cleanup"] == {"ok": True, "errors": []}
    assert result["agent"]["image"] == "aeo-demo-agent:0.1.0"
    assert result["agent"]["image_id"] == IMAGE_IDS["aeo-demo-agent:0.1.0"]
    assert out.agent_stdout == b"agent says hi\n"


def test_agent_container_is_isolated(spec):
    fake = FakeDocker()
    run(spec, fake)
    [create] = [c for c in fake.cmds("create") if f"{RUN_ID}-agent" in c]
    joined = " ".join(create)
    assert flag_value(create, "--network") == ["none"]
    assert flag_value(create, "--cap-drop") == ["ALL"]
    assert flag_value(create, "--security-opt") == ["no-new-privileges"]
    assert "--read-only" in create
    assert flag_value(create, "--user") == [f"{AGENT_UID}:{AGENT_UID}"]
    assert flag_value(create, "--memory") == ["256m"]
    assert flag_value(create, "--memory-swap") == ["256m"]
    assert flag_value(create, "--pids-limit") == ["64"]
    assert flag_value(create, "--cpus") == ["1.0"]
    assert flag_value(create, "--pull") == ["never"]
    assert flag_value(create, "--log-driver") == ["none"]
    [mount] = flag_value(create, "--mount")
    assert mount == f"type=volume,src={RUN_ID}-ws,dst=/workspace,volume-nocopy"
    for forbidden in ["--privileged", "-v", "--volume", "--cap-add", "docker.sock", "bind", "-e"]:
        assert forbidden not in create, forbidden
    assert "--env" not in create or flag_value(create, "--env") == ["HOME=/tmp"]
    assert "SECRET" not in joined and "TOKEN" not in joined
    # spec command comes last, after the image
    # the container is created from the exact image ID that was inspected, not the tag
    assert "aeo-demo-agent:0.1.0" not in create
    image_at = create.index(IMAGE_IDS["aeo-demo-agent:0.1.0"])
    assert create[image_at + 1 :] == list(spec.agent.command)


def test_workspace_is_populated_without_bind_mounts_and_prep_never_starts(spec):
    fake = FakeDocker()
    run(spec, fake)
    [prep] = [c for c in fake.cmds("create") if f"{RUN_ID}-prep" in c]
    assert flag_value(prep, "--network") == ["none"]
    assert flag_value(prep, "--mount") == [
        f"type=volume,src={RUN_ID}-ws,dst=/aeo-workspace,volume-nocopy"
    ]
    # the prep container uses the trusted verifier image and is never started
    assert IMAGE_IDS["aeo-demo-verifier:0.1.0"] in prep
    assert not [c for c in fake.cmds("start") if f"{RUN_ID}-prep" in c]
    [cp] = fake.cmds("cp")
    assert cp == ["cp", "-a", "-", f"{RUN_ID}-prep:/"]
    with tarfile.open(fileobj=io.BytesIO(fake.stdin["cp"])) as tar:
        names = sorted(tar.getnames())
    assert names == ["aeo-workspace", "aeo-workspace/numbers.txt", "aeo-workspace/task.md"]


def test_agent_is_removed_before_verifier_reads_workspace_read_only(spec):
    fake = FakeDocker()
    run(spec, fake)
    rm_agent = fake.index(lambda c: c[0] == "rm" and f"{RUN_ID}-agent" in c)
    verifier_run = fake.index(lambda c: c[0] == "run")
    assert rm_agent < verifier_run
    verifier = fake.calls[verifier_run]
    assert flag_value(verifier, "--mount") == [
        f"type=volume,src={RUN_ID}-ws,dst=/workspace,readonly,volume-nocopy"
    ]
    assert flag_value(verifier, "--network") == ["none"]
    assert flag_value(verifier, "--cap-drop") == ["ALL"]
    assert "--read-only" in verifier
    assert "--rm" not in verifier
    assert IMAGE_IDS["aeo-demo-verifier:0.1.0"] in verifier


def test_everything_is_labelled_and_cleaned_up(spec):
    fake = FakeDocker()
    run(spec, fake)
    for c in fake.cmds("create") + fake.cmds("run") + fake.cmds("volume create"):
        assert f"aeo.run_id={RUN_ID}" in flag_value(c, "--label")
    assert ["volume", "rm", "-f", f"{RUN_ID}-ws"] in fake.calls
    # final leak check by label
    assert any(c[0] == "ps" and f"label=aeo.run_id={RUN_ID}" in c for c in fake.calls)
    assert any(c[:2] == ["volume", "ls"] for c in fake.calls)


def test_zero_reward_is_completed_not_error(spec):
    fake = FakeDocker()
    fake.verifier_result = ok(b'{"reward": 0, "details": {"reason": "wrong"}}')
    result = run(spec, fake).result
    assert result["status"] == "completed"
    assert result["reward"] == 0.0


@pytest.mark.parametrize(
    "stdout",
    [b'{"reward": NaN}', b'{"reward": true}', b'{"reward": 1.5}', b"not json", b""],
)
def test_invalid_verifier_output_is_error_with_null_reward(spec, stdout):
    fake = FakeDocker()
    fake.verifier_result = ok(stdout)
    result = validate_result(run(spec, fake).result)
    assert result["status"] == "error"
    assert result["reward"] is None
    assert result["error"]["kind"] == "verifier_output_invalid"
    assert result["cleanup"]["ok"]


def test_oversized_verifier_output_is_error(spec):
    fake = FakeDocker()
    fake.verifier_result = ok(b'{"reward": 1}', total=10**6)
    result = run(spec, fake).result
    assert result["error"]["kind"] == "verifier_output_invalid"


def test_verifier_nonzero_exit_is_error(spec):
    fake = FakeDocker()
    fake.verifier_result = ok(b'{"reward": 1}', returncode=2)
    fake.verifier_state = state(2)
    result = run(spec, fake).result
    assert result["status"] == "error"
    assert result["reward"] is None
    assert result["error"]["kind"] == "verifier_failed"
    assert result["verifier"]["exit_code"] == 2


def test_verifier_timeout_kills_and_is_error(spec):
    fake = FakeDocker()
    fake.verifier_result = ok(returncode=137, timed_out=True)
    result = run(spec, fake).result
    assert result["error"]["kind"] == "verifier_timeout"
    assert result["reward"] is None
    assert ["kill", f"{RUN_ID}-verifier"] in fake.calls


def test_agent_timeout_is_recorded_and_workspace_still_verified(spec):
    fake = FakeDocker()
    fake.agent_result = ok(b"partial", returncode=137, timed_out=True)
    fake.agent_state = state(137)
    fake.verifier_result = ok(b'{"reward": 0}')
    result = validate_result(run(spec, fake).result)
    assert ["kill", f"{RUN_ID}-agent"] in fake.calls
    assert result["agent"]["timed_out"] is True
    assert result["status"] == "completed"
    assert result["reward"] == 0.0


def test_agent_crash_is_scored_by_verifier(spec):
    fake = FakeDocker()
    fake.agent_result = ok(returncode=1)
    fake.agent_state = state(1, oom=True)
    fake.verifier_result = ok(b'{"reward": 0}')
    result = run(spec, fake).result
    assert result["status"] == "completed"
    assert result["agent"]["exit_code"] == 1
    assert result["agent"]["oom_killed"] is True


def test_agent_that_never_started_is_error(spec):
    fake = FakeDocker()
    fake.agent_result = ok(returncode=127, stderr=b"exec: not found")
    fake.agent_state = state(127, error="exec failed")
    result = run(spec, fake).result
    assert result["error"]["kind"] == "agent_start_failed"
    assert result["reward"] is None
    assert not fake.cmds("run")  # verifier not run


def test_missing_image_fails_before_creating_anything(spec):
    fake = FakeDocker()
    fake.missing_images = {"aeo-demo-verifier:0.1.0"}
    result = validate_result(run(spec, fake).result)
    assert result["error"]["kind"] == "image_unavailable"
    assert "aeo-demo-verifier:0.1.0" in result["error"]["message"]
    assert not fake.cmds("volume create")
    assert not fake.cmds("create")
    assert result["cleanup"]["ok"]


def test_asset_copy_failure_cleans_up_volume(spec):
    fake = FakeDocker()
    fake.fail["cp"] = ok(returncode=1, stderr=b"Error response from daemon: boom")
    result = run(spec, fake).result
    assert result["error"]["kind"] == "workspace_setup_failed"
    assert ["volume", "rm", "-f", f"{RUN_ID}-ws"] in fake.calls
    assert ["rm", "-f", f"{RUN_ID}-prep"] in fake.calls or any(
        c[0] == "rm" and f"{RUN_ID}-prep" in c for c in fake.calls
    )


def test_interrupt_during_agent_still_cleans_up(spec):
    fake = FakeDocker()
    fake.raise_on["start"] = KeyboardInterrupt()
    out = run(spec, fake)
    assert out.result["error"]["kind"] == "interrupted"
    assert out.interrupted
    assert ["volume", "rm", "-f", f"{RUN_ID}-ws"] in fake.calls
    assert any(c[0] == "rm" and f"{RUN_ID}-agent" in c for c in fake.calls)


def test_cleanup_failure_is_reported(spec):
    fake = FakeDocker()
    fake.fail["volume rm"] = ok(returncode=1, stderr=b"volume is in use")
    fake.leftover_volumes = f"{RUN_ID}-ws\n".encode()
    result = validate_result(run(spec, fake).result)
    assert result["cleanup"]["ok"] is False
    assert result["cleanup"]["errors"]
    # the task outcome itself is still reported
    assert result["status"] == "completed"


def test_docker_error_message_is_bounded_and_printable(spec):
    fake = FakeDocker()
    fake.fail["volume create"] = ok(returncode=1, stderr=b"\x1b[31mbad\x00\n" + b"x" * 5000)
    result = validate_result(run(spec, fake).result)
    assert result["error"]["kind"] == "workspace_setup_failed"
    msg = result["error"]["message"]
    assert len(msg) <= 500
    assert "\x1b" not in msg and "\x00" not in msg


def test_docker_missing_raises_unavailable(spec):
    fake = FakeDocker()
    fake.raise_on["version"] = FileNotFoundError("docker")
    with pytest.raises(DockerUnavailableError):
        DockerBackend(fake).check_available()


def test_docker_env_only_passes_client_settings(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-pass")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "nope")
    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/docker.sock")
    env = docker_env()
    assert env["DOCKER_HOST"] == "unix:///tmp/docker.sock"
    assert "OPENAI_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_spec_resources_flow_into_verifier_flags(spec):
    spec = replace(spec, verifier=replace(spec.verifier, timeout_seconds=7))
    fake = FakeDocker()
    run(spec, fake)
    [verifier] = fake.cmds("run")
    assert flag_value(verifier, "--memory") == ["512m"]
    assert flag_value(verifier, "--pids-limit") == ["128"]


def test_agent_still_running_after_attach_ends_is_error_not_reward(spec):
    # e.g. the attach stream dropped while the agent kept working
    fake = FakeDocker()
    fake.agent_result = ok(returncode=1, stderr=b"attach: connection reset")
    fake.agent_state = state(0, status="running")
    result = validate_result(run(spec, fake).result)
    assert result["status"] == "error"
    assert result["reward"] is None
    assert result["error"]["kind"] == "docker_error"
    assert not fake.cmds("run")  # verifier never scores a half-finished workspace
    assert any(c[0] == "rm" and f"{RUN_ID}-agent" in c for c in fake.calls)


def test_agent_that_never_left_created_state_is_error(spec):
    fake = FakeDocker()
    fake.agent_result = ok(returncode=1, timed_out=True)
    fake.agent_state = state(0, status="created", started="0001-01-01T00:00:00Z")
    result = validate_result(run(spec, fake).result)
    assert result["error"]["kind"] == "agent_start_failed"
    assert result["reward"] is None
    assert not fake.cmds("run")


def test_verifier_not_exited_is_error(spec):
    fake = FakeDocker()
    fake.verifier_result = ok(b'{"reward": 0}', returncode=1)
    fake.verifier_state = state(0, status="running")
    result = validate_result(run(spec, fake).result)
    assert result["status"] == "error"
    assert result["reward"] is None
    assert result["error"]["kind"] == "verifier_failed"


def test_fractional_cpus_are_not_rounded(spec):
    agent = replace(spec.agent, resources=replace(spec.agent.resources, cpus=0.25))
    fake = FakeDocker()
    run(replace(spec, agent=agent), fake)
    [create] = [c for c in fake.cmds("create") if f"{RUN_ID}-agent" in c]
    assert flag_value(create, "--cpus") == ["0.25"]


def test_attach_transport_failure_leaving_created_state_is_not_rewarded(spec):
    # Reviewer scenario: `docker start --attach` fails before the agent ever runs; the
    # container stays `created` with an empty Error and Docker's default ExitCode 0.
    fake = FakeDocker()
    fake.agent_result = ok(returncode=1, stderr=b"error during connect: EOF")
    fake.agent_state = state(0, status="created", started="0001-01-01T00:00:00Z")
    fake.verifier_result = ok(b'{"reward": 0.0, "details": {"reason": "missing_answer"}}\n')
    result = validate_result(run(spec, fake).result)
    assert result["status"] == "error"
    assert result["reward"] is None
    assert result["error"]["kind"] == "agent_start_failed"
    assert not fake.cmds("run")


def test_attach_exit_code_disagreeing_with_container_is_infrastructure_error(spec):
    # The container ran and exited 0, but the attach client failed (125 = docker error):
    # aeo can not tell that the agent's run was observed correctly, so no reward.
    fake = FakeDocker()
    fake.agent_result = ok(returncode=125, stderr=b"Error response from daemon: hijack failed")
    fake.agent_state = state(0)
    result = validate_result(run(spec, fake).result)
    assert result["status"] == "error"
    assert result["reward"] is None
    assert result["error"]["kind"] == "docker_error"
    assert not fake.cmds("run")


def test_verifier_client_failure_is_not_scored(spec):
    fake = FakeDocker()
    fake.verifier_result = ok(b'{"reward": 1.0}', returncode=125, stderr=b"daemon went away")
    fake.verifier_state = state(0)
    result = validate_result(run(spec, fake).result)
    assert result["reward"] is None
    assert result["error"]["kind"] == "verifier_failed"


@pytest.mark.parametrize("image", ["aeo-demo-agent:0.1.0", "aeo-demo-verifier:0.1.0"])
def test_image_declared_volumes_are_rejected_before_anything_is_created(spec, image):
    fake = FakeDocker()
    fake.image_configs = {image: '{"Volumes": {"/scratch": {}}}'}
    result = validate_result(run(spec, fake).result)
    assert result["status"] == "error"
    assert result["error"]["kind"] == "image_unsupported"
    assert "/scratch" in result["error"]["message"] and image in result["error"]["message"]
    assert not fake.cmds("volume create")
    assert not fake.cmds("create")
    assert not fake.cmds("run")


@pytest.mark.parametrize("config", ["null", '{"Volumes": null}', '{"Volumes": {}}'])
def test_images_without_volumes_are_accepted(spec, config):
    fake = FakeDocker()
    fake.image_configs = {"aeo-demo-agent:0.1.0": config}
    assert run(spec, fake).result["status"] == "completed"


@pytest.mark.parametrize("config", ["garbage", "[]", '{"Volumes": ["/x"]}'])
def test_unreadable_image_config_is_rejected_before_anything_is_created(spec, config):
    fake = FakeDocker()
    fake.image_configs = {"aeo-demo-agent:0.1.0": config}
    result = validate_result(run(spec, fake).result)
    assert result["error"]["kind"] == "image_unsupported"
    assert not fake.cmds("volume create")


def test_container_removal_also_removes_anonymous_volumes(spec):
    # Defence in depth on top of rejecting image VOLUMEs: `rm -v` drops anonymous volumes
    # (it never removes the named run workspace, which is removed explicitly).
    fake = FakeDocker()
    run(spec, fake)
    removals = [c for c in fake.calls if c[0] == "rm"]
    assert removals
    assert all(c[:3] == ["rm", "-f", "-v"] for c in removals), removals
