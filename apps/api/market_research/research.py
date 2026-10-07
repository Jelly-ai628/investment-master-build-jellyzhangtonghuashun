"""DeepSeek plans actual tools; deterministic facts and checked narrative form a report."""

import asyncio
from datetime import datetime
from datetime import timedelta
import json
from pathlib import Path
import re
import time
from typing import Literal
import uuid

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .connections import Settings, ProbeError, DEEPSEEK, SHANGHAI, request_json, first_message
from .market_data import MarketData, INDICES, private_json
from .insights import MAX_DETAILS, InsightSelection, candidates, full_text, render_selection
from .narrative import WRITER_INSTRUCTIONS, GroundedNarrative, NarrativeRejected, available_charts, check_narrative, merge_calls, normalize_draft, writer_tool
from .intent import classify_question
from .chat import answer_without_data
from .sector_ranking import ranking_answer


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=2, max_length=4000)
    window: Literal[5, 10, 20, 60] = 20
    end_date: str | None = None
    compare_end_date: str | None = None
    context: str = Field(default="", max_length=2500)


def asks_for_prohibited_output(question: str) -> bool:
    for match in re.finditer(r"明天.{0,8}(涨|跌)|预测.{0,8}(涨|跌)|必涨|必跌|保证收益|收益承诺|仓位|(?:推荐|建议|应该|直接).{0,8}(买入|卖出|建仓)|(?:可以|能不能|能否|该不该|要不要|值不值得|值得)(?:现在)?(买|卖|入手|抄底|加仓|减仓|建仓)", question):
        if not re.search(r"不要|不需要|不要求|不提供", question[max(0, match.start() - 12):match.start()]):
            return True
    return False


MARKET_WORDS = re.compile(r"A股|大盘|市场|股市|两市|沪深两市|全市场|整体市场|A股市场")
IFIND_TOOL_TIMEOUT = 50
SKILL_VERSION = "research-skills-v3"
WRITER_ATTEMPTS = 3
# Seconds after the run started beyond which another rewrite would risk the 240-second job limit.
WRITER_DEADLINE = 150
# Planning and fallback-selection guidance is noise for the writer, which only needs how to read the evidence.
WRITER_SKIPS = re.compile(r"^## .*(取数计划|选择与排序|撰写、选择与排序|标准流程|问题与证据组合)")


def writer_skill(skill: str) -> str:
    kept, skipping = [], False
    for line in skill.splitlines():
        if line.startswith("## "):
            skipping = bool(WRITER_SKIPS.match(line))
        if not skipping:
            kept.append(line)
    return "\n".join(kept)


def select_skills(task: str, question: str, *, stock: bool, industries: bool, history: bool,
                  risk: bool, valuation: bool, events: bool) -> list[str]:
    # The protocol always applies; each topic skill is loaded only when its evidence is in this plan.
    names = ["research-protocol", "security-context" if stock else "market-state"]
    if task == "named_comparison" or re.search("比较|对比|大小盘|风格", question):
        names.append("comparison")
    if industries or task == "sector_ranking":
        names.append("industry-context")
    if history:
        names.append("history-context")
    if risk:
        names.append("risk-context")
    if valuation or stock:
        names.append("valuation-boundary")
    if events:
        names.append("event-context")
    return names


async def bounded(coroutine):
    # iFinD calls are optional evidence; one slow provider must not consume the whole research budget.
    try:
        return await asyncio.wait_for(coroutine, IFIND_TOOL_TIMEOUT)
    except (TimeoutError, asyncio.TimeoutError):
        raise ProbeError("ifind_tool_timeout") from None


def tool_summary(item: dict) -> dict:
    data = dict(item["data"])
    data.pop("series", None)
    return {"evidence_id": item["id"], "kind": item["kind"], "data": data,
            "source": item["provenance"]["source"], "observation_date": item["provenance"]["observation_date"]}


def deterministic_report(evidences: dict, failures: list[dict]) -> dict:
    facts, conditions = [], []
    for key, item in evidences.items():
        data = item["data"]
        if item["kind"] == "index_history":
            stock = data.get("instrument_type") == "stock"
            missing = data.get("missing_dates") or []
            facts.append({"text": f"{data['name']}从{data['start_date']}至{data['end_date']}，首末{'前复权收盘价' if stock else '点位'}变化{data['window_return_pct']:+.2f}%，区间最大回撤{data['max_drawdown_pct']:.2f}%。"
                          + (f"研究区间内{'、'.join(missing[:5])}{'等' if len(missing) > 5 else ''}共{len(missing)}个交易日没有该股日K，未补值。" if missing else ""), "evidence_ids": [key], "kind": "fact"})
            now = "涨" if data["window_return_pct"] > 0 else "跌"
            conditions.append({"label": f"{data['name']}的区间方向是否反转", "metric": "window_return_pct", "baseline": data["window_return_pct"],
                               "unit": "%", "evidence_id": key, "observation_window": "后续2个完整交易日", "rule_version": "state-review-v1",
                               "condition": f"现在{data.get('return_intervals', 20)}个交易日首末变化是{data['window_return_pct']:+.2f}%（{now}）。之后2个完整交易日各重算一次同长度窗口，如果两次都转为{'下跌' if now == '涨' else '上涨'}，就要重新判断它的区间方向。这是复核规则，不是交易信号。"})
        elif item["kind"] == "market_breadth" and data["quality"] != "insufficient_coverage":
            facts.append({"text": f"{data['date']}核验样本{data['eligible_count']}家，上涨{data['advancers']}家、下跌{data['decliners']}家、平盘{data['unchanged']}家；上涨占比{data['advancing_ratio_pct']:.2f}%，有效覆盖{data['coverage_pct']:.2f}%。", "evidence_ids": [key], "kind": "fact"})
            more = "上涨" if data["advancers"] > data["decliners"] else "下跌"
            conditions.append({"label": "多数股票的方向是否改变", "metric": "advancing_ratio_pct", "baseline": data["advancing_ratio_pct"],
                               "unit": "%", "evidence_id": key, "observation_window": "后续2个完整交易日", "rule_version": "state-review-v1",
                               "condition": f"本次{more}家数更多（上涨占比{data['advancing_ratio_pct']:.2f}%）。如果之后2个完整交易日按同一口径统计，都变成{'下跌' if more == '上涨' else '上涨'}家数更多，就要重新判断参与度；覆盖率不足98%的那天不计入。"})
        elif item["kind"] == "sentiment":
            facts.append({"text": f"{data['date']}供应商涨停池{data['limit_up_count']}家、跌停池{data['limit_down_count']}家，已按同日收盘价逐条核对。", "evidence_ids": [key], "kind": "fact"})
        elif item["kind"] == "historical_comparison":
            current, previous = data["current"], data["previous"]
            facts.append({"text": f"{data['name']}当前区间{current['start_date']}至{current['end_date']}变化{current['window_return_pct']:+.2f}%；对照区间{previous['start_date']}至{previous['end_date']}变化{previous['window_return_pct']:+.2f}%，两者相差{data['return_difference_pp']:+.2f}个百分点。", "evidence_ids": [key], "kind": "fact"})
        elif item["kind"] == "valuation":
            values = "、".join(f"{row['metric']}为{row['raw_value']}" for row in data["items"])
            if data.get("snapshot"):
                facts.append({"text": f"扶摇最新估值快照显示{data['name']}的{values}（单位：倍，数据时间{data['date']}）。仅为当前水平，未计算历史分位。", "evidence_ids": [key], "kind": "fact"})
            else:
                facts.append({"text": f"iFinD返回{data['name']}在{data['date']}的{values}（单位：倍，交易日期参数已核对）。仅为截至日指标水平，未计算历史分位。", "evidence_ids": [key], "kind": "fact"})
        elif item["kind"] == "events":
            for record in data["records"]:
                facts.append({"text": f"{record['published_at']}报道《{record['title']}》，原网页标题与日期已核对；这是媒体报道，不是央行原始公告。", "evidence_ids": [key], "kind": "fact"})
        elif item["kind"] == "sector_ranking":
            facts.append({"text": ranking_answer(data), "evidence_ids": [key], "kind": "fact"})
            conditions.append({"label": "何时重新评估这份名单", "metric": "sector_rank", "baseline": 0, "unit": "排序",
                "evidence_id": key, "condition": f"之后的完整交易日按同样的{data['window']}个交易日窗口和行业范围重算。如果前五名换了人，或它们近5日的方向变了，就重新比较；数据覆盖下降时不沿用旧名单。"})
    has_breadth = any(item["kind"] == "market_breadth" and item["data"]["quality"] != "insufficient_coverage" for item in evidences.values())
    indices = sum(item["kind"] == "index_history" for item in evidences.values())
    broad = [item for item in evidences.values() if item["kind"] == "index_history" and item["data"].get("instrument_type") not in ("industry", "stock")]
    breadth = next((item for item in evidences.values() if item["kind"] == "market_breadth" and item["data"]["quality"] != "insufficient_coverage"), None)
    state = "证据不足，暂不形成综合状态"
    basis = []
    if broad and breadth:
        returns = [item["data"]["window_return_pct"] for item in broad]
        movement = "所选指数区间回落" if all(value < 0 for value in returns) else "所选指数区间上行" if all(value > 0 for value in returns) else "所选指数区间方向分化"
        b = breadth["data"]
        participation = "末日下跌家数较多" if b["decliners"] > b["advancers"] else "末日上涨家数较多" if b["advancers"] > b["decliners"] else "末日涨跌家数相同"
        state = movement + "，" + participation
        basis = [item["id"] for item in broad] + [breadth["id"]]
    kinds = {item["kind"] for item in evidences.values()}
    failed = {item["tool"]: item["reason"] for item in failures}
    def attempted(kind, tool, found, missing):
        if kind in kinds:
            return found
        return f"已调用，未通过核验（{failed[tool]}）" if tool in failed else missing
    gaps = [name for name, kind in (("估值", "valuation"), ("重要事件", "events")) if kind not in kinds] + ["宏观"]
    return {"market_state": {"label": state, "evidence_ids": basis, "scope": "行情与市场宽度的局部状态；其他维度仍按数据可用性呈现。", "rule_version": "state-review-v1"},
            "facts": facts, "transition_conditions": conditions,
            "confidence": {"level": "中" if has_breadth and indices else "低", "scope": "仅针对已取得的行情/宽度局部观察，不代表涨跌概率。", "reasons": ["事实由字段与代码计算支持。", "、".join(gaps) + "等维度未核实，不能形成高置信度的完整市场判断。"]},
            "dimensions": [
                {"name": "行情结构", "status": "已取得" if indices else "缺失"},
                {"name": "市场宽度", "status": "已取得（有样本限制）" if has_breadth else "覆盖不足，未用于结论" if "market_breadth" in kinds else attempted("market_breadth", "get_market_breadth", "", "本轮未调用")},
                {"name": "风格轮动", "status": "指数比较可用" if indices > 1 else "尚未比较"},
                {"name": "估值", "status": attempted("valuation", "get_valuation_context", "已取得截至日PE/PB；无历史分位", "本轮未调用")},
                {"name": "流动性", "status": "仅成交活跃度可观察；杠杆/利率缺失" if has_breadth else "本轮未取得成交活跃度；杠杆/利率缺失"},
                {"name": "情绪", "status": attempted("sentiment", "get_risk_context", "已取得涨跌停分布；其他情绪证据有限", "涨跌停数据本轮未调用" + ("；仅有参与度代理" if has_breadth else ""))},
                {"name": "重要事件", "status": attempted("events", "get_event_context", "已取得核对原文的流动性报道；非全部事件", "本轮未调用")}],
            "uncertainties": ["区间指数表现与单日市场宽度时间尺度不同，不能据此断言整个区间的参与度。", "快照日期通过带日期日K关联核对，缺失记录明确排除，不推定为停牌。", "当前证据尚不足以核实" + "、".join(gaps) + "等驱动。"],
            "tool_failures": failures}


def security_report(stock_items: list[dict], evidences: dict, failures: list[dict]) -> dict:
    # A stock question is answered about the stock itself; the market only supplies a same-window benchmark.
    stock = stock_items[0]
    s = stock["data"]
    benchmark = next((item for item in evidences.values() if item["kind"] == "index_history" and item["data"].get("code") == "000300.SH"), None)
    valuation = next((item for item in evidences.values() if item["kind"] == "valuation" and item["data"].get("code") == s["code"]), None)
    movement = "区间上行" if s["window_return_pct"] > 0 else "区间回落" if s["window_return_pct"] < 0 else "区间持平"
    label, basis = s["name"] + movement, [stock["id"]]
    relative = "缺失"
    if benchmark and (benchmark["data"]["start_date"], benchmark["data"]["end_date"]) == (s["start_date"], s["end_date"]):
        spread = s["window_return_pct"] - benchmark["data"]["window_return_pct"]
        label += "，" + ("强于" if spread > 0 else "弱于" if spread < 0 else "持平于") + "沪深300"
        basis.append(benchmark["id"])
        relative = f"已与沪深300同区间比较（{spread:+.2f}个百分点）"
    failed = {item["tool"]: item["reason"] for item in failures}
    valuation_status = "已取得最新PE/PB快照；无历史分位" if valuation else f"已调用，未通过核验（{failed['get_stock_valuation']}）" if "get_stock_valuation" in failed else "本轮未调用"
    uncertainties = ["个股价格使用扶摇前复权日K，复权口径由供应商确定；区间变化不含分红再投资。",
                     "所属行业、公司公告与财务变化本轮未核实（扶摇股票基础信息接口尚未开放），不能把价格变化归因于基本面或行业轮动。"]
    if valuation:
        uncertainties.append(f"估值为扶摇最新快照（数据时间{valuation['data']['date']}），不是历史序列，不能判断高低估。")
    if s.get("missing_dates"):
        uncertainties.append(f"研究区间内有{len(s['missing_dates'])}个交易日没有该股日K（可能停牌或尚未上市），区间变化按已有交易日计算，未补值。")
    if relative == "缺失" and benchmark:
        relative = "该股与沪深300的起止日期不同（有交易日缺失），未做相对比较"
    return {"market_state": {"label": label, "evidence_ids": basis, "scope": "该股已发生的行情及与沪深300的同区间相对表现。", "rule_version": "security-state-v1"},
            "confidence": {"level": "中" if len(basis) > 1 else "低", "scope": "仅针对该股已发生的行情与估值快照，不代表涨跌概率。",
                           "reasons": ["价格事实由扶摇日K字段与代码计算支持。", "行业归属、公司公告与财务驱动未核实。"]},
            "dimensions": [{"name": "个股行情", "status": "已取得（前复权日K）"}, {"name": "相对大盘", "status": relative},
                           {"name": "估值", "status": valuation_status}, {"name": "行业归属", "status": "扶摇接口尚未开放，未比较"},
                           {"name": "公司事件与公告", "status": "本轮未调用"}],
            "uncertainties": uncertainties}


async def run_research(request: ResearchRequest, settings: Settings, root: Path, emit=None) -> dict:
    run_id = uuid.uuid4().hex
    events = []
    started = time.monotonic()

    async def event(kind, **payload):
        item = {"seq": len(events) + 1, "type": kind, "time": datetime.now(SHANGHAI).isoformat(), **payload}
        events.append(item)
        if emit:
            await emit(item)

    if asks_for_prohibited_output(request.question):
        result = {"run_id": run_id, "question": request.question, "as_of": "未取数", "window": request.window, "status": "scope_guidance",
                  "presentation": "message", "created_at": datetime.now(SHANGHAI).isoformat(),
                  "narrative": {"summary": "这个产品用于理解市场状态、证据与不确定性，不能提供未来涨跌或具体操作结论。可以继续研究已发生的行情、参与度，以及哪些条件会改变当前判断。",
                                "main_tension": "未来结果尚未发生，不能把研究观察转成确定结论。", "interpretations": [], "author": "rules"},
                  "facts": [], "evidence": [], "transition_conditions": [], "dimensions": [],
                  "confidence": {"level": "不适用", "scope": "未进行金融数据取数，不生成市场状态判断。", "reasons": []},
                  "uncertainties": ["本轮仅说明产品研究边界，没有生成行情或投资结论。"],
                  "followups": ["最近20个交易日市场行情与参与度如何？", "当前有哪些证据缺口和风险变量？"],
                  "events": events, "tool_failures": [], "model": None}
        await event("completed", run_id=run_id, status=result["status"])
        private_json(root / "work" / "research-runs" / f"{run_id}.json", result)
        return result

    if not settings.ready("deepseek"):
        raise ProbeError("deepseek_not_configured")
    headers = {"Authorization": "Bearer " + settings.get("DEEPSEEK_API_KEY")}
    model = settings.get("DEEPSEEK_MODEL")
    async def guidance(intent, message, followups, scope, author="rules"):
        result = {"run_id": run_id, "question": request.question, "as_of": "未取数", "window": request.window,
            "status": "scope_guidance", "presentation": "message", "created_at": datetime.now(SHANGHAI).isoformat(),
            "narrative": {"summary": message, "main_tension": "", "interpretations": [], "author": author},
            "facts": [], "evidence": [], "transition_conditions": [], "dimensions": [], "uncertainties": [],
            "confidence": {"level": "不适用", "scope": scope, "reasons": []},
            "followups": followups, "events": events, "tool_failures": [], "model": model, "intent": intent.model_dump()}
        await event("completed", run_id=run_id, status=result["status"])
        private_json(root / "work" / "research-runs" / f"{run_id}.json", result)
        return result

    async with httpx.AsyncClient(timeout=40, follow_redirects=False) as client:
        await event("step_started", label="理解你的研究问题")
        intent = await classify_question(client, settings, request.question, request.context, request.window)
        await event("intent", task=intent.task, entities=intent.entities)
        if intent.task == "single_security" and not intent.entities:
            return await guidance(intent, "请补充要研究的股票简称或6位代码，例如“贵州茅台”或“600519”。", ["贵州茅台最近20个交易日表现如何？", "最近20个交易日市场整体状态如何？"], "未给出具体证券，未进行取数。")
        if intent.task in ("explanation", "chat", "clarification") or (intent.task == "sector_ranking" and intent.sector_universe == "concept"):
            # Questions that need no market data get a real answer from DeepSeek; the canned texts are only a fallback.
            await event("step_started", label="DeepSeek回答（本轮不取数）")
            try:
                reply = await answer_without_data(client, settings, request.question, request.context, asks_for_prohibited_output,
                    lambda attempt, item: private_json(root / "work" / "validation-attempts" / f"{run_id}-chat-{attempt}.json", item))
                return await guidance(intent, reply["answer"], reply["followups"], "本轮没有调用数据接口，回答不包含行情数据。", "deepseek")
            except ProbeError as error:
                await event("validation_retry", reason=error.category)
            explanations = {
                "breadth": "市场宽度衡量有多少证券参与了市场表现。上涨家数、下跌家数和上涨占比是常见观察项，必须说明样本范围、停牌/缺失处理和统计日期。它与指数涨跌并不相同。",
                "valuation": "PE比较价格与盈利，PB比较价格与账面净资产。比较时需要同一时点和财务口径；当前值不等于历史分位，低估值也不自动代表低风险。",
                "drawdown": "最大回撤是一个区间内从已经出现的高点到后续低点的最大下降幅度，与区间首末收益不同，不能据此直接推断未来表现。",
                "moving_average": "均线是一定数量交易日价格的算术平均。当前点位低于均线，不等于当天刚刚穿过均线，也不等于市场参与者的真实成本。",
                "usage": "可以直接问：市场整体状态、哪些行业近期较强、指定行业之间怎么比、大小盘风格、涨跌停与风险信号、某段时间和上个月相比，或者某只A股股票的表现。股票用简称或6位代码提问，例如“贵州茅台最近20个交易日表现如何？”或“600519相对沪深300表现如何？”；名称检索不到时换成6位代码再试。我会实际取数，并说明依据与局限。",
            }
            message = explanations.get(intent.explanation_topic, explanations["usage"])
            return await guidance(intent, message, ["贵州茅台最近20个交易日表现如何？", "最近有哪些表现较强的行业板块？", "最近20个交易日市场整体状态如何？"], "本轮是概念说明或范围澄清。")
        data = MarketData(settings, root, client)
        data.on_progress = lambda label: event("step_started", label=label)
        await event("step_started", label="确认交易日与研究范围")
        await data.initialize(request.end_date)
        explicit_window = re.search(r"(?:最近|近)(5|10|20|60)个?交易日", request.question)
        if explicit_window:
            request = request.model_copy(update={"window": int(explicit_window[1])})
        comparison_end = request.compare_end_date
        if not comparison_end and (intent.task == "historical_comparison" or re.search("上个月|上一阶段", request.question)):
            dates = re.findall(r"\d{4}-\d{2}-\d{2}", request.question)
            comparison_end = dates[-1] if dates else (datetime.fromisoformat(data.expected_day).replace(day=1) - timedelta(days=1)).date().isoformat()
        def names_index(entity):
            return any(code in entity or name in entity for code, name in INDICES.items())
        catalog = ()
        if intent.task in ("sector_ranking", "named_comparison") or (intent.task != "single_security" and not all(map(names_index, intent.entities))):
            catalog = await data.load_industries()
        industry_names = [name for name in intent.entities if name in catalog][:3]
        unresolved = [entity for entity in intent.entities if entity not in catalog and not names_index(entity) and not MARKET_WORDS.fullmatch(entity)]
        # Named objects outside the index and industry catalogues are looked up as A-share stocks before anything else is considered.
        securities, failures, found = {}, [], set()
        hints = dict(zip(intent.entities, intent.entity_codes))
        for entity in unresolved[:3]:
            await event("tool_started", tool="resolve_security", purpose="在扶摇A股代码表中匹配" + entity, code=None)
            try:
                code = await data.resolve_security(entity, hints.get(entity) or None)
                securities[code] = data.securities[code]
                found.add(entity)
            except ProbeError as error:
                failures.append({"tool": "resolve_security", "code": entity, "reason": error.category})
            await event("tool_completed", tool="resolve_security", evidence_id=None, status="collected" if entity in found else "unavailable")
        unresolved = [entity for entity in unresolved if entity not in found]
        ambiguous = {entity: names for entity, names in getattr(data, "ambiguous", {}).items() if entity in unresolved}
        if ambiguous and not securities:
            entity, names = next(iter(ambiguous.items()))
            return await guidance(intent, f"扶摇A股代码表里有不止一只股票的名称包含“{entity}”：{'、'.join(names)}。你想研究的是哪一只？",
                [f"{name}最近{request.window}个交易日表现如何？" for name in names[:3]], "问题对象对应多只股票，未进行取数。")
        # Narrow questions ("只说明……") get only the dimensions they name; broad overviews still try every framework dimension.
        narrow = bool(re.search(r"只(?:说明|看|讲|要|关注|分析|研究|谈|问)", request.question))
        risk_asked = intent.task == "risk_research" or bool(re.search("风险|情绪|涨停|跌停|极端", request.question))
        # Whole-market questions also attempt the sentiment dimension; only explicit risk questions must lead with it.
        wants_risk = risk_asked or (intent.task == "market_overview" and not narrow)
        # Valuation and events are attempted for whole-market questions so their absence is observed, not assumed.
        wants_valuation = intent.task not in ("sector_ranking", "single_security") and ((intent.task == "market_overview" and not narrow) or intent.task == "valuation" or bool(re.search("估值|市盈率|市净率|PE|PB", request.question, re.I)))
        wants_events = intent.task != "sector_ranking" and ((intent.task == "market_overview" and not narrow) or intent.task == "events" or bool(re.search("事件|政策|新闻|央行|公告|逆回购", request.question)))
        await event("scope", as_of=data.expected_day, window=request.window, latest_day=data.latest_day, comparison_end_date=comparison_end, industries=industry_names, securities=securities)
        skill_names = select_skills(intent.task, request.question, stock=bool(securities), industries=bool(industry_names),
                                    history=bool(comparison_end), risk=wants_risk, valuation=wants_valuation, events=wants_events)
        loaded_skills = [name for name in skill_names if (root / "research-skills" / (name + ".md")).exists()]
        skill = "\n\n".join((root / "research-skills" / (name + ".md")).read_text() for name in loaded_skills)
        messages = [{"role": "system", "content": skill + "\n你是只读取证的市场研究Agent。按已识别的问题意图选择工具，不套用大盘报告。行业排名必须用get_sector_ranking，不能拿宽基指数或随意选几个行业代替。每个purpose说明取数用途，不输出内部思考。已取得的工具不要重复调用。接口失败明确记录。研究个股时用get_stock_history取该股行情，沪深300只作为大盘对照，不能用指数代替个股。"},
                    {"role": "user", "content": json.dumps({"question": request.question, "conversation_context": request.context, "verified_end_date": data.expected_day, "return_intervals": request.window, "comparison_end": comparison_end, "selected_industries": industry_names, "selected_securities": securities}, ensure_ascii=False)}]
        tools = [
            {"type": "function", "function": {"name": "get_market_breadth", "description": "最新完整交易日的核验市场宽度与成交活跃度；历史日期不支持快照宽度。", "parameters": {"type": "object", "properties": {"purpose": {"type": "string"}}, "required": ["purpose"], "additionalProperties": False}}},
            {"type": "function", "function": {"name": "get_index_history", "description": "获取并计算研究区间的指数点位表现、回撤、均线与区间收盘高低点位置，服务器固定研究日期和窗口。", "parameters": {"type": "object", "properties": {"code": {"type": "string", "enum": list(INDICES)}, "purpose": {"type": "string"}}, "required": ["code", "purpose"], "additionalProperties": False}}},
        ]
        if intent.task == "sector_ranking":
            tools = [{"type": "function", "function": {"name": "get_sector_ranking", "description": "对同口径的行业样本实际取数，按选定区间累计表现排名，并比较近5日表现与沪深300相对强弱。", "parameters": {"type": "object", "properties": {"purpose": {"type": "string"}}, "required": ["purpose"], "additionalProperties": False}}}]
        if industry_names:
            tools.append({"type": "function", "function": {"name": "get_industry_history", "description": "研究所选同花顺行业指数的同区间表现。", "parameters": {"type": "object", "properties": {"industry": {"type": "string", "enum": industry_names}, "purpose": {"type": "string"}}, "required": ["industry", "purpose"], "additionalProperties": False}}})
        if securities:
            tools.append({"type": "function", "function": {"name": "get_stock_history", "description": "经扶摇获取所选A股的前复权日K，计算同一研究区间的首末变化、回撤、均线与区间收盘高低点位置。", "parameters": {"type": "object", "properties": {"code": {"type": "string", "enum": list(securities)}, "purpose": {"type": "string"}}, "required": ["code", "purpose"], "additionalProperties": False}}})
            tools.append({"type": "function", "function": {"name": "get_stock_valuation", "description": "经扶摇读取所选A股的最新PE/PB估值快照；不提供历史估值或高低估结论。", "parameters": {"type": "object", "properties": {"code": {"type": "string", "enum": list(securities)}, "purpose": {"type": "string"}}, "required": ["code", "purpose"], "additionalProperties": False}}})
        if comparison_end:
            tools.append({"type": "function", "function": {"name": "compare_history", "description": "比较当前与指定历史截至日期的相同长度区间。", "parameters": {"type": "object", "properties": {"code": {"type": "string", "enum": list(INDICES)}, "purpose": {"type": "string"}}, "required": ["code", "purpose"], "additionalProperties": False}}})
        if wants_risk:
            tools.append({"type": "function", "function": {"name": "get_risk_context", "description": "核对末日涨跌停分布，说明可观察的极端交易风险，不代替完整宏观风险分析。", "parameters": {"type": "object", "properties": {"purpose": {"type": "string"}}, "required": ["purpose"], "additionalProperties": False}}})
        if wants_valuation:
            tools.append({"type": "function", "function": {"name": "get_valuation_context", "description": "经iFinD读取宽基指数在研究截至日的PE(TTM)与PB，核对日期参数与单位；只给当前水平，不给历史分位或高低估结论。", "parameters": {"type": "object", "properties": {"code": {"type": "string", "enum": list(INDICES)}, "purpose": {"type": "string"}}, "required": ["code", "purpose"], "additionalProperties": False}}})
        if wants_events:
            tools.append({"type": "function", "function": {"name": "get_event_context", "description": "经iFinD检索研究区间末段的央行公开市场操作报道，并核对原网页标题与日期；只覆盖流动性相关事件，不代表全部重要事件。", "parameters": {"type": "object", "properties": {"purpose": {"type": "string"}}, "required": ["purpose"], "additionalProperties": False}}})
        completed = {}
        call_count = 0
        required_codes = {code for code, name in INDICES.items() if name in request.question or code in request.question or any(name in entity or code in entity for entity in intent.entities)}
        if "大小盘" in request.question:
            required_codes.update({"000300.SH", "000905.SH", "000852.SH"})
        elif "小盘" in request.question:
            required_codes.add("000852.SH")
        if securities and not required_codes:
            # A stock is read against the broad market, not replaced by it.
            required_codes = {"000300.SH"}
        if not required_codes and not industry_names:
            if unresolved:
                # The default index only stands in for "the market", never for an object the user named.
                reasons = "；".join(f"{item['code']}：{item['reason']}" for item in failures if item["reason"] not in ("security_not_resolved", "ambiguous_security"))
                return await guidance(intent, f"没有找到“{'、'.join(unresolved)}”：它不在支持的宽基指数（{'、'.join(INDICES.values())}）和同花顺行业目录里，扶摇A股代码表按名称和代码也没有检索到" + (f"（接口返回：{reasons}）" if reasons else "") + "。本轮没有取数，也不会拿沪深300等其他对象代替。如果是A股股票，换成6位代码再问一次通常就能找到，例如“600519最近20个交易日表现如何？”。",
                    ["贵州茅台最近20个交易日表现如何？", "最近有哪些表现较强的行业板块？", "最近20个交易日市场整体状态如何？"], "问题对象未能匹配到可研究的指数、行业或股票，未进行取数。")
            required_codes = {"000300.SH"}
        required_calls = {"get_index_history:" + code for code in required_codes}
        required_calls.update("get_stock_history:" + code for code in securities)
        required_calls.update("get_stock_valuation:" + code for code in securities)
        if intent.task in ("market_overview", "risk_research"):
            required_calls.add("get_market_breadth:")
        required_calls.update("get_industry_history:" + name for name in industry_names)
        if comparison_end:
            required_calls.add("compare_history:" + (sorted(required_codes)[0] if required_codes else "000300.SH"))
        if wants_risk:
            required_calls.add("get_risk_context:")
        if wants_valuation:
            required_calls.add("get_valuation_context:" + ("000300.SH" if "000300.SH" in required_codes or not required_codes else sorted(required_codes)[0]))
        if wants_events:
            required_calls.add("get_event_context:")
        if intent.task == "sector_ranking":
            required_calls = {"get_sector_ranking:"}
        messages.append({"role": "system", "content": "完成本次问题至少需要尝试这些工具：" + json.dumps(sorted(required_calls))})
        for round_number in range(4):
            await event("step_started", label="DeepSeek规划取数" if round_number == 0 else "检查是否需要补充证据")
            body = await request_json(client, "POST", DEEPSEEK + "/chat/completions", headers=headers,
                                      json={"model": model, "messages": messages, "tools": tools, "max_tokens": 1400, "thinking": {"type": "disabled"}})
            message = first_message(body)
            calls = message.get("tool_calls") or []
            if not calls:
                break
            messages.append(message)
            for call in calls:
                call_count += 1
                if call_count > 10:
                    raise ProbeError("tool_budget_exceeded")
                try:
                    name = call["function"]["name"]
                    arguments = json.loads(call["function"]["arguments"])
                    if name not in {tool["function"]["name"] for tool in tools} or not isinstance(arguments, dict):
                        raise ValueError
                    stock_tools = ("get_stock_history", "get_stock_valuation")
                    coded = ("get_index_history", "compare_history", "get_valuation_context") + stock_tools
                    allowed_keys = {"industry", "purpose"} if name == "get_industry_history" else {"code", "purpose"} if name in coded else {"purpose"}
                    if set(arguments) != allowed_keys or not isinstance(arguments.get("purpose"), str) or len(arguments["purpose"]) > 300:
                        raise ValueError
                    if name in coded and arguments.get("code") not in (securities if name in stock_tools else INDICES):
                        raise ValueError
                    if name == "get_industry_history" and arguments.get("industry") not in industry_names:
                        raise ValueError
                except (KeyError, TypeError, ValueError):
                    raise ProbeError("invalid_research_tool_call") from None
                cache_key = name + ":" + arguments.get("code", arguments.get("industry", ""))
                await event("tool_started", tool=name, purpose=arguments["purpose"], code=arguments.get("code"))
                if cache_key in completed:
                    result = completed[cache_key]
                else:
                    try:
                        if name == "get_market_breadth":
                            item = await data.get_market_breadth()
                        elif name == "get_index_history":
                            item = await data.get_index_history(arguments["code"], request.window)
                        elif name == "get_stock_history":
                            item = await data.get_index_history(arguments["code"], request.window)
                        elif name == "get_stock_valuation":
                            item = await data.get_stock_valuation(arguments["code"])
                        elif name == "get_industry_history":
                            item = await data.get_industry_history(arguments["industry"], request.window)
                        elif name == "compare_history":
                            item = await data.compare_history(arguments["code"], request.window, comparison_end)
                        elif name == "get_sector_ranking":
                            item = await data.get_sector_ranking(request.window)
                        elif name == "get_valuation_context":
                            item = await bounded(data.get_valuation_context(arguments["code"]))
                        elif name == "get_event_context":
                            item = await bounded(data.get_event_context())
                        else:
                            item = await data.get_risk_context()
                        result = tool_summary(item)
                        if item["kind"] == "market_breadth" and item["data"]["quality"] == "insufficient_coverage":
                            result["not_for_conclusions"] = True
                    except ProbeError as error:
                        result = {"status": "unavailable", "reason": error.category}
                        failures.append({"tool": name, "code": arguments.get("code"), "reason": error.category})
                    completed[cache_key] = result
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})
                await event("tool_completed", tool=name, evidence_id=result.get("evidence_id"), status=result.get("status", "collected"))
            if required_calls.issubset(completed):
                break
        if not required_calls.issubset(completed):
            raise ProbeError("incomplete_research_plan")
        base = deterministic_report(data.evidence, failures)
        ranking = next((item for item in data.evidence.values() if item["kind"] == "sector_ranking"), None)
        if intent.task == "sector_ranking":
            if not ranking:
                raise ProbeError("sector_ranking_not_obtained")
            base["facts"] = [item for item in base["facts"] if ranking["id"] in item["evidence_ids"]]
            base["transition_conditions"] = [item for item in base["transition_conditions"] if item["evidence_id"] == ranking["id"]]
            base["uncertainties"] = [ranking["data"]["scope"]] + ranking["data"]["limitations"]
            base["confidence"] = {"level": "中" if ranking["data"]["coverage_pct"] >= 98 else "低",
                "scope": "只针对所列行业样本过去表现的排序，不代表未来上涨概率。", "reasons": [f"有效序列{ranking['data']['valid_count']}/{ranking['data']['expected_count']}，使用同一窗口。"]}
            base["dimensions"] = []
            base.pop("market_state", None)
        stock_items = [item for item in data.evidence.values() if item["kind"] == "index_history" and item["data"].get("instrument_type") == "stock"]
        if securities:
            if not stock_items:
                # Without the stock's own prices the answer would be about something else; fail visibly instead.
                raise ProbeError("security_history_not_obtained")
            base.update(security_report(stock_items, data.evidence, failures))
        notices = []
        for match in getattr(data, "matches", {}).values():
            if match.get("name_verified") is False:
                notices.append(f"扶摇A股代码表检索不到“{match['query']}”。本轮按{match['via']}进行研究；如果{match['code']}不是你要的股票，请改用正确的6位代码提问。")
            elif not match["exact"]:
                notices.append(f"“{match['query']}”不是代码表里的完整名称，已按{match['via']}匹配为{match['name']}（{match['code']}）；如果不是你要的股票，请改用6位代码提问。")
        base["uncertainties"][:0] = notices
        if unresolved:
            base["uncertainties"].append(f"未能匹配到“{'、'.join(unresolved)}”，本轮未纳入研究，也没有用其他对象代替。")
        available = {key: item for key, item in data.evidence.items() if item["kind"] != "market_breadth" or item["data"]["quality"] != "insufficient_coverage"}
        if not available:
            raise ProbeError("no_verified_research_evidence")
        await event("step_started", label="根据证据归纳并校验结果")
        insights, tensions = candidates(available)
        if intent.task == "sector_ranking":
            insights = {key: value for key, value in insights.items() if value["dimension"] == "板块强弱"}
            tensions = {key: value for key, value in tensions.items() if key == "sector_ranking_scope"}
        def coverage_problem(summary_ids, detail_ids):
            # The question the user asked must be answered by what is shown first, whoever wrote it.
            selected = [insights[key] for key in detail_ids]
            if intent.task == "sector_ranking" and "sector_leaders" not in summary_ids:
                return "sector_question_not_answered_first"
            if securities and not any(insights[key]["dimension"] in ("个股行情", "相对大盘") for key in summary_ids):
                return "security_question_not_answered_first"
            if risk_asked and any(e["kind"] == "sentiment" for e in available.values()) and not any(item["dimension"] == "风险变量" for item in selected):
                return "risk_question_not_addressed"
            if comparison_end and any(e["kind"] == "historical_comparison" for e in available.values()) and not any(item["dimension"] == "历史阶段" for item in selected):
                return "history_question_not_addressed"
            if industry_names and any(e["data"].get("instrument_type") == "industry" for e in available.values()) and not any(item["dimension"] in ("行业研究", "行业比较") for item in selected):
                return "industry_question_not_addressed"
            if intent.task == "valuation" and any(e["kind"] == "valuation" for e in available.values()) and not any(item["dimension"] == "估值" for item in selected):
                return "valuation_question_not_addressed"
            if intent.task == "events" and any(e["kind"] == "events" for e in available.values()) and not any(item["dimension"] == "重要事件" for item in selected):
                return "events_question_not_addressed"
            return None

        narrative, selection_record, written_followups = None, None, None
        # DeepSeek first writes the interpretation layer itself; every claim is checked against the insights it cites.
        await event("step_started", label="DeepSeek撰写解读")
        facts_for_model = [{"text": fact["text"], "evidence_ids": fact["evidence_ids"]} for fact in base.get("facts", [])]
        asked = [name for name, pattern in (("行情结构", "行情|走势|表现|涨|跌|指数|大盘|结构"), ("市场宽度", "宽度|参与度|涨跌家数|普涨|普跌"),
                    ("风格轮动", "风格|大小盘|大盘股|小盘|成长|价值"), ("估值", "估值|市盈率|市净率|PE|PB"), ("流动性", "成交|流动性|量能|缩量|放量"),
                    ("情绪", "情绪|涨停|跌停|风险|极端"), ("重要事件", "事件|政策|新闻|央行|逆回购"), ("历史阶段", "历史|上个月|相比|对比"))
                 if re.search(pattern, request.question, re.I)]
        focus = {"task": intent.task, "objects": list(securities.values()) + industry_names + [INDICES[code] for code in sorted(required_codes) if code in INDICES],
                 "asked_dimensions": asked or ["用户没有限定维度，按问题回答最重要的发现"],
                 "matched_securities": notices}
        # A narrow question is not padded with the dimensions it did not ask about, even to say they are missing.
        not_obtained = [item["name"] + "：" + item["status"] for item in base.get("dimensions", [])
                        if not item["status"].startswith("已取得") and (not narrow or item["name"] in asked)]
        writer = [{"role": "system", "content": writer_skill(skill) + "\n\n" + WRITER_INSTRUCTIONS},
                  {"role": "user", "content": json.dumps({"question": request.question, "focus": focus, "as_of": data.expected_day, "window": request.window,
                      "market_state": (base.get("market_state") or {}).get("label"), "facts": facts_for_model,
                      "insights": [{k: v for k, v in item.items() if k != "derivation"} for item in insights.values()], "tensions": list(tensions.values()),
                      "not_obtained": not_obtained,
                      "tool_failures": [{"tool": item["tool"], "reason": item["reason"]} for item in failures]}, ensure_ascii=False)}]
        tool = writer_tool(insights, tensions, available_charts(available))
        for attempt in range(WRITER_ATTEMPTS):
            if attempt and time.monotonic() - started > WRITER_DEADLINE:
                # A rewrite that would overrun the job budget is skipped; the verified selection below still answers.
                break
            response = await request_json(client, "POST", DEEPSEEK + "/chat/completions", headers=headers,
                                          json={"model": model, "messages": writer, "max_tokens": 3000, "thinking": {"type": "disabled"},
                                                "tools": [tool], "tool_choice": {"type": "function", "function": {"name": "write_grounded_narrative"}}})
            content, finish = None, (response.get("choices") or [{}])[0].get("finish_reason")
            try:
                reply = first_message(response)
                calls = [call for call in reply.get("tool_calls") or [] if call.get("function", {}).get("name") == "write_grounded_narrative"]
                if calls:
                    content = json.dumps(merge_calls([call["function"].get("arguments") or "" for call in calls]), ensure_ascii=False)
                else:
                    # Live 2026-10-07: the first forced call sometimes came back without a tool call; a JSON body in the text is still a draft.
                    text = reply.get("content") if isinstance(reply.get("content"), str) else ""
                    found = re.search(r"\{.*\}", text, re.S)
                    if not found:
                        raise NarrativeRejected([f"没有调用 write_grounded_narrative（finish_reason={finish}，正文{len(text)}字）"])
                    content = found.group(0)
                try:
                    draft = GroundedNarrative.model_validate(normalize_draft(json.loads(content)))
                except json.JSONDecodeError as error:
                    raise NarrativeRejected([f"参数不是合法JSON：{error.msg}（第{error.pos}个字符附近）"]) from None
                except ValidationError as error:
                    raise NarrativeRejected([f"{'.'.join(map(str, item['loc']))}：{item['msg']}" for item in error.errors()[:6]]) from None
                scope = f"{request.window}个交易日 {data.expected_day} " + " ".join(list(INDICES.values()) + list(securities.values()) + industry_names)
                written = check_narrative(draft, insights, tensions, asks_for_prohibited_output, scope, facts_for_model)
                detail_ids = list(dict.fromkeys(key for item in written["paragraphs"] for key in item["insight_ids"]))
                problem = coverage_problem(written["summary_ids"], detail_ids)
                if problem:
                    raise NarrativeRejected([problem])
                written_followups = written.pop("followups")
                narrative = written
                break
            except NarrativeRejected as error:
                await event("validation_retry", reason="narrative_unverified", problems=error.problems[:8])
                private_json(root / "work" / "validation-attempts" / f"{run_id}-narrative-{attempt}.json", {"problems": error.problems, "content": content, "finish_reason": finish,
                            "raw_message": json.dumps((response.get("choices") or [{}])[0].get("message"), ensure_ascii=False)[:3000] if content is None else None, "not_for_display": True})
                writer.append({"role": "assistant", "content": content if isinstance(content, str) else "{}"})
                writer.append({"role": "user", "content": "解读未通过服务端核验，请修正后重新调用 write_grounded_narrative，参数必须是合法JSON：" + "；".join(error.problems[:8]) + "。数字和日期只能来自所依据的观点或事实（可以四舍五入，正负方向不能写反）；没有依据的数字直接删掉。"})

        # If the written narrative cannot be verified, DeepSeek still orders the code-written insights by ID.
        composer = [{"role": "system", "content": skill + "\n根据用户问题，从已计算并可追溯的候选观点中选择最相关的摘要、主要矛盾和展开顺序。不要改写、补造或返回文字，只返回JSON选择。summary_ids必须包含在detail_ids中，不得重复；仅使用候选id。字段为summary_ids数组、tension_id字符串、detail_ids数组。"},
                    {"role": "user", "content": json.dumps({"question": request.question, "insights": list(insights.values()), "tensions": list(tensions.values())}, ensure_ascii=False)}]
        selection_schema = InsightSelection.model_json_schema()
        selection_schema["properties"]["summary_ids"]["items"]["enum"] = list(insights)
        selection_schema["properties"]["detail_ids"]["items"]["enum"] = list(insights)
        selection_schema["properties"]["tension_id"]["enum"] = list(tensions)
        selection_tool = {"type": "function", "function": {"name": "select_verified_insights", "description": "选择与问题相关的已核验观点，不能新增或改写结论。", "parameters": selection_schema}}
        for attempt in range(0 if narrative else 2):
            response = await request_json(client, "POST", DEEPSEEK + "/chat/completions", headers=headers,
                                          json={"model": model, "messages": composer, "max_tokens": 600, "thinking": {"type": "disabled"},
                                                "tools": [selection_tool], "tool_choice": {"type": "function", "function": {"name": "select_verified_insights"}}})
            message = first_message(response)
            content = None
            try:
                selection_calls = message.get("tool_calls") or []
                if len(selection_calls) != 1 or selection_calls[0].get("function", {}).get("name") != "select_verified_insights":
                    raise ProbeError("invalid_selection_tool_call")
                content = selection_calls[0]["function"]["arguments"]
                selection = InsightSelection.model_validate_json(content)
                # Dropping ids that are not verified insights only removes content; it never adds or rewrites any.
                dropped = [key for key in selection.summary_ids + selection.detail_ids if key not in insights]
                if dropped:
                    summary = [key for key in selection.summary_ids if key in insights]
                    if not summary:
                        raise ProbeError("unknown_insight_selection")
                    details = list(dict.fromkeys(summary + [key for key in selection.detail_ids if key in insights]))[:MAX_DETAILS]
                    selection = InsightSelection(summary_ids=summary, tension_id=selection.tension_id, detail_ids=details)
                    await event("validation_repair", dropped_ids=dropped)
                narrative = render_selection(selection, insights, tensions)
                problem = coverage_problem(selection.summary_ids, selection.detail_ids)
                if problem:
                    raise ProbeError(problem)
                narrative["author"] = "selection"
                selection_record = {**selection.model_dump(), "dropped_unknown_ids": dropped}
                break
            except (ValidationError, ProbeError, TypeError) as error:
                narrative = None
                reason = error.category if isinstance(error, ProbeError) else "invalid_selection_schema"
                await event("validation_retry", reason=reason)
                private_json(root / "work" / "validation-attempts" / f"{run_id}-selection-{attempt}.json", {"reason": reason, "content": content, "not_for_display": True})
                if attempt == 0:
                    composer.append({"role": "assistant", "content": content if isinstance(content, str) else "{}"})
                    composer.append({"role": "user", "content": "选择未通过校验：" + reason + f"。summary_ids为1至3条，detail_ids为1至{MAX_DETAILS}条；summary_ids与detail_ids只能使用insights中的id：" + "、".join(insights) + "；tension_id只能使用：" + "、".join(tensions) + "。"})
        if narrative:
            # Framework dimensions with verified evidence stay visible even when the answer leaves them out.
            cited = [key for item in narrative.get("paragraphs", []) for key in item["insight_ids"]]
            chosen = {insights[key]["dimension"] for key in cited} | {item["dimension"] for item in narrative["interpretations"]}
            missing = []
            for key, item in insights.items():
                if item["dimension"] in ("估值", "风险变量", "重要事件") and item["dimension"] not in chosen:
                    missing.append({**{k: v for k, v in item.items() if k in ("id", "text", "dimension", "evidence_ids")}, "author": "rules"})
                    chosen.add(item["dimension"])
            # They are listed apart from the answer, so a question about one thing is not padded with another.
            narrative["supplements"] = [{**item, "text": full_text(insights[item["id"]])} for item in missing]
        default_followups = ([f"{name}最近60个交易日与沪深300相比表现如何？" for name in list(securities.values())[:1]] + ["最近有哪些表现较强的行业板块？", "最近20个交易日市场整体状态如何？"] if securities else
                             ["同一区间大小盘风格有什么差异？", "银行和半导体行业在同一区间表现如何？", "末日涨跌停分布有哪些风险信号？", "与上个月末结束的同长度历史区间相比有什么不同？"])
        result = {"run_id": run_id, "question": request.question, "as_of": data.expected_day, "window": request.window,
                  "status": "completed_with_limits" if narrative else "facts_only", "created_at": datetime.now(SHANGHAI).isoformat(),
                  "narrative": narrative,
                  "narrative_warning": None if narrative else "解释未通过校验，仅呈现已验证事实。",
                  **base, "evidence": list(data.evidence.values()),
                  "followups": (written_followups or [])[:4] if len(written_followups or []) >= 2 else list(dict.fromkeys((written_followups or []) + default_followups))[:4], "notices": notices,
                  "model": model, "skill_version": SKILL_VERSION, "events": events}
        result["analysis_protocol"] = "grounded-narrative-v1" if narrative and narrative["author"] == "deepseek" else "grounded-insight-selection-v1"
        result["intent"] = intent.model_dump()
        result["presentation"] = "sector_ranking" if intent.task == "sector_ranking" else "research"
        result["skills_loaded"] = loaded_skills
        result["selected_insights"] = selection_record
        result["verified_insights"] = list(insights.values())
        await event("completed", run_id=run_id, status=result["status"])
        private_json(root / "work" / "research-runs" / f"{run_id}.json", result)
        return result
