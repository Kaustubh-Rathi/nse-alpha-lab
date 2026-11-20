"""MSC400 universe: official dated constituent tables, no inferred membership."""
from datetime import date

import pytest

from nse_pipeline.backtest.msc400 import (
    Constituent,
    Snapshot,
    membership_by_date,
    parse_constituent_text,
)

# A faithful excerpt of the real NSE layout: symbol, company, sector, price,
# weight, market cap. Note the wrapped company name and the wrapped sector.
SAMPLE = """\
AMARAJABAT
Amara Raja Batteries Ltd.
AUTOMOBILE
699.60
0.43
5736
BBTC
Bombay Burmah Trading
Corporation Ltd.
CONSUMER GOODS
1200.50
0.21
2500
ZEEL
Zee Entertainment Enterprises Ltd.
MEDIA, ENTERTAINMENT &
PUBLICATION
300.95
1.10
27750
ZYDUSWELL
Zydus Wellness Ltd.
CONSUMER GOODS
2054.50
0.12
3138
"""


class TestParsing:
    def test_parses_simple_rows(self):
        rows = parse_constituent_text(SAMPLE)
        assert len(rows) == 4
        assert rows[0].symbol == "AMARAJABAT"
        assert rows[0].market_cap_crore == 5736.0

    def test_wrapped_company_name_is_rejoined(self):
        rows = parse_constituent_text(SAMPLE)
        bombay = next(row for row in rows if row.symbol == "BBTC")
        assert bombay.company == "Bombay Burmah Trading Corporation Ltd."

    def test_wrapped_sector_is_rejoined(self):
        rows = parse_constituent_text(SAMPLE)
        zee = next(row for row in rows if row.symbol == "ZEEL")
        assert zee.sector == "MEDIA, ENTERTAINMENT & PUBLICATION"

    def test_numeric_fields_are_floats(self):
        rows = parse_constituent_text(SAMPLE)
        assert all(isinstance(row.price, float) for row in rows)
        assert all(isinstance(row.weight_pct, float) for row in rows)
        assert all(isinstance(row.market_cap_crore, float) for row in rows)

    def test_duplicate_symbol_kept_once(self):
        doubled = SAMPLE + SAMPLE
        rows = parse_constituent_text(doubled)
        symbols = [row.symbol for row in rows]
        assert len(symbols) == len(set(symbols))

    def test_empty_text_yields_nothing(self):
        assert parse_constituent_text("") == ()

    def test_garbage_yields_nothing(self):
        assert parse_constituent_text("hello\nworld\nno numbers here") == ()

    def test_row_without_numbers_is_skipped(self):
        broken = "AMARAJABAT\nAmara Raja Batteries Ltd.\nAUTOMOBILE\nnot-a-price\n0.43\n5736\n"
        assert parse_constituent_text(broken) == ()

    def test_unknown_sector_is_skipped_not_guessed(self):
        unknown = "FOO\nFoo Ltd.\nMADE UP SECTOR\n100.0\n0.1\n500\n"
        assert parse_constituent_text(unknown) == ()

    def test_numeric_market_cap_is_never_treated_as_a_symbol(self):
        # A row whose ticker line is missing must be dropped, not given the
        # previous row's market cap as a symbol.
        broken = ("Bombay Burmah Trading\nCorporation Ltd.\nCONSUMER GOODS\n"
                  "1200.50\n0.21\n2500\n")
        assert parse_constituent_text(broken) == ()


class TestSnapshot:
    def test_symbols_property(self):
        snapshot = Snapshot(
            as_of=date(2019, 10, 31),
            index_name="MidSmallcap_400",
            source_zip="indices_dataOct2019.zip",
            source_member="NIFTY_MidSmallcap_400_Oct2019.pdf",
            constituents=(
                Constituent("AAA", "A Ltd.", "IT", 1.0, 0.1, 100.0),
                Constituent("BBB", "B Ltd.", "IT", 2.0, 0.2, 200.0),
            ),
        )
        assert snapshot.symbols == frozenset({"AAA", "BBB"})

    def test_membership_by_date(self):
        snapshot = Snapshot(
            as_of=date(2019, 10, 31), index_name="MidSmallcap_400",
            source_zip="z.zip", source_member="m.pdf",
            constituents=(Constituent("AAA", "A Ltd.", "IT", 1.0, 0.1, 100.0),),
        )
        assert membership_by_date([snapshot]) == {date(2019, 10, 31): {"AAA"}}


class TestRealArchive:
    """These read the actual ZIPs on disk; skipped when the archive is absent."""

    def test_real_snapshots_have_roughly_400_rows(self):
        from nse_pipeline.backtest.msc400 import DEFAULT_RAW, load_snapshots
        if not DEFAULT_RAW.is_dir():
            pytest.skip("raw index archive not present")
        snapshots = load_snapshots()
        if not snapshots:
            pytest.skip("no MidSmallcap 400 tables in the archive")
        for snapshot in snapshots:
            # NSE's own tables vary slightly month to month; a large drop would
            # mean the parser broke, not that the index shrank.
            assert 380 <= len(snapshot.constituents) <= 400, (
                f"{snapshot.as_of}: {len(snapshot.constituents)} rows")

    def test_real_snapshots_carry_market_caps(self):
        from nse_pipeline.backtest.msc400 import DEFAULT_RAW, load_snapshots
        if not DEFAULT_RAW.is_dir():
            pytest.skip("raw index archive not present")
        snapshots = load_snapshots()
        if not snapshots:
            pytest.skip("no MidSmallcap 400 tables in the archive")
        for snapshot in snapshots:
            with_cap = sum(1 for row in snapshot.constituents
                           if row.market_cap_crore and row.market_cap_crore > 0)
            assert with_cap >= len(snapshot.constituents) - 2
