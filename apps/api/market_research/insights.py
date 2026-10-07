"""Auditable propositions computed from evidence; AI selects their relevance and order."""

from pydantic import BaseModel, ConfigDict, Field

from .connections import ProbeError
from .sector_ranking import ranking_answer


class InsightSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary_ids: list[str] = Field(min_length=1, max_length=3)
    tension_id: str
    detail_ids: list[str] = Field(min_length=1, max_length=8)


def candidates(evidence: dict) -> tuple[dict, dict]:
    insights, tensions = {}, {}
    histories = [item for item in evidence.values() if item["kind"] == "index_history"]
    breadth = next((item for item in evidence.values() if item["kind"] == "market_breadth" and item["data"]["quality"] != "insufficient_coverage"), None)

    def add(key, text, dimension, refs, check):
        insights[key] = {"id": key, "text": text, "dimension": dimension, "evidence_ids": refs, "derivation": check}

    for index, item in enumerate(histories):
        d = item["data"]
        relation = []
        for period in (20, 60):
            ma = d.get("ma" + str(period))
            if ma is not None:
                direction = "低于" if d["last_close"] < ma else "高于" if d["last_close"] > ma else "等于"
                relation.append(f"{direction}{period}日均线")
        stock = d.get("instrument_type") == "stock"
        level = "前复权收盘价" if stock else "点位"
        add("index_" + str(index), f"{d['name']}在{d['start_date']}至{d['end_date']}的首末{level}变化为{d['window_return_pct']:+.2f}%" + (f"，区间最大回撤{d['max_drawdown_pct']:.2f}%" if stock else "") + f"；末日{level}" + ("、".join(relation) if relation else "暂无足够历史计算均线") + "。这些比较描述已发生的价格位置。",
            "个股行情" if stock else "行业研究" if d.get("instrument_type") == "industry" else "行情结构", [item["id"]], {"return_pct": d["window_return_pct"], "close": d["last_close"], "ma20": d.get("ma20"), "ma60": d.get("ma60")})
    stocks = [item for item in histories if item["data"].get("instrument_type") == "stock"]
    benchmark = next((item for item in histories if item["data"].get("code") == "000300.SH"), None)
    for index, item in enumerate(stocks):
        d = item["data"]
        if benchmark and (benchmark["data"]["start_date"], benchmark["data"]["end_date"]) == (d["start_date"], d["end_date"]):
            b = benchmark["data"]
            spread = d["window_return_pct"] - b["window_return_pct"]
            relation = "强于" if spread > 0 else "弱于" if spread < 0 else "持平于"
            add("relative_" + str(index), f"同一区间{d['name']}变化{d['window_return_pct']:+.2f}%，沪深300变化{b['window_return_pct']:+.2f}%，{d['name']}{relation}沪深300 {abs(spread):.2f}个百分点。这是已发生的相对表现，不代表后续强弱或操作结论。",
                "相对大盘", [item["id"], benchmark["id"]], {"spread_percentage_points": spread})
    if stocks:
        tensions["stock_vs_market"] = {"id": "stock_vs_market", "text": "个股与大盘的相对强弱只描述已经发生的价格路径。所属行业、公司公告和盈利变化本轮尚未核实，不能把这段差异归因于公司基本面、资金行为或行业轮动。",
            "evidence_ids": [item["id"] for item in histories]}
    # Cross-index comparison describes style or industry rotation; a single stock is compared above instead.
    histories = [item for item in histories if item["data"].get("instrument_type") != "stock"]
    same_window = len({(item["data"]["start_date"], item["data"]["end_date"]) for item in histories}) == 1
    if len(histories) > 1 and same_window:
        ordered = sorted(histories, key=lambda item: item["data"]["window_return_pct"])
        worst, best = ordered[0]["data"], ordered[-1]["data"]
        spread = best["window_return_pct"] - worst["window_return_pct"]
        values = [item["data"]["window_return_pct"] for item in histories]
        alignment = "均回落" if all(v < 0 for v in values) else "均上行" if all(v > 0 for v in values) else "方向存在分化"
        add("comparison", f"同一区间，所比较指数{alignment}。{best['name']}相对表现最强，{worst['name']}相对最弱，首末变化相差{spread:.2f}个百分点。这是已选指数之间的比较，不能据此推断所有行业或未来轮动。",
            "行业比较" if any(e["data"].get("instrument_type") == "industry" for e in histories) else "风格轮动", [item["id"] for item in histories], {"spread_percentage_points": spread, "aligned_direction": alignment})
    if breadth:
        d = breadth["data"]
        description = "下跌家数多于上涨家数" if d["decliners"] > d["advancers"] else "上涨家数多于下跌家数" if d["advancers"] > d["decliners"] else "上涨与下跌家数相同"
        add("participation", f"{d['date']}的有效样本中，{description}，上涨占比为{d['advancing_ratio_pct']:.2f}%。这一观察只覆盖该交易日，尚不能说明整个研究区间的参与度是否持续改善或恶化。",
            "市场宽度", [breadth["id"]], {"advancers": d["advancers"], "decliners": d["decliners"], "denominator": d["eligible_count"]})
        add("coverage", f"本次核验覆盖代码表的{d['coverage_pct']:.2f}%，有{d['excluded_count']}条记录未进入宽度分母。排除代表资料未满足本次核验条件，不代表这些证券全部停牌。",
            "市场宽度", [breadth["id"]], {"coverage": d["coverage_pct"], "excluded": d["excluded_count"]})
        turnover = d.get("turnover_history", [])
        if len(turnover) >= 2 and turnover[-2]["turnover_cny"] > 0:
            last, previous = turnover[-1], turnover[-2]
            change = (last["turnover_cny"] / previous["turnover_cny"] - 1) * 100
            rank = 1 + sum(row["turnover_cny"] < last["turnover_cny"] for row in turnover)
            direction = "上升" if change > 0 else "下降" if change < 0 else "持平"
            add("turnover", f"{last['date']}成交额较前一个已覆盖交易日{direction}{abs(change):.2f}%，处于所覆盖{len(turnover)}个交易日中第{rank}低的位置。各日有效样本数有所不同；成交额仅反映交易活跃度。",
                "流动性", [breadth["id"]], {"previous_date": previous["date"], "change_pct": change, "ascending_rank": rank, "sample_count": len(turnover)})
            if histories:
                tensions["activity_vs_price"] = {"id": "activity_vs_price", "text": f"区间指数表现与末日成交活跃度属于不同时间尺度。末日成交额较前一覆盖交易日{direction}，但不能凭一个交易日确认区间趋势已经改变；需要继续核对同口径参与度与价格表现。",
                    "evidence_ids": [breadth["id"]] + [item["id"] for item in histories]}
    refs = list(evidence)
    for i, item in enumerate(evidence.values()):
        d = item["data"]
        if item["kind"] == "sentiment":
            add("risk_" + str(i), f"{d['date']}核验涨停池{d['limit_up_count']}家、跌停池{d['limit_down_count']}家。这是供应商定义下的单日极端交易分布，不能替代整体市场宽度，也不能推断下一交易日的方向。",
                "风险变量", [item["id"]], {"limit_up_count": d["limit_up_count"], "limit_down_count": d["limit_down_count"]})
        elif item["kind"] == "historical_comparison":
            current, previous = d["current"], d["previous"]
            add("history_" + str(i), f"{d['name']}当前区间与截至{previous['end_date']}的同长度历史区间，首末变化相差{d['return_difference_pp']:+.2f}个百分点。比较的是已经发生的价格路径；历史阶段的政策、估值和成分差异尚未核实，不据此外推未来表现。",
                "历史阶段", [item["id"]], {"difference_pp": d["return_difference_pp"], "current_range": [current["start_date"], current["end_date"]], "previous_range": [previous["start_date"], previous["end_date"]]})
        elif item["kind"] == "valuation":
            values = "、".join(f"{row['metric']}为{row['value']:.2f}倍" for row in d["items"])
            missing = "；" + "、".join(row["metric"] for row in d["missing_metrics"]) + "未通过核验" if d["missing_metrics"] else ""
            when = "最新估值快照（数据时间" + d["date"] + "）" if d.get("snapshot") else "截至日指标水平"
            add("valuation_" + str(i), f"{d['name']}在{d['date']}的{values}{missing}。这是供应商给出的{when}，缺少同口径历史序列，不能据此判断估值处于高位或低位。",
                "估值", [item["id"]], {"metrics": {row["metric"]: row["value"] for row in d["items"]}, "missing": [row["metric"] for row in d["missing_metrics"]]})
        elif item["kind"] == "events":
            titles = "；".join(f"{row['published_at']}《{row['title']}》" for row in d["records"])
            plan = "其中包含拟实施的安排，计划不等于已经实施。" if any(row["contains_plan"] for row in d["records"]) else ""
            add("events_" + str(i), f"{d['start_date']}至{d['date']}检索到{len(d['records'])}篇已核对原网页标题与日期的流动性相关报道：{titles}。{plan}报道说明发生了什么，不能证明它与指数或参与度变化之间存在因果。",
                "重要事件", [item["id"]], {"record_count": len(d["records"]), "range": [d["start_date"], d["date"]]})
        elif item["kind"] == "sector_ranking":
            add("sector_leaders", ranking_answer(d), "板块强弱", [item["id"]], {"ranking_by": "window_return_pct", "window": d["window"]})
            for rank, row in enumerate(d["ranking"][:5], start=1):
                add("sector_" + str(rank), f"{row['name']}在比较样本中列第{rank}，区间变化{row['window_return_pct']:+.2f}%，近5个交易日变化{row['five_day_return_pct']:+.2f}%，相对沪深300多{row['relative_return_pp']:+.2f}个百分点。两个窗口描述不同长度的已发生表现。",
                    "板块强弱", [item["id"]], {"code": row["code"], "return_pct": row["window_return_pct"], "five_day_return_pct": row["five_day_return_pct"]})
            add("sector_coverage", f"本次有效行业序列为{d['valid_count']}/{d['expected_count']}，统一使用{d['start_date']}至{d['end_date']}。"+d["scope"], "板块强弱", [item["id"]], {"coverage": d["coverage_pct"]})
            tensions["sector_ranking_scope"] = {"id": "sector_ranking_scope", "text": "累计表现靠前与最近几日继续走强不是一回事。需要同时查看近5日表现，并在下一交易日按相同窗口重算；这份排序不等于未来表现或操作结论。", "evidence_ids": [item["id"]]}
    kinds = {item["kind"] for item in evidence.values()}
    gaps = [name for name, kind in (("估值", "valuation"), ("事件", "events")) if kind not in kinds]
    if gaps:
        gap_text = "行情与参与度可以描述已发生的市场现象，但" + "、".join(gaps + ["宏观"]) + "证据尚未完成核验，现阶段不能把这些现象归因于某个政策、资金来源或基本面驱动。"
    else:
        gap_text = "已取得的估值水平和事件报道只能提供背景：估值缺少历史分位，事件仅覆盖流动性相关报道，宏观数据尚未核验，现阶段不能把行情归因于某个政策或基本面驱动。"
    tensions["evidence_gaps"] = {"id": "evidence_gaps", "text": gap_text, "evidence_ids": refs}
    if histories and breadth:
        tensions["different_windows"] = {"id": "different_windows", "text": "区间指数变化与末日市场宽度不能互相替代。即使两者方向一致，也缺少整个区间的连续宽度证据来证明参与度的持续性。", "evidence_ids": refs}
    return insights, tensions


def render_selection(selection: InsightSelection, insights: dict, tensions: dict) -> dict:
    if selection.tension_id not in tensions:
        raise ProbeError("unknown_tension_selection")
    if any(key not in insights for key in selection.summary_ids + selection.detail_ids):
        raise ProbeError("unknown_insight_selection")
    if len(set(selection.detail_ids)) != len(selection.detail_ids) or len(set(selection.summary_ids)) != len(selection.summary_ids):
        raise ProbeError("duplicate_insight_selection")
    if not set(selection.summary_ids).issubset(selection.detail_ids):
        raise ProbeError("summary_without_supporting_detail")
    return {"summary": " ".join(insights[key]["text"] for key in selection.summary_ids),
            "main_tension": tensions[selection.tension_id]["text"],
            "summary_evidence_ids": list(dict.fromkeys(ref for key in selection.summary_ids for ref in insights[key]["evidence_ids"])),
            "tension_evidence_ids": tensions[selection.tension_id]["evidence_ids"],
            "interpretations": [{key: value for key, value in insights[identifier].items() if key in ("text", "dimension", "evidence_ids")} for identifier in selection.detail_ids]}
