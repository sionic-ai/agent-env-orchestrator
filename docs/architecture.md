# Architecture

aeo is a small Python package (no runtime dependencies) that drives the `docker` CLI. A run
consists of one untrusted **agent** container and one trusted **verifier** container that
share a run-owned Docker volume, but never at the same time.

## Components

| module | responsibility |
| ------ | -------------- |
| `aeo.spec` | parse and bound `environment.json` (`aeo.environment/v1`); reject unknown fields |
| `aeo.validation` | IDs, image references, argv, hardened JSON loading (size, depth, NaN, duplicate keys) |
| `aeo.assets` | pack a trusted asset directory into a normalised tar stream (no symlinks or special files) |
| `aeo.proc` | `subprocess` with `shell=False`, a hard deadline that also covers pipes held open by descendants, size-limited output capture |
| `aeo.docker_backend` | run lifecycle, isolation flags, error classification, cleanup |
| `aeo.result` | verifier output contract, result contract (`aeo.result/v1`), reward rules |
| `aeo.runs` | host-side result store (`runs/<run_id>/`) |
| `aeo.catalog` | discover environments in a directory |
| `aeo.mimo` | local MiMo rows + image mapping → metadata catalog (no execution) |
| `aeo.cli` | `aeo` argparse CLI |

## Run lifecycle

```
host (aeo)                                   Docker daemon
──────────                                   ─────────────
load + validate spec, pack assets (tar)
docker version                       ──────▶ daemon reachable?
docker image inspect agent, verifier ──────▶ both present locally? → image IDs; any VOLUME? → refuse
docker volume create <run>-ws        ──────▶ run-owned volume (labelled)
docker create <run>-prep (verifier image ID, never started, volume at /aeo-workspace)
docker cp -a - <run>-prep:/   ◀── tar on stdin (uid/gid 10001, fixed modes, mtime 0)
docker rm -f -v <run>-prep
docker create <run>-agent (agent image ID, isolation flags, volume at /workspace rw)
docker start --attach <run>-agent    ──────▶ agent runs; stdout/stderr captured (≤ 1 MiB each)
  └─ on timeout: docker kill <run>-agent
docker inspect <run>-agent           ──────▶ status, exit code (must match attach), OOMKilled
docker rm -f -v <run>-agent                    (agent is gone before verification)
docker run <run>-verifier (verifier image ID, isolation flags, volume at /workspace ro)
  └─ stdout (≤ 16 KiB) → parse verdict; on timeout: docker kill
docker inspect <run>-verifier        ──────▶ status, exit code (client must also exit 0)
finally: docker rm -f -v prep/agent/verifier; docker volume rm -f <run>-ws
         docker ps -a / volume ls --filter label=aeo.run_id=<run>  → must be empty
write runs/<run>/result.json (+ bounded logs) on the host
```

Notes:

- **Names and labels.** Run IDs are `aeo-` plus 16 random hex digits. Every object is named
  `<run_id>-{ws,prep,agent,verifier}` and labelled `aeo.managed=true`,
  `aeo.run_id=<run_id>` and `aeo.role=<role>`.
- **Image pinning.** Images are resolved to their image ID once. Every container is then
  created from that ID, so a tag moved during a run has no effect, and the ID is recorded in
  the result.
- **No pulls.** Every `create`/`run` uses `--pull never`. Images must already exist locally.
- **No image-declared volumes.** An image with a `VOLUME` instruction would make Docker
  create an unlabelled anonymous volume for each container. That volume outlives
  `docker rm -f` and escapes the label-based leak check. aeo reads each image's config
  before creating anything and refuses such images (`image_unsupported`). As a second line
  of defence, containers are removed with `rm -f -v`, which removes anonymous volumes but
  never the named run workspace.
- **Workspace population** uses a container that is created but never started, plus
  `docker cp` from stdin. No host path is mounted, no process runs, and `volume-nocopy`
  keeps image content out of the volume.
- **Agent outcome versus reward.** After the attach ends, aeo inspects the container. If it
  never started, the run is `agent_start_failed`. If it is not `exited` (for example, the
  attach stream dropped while the agent kept running), the run is `docker_error`, and the
  verifier never scores a workspace the agent may still be writing. If the attach client's
  exit code differs from the container's (for example `125` for a daemon or attach error),
  the run is also `docker_error`: aeo cannot vouch for a run it did not observe correctly.
  The same checks apply to the verifier (state `exited`, and `docker run` itself exits 0). Otherwise the agent's exit code, timeout and OOM status are recorded, but they
  do not set the reward. The verifier scores whatever the workspace contains when
  the agent stops. A crashed or timed-out agent usually gets `0.0` from the verifier.
- **Results reach the host only through captured stdout/stderr.** No container gets a
  writable host mount.

## Result statuses

| status | reward | when |
| ------ | ------ | ---- |
| `completed` | float in `[0, 1]` | verifier exited 0 and printed a valid verdict |
| `error` | `null` | `image_unavailable`, `image_unsupported`, `workspace_setup_failed`, `agent_start_failed`, `verifier_failed`, `verifier_timeout`, `verifier_output_invalid`, `docker_error`, `interrupted`, `internal_error` |

`cleanup.ok` is reported separately. A run whose task result is valid but whose cleanup
could not be confirmed keeps its status, and the CLI exits with code 1.

## Testing seams

`DockerBackend` takes any object with the `DockerRunner.run(args, *, timeout, stdin,
max_stdout, max_stderr, on_timeout)` interface. Unit tests use a recording fake to check the
exact argv (isolation flags, mount options, ordering), error mapping and cleanup without a
daemon. The `docker`-marked tests run the same code against a real daemon.
