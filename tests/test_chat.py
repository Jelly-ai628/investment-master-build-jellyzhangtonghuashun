import asyncio
import json

import httpx

from market_research.connections import Settings
from market_research.research import ResearchRequest, run_research


def run(tmp_path, monkeypatch, answers, task="chat"):
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        name = body["tools"][0]["function"]["name"]
        arguments = json.dumps({"task": task, "entities": []}) if name == "identify_research_intent" else json.dumps(answers.pop(0), ensure_ascii=False)
        message = {"role": "assistant", "content": None, "tool_calls": [{"id": "x", "type": "function", "function": {"name": name, "arguments": arguments}}]}
        return httpx.Response(200, json={"choices": [{"message": message}]})
    original = httpx.AsyncClient
    class MockClient(original):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    result = asyncio.run(run_research(ResearchRequest(question="你可以研究哪些个股"), Settings({"DEEPSEEK_API_KEY": "t", "DEEPSEEK_MODEL": "t"}), tmp_path))
    return result, calls


GOOD = {"answer": "沪深北的A股都可以，用简称或6位代码问就行，比如“贵州茅台”或“600519”。我会取它的前复权行情，和沪深300做同区间比较，再看最新的PE/PB快照；所属行业和财报目前还没接入。",
        "followups": ["贵州茅台最近20个交易日表现如何？", "600519相对沪深300表现如何？"]}


def test_general_question_is_answered_by_the_model_without_data(tmp_path, monkeypatch):
    result, calls = run(tmp_path, monkeypatch, [GOOD])
    assert result["presentation"] == "message" and result["narrative"]["author"] == "deepseek"
    assert result["narrative"]["summary"].startswith("沪深北的A股都可以") and result["evidence"] == []
    assert result["followups"] == GOOD["followups"]
    assert [c["tools"][0]["function"]["name"] for c in calls] == ["identify_research_intent", "answer_without_data"]


def test_invented_market_figures_fall_back_to_the_fixed_reply(tmp_path, monkeypatch):
    invented = {"answer": "最近沪深300跌了5.5%，可以研究个股。", "followups": ["贵州茅台最近20个交易日表现如何？"]}
    result, calls = run(tmp_path, monkeypatch, [invented, invented])
    assert result["narrative"]["author"] == "rules" and "5.5%" not in result["narrative"]["summary"]
    # The retry told the model exactly what was rejected.
    assert "5.5%" in calls[-1]["messages"][-1]["content"]
    assert any(e["type"] == "validation_retry" for e in result["events"])


def test_saying_what_is_not_offered_is_not_advice(tmp_path, monkeypatch):
    answer = {"answer": "可以研究沪深北A股，用简称或6位代码问就行；我不提供买入或卖出建议，只看已经发生的行情。",
              "followups": ["贵州茅台最近20个交易日表现如何？", "现在可以买入茅台吗？", "看看"], "note": "extra key"}
    result, calls = run(tmp_path, monkeypatch, [answer])
    assert result["narrative"]["author"] == "deepseek" and len(calls) == 2
    # Out-of-bounds follow-ups are dropped instead of failing the answer.
    assert result["followups"] == ["贵州茅台最近20个交易日表现如何？"]
