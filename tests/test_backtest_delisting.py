"""Reason-conditioned delisting returns: no invented haircut, no silent default."""
from decimal import Decimal

import pytest

from nse_pipeline.backtest.delisting import (
    REASON_HAIRCUTS,
    load_delisting_register,
    normalise_reason,
    terms_for,
)

D = Decimal


class TestReasonNormalisation:
    def test_compulsory(self):
        assert normalise_reason("Compulsory Delisting") == "compulsory"

    def test_liquidation(self):
        assert normalise_reason("Delisting - Liquidation") == "liquidation"

    def test_voluntary(self):
        assert normalise_reason("Voluntary Delisting ") == "voluntary"

    def test_case_and_whitespace_tolerated(self):
        assert normalise_reason("  COMPULSORY DELISTING  ") == "compulsory"

    def test_unknown_returns_none(self):
        assert normalise_reason("Merged") is None

    def test_empty_returns_none(self):
        assert normalise_reason("") is None
        assert normalise_reason(None) is None


class TestTerms:
    def test_liquidation_is_total_loss(self):
        terms = terms_for("Delisting - Liquidation")
        assert terms.haircut == D("1.00")
        assert terms.exit_price(D("100")) == D("0")

    def test_compulsory_uses_shumway_thirty_percent(self):
        terms = terms_for("Compulsory Delisting")
        assert terms.haircut == D("0.30")
        assert terms.exit_price(D("100")) == D("70")

    def test_voluntary_has_no_haircut(self):
        terms = terms_for("Voluntary Delisting")
        assert terms.haircut == D("0.00")
        assert terms.exit_price(D("100")) == D("100")

    def test_unknown_reason_returns_none_not_a_guess(self):
        assert terms_for("Something Else") is None

    def test_every_reason_carries_a_citation(self):
        for raw in ("Compulsory Delisting", "Delisting - Liquidation",
                    "Voluntary Delisting"):
            terms = terms_for(raw)
            assert terms.citation
            assert "Shumway" in terms.citation or "SEBI" in terms.citation

    def test_exit_price_rejects_non_positive(self):
        terms = terms_for("Compulsory Delisting")
        with pytest.raises(ValueError, match="positive Decimal"):
            terms.exit_price(D("0"))

    def test_haircut_table_is_complete(self):
        assert set(REASON_HAIRCUTS) == {"liquidation", "compulsory", "voluntary"}


class TestRegister:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_delisting_register(tmp_path / "nope.csv") == {}

    def test_parses_real_columns(self, tmp_path):
        path = tmp_path / "delisted.csv"
        path.write_text(
            "Symbol,Company,Delisted Date,Type of Delisting\n"
            "ANIKSHIP,Anik Ship Breaking,26-Jul-04,Compulsory Delisting \n"
            "CABOTINDIA,Cabot India Ltd,15-Apr-02,Voluntary Delisting \n",
            encoding="utf-8")
        register = load_delisting_register(path)
        assert register["ANIKSHIP"]["reason"] == "compulsory"
        assert register["CABOTINDIA"]["reason"] == "voluntary"
        assert register["ANIKSHIP"]["delisted_date"].year == 2004

    def test_earliest_record_wins_on_symbol_reuse(self, tmp_path):
        path = tmp_path / "delisted.csv"
        path.write_text(
            "Symbol,Company,Delisted Date,Type of Delisting\n"
            "REUSED,Later Co,26-Jul-04,Compulsory Delisting \n"
            "REUSED,First Co,15-Apr-02,Voluntary Delisting \n",
            encoding="utf-8")
        register = load_delisting_register(path)
        assert register["REUSED"]["company"] == "First Co"

    def test_unparseable_date_is_none_not_invented(self, tmp_path):
        path = tmp_path / "delisted.csv"
        path.write_text(
            "Symbol,Company,Delisted Date,Type of Delisting\n"
            "X,Co,not-a-date,Compulsory Delisting \n",
            encoding="utf-8")
        assert load_delisting_register(path)["X"]["delisted_date"] is None
