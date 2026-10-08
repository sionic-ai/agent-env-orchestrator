"""Run a subprocess with ``shell=False``, a hard timeout and bounded output capture."""

from __future__ import annotations

import contextlib
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import IO


@dataclass(frozen=True)
class ProcResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    stdout_total: int
    stderr_total: int
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    duration_seconds: float


# After the client's process group is SIGKILLed, a descendant that left the group (setsid)
# may still hold the pipes open. Wait at most this long for EOF before abandoning them.
_POST_KILL_DRAIN_S = 2.0


class _Sink:
    """Count everything read from one pipe, keeping only the first ``limit`` bytes."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.kept = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self.limit - len(self.kept)
        if room > 0:
            self.kept += chunk[:room]


class _Pump:
    """Single-threaded, non-blocking pump for the child's stdin/stdout/stderr pipes.

    Nothing here ever blocks on a pipe, so a descendant that inherited the pipes and keeps
    them open can not stretch a call past its deadline, and closing our ends never waits.
    """

    def __init__(self, proc: subprocess.Popen[bytes], sinks: dict[int, _Sink], stdin: bytes):
        self.sel = selectors.DefaultSelector()
        self.sinks = sinks
        self.files: list[IO[bytes]] = []
        for stream in (proc.stdout, proc.stderr):
            assert stream is not None  # noqa: S101 - PIPE given
            fd = stream.fileno()
            os.set_blocking(fd, False)
            self.sel.register(fd, selectors.EVENT_READ)
            self.files.append(stream)
        self.pending = memoryview(stdin)
        if proc.stdin is not None:
            fd = proc.stdin.fileno()
            os.set_blocking(fd, False)
            self.files.append(proc.stdin)
            if self.pending:
                self.sel.register(fd, selectors.EVENT_WRITE)
            else:
                self._close(proc.stdin)

    def _close(self, stream: IO[bytes]) -> None:
        with contextlib.suppress(KeyError, ValueError, OSError):
            self.sel.unregister(stream.fileno())
        with contextlib.suppress(OSError, ValueError):
            stream.close()

    def _stream(self, fd: int) -> IO[bytes]:
        return next(f for f in self.files if not f.closed and f.fileno() == fd)

    @property
    def readers_open(self) -> bool:
        return any(key.events & selectors.EVENT_READ for key in self.sel.get_map().values())

    def pump(self, timeout: float) -> None:
        """Move data for at most ``timeout`` seconds (returns early once a pipe is ready)."""
        if not self.sel.get_map():
            time.sleep(max(0.0, min(timeout, 0.05)))
            return
        for key, _ in self.sel.select(max(0.0, timeout)):
            fd = key.fd
            if key.events & selectors.EVENT_WRITE:
                try:
                    written = os.write(fd, self.pending[: 1 << 16])
                except BlockingIOError:
                    continue
                except OSError:  # EPIPE: the child stopped reading
                    written = len(self.pending)
                self.pending = self.pending[written:]
                if not self.pending:
                    self._close(self._stream(fd))
                continue
            try:
                chunk = os.read(fd, 1 << 16)
            except BlockingIOError:
                continue
            except OSError:
                chunk = b""
            if chunk:
                self.sinks[fd].add(chunk)
            else:
                self._close(self._stream(fd))

    def close(self) -> None:
        for stream in self.files:
            self._close(stream)
        self.sel.close()


def run_bounded(
    argv: Sequence[str],
    *,
    timeout: float,
    max_stdout: int,
    max_stderr: int,
    stdin: bytes | None = None,
    env: Mapping[str, str] | None = None,
    on_timeout: Callable[[], None] | None = None,
    kill_grace: float = 10.0,
) -> ProcResult:
    """Run ``argv`` (never through a shell).

    The call finishes when the client has exited *and* its output pipes reached EOF. If that
    has not happened within ``timeout`` seconds (including the case where the client exited
    but a descendant still holds its pipes), the run is timed out: ``on_timeout`` is called
    (e.g. ``docker kill`` for the container the client is attached to), the client gets
    ``kill_grace`` seconds to finish, then its whole process group is SIGKILLed and the pipes
    are abandoned after a short bounded drain. Output beyond the limits is counted, not kept.
    """
    start = time.monotonic()
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        close_fds=True,
        env=dict(env) if env is not None else None,
        start_new_session=True,
    )
    assert proc.stdout is not None and proc.stderr is not None  # noqa: S101 - PIPE given
    out, err = _Sink(max_stdout), _Sink(max_stderr)
    sinks = {proc.stdout.fileno(): out, proc.stderr.fileno(): err}
    pump: _Pump | None = None
    timed_out = False
    try:
        pump = _Pump(proc, sinks, stdin or b"")

        def run_until(deadline: float) -> bool:
            """Pump until the client exited and its pipes closed; False if the deadline hit."""
            while pump.readers_open or proc.poll() is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                pump.pump(min(left, 0.1))
            return True

        if not run_until(start + timeout):
            timed_out = True
            if on_timeout is not None:
                on_timeout()
            if not run_until(time.monotonic() + kill_grace):
                _kill_group(proc)
                run_until(time.monotonic() + _POST_KILL_DRAIN_S)
    except BaseException:
        _kill_group(proc)
        raise
    finally:
        if pump is not None:
            pump.close()
        else:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
        if proc.poll() is None:
            _kill_group(proc)
        proc.wait()

    return ProcResult(
        returncode=proc.returncode,
        stdout=bytes(out.kept),
        stderr=bytes(err.kept),
        stdout_total=out.total,
        stderr_total=err.total,
        stdout_truncated=out.total > max_stdout,
        stderr_truncated=err.total > max_stderr,
        timed_out=timed_out,
        duration_seconds=round(time.monotonic() - start, 3),
    )


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
