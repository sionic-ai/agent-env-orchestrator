"""Pack a trusted asset directory into a tar stream for the run's workspace volume.

The stream is built entirely in Python so that what lands in the volume is exactly what was
validated: only regular files and directories, plain names, normalised modes and ownership,
fixed mtimes, bounded counts and sizes. Symlinks, hard links and special files are refused,
and every file is opened relative to an already-opened parent directory with ``O_NOFOLLOW``
so a path swapped for a symlink mid-walk can not redirect the read.
"""

from __future__ import annotations

import io
import os
import re
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path

from aeo.errors import ValidationError

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


@dataclass(frozen=True)
class AssetLimits:
    max_entries: int = 1000
    max_file_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
    max_depth: int = 16


class _Packer:
    def __init__(self, tar: tarfile.TarFile, uid: int, gid: int, limits: AssetLimits) -> None:
        self.tar = tar
        self.uid = uid
        self.gid = gid
        self.limits = limits
        self.entries = 0
        self.total = 0

    def _info(self, name: str, *, is_dir: bool, mode: int, size: int = 0) -> tarfile.TarInfo:
        info = tarfile.TarInfo(name)
        info.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
        info.mode = mode
        info.uid, info.gid = self.uid, self.gid
        info.uname = info.gname = ""
        info.mtime = 0
        info.size = size
        return info

    def add_dir(self, name: str) -> None:
        self.tar.addfile(self._info(name, is_dir=True, mode=0o755))

    def walk(self, dir_fd: int, prefix: str, depth: int) -> None:
        with os.scandir(dir_fd) as it:
            names = sorted(entry.name for entry in it)
        for name in names:
            rel = f"{prefix}/{name}"
            if name in (".", "..") or not _NAME_RE.fullmatch(name):
                raise ValidationError(
                    f"asset name not allowed (use [A-Za-z0-9._-], max 128): {rel!r}"
                )
            self.entries += 1
            if self.entries > self.limits.max_entries:
                raise ValidationError(f"too many asset entries (limit {self.limits.max_entries})")
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                raise ValidationError(f"asset must not be a symlink: {rel}")
            if stat.S_ISDIR(st.st_mode):
                if depth + 1 > self.limits.max_depth:
                    raise ValidationError(f"asset tree too deep (limit {self.limits.max_depth})")
                self.add_dir(rel)
                child = os.open(
                    name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=dir_fd
                )
                try:
                    _same_inode(child, st, rel)
                    self.walk(child, rel, depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(st.st_mode):
                self.add_file(dir_fd, name, rel, st)
            else:
                raise ValidationError(f"asset is not a regular file or directory: {rel}")

    def add_file(self, dir_fd: int, name: str, rel: str, st: os.stat_result) -> None:
        if st.st_nlink > 1:
            raise ValidationError(f"asset must not be a hard link: {rel}")
        if st.st_size > self.limits.max_file_bytes:
            raise ValidationError(
                f"asset file too large (limit {self.limits.max_file_bytes} bytes): {rel}"
            )
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=dir_fd)
        try:
            _same_inode(fd, st, rel)
            data = _read_at_most(fd, self.limits.max_file_bytes)
        finally:
            os.close(fd)
        if len(data) > self.limits.max_file_bytes:
            raise ValidationError(f"asset file too large while reading: {rel}")
        self.total += len(data)
        if self.total > self.limits.max_total_bytes:
            raise ValidationError(
                f"assets exceed total size limit ({self.limits.max_total_bytes} bytes)"
            )
        mode = 0o755 if st.st_mode & stat.S_IXUSR else 0o644
        self.tar.addfile(self._info(rel, is_dir=False, mode=mode, size=len(data)), io.BytesIO(data))


def _same_inode(fd: int, expected: os.stat_result, rel: str) -> None:
    st = os.fstat(fd)
    if (st.st_dev, st.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValidationError(f"asset changed while being read: {rel}")


def _read_at_most(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 1 << 16))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def build_workspace_tar(
    source: Path | None,
    *,
    root_name: str,
    uid: int,
    gid: int,
    limits: AssetLimits | None = None,
) -> bytes:
    """Return a tar archive whose single top-level directory ``root_name`` holds the assets."""
    limits = limits or AssetLimits()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        packer = _Packer(tar, uid, gid, limits)
        packer.add_dir(root_name)
        if source is not None:
            try:
                st = os.lstat(source)
            except FileNotFoundError:
                raise ValidationError(f"assets directory not found: {source}") from None
            if stat.S_ISLNK(st.st_mode):
                raise ValidationError(f"assets directory must not be a symlink: {source}")
            if not stat.S_ISDIR(st.st_mode):
                raise ValidationError(f"assets path is not a directory: {source}")
            root_fd = os.open(source, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC)
            try:
                _same_inode(root_fd, st, str(source))
                packer.walk(root_fd, root_name, 0)
            finally:
                os.close(root_fd)
    return buf.getvalue()
