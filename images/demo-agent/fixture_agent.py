"""Deterministic FIXTURE agent for the cpu-sum-demo environment.

This is NOT an AI model and makes no model or network requests. It exists so the
orchestration path (workspace, isolation, verifier, reward, cleanup) can be exercised
end to end without any external dependency. The mode is the first argument:

  solve    write the correct sum to answer.txt
  wrong    write an incorrect sum
  crash    exit with status 3 without writing an answer
  hang     sleep far beyond any sane timeout
  symlink  make answer.txt a symlink into the verifier image's private files
  fifo     make answer.txt a FIFO, trying to stall the verifier into a timeout
  plus     write the right sum in a non-canonical form ("+N")
  probe    print a JSON report of the sandbox as seen from inside, then solve
"""

import json
import os
import socket
import subprocess
import sys
import time

WORKSPACE = "/workspace"


def numbers_sum() -> int:
    with open(os.path.join(WORKSPACE, "numbers.txt"), encoding="ascii") as fh:
        return sum(int(line) for line in fh if line.strip())


def write_answer(value: int) -> None:
    with open(os.path.join(WORKSPACE, "answer.txt"), "w", encoding="ascii") as fh:
        fh.write(f"{value}\n")


def attempt(fn) -> str:
    try:
        fn()
    except Exception as exc:  # report, do not crash the probe
        return f"error: {type(exc).__name__}"
    return "ok"


def read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:
        return None


def status_field(name: str) -> str | None:
    for line in (read("/proc/self/status") or "").splitlines():
        if line.startswith(name + ":"):
            return line.split(":", 1)[1].strip()
    return None


def interfaces_up() -> list[str]:
    """Interfaces with IFF_UP set. A fresh netns also lists down tunnel stubs (gre0, ...)."""
    up = []
    for name in sorted(os.listdir("/sys/class/net")):
        flags = read(f"/sys/class/net/{name}/flags")
        if flags is not None and int(flags, 16) & 0x1:
            up.append(name)
    return up


PSEUDO_FS = {"proc", "sysfs", "tmpfs", "devpts", "mqueue", "cgroup", "cgroup2", "overlay"}


def storage_mounts() -> list[list[str]]:
    """[mount point, source root] for every mount backed by real storage (not pseudo fs)."""
    found = []
    for line in (read("/proc/self/mountinfo") or "").splitlines():
        pre, _, post = line.partition(" - ")
        fields = pre.split()
        if post.split()[0] not in PSEUDO_FS:
            found.append([fields[4], fields[3]])
    return sorted(found)


def probe() -> dict:
    def connect():
        with socket.create_connection(("1.1.1.1", 53), timeout=2):
            pass

    def write_rootfs():
        with open("/aeo-probe", "w") as fh:
            fh.write("x")

    def exec_tmp():
        path = "/tmp/aeo-probe.sh"
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)
        subprocess.run([path], check=True)  # PermissionError on a noexec /tmp

    def write_workspace():
        with open(os.path.join(WORKSPACE, ".probe"), "w") as fh:
            fh.write("x")
        os.unlink(os.path.join(WORKSPACE, ".probe"))

    return {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "interfaces_up": interfaces_up(),
        "ipv4_routes": len((read("/proc/net/route") or "").splitlines()[1:]),
        "tcp_connect": attempt(connect),
        "docker_socket_present": os.path.exists("/var/run/docker.sock"),
        "verifier_code_visible": os.path.exists("/opt/aeo-verifier"),
        "cap_eff": status_field("CapEff"),
        "no_new_privs": status_field("NoNewPrivs"),
        "rootfs_write": attempt(write_rootfs),
        "tmp_exec": attempt(exec_tmp),
        "workspace_write": attempt(write_workspace),
        "memory_max": read("/sys/fs/cgroup/memory.max"),
        "pids_max": read("/sys/fs/cgroup/pids.max"),
        "env_keys": sorted(os.environ),
        "storage_mounts": storage_mounts(),
    }


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "solve"
    if mode == "solve":
        write_answer(numbers_sum())
    elif mode == "wrong":
        write_answer(numbers_sum() + 1)
    elif mode == "crash":
        print("fixture agent: simulated crash", file=sys.stderr)
        return 3
    elif mode == "hang":
        time.sleep(86400)
    elif mode == "symlink":
        os.symlink("/opt/aeo-verifier/expected.json", os.path.join(WORKSPACE, "answer.txt"))
    elif mode == "fifo":
        os.mkfifo(os.path.join(WORKSPACE, "answer.txt"))
    elif mode == "plus":
        with open(os.path.join(WORKSPACE, "answer.txt"), "w", encoding="ascii") as fh:
            fh.write(f"+{numbers_sum()}\n")
    elif mode == "probe":
        report = probe()
        write_answer(numbers_sum())
        print(json.dumps(report, sort_keys=True))
    else:
        print(f"fixture agent: unknown mode {mode!r}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
