"""Live end-to-end check that a named stock is researched with its own prices, not a substituted index."""

import asyncio
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "apps" / "api"))
from market_research.connections import ProbeError, Settings  # noqa: E402
from market_research.research import ResearchRequest, run_research  # noqa: E402

QUESTIONS = sys.argv[1:] or ["贵州茅台的涨跌和趋势如何？", "600519最近20个交易日走势怎么样？", "不存在公司的走势如何？"]


async def main():
    rows = []
    for question in QUESTIONS:
        try:
            result = await run_research(ResearchRequest(question=question), Settings.load(root), root)
            rows.append({"question": question, "run_id": result["run_id"], "status": result["status"], "intent": result.get("intent"),
                         "market_state": (result.get("market_state") or {}).get("label"), "summary": (result.get("narrative") or {}).get("summary"),
                         "facts": [fact["text"] for fact in result["facts"]], "dimensions": result["dimensions"], "tool_failures": result["tool_failures"],
                         "tools": [(event.get("tool"), event.get("code"), event.get("status")) for event in result["events"] if event["type"] in ("tool_started", "tool_completed")]})
        except ProbeError as error:
            rows.append({"question": question, "error": error.category})
    output = root / "work" / "stock-research-check.json"
    output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    print(output, [(row["question"], row.get("status", row.get("error"))) for row in rows])


asyncio.run(main())
