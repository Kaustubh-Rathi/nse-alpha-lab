"""Settings - single responsibility: all configurable paths/cutoffs."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from nse_pipeline.paths import data_root, resolve_data


@dataclass(frozen=True)
class Settings:
    data: Path = field(default_factory=data_root)
    cutoff: date = date.today() - timedelta(days=1)  # yesterday, exclude today
    workers: int = 4
    start: date = date(2000, 1, 1)

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", resolve_data(self.data))

    @property
    def raw(self) -> Path:
        return self.data / "raw"

    @property
    def processed(self) -> Path:
        return self.data / "processed"

    @property
    def reports(self) -> Path:
        return self.data / "reports"

    @property
    def db(self) -> Path:
        return self.data / "market.duckdb"
