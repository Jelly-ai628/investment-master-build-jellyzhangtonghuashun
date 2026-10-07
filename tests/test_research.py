import asyncio
import json

import httpx
import pytest

from market_research.connections import ProbeError, Settings
from market_research.research import ResearchRequest, asks_for_prohibited_output, run_research


def test_prohibited_request_routes_without_blocking_negative_constraint():
    assert asks_for_prohibited_output("预测明天会不会上涨")
    assert asks_for_prohibited_output("告诉我仓位怎么配")
    assert not asks_for_prohibited_output("不要仓位建议，只研究市场宽度")
    assert not asks_for_prohibited_output("最近20个交易日行情结构如何？")
    # Stock research makes "can I buy it" the most likely boundary question.
    for question in ("贵州茅台现在可以买入吗？", "宁德时代要不要卖", "这只股票值不值得入手", "能不能抄底银行"):
        assert asks_for_prohibited_output(question), question
    assert not asks_for_prohibited_output("贵州茅台最近20个交易日相对沪深300表现如何？")


def test_data_question_is_not_answered_with_a_definition():
    from market_research.intent import ResearchIntent, override_explanation
    misread = ResearchIntent(task="explanation", explanation_topic="breadth")
    # Live acceptance run 2026-10-07: this question was classified as a breadth definition.
    assert override_explanation(misread, "末日涨跌停分布有哪些风险信号？").task == "risk_research"
    assert override_explanation(misread, "什么是市场宽度？").task == "explanation"
    assert override_explanation(misread, "涨停是什么意思？").task == "explanation"


def test_boundary_route_does_not_invent_tools_or_require_keys(tmp_path):
    result = asyncio.run(run_research(ResearchRequest(question="预测明天会不会上涨"), Settings({}), tmp_path))
    assert result["status"] == "scope_guidance"
    assert result["facts"] == [] and result["evidence"] == []
    assert result["model"] is None
    # Rendered as a plain message, not as a research report with empty sections.
    assert result["presentation"] == "message" and result["narrative"]["author"] == "rules"


def stock_fixture(fetched, known):
    class FakeData:
        def __init__(self, *args):
            self.evidence, self.securities = {}, {}
            self.expected_day = self.latest_day = "2026-09-30"
        async def initialize(self, date):
            pass
        async def load_industries(self):
            return {"白酒": "881125.TI"}
        async def resolve_security(self, entity, hint=None):
            if entity not in known:
                raise ProbeError("security_not_resolved")
            self.securities[known[entity]] = "贵州茅台"
            return known[entity]
        async def get_index_history(self, code, window):
            fetched.append(code)
            stock = code == "600519.SH"
            item = {"id": "s" if stock else "i", "kind": "index_history", "data": {
                "name": "贵州茅台" if stock else "沪深300", "code": code, "instrument_type": "stock" if stock else "index",
                "start_date": "2026-09-01", "end_date": "2026-09-30", "window_return_pct": -2 if stock else -5,
                "max_drawdown_pct": -4, "last_close": 1258.62 if stock else 95, "ma20": 1270 if stock else 97, "ma60": None,
            }, "provenance": {"source": "synthetic fixture", "observation_date": "2026-09-30"}}
            self.evidence[item["id"]] = item
            return item
        async def get_stock_valuation(self, code):
            fetched.append("valuation:" + code)
            raise ProbeError("valuation_snapshot_stale")
    return FakeData


def mock_deepseek(monkeypatch, handler):
    original = httpx.AsyncClient
    class MockClient(original):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", MockClient)


@pytest.mark.parametrize("intent", ['{"task":"single_security","entities":["贵州茅台"]}', '{"task":"index_research","entities":["贵州茅台"]}'])
def test_named_stock_is_researched_with_its_own_prices(tmp_path, monkeypatch, intent):
    from market_research import research

    fetched, calls = [], []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        first = body["tools"][0]["function"]["name"]
        names = {tool["function"]["name"] for tool in body["tools"]}
        if first == "identify_research_intent":
            tool_calls = [{"id": "intent", "type": "function", "function": {"name": first, "arguments": intent}}]
        elif "get_stock_history" in names:
            tool_calls = [{"id": "a", "type": "function", "function": {"name": "get_stock_history", "arguments": '{"code":"600519.SH","purpose":"该股行情"}'}},
                          {"id": "b", "type": "function", "function": {"name": "get_index_history", "arguments": '{"code":"000300.SH","purpose":"大盘对照"}'}},
                          {"id": "c", "type": "function", "function": {"name": "get_stock_valuation", "arguments": '{"code":"600519.SH","purpose":"估值快照"}'}}]
        else:
            # The first selection leads with the benchmark only; it must be rejected for a stock question.
            summary = ["index_1"] if sum(c["tools"][0]["function"]["name"] == "select_verified_insights" for c in calls) == 1 else ["relative_0"]
            selection = {"summary_ids": summary, "tension_id": "stock_vs_market", "detail_ids": list(dict.fromkeys(summary + ["index_0", "relative_0"]))}
            tool_calls = [{"id": "sel", "type": "function", "function": {"name": "select_verified_insights", "arguments": json.dumps(selection)}}]
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": tool_calls}}]})

    monkeypatch.setattr(research, "MarketData", stock_fixture(fetched, {"贵州茅台": "600519.SH"}))
    mock_deepseek(monkeypatch, handler)
    result = asyncio.run(run_research(ResearchRequest(question="贵州茅台的涨跌和趋势如何？"), Settings({"DEEPSEEK_API_KEY": "test", "DEEPSEEK_MODEL": "test"}), tmp_path))
    assert result["status"] == "completed_with_limits"
    assert fetched == ["600519.SH", "000300.SH", "valuation:600519.SH"]
    assert result["market_state"]["label"] == "贵州茅台区间回落，强于沪深300"
    assert result["narrative"]["summary"].startswith("同一区间贵州茅台变化-2.00%，沪深300变化-5.00%")
    assert any(e["type"] == "validation_retry" and e["reason"] == "security_question_not_answered_first" for e in result["events"])
    dimensions = {item["name"]: item["status"] for item in result["dimensions"]}
    assert dimensions["估值"] == "已调用，未通过核验（valuation_snapshot_stale）"
    assert "前复权收盘价" in result["facts"][0]["text"]


def test_unmatched_named_object_gets_explicit_guidance_not_default_index(tmp_path, monkeypatch):
    from market_research import research

    fetched, calls = [], []
    def handler(request):
        calls.append(json.loads(request.content))
        tool_calls = [{"id": "intent", "type": "function", "function": {"name": "identify_research_intent", "arguments": '{"task":"index_research","entities":["不存在公司"]}'}}]
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": tool_calls}}]})

    monkeypatch.setattr(research, "MarketData", stock_fixture(fetched, {}))
    mock_deepseek(monkeypatch, handler)
    result = asyncio.run(run_research(ResearchRequest(question="不存在公司的走势如何？"), Settings({"DEEPSEEK_API_KEY": "test", "DEEPSEEK_MODEL": "test"}), tmp_path))
    assert result["status"] == "scope_guidance" and "不存在公司" in result["narrative"]["summary"]
    # No data was fetched for a substituted index, and planning never ran.
    assert result["evidence"] == [] and fetched == [] and len(calls) == 1


@pytest.mark.parametrize("invalid_selection,valuation_ok", [(False, False), (True, False), (False, True)])
def test_agent_plans_calls_and_only_renders_verified_selections(tmp_path, monkeypatch, invalid_selection, valuation_ok):
    from market_research import research

    class FakeData:
        def __init__(self, *args):
            self.evidence = {}
            self.expected_day = self.latest_day = "2026-09-30"
        async def initialize(self, date):
            pass
        async def get_market_breadth(self):
            item = {"id": "b", "kind": "market_breadth", "data": {
                "date": "2026-09-30", "quality": "usable_with_limits", "eligible_count": 10,
                "advancers": 4, "decliners": 5, "unchanged": 1, "advancing_ratio_pct": 40,
                "coverage_pct": 100, "excluded_count": 0, "turnover_history": [],
            }, "provenance": {"source": "synthetic fixture", "observation_date": "2026-09-30"}}
            self.evidence["b"] = item
            return item
        async def get_index_history(self, code, window):
            item = {"id": "i", "kind": "index_history", "data": {
                "name": "沪深300", "code": code, "start_date": "2026-09-01", "end_date": "2026-09-30",
                "window_return_pct": -5, "max_drawdown_pct": -6, "last_close": 95, "ma20": 97, "ma60": 99,
            }, "provenance": {"source": "synthetic fixture", "observation_date": "2026-09-30"}}
            self.evidence["i"] = item
            return item
        async def get_valuation_context(self, code):
            if not valuation_ok:
                raise ProbeError("valuation_date_unverified")
            item = {"id": "v", "kind": "valuation", "data": {"name": "沪深300", "code": code, "date": "2026-09-30",
                "items": [{"metric": "市盈率（PE，TTM）", "value": 13.2, "raw_value": "13.2"}], "missing_metrics": []},
                "provenance": {"source": "synthetic fixture", "observation_date": "2026-09-30"}}
            self.evidence["v"] = item
            return item
        async def get_event_context(self):
            raise ProbeError("news_original_not_verified")
        async def get_risk_context(self):
            raise ProbeError("sentiment_date_link_unverified")

    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["tools"][0]["function"]["name"] == "identify_research_intent":
            tool_calls = [{"id": "intent", "type": "function", "function": {"name": "identify_research_intent", "arguments": '{"task":"market_overview","entities":[]}'}}]
        elif body["tools"][0]["function"]["name"] == "get_market_breadth":
            tool_calls = [{"id": "one", "type": "function", "function": {"name": "get_market_breadth", "arguments": '{"purpose":"核对市场宽度"}'}},
                          {"id": "two", "type": "function", "function": {"name": "get_index_history", "arguments": '{"code":"000300.SH","purpose":"核对指数"}'}},
                          {"id": "val", "type": "function", "function": {"name": "get_valuation_context", "arguments": '{"code":"000300.SH","purpose":"核对估值"}'}},
                          {"id": "evt", "type": "function", "function": {"name": "get_event_context", "arguments": '{"purpose":"核对事件"}'}},
                          {"id": "risk", "type": "function", "function": {"name": "get_risk_context", "arguments": '{"purpose":"核对涨跌停"}'}}]
        else:
            # Live acceptance 2026-10-07: the model put the tension id into summary_ids. That id is dropped, not rendered.
            summary = ["invented"] if invalid_selection else ["index_0", "different_windows"] if valuation_ok else ["index_0"]
            selection = {"summary_ids": summary, "tension_id": "different_windows", "detail_ids": ["index_0", "participation"]}
            tool_calls = [{"id": "three", "type": "function", "function": {"name": "select_verified_insights", "arguments": json.dumps(selection)}}]
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": tool_calls}}]})

    original = httpx.AsyncClient
    class MockClient(original):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(research, "MarketData", FakeData)
    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    (tmp_path / "research-skills").mkdir()
    (tmp_path / "research-skills" / "market-state.md").write_text("Synthetic test instructions.")
    result = asyncio.run(run_research(ResearchRequest(question="研究市场状态"), Settings({"DEEPSEEK_API_KEY": "test", "DEEPSEEK_MODEL": "test"}), tmp_path))
    assert len(result["evidence"]) == (3 if valuation_ok else 2)
    assert len([event for event in result["events"] if event["type"] == "tool_completed"]) == 5
    # Failed optional providers are reported as attempted, never silently treated as normal.
    failed = {item["tool"] for item in result["tool_failures"]}
    assert failed == {"get_event_context", "get_risk_context"} | (set() if valuation_ok else {"get_valuation_context"})
    dimensions = {item["name"]: item["status"] for item in result["dimensions"]}
    assert dimensions["估值"] == ("已取得截至日PE/PB；无历史分位" if valuation_ok else "已调用，未通过核验（valuation_date_unverified）")
    assert dimensions["重要事件"] == "已调用，未通过核验（news_original_not_verified）"
    assert dimensions["情绪"] == "已调用，未通过核验（sentiment_date_link_unverified）"
    if valuation_ok:
        # The model did not select the valuation insight; it is listed apart from the answer instead of padding it.
        assert [item["dimension"] for item in result["narrative"]["supplements"]] == ["估值"]
        assert "估值" not in [item["dimension"] for item in result["narrative"]["interpretations"]]
        assert "估值" not in " ".join(result["confidence"]["reasons"])
    planning_tools = {tool["function"]["name"] for tool in calls[1]["tools"]}
    assert {"get_valuation_context", "get_event_context", "get_risk_context"} <= planning_tools
    if invalid_selection:
        assert result["status"] == "facts_only" and result["narrative"] is None
        # intent + planning + three rejected narrative drafts + two rejected selections
        assert len(calls) == 7
    else:
        assert result["status"] == "completed_with_limits"
        assert result["narrative"]["summary_evidence_ids"] == ["i"]
        assert "首末点位变化-5.00%" in result["narrative"]["summary"]
        # The mock never writes a narrative, so every draft is rejected and the ID selection is the fallback.
        assert len(calls) == 6 and result["narrative"]["author"] == "selection"
        assert sum(e["type"] == "validation_retry" and e["reason"] == "narrative_unverified" for e in result["events"]) == 3
        assert result["selected_insights"]["dropped_unknown_ids"] == (["different_windows"] if valuation_ok else [])


@pytest.mark.parametrize("adjust", ["forward", None])
def test_stock_lookup_uses_the_code_table_and_prices_must_be_forward_adjusted(tmp_path, adjust):
    from datetime import datetime, timedelta
    from market_research.connections import SHANGHAI
    from market_research.market_data import MarketData

    days = [datetime(2026, 7, 1, tzinfo=SHANGHAI) + timedelta(days=i) for i in range(61)]
    table = [{"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台", "asset_type": "a-share"},
             {"thscode": "000799.SZ", "ticker": "000799", "name": "酒鬼酒", "asset_type": "a-share"},
             {"thscode": "600307.SH", "ticker": "600307", "name": "酒钢宏兴", "asset_type": "a-share"},
             {"thscode": "600941.SH", "ticker": "600941", "name": "XD中国移", "asset_type": "a-share"}]
    seen = []
    def handler(request):
        params = dict(request.url.params)
        seen.append((request.url.path, params))
        if request.url.path == "/api/meta/tickers/search":
            query = params["q"]
            # Live probe 2026-10-07: the table name of 600941 is the exchange short name "XD中国移", so "中国移动" finds nothing.
            rows = [row for row in table if query in (row["name"], row["ticker"]) or query in row["name"]]
            if query == "茅台":
                rows = table[:2]
            return httpx.Response(200, json={"code": 0, "data": {"item": rows}})
        if request.url.path == "/api/a-share/prices/snapshot":
            listed = [code for code in params["thscodes"].split(",") if code == "601857.SH"]
            return httpx.Response(200, json={"code": 0, "data": {"item": [{"thscode": code} for code in listed]}})
        bars = [{"date_ms": int(day.timestamp() * 1000), "close_price": 1200 + i} for i, day in enumerate(days)]
        data = {"item": bars, **({"adjust": adjust} if adjust else {})}
        return httpx.Response(200, json={"code": 0, "request_id": "r", "data": data})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            market = MarketData(Settings({"FUYAO_API_KEY": "test"}), tmp_path, client)
            market.calendar, market.expected_day = days, days[-1].date().isoformat()
            assert await market.resolve_security("600519") == "600519.SH"
            # A partial name resolves only when one listed name contains it, and the match is recorded as non-exact.
            assert await market.resolve_security("茅台") == "600519.SH" and market.matches["600519.SH"]["exact"] is False
            with pytest.raises(ProbeError, match="ambiguous_security"):
                await market.resolve_security("酒")
            assert market.ambiguous["酒"] == ["酒鬼酒", "酒钢宏兴"]
            # A truncated, marked short name is found by its first characters and shown under the user's name.
            assert await market.resolve_security("中国移动") == "600941.SH"
            assert market.securities["600941.SH"] == "中国移动" and market.matches["600941.SH"]["listed_name"] == "XD中国移"
            assert market.matches["600941.SH"]["exact"] is False and "XD中国移" in market.matches["600941.SH"]["via"]
            # A ticker hint whose table name disagrees with the user's name is refused.
            with pytest.raises(ProbeError, match="security_not_resolved"):
                await market.resolve_security("工商银行", "600519")
            # When the search knows neither the name nor the ticker, a listed code is used only with an explicit "name not verified" flag.
            assert await market.resolve_security("中国石油", "601857") == "601857.SH"
            assert market.matches["601857.SH"]["name_verified"] is False and "未能核对" in market.matches["601857.SH"]["via"]
            with pytest.raises(ProbeError, match="security_not_resolved"):
                await market.resolve_security("不存在公司", "688999")
            return await market.get_index_history("600519.SH", 20)

    if adjust is None:
        with pytest.raises(ProbeError, match="stock_adjustment_unverified"):
            asyncio.run(scenario())
        return
    item = asyncio.run(scenario())
    assert item["data"]["instrument_type"] == "stock" and item["data"]["name"] == "贵州茅台"
    assert item["provenance"]["endpoint"] == "/api/a-share/prices/historical"
    assert seen[-1][1]["adjust"] == "forward"
    # Stocks are not re-checked against the index catalogue.
    assert not any(params.get("asset_type") == "a-share-index" for _, params in seen)


def test_suspended_days_do_not_block_stock_research():
    from datetime import datetime, timedelta
    from market_research.connections import SHANGHAI
    from market_research.market_metrics import index_metrics

    days = [datetime(2026, 7, 1, tzinfo=SHANGHAI) + timedelta(days=i) for i in range(61)]
    expected = [day.date().isoformat() for day in days]
    bars = [{"date_ms": int(day.timestamp() * 1000), "close_price": 100 + i} for i, day in enumerate(days) if i not in (45, 46)]
    with pytest.raises(ProbeError, match="incomplete_index_history"):
        index_metrics(bars, expected_days=expected, window=20)
    metrics = index_metrics(bars, expected_days=expected, window=20, allow_gaps=True)
    assert metrics["missing_dates"] == [expected[45], expected[46]] and metrics["return_intervals"] == 20
    assert metrics["start_date"] == expected[40] and metrics["end_date"] == expected[-1]
    assert abs(metrics["window_return_pct"] - (160 / 140 - 1) * 100) < 1e-9
    # Too few sessions inside the window is reported, not stretched into a result.
    with pytest.raises(ProbeError, match="insufficient_stock_history"):
        index_metrics(bars[:45], expected_days=expected, window=20, allow_gaps=True)


def test_stock_picking_is_not_a_sector_ranking():
    from market_research.intent import ResearchIntent, override_explanation
    # Live 2026-10-07: "哪些表现比较好的股票标的" was planned as an industry ranking.
    misread = ResearchIntent(task="sector_ranking")
    assert override_explanation(misread, "请问A股最近有哪些表现比较好的股票标的").task == "chat"
    assert override_explanation(misread, "最近有哪些表现较强的行业板块？").task == "sector_ranking"


def test_a_stalled_read_is_retried_once():
    from market_research.connections import request_json
    calls = []
    def handler(request):
        calls.append(request.method)
        if len(calls) == 1:
            raise httpx.ReadTimeout("stalled", request=request)
        return httpx.Response(200, json={"code": 0})
    async def scenario(method):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await request_json(client, method, "https://example.test/x")
    assert asyncio.run(scenario("GET")) == {"code": 0} and calls == ["GET", "GET"]
    calls.clear()
    # Model calls are not repeated automatically.
    with pytest.raises(ProbeError, match="timeout"):
        asyncio.run(scenario("POST"))
    assert calls == ["POST"]
