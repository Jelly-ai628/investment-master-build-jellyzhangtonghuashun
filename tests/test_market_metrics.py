import copy
from datetime import datetime

import pytest

from market_research.connections import ProbeError, SHANGHAI
from market_research.market_metrics import market_breadth, index_metrics


def samples():
    stamp = int(datetime(2026, 9, 30, tzinfo=SHANGHAI).timestamp() * 1000)
    records, bars = [], []
    for code, close in [("000001.SZ", 11), ("600000.SH", 9), ("920001.BJ", 10)]:
        records.append(dict(thscode=code, last_price=close, open_price=10, high_price=max(close, 10), low_price=min(close, 10),
                            volume=100, turnover=1000, prev_price=10, price_change_ratio_pct=(close / 10 - 1) * 100))
        bars.append(dict(thscode=code, date_ms=stamp, currency="CNY", interval="1d", adjusted="none",
                         close_price=close, open_price=10, high_price=max(close, 10), low_price=min(close, 10), volume=100, turnover=1000))
    return [{"code": 0, "data": {"total": 3, "timestamp": stamp + 7 * 86400000, "item": records}}], bars


def test_breadth_uses_dated_bars_and_valid_denominator():
    pages, bars = samples()
    result = market_breadth(pages, bars, "2026-09-30")
    assert (result["advancers"], result["decliners"], result["unchanged"]) == (1, 1, 1)
    assert result["date"] == "2026-09-30"  # ready time is October 7, never used as trade date
    assert result["coverage_pct"] == 100
    assert result["advancing_ratio_pct"] == pytest.approx(100 / 3)
    assert set(result["exchange_counts"]) == {"SH", "SZ", "BJ"}


def test_missing_dated_bar_is_not_unchanged_or_suspended():
    pages, bars = samples()
    result = market_breadth(pages, bars[:-1], "2026-09-30")
    assert result["unchanged"] == 0 and result["eligible_count"] == 2
    assert result["excluded_reasons"] == {"missing_dated_bar": 1}
    assert result["quality"] == "insufficient_coverage"


@pytest.mark.parametrize("problem", ["missing_page", "duplicate", "changing_total"])
def test_pagination_integrity(problem):
    pages, bars = samples()
    if problem == "missing_page":
        pages[0]["data"]["item"].pop()
    elif problem == "duplicate":
        pages[0]["data"]["item"][1] = pages[0]["data"]["item"][0]
    else:
        pages.append({"code": 0, "data": {"total": 4, "item": [], "timestamp": 0}})
    with pytest.raises(ProbeError):
        market_breadth(pages, bars, "2026-09-30")


def test_activity_rounding_is_explicit_and_large_mismatch_excluded():
    pages, bars = samples()
    pages[0]["data"]["item"][0]["turnover"] += .5
    pages[0]["data"]["item"][1]["turnover"] += 100
    result = market_breadth(pages, bars, "2026-09-30")
    assert result["activity_precision_difference_count"] == 1
    assert result["excluded_reasons"] == {"activity_date_link_mismatch": 1}


def test_bad_change_does_not_enter_breadth():
    pages, bars = samples()
    pages[0]["data"]["item"][0]["price_change_ratio_pct"] = 50
    result = market_breadth(pages, bars, "2026-09-30")
    assert result["advancers"] == 0
    assert result["excluded_reasons"]["price_change_conflict"] == 1


def test_no_activity_is_not_called_suspended():
    pages, bars = samples()
    pages[0]["data"]["item"][0]["volume"] = bars[0]["volume"] = 0
    result = market_breadth(pages, bars, "2026-09-30")
    assert result["excluded_reasons"] == {"no_positive_trading_activity": 1}


def test_future_and_duplicate_daily_bars_rejected():
    pages, bars = samples()
    with pytest.raises(ProbeError, match="future_daily_bar"):
        market_breadth(pages, bars, "2026-09-29")
    with pytest.raises(ProbeError, match="duplicate_daily_bar"):
        market_breadth(pages, bars + [copy.deepcopy(bars[0])], "2026-09-30")


def test_index_returns_are_compounded_not_summed():
    dates = ["2026-09-28", "2026-09-29", "2026-09-30"]
    rows = [{"date_ms": int(datetime.fromisoformat(day).replace(tzinfo=SHANGHAI).timestamp() * 1000), "close_price": close}
            for day, close in zip(dates, [100, 90, 99])]
    result = index_metrics(rows, expected_days=dates, window=2)
    assert result["window_return_pct"] == pytest.approx(-1)
    assert result["max_drawdown_pct"] == pytest.approx(-10)
    assert result["ma60"] is None
    with pytest.raises(ProbeError, match="incomplete_index_history"):
        index_metrics(rows[:-1], expected_days=dates, window=2)
