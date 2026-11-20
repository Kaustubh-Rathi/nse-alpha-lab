from datetime import date

import pytest

from nse_pipeline.backtest.monthly_universe import coverage, load_monthly_sets


@pytest.mark.parametrize(
    ("alias", "nominal", "expected_months", "first", "last", "size_min", "size_max"),
    [
        ("msc400", 400, 57, "2017-07-31", "2022-03-31", 400, 400),
        ("nifty500", 500, 107, "2013-05-31", "2022-03-31", 500, 501),
    ],
)
def test_monthly_sets_and_coverage(alias, nominal, expected_months,
                                   first, last, size_min, size_max):
    sets = load_monthly_sets(alias)

    assert isinstance(sets, dict)
    assert len(sets) == expected_months
    assert all(isinstance(month, date) for month in sets)
    assert all(isinstance(members, set) and members for members in sets.values())
    sizes = [len(members) for members in sets.values()]
    assert min(sizes) == size_min
    assert max(sizes) == size_max
    assert all(nominal <= size <= nominal + 1 for size in sizes)

    assert coverage(alias) == {
        "index": "NIFTY_MidSmallcap_400" if alias == "msc400" else "NIFTY_500",
        "months": expected_months,
        "first": first,
        "last": last,
        "size_min": size_min,
        "size_max": size_max,
    }
