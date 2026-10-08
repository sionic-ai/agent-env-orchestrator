# Contributing

Thanks for helping! This project is an early MVP. Small, well-tested changes are the
easiest to review.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev,parquet]'
```

Tool versions are pinned in `pyproject.toml` (`pytest`, `ruff`, `build`).

## Checks

```bash
ruff check . && ruff format --check .
pytest                       # unit tests, no Docker needed
./scripts/build-demo-images.sh
pytest -m docker             # real Docker integration tests
./scripts/docker-smoke.sh    # CLI smoke run
terraform fmt -check -recursive deploy/terraform   # if you touch Terraform
```

CI runs all of these on Ubuntu (see `.github/workflows/ci.yml`).

## Guidelines

- **Test first.** Add a failing test, then make it pass. Backend changes need both a
  fake-Docker unit test (`tests/unit/test_docker_backend.py`) for the exact argv/ordering and,
  where behaviour depends on Docker, a `docker`-marked test in `tests/docker/`.
- **No privilege knobs in specs.** Isolation flags are fixed in `aeo.docker_backend`. Do not
  add spec fields that relax them (`privileged`, extra mounts, capabilities, host
  networking, env passthrough, …).
- **Keep reward semantics.** Infrastructure or verifier failures must produce
  `status: "error"` with `reward: null`, never `0.0`.
- **Bound everything** that comes from outside: sizes, counts, timeouts, string lengths.
- **No `shell=True`**, and never put untrusted output into logs or results without
  bounding and sanitising it.
- **No runtime dependencies** without discussion. The core uses only the standard library.
- **Be honest in docs.** Only describe something as supported if it is implemented and
  tested; mark everything else as planned in `docs/compatibility.md`.
- Do not commit datasets, credentials, internal hostnames/IPs, or `runs/` output.

By contributing, you agree that your contributions are licensed under the Apache License 2.0.
