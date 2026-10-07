"""Question semantics determine the research task before any market data is fetched."""

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .connections import DEEPSEEK, ProbeError, first_message, request_json


class ResearchIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: Literal["market_overview", "sector_ranking", "named_comparison", "index_research", "risk_research", "historical_comparison", "valuation", "events", "single_security", "explanation", "clarification"]
    entities: list[str] = Field(default_factory=list, max_length=5)
    sector_universe: Literal["industry", "concept"] = "industry"
    explanation_topic: Literal["breadth", "valuation", "drawdown", "moving_average", "usage", "none"] = "none"


async def classify_question(client, settings, question, context, window):
    schema = ResearchIntent.model_json_schema()
    tool = {"type": "function", "function": {"name": "identify_research_intent", "description": "识别用户真正要解决的问题，不规划无关的大盘模板。", "parameters": schema}}
    response = await request_json(client, "POST", DEEPSEEK + "/chat/completions",
        headers={"Authorization": "Bearer " + settings.get("DEEPSEEK_API_KEY")},
        json={"model": settings.get("DEEPSEEK_MODEL"), "thinking": {"type": "disabled"}, "max_tokens": 500,
              "tools": [tool], "tool_choice": {"type": "function", "function": {"name": "identify_research_intent"}},
              "messages": [{"role": "system", "content": "只识别问题意图。询问哪些板块/行业最近更强、势头较好、领涨、热点或排名，应是sector_ranking，不能是market_overview。没有指定具体板块时entities留空，绝不自选银行或半导体。板块未明确概念时先按industry。对指定行业/指数的比较为named_comparison，利用上下文解析“第一个/它/这些”。询问具体公司或单只股票/基金（如贵州茅台、宁德时代、600519）的涨跌、走势、估值或前景时为single_security，entities写该证券名，不能改成指数或market_overview。问概念含义（如“什么是市场宽度”“PE是什么意思”）或打招呼用explanation，不需要行情取数；询问近期或末日的涨跌停分布、风险信号、情绪、极端交易等实际市场情况时是risk_research，不是explanation。市场整体状态才用market_overview。无法明确对象时clarification。entities只写用户或上下文中明确的实体名，不编造代码。"},
                           {"role": "user", "content": json.dumps({"question": question, "context": context, "selected_window": window}, ensure_ascii=False)}]})
    try:
        calls = first_message(response)["tool_calls"]
        if len(calls) != 1 or calls[0]["function"]["name"] != "identify_research_intent":
            raise ValueError
        intent = ResearchIntent.model_validate_json(calls[0]["function"]["arguments"])
    except (KeyError, ValueError, TypeError, ValidationError):
        raise ProbeError("intent_not_resolved") from None
    return override_explanation(intent, question)


DEFINITIONAL = re.compile(r"什么是|是什么|含义|意思|定义|怎么理解|如何理解|怎么算|如何计算|怎么用|你好|您好|能做什么")
RISK_WORDS = re.compile(r"风险|情绪|涨停|跌停|极端")


def override_explanation(intent: ResearchIntent, question: str) -> ResearchIntent:
    # A data question must not be answered with a canned definition just because it mentions a concept.
    if intent.task == "explanation" and not DEFINITIONAL.search(question) and RISK_WORDS.search(question):
        return intent.model_copy(update={"task": "risk_research", "explanation_topic": "none"})
    return intent
