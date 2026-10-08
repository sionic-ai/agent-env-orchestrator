# Compatibility and status

## Implemented vs planned

| area | status | notes |
| ---- | ------ | ----- |
| Local Docker backend (single host) | **implemented** | tested on Docker Desktop 29.4 (macOS arm64); CI job on GitHub Ubuntu runners |
| CPU-only execution | **implemented** | by design; no GPU flags exist |
| Environment spec `aeo.environment/v1` | **implemented** | strict validation, bounded |
| Result contract `aeo.result/v1` | **implemented** | `completed` + reward / `error` + `null` |
| Trusted verifier in a separate image, read-only workspace | **implemented** | |
| Isolation (no network, cap drop, no-new-privileges, read-only rootfs, non-root, limits) | **implemented** | see [security.md](security.md) |
| Cleanup on success, failure, timeout, Ctrl-C, with leak check | **implemented** | |
| CLI: `catalog`, `validate`, `run`, `result` | **implemented** | |
| Deterministic demo (`cpu-sum-demo`) with fixture agent | **implemented** | the fixture is a script, **not AI** |
| MiMo rows + image mapping → metadata catalog | **implemented (first pass)** | metadata only, see below |
| Executing MiMo tasks | **not supported** | `aeo run` on a MiMo catalog exits 3 |
| Terraform module for a namespace in an existing cluster | **implemented, optional** | applied on kind + Calico; not run in CI against a cluster |
| Running environments on Kubernetes | planned | no scheduler/controller exists yet |
| Networked environments / egress allowlists | planned | `network` accepts only `"none"` |
| Images that declare `VOLUME` | not supported | refused before any container is created (`image_unsupported`) |
| Workspace disk quotas | planned | Docker `local` volumes have no size limit |
| VM-level isolation (gVisor/Kata/Firecracker) | planned (pluggable runtime) | not provided today |
| Model/agent connectors (call an external model from an agent) | planned | aeo makes no model requests; no trainer or model is included |
| Batch runs, queues, cancellation API, HTTP API | not implemented | intentionally absent from the MVP |
| Training loop integration (RL trainers) | planned (external) | aeo should stay the environment layer |
| GPU support | out of scope for now | |

## Platforms

- Python 3.11, 3.12, 3.13 (CI matrix; tested locally on all three).
- Linux Docker Engine with cgroup v2 is the main target. Docker Desktop on macOS works
  (tested). Windows has not been tested.
- The demo images use a `python:3.12-slim` base pinned by a multi-architecture digest
  (amd64 and arm64).

## MiMo adapter details

Input format, as the adapter understands it:

- **Rows**: `.jsonl` (one row per line), `.json` (array of rows) or `.parquet` (needs the
  `parquet` extra; only the `extra_info` column is read). Each row has
  `extra_info.instance_json`, a **JSON string** that decodes to an object with
  `docker_image`, `cwd`, `instance_id` and `dataset_type`. Other keys are ignored.
  `extra_info` may itself be a JSON string.
- **Image mapping**: JSONL lines with exactly `dataset_image` and `dockerhub_image`.
  Conflicting duplicates are rejected, and `dockerhub_image` must be a valid image reference.

Output (`aeo.mimo-catalog/v1`): per entry `row_index`, `instance_id`, `dataset_type`, `cwd`,
`dataset_image`, `image` (mapped Docker Hub image or `null`), `executable: false`, and
`unsupported_reason`. Malformed rows go to `skipped` with a reason. Duplicate `instance_id`s
are skipped.

What is unsupported and why:

- **Rows without `docker_image`** (e.g. the music domain) have no container to run. They are
  catalogued with a reason.
- **Code/SWE-style rows** have an image and `cwd`, but aeo does not implement MiMo's agent
  harness, test execution or scoring protocol yet. Those images also expect behaviour (repo
  checkout, tests, often network for dependencies) that the current spec cannot express. So
  these are metadata too.
- Fields outside `extra_info.instance_json` (prompts, reference solutions, reward-model
  configuration) are never copied into the catalog.

aeo never downloads MiMo data or images. Use of a local copy is governed by the dataset's
own license and terms.

## Roadmap (not implemented)

1. **Kubernetes execution**: a backend that creates per-run Pods/Jobs in the namespace
   prepared by `deploy/terraform/modules/aeo-namespace`, keeping the same contracts (verifier
   after agent, read-only workspace, null reward on infrastructure errors).
2. **Workflow orchestration**: Argo Workflows or a small controller for batches, retries and
   fan-out.
3. **Multi-user operation**: SSO-backed access and fair-share scheduling or quotas per team.
   This needs an API server, which the MVP intentionally does not have.
4. **External model connector**: let an agent image call an operator-configured model
   endpoint through an explicit egress allowlist, instead of `network: none`.
5. **External training integration**: emit results in formats RL trainers can use. Training
   itself stays out of this repository.
