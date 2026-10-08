"""Filesystem helpers that refuse symlinks and bound how much they read."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from aeo.errors import ValidationError

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
# Opening a FIFO without O_NONBLOCK waits for a writer forever; the type is checked by fstat
# right after the open. O_NONBLOCK has no effect on reads from a regular file.
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def open_regular_file(path: Path, *, what: str = "file") -> int:
    """Open a regular file read-only without following a symlink at its final component.

    Never blocks on a FIFO or device node. Returns a descriptor the caller must close.
    """
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK)
    except FileNotFoundError:
        raise ValidationError(f"{what} not found: {path}") from None
    except OSError as exc:
        if os.path.islink(path):
            raise ValidationError(f"{what} must not be a symlink: {path}") from None
        raise ValidationError(f"cannot open {what}: {path} ({exc.strerror})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValidationError(f"{what} is not a regular file: {path}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_regular_file(path: Path, *, max_bytes: int, what: str = "file") -> bytes:
    """Read a regular file without following a symlink at its final component."""
    fd = open_regular_file(path, what=what)
    try:
        if os.fstat(fd).st_size > max_bytes:
            raise ValidationError(f"{what} too large (limit {max_bytes} bytes): {path}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 16))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise ValidationError(f"{what} too large (limit {max_bytes} bytes): {path}")
        return data
    finally:
        os.close(fd)


def ensure_no_symlink_components(path: Path, *, root: Path, what: str) -> None:
    """Fail if any component of ``path`` below ``root`` is a symlink or escapes ``root``."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        raise ValidationError(f"{what} must stay inside {root}") from None
    current = root
    for part in rel.parts:
        current = current / part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            raise ValidationError(f"{what} not found: {current}") from None
        if stat.S_ISLNK(st.st_mode):
            raise ValidationError(f"{what} must not be or contain a symlink: {current}")
