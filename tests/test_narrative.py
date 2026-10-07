import asyncio
import json

import httpx
import pytest

from market_research.connections import Settings
from market_research.insights import candidates
from market_research.narrative import GroundedNarrative, NarrativeRejected, check_narrative, writer_tool
from market_research.research import ResearchRequest, asks_for_prohibited_output, run_research


def evidence():
    return {
        "i": {"id": "i", "kind": "index_history", "data": {"name": "沪深300", "code": "000300.SH", "start_date": "2026-09-01", "end_date": "2026-09-30",
              "window_return_pct": -5, "max_drawdown_pct": -6, "last_close": 95, "ma20": 97, "ma60": 99},
              "provenance": {"source": "synthetic fixture", "observation_date": "2026-09-30"}},
        "b": {"id": "b", "kind": "market_breadth", "data": {"date": "2026-09-30", "quality": "usable_with_limits", "eligible_count": 10,
              "advancers": 4, "decliners": 5, "unchanged": 1, "advancing_ratio_pct": 40, "coverage_pct": 100, "excluded_count": 0, "turnover_history": []},
              "provenance": {"source": "synthetic fixture", "observation_date": "2026-09-30"}},
    }


def draft(**changes):
    value = {
        "headline": {"text": "沪深300区间回落，末日多数股票也在下跌。", "insight_ids": ["index_0", "participation"]},
        "paragraphs": [{"text": "沪深300在2026-09-01至2026-09-30回落5.00%，末日点位低于20日和60日均线，说明这段时间价格整体处在偏弱的位置。", "insight_ids": ["index_0"], "chart": "indices"},
                       {"text": "参与度方面，末日上涨占比为40.00%，下跌家数多于上涨家数，不过这只是一个交易日的截面。", "insight_ids": ["participation"], "chart": "breadth"}],
        "main_tension": {"text": "区间下跌与单日宽度属于不同时间尺度，不能互相证明。", "insight_ids": ["index_0", "participation"], "tension_id": "different_windows"},
        "hypotheses": [{"text": "下跌可能集中在少数权重股，而不是多数股票同步走弱。", "check": "需要按市值分组的涨跌家数或成分股贡献数据。", "insight_ids": ["index_0", "participation"]}],
        "followups": ["同一区间大小盘风格有什么差异？", "末日涨跌停分布有哪些风险信号？"],
    }
    value.update(changes)
    return GroundedNarrative.model_validate(value)


def verify(value):
    insights, tensions = candidates(evidence())
    return check_narrative(value, insights, tensions, asks_for_prohibited_output)


def test_grounded_narrative_is_rendered_with_evidence_ids():
    result = verify(draft())
    assert result["author"] == "deepseek" and result["summary_evidence_ids"] == ["i", "b"]
    assert [p["chart"] for p in result["paragraphs"]] == ["indices", "breadth"] and result["paragraphs"][0]["evidence_ids"] == ["i"]
    assert result["hypotheses"][0]["check"].startswith("需要") and result["hypotheses"][0]["evidence_ids"] == ["i", "b"]
    assert len(result["followups"]) == 2


@pytest.mark.parametrize("text,problem", [
    ("沪深300在2026-09-01至2026-09-30回落5.30%。", "数字5.30不在所依据的观点或事实中"),
    ("沪深300在2026-09-01至2026-09-30上涨5.00%。", "负值，不能写成上涨"),
    ("沪深300在2026-08-01之后回落5.00%。", "日期2026-08-01不在所依据的观点或事实中"),
    ("沪深300区间变化+5.00%，偏弱。", "正负号与原文不一致"),
    ("沪深300回落5.00%，预计短期仍将承压。", "不允许的措辞“预计”"),
    ("沪深300回落5.00%，可以考虑逢低布局。", "不允许的措辞“逢低”"),
])
def test_ungrounded_or_out_of_bounds_claims_are_rejected(text, problem):
    with pytest.raises(NarrativeRejected) as caught:
        verify(draft(paragraphs=[{"text": text, "insight_ids": ["index_0"]}, draft().paragraphs[1].model_dump()]))
    assert any(problem in item for item in caught.value.problems), caught.value.problems


def test_followups_outside_the_boundary_are_dropped_not_rewritten():
    result = verify(draft(followups=["现在可以买入沪深300吗？", "看看", "同一区间大小盘风格有什么差异？"]))
    assert result["followups"] == ["同一区间大小盘风格有什么差异？"]


def test_hypothesis_check_is_also_verified():
    hypothesis = {"text": "下跌可能集中在少数权重股。", "check": "若下周继续下跌3.5%则成立。", "insight_ids": ["index_0"]}
    with pytest.raises(NarrativeRejected) as caught:
        verify(draft(hypotheses=[hypothesis]))
    assert any("3.5" in item for item in caught.value.problems)


def test_writer_tool_limits_ids_without_schema_references():
    insights, tensions = candidates(evidence())
    tool = writer_tool(insights, tensions, ['indices'])
    assert tool['function']['parameters']['properties']['paragraphs']['items']['properties']['chart']['enum'] == ['none', 'indices']
    text = json.dumps(tool)
    assert "$ref" not in text and "$defs" not in text
    paragraph = tool["function"]["parameters"]["properties"]["paragraphs"]["items"]
    assert paragraph["properties"]["insight_ids"]["items"]["enum"] == list(insights)


def run(tmp_path, monkeypatch, drafts, question="沪深300最近的行情和市场宽度如何？", intent='{"task":"index_research","entities":["沪深300"]}'):
    from market_research import research

    class FakeData:
        def __init__(self, *args):
            self.evidence = {}
            self.expected_day = self.latest_day = "2026-09-30"
        async def initialize(self, date):
            pass
        async def get_market_breadth(self):
            self.evidence["b"] = evidence()["b"]
            return self.evidence["b"]
        async def get_index_history(self, code, window):
            self.evidence["i"] = evidence()["i"]
            return self.evidence["i"]

    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        name = body["tools"][0]["function"]["name"]
        if name == "identify_research_intent":
            arguments = {"name": name, "arguments": intent}
        elif name == "write_grounded_narrative":
            arguments = {"name": name, "arguments": json.dumps(drafts.pop(0), ensure_ascii=False)}
        elif name == "select_verified_insights":
            arguments = {"name": name, "arguments": '{"summary_ids":["index_0"],"tension_id":"different_windows","detail_ids":["index_0"]}'}
        else:
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "one", "type": "function", "function": {"name": "get_index_history", "arguments": '{"code":"000300.SH","purpose":"指数"}'}},
                {"id": "two", "type": "function", "function": {"name": "get_market_breadth", "arguments": '{"purpose":"宽度"}'}}]}
            return httpx.Response(200, json={"choices": [{"message": message}]})
        message = {"role": "assistant", "content": None, "tool_calls": [{"id": "x", "type": "function", "function": arguments}]}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    original = httpx.AsyncClient
    class MockClient(original):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(research, "MarketData", FakeData)
    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    result = asyncio.run(run_research(ResearchRequest(question=question), Settings({"DEEPSEEK_API_KEY": "t", "DEEPSEEK_MODEL": "t"}), tmp_path))
    return result, calls


def test_research_uses_the_model_written_narrative(tmp_path, monkeypatch):
    result, calls = run(tmp_path, monkeypatch, [draft().model_dump()])
    assert result["status"] == "completed_with_limits" and result["analysis_protocol"] == "grounded-narrative-v1"
    assert result["narrative"]["author"] == "deepseek" and result["narrative"]["headline"].startswith("沪深300区间回落")
    assert result["narrative"]["paragraphs"][1]["chart"] == "breadth" and result["narrative"]["interpretations"] == []
    assert result["narrative"]["hypotheses"][0]["text"].startswith("下跌可能")
    assert result["followups"] == ["同一区间大小盘风格有什么差异？", "末日涨跌停分布有哪些风险信号？"]
    # Facts are still rendered by code, never by the model.
    assert result["facts"][0]["text"].startswith("沪深300从2026-09-01至2026-09-30")
    assert [c["tools"][0]["function"]["name"] for c in calls].count("select_verified_insights") == 0


def test_rejected_draft_is_corrected_with_the_server_problems(tmp_path, monkeypatch):
    wrong = draft(headline={"text": "沪深300回落5.80%。", "insight_ids": ["index_0"]}).model_dump()
    result, calls = run(tmp_path, monkeypatch, [wrong, draft().model_dump()])
    assert result["narrative"]["author"] == "deepseek"
    retry = calls[-1]["messages"][-1]["content"]
    assert "数字5.80不在所依据的观点或事实中" in retry and "只能来自所依据" in retry


def test_research_scope_may_be_stated_without_citing_it():
    insights, tensions = candidates(evidence())
    claim = {"text": "最近20个交易日的末日，沪深300所在市场上涨占比40.00%。", "insight_ids": ["participation"]}
    with pytest.raises(NarrativeRejected):
        check_narrative(draft(headline=claim), insights, tensions, asks_for_prohibited_output)
    result = check_narrative(draft(headline=claim), insights, tensions, asks_for_prohibited_output, "20个交易日 2026-09-30 沪深300")
    assert result["headline"].startswith("最近20个交易日")


def test_facts_from_the_same_evidence_may_be_quoted():
    insights, tensions = candidates(evidence())
    facts = [{"text": "2026-09-30核验样本5560家，上涨2567家、下跌2823家。", "evidence_ids": ["b"]}]
    claim = {"text": "末日5560家有效样本中上涨2567家、下跌2823家，上涨占比40.00%。", "insight_ids": ["participation"]}
    with pytest.raises(NarrativeRejected):
        check_narrative(draft(paragraphs=[claim, claim]), insights, tensions, asks_for_prohibited_output, "", [])
    result = check_narrative(draft(paragraphs=[claim, claim]), insights, tensions, asks_for_prohibited_output, "", facts)
    assert result["paragraphs"][0]["evidence_ids"] == ["b"]


def test_a_chart_is_placed_only_once():
    paragraphs = [{"text": "沪深300区间回落5.00%，价格偏弱。", "insight_ids": ["index_0"], "chart": "indices"}] * 2
    with pytest.raises(NarrativeRejected) as caught:
        verify(draft(paragraphs=paragraphs))
    assert "同一张图只能配一次" in caught.value.problems


def test_rounded_copies_and_named_periods_are_grounded():
    insights, tensions = candidates(evidence())
    paragraphs = [{"text": "沪深300在9月30日前的20个交易日里跌了5%，回撤约6%，末日还在60日均线下方；2026-09-01至09-30，沪深300月末低于均线。", "insight_ids": ["index_0"]},
                  {"text": "9月30日上涨占比40%，跌的股票略多。", "insight_ids": ["participation"]}]
    hypothesis = [{"text": "弱势可能来自少数权重股。", "check": "需要成分股贡献和250日均线以上的个股比例。", "insight_ids": ["index_0"]}]
    result = check_narrative(draft(paragraphs=paragraphs, hypotheses=hypothesis), insights, tensions, asks_for_prohibited_output)
    assert result["paragraphs"][0]["text"].startswith("沪深300在9月30日")
    # Rounding is allowed; a different value, an absent day or a flipped direction is not.
    for text, problem in (("沪深300在9月15日前回落5.00%。", "日期9月15日"), ("沪深300跌了7%。", "数字7"), ("沪深300上涨5%。", "负值，不能写成上涨")):
        with pytest.raises(NarrativeRejected) as caught:
            check_narrative(draft(paragraphs=[{"text": text, "insight_ids": ["index_0"]}]), insights, tensions, asks_for_prohibited_output)
        assert any(problem in item for item in caught.value.problems), caught.value.problems


def test_writer_gets_caveats_apart_and_data_tensions_first():
    data = evidence()
    data["i"]["data"]["series"] = [{"date": "2026-09-01", "close": 100}, {"date": "2026-09-15", "close": 102}, {"date": "2026-09-30", "close": 95}]
    data["b"]["data"]["advancing_ratio_pct"] = 48
    insights, tensions = candidates(data)
    assert insights["participation"]["caveat"] and "只是一个交易日" not in insights["participation"]["text"]
    # The index fell while nearly half the stocks rose: that divergence, not a method note, is the data tension.
    assert tensions["price_vs_breadth"]["kind"] == "data" and "48.00%" in tensions["price_vs_breadth"]["text"]
    assert tensions["different_windows"]["kind"] == "method"


def test_narrow_overview_only_plans_the_named_dimensions(tmp_path, monkeypatch):
    result, calls = run(tmp_path, monkeypatch, [draft().model_dump()], "不要仓位建议，只说明最近20个交易日的市场宽度和指数表现", '{"task":"market_overview","entities":[]}')
    planning = {tool["function"]["name"] for tool in calls[1]["tools"]}
    assert planning == {"get_market_breadth", "get_index_history"}
    assert result["narrative"]["author"] == "deepseek"
    # The writer is told what was asked, and receives no planning or selection rules.
    payload = json.loads(calls[-1]["messages"][1]["content"])
    assert payload["focus"]["asked_dimensions"] == ["行情结构", "市场宽度"]
    assert "summary_ids" not in calls[-1]["messages"][0]["content"]


def test_string_headline_and_text_only_reply_are_recovered(tmp_path, monkeypatch):
    from market_research import research
    # Live 2026-10-07: one draft came back without a tool call, another sent headline as a bare string.
    value = draft().model_dump()
    value["headline"] = value["headline"]["text"]
    replies = [{"role": "assistant", "content": "好的。" + json.dumps(value, ensure_ascii=False)}]

    class FakeData:
        def __init__(self, *args):
            self.evidence = {}
            self.expected_day = self.latest_day = "2026-09-30"
        async def initialize(self, date):
            pass
        async def get_market_breadth(self):
            self.evidence["b"] = evidence()["b"]
            return self.evidence["b"]
        async def get_index_history(self, code, window):
            self.evidence["i"] = evidence()["i"]
            return self.evidence["i"]

    def handler(request):
        body = json.loads(request.content)
        name = body["tools"][0]["function"]["name"]
        if name == "identify_research_intent":
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "x", "type": "function", "function": {"name": name, "arguments": '{"task":"index_research","entities":["沪深300"]}'}}]}
        elif name == "write_grounded_narrative":
            message = replies.pop(0)
        else:
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "one", "type": "function", "function": {"name": "get_index_history", "arguments": '{"code":"000300.SH","purpose":"指数"}'}},
                {"id": "two", "type": "function", "function": {"name": "get_market_breadth", "arguments": '{"purpose":"宽度"}'}}]}
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}]})

    original = httpx.AsyncClient
    class MockClient(original):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(research, "MarketData", FakeData)
    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    result = asyncio.run(run_research(ResearchRequest(question="沪深300最近的行情和市场宽度如何？"), Settings({"DEEPSEEK_API_KEY": "t", "DEEPSEEK_MODEL": "t"}), tmp_path))
    assert result["narrative"]["author"] == "deepseek" and result["narrative"]["headline"] == value["headline"]
    assert result["narrative"]["summary_evidence_ids"] == ["i"]


def test_content_free_openers_are_removed():
    from market_research.narrative import tidy
    assert tidy("先直接说结论：沪深300区间回落。") == "沪深300区间回落。"
    assert tidy("再看市场参与度：上涨占比40%。") == "上涨占比40%。"
    assert tidy("最后的结果是回落。") == "最后的结果是回落。"


def test_flat_headline_and_split_calls_are_one_draft():
    from market_research.narrative import merge_calls, normalize_draft
    insights, tensions = candidates(evidence())
    tool = writer_tool(insights, tensions, ["indices"])["function"]["parameters"]
    assert tool["properties"]["headline"]["type"] == "string" and "headline_insight_ids" in tool["required"]
    assert tool["properties"]["main_tension"]["type"] == "string" and tool["properties"]["tension_id"]["enum"] == list(tensions)
    value = draft().model_dump()
    head = {"headline": value["headline"]["text"], "headline_insight_ids": ["participation", "index_0"]}
    rest = {key: item for key, item in value.items() if key not in ("headline", "main_tension")}
    tension = value["main_tension"]
    rest.update({"main_tension": tension["text"], "tension_id": tension["tension_id"], "tension_insight_ids": tension["insight_ids"]})
    # Live 2026-10-07: one call carried a broken headline, the next carried the paragraphs.
    merged = merge_calls(['{"headline">\n<DSML: "x"}', json.dumps(head, ensure_ascii=False), json.dumps(rest, ensure_ascii=False)])
    result = check_narrative(GroundedNarrative.model_validate(normalize_draft(merged)), insights, tensions, asks_for_prohibited_output)
    assert result["summary_evidence_ids"] == ["b", "i"]
