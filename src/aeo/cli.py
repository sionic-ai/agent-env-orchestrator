"""``aeo`` command line interface.

Exit codes: 0 run completed (any valid reward, including 0.0) / command succeeded,
1 run finished with status "error" or cleanup problems, 2 invalid input,
3 recognised but unsupported input, 4 Docker unavailable, 130 interrupted.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tarfile
from pathlib import Path
from typing import Any

from aeo import __version__
from aeo.assets import build_workspace_tar
from aeo.catalog import scan_catalog
from aeo.docker_backend import AGENT_UID, DockerBackend, DockerCLI, new_run_id
from aeo.errors import DockerUnavailableError, UnsupportedExecutionError, ValidationError
from aeo.mimo import (
    build_catalog,
    is_mimo_catalog,
    load_catalog,
    require_executable,
    write_catalog,
)
from aeo.runs import RunStore
from aeo.spec import load_spec

EXIT_OK = 0
EXIT_RUN_ERROR = 1
EXIT_INVALID = 2
EXIT_UNSUPPORTED = 3
EXIT_NO_DOCKER = 4
EXIT_INTERRUPTED = 130


def make_backend() -> DockerBackend:
    return DockerBackend(DockerCLI())


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, allow_nan=False))


def _asset_file_count(spec) -> int:
    tar = build_workspace_tar(spec.assets_dir, root_name="w", uid=AGENT_UID, gid=AGENT_UID)
    with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
        return sum(1 for m in archive.getmembers() if m.isfile())


def cmd_validate(args: argparse.Namespace) -> int:
    spec = load_spec(args.path)
    count = _asset_file_count(spec)
    print(f"OK {spec.id}: spec valid, {count} asset file(s), sha256 {spec.sha256[:12]}")
    return EXIT_OK


def cmd_catalog(args: argparse.Namespace) -> int:
    if args.mimo is not None:
        return _mimo_catalog(args)
    entries = scan_catalog(args.root)
    if args.json:
        _print_json([e.as_dict() for e in entries])
        return EXIT_OK
    if not entries:
        print(f"no environments found under {args.root}")
    for entry in entries:
        if entry.spec is None:
            print(f"INVALID  {entry.path.name:<24} {entry.error}")
        else:
            print(f"{entry.spec.id:<32} {entry.spec.description}")
    return EXIT_OK


def _mimo_catalog(args: argparse.Namespace) -> int:
    catalog = load_catalog(args.mimo)
    if args.json:
        _print_json(catalog["entries"])
        return EXIT_OK
    for entry in catalog["entries"]:
        print(
            f"{entry.get('instance_id')!s:<40} {entry.get('dataset_type')!s:<12} "
            f"{entry.get('image') or '-':<40} metadata-only"
        )
    return EXIT_OK


def cmd_mimo_import(args: argparse.Namespace) -> int:
    if args.out.exists() or args.out.is_symlink():
        raise ValidationError(f"refusing to overwrite existing file: {args.out}")
    catalog = build_catalog(args.rows, args.mapping)
    write_catalog(args.out, catalog)
    entries = catalog["entries"]
    with_image = sum(1 for e in entries if e["image"])
    print(
        f"wrote {args.out}: {len(entries)} entries ({with_image} with a mapped image), "
        f"{len(catalog['skipped'])} skipped rows, 0 executable (MiMo execution is not supported)"
    )
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    if is_mimo_catalog(args.path):
        entries = load_catalog(args.path)["entries"]
        require_executable(entries[0] if entries else {})
    spec = load_spec(args.path)
    _asset_file_count(spec)  # fail on a bad asset tree before Docker is touched
    backend = make_backend()
    backend.check_available()
    run_id = new_run_id()
    store = RunStore(args.runs_dir)
    try:
        run_dir = store.create(run_id)  # before running, so a bad --runs-dir costs nothing
    except OSError as exc:
        raise ValidationError(
            f"cannot create run in runs directory {args.runs_dir}: {exc.strerror}"
        ) from None
    print(f"aeo: starting run {run_id} for {spec.id}", file=sys.stderr)
    outputs = backend.run(spec, run_id=run_id)
    _print_json(outputs.result)  # printed first: the result survives a failing save
    try:
        store.save(run_dir, outputs)
    except OSError as exc:
        print(f"aeo: could not store result in {run_dir}: {exc.strerror}", file=sys.stderr)
        return EXIT_RUN_ERROR
    print(f"aeo: result written to {run_dir / 'result.json'}", file=sys.stderr)
    if outputs.interrupted:
        return EXIT_INTERRUPTED
    if outputs.result["status"] != "completed" or not outputs.result["cleanup"]["ok"]:
        return EXIT_RUN_ERROR
    return EXIT_OK


def cmd_result(args: argparse.Namespace) -> int:
    _print_json(RunStore(args.runs_dir).load(args.run_id))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aeo",
        description="Run isolated, CPU-only agent environments on a local Docker daemon.",
    )
    parser.add_argument("--version", action="version", version=f"aeo {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("catalog", help="list environments under a directory")
    p.add_argument("--root", type=Path, default=Path("environments"))
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--mimo", type=Path, help="list a MiMo metadata catalog instead")
    p.set_defaults(func=cmd_catalog)

    p = sub.add_parser("validate", help="validate an environment spec and its assets")
    p.add_argument("path", type=Path, help="environment directory or environment.json")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("run", help="run an environment once with Docker and print the result")
    p.add_argument("path", type=Path, help="environment directory or environment.json")
    p.add_argument("--runs-dir", type=Path, default=Path("runs"))
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("result", help="print a stored run result")
    p.add_argument("run_id")
    p.add_argument("--runs-dir", type=Path, default=Path("runs"))
    p.set_defaults(func=cmd_result)

    mimo = sub.add_parser("mimo", help="MiMo dataset metadata adapter (no execution)")
    mimo_sub = mimo.add_subparsers(dest="mimo_command", required=True)
    p = mimo_sub.add_parser(
        "import", help="normalise local MiMo rows + image mapping into a metadata catalog"
    )
    p.add_argument("--rows", type=Path, required=True, help="rows .jsonl/.json/.parquet")
    p.add_argument("--mapping", type=Path, required=True, help="image mapping .jsonl")
    p.add_argument("--out", type=Path, required=True, help="catalog JSON to create")
    p.set_defaults(func=cmd_mimo_import)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ValidationError as exc:
        print(f"aeo: invalid input: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except UnsupportedExecutionError as exc:
        print(f"aeo: unsupported: {exc}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    except DockerUnavailableError as exc:
        print(f"aeo: {exc}", file=sys.stderr)
        return EXIT_NO_DOCKER
    except KeyboardInterrupt:
        print("aeo: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
