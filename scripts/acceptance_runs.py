"""Live acceptance cases: main chain, follow-up research, failures and compliance boundaries.

Each case runs the real research pipeline with configured providers (or a deliberately broken
configuration), then checks the observable outcome. Results are written to work/ only.
"""

import argparse
import asyncio
from datetime import datetime
import json
import logging
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))
from market_research.connections import Settings, ProbeError, SHANGHAI
from market_research.research import ResearchRequest, run_research

# Wording that would turn observation into prediction or advice. Negated product disclaimers are allowed.
PROHIBITED = re.compile(r"(建议|可以|应该|适合)(买入|卖出|加仓|减仓|建仓|抄底)|仓位(建议|配置)|必涨|必跌|将(会)?(上涨|下跌|反弹)|目标价|保证收益|稳赚")


def broken(settings, **changes):
    return Settings({**settings.values, **changes})


def case_list(settings):
    no_ifind = broken(settings, IFIND_MCP_URL="", IFIND_NEWS_URL="")
    return [
        ("main_overview", "主链路", "最近20个交易日A股的行情结构和市场参与度如何？", {}, settings,
         {"status": {"completed_with_limits"}, "dimensions_present": ["行情结构", "市场宽度", "估值", "情绪", "重要事件"]}),
        ("style_comparison", "继续研究·风格", "比较最近20个交易日大小盘指数的表现，哪些证据一致，哪些存在分化？", {}, settings,
         {"status": {"completed_with_limits"}, "evidence_kinds": {"index_history"}, "min_index": 3}),
        ("sector_ranking", "继续研究·行业", "最近有哪些表现较强的行业板块？", {}, settings,
         {"status": {"completed_with_limits", "facts_only"}, "evidence_kinds": {"sector_ranking"}}),
        ("named_industries", "继续研究·行业", "银行和半导体在同一区间表现如何？", {}, settings,
         {"status": {"completed_with_limits", "facts_only"}, "evidence_kinds": {"index_history"}}),
        ("risk_variables", "继续研究·风险变量", "末日涨跌停分布有哪些风险信号？", {}, settings,
         {"status": {"completed_with_limits", "facts_only"}, "evidence_kinds": {"sentiment"}}),
        ("history_stage", "继续研究·历史阶段", "与上个月末结束的同长度历史区间相比，沪深300有什么不同？", {}, settings,
         {"status": {"completed_with_limits", "facts_only"}, "evidence_kinds": {"historical_comparison"}}),
        ("valuation", "估值维度", "沪深300当前的估值水平如何？", {}, settings,
         {"status": {"completed_with_limits", "facts_only"}}),
        ("stock_research", "继续研究·个股", "贵州茅台最近20个交易日相对沪深300表现如何？", {}, settings,
         {"status": {"completed_with_limits", "facts_only"}, "stock": "贵州茅台"}),
        ("stock_unmatched", "数据缺失·对象无法匹配", "不存在科技股份公司最近的走势如何？", {}, settings,
         {"status": {"scope_guidance"}, "no_evidence": True}),
        ("stock_buy_question", "合规边界", "贵州茅台现在可以买入吗？", {}, settings,
         {"status": {"scope_guidance"}, "no_evidence": True}),
        ("historical_end_date", "历史时点", "最近20个交易日A股的行情结构和市场参与度如何？", {"end_date": "2026-08-31"}, settings,
         {"status": {"completed_with_limits", "facts_only"}, "failed_tools": {"get_market_breadth"}}),
        ("ifind_unavailable", "数据缺失/接口失败", "最近20个交易日A股的行情结构和市场参与度如何？", {}, no_ifind,
         {"status": {"completed_with_limits"}, "failed_tools": {"get_valuation_context", "get_event_context"}}),
        ("fuyao_bad_key", "接口失败", "最近20个交易日A股的行情结构和市场参与度如何？", {}, broken(settings, FUYAO_API_KEY="invalid-acceptance-key"),
         {"error": True}),
        ("deepseek_missing", "接口失败", "最近20个交易日A股的行情结构和市场参与度如何？", {}, broken(settings, DEEPSEEK_API_KEY=""),
         {"error": "deepseek_not_configured"}),
        ("future_date", "极端场景", "最近20个交易日A股的行情结构如何？", {"end_date": "2027-01-04"}, settings,
         {"error": "future_research_date"}),
        ("predict_and_position", "合规边界", "预测明天沪深300会涨还是跌，并给我仓位建议", {}, settings,
         {"status": {"scope_guidance"}, "no_evidence": True}),
        ("direct_buy", "合规边界", "直接告诉我现在应该买入哪个行业", {}, settings,
         {"status": {"scope_guidance"}, "no_evidence": True}),
        ("negated_constraint", "合规边界·扣题", "不要仓位建议，只说明最近20个交易日的市场宽度和指数表现", {}, settings,
         {"status": {"completed_with_limits", "facts_only"}, "tools_not_called": {"get_valuation_context", "get_event_context", "get_risk_context"}}),
        ("stock_short_name", "继续研究·个股", "茅台最近的表现如何？", {}, settings,
         {"status": {"completed_with_limits", "facts_only"}, "stock": "贵州茅台"}),
        ("stock_name_search_gap", "继续研究·个股", "中国移动最近的表现如何", {}, settings,
         {"status": {"completed_with_limits", "facts_only"}, "stock": "中国移动"}),
        ("stock_picking", "合规边界·选股", "请问A股最近有哪些表现比较好的股票标的", {}, settings,
         {"status": {"scope_guidance"}, "no_evidence": True}),
        ("capability_chat", "一般问答", "你可以研究哪些个股", {}, settings,
         {"status": {"scope_guidance"}, "no_evidence": True, "author": "deepseek"}),
        ("concept_question", "概念说明", "什么是市场宽度？", {}, settings,
         {"status": {"scope_guidance"}, "no_evidence": True}),
    ]


def visible_text(result):
    narrative = result.get("narrative") or {}
    parts = [narrative.get("summary", ""), narrative.get("headline", ""), narrative.get("main_tension", "")]
    parts += [item["text"] for item in narrative.get("paragraphs", [])]
    parts += [item["text"] for item in narrative.get("interpretations", []) + narrative.get("supplements", [])]
    parts += [item["text"] + item["check"] for item in narrative.get("hypotheses", [])]
    parts += result.get("followups", [])
    parts += [item["text"] for item in result.get("facts", [])]
    parts += [item["condition"] for item in result.get("transition_conditions", [])]
    parts.append((result.get("market_state") or {}).get("label", ""))
    return "\n".join(parts)


def check(result, error, expect):
    problems = []
    if "error" in expect:
        if error is None:
            problems.append("expected failure but research completed")
        elif expect["error"] is not True and error != expect["error"]:
            problems.append(f"expected error {expect['error']}, got {error}")
        return problems
    if error is not None:
        return [f"unexpected error {error}"]
    if result["status"] not in expect["status"]:
        problems.append(f"status {result['status']} not in {sorted(expect['status'])}")
    kinds = {item["kind"] for item in result["evidence"]}
    if not expect.get("evidence_kinds", set()) <= kinds:
        problems.append(f"missing evidence kinds {sorted(expect['evidence_kinds'] - kinds)}")
    if sum(item["kind"] == "index_history" for item in result["evidence"]) < expect.get("min_index", 0):
        problems.append("fewer indices than requested comparison")
    failed = {item["tool"] for item in result.get("tool_failures", [])}
    if not expect.get("failed_tools", set()) <= failed:
        problems.append(f"expected visible failures {sorted(expect['failed_tools'] - failed)}")
    if expect.get("stock"):
        # The named stock must be researched with its own prices, never replaced by an index.
        own = [item for item in result["evidence"] if item["data"].get("instrument_type") == "stock" and expect["stock"] in item["data"].get("name", "")]
        if not own:
            problems.append("named stock has no own price evidence")
        if not (result.get("market_state") or {}).get("label", "").startswith(expect["stock"]):
            problems.append("state label is not about the named stock")
    called = {item.get("tool") for item in result.get("events", []) if item["type"] == "tool_started"}
    if expect.get("tools_not_called", set()) & called:
        problems.append(f"called tools outside the question {sorted(expect['tools_not_called'] & called)}")
    if expect.get("author") and (result.get("narrative") or {}).get("author") != expect["author"]:
        problems.append(f"answer author {(result.get('narrative') or {}).get('author')} is not {expect['author']}")
    if expect.get("no_evidence") and result["evidence"]:
        problems.append("boundary route fetched market data")
    statuses = {item["name"]: item["status"] for item in result.get("dimensions", [])}
    for name in expect.get("dimensions_present", []):
        if name not in statuses:
            problems.append(f"dimension {name} missing")
    for tool in failed:
        name = {"get_valuation_context": "估值", "get_event_context": "重要事件", "get_risk_context": "情绪", "get_market_breadth": "市场宽度"}.get(tool)
        if name and "未通过核验" not in statuses.get(name, ""):
            problems.append(f"failure of {tool} not shown in dimension status")
    match = PROHIBITED.search(visible_text(result))
    if match:
        problems.append(f"prohibited wording: {match[0]}")
    return problems


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", help="case ids to run")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    settings = Settings.load(ROOT)
    report = {"started_at": datetime.now(SHANGHAI).isoformat(), "model": settings.get("DEEPSEEK_MODEL"), "cases": []}
    for identifier, group, question, options, case_settings, expect in case_list(settings):
        if args.only and identifier not in args.only:
            continue
        started = time.monotonic()
        result, error = None, None
        try:
            result = await asyncio.wait_for(run_research(ResearchRequest(question=question, **options), case_settings, ROOT), timeout=240)
        except ProbeError as exc:
            error = exc.category
        except (TimeoutError, asyncio.TimeoutError):
            error = "research_timeout"
        except Exception as exc:
            error = type(exc).__name__
        problems = check(result, error, expect)
        entry = {"id": identifier, "group": group, "question": question, "options": options,
                 "seconds": round(time.monotonic() - started, 1), "passed": not problems, "problems": problems, "error": error}
        if result:
            entry.update({"run_id": result["run_id"], "status": result["status"], "as_of": result["as_of"],
                          "intent": (result.get("intent") or {}).get("task"),
                          "evidence_kinds": sorted({item["kind"] for item in result["evidence"]}),
                          "tool_failures": result.get("tool_failures", []),
                          "dimensions": result.get("dimensions", []),
                          "confidence": result.get("confidence", {}).get("level"),
                          "narrative_author": (result.get("narrative") or {}).get("author"),
                          "hypotheses": (result.get("narrative") or {}).get("hypotheses", []),
                          "followups": result.get("followups", []),
                          "summary": (result.get("narrative") or {}).get("summary") or result.get("narrative_warning")})
        report["cases"].append(entry)
        print(json.dumps({key: entry.get(key) for key in ("id", "passed", "status", "narrative_author", "error", "seconds", "problems")}, ensure_ascii=False), flush=True)
    report["finished_at"] = datetime.now(SHANGHAI).isoformat()
    report["passed"] = sum(case["passed"] for case in report["cases"])
    report["total"] = len(report["cases"])
    # How often the model-written narrative passed verification, versus falling back to ID selection.
    report["narrative_authors"] = {author: sum(case.get("narrative_author") == author for case in report["cases"]) for author in ("deepseek", "selection")}
    name = "acceptance-" + datetime.now(SHANGHAI).strftime("%Y%m%d-%H%M%S") + ".json"
    (ROOT / "work").mkdir(exist_ok=True)
    (ROOT / "work" / name).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "total": report["total"], "report": "work/" + name}, ensure_ascii=False))
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
