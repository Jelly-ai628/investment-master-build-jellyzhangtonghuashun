import pytest

from market_research.connections import ProbeError
from market_research.insights import InsightSelection, candidates, render_selection


def sample():
    return {
        "ev_index": {"id": "ev_index", "kind": "index_history", "data": {
            "name": "样本指数", "code": "TEST", "start_date": "2026-09-01", "end_date": "2026-09-30",
            "window_return_pct": -5, "last_close": 95, "ma20": 97, "ma60": 90,
        }},
        "ev_breadth": {"id": "ev_breadth", "kind": "market_breadth", "data": {
            "date": "2026-09-30", "quality": "usable_with_limits", "advancers": 4, "decliners": 5,
            "advancing_ratio_pct": 40, "coverage_pct": 99, "excluded_count": 1, "eligible_count": 10,
            "turnover_history": [{"date": "2026-09-28", "turnover_cny": 300},
                                 {"date": "2026-09-29", "turnover_cny": 100},
                                 {"date": "2026-09-30", "turnover_cny": 200}],
        }},
    }


def test_second_lowest_and_daily_rebound_are_exact():
    insights, _ = candidates(sample())
    assert "上升100.00%" in insights["turnover"]["text"]
    assert "第2低" in insights["turnover"]["text"]
    assert "低于20日均线、高于60日均线" in insights["index_0"]["text"]
    assert "最低" not in insights["turnover"]["text"]


def test_selection_cannot_invent_or_mutate_claims():
    insights, tensions = candidates(sample())
    selected = InsightSelection(summary_ids=["turnover"], tension_id="different_windows", detail_ids=["index_0", "turnover"])
    report = render_selection(selected, insights, tensions)
    assert report["summary"] == insights["turnover"]["text"]
    assert report["summary_evidence_ids"] == ["ev_breadth"]
    with pytest.raises(ProbeError, match="unknown_insight"):
        render_selection(selected.model_copy(update={"summary_ids": ["made_up"]}), insights, tensions)
    with pytest.raises(ProbeError, match="summary_without"):
        render_selection(selected.model_copy(update={"detail_ids": ["index_0"]}), insights, tensions)


def test_low_coverage_has_no_breadth_insight():
    data = sample()
    data["ev_breadth"]["data"]["quality"] = "insufficient_coverage"
    insights, _ = candidates(data)
    assert "participation" not in insights


def test_valuation_and_events_are_bounded_claims():
    data = sample()
    _, tensions = candidates(data)
    assert "估值、事件、宏观" in tensions["evidence_gaps"]["text"]
    data["val"] = {"id": "val", "kind": "valuation", "data": {"name": "沪深300", "date": "2026-09-30",
        "items": [{"metric": "市盈率（PE，TTM）", "value": 12.345, "raw_value": "12.345"}],
        "missing_metrics": [{"metric": "市净率（PB）", "reason": "valuation_date_unverified"}]}}
    data["evt"] = {"id": "evt", "kind": "events", "data": {"start_date": "2026-09-22", "date": "2026-09-30",
        "records": [{"published_at": "2026-09-29", "title": "央行开展逆回购操作", "contains_plan": True}]}}
    insights, tensions = candidates(data)
    valuation = next(item for item in insights.values() if item["dimension"] == "估值")
    assert "12.35倍" in valuation["text"] and "市净率（PB）未通过核验" in valuation["text"]
    assert "高位或低位" in valuation["text"] and valuation["evidence_ids"] == ["val"]
    events = next(item for item in insights.values() if item["dimension"] == "重要事件")
    assert "计划不等于已经实施" in events["text"] and "不能证明" in events["text"]
    assert "估值缺少历史分位" in tensions["evidence_gaps"]["text"]


def test_history_and_risk_are_separate_evidence_kinds():
    data = sample()
    data["history"] = {"id": "history", "kind": "historical_comparison", "data": {
        "name": "样本指数", "current": data["ev_index"]["data"],
        "previous": {"start_date": "2026-08-01", "end_date": "2026-08-31"}, "return_difference_pp": -2,
    }}
    data["risk"] = {"id": "risk", "kind": "sentiment", "data": {"date": "2026-09-30", "limit_up_count": 56, "limit_down_count": 11}}
    insights, _ = candidates(data)
    assert any(item["dimension"] == "历史阶段" and item["evidence_ids"] == ["history"] for item in insights.values())
    assert any(item["dimension"] == "风险变量" and item["evidence_ids"] == ["risk"] for item in insights.values())
