"""Data location policy shared by the CLI and importable pipeline modules."""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def data_root() -> Path:
    """Environment override, otherwise repository data; independent of CWD.

    Relative NSE_DATA_DIR values are anchored to the repository. Explicit
    caller paths in resolve_data are instead relative to the caller's CWD.
    Read the environment at call time, not at module import time.
    """
    configured = os.environ.get("NSE_DATA_DIR")
    if not configured:
        return REPO_ROOT / "data"
    path = Path(configured).expanduser()
    return (path if path.is_absolute() else REPO_ROOT / path).resolve()


def resolve_data(arg: str | Path | None = None) -> Path:
    """Explicit path wins over the environment and repository default."""
    return Path(arg).expanduser().resolve() if arg is not None else data_root()


def repo_relative(path: str | Path) -> str:
    """Portable provenance path; refuse paths outside the repository."""
    return Path(path).resolve().relative_to(REPO_ROOT).as_posix()
