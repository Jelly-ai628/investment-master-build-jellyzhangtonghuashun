"""Run from project root with uv run python scripts/check_connections.py."""

import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

from market_research.connections import Settings, run_checks


def main() -> int:
    # SDK transport logs may contain key-bearing URLs; this CLI emits only its own report.
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description="Provider readiness checks; credentials and raw responses are never printed.")
    parser.add_argument("--live", action="store_true", help="Call configured services. DeepSeek uses up to two small paid model requests.")
    parser.add_argument("--provider", choices=["deepseek", "fuyao", "ifind", "all"], default="all")
    args = parser.parse_args()
    providers = ["deepseek", "fuyao", "ifind"] if args.provider == "all" else [args.provider]
    report = asyncio.run(run_checks(Settings.load(ROOT), providers, args.live))
    output = ROOT / "work" / "connection-check.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    print(f"Report: {output}")
    if any(p["status"] == "failed" for p in report["providers"].values()):
        return 1
    if any(p["status"] == "not_configured" for p in report["providers"].values()):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
