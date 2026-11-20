# Synthetic mechanics demo

**This directory contains synthetic data. It is not market data, not a backtest
result, and not evidence for or against the strategy. Do not cite it.**

The purpose is narrow: let a reader who has not downloaded the real data pack
run the CLI end-to-end and see the shape of a result. Every price is generated
by a seeded random number generator in `build_pack.py`; there is no connection
to NSE, NIFTY, or any real security.

```bash
# from the repository root
python examples/synthetic_demo/build_pack.py

$env:PYTHONPATH='src'   # PowerShell; use: export PYTHONPATH=src
uv run python -m nse_pipeline backtest \
  examples/synthetic_demo/hello_alpha.json \
  --pack examples/synthetic_demo/pack \
  --out examples/synthetic_demo/result.json
```

For a real run, fetch the checksummed data pack and use
`examples/specs/primary.json` instead. See [../../docs/HELLO_ALPHA.md](../../docs/HELLO_ALPHA.md).

The generated `pack/` directory is ignored by git and can be deleted at any
time. `hello_alpha.json` is deliberately kept outside `examples/specs/` so it
cannot be mistaken for a research specification.
