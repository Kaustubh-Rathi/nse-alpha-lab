"""Contracts - dependency inversion: stages depend on these, not concrete NSE URLs."""
from __future__ import annotations
from pathlib import Path
from typing import Protocol


class Downloader(Protocol):
    def download(self, url: str, target: Path) -> dict: ...


class Parser(Protocol):
    def parse(self, raw: bytes) -> list[dict]: ...


class Validator(Protocol):
    def validate(self, rows: list[dict]) -> list[str]: ...


class Writer(Protocol):
    def write(self, rows: list[dict], target: Path) -> None: ...
