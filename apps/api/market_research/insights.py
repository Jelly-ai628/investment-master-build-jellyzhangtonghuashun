"""Auditable propositions computed from evidence; AI writes about them or, as a fallback, selects and orders them."""

from pydantic import BaseModel, ConfigDict, Field

from .connections import ProbeError
from .sector_ranking import ranking_answer


# Every evidence kind can contribute a direction, a range and a caveat, so a full overview needs room for about ten.
MAX_DETAILS = 10


class InsightSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary_ids: list[str] = Field(min_length=1, max_length=3)
    tension_id: str
    detail_ids: list[str] = Field(min_length=1, max_length=MAX_DETAILS)


def full_text(insight: dict) -> str:
    return insight["text"] + (insight["caveat"] if insight.get("caveat") else "")


def candidates(evidence: dict) -> tuple[dict, dict]:
    # Each insight states what the data shows; its caveat (what it cannot show) is kept apart so it is said once, where it matters.
    insights, tensions = {}, {}
    histories = [item for item in evidence.values() if item["kind"] == "index_history"]
    breadth = next((item for item in evidence.values() if item["kind"] == "market_breadth" and item["data"]["quality"] != "insufficient_coverage"), None)
    sentiment = next((item for item in evidence.values() if item["kind"] == "sentiment"), None)

    def add(key, text, dimension, refs, check, caveat=""):
        insights[key] = {"id": key, "text": text, "caveat": caveat, "dimension": dimension, "evidence_ids": refs, "derivation": check}

    def tension(key, text, refs, kind="data"):
        tensions[key] = {"id": key, "text": text, "evidence_ids": refs, "kind": kind}

    positions = {}
    for index, item in enumerate(histories):
        d = item["data"]
        relation = []
        for period in (20, 60):
            ma = d.get("ma" + str(period))
            if ma is not None:
                relation.append(("低于" if d["last_close"] < ma else "高于" if d["last_close"] > ma else "等于") + f"{period}日均线")
        stock = d.get("instrument_type") == "stock"
        level = "前复权收盘价" if stock else "点位"
        dimension = "个股行情" if stock else "行业研究" if d.get("instrument_type") == "industry" else "行情结构"
        drawdown = f"，区间最大回撤{d['max_drawdown_pct']:.2f}%" if d.get("max_drawdown_pct") is not None else ""
        missing = d.get("missing_dates") or []
        gaps = f"区间内有{len(missing)}个交易日没有该股日K（可能停牌或尚未上市），按已有交易日计算。" if missing else ""
        add("index_" + str(index), f"{d['name']}在{d['start_date']}至{d['end_date']}的首末{level}变化{d['window_return_pct']:+.2f}%{drawdown}；末日" + ("、".join(relation) if relation else "暂无足够历史计算均线") + "。" + gaps,
            dimension, [item["id"]], {"return_pct": d["window_return_pct"], "close": d["last_close"], "ma20": d.get("ma20"), "ma60": d.get("ma60")},
            "均线与回撤只描述已经走过的价格路径。")
        closes = [row["close"] for row in d.get("series", [])]
        if len(closes) >= 2 and max(closes) > min(closes):
            # Where the last close sits inside the window's closing range: describes the path, not support or resistance.
            high, low, last = max(closes), min(closes), closes[-1]
            position = (last - low) / (high - low) * 100
            positions[item["id"]] = position
            unit = "元（前复权）" if stock else "点"
            add("range_" + str(index), f"{d['name']}区间收盘最低{low:.2f}{unit}、最高{high:.2f}{unit}，末日{last:.2f}{unit}位于这一区间自低到高的{position:.0f}%处，较区间最高收盘{(last / high - 1) * 100:+.2f}%，较最低收盘{(last / low - 1) * 100:+.2f}%。",
                dimension, [item["id"]], {"low": low, "high": high, "last": last, "position_pct": position}, "高低点按收盘计算，不是支撑位或压力位。")
    stocks = [item for item in histories if item["data"].get("instrument_type") == "stock"]
    benchmark = next((item for item in histories if item["data"].get("code") == "000300.SH"), None)
    for index, item in enumerate(stocks):
        d = item["data"]
        if benchmark and (benchmark["data"]["start_date"], benchmark["data"]["end_date"]) == (d["start_date"], d["end_date"]):
            b = benchmark["data"]
            spread = d["window_return_pct"] - b["window_return_pct"]
            relation = "强于" if spread > 0 else "弱于" if spread < 0 else "持平于"
            add("relative_" + str(index), f"同一区间{d['name']}变化{d['window_return_pct']:+.2f}%，沪深300变化{b['window_return_pct']:+.2f}%，{d['name']}{relation}沪深300 {abs(spread):.2f}个百分点。",
                "相对大盘", [item["id"], benchmark["id"]], {"spread_percentage_points": spread}, "这是已发生的相对表现。")
            below = d.get("ma20") is not None and d["last_close"] < d["ma20"]
            if spread > 0 and (d["window_return_pct"] < 0 or below):
                tension("stock_vs_market", f"{d['name']}同一区间比沪深300强{spread:.2f}个百分点，但自身首末变化{d['window_return_pct']:+.2f}%" + ("、末日仍低于20日均线" if below else "") + "：相对抗跌不等于自身走强。",
                        [item["id"], benchmark["id"]])
            elif spread < 0 and d["window_return_pct"] > 0:
                tension("stock_vs_market", f"{d['name']}首末变化{d['window_return_pct']:+.2f}%，自身在涨，但同一区间比沪深300弱{abs(spread):.2f}个百分点：上涨幅度不及大盘。",
                        [item["id"], benchmark["id"]])
    if stocks and "stock_vs_market" not in tensions:
        tension("stock_vs_market", "个股与大盘的相对强弱只描述已经发生的价格路径。所属行业、公司公告和盈利变化本轮没有核实，这段差异还不能归因于公司基本面、资金或行业轮动。",
                [item["id"] for item in histories], "method")
    # Cross-index comparison describes style or industry rotation; a single stock is compared above instead.
    histories = [item for item in histories if item["data"].get("instrument_type") != "stock"]
    same_window = len({(item["data"]["start_date"], item["data"]["end_date"]) for item in histories}) == 1
    if len(histories) > 1 and same_window:
        ordered = sorted(histories, key=lambda item: item["data"]["window_return_pct"])
        worst, best = ordered[0]["data"], ordered[-1]["data"]
        spread = best["window_return_pct"] - worst["window_return_pct"]
        values = [item["data"]["window_return_pct"] for item in histories]
        alignment = "均回落" if all(v < 0 for v in values) else "均上行" if all(v > 0 for v in values) else "方向存在分化"
        industry = any(e["data"].get("instrument_type") == "industry" for e in histories)
        add("comparison", f"同一区间所比较指数{alignment}：{best['name']}相对最强（{best['window_return_pct']:+.2f}%），{worst['name']}相对最弱（{worst['window_return_pct']:+.2f}%），首末变化相差{spread:.2f}个百分点。",
            "行业比较" if industry else "风格轮动", [item["id"] for item in histories], {"spread_percentage_points": spread, "aligned_direction": alignment},
            "这只对所选指数成立，不代表所有行业或风格。")
        if alignment == "方向存在分化" or spread >= 2:
            tension("divergence", f"同一区间{best['name']}{best['window_return_pct']:+.2f}%、{worst['name']}{worst['window_return_pct']:+.2f}%，相差{spread:.2f}个百分点：所比较的对象并没有一起动，差异来自哪里还需要拆开看。",
                    [item["id"] for item in histories])
    main = benchmark if benchmark and benchmark["data"].get("instrument_type") != "stock" else next(iter(histories), None)
    if breadth:
        d = breadth["data"]
        add("participation", f"{d['date']}有效样本{d['eligible_count']}家中，上涨{d.get('advancers')}家、下跌{d.get('decliners')}家、平盘{d.get('unchanged', 0)}家，上涨占比{d['advancing_ratio_pct']:.2f}%。",
            "市场宽度", [breadth["id"]], {"advancers": d["advancers"], "decliners": d["decliners"], "denominator": d["eligible_count"]},
            "这只是一个交易日的截面，不能说明整个区间的参与度。")
        add("coverage", f"宽度统计覆盖代码表的{d['coverage_pct']:.2f}%，有{d['excluded_count']}条记录未通过核验、未进入分母。",
            "市场宽度", [breadth["id"]], {"coverage": d["coverage_pct"], "excluded": d["excluded_count"]}, "被排除不代表这些证券停牌。")
        if main:
            m = main["data"]
            ratio, ret = d["advancing_ratio_pct"], m["window_return_pct"]
            spot = f"、末日位于区间收盘自低到高的{positions[main['id']]:.0f}%处" if main["id"] in positions else ""
            if ret < 0 and ratio >= 45:
                tension("price_vs_breadth", f"{m['name']}区间首末变化{ret:+.2f}%{spot}，看起来偏弱；但末日上涨占比仍有{ratio:.2f}%，下跌的股票并不明显多于上涨的。指数的弱势有多少来自少数权重股，是眼下最该弄清的问题。",
                        [main["id"], breadth["id"]])
            elif ret > 0 and ratio < 50:
                tension("price_vs_breadth", f"{m['name']}区间首末变化{ret:+.2f}%，但末日上涨占比只有{ratio:.2f}%：指数在涨，多数股票却没有跟上，上涨可能集中在少数股票。",
                        [main["id"], breadth["id"]])
        turnover = d.get("turnover_history", [])
        if len(turnover) >= 2 and turnover[-2]["turnover_cny"] > 0:
            last, previous = turnover[-1], turnover[-2]
            change = (last["turnover_cny"] / previous["turnover_cny"] - 1) * 100
            rank = 1 + sum(row["turnover_cny"] < last["turnover_cny"] for row in turnover)
            direction = "上升" if change > 0 else "下降" if change < 0 else "持平"
            add("turnover", f"{last['date']}全市场成交额较前一个已覆盖交易日{direction}{abs(change):.2f}%，在所覆盖{len(turnover)}个交易日中排第{rank}低。",
                "流动性", [breadth["id"]], {"previous_date": previous["date"], "change_pct": change, "ascending_rank": rank, "sample_count": len(turnover)},
                "成交额只反映交易活跃度，各日有效样本数略有不同。")
            if len(turnover) >= 6 and sum(row["turnover_cny"] for row in turnover) > 0:
                # Pace of activity: recent five-day average against the whole covered span, plus how many of those days expanded.
                recent = turnover[-5:]
                recent_avg = sum(row["turnover_cny"] for row in recent) / 5
                overall_avg = sum(row["turnover_cny"] for row in turnover) / len(turnover)
                pace = recent_avg / overall_avg * 100
                expanded = sum(turnover[i]["turnover_cny"] > turnover[i - 1]["turnover_cny"] for i in range(len(turnover) - 5, len(turnover)))
                counts = [row["valid_count"] for row in turnover if "valid_count" in row]
                samples = f"各日有效样本数在{min(counts)}至{max(counts)}只之间；" if counts else ""
                add("turnover_trend", f"最近5个已覆盖交易日（{recent[0]['date']}至{recent[-1]['date']}）全市场日均成交额{recent_avg / 1e8:.0f}亿元，是所覆盖{len(turnover)}个交易日日均{overall_avg / 1e8:.0f}亿元的{pace:.1f}%；这5天中有{expanded}天较前一日放大。",
                    "流动性", [breadth["id"]], {"recent_avg_cny": recent_avg, "overall_avg_cny": overall_avg, "expanded_days": expanded, "sample_count": len(turnover)},
                    samples + "这是交易活跃度的节奏，不代表资金净流入。")
                if main and (pace < 95 or pace > 105):
                    ret = main["data"]["window_return_pct"]
                    shape = ("缩量" if pace < 95 else "放量") + ("下跌" if ret < 0 else "上涨")
                    if shape != "放量上涨":
                        reading = {"缩量下跌": "卖压是在释放还是只是交投变淡，单看价格分不出来",
                                   "放量下跌": "成交放大时价格走弱，分歧在加大",
                                   "缩量上涨": "上涨没有得到成交的配合，持续性需要更多证据"}[shape]
                        tension("activity_vs_price", f"{main['data']['name']}区间首末变化{ret:+.2f}%，最近5个交易日日均成交额是覆盖期日均的{pace:.1f}%，属于{shape}：{reading}。",
                                [main["id"], breadth["id"]])
    if sentiment and main:
        s, ret = sentiment["data"], main["data"]["window_return_pct"]
        up, down = s["limit_up_count"], s["limit_down_count"]
        if ret < 0 and up >= 2 * max(down, 1):
            tension("sentiment_vs_price", f"{main['data']['name']}区间首末变化{ret:+.2f}%，但{s['date']}涨停{up}家、跌停{down}家，涨停明显多于跌停：指数走弱的同时，局部仍有活跃的交易。",
                    [main["id"], sentiment["id"]])
        elif ret > 0 and down >= 2 * max(up, 1):
            tension("sentiment_vs_price", f"{main['data']['name']}区间首末变化{ret:+.2f}%，但{s['date']}跌停{down}家、涨停{up}家：指数在涨，极端下跌却更多。",
                    [main["id"], sentiment["id"]])
    refs = list(evidence)
    for i, item in enumerate(evidence.values()):
        d = item["data"]
        if item["kind"] == "sentiment":
            add("risk_" + str(i), f"{d['date']}涨停池{d['limit_up_count']}家、跌停池{d['limit_down_count']}家。",
                "风险变量", [item["id"]], {"limit_up_count": d["limit_up_count"], "limit_down_count": d["limit_down_count"]},
                "这是供应商口径的单日极端交易分布，不能替代市场宽度，也不能推断下一交易日。")
        elif item["kind"] == "historical_comparison":
            current, previous = d["current"], d["previous"]
            add("history_" + str(i), f"{d['name']}当前区间与截至{previous['end_date']}的同长度历史区间相比，首末变化相差{d['return_difference_pp']:+.2f}个百分点。",
                "历史阶段", [item["id"]], {"difference_pp": d["return_difference_pp"], "current_range": [current["start_date"], current["end_date"]], "previous_range": [previous["start_date"], previous["end_date"]]},
                "两个阶段的政策、估值和成分差异本轮没有核实，不据此外推。")
        elif item["kind"] == "valuation":
            values = "、".join(f"{row['metric']}为{row['value']:.2f}倍" for row in d["items"])
            missing = "；" + "、".join(row["metric"] for row in d["missing_metrics"]) + "未通过核验" if d["missing_metrics"] else ""
            when = "最新估值快照（数据时间" + d["date"] + "）" if d.get("snapshot") else "截至日指标水平"
            add("valuation_" + str(i), f"{d['name']}在{d['date']}的{values}{missing}。",
                "估值", [item["id"]], {"metrics": {row["metric"]: row["value"] for row in d["items"]}, "missing": [row["metric"] for row in d["missing_metrics"]]},
                f"这是供应商给出的{when}，缺少同口径历史序列，不能据此判断估值处于高位或低位。")
        elif item["kind"] == "events":
            titles = "；".join(f"{row['published_at']}《{row['title']}》" for row in d["records"])
            plan = "其中包含拟实施的安排，计划不等于已经实施。" if any(row["contains_plan"] for row in d["records"]) else ""
            add("events_" + str(i), f"{d['start_date']}至{d['date']}检索到{len(d['records'])}篇已核对原网页标题与日期的央行流动性相关报道：{titles}。{plan}",
                "重要事件", [item["id"]], {"record_count": len(d["records"]), "range": [d["start_date"], d["date"]]},
                "报道说明发生了什么，不能证明它与指数或参与度变化之间存在因果。")
        elif item["kind"] == "sector_ranking":
            add("sector_leaders", ranking_answer(d), "板块强弱", [item["id"]], {"ranking_by": "window_return_pct", "window": d["window"]}, "这是已发生的行业指数表现。")
            for rank, row in enumerate(d["ranking"][:5], start=1):
                add("sector_" + str(rank), f"{row['name']}在比较样本中列第{rank}，区间变化{row['window_return_pct']:+.2f}%，近5个交易日变化{row['five_day_return_pct']:+.2f}%，相对沪深300{row['relative_return_pp']:+.2f}个百分点。",
                    "板块强弱", [item["id"]], {"code": row["code"], "return_pct": row["window_return_pct"], "five_day_return_pct": row["five_day_return_pct"]})
            add("sector_coverage", f"本次有效行业序列为{d['valid_count']}/{d['expected_count']}，统一使用{d['start_date']}至{d['end_date']}。", "板块强弱", [item["id"]], {"coverage": d["coverage_pct"]}, d["scope"])
            faded = [row for row in d["ranking"][:5] if row["five_day_return_pct"] < 0]
            if faded:
                tension("sector_ranking_scope", "累计排在前五的行业里，" + "、".join(f"{row['name']}近5个交易日{row['five_day_return_pct']:+.2f}%" for row in faded[:3]) + "：区间累计靠前，最近几天却在回落，排名和最近的势头并不一致。", [item["id"]])
            else:
                tension("sector_ranking_scope", "累计表现靠前与最近几日继续走强不是一回事。需要同时看近5日表现，并在下一交易日按相同窗口重算。", [item["id"]], "method")
    kinds = {item["kind"] for item in evidence.values()}
    gaps = [name for name, kind in (("估值", "valuation"), ("事件", "events")) if kind not in kinds]
    if gaps:
        gap_text = "行情与参与度能描述已经发生的现象，但" + "、".join(gaps + ["宏观"]) + "证据本轮没有取得，还不能把这些现象归因于某个政策、资金来源或基本面驱动。"
    else:
        gap_text = "估值只有当前水平、事件只覆盖流动性报道、宏观数据没有取得，还不能把行情归因于某个政策或基本面驱动。"
    tension("evidence_gaps", gap_text, refs, "method")
    if histories and breadth:
        tension("different_windows", "区间指数变化覆盖整段时间，市场宽度只覆盖最后一个交易日，两者不能互相替代；缺少整段区间的连续宽度证据。", refs, "method")
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
    return {"summary": "".join(insights[key]["text"] for key in selection.summary_ids),
            "main_tension": tensions[selection.tension_id]["text"],
            "summary_evidence_ids": list(dict.fromkeys(ref for key in selection.summary_ids for ref in insights[key]["evidence_ids"])),
            "tension_evidence_ids": tensions[selection.tension_id]["evidence_ids"],
            "interpretations": [{"text": full_text(insights[identifier]), "dimension": insights[identifier]["dimension"], "evidence_ids": insights[identifier]["evidence_ids"]}
                                for identifier in selection.detail_ids]}
