"""Run one live, bounded research request; saves private evidence and report."""

import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))
from market_research.connections import Settings, ProbeError
from market_research.research import ResearchRequest, run_research


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--window", type=int, choices=[5, 10, 20, 60], default=20)
    parser.add_argument("--end-date")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)

    async def progress(event):
        print(json.dumps(event, ensure_ascii=False), flush=True)

    try:
        result = await asyncio.wait_for(run_research(ResearchRequest(question=args.question, window=args.window, end_date=args.end_date), Settings.load(ROOT), ROOT, progress), timeout=240)
    except ProbeError as error:
        print(json.dumps({"status": "failed", "reason": error.category}, ensure_ascii=False))
        return 1
    except Exception as error:
        print(json.dumps({"status": "failed", "reason": type(error).__name__}, ensure_ascii=False))
        return 1
    print(json.dumps({"run_id": result["run_id"], "status": result["status"], "as_of": result["as_of"],
                      "facts": result["facts"], "narrative": result["narrative"], "dimensions": result["dimensions"],
                      "tool_failures": result["tool_failures"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
