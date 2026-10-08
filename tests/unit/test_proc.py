import os
import sys
import time

import pytest

from aeo.proc import run_bounded

PY = sys.executable


def test_captures_stdout_stderr_and_exit_code():
    res = run_bounded(
        [PY, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
        timeout=10,
        max_stdout=100,
        max_stderr=100,
    )
    assert res.returncode == 3
    assert res.stdout == b"out\n"
    assert res.stderr == b"err\n"
    assert not res.timed_out
    assert not res.stdout_truncated


def test_output_is_bounded_but_fully_drained():
    # 5 MB of output must not block the child nor be held in memory.
    res = run_bounded(
        [PY, "-c", "import sys; sys.stdout.write('x' * 5_000_000)"],
        timeout=30,
        max_stdout=1000,
        max_stderr=10,
    )
    assert res.returncode == 0
    assert len(res.stdout) == 1000
    assert res.stdout_truncated
    assert res.stdout_total == 5_000_000


def test_timeout_calls_hook_and_kills():
    calls = []
    start = time.monotonic()
    res = run_bounded(
        [PY, "-c", "import time; print('started', flush=True); time.sleep(60)"],
        timeout=1,
        max_stdout=100,
        max_stderr=100,
        on_timeout=lambda: calls.append("hook"),
        kill_grace=2,
    )
    assert res.timed_out
    assert calls == ["hook"]
    assert time.monotonic() - start < 10
    assert res.stdout == b"started\n"


def test_stdin_is_fed_and_argv_is_not_shell_interpreted(tmp_path):
    marker = tmp_path / "pwned"
    res = run_bounded(
        [PY, "-c", "import sys; print(sys.stdin.read()); print(sys.argv[1])", f"; touch {marker}"],
        timeout=10,
        max_stdout=1000,
        max_stderr=1000,
        stdin=b"hello",
    )
    assert res.stdout.splitlines() == [b"hello", f"; touch {marker}".encode()]
    assert not marker.exists()


def test_env_is_exactly_what_was_passed():
    res = run_bounded(
        [PY, "-c", "import os; print(sorted(k for k in os.environ if k.startswith('AEO_T')))"],
        timeout=10,
        max_stdout=1000,
        max_stderr=1000,
        env={"AEO_TEST_ONLY": "1"},
    )
    assert res.stdout.strip() == b"['AEO_TEST_ONLY']"


def test_missing_executable_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        run_bounded(["/nonexistent/aeo-binary"], timeout=1, max_stdout=1, max_stderr=1)


def test_interrupt_during_timeout_handling_kills_child_and_returns_promptly():
    def interrupted_hook():
        raise KeyboardInterrupt

    start = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        run_bounded(
            [PY, "-c", "import time; print('x', flush=True); time.sleep(60)"],
            timeout=1,
            max_stdout=100,
            max_stderr=100,
            on_timeout=interrupted_hook,
            kill_grace=30,
        )
    assert time.monotonic() - start < 15


def test_interrupt_while_waiting_kills_child(tmp_path):
    import signal
    import threading

    pidfile = tmp_path / "pid"
    timer = threading.Timer(1.0, lambda: signal.raise_signal(signal.SIGINT))
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            run_bounded(
                [
                    PY,
                    "-c",
                    "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid()));"
                    " time.sleep(60)",
                    str(pidfile),
                ],
                timeout=30,
                max_stdout=100,
                max_stderr=100,
            )
    finally:
        timer.cancel()
    pid = int(pidfile.read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


DESCENDANT_HOLDS_PIPES = (
    # The direct child exits at once; a grandchild inherits stdout/stderr and keeps them open.
    "import subprocess, sys;"
    " subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']);"
    " print('parent done', flush=True)"
)


def test_descendant_holding_pipes_can_not_outlive_the_deadline():
    start = time.monotonic()
    res = run_bounded(
        [PY, "-c", DESCENDANT_HOLDS_PIPES],
        timeout=0.5,
        max_stdout=100,
        max_stderr=100,
        kill_grace=0.5,
    )
    elapsed = time.monotonic() - start
    assert elapsed < 4, elapsed
    assert res.timed_out
    assert res.stdout == b"parent done\n"


def test_descendant_that_escapes_the_process_group_is_still_bounded(tmp_path):
    pidfile = tmp_path / "pid"
    code = (
        "import subprocess, sys;"
        " p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
        " start_new_session=True);"
        f" open({str(pidfile)!r}, 'w').write(str(p.pid))"
    )
    start = time.monotonic()
    res = run_bounded([PY, "-c", code], timeout=0.5, max_stdout=100, max_stderr=100, kill_grace=0.5)
    assert time.monotonic() - start < 8
    assert res.timed_out
    os.kill(int(pidfile.read_text()), 9)  # outside our group: the test cleans it up itself


def test_large_stdin_and_output_do_not_deadlock():
    data = b"y" * 3_000_000
    res = run_bounded(
        [PY, "-c", "import sys; d = sys.stdin.buffer.read(); sys.stdout.buffer.write(d)"],
        timeout=30,
        max_stdout=10,
        max_stderr=10,
        stdin=data,
    )
    assert res.returncode == 0
    assert res.stdout_total == len(data)
    assert not res.timed_out
