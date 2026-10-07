"""Revalidate the recorded P0 samples without network calls or model requests."""

import json
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))
from market_research.connections import SHANGHAI
from market_research.ifind_contract import ExpectedField, inspect_result, decode_result, table_rows


def main():
    work = ROOT / "work"
    index_fields = [ExpectedField("收盘价", "点"), ExpectedField("市盈率（PE，TTM）", "倍"), ExpectedField("市净率（PB）", "倍")]
    count_fields = [ExpectedField(name, "家") for name in ["上涨股票数量", "下跌股票数量", "平盘股票数量", "停牌股票数量"]]
    cases = [
        ("ifind-index_data-20260930", "000300.SH", {"20260928", "20260929", "20260930"}, index_fields),
        ("ifind-clarification-index_data", "000300.SH", {"20260831"}, index_fields),
        ("ifind-sector_data-20260930", "001005010", {"20260930"}, count_fields + [ExpectedField("成份股个数", "家"), ExpectedField("成交金额(合计)", "元")]),
        ("ifind-clarification-sector_data", "001005010", {"20260930"}, count_fields),
    ]
    required = [work / (case[0] + ".json") for case in cases] + [work / "fuyao-ifind-comparison-20260930.json"]
    if not all(path.exists() for path in required):
        print("Recorded local samples are missing. No network requests made.")
        return 2
    report = {"checked_at": datetime.now(SHANGHAI).isoformat(), "queries": []}
    for name, instrument, dates, fields in cases:
        path = work / (name + ".json")
        sample = json.loads(path.read_text())
        result = inspect_result(sample["response"], instrument=instrument, dates=dates, fields=fields)
        report["queries"].append({"source_file": str(path.relative_to(ROOT)), "tool": sample["tool"], "inspection": result})
        print(f"{name}: {result['status']}; eligible observations: {result['eligible_observation_count']}")
    fuyao = json.loads(required[-1].read_text())
    bydate = {datetime.fromtimestamp(row["date_ms"] / 1000, SHANGHAI).strftime("%Y%m%d"): Decimal(str(row["close_price"])) for row in fuyao["rows"]}
    assert len(bydate) == len(fuyao["rows"]) == 3
    sample = json.loads(required[0].read_text())
    _, rows = table_rows(decode_result(sample["response"])["answer"])
    assert {row["日期"] for row in rows} == set(bydate) and len(rows) == 3
    checks = []
    for row in rows:
        left, right = Decimal(row["收盘价（单位：元）"]), bydate[row["日期"]]
        checks.append({"date": row["日期"], "absolute_difference": str(abs(left - right)),
                       "matches_at_fuyao_precision": left.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) == right})
    report["cross_source_close_check"] = {"instrument": "000300.SH", "checks": checks,
        "all_match": all(check["matches_at_fuyao_precision"] for check in checks),
        "limitations": ["Numeric agreement only; iFinD unit label remains unverified.",
                        "Both providers belong to Tonghuashun; not independent-source validation.",
                        "No PE/PB cross-validation performed."]}
    output = work / "ifind-data-quality-report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    output.chmod(0o600)
    print(f"Local report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
