# Security model

## Scope and non-goals

aeo isolates an agent with **standard Linux container isolation on a local Docker daemon**.
That reduces what a misbehaving or buggy agent can reach. It is **not** a hardened sandbox
for deliberately malicious code:

- Containers share the host kernel. A kernel or container-runtime escape defeats every
  control listed below. To run hostile code, add a VM boundary (e.g. gVisor, Kata or
  Firecracker-backed runtimes). aeo does not provide one.
- The Docker daemon runs with root privileges. Anyone who can run `aeo` can already control
  Docker.
- aeo is a local, single-user tool. It is not a multi-tenant service and has no
  authentication, quotas per user, or API.

## Trust boundaries

| party | trusted? | notes |
| ----- | -------- | ----- |
| operator running `aeo`, the host, the Docker daemon | yes | |
| environment spec + asset directory | partly | validated and bounded, but it chooses the images and commands |
| verifier image and its command | **yes** | it decides the reward; it is the only party that holds the answers |
| agent image and agent process | **no** | it is the subject under test |
| verifier stdout | no | parsed strictly, because a verifier bug can still emit garbage |
| MiMo rows / mapping files | no | parsed as data and never executed |

## Controls (implemented and tested)

**Agent container** (fixed by the backend; no spec field can change them):

| control | flag |
| ------- | ---- |
| no network | `--network none` (only `lo` is up, no routes) |
| no Linux capabilities | `--cap-drop ALL` (CapEff `0000000000000000`) |
| no privilege escalation | `--security-opt no-new-privileges` |
| read-only root filesystem | `--read-only`; writable: `/workspace` volume, `/tmp` tmpfs (`noexec,nosuid,nodev`, 64 MiB) |
| non-root | `--user 10001:10001` |
| resource limits | `--cpus`, `--memory` = `--memory-swap`, `--pids-limit`, `--ulimit nofile=1024` |
| no host mounts, no Docker socket | only a run-owned named volume is mounted |
| no host environment | only `HOME=/tmp` is set by aeo; the image's own `ENV` still applies |
| no container log persistence | `--log-driver none`; output is captured by aeo, size-limited |
| image pinning, no pulls | containers use the inspected image ID; `--pull never` |
| no image-declared volumes | images with `VOLUME` are refused before anything is created; containers are removed with `rm -f -v` |
| default seccomp/AppArmor | never disabled; no `--privileged`, `--cap-add`, devices, `--pid/--ipc host` |

These properties are checked from inside a real container by the fixture agent's `probe`
mode in `tests/docker/test_docker_e2e.py::test_agent_isolation_as_observed_from_inside`.

**Verifier separation.** The agent container is removed before the verifier starts. The
verifier gets the same hardening, and its workspace mount is `readonly`. Verifier code and
expected answers live only in the verifier image, so the agent cannot see them (`probe`
confirms `/opt/aeo-verifier` does not exist in the agent). The demo verifier opens the answer
with `O_NOFOLLOW`. A symlink planted by the agent toward the verifier's answer file scores 0
(`test_symlink_answer_can_not_trick_verifier`). The verifier also opens with `O_NONBLOCK`,
so a FIFO planted as the answer cannot stall it into a timeout. Such a timeout would turn a
failed task into an excluded (`reward: null`) sample (`test_fifo_answer_can_not_stall_verifier_into_error`).
Custom verifiers must follow the same rule: never block on, or follow, agent-controlled files.

**Input handling.**

- Spec: 64 KiB maximum, strict JSON (no NaN/Infinity, no duplicate keys, depth limit),
  unknown fields rejected, every number range-checked. Image references follow a strict
  grammar that cannot start with `-`. Commands are argv arrays and never pass through a shell.
- Assets: read through directory file descriptors with `O_NOFOLLOW` and inode re-checks.
  Symlinks, hard links and special files are refused. Names, entry count, file size, total
  size and depth are bounded. Ownership and modes are normalised.
- Run IDs are `^aeo-[0-9a-f]{16}$` before they touch a path, so `aeo result ../../x` is
  refused. Result files are read with `O_NOFOLLOW` and re-validated.
- Host input files (spec, MiMo rows, catalogs, stored results) are opened with
  `O_NOFOLLOW | O_NONBLOCK` and must be regular files (`fstat`), so a FIFO cannot hang aeo.
- Subprocesses: `shell=False`, a hard deadline, bounded capture (extra output is drained and
  counted, not kept). Pipes are read non-blockingly from a single thread. A descendant
  that keeps the client's stdout/stderr open cannot extend a call past its deadline: the
  call times out, the client's process group is killed, and the pipes are abandoned after a
  short bounded drain. The `docker` client gets an allowlisted environment only (`PATH`,
  `HOME`, `DOCKER_*`, …). API keys and cloud credentials in the caller's environment are not
  passed on.
- Errors embedded in results are reduced to printable ASCII and truncated.

**Cleanup.** Containers and the volume are removed in a `finally` path, which also covers
Ctrl-C. aeo then re-lists objects by `aeo.run_id` label and reports any leftovers in
`cleanup.errors` (exit code 1).

## Known limitations

- **No disk quota on the workspace volume.** Docker's `local` volume driver has no size
  limit, so an agent can fill the Docker data disk. Memory, CPU and PIDs are limited.
  Planned: tmpfs- or quota-backed workspaces.
- **The agent image is not sandboxed from itself.** Anything inside the image (including its
  `ENV`) is visible to the agent. Do not bake secrets into agent images.
- **Base-image environment.** `python:*` images set variables such as `GPG_KEY` (a public key
  ID, not a secret). aeo cannot remove an image's `ENV`.
- **Logs on the host.** Agent stdout/stderr (≤ 1 MiB each) and verifier stderr (≤ 8 KiB) are
  stored in `runs/<run_id>/` with mode `0600`. Verifier stderr may contain verifier internals,
  so treat run directories as private.
- **Timing side channels, CPU contention and kernel attack surface** are out of scope.
- **Docker Desktop and rootless Docker** were not separately hardened. Tests ran on Docker
  Desktop (macOS, linuxkit kernel) and are configured to run on GitHub's Ubuntu runners.
- The Kubernetes module's NetworkPolicy only works when the cluster's CNI enforces
  NetworkPolicy. It was confirmed with Calico. kind's default CNI (kindnet in kind v0.33.0)
  did **not** enforce it in our test.

## Reporting

See [SECURITY.md](../SECURITY.md).
