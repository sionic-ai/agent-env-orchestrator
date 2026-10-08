"""Host-side storage for run results: ``<runs_dir>/<run_id>/result.json`` plus bounded logs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from aeo.docker_backend import RunOutputs
from aeo.result import validate_result
from aeo.safefs import read_regular_file
from aeo.validation import load_json_bytes, validate_run_id

MAX_RESULT_BYTES = 256 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class RunStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def path(self, run_id: str) -> Path:
        return self.root / validate_run_id(run_id)

    def create(self, run_id: str) -> Path:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        run_dir = self.path(run_id)
        run_dir.mkdir(mode=0o700)  # raises FileExistsError: a run id is never reused
        os.chmod(run_dir, 0o700)
        return run_dir

    def _write(self, run_dir: Path, name: str, data: bytes) -> None:
        fd = os.open(run_dir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())

    def save(self, run_dir: Path, outputs: RunOutputs) -> None:
        self._write(run_dir, "agent.stdout.log", outputs.agent_stdout)
        self._write(run_dir, "agent.stderr.log", outputs.agent_stderr)
        self._write(run_dir, "verifier.stderr.log", outputs.verifier_stderr)
        body = json.dumps(outputs.result, indent=2, sort_keys=True, allow_nan=False) + "\n"
        # Written last and renamed into place, so result.json is either complete or absent.
        self._write(run_dir, "result.json.tmp", body.encode())
        os.replace(run_dir / "result.json.tmp", run_dir / "result.json")

    def load(self, run_id: str) -> dict[str, Any]:
        data = read_regular_file(
            self.path(run_id) / "result.json", max_bytes=MAX_RESULT_BYTES, what="result file"
        )
        return validate_result(load_json_bytes(data, max_bytes=MAX_RESULT_BYTES))
