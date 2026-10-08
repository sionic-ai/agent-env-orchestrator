# Development

## Commands

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev,parquet]'

ruff check . && ruff format --check .
pytest                        # unit tests (Docker not needed); "docker" tests are deselected
./scripts/build-demo-images.sh
pytest -m docker              # integration tests against the local Docker daemon
./scripts/docker-smoke.sh     # CLI smoke: build, catalog, validate, run, result, leak check
python -m build               # sdist + wheel
```

Test layout:

- `tests/unit/`: pure Python. The Docker backend is driven through a recording fake `docker`
  CLI (`FakeDocker`), so the exact argv (isolation flags, mounts, ordering), error mapping
  and cleanup are checked without a daemon.
- `tests/docker/` (`@pytest.mark.docker`): builds the demo images and runs real containers:
  happy path, wrong answer, crashing agent, hanging agent (timeout + kill), symlink attack on
  the verifier, in-container isolation probe, invalid/out-of-range/failing/hanging verifier,
  missing image, and the CLI end to end. Every test asserts that no container or volume
  labelled with its run ID is left behind.

## Recorded evidence

The output below is copied from real runs on 2026-10-08 on macOS (arm64), Docker Desktop
29.4.1 (linuxkit kernel), Python 3.11.10 unless noted. None of it is invented. Docker image
IDs and run IDs will differ on other machines.

### Test-first cycles (red → green)

Each module was written test-first. Its new test file was run and failed before the
implementation existed (collection error: module missing), and passed afterwards. The test
run also caught real bugs, noted below.

| cycle | test file | red | green |
| ----- | --------- | --- | ----- |
| 1 | `test_validation.py` | `1 error during collection` | first run: `1 failed, 34 passed`. Image refs like `UPPER/case` were accepted because the first component was parsed as a registry host. Fixed to match Docker's rule (host must contain `.`/`:` or be `localhost`), giving `35 passed` |
| 2 | `test_spec.py` | `1 error during collection` | `35 passed` |
| 3 | `test_assets.py` | `1 error during collection` | `14 passed` |
| 4 | `test_result.py` | `1 error during collection` | `53 passed` |
| 5 | `test_proc.py` | `1 error during collection` | `6 passed` |
| 6 | `test_docker_backend.py` | `1 error during collection` | `26 passed` |
| 7 | `test_cli.py` | `1 error during collection` | first run: `1 failed, 19 passed` (bug in the test helper's directory layout). After the fix, the whole unit suite gave `189 passed` |
| 8 | `tests/docker/test_docker_e2e.py` | `12 errors` (no demo env/images yet) | see below |
| 9 | `test_mimo.py` | `1 error during collection` | `24 passed` |
| 10 | image-ID pinning (added to `test_docker_backend.py`) | `3 failed, 23 passed` (containers were still created by tag) | `213 passed` unit + `12 passed` docker |
| 11 | independent code review findings (unit tests) | `9 failed, 215 passed in 65.01s` (the interrupt-during-timeout test hung for about 60 s, reproducing the bug) | `224 passed in 5.87s` |
| 12 | review findings (Docker tests: FIFO and `+N` answers) | `2 failed` | `14 passed` docker |
| 13 | second independent review (Codex), re-checked against the latest code (unit tests) | `47 failed, 197 passed, 14 deselected in 46.25s` (see below) | `249 passed, 17 deselected in 9.66s` |
| 14 | second review: image-declared `VOLUME` (Docker tests) | first run `1 failed, 2 passed`: a real-Docker bug in the new check (see below) | `17 passed` docker |

Cycle 8 needed three real fixes, all found by running against Docker:

1. `7 failed, 5 passed`: the verifier exited with code 2 (`can't open file
   '/opt/aeo-verifier/verify.py': Permission denied`). BuildKit applied `COPY --chmod=0444`
   to the parent directory it created. Fixed by copying `verify.py` (0555) first. This failure
   was itself reported correctly as `status: "error"`, `kind: "verifier_failed"`,
   `reward: null`.
2. `1 failed, 11 passed`: the isolation probe asserted that only `lo` exists. A fresh network
   namespace also lists down tunnel stubs (`gre0`, `sit0`, …). The probe now checks that only
   `lo` has `IFF_UP` and that there are no IPv4 routes.
3. `1 failed, 11 passed`: Docker's masked `/proc` paths look like bind mounts. The probe now
   classifies mounts by filesystem type. After that: `12 passed in 15.59s`.

Cycles 11–12 fixed the defects found by a separate review pass:

- `proc.run_bounded`: a Ctrl-C raised during timeout handling (inside the `except
  TimeoutExpired` clause) skipped the process-group kill, and closing a pipe that a reader
  thread still held could block forever. Timeout handling now runs outside the except
  clause, and pipes are only closed once their reader has finished. (Cycle 13 replaced the
  reader threads altogether; see below.)
- The backend ignored the container's final state. An agent still `running` after its
  attach stream dropped, or one that never left `created`, could be scored as a normal
  `completed` run. These are now `docker_error` / `agent_start_failed` with `reward: null`,
  and a verifier that has not `exited` is `verifier_failed`.
- The demo verifier blocked on a FIFO planted as `answer.txt`, so an agent could turn a
  failure into a null-reward timeout. It now opens with `O_NONBLOCK` and accepts only a
  canonical integer.
- JSON with integers longer than 4300 digits raised a bare `ValueError`. It is now a
  `ValidationError`.
- `aeo run` created the run directory only after the run. It now fails before running if
  `--runs-dir` is unusable, prints the result before saving it, and writes `result.json`
  atomically (temp file, fsync, rename).
- `aeo mimo import` with a missing mapping file gave a traceback. It is now exit 2.
- `cpus: 0.25` was rounded to `--cpus 0.2`. It is now passed through unchanged.

Cycles 13–14 handled a second, independent review (Codex). That review ran on an earlier
snapshot, so every finding was re-checked against the latest code before fixing. Of the 47
red unit tests in cycle 13, 30 were existing backend/CLI tests that failed only because the
test fake now answers `docker image inspect` in the new `{{.Id}} {{json .Config}}` format.
The other 17 were the new regression tests. Each of these reproduced its finding:

- **Descendant holding the pipes.** The client exits at once, but a grandchild keeps
  stdout/stderr open. With `timeout=0.5`, the call took `10.05 s` and reported
  `timed_out=False` (two 5 s thread joins; the reviewer measured 14.05 s on an older
  snapshot). `run_bounded` now pumps the pipes non-blockingly from one thread under a single
  deadline. On expiry it calls `on_timeout`, waits `kill_grace`, kills the process group, and
  abandons pipes still held by a descendant that left the group after at most 2 s. Now
  `elapsed=1.00s timed_out=True`. A setsid'd descendant is also bounded.
- **Attach failure scored as a run.** The exact reviewer scenario (`created` state, empty
  `Error`, default `ExitCode` 0, attach client exit 1) was already `agent_start_failed`
  in the latest code, and is now pinned by a test. Still open was a container that `exited`
  while `docker start --attach` exited differently (e.g. `125`). That run was `completed`
  with a reward. It is now `docker_error`. A verifier whose `docker run` client exits
  non-zero is `verifier_failed`.
- **Image-declared `VOLUME`.** Images that declare volumes are now refused
  (`image_unsupported`) before any volume or container is created. Containers are removed
  with `rm -f -v`.
- **FIFO as host input.** The demo verifier already used `O_NONBLOCK`, but
  `safefs.read_regular_file` did not. `aeo validate` on a FIFO `environment.json` hung, and
  the test's 20 s guard fired. Host files now go through `open_regular_file`
  (`O_NOFOLLOW | O_NONBLOCK`, then `fstat` must show a regular file). The MiMo JSONL reader
  uses it too.
- **`cpus: 10**400`** raised `OverflowError` in `math.isfinite`. The range check now runs
  first and compares exactly, so it is a `ValidationError` (exit 2).
- **MiMo catalog with `entries: [null]`** passed `load_catalog` and crashed listing with
  `AttributeError`. Every entry is now checked: object, safe `instance_id`,
  `executable: false`, string-or-null fields. Errors are exit 2.

Cycle 14 found a real-Docker bug that the fake had hidden. On Docker 29.4.1 (containerd
image store), `{{json .Config.Volumes}}` is a template error (`map has no entry for key
"Volumes"`) for every image *without* volumes. As a result, the demo images were reported as
unavailable. aeo now reads `{{json .Config}}` and fails closed (`image_unsupported`) if the
config cannot be parsed. The Docker tests also show Docker's own behaviour directly
(`test_docker_rm_without_v_leaks_image_declared_volume`): plain `rm -f` leaves the anonymous
`/scratch` volume behind, `rm -f -v` removes it, and `-v` keeps the named workspace volume.
A run with the image check bypassed still leaves no volume behind.

### Final suite

All checks after the last change (re-run for cycles 13–14 on 2026-10-08; raw output in
`/tmp/aeo-fix-red.txt` and `/tmp/aeo-fix-green.txt` on the dev machine):

```
$ ruff check . && ruff format --check .
All checks passed!
38 files already formatted
$ pytest            # Python 3.11.10
249 passed, 17 deselected in 9.66s
$ pytest            # Python 3.12 (uv, offline: pyarrow not installed)
248 passed, 1 skipped, 17 deselected in 9.71s
$ pytest            # Python 3.13 (uv, offline: pyarrow not installed)
248 passed, 1 skipped, 17 deselected in 9.71s
$ pytest -m docker  # Docker Desktop 29.4.1
17 passed in 15.31s
$ ./scripts/docker-smoke.sh
smoke: run aeo-a2df701c1a8902b8 completed with reward 1.0
smoke: OK (no leftover containers or volumes)
$ python -m build
Successfully built agent_env_orchestrator-0.1.0.tar.gz and agent_env_orchestrator-0.1.0-py3-none-any.whl
$ terraform fmt -check -recursive deploy/terraform && terraform validate   # module unchanged
Success! The configuration is valid.
$ docker ps -aq / volume ls -q --filter label=aeo.managed=true | wc -l
0 containers, 0 volumes
```

### Demo runs through the CLI

```
$ aeo catalog
cpu-sum-demo                     Synthetic CPU task: sum integers from numbers.txt into answer.txt. Solved by a deterministic fixture agent (not an AI model).
$ aeo validate environments/cpu-sum-demo
OK cpu-sum-demo: spec valid, 2 asset file(s), sha256 069e09d91a09
```

Four runs (the demo, plus copies with the agent or verifier command changed):

| variant | exit | status | reward | error | verifier details | cleanup |
| ------- | ---- | ------ | ------ | ----- | ---------------- | ------- |
| fixture `solve` | 0 | completed | 1.0 | null | `reason: correct` | ok |
| fixture `wrong` | 0 | completed | 0.0 | null | `reason: wrong_answer` | ok |
| verifier prints `{"reward": NaN}` | 1 | error | null | `verifier_output_invalid: JSON constant NaN is not allowed` | null | ok |
| fixture `probe` | 0 | completed | 1.0 | null | `reason: correct` | ok |

Afterwards, `docker ps -aq --filter label=aeo.managed=true` and
`docker volume ls -q --filter label=aeo.managed=true` both returned nothing.

The `probe` agent's report from inside the agent container (`runs/<id>/agent.stdout.log`):

```json
{"cap_eff": "0000000000000000", "docker_socket_present": false,
 "env_keys": ["GPG_KEY", "HOME", "HOSTNAME", "LANG", "PATH", "PYTHONDONTWRITEBYTECODE",
              "PYTHONUNBUFFERED", "PYTHON_SHA256", "PYTHON_VERSION"],
 "gid": 10001, "interfaces_up": ["lo"], "ipv4_routes": 0, "memory_max": "536870912",
 "no_new_privs": "1", "pids_max": "128", "rootfs_write": "error: OSError",
 "storage_mounts": [["/etc/hostname", "/docker/containers/<id>/hostname"],
                    ["/etc/hosts", "/docker/containers/<id>/hosts"],
                    ["/etc/resolv.conf", "/docker/containers/<id>/resolv.conf"],
                    ["/workspace", "/docker/volumes/aeo-00484c713bee2e60-ws/_data"]],
 "tcp_connect": "error: OSError", "tmp_exec": "error: PermissionError", "uid": 10001,
 "verifier_code_visible": false, "workspace_write": "ok"}
```

`scripts/docker-smoke.sh` (local run):

```
smoke: run aeo-b98456142191251c completed with reward 1.0
smoke: OK (no leftover containers or volumes)
```

### Packaging and Python versions

- `python -m build` produced `agent_env_orchestrator-0.1.0.tar.gz` and
  `agent_env_orchestrator-0.1.0-py3-none-any.whl` (the wheel contains `aeo/*` and
  `LICENSE`, `NOTICE`). The wheel installed into a clean Python 3.13 venv. `aeo --version`
  printed `aeo 0.1.0`, and `aeo validate environments/cpu-sum-demo` succeeded.
- Unit suite on Python 3.12 and 3.13: see the final suite above.

### Terraform / Kubernetes

Terraform 1.16.5 (official darwin_arm64 zip, SHA-256 checked against HashiCorp's
`SHA256SUMS`):

- `terraform fmt -check -recursive deploy/terraform` passed.
- In `deploy/terraform/examples/existing-cluster`: `terraform init -backend=false` and
  `terraform validate` returned `Success! The configuration is valid.`
- `terraform providers lock -platform=linux_amd64 -platform=linux_arm64
  -platform=darwin_amd64 -platform=darwin_arm64` produced the committed `.terraform.lock.hcl`.

The module was applied to a throwaway **kind v0.33.0** cluster (Kubernetes v1.37.0), using a
scratch copy and a separate kubeconfig:

- With kind's default CNI (kindnet), `apply` created all 5 resources. However, a pod in
  `aeo-envs` could still reach the API server, a CoreDNS pod and DNS, so **kindnet did not
  enforce NetworkPolicy**.
- Recreated with `disableDefaultCNI` and **Calico v3.33.0**:

  ```
  Apply complete! Resources: 5 added, 0 changed, 0 destroyed.
  -- aeo-envs/probe:            -- default/probe (control):
  api_tcp=blocked TimeoutError   api_tcp=ok
  pod_tcp=blocked TimeoutError   pod_tcp=ok
  dns=blocked gaierror           dns=ok
  sa_token_mounted= False        sa_token_mounted= True
  ```

  A privileged pod was rejected: `violates PodSecurity "restricted:latest"`. A pod with
  `nvidia.com/gpu: 1` was rejected: `exceeded quota: aeo-quota, requested:
  requests.nvidia.com/gpu=1 … limited: requests.nvidia.com/gpu=0`. The LimitRange defaults
  were applied (`limits cpu 1 / memory 512Mi`, `requests 250m / 256Mi`).
  `terraform destroy` then gave `Destroy complete! Resources: 5 destroyed.`, and the cluster
  was deleted.

### Not verified here

- The GitHub Actions workflow (`.github/workflows/ci.yml`) has not run yet: nothing has been
  pushed. Its steps mirror the local commands above.
- Docker Engine on native Linux was not run locally. The `docker-smoke` CI job covers it once
  CI runs.
