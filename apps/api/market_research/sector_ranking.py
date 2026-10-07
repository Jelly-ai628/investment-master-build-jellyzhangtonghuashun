"""Same-family sector ranking, based on completed-period returns, not forecasts."""

from .connections import ProbeError


def rank_sectors(rows, benchmark_return, expected_count):
    if not rows:
        raise ProbeError("no_sector_history")
    if len({row["code"] for row in rows}) != len(rows):
        raise ProbeError("duplicate_sector")
    windows = {(row["start_date"], row["end_date"]) for row in rows}
    if len(windows) != 1:
        raise ProbeError("incomparable_sector_windows")
    ordered = sorted(rows, key=lambda row: (-row["window_return_pct"], row["code"]))
    return [{**row, "rank": i + 1, "relative_return_pp": row["window_return_pct"] - benchmark_return} for i, row in enumerate(ordered)]


def ranking_answer(data):
    leaders = data["ranking"][:5]
    names = "、".join(row["name"] for row in leaders)
    all_negative = all(row["window_return_pct"] < 0 for row in leaders)
    wording = "相对抗跌、但仍为下跌" if all_negative else "累计表现居前"
    scope = "在本次有效行业样本中" if data["valid_count"] < data["expected_count"] else "在本次同口径行业样本中"
    return f"按{data['start_date']}至{data['end_date']}的{data['window']}个交易日累计表现比较，{scope}{names}{wording}。这是已发生的行业指数表现，不是后续上涨判断。"
