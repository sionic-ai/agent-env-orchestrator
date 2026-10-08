import json
import os
import stat

import pytest

from aeo import cli
from aeo.docker_backend import DockerBackend
from aeo.errors import DockerUnavailableError
from aeo.runs import RunStore
from aeo.spec import SPEC_FILENAME

from .test_docker_backend import FakeDocker, ok
from .test_spec import base_spec, write_env


@pytest.fixture
def env_dir(tmp_path):
    env = write_env(tmp_path, base_spec())
    (env / "assets" / "numbers.txt").write_text("1\n2\n")
    return env


@pytest.fixture
def fake(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(cli, "make_backend", lambda: DockerBackend(fake))
    return fake


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_validate_ok(capsys, env_dir):
    code, out, _ = run_cli(capsys, "validate", str(env_dir))
    assert code == 0
    assert "cpu-sum-demo" in out
    assert "2 asset file(s)" in out


def test_validate_bad_spec_exit_2(capsys, tmp_path):
    env = write_env(tmp_path, base_spec(network="host"))
    code, _, err = run_cli(capsys, "validate", str(env))
    assert code == 2
    assert "network" in err


def test_validate_bad_assets_exit_2(capsys, env_dir):
    os.symlink("/etc/hosts", env_dir / "assets" / "hosts")
    code, _, err = run_cli(capsys, "validate", str(env_dir))
    assert code == 2
    assert "symlink" in err


def test_catalog_lists_valid_and_invalid(capsys, tmp_path):
    root = tmp_path / "envs"
    good = write_env(root, base_spec(), name="good")
    bad = write_env(root, base_spec(id="Bad Id"), name="bad")
    (root / "not-an-env").mkdir()
    assert good.exists() and bad.exists()
    code, out, _ = run_cli(capsys, "catalog", "--root", str(root), "--json")
    assert code == 0
    entries = json.loads(out)
    by_valid = {e["valid"]: e for e in entries}
    assert by_valid[True]["id"] == "cpu-sum-demo"
    assert by_valid[True]["agent_image"] == "aeo-demo-agent:0.1.0"
    assert "environment id" in by_valid[False]["error"]

    code, out, _ = run_cli(capsys, "catalog", "--root", str(root))
    assert code == 0
    assert "cpu-sum-demo" in out and "INVALID" in out


def test_run_writes_result_and_logs_to_host(capsys, env_dir, tmp_path, fake):
    runs = tmp_path / "runs"
    code, out, _ = run_cli(capsys, "run", str(env_dir), "--runs-dir", str(runs))
    assert code == 0
    result = json.loads(out)
    assert result["status"] == "completed"
    assert result["reward"] == 1.0
    run_dir = runs / result["run_id"]
    assert stat.S_IMODE(os.stat(run_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(run_dir / "result.json").st_mode) == 0o600
    assert json.loads((run_dir / "result.json").read_text()) == result
    assert (run_dir / "agent.stdout.log").read_bytes() == b"agent says hi\n"

    code, out2, _ = run_cli(capsys, "result", result["run_id"], "--runs-dir", str(runs))
    assert code == 0
    assert json.loads(out2) == result


def test_run_error_result_exit_1(capsys, env_dir, tmp_path, fake):
    fake.verifier_result = ok(b'{"reward": NaN}')
    code, out, _ = run_cli(capsys, "run", str(env_dir), "--runs-dir", str(tmp_path / "r"))
    assert code == 1
    result = json.loads(out)
    assert result["reward"] is None
    assert result["error"]["kind"] == "verifier_output_invalid"


def test_run_invalid_spec_creates_no_run(capsys, tmp_path, fake):
    env = write_env(tmp_path, base_spec(privileged=True))
    runs = tmp_path / "runs"
    code, _, err = run_cli(capsys, "run", str(env), "--runs-dir", str(runs))
    assert code == 2
    assert "privileged" in err
    assert not fake.calls
    assert not runs.exists() or not any(runs.iterdir())


def test_run_without_docker_exit_4(capsys, env_dir, tmp_path, fake):
    fake.raise_on["version"] = FileNotFoundError("docker")
    code, _, err = run_cli(capsys, "run", str(env_dir), "--runs-dir", str(tmp_path / "r"))
    assert code == 4
    assert "docker" in err.lower()


def test_run_interrupted_exit_130(capsys, env_dir, tmp_path, fake):
    fake.raise_on["start"] = KeyboardInterrupt()
    code, out, _ = run_cli(capsys, "run", str(env_dir), "--runs-dir", str(tmp_path / "r"))
    assert code == 130
    assert json.loads(out)["error"]["kind"] == "interrupted"


def test_run_with_cleanup_failure_exit_1(capsys, env_dir, tmp_path, fake):
    fake.leftover_volumes = b"leftover\n"
    code, out, _ = run_cli(capsys, "run", str(env_dir), "--runs-dir", str(tmp_path / "r"))
    assert code == 1
    assert json.loads(out)["cleanup"]["ok"] is False


@pytest.mark.parametrize("bad", ["../../etc/passwd", "aeo-xyz", "/abs", "aeo-0123456789abcdef/.."])
def test_result_rejects_unsafe_run_id(capsys, tmp_path, bad):
    code, _, err = run_cli(capsys, "result", bad, "--runs-dir", str(tmp_path))
    assert code == 2
    assert "run id" in err


def test_result_rejects_tampered_file(capsys, tmp_path):
    run_dir = tmp_path / "aeo-0123456789abcdef"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "completed", "reward": True}))
    code, _, _ = run_cli(capsys, "result", "aeo-0123456789abcdef", "--runs-dir", str(tmp_path))
    assert code == 2


def test_result_refuses_symlinked_result(capsys, tmp_path):
    run_dir = tmp_path / "aeo-0123456789abcdef"
    run_dir.mkdir()
    os.symlink("/etc/hosts", run_dir / "result.json")
    code, _, err = run_cli(capsys, "result", "aeo-0123456789abcdef", "--runs-dir", str(tmp_path))
    assert code == 2
    assert "symlink" in err


def test_run_store_never_overwrites(tmp_path):
    store = RunStore(tmp_path)
    store.create("aeo-0123456789abcdef")
    with pytest.raises(FileExistsError):
        store.create("aeo-0123456789abcdef")


def test_docker_unavailable_error_message_is_used(monkeypatch, capsys, env_dir, tmp_path):
    class Broken:
        def check_available(self):
            raise DockerUnavailableError("docker daemon not reachable: nope")

    monkeypatch.setattr(cli, "make_backend", lambda: Broken())
    code, _, err = run_cli(capsys, "run", str(env_dir), "--runs-dir", str(tmp_path / "r"))
    assert code == 4
    assert "not reachable" in err


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "0.1.0" in capsys.readouterr().out


def test_spec_file_path_also_accepted(capsys, env_dir):
    code, _, _ = run_cli(capsys, "validate", str(env_dir / SPEC_FILENAME))
    assert code == 0


def test_run_with_unusable_runs_dir_fails_before_running(capsys, env_dir, tmp_path, fake):
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    code, _, err = run_cli(capsys, "run", str(env_dir), "--runs-dir", str(not_a_dir))
    assert code == 2
    assert "runs directory" in err
    assert not fake.cmds("create")  # nothing was executed


def test_result_file_written_atomically(capsys, env_dir, tmp_path, fake):
    runs = tmp_path / "runs"
    code, out, _ = run_cli(capsys, "run", str(env_dir), "--runs-dir", str(runs))
    assert code == 0
    run_dir = runs / json.loads(out)["run_id"]
    assert sorted(p.name for p in run_dir.iterdir()) == [
        "agent.stderr.log",
        "agent.stdout.log",
        "result.json",
        "verifier.stderr.log",
    ]
