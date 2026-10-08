# agent-env-orchestrator

Orchestrate isolated agent environments for evaluation and reinforcement learning.

`aeo` runs one **agent** container against a task workspace, then scores the result with a
separate, **trusted verifier** container, and writes a structured result with a reward in
`[0, 1]`. Everything runs locally on a Docker daemon, CPU-only.

> **Status: early MVP (0.1.0).** What works today is a single-host, local-Docker, CPU-only
> vertical slice. It is **not** a VM-grade sandbox for hostile code, **not** a hosted or
> multi-tenant service, and it contains **no model, trainer or GPU support**. See
> [docs/compatibility.md](docs/compatibility.md) for what is implemented and what is planned.

## What you get

- `aeo` CLI: `catalog`, `validate`, `run`, `result`, and `mimo import`.
- A versioned environment spec (`aeo.environment/v1`) and result contract (`aeo.result/v1`),
  both strictly validated. Unknown fields are rejected, so a spec cannot ask for privileges.
- A Docker backend that, for every run:
  - creates uniquely named, labelled containers and a run-owned volume;
  - fills the workspace from validated asset files by streaming a tar archive into the volume
    (no host bind mounts);
  - runs the agent with **no network**, all capabilities dropped, `no-new-privileges`, a
    read-only root filesystem, a fixed non-root uid, and CPU/memory/PID limits;
  - removes the agent container, **then** runs the verifier with the workspace mounted
    **read-only**; the verifier's code and answers live only in the verifier image;
  - always removes containers and the volume (on success, failure, timeout or Ctrl-C), then
    checks by label that nothing was left behind.
- Reward rules that keep "task failed" and "infrastructure failed" apart: a valid score of
  `0.0` is `status: "completed"`; Docker or verifier failures are `status: "error"` with
  `reward: null`. Booleans, NaN, ±Infinity and values outside `[0, 1]` are never accepted
  as rewards.
- A demo environment (`environments/cpu-sum-demo`) with a **deterministic fixture agent**.
  The fixture agent is a short Python script, not an AI model, so the full pipeline can be
  exercised with no model and no network.
- A first-pass **MiMo** adapter that turns local MiMo rows plus an image mapping into catalog
  metadata. It only records metadata: running MiMo tasks is explicitly unsupported.
- An optional Terraform module that adds guard rails to a namespace in an **existing**
  Kubernetes cluster. aeo does not schedule runs on Kubernetes yet.

## Quickstart

Requirements: Python 3.11+, Docker (Docker Engine on Linux or Docker Desktop), `bash`.

```bash
git clone https://github.com/sionic-ai/agent-env-orchestrator.git
cd agent-env-orchestrator
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

# Build the two local demo images (agent fixture + trusted verifier).
./scripts/build-demo-images.sh

aeo catalog                                # list environments under ./environments
aeo validate environments/cpu-sum-demo     # validate the spec and its asset tree
aeo run environments/cpu-sum-demo          # run once; prints the result JSON
aeo result <run_id>                        # show a stored result again
```

`aeo run` prints the result and stores it in `runs/<run_id>/result.json`, along with
size-limited agent stdout/stderr and verifier stderr logs (directory mode `0700`, files
`0600`). Real output from a run of the demo:

```json
{
  "aeo_version": "0.1.0",
  "agent": {
    "duration_seconds": 0.184,
    "exit_code": 0,
    "image": "aeo-demo-agent:0.1.0",
    "image_id": "sha256:b491689172753d730c2c058800e311fea725f66f76a772e74aa56f137d7684e0",
    "oom_killed": false,
    "output_truncated": false,
    "stderr_bytes": 0,
    "stdout_bytes": 0,
    "timed_out": false
  },
  "cleanup": { "errors": [], "ok": true },
  "environment_id": "cpu-sum-demo",
  "error": null,
  "finished_at": "2026-10-08T14:17:31.419681Z",
  "reward": 1.0,
  "run_id": "aeo-24e14762e25b5bfe",
  "schema_version": "aeo.result/v1",
  "spec_sha256": "069e09d91a0967a1d6d9589cfd9c47730a108795afbb4a4ec3d728db1ab9e73b",
  "started_at": "2026-10-08T14:17:30.513751Z",
  "status": "completed",
  "verifier": {
    "details": { "reason": "correct" },
    "duration_seconds": 0.184,
    "exit_code": 0,
    "image": "aeo-demo-verifier:0.1.0",
    "image_id": "sha256:5ecea2b8e698534572a14951aab5439ebfbc0ab15aedb5fdbc0db9cdfe0f19c1",
    "timed_out": false
  }
}
```

To see other outcomes, copy the environment and change the fixture agent's mode
(`solve`, `wrong`, `crash`, `hang`, `symlink`, `fifo`, `plus`, `probe`) in `agent.command`. For example,
`wrong` gives `"status": "completed", "reward": 0.0`. A verifier that prints
`{"reward": NaN}` gives `"status": "error", "reward": null` with
`error.kind = "verifier_output_invalid"`.

### Exit codes

| code | meaning |
| ---- | ------- |
| 0 | run completed with a valid reward (including `0.0`), or command succeeded |
| 1 | run finished with `status: "error"`, or cleanup could not confirm removal |
| 2 | invalid input (spec, assets, run id, result file, MiMo files) |
| 3 | recognised but unsupported input (e.g. `aeo run` on a MiMo catalog) |
| 4 | Docker CLI or daemon unavailable |
| 130 | interrupted (resources are still cleaned up) |

## Environment spec

```json
{
  "schema_version": "aeo.environment/v1",
  "id": "cpu-sum-demo",
  "description": "…",
  "assets": "assets",
  "network": "none",
  "agent": {
    "image": "aeo-demo-agent:0.1.0",
    "command": ["python3", "/opt/aeo-agent/fixture_agent.py", "solve"],
    "timeout_seconds": 60,
    "resources": {"cpus": 1.0, "memory_mb": 512, "pids": 128}
  },
  "verifier": {
    "image": "aeo-demo-verifier:0.1.0",
    "command": ["python3", "/opt/aeo-verifier/verify.py"],
    "timeout_seconds": 30
  }
}
```

| field | rules |
| ----- | ----- |
| `id` | `^[a-z0-9][a-z0-9._-]{0,63}$` |
| `assets` | optional relative directory inside the environment dir; no `..`, no symlinks |
| `network` | only `"none"` is accepted |
| `*.image` | a plain local Docker image reference; aeo **never pulls** (`--pull never`). Images with a `VOLUME` instruction are refused |
| `*.command` | JSON array of 1–64 strings (≤ 4096 chars, no NUL); never run through a shell |
| `agent.timeout_seconds` | integer 1–3600 |
| `verifier.timeout_seconds` | integer 1–600 |
| `resources` | `cpus` 0.1–8, `memory_mb` 64–16384 (swap disabled), `pids` 16–4096 |

Asset trees are limited to 1000 entries, 16 MiB per file, 64 MiB in total and a depth of 16.
Names must match `[A-Za-z0-9._-]`. Symlinks, hard links, FIFOs, sockets and devices are refused.

### Verifier contract

The verifier reads `/workspace` (read-only) and prints **exactly one** JSON object on stdout,
at most 16 KiB:

```json
{"reward": 0.0, "details": {"reason": "wrong_answer"}}
```

`details` is optional, flat, and limited to 32 keys. Each value is a string (≤ 256 chars), a
finite number, a boolean, or null. Anything else, a non-zero exit code, or a timeout makes the
run `status: "error"` with `reward: null`.

Verifiers read agent-controlled files, so they must not trust them. The demo verifier opens
the answer with `O_NOFOLLOW | O_NONBLOCK`, requires a regular file, bounds the read, and
accepts only a canonical integer. As a result, a symlink, a FIFO (which would otherwise stall
the verifier into a null-reward timeout) or `+1741` all score `0.0` rather than an error or a
false pass.

## MiMo metadata adapter

```bash
pip install -e '.[parquet]'   # only needed for .parquet input
aeo mimo import --rows data/rows.parquet --mapping data/image_mapping.jsonl --out data/mimo-catalog.json
aeo catalog --mimo data/mimo-catalog.json
```

Each row's `extra_info.instance_json` is a JSON string with `docker_image`, `cwd`,
`instance_id` and `dataset_type`. The mapping is JSONL with
`{"dataset_image": ..., "dockerhub_image": ...}`. For Parquet input, only the `extra_info`
column is read, so prompts and solutions are never copied. Every entry is
`"executable": false` and lists the reason. Entries without a `docker_image` (e.g. music)
say so. `aeo run` on a MiMo catalog exits with code 3. Catalog files are re-validated
when loaded, so a hand-edited catalog with malformed entries (e.g. `null`, or
`"executable": true`) is rejected with exit code 2. aeo does not download the dataset and
this repository contains none of its data. See [docs/compatibility.md](docs/compatibility.md).

## Kubernetes (optional, existing cluster)

`deploy/terraform/modules/aeo-namespace` creates a namespace with Pod Security `restricted`,
a ResourceQuota (GPU requests set to 0), a LimitRange, a default-deny-all NetworkPolicy, and a
default service account that does not auto-mount API tokens. See
`deploy/terraform/examples/existing-cluster`. It was applied and checked on a local kind
cluster (details in [docs/development.md](docs/development.md)). **aeo itself does not run
environments on Kubernetes yet.**

## Documentation

- [docs/architecture.md](docs/architecture.md): run lifecycle and components
- [docs/security.md](docs/security.md): threat model, controls and known limitations
- [docs/compatibility.md](docs/compatibility.md): implemented vs planned matrix and roadmap
- [docs/development.md](docs/development.md): tests, CI, and recorded evidence
- [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md)

## License

Code in this repository is licensed under the [Apache License 2.0](LICENSE). Third-party
software, container base images and datasets are covered by their own licenses. See
[NOTICE](NOTICE).
