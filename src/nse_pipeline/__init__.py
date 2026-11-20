"""NSE-only historical pipeline (SOLID).

Primary source = NSE official archives/APIs only.
Third-party (Kaggle/Yahoo/Wayback) = labelled cross-check, never primary.

Modules:
- config: Settings (SRP - all paths/cutoffs/workers in one place)
- contracts: Protocols (DIP - high-level code depends on abstractions)
- download: HTTP client + NSE sources (SRP/OCP)
- build: price/action normalization + DuckDB catalog
- audit: calendar/delisting read-only checks
- adjust/universe/identity/backtest: single-responsibility stages
"""
from .config import Settings
from .contracts import Downloader, Parser, Validator, Writer

__all__ = ["Settings", "Downloader", "Parser", "Validator", "Writer"]
