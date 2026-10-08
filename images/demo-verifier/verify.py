"""Trusted verifier for the cpu-sum-demo environment.

Runs in its own container after the agent container has been removed, with the workspace
mounted read-only. The expected answer lives only in this image. Prints exactly one JSON
object on stdout: {"reward": <0.0..1.0>, "details": {...}}; never echoes workspace content.
"""

import json
import os
import re
import stat
import sys

ANSWER = "/workspace/answer.txt"
EXPECTED = "/opt/aeo-verifier/expected.json"
MAX_ANSWER_BYTES = 64
CANONICAL_INT = re.compile(rb"-?(0|[1-9][0-9]*)\n?")


def verdict(reward: float, reason: str) -> int:
    print(json.dumps({"reward": reward, "details": {"reason": reason}}))
    return 0


def main() -> int:
    with open(EXPECTED, encoding="ascii") as fh:
        expected = json.load(fh)["sum"]
    try:
        # O_NONBLOCK: opening a FIFO planted by the agent must not stall the verifier into
        # a timeout (which would turn a failed task into a null-reward error).
        fd = os.open(ANSWER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return verdict(0.0, "missing_answer")
    except OSError:
        # ELOOP for a symlink, EACCES for an unreadable file, etc.
        return verdict(0.0, "answer_not_regular_file")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return verdict(0.0, "answer_not_regular_file")
        data = os.read(fd, MAX_ANSWER_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_ANSWER_BYTES:
        return verdict(0.0, "answer_too_large")
    if not CANONICAL_INT.fullmatch(data):
        return verdict(0.0, "answer_not_an_integer")
    value = int(data)
    return verdict(1.0, "correct") if value == expected else verdict(0.0, "wrong_answer")


if __name__ == "__main__":
    sys.exit(main())
