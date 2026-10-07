"""Deterministic, point-in-time market metrics. No forecasting or allocation rules."""

from collections import Counter
from datetime import datetime
import math
import re
from statistics import mean

from .connections import ProbeError, SHANGHAI


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def market_breadth(pages: list[dict], bars: list[dict], expected_day: str) -> dict:
    if not pages or not bars:
        raise ProbeError("empty_market_data")
    records, totals = [], set()
    timestamps = []
    for page in pages:
        if page.get("code") != 0 or not isinstance(page.get("data"), dict):
            raise ProbeError("invalid_market_page")
        data = page["data"]
        if type(data.get("total")) is not int or not isinstance(data.get("item"), list):
            raise ProbeError("invalid_market_page")
        totals.add(data["total"])
        records.extend(data["item"])
        if not number(data.get("timestamp")):
            raise ProbeError("missing_snapshot_ready_time")
        timestamps.append(data["timestamp"])
    if len(totals) != 1 or len(records) != next(iter(totals)):
        raise ProbeError("incomplete_or_changing_universe")
    codes = [row.get("thscode") for row in records]
    if any(not isinstance(code, str) or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code) for code in codes):
        raise ProbeError("unexpected_market_instrument")
    if len(set(codes)) != len(codes):
        raise ProbeError("duplicate_market_instrument")
    unique_keys, daily, daily_turnover = set(), {}, {}
    for row in bars:
        if row.get("currency") != "CNY" or row.get("interval") != "1d" or row.get("adjusted") != "none":
            raise ProbeError("unexpected_daily_bar_convention")
        if not number(row.get("date_ms")) or not isinstance(row.get("thscode"), str):
            raise ProbeError("invalid_daily_bar")
        key = (row["thscode"], row["date_ms"])
        if key in unique_keys:
            raise ProbeError("duplicate_daily_bar")
        unique_keys.add(key)
        day = datetime.fromtimestamp(row["date_ms"] / 1000, SHANGHAI).date().isoformat()
        if day > expected_day:
            raise ProbeError("future_daily_bar")
        if number(row.get("turnover")) and row["turnover"] >= 0:
            item = daily_turnover.setdefault(day, {"date": day, "turnover_cny": 0, "valid_count": 0})
            item["turnover_cny"] += row["turnover"]
            item["valid_count"] += 1
        if day == expected_day:
            daily[row["thscode"]] = row
    if not daily:
        raise ProbeError("daily_market_data_not_ready")
    excluded, exchanges, counts, eligible_codes = Counter(), Counter(), Counter(), []
    precision_differences = 0
    for row in records:
        code = row["thscode"]
        exchanges[code.rsplit(".", 1)[1]] += 1
        bar = daily.get(code)
        if not bar:
            excluded["missing_dated_bar"] += 1
            continue
        prices = [("last_price", "close_price"), ("open_price", "open_price"), ("high_price", "high_price"), ("low_price", "low_price")]
        if any(not number(row.get(a)) or not number(bar.get(b)) or abs(row[a] - bar[b]) > 1e-6 for a, b in prices):
            excluded["price_date_link_mismatch"] += 1
            continue
        # Explicit numerical precision tolerance, not an assertion of exact equality.
        if any(not number(row.get(k)) or not number(bar.get(k)) or abs(row[k] - bar[k]) > max(1, abs(bar[k]) * 1e-6) for k in ("volume", "turnover")):
            excluded["activity_date_link_mismatch"] += 1
            continue
        if any(row[k] != bar[k] for k in ("volume", "turnover")):
            precision_differences += 1
        if row["volume"] <= 0 or row["turnover"] <= 0:
            excluded["no_positive_trading_activity"] += 1
            continue
        previous, current, change = row.get("prev_price"), row.get("last_price"), row.get("price_change_ratio_pct")
        if not all(number(value) for value in (previous, current, change)) or previous <= 0 or current <= 0:
            excluded["invalid_price_change_inputs"] += 1
            continue
        if abs((current / previous - 1) * 100 - change) > 0.0001:
            excluded["price_change_conflict"] += 1
            continue
        counts["advancers" if change > 0 else "decliners" if change < 0 else "unchanged"] += 1
        eligible_codes.append(code)
    eligible = len(eligible_codes)
    if not eligible:
        raise ProbeError("no_eligible_breadth_samples")
    coverage = eligible / len(records)
    return {
        "date": expected_day, "universe_count": len(records), "dated_bar_count": len(daily),
        "eligible_count": eligible, "excluded_count": len(records) - eligible,
        "excluded_reasons": dict(excluded), "exchange_counts": dict(exchanges),
        "advancers": counts["advancers"], "decliners": counts["decliners"], "unchanged": counts["unchanged"],
        "advancing_ratio_pct": counts["advancers"] / eligible * 100, "coverage_pct": coverage * 100,
        "quality": "usable_with_limits" if coverage >= .98 else "insufficient_coverage",
        "coverage_rule": "研究描述要求有效覆盖至少98%，这是工程质量门槛，不是预测阈值。",
        "ready_time_range_ms": [min(timestamps), max(timestamps)],
        "date_link": "快照OHLC与带日期日K匹配；量额按max(1,abs(日K值)*1e-6)容差核对。",
        "activity_precision_difference_count": precision_differences,
        "sample_definition": "供应商A股代码表中的沪深北标的，限定有当日日K、价量匹配、正成交且涨跌幅可核对的样本。",
        "limitations": ["数据就绪时间不是交易时点，日期来自关联日K。", "无成交/缺数据不直接归类为停牌。",
                        "上涨占比分母为有效样本，不是代码表总数。", "同源数据对齐不等于独立来源验证。"],
        "turnover_history": sorted(daily_turnover.values(), key=lambda item: item["date"]),
        "turnover_sample_note": "各日样本数量不同，数据仅覆盖本文件列出的交易日，不能称整月统计。",
    }


def index_metrics(rows: list[dict], *, expected_days: list[str], window: int, allow_gaps: bool = False) -> dict:
    if not rows:
        raise ProbeError("empty_index_history")
    ordered = sorted(rows, key=lambda row: row["date_ms"])
    dates = [datetime.fromtimestamp(row["date_ms"] / 1000, SHANGHAI).date().isoformat() for row in ordered]
    if len(set(dates)) != len(dates):
        raise ProbeError("incomplete_index_history")
    missing, requested = [], window
    if allow_gaps:
        # A stock may be suspended or newly listed: missing sessions are reported, never filled in.
        if not set(dates) <= set(expected_days):
            raise ProbeError("unexpected_bar_date")
        window_days = expected_days[-window - 1:]
        missing = [day for day in window_days if day not in dates]
        if sum(day in window_days for day in dates) < max(3, (window + 1) // 2):
            raise ProbeError("insufficient_stock_history")
        window = sum(day >= window_days[0] for day in dates) - 1
    elif dates != expected_days:
        raise ProbeError("incomplete_index_history")
    closes = [row.get("close_price") for row in ordered]
    if any(not number(value) or value <= 0 for value in closes):
        raise ProbeError("invalid_index_level")
    if len(closes) < window + 1:
        raise ProbeError("insufficient_index_history")
    chosen = closes[-window - 1:]
    peak, max_drawdown = chosen[0], 0.0
    for value in chosen:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, (value / peak - 1) * 100)
    return {"start_date": dates[-window - 1], "end_date": dates[-1], "return_intervals": requested, "missing_dates": missing,
            "window_return_pct": (chosen[-1] / chosen[0] - 1) * 100,
            "max_drawdown_pct": max_drawdown, "last_close": closes[-1],
            "ma20": mean(closes[-20:]) if len(closes) >= 20 else None,
            "ma60": mean(closes[-60:]) if len(closes) >= 60 else None,
            "unit": "点", "return_basis": "指数点位首末之比，不含分红再投资；不是未来收益预测。",
            "series": [{"date": date, "close": close, "normalized": close / chosen[0] * 100}
                       for date, close in zip(dates[-window - 1:], chosen)]}
