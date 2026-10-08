"""MiMo adapter tests. All rows below are synthetic and written by these tests; no MiMo data is
downloaded or vendored."""

import json

import pytest

from aeo import cli
from aeo.errors import UnsupportedExecutionError, ValidationError
from aeo.mimo import (
    MIMO_CATALOG_SCHEMA,
    MimoLimits,
    build_catalog,
    load_catalog,
    load_image_mapping,
    require_executable,
)


def row(instance, **extra):
    return {
        "prompt": [{"role": "user", "content": "SECRET PROMPT TEXT MUST NOT BE COPIED"}],
        "reward_model": {"ground_truth": "SECRET SOLUTION"},
        "extra_info": {"instance_json": json.dumps(instance), **extra},
    }


CODE = {
    "docker_image": "dataset/swe-task:abc",
    "cwd": "/testbed",
    "instance_id": "astropy__astropy-12907",
    "dataset_type": "swe",
}
MUSIC = {"cwd": "/work", "instance_id": "music-0001", "dataset_type": "music"}
UNMAPPED = {
    "docker_image": "dataset/unmapped:1",
    "cwd": "/repo",
    "instance_id": "unmapped-1",
    "dataset_type": "swe",
}


def write_jsonl(path, items):
    path.write_text("".join(json.dumps(i) + "\n" for i in items))
    return path


@pytest.fixture
def mapping(tmp_path):
    return write_jsonl(
        tmp_path / "mapping.jsonl",
        [{"dataset_image": "dataset/swe-task:abc", "dockerhub_image": "example/swe-task:abc"}],
    )


def test_code_row_is_normalised_with_mapped_image(tmp_path, mapping):
    rows = write_jsonl(tmp_path / "rows.jsonl", [row(CODE)])
    catalog = build_catalog(rows, mapping)
    assert catalog["schema_version"] == MIMO_CATALOG_SCHEMA
    [entry] = catalog["entries"]
    assert entry == {
        "row_index": 0,
        "instance_id": "astropy__astropy-12907",
        "dataset_type": "swe",
        "cwd": "/testbed",
        "dataset_image": "dataset/swe-task:abc",
        "image": "example/swe-task:abc",
        "executable": False,
        "unsupported_reason": entry["unsupported_reason"],
    }
    assert "not implemented" in entry["unsupported_reason"]


def test_prompts_and_solutions_are_not_copied(tmp_path, mapping):
    rows = write_jsonl(tmp_path / "rows.jsonl", [row(CODE), row(MUSIC)])
    text = json.dumps(build_catalog(rows, mapping))
    assert "SECRET" not in text


def test_music_row_without_docker_image_is_metadata_only(tmp_path, mapping):
    rows = write_jsonl(tmp_path / "rows.jsonl", [row(MUSIC)])
    [entry] = build_catalog(rows, mapping)["entries"]
    assert entry["dataset_type"] == "music"
    assert entry["dataset_image"] is None
    assert entry["image"] is None
    assert entry["executable"] is False
    assert "no docker image" in entry["unsupported_reason"]


def test_unmapped_image_is_reported(tmp_path, mapping):
    rows = write_jsonl(tmp_path / "rows.jsonl", [row(UNMAPPED)])
    [entry] = build_catalog(rows, mapping)["entries"]
    assert entry["image"] is None
    assert "no dockerhub_image mapping" in entry["unsupported_reason"]


def test_extra_info_given_as_json_string_is_accepted(tmp_path, mapping):
    raw = {"extra_info": json.dumps({"instance_json": json.dumps(CODE)})}
    rows = write_jsonl(tmp_path / "rows.jsonl", [raw])
    assert build_catalog(rows, mapping)["entries"][0]["instance_id"] == CODE["instance_id"]


def test_json_array_rows_file(tmp_path, mapping):
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([row(CODE)]))
    assert len(build_catalog(rows, mapping)["entries"]) == 1


@pytest.mark.parametrize(
    ("bad_row", "reason"),
    [
        ({"no_extra_info": 1}, "extra_info"),
        ({"extra_info": {"other": 1}}, "instance_json"),
        ({"extra_info": {"instance_json": "{not json"}}, "instance_json"),
        ({"extra_info": {"instance_json": json.dumps([1])}}, "object"),
        (row({**CODE, "instance_id": "../../etc/passwd"}), "instance_id"),
        (row({**CODE, "instance_id": None}), "instance_id"),
        (row({**CODE, "cwd": "relative/path"}), "cwd"),
        (row({**CODE, "cwd": "/testbed/../../etc"}), "cwd"),
        (row({**CODE, "docker_image": "--privileged"}), "docker_image"),
        (row({**CODE, "dataset_type": "bad type!"}), "dataset_type"),
        (row({**CODE, "instance_json_extra": "x" * 70000}), "too large"),
    ],
)
def test_malformed_rows_are_skipped_with_reason(tmp_path, mapping, bad_row, reason):
    rows = write_jsonl(tmp_path / "rows.jsonl", [bad_row, row(CODE)])
    catalog = build_catalog(rows, mapping)
    assert [e["instance_id"] for e in catalog["entries"]] == [CODE["instance_id"]]
    [skipped] = catalog["skipped"]
    assert skipped["row_index"] == 0
    assert reason in skipped["reason"]


def test_duplicate_instance_ids_are_skipped(tmp_path, mapping):
    rows = write_jsonl(tmp_path / "rows.jsonl", [row(CODE), row(CODE)])
    catalog = build_catalog(rows, mapping)
    assert len(catalog["entries"]) == 1
    assert "duplicate" in catalog["skipped"][0]["reason"]


def test_row_limit(tmp_path, mapping):
    rows = write_jsonl(
        tmp_path / "rows.jsonl", [row({**CODE, "instance_id": f"i{n}"}) for n in range(5)]
    )
    with pytest.raises(ValidationError, match="rows"):
        build_catalog(rows, mapping, limits=MimoLimits(max_rows=3))


def test_mapping_validation(tmp_path):
    good = write_jsonl(
        tmp_path / "m.jsonl", [{"dataset_image": "a/b:1", "dockerhub_image": "x/b:1"}]
    )
    assert load_image_mapping(good) == {"a/b:1": "x/b:1"}
    for name, items in {
        "missing": [{"dataset_image": "a/b:1"}],
        "inject": [{"dataset_image": "a/b:1", "dockerhub_image": "-v/:/host"}],
        "conflict": [
            {"dataset_image": "a/b:1", "dockerhub_image": "x/b:1"},
            {"dataset_image": "a/b:1", "dockerhub_image": "y/b:1"},
        ],
        "extra": [{"dataset_image": "a/b:1", "dockerhub_image": "x/b:1", "token": "t"}],
    }.items():
        path = write_jsonl(tmp_path / f"{name}.jsonl", items)
        with pytest.raises(ValidationError):
            load_image_mapping(path)


def test_unknown_rows_format_rejected(tmp_path, mapping):
    rows = tmp_path / "rows.csv"
    rows.write_text("a,b\n")
    with pytest.raises(ValidationError, match="format"):
        build_catalog(rows, mapping)


def test_every_entry_refuses_execution(tmp_path, mapping):
    rows = write_jsonl(tmp_path / "rows.jsonl", [row(CODE), row(MUSIC)])
    for entry in build_catalog(rows, mapping)["entries"]:
        with pytest.raises(UnsupportedExecutionError):
            require_executable(entry)


def test_parquet_rows_read_only_extra_info(tmp_path, mapping):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    table = pa.Table.from_pylist([row(CODE), row(MUSIC)])
    path = tmp_path / "rows.parquet"
    pq.write_table(table, path)
    catalog = build_catalog(path, mapping)
    assert [e["dataset_type"] for e in catalog["entries"]] == ["swe", "music"]
    assert "SECRET" not in json.dumps(catalog)


def test_cli_mimo_import_and_run_is_unsupported(tmp_path, mapping, capsys):
    rows = write_jsonl(tmp_path / "rows.jsonl", [row(CODE), row(MUSIC)])
    out = tmp_path / "catalog.json"
    code = cli.main(
        ["mimo", "import", "--rows", str(rows), "--mapping", str(mapping), "--out", str(out)]
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "2 entries" in printed and "0 executable" in printed
    assert json.loads(out.read_text())["schema_version"] == MIMO_CATALOG_SCHEMA

    # refuses to overwrite
    code = cli.main(
        ["mimo", "import", "--rows", str(rows), "--mapping", str(mapping), "--out", str(out)]
    )
    assert code == 2
    capsys.readouterr()

    code = cli.main(["catalog", "--mimo", str(out)])
    listing = capsys.readouterr().out
    assert code == 0
    assert "astropy__astropy-12907" in listing and "metadata-only" in listing

    code = cli.main(["run", str(out)])
    err = capsys.readouterr().err
    assert code == 3
    assert "metadata only" in err


def test_missing_mapping_file_is_validation_error(tmp_path):
    rows = write_jsonl(tmp_path / "rows.jsonl", [row(CODE)])
    with pytest.raises(ValidationError, match="not found"):
        build_catalog(rows, tmp_path / "missing.jsonl")


@pytest.mark.parametrize(
    "bad_entry",
    [
        None,
        "astropy__astropy-12907",
        [],
        {"instance_id": None, "executable": False},
        {"instance_id": "x", "executable": True},
        {"instance_id": "x", "executable": False, "image": 5},
    ],
)
def test_malformed_imported_catalog_entries_are_validation_errors(tmp_path, capsys, bad_entry):
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps({"schema_version": MIMO_CATALOG_SCHEMA, "entries": [bad_entry], "skipped": []})
    )
    with pytest.raises(ValidationError, match="entry 0"):
        load_catalog(path)
    for argv in (["catalog", "--mimo", str(path)], ["catalog", "--mimo", str(path), "--json"]):
        assert cli.main(argv) == 2
    assert cli.main(["run", str(path)]) == 2
    capsys.readouterr()


def test_mimo_rows_fifo_is_rejected_without_blocking(tmp_path, mapping):
    import os
    import subprocess
    import sys

    rows = tmp_path / "rows.jsonl"
    os.mkfifo(rows)
    code = (
        "import sys\nfrom aeo.errors import ValidationError\nfrom aeo.mimo import build_catalog\n"
        "try:\n    build_catalog(sys.argv[1], sys.argv[2])\n"
        "except ValidationError as e:\n    print(e)\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", code, str(rows), str(mapping)],
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert b"not a regular file" in res.stdout, res
