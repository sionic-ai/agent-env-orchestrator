import io
import os
import socket
import tarfile

import pytest

from aeo.assets import AssetLimits, build_workspace_tar
from aeo.errors import ValidationError

UID = 10001


def members(data: bytes):
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return {m.name: m for m in tar.getmembers()}, tar


def read_member(data: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return tar.extractfile(name).read()


def test_tar_contains_root_dir_and_files_owned_by_workspace_uid(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "task.md").write_text("hello")
    (tmp_path / "sub" / "data.txt").write_text("1\n2\n")
    os.chmod(tmp_path / "task.md", 0o600)
    data = build_workspace_tar(tmp_path, root_name="aeo-workspace", uid=UID, gid=UID)
    found, _ = members(data)
    assert set(found) == {
        "aeo-workspace",
        "aeo-workspace/sub",
        "aeo-workspace/sub/data.txt",
        "aeo-workspace/task.md",
    }
    for m in found.values():
        assert (m.uid, m.gid) == (UID, UID)
        assert m.mtime == 0
        assert m.isdir() or m.isfile()
    # modes are normalised, not copied from the host
    assert found["aeo-workspace/task.md"].mode == 0o644
    assert found["aeo-workspace"].mode == 0o755
    assert read_member(data, "aeo-workspace/sub/data.txt") == b"1\n2\n"


def test_tar_is_deterministic(tmp_path):
    (tmp_path / "b.txt").write_text("b")
    (tmp_path / "a.txt").write_text("a")
    one = build_workspace_tar(tmp_path, root_name="w", uid=UID, gid=UID)
    two = build_workspace_tar(tmp_path, root_name="w", uid=UID, gid=UID)
    assert one == two


def test_empty_source_gives_only_root(tmp_path):
    data = build_workspace_tar(None, root_name="w", uid=UID, gid=UID)
    found, _ = members(data)
    assert list(found) == ["w"]


def test_executable_bit_is_kept_as_0755(tmp_path):
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\n")
    os.chmod(script, 0o700)
    found, _ = members(build_workspace_tar(tmp_path, root_name="w", uid=UID, gid=UID))
    assert found["w/run.sh"].mode == 0o755


def test_symlink_file_is_rejected(tmp_path):
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("host secret")
    src = tmp_path / "assets"
    src.mkdir()
    os.symlink(secret, src / "leak.txt")
    with pytest.raises(ValidationError, match="symlink"):
        build_workspace_tar(src, root_name="w", uid=UID, gid=UID)


def test_symlink_dir_is_rejected(tmp_path):
    src = tmp_path / "assets"
    src.mkdir()
    os.symlink("/etc", src / "etc")
    with pytest.raises(ValidationError, match="symlink"):
        build_workspace_tar(src, root_name="w", uid=UID, gid=UID)


def test_hardlink_is_rejected(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("host data")
    src = tmp_path / "assets"
    src.mkdir()
    os.link(outside, src / "hard.txt")
    with pytest.raises(ValidationError, match="hard link"):
        build_workspace_tar(src, root_name="w", uid=UID, gid=UID)


def test_fifo_and_socket_are_rejected(tmp_path):
    src = tmp_path / "fifo"
    src.mkdir()
    os.mkfifo(src / "pipe")
    with pytest.raises(ValidationError, match="regular file"):
        build_workspace_tar(src, root_name="w", uid=UID, gid=UID)

    src2 = tmp_path / "sock"
    src2.mkdir()
    # AF_UNIX path length is limited, so bind relative to the directory.
    cwd = os.getcwd()
    os.chdir(src2)
    try:
        s = socket.socket(socket.AF_UNIX)
        s.bind("s")
    finally:
        os.chdir(cwd)
    try:
        with pytest.raises(ValidationError, match="regular file"):
            build_workspace_tar(src2, root_name="w", uid=UID, gid=UID)
    finally:
        s.close()


@pytest.mark.parametrize(
    "name", ["bad name.txt", "semi;colon", "new\nline", "-dash-is-ok-but-not-this\x01"]
)
def test_unsafe_names_are_rejected(tmp_path, name):
    (tmp_path / name).write_text("x")
    with pytest.raises(ValidationError, match="name"):
        build_workspace_tar(tmp_path, root_name="w", uid=UID, gid=UID)


def test_limits_on_count_size_and_depth(tmp_path):
    for i in range(3):
        (tmp_path / f"f{i}.txt").write_text("x" * 10)
    with pytest.raises(ValidationError, match="too many"):
        build_workspace_tar(
            tmp_path, root_name="w", uid=UID, gid=UID, limits=AssetLimits(max_entries=2)
        )
    with pytest.raises(ValidationError, match="file too large"):
        build_workspace_tar(
            tmp_path, root_name="w", uid=UID, gid=UID, limits=AssetLimits(max_file_bytes=5)
        )
    with pytest.raises(ValidationError, match="total"):
        build_workspace_tar(
            tmp_path, root_name="w", uid=UID, gid=UID, limits=AssetLimits(max_total_bytes=25)
        )
    deep = tmp_path / "d"
    cur = deep
    for i in range(4):
        cur = cur / f"l{i}"
    cur.mkdir(parents=True)
    with pytest.raises(ValidationError, match="deep"):
        build_workspace_tar(
            tmp_path, root_name="w", uid=UID, gid=UID, limits=AssetLimits(max_depth=3)
        )


def test_source_root_itself_must_not_be_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    with pytest.raises(ValidationError, match="symlink"):
        build_workspace_tar(link, root_name="w", uid=UID, gid=UID)
