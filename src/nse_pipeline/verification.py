"""Scoped verification isolation, never enabled by normal pipeline commands.

Python audit hooks reject writes below protected roots. On Windows, read-only
sharing handles additionally protect existing files against native libraries and
child processes, even ones that bypass Python startup hooks. This is not a
general-purpose sandbox for untrusted executables.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import sys

ENVIRONMENT_KEY = "NSE_VERIFY_PROTECTED_ROOTS"
_active_roots: list[Path] = []
_hook_installed = False


def _protected(value) -> bool:
    if not isinstance(value, (str, bytes, os.PathLike)):
        return False  # e.g. a pre-opened integer file descriptor
    path = Path(os.fsdecode(value)).resolve()
    return any(path == root or path.is_relative_to(root) for root in _active_roots)


def require_writable_output(path):
    """Reject a mutating command before expensive work or exist_ok mkdir.

    Path.mkdir(exist_ok=True) can swallow an audit-hook PermissionError when
    the directory already exists. Actual writes stay protected, but commands
    should not do large dataset reads before eventually hitting that guard.
    """
    if _protected(path):
        raise PermissionError(f"Verification cannot modify protected production artifacts: {path}")


def _audit(event, args):
    paths = ()
    if event == "open":
        path, mode, flags = args
        writing = ((isinstance(mode, str) and any(char in mode for char in "wax+"))
                   or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
        if writing:
            paths = (path,)
    elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime", "os.truncate"):
        paths = args[:1]
    elif event in ("os.rename", "os.link", "os.symlink"):
        paths = args[:2]
    if any(_protected(path) for path in paths):
        raise PermissionError(f"Verification cannot modify protected production artifacts: {paths}")


@contextmanager
def protect_artifacts(roots):
    """Protect roots for this scope and propagate the policy to Python children.

    Existing Windows files are held without FILE_SHARE_WRITE/DELETE. Failure to
    acquire a handle aborts verification rather than silently dropping a guard.
    New output must go to temporary trees outside the protected roots.
    """
    global _hook_installed
    roots = [Path(root).resolve() for root in roots]
    handles = []
    close_handle = None
    activated = False
    previous = os.environ.get(ENVIRONMENT_KEY)
    if not _hook_installed:
        sys.addaudithook(_audit)
        _hook_installed = True
    try:
        if os.name == "nt":
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel.CreateFileW
            create_file.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
            create_file.restype = wintypes.HANDLE
            close_handle = kernel.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL
            for root in roots:
                paths = [root] if root.is_file() else root.rglob("*") if root.exists() else []
                for path in paths:
                    if not path.is_file():
                        continue
                    handle = create_file(str(path), 0x80000000, 1, None, 3, 0x80, None)
                    if handle == ctypes.c_void_p(-1).value:
                        raise OSError(ctypes.get_last_error(), f"Cannot protect {path}")
                    handles.append(handle)
        _active_roots.extend(roots)
        activated = True
        os.environ[ENVIRONMENT_KEY] = json.dumps([str(root) for root in _active_roots])
        yield
    finally:
        for root in reversed(roots) if activated else []:
            # Acquisition can fail before these roots are activated.
            if root in _active_roots:
                index = len(_active_roots) - 1 - _active_roots[::-1].index(root)
                _active_roots.pop(index)
        if previous is None:
            os.environ.pop(ENVIRONMENT_KEY, None)
        else:
            os.environ[ENVIRONMENT_KEY] = previous
        if close_handle:
            for handle in reversed(handles):
                close_handle(handle)
