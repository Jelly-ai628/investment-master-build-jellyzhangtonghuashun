"""Live check that named stocks are routed to scope guidance instead of a substituted index."""

import asyncio
import json
from pathlib import Path
import sys

import httpx

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "apps" / "api"))
from market_research.connections import Settings  # noqa: E402
from market_research.intent import classify_question  # noqa: E402

QUESTIONS = ["贵州茅台的涨跌和趋势如何？", "600519最近走势怎么样", "最近20个交易日市场整体状态如何？", "白酒行业最近表现如何？"]


async def main():
    settings = Settings.load(root)
    rows = []
    async with httpx.AsyncClient(timeout=40) as client:
        for question in QUESTIONS:
            intent = await classify_question(client, settings, question, "", 20)
            rows.append({"question": question, **intent.model_dump()})
    output = root / "work" / "intent-check.json"
    output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    print(output)


asyncio.run(main())
