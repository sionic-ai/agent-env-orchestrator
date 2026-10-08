"""Local Docker backend: one isolated agent run plus one trusted verifier run per invocation.

Lifecycle of a run ``<run_id>`` (every object is labelled ``aeo.run_id=<run_id>``):

1. Check both images exist locally (``--pull never``: aeo never pulls).
2. Create the run-owned volume ``<run_id>-ws``.
3. Create (but never start) ``<run_id>-prep`` from the trusted verifier image with the volume
   mounted, and stream the validated asset tar into it with ``docker cp -a -``. No host path
   is bind-mounted and no process runs during population.
4. Create and start ``<run_id>-agent``: no network, all capabilities dropped,
   no-new-privileges, read-only root filesystem, fixed non-root uid, cpu/memory/pid limits.
   The workspace volume is its only writable persistent storage.
5. Remove the agent container, then run ``<run_id>-verifier`` with the volume mounted
   read-only. The verifier's code and expected answers live only in the verifier image.
6. Always remove containers and the volume, then re-check by label that nothing leaked.

Results come back to the host only through captured, size-bounded stdout.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from aeo import __version__
from aeo.assets import build_workspace_tar
from aeo.errors import DockerUnavailableError, ValidationError
from aeo.proc import ProcResult, run_bounded
from aeo.result import (
    MAX_ERROR_MESSAGE,
    MAX_VERIFIER_STDOUT_BYTES,
    RESULT_SCHEMA_VERSION,
    VerifierVerdict,
    parse_verifier_output,
)
from aeo.spec import ContainerRole, EnvironmentSpec
from aeo.validation import validate_run_id

AGENT_UID = 10001
WORKSPACE = "/workspace"
PREP_MOUNT = "aeo-workspace"
TMPFS = "/tmp:rw,nosuid,nodev,noexec,size=64m"  # noqa: S108 - path inside the container
LABEL_MANAGED = "aeo.managed=true"

OP_TIMEOUT_S = 120  # docker housekeeping commands (inspect, create, rm, ...)
KILL_TIMEOUT_S = 30
AGENT_MAX_OUTPUT = 1024 * 1024  # bytes kept per stream; the rest is counted and dropped
VERIFIER_MAX_STDERR = 8 * 1024
HOUSEKEEPING_MAX_OUTPUT = 64 * 1024
IMAGE_CONFIG_MAX_BYTES = 1024 * 1024

# Only settings the docker *client* needs are passed through to it. Containers never see the
# host environment: aeo passes no --env except a fixed HOME.
_DOCKER_ENV_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "TMPDIR",
    "XDG_RUNTIME_DIR",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "DOCKER_API_VERSION",
)

_UNPRINTABLE = re.compile(r"[^\x20-\x7e]+")


def docker_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    environ = os.environ if environ is None else environ
    return {k: environ[k] for k in _DOCKER_ENV_KEYS if k in environ}


def new_run_id() -> str:
    return "aeo-" + secrets.token_hex(8)


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _clean(text: str | bytes, limit: int = 300) -> str:
    """Make docker output safe to embed in a result: printable ASCII, bounded."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = _UNPRINTABLE.sub(" ", text).strip()
    return text[:limit]


class DockerRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        timeout: float,
        stdin: bytes | None = None,
        max_stdout: int = HOUSEKEEPING_MAX_OUTPUT,
        max_stderr: int = HOUSEKEEPING_MAX_OUTPUT,
        on_timeout: Callable[[], None] | None = None,
    ) -> ProcResult: ...


class DockerCLI:
    """Runs ``docker <args>`` with ``shell=False`` and a filtered client environment."""

    def __init__(self, docker: str = "docker", env: Mapping[str, str] | None = None) -> None:
        self.docker = docker
        self.env = docker_env() if env is None else dict(env)

    def run(
        self,
        args: list[str],
        *,
        timeout: float,
        stdin: bytes | None = None,
        max_stdout: int = HOUSEKEEPING_MAX_OUTPUT,
        max_stderr: int = HOUSEKEEPING_MAX_OUTPUT,
        on_timeout: Callable[[], None] | None = None,
    ) -> ProcResult:
        return run_bounded(
            [self.docker, *args],
            timeout=timeout,
            stdin=stdin,
            max_stdout=max_stdout,
            max_stderr=max_stderr,
            env=self.env,
            on_timeout=on_timeout,
        )


class _RunFailure(Exception):
    def __init__(
        self,
        kind: str,
        message: str,
        *,
        agent_info: dict[str, Any] | None = None,
        verifier_info: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = _clean(message, MAX_ERROR_MESSAGE)
        self.agent_info = agent_info
        self.verifier_info = verifier_info


@dataclass
class RunOutputs:
    result: dict[str, Any]
    agent_stdout: bytes = b""
    agent_stderr: bytes = b""
    verifier_stderr: bytes = b""
    interrupted: bool = False


@dataclass
class _Names:
    run_id: str
    volume: str = field(init=False)
    prep: str = field(init=False)
    agent: str = field(init=False)
    verifier: str = field(init=False)

    def __post_init__(self) -> None:
        self.volume = f"{self.run_id}-ws"
        self.prep = f"{self.run_id}-prep"
        self.agent = f"{self.run_id}-agent"
        self.verifier = f"{self.run_id}-verifier"


def _isolation_flags(role: ContainerRole) -> list[str]:
    """Fixed hardening applied to every started container. Not configurable by specs."""
    res = role.resources
    return [
        "--network", "none",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", TMPFS,
        "--user", f"{AGENT_UID}:{AGENT_UID}",
        "--env", "HOME=/tmp",
        "--cpus", repr(res.cpus),
        "--memory", f"{res.memory_mb}m",
        "--memory-swap", f"{res.memory_mb}m",
        "--pids-limit", str(res.pids),
        "--ulimit", "nofile=1024:1024",
        "--ipc", "private",
        "--log-driver", "none",
        "--pull", "never",
        "--workdir", WORKSPACE,
    ]  # fmt: skip


class DockerBackend:
    def __init__(self, docker: DockerRunner, *, now: Callable[[], str] = _utcnow) -> None:
        self.docker = docker
        self.now = now

    # -- helpers -----------------------------------------------------------------------

    def _labels(self, run_id: str, role: str) -> list[str]:
        return [
            "--label", LABEL_MANAGED,
            "--label", f"aeo.run_id={run_id}",
            "--label", f"aeo.role={role}",
        ]  # fmt: skip

    def _op(self, args: list[str], kind: str, what: str, **kw: Any) -> ProcResult:
        res = self.docker.run(args, timeout=kw.pop("timeout", OP_TIMEOUT_S), **kw)
        if res.timed_out or res.returncode != 0:
            detail = "timed out" if res.timed_out else _clean(res.stderr)
            raise _RunFailure(kind, f"{what} failed: {detail}")
        return res

    def _kill(self, name: str) -> Callable[[], None]:
        def kill() -> None:
            self.docker.run(["kill", name], timeout=KILL_TIMEOUT_S)

        return kill

    def _state(self, name: str) -> dict[str, Any]:
        res = self._op(
            ["inspect", "--format", "{{json .State}}", "--", name],
            "docker_error",
            f"inspect {name}",
        )
        try:
            state = json.loads(res.stdout)
        except ValueError:
            raise _RunFailure("docker_error", f"unparseable state for {name}") from None
        if not isinstance(state, dict):
            raise _RunFailure("docker_error", f"unexpected state for {name}")
        return state

    def check_available(self) -> str:
        try:
            res = self.docker.run(
                ["version", "--format", "{{.Server.Version}}"], timeout=OP_TIMEOUT_S
            )
        except FileNotFoundError:
            raise DockerUnavailableError("docker CLI not found on PATH") from None
        if res.returncode != 0 or res.timed_out:
            raise DockerUnavailableError(f"docker daemon not reachable: {_clean(res.stderr)}")
        return _clean(res.stdout, 64)

    def image_info(self, image: str) -> tuple[str, list[str]] | None:
        """Return the local image's ID and its declared VOLUME paths; None if not present.

        Raises ``_RunFailure("image_unsupported")`` if the image config can not be read.
        """
        # `.Config.Volumes` is absent (not null) on some engines, so read the whole config.
        res = self.docker.run(
            ["image", "inspect", "--format", "{{.Id}} {{json .Config}}", "--", image],
            timeout=OP_TIMEOUT_S,
            max_stdout=IMAGE_CONFIG_MAX_BYTES,
        )
        if res.returncode != 0 or res.timed_out:
            return None
        image_id, _, config_json = res.stdout.decode("utf-8", "replace").strip().partition(" ")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            return None
        try:
            if res.stdout_truncated:
                raise ValueError("image config too large")
            config = json.loads(config_json)
            volumes = config.get("Volumes") if isinstance(config, dict) else None
            if config is not None and not isinstance(config, dict):
                raise ValueError("image config is not an object")
            if volumes is not None and not isinstance(volumes, dict):
                raise ValueError("Volumes is not an object")
        except ValueError:
            raise _RunFailure(
                "image_unsupported", f"could not read the config of image {image}"
            ) from None
        return image_id, sorted(str(v) for v in volumes or {})

    # -- the run -----------------------------------------------------------------------

    def run(self, spec: EnvironmentSpec, *, run_id: str) -> RunOutputs:
        validate_run_id(run_id)
        # Validate and pack assets before touching Docker: a bad asset tree is a spec error,
        # not a run, and must not leave anything behind.
        tar = build_workspace_tar(
            spec.assets_dir, root_name=PREP_MOUNT, uid=AGENT_UID, gid=AGENT_UID
        )
        names = _Names(run_id)
        out = RunOutputs(result={})
        started_at = self.now()
        agent_info: dict[str, Any] | None = None
        verifier_info: dict[str, Any] | None = None
        status, reward, error = "error", None, None
        try:
            # Containers are created from the inspected image IDs, not the tags, so a tag
            # moved mid-run can not swap in a different image than the one recorded.
            ids: dict[str, str] = {}
            for role in (spec.agent, spec.verifier):
                info = self.image_info(role.image)
                if info is None:
                    raise _RunFailure(
                        "image_unavailable",
                        f"image {role.image} is not available locally "
                        "(aeo never pulls; build or pull it first)",
                    )
                image_id, volumes = info
                if volumes:
                    # Docker would create an unlabelled anonymous volume per declared path;
                    # it would outlive the run and escape the label-based leak check.
                    raise _RunFailure(
                        "image_unsupported",
                        f"image {role.image} declares VOLUME {', '.join(volumes)}; "
                        "images with VOLUME instructions are not supported",
                    )
                ids[role.image] = image_id
            self._prepare_workspace(names, ids[spec.verifier.image], tar)
            agent_info = self._run_agent(spec, names, ids[spec.agent.image], out)
            verifier_info, verdict = self._run_verifier(spec, names, ids[spec.verifier.image], out)
            status, reward = "completed", verdict.reward
        except _RunFailure as exc:
            error = {"kind": exc.kind, "message": exc.message}
            agent_info = exc.agent_info or agent_info
            verifier_info = exc.verifier_info or verifier_info
        except KeyboardInterrupt:
            out.interrupted = True
            error = {"kind": "interrupted", "message": "run interrupted; resources cleaned up"}
        except Exception as exc:
            error = {"kind": "internal_error", "message": _clean(type(exc).__name__)}
        finally:
            cleanup = self._cleanup(names)

        out.result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "run_id": run_id,
            "environment_id": spec.id,
            "spec_sha256": spec.sha256 or "0" * 64,
            "status": status,
            "reward": reward,
            "error": error,
            "agent": agent_info,
            "verifier": verifier_info,
            "cleanup": cleanup,
            "started_at": started_at,
            "finished_at": self.now(),
            "aeo_version": __version__,
        }
        return out

    def _prepare_workspace(self, names: _Names, verifier_image_id: str, tar: bytes) -> None:
        kind = "workspace_setup_failed"
        self._op(
            ["volume", "create", *self._labels(names.run_id, "workspace"), names.volume],
            kind,
            "volume create",
        )
        # Never started: it exists only so `docker cp` can stream the tar into the volume.
        self._op(
            [
                "create",
                "--name",
                names.prep,
                *self._labels(names.run_id, "prep"),
                "--network",
                "none",
                "--log-driver",
                "none",
                "--pull",
                "never",
                "--mount",
                f"type=volume,src={names.volume},dst=/{PREP_MOUNT},volume-nocopy",
                verifier_image_id,
                "true",
            ],
            kind,
            "prep container create",
        )
        self._op(["cp", "-a", "-", f"{names.prep}:/"], kind, "asset copy", stdin=tar)
        self._op(["rm", "-f", "-v", names.prep], kind, "prep container remove")

    def _run_agent(
        self, spec: EnvironmentSpec, names: _Names, image_id: str, out: RunOutputs
    ) -> dict[str, Any]:
        role = spec.agent
        self._op(
            [
                "create",
                "--name",
                names.agent,
                *self._labels(names.run_id, "agent"),
                "--hostname",
                "aeo-agent",
                *_isolation_flags(role),
                "--mount",
                f"type=volume,src={names.volume},dst={WORKSPACE},volume-nocopy",
                image_id,
                *role.command,
            ],
            "agent_start_failed",
            "agent container create",
        )
        res = self.docker.run(
            ["start", "--attach", names.agent],
            timeout=role.timeout_seconds,
            max_stdout=AGENT_MAX_OUTPUT,
            max_stderr=AGENT_MAX_OUTPUT,
            on_timeout=self._kill(names.agent),
        )
        out.agent_stdout, out.agent_stderr = res.stdout, res.stderr
        state = self._state(names.agent)
        info = {
            "image": role.image,
            "image_id": image_id,
            "exit_code": _int_or_none(state.get("ExitCode")),
            "timed_out": res.timed_out,
            "oom_killed": bool(state.get("OOMKilled", False)),
            "duration_seconds": res.duration_seconds,
            "stdout_bytes": res.stdout_total,
            "stderr_bytes": res.stderr_total,
            "output_truncated": res.stdout_truncated or res.stderr_truncated,
        }
        if state.get("Error") or not _has_started(state):
            raise _RunFailure(
                "agent_start_failed",
                f"agent container did not start: {state.get('Error') or state.get('Status')}",
                agent_info=info,
            )
        if state.get("Status") != "exited":
            # e.g. the attach stream dropped while the agent kept running. Scoring a
            # workspace the agent is still writing would turn an infrastructure failure
            # into a reward.
            raise _RunFailure(
                "docker_error",
                f"agent container in state {state.get('Status')!r} after attach ended "
                f"(docker exit code {res.returncode})",
                agent_info=info,
            )
        if not res.timed_out and res.returncode != info["exit_code"]:
            # `docker start --attach` exits with the container's exit code. Any other code
            # means the client failed (e.g. 125 for a daemon/attach error), so aeo can not
            # vouch for what it observed.
            raise _RunFailure(
                "docker_error",
                f"docker start exited {res.returncode} but the agent exited "
                f"{info['exit_code']}: {_clean(res.stderr, 200)}",
                agent_info=info,
            )
        # The agent must be gone before anything trusted looks at the workspace.
        self._op(["rm", "-f", "-v", names.agent], "docker_error", "agent container remove")
        return info

    def _run_verifier(
        self, spec: EnvironmentSpec, names: _Names, image_id: str, out: RunOutputs
    ) -> tuple[dict[str, Any], VerifierVerdict]:
        role = spec.verifier
        res = self.docker.run(
            [
                "run",
                "--name",
                names.verifier,
                *self._labels(names.run_id, "verifier"),
                "--hostname",
                "aeo-verifier",
                *_isolation_flags(role),
                "--mount",
                f"type=volume,src={names.volume},dst={WORKSPACE},readonly,volume-nocopy",
                image_id,
                *role.command,
            ],
            timeout=role.timeout_seconds,
            max_stdout=MAX_VERIFIER_STDOUT_BYTES,
            max_stderr=VERIFIER_MAX_STDERR,
            on_timeout=self._kill(names.verifier),
        )
        out.verifier_stderr = res.stderr
        info: dict[str, Any] = {
            "image": role.image,
            "image_id": image_id,
            "exit_code": None,
            "timed_out": res.timed_out,
            "duration_seconds": res.duration_seconds,
            "details": None,
        }

        def fail(kind: str, message: str) -> _RunFailure:
            return _RunFailure(kind, message, verifier_info=info)

        if res.timed_out:
            raise fail("verifier_timeout", f"verifier exceeded {role.timeout_seconds}s")
        try:
            state = self._state(names.verifier)
        except _RunFailure as exc:
            raise fail(exc.kind, exc.message) from None
        info["exit_code"] = _int_or_none(state.get("ExitCode"))
        if state.get("Status") != "exited" or not _has_started(state):
            raise fail(
                "verifier_failed",
                f"verifier container in state {state.get('Status')!r} after run ended",
            )
        if state.get("Error") or info["exit_code"] != 0:
            raise fail("verifier_failed", f"verifier exited with code {info['exit_code']}")
        if res.returncode != 0:
            # The container exited 0 but the client did not: its stdout may be incomplete.
            raise fail("verifier_failed", f"docker run exited {res.returncode}")
        try:
            verdict = parse_verifier_output(res.stdout, truncated=res.stdout_truncated)
        except ValidationError as exc:
            raise fail("verifier_output_invalid", str(exc)) from None
        info["details"] = verdict.details
        return info, verdict

    def _cleanup(self, names: _Names) -> dict[str, Any]:
        errors: list[str] = []

        def attempt(args: list[str], what: str) -> None:
            try:
                res = self.docker.run(args, timeout=OP_TIMEOUT_S)
            except (OSError, KeyboardInterrupt) as exc:
                errors.append(f"{what}: {_clean(type(exc).__name__)}")
                return
            if res.returncode != 0 or res.timed_out:
                stderr = _clean(res.stderr, 200)
                if "No such" not in stderr and "no such" not in stderr:
                    errors.append(f"{what}: {stderr or 'timed out'}")

        for name in (names.prep, names.agent, names.verifier):
            attempt(["rm", "-f", "-v", name], f"remove container {name}")
        attempt(["volume", "rm", "-f", names.volume], f"remove volume {names.volume}")

        label = f"label=aeo.run_id={names.run_id}"
        for args, what in (
            (["ps", "-a", "-q", "--filter", label], "containers"),
            (["volume", "ls", "-q", "--filter", label], "volumes"),
        ):
            try:
                res = self.docker.run(args, timeout=OP_TIMEOUT_S)
            except (OSError, KeyboardInterrupt) as exc:
                errors.append(f"leak check ({what}): {_clean(type(exc).__name__)}")
                continue
            if res.returncode != 0:
                errors.append(f"leak check ({what}) failed: {_clean(res.stderr, 200)}")
            elif res.stdout.strip():
                errors.append(f"leftover {what}: {_clean(res.stdout, 200)}")
        return {"ok": not errors, "errors": errors[:20]}


def _has_started(state: dict[str, Any]) -> bool:
    started = state.get("StartedAt")
    return isinstance(started, str) and not started.startswith("0001-01-01")


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
