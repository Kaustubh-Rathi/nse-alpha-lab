import pytest

from nse_pipeline.actions.factors import (
    consolidation_share_multiplier,
    rights_share_multiplier,
)


def test_rights_share_multiplier_clean_ratios():
    assert rights_share_multiplier("RHTS 2:5 @PREMIUM RS.140") == pytest.approx(7 / 5)
    assert rights_share_multiplier("RIGHTS 2:5@ PREM RS 75") == pytest.approx(7 / 5)
    assert rights_share_multiplier("RHTS 5:11 @PREMIUM RS.63") == pytest.approx(16 / 11)
    assert rights_share_multiplier("RIGHTS 2:5 @ PREMUM RE 1") == pytest.approx(7 / 5)


def test_rights_share_multiplier_rejects_complex_instruments():
    # NCD/warrant/CCPS instruments are not plain share ratios.
    assert rights_share_multiplier("RIGHTS- 1NCD&2WRNTS:8EQSH") is None
    assert rights_share_multiplier("RIGHTS - 7 Ccps And 7 Warrants:40") is None


def test_rights_share_multiplier_no_ratio_or_invalid():
    assert rights_share_multiplier("Right Issue of Equity Shares") is None
    assert rights_share_multiplier("") is None
    assert rights_share_multiplier(None) is None
    # zero denominator is not a valid multiplier
    assert rights_share_multiplier("RIGHTS 2:0") is None


def test_consolidation_share_multiplier():
    assert consolidation_share_multiplier("CONSOLIDATION RE1 TO RS10") == pytest.approx(0.1)
    assert consolidation_share_multiplier("CONSOLIDATION RE2 TO RS10") == pytest.approx(0.2)
    assert consolidation_share_multiplier("CONSOLIDATION RE 5 TO RS 10") == pytest.approx(0.5)


def test_consolidation_share_multiplier_rejects_non_consolidation():
    assert consolidation_share_multiplier("SCHEME OF ARRANGEMENT") is None
    assert consolidation_share_multiplier("DEMERGER") is None
    assert consolidation_share_multiplier("") is None
