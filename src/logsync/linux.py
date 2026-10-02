"""Small Linux-specific filesystem helpers."""

import ctypes
import os
from typing import cast


FALLOC_FL_KEEP_SIZE = 0x01
FALLOC_FL_PUNCH_HOLE = 0x02
AT_FDCWD = -100
RENAME_NOREPLACE = 1

_libc = ctypes.CDLL(None, use_errno=True)
_libc.fallocate.argtypes = [
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_longlong,
    ctypes.c_longlong,
]
_libc.fallocate.restype = ctypes.c_int
_libc.renameat2.argtypes = [
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_uint,
]
_libc.renameat2.restype = ctypes.c_int


def punch_hole(fd: int, offset: int, length: int) -> None:
    """Deallocate a byte range without changing file size."""
    if length <= 0:
        return

    mode = FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE
    if cast(int, _libc.fallocate(fd, mode, offset, length)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def rename_noreplace(source: str, destination: str) -> None:
    """Atomically rename without replacing an existing destination."""
    result = cast(int, _libc.renameat2(
        AT_FDCWD,
        os.fsencode(source),
        AT_FDCWD,
        os.fsencode(destination),
        RENAME_NOREPLACE,
    ))
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)
