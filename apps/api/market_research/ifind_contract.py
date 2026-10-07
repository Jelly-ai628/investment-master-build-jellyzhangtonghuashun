"""Validate observed iFinD index/sector table responses before admitting evidence.

The observed provider envelope uses code=1 for success, unlike Fuyao's code=0.
This deliberately supports one plain Markdown table, not arbitrary generated prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import re

from .connections import ProbeError


@dataclass(frozen=True)
class ExpectedField:
    name: str
    unit: str


def decode_result(result: dict) -> dict:
    if result.get("isError") is True:
        raise ProbeError("mcp_tool_error")
    content = result.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        raise ProbeError("unsupported_mcp_content")
    block = content[0]
    if block.get("type") != "text" or not isinstance(block.get("text"), str):
        raise ProbeError("unsupported_mcp_content")
    if len(block["text"]) > 1_000_000:
        raise ProbeError("response_size_limit")
    try:
        body = json.loads(block["text"])
    except ValueError:
        raise ProbeError("invalid_ifind_json") from None
    if not isinstance(body, dict) or type(body.get("code")) is not int:
        raise ProbeError("invalid_ifind_envelope")
    if body["code"] != 1 or body.get("subCode") not in (None, 0):
        raise ProbeError("ifind_business_error")
    data = body.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("answer"), str):
        raise ProbeError("invalid_ifind_data")
    if not isinstance(data.get("indicators_params"), dict):
        raise ProbeError("missing_indicator_parameters")
    return data


def table_rows(answer: str) -> tuple[list[str], list[dict[str, str]]]:
    lines = [line.strip() for line in answer.splitlines() if line.strip()]
    if not lines:
        raise ProbeError("empty_data")
    if any(not line.startswith("|") or not line.endswith("|") or "\\|" in line for line in lines):
        raise ProbeError("unsupported_table_format")
    rows = [[cell.strip() for cell in line[1:-1].split("|")] for line in lines]
    if len(rows) < 3:
        raise ProbeError("empty_data")
    headers = rows[0]
    if len(set(headers)) != len(headers) or any(not key for key in headers):
        raise ProbeError("duplicate_or_empty_header")
    if len(rows[1]) != len(headers) or any(not re.fullmatch(r":?-{3,}:?", cell) for cell in rows[1]):
        raise ProbeError("invalid_table_separator")
    if any(len(row) != len(headers) for row in rows[2:]):
        raise ProbeError("inconsistent_table_columns")
    if len(rows) > 10003:
        raise ProbeError("table_row_limit")
    return headers, [dict(zip(headers, row)) for row in rows[2:]]


def split_header(header: str) -> tuple[str, str | None]:
    match = re.fullmatch(r"(.+?)[（(]单位[：:]\s*(.+?)[）)]", header)
    return (match[1].strip(), match[2].strip()) if match else (header, None)


def parse_quantity(raw: str) -> tuple[Decimal | None, str]:
    if raw in ("", "-", "--", "N/A", "null", "暂无"):
        return None, "missing"
    match = re.fullmatch(r"([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(万亿|亿|万)?", raw)
    if not match:
        raise ProbeError("unsupported_quantity_format")
    try:
        value = Decimal(match[1].replace(",", ""))
        multiplier = {None: 1, "万": 10_000, "亿": 100_000_000, "万亿": 1_000_000_000_000}[match[2]]
        return value * multiplier, "display_scaled" if match[2] else "numeric"
    except InvalidOperation:
        raise ProbeError("unsupported_quantity_format") from None


def inspect_result(result: dict, *, instrument: str, dates: set[str], fields: list[ExpectedField]) -> dict:
    """Return candidates and issues; transport success never implies usable evidence."""
    if not dates or not fields or len({field.name for field in fields}) != len(fields):
        raise ProbeError("invalid_validation_contract")
    data = decode_result(result)
    headers, rows = table_rows(data["answer"])
    if "证券代码" not in headers or "日期" not in headers:
        raise ProbeError("missing_identity_columns")
    identity_issues = []
    if any(row["证券代码"] != instrument for row in rows):
        identity_issues.append("instrument_mismatch")
    seen_dates = [row["日期"] for row in rows]
    if set(seen_dates) != dates:
        identity_issues.append("date_coverage_mismatch")
    if len(seen_dates) != len(set(seen_dates)):
        identity_issues.append("duplicate_dates")
    by_name = {}
    for header in headers:
        name, unit = split_header(header)
        if name in by_name:
            raise ProbeError("ambiguous_metric_header")
        by_name[name] = (header, unit)
    observations = []
    metric_issues = {}
    for field in fields:
        if field.name not in by_name:
            metric_issues[field.name] = ["requested_field_missing"]
            continue
        header, unit = by_name[field.name]
        params = data["indicators_params"].get(field.name)
        issues = list(identity_issues)
        if not isinstance(params, dict):
            issues.append("indicator_parameters_missing")
            params = {}
        parameter_unit = params.get("单位")
        if isinstance(parameter_unit, str):
            parameter_unit = parameter_unit.strip()
            if unit and unit != parameter_unit:
                issues.append("unit_metadata_conflict")
            unit = unit or parameter_unit
        if unit is None:
            issues.append("unit_not_reported")
        elif unit != field.unit:
            issues.append("unit_mismatch")
        date_params = [value for key, value in params.items() if key in ("交易日期", "截止日期", "日期")]
        if not date_params:
            issues.append("date_parameter_missing")
        elif any(value == "最新" for value in date_params):
            issues.append("dynamic_date_parameter")
        elif any(not isinstance(value, str) or value not in dates for value in date_params):
            issues.append("date_parameter_unverified")
        metric_issues[field.name] = issues
        for row_number, row in enumerate(rows, start=1):
            cell_issues = list(issues)
            if any(isinstance(value, str) and re.fullmatch(r"\d{8}", value) and value != row["日期"] for value in date_params):
                cell_issues.append("row_parameter_date_conflict")
            try:
                value, numeric_quality = parse_quantity(row[header])
                if value is None:
                    cell_issues.append("missing_value")
                elif field.unit == "家" and (value < 0 or value != value.to_integral_value()):
                    cell_issues.append("invalid_count")
                elif field.unit == "点" and value <= 0:
                    cell_issues.append("invalid_index_level")
                if numeric_quality == "display_scaled":
                    cell_issues.append("display_rounded_precision")
            except ProbeError:
                value, numeric_quality = None, "unparsed"
                cell_issues.append("unsupported_quantity_format")
            observations.append({
                "instrument": row["证券代码"], "date": row["日期"], "metric": field.name,
                "raw_value": row[header], "parsed_value": str(value) if value is not None else None,
                "reported_unit": unit, "expected_unit": field.unit, "numeric_quality": numeric_quality,
                "source_row": row_number, "source_header": header, "source_parameters": params,
                "issues": cell_issues, "eligible_for_claim": not cell_issues,
            })
    valid = sum(item["eligible_for_claim"] for item in observations)
    complete = not identity_issues and all(not issues for issues in metric_issues.values()) and valid == len(rows) * len(fields)
    return {"status": "validated" if complete else "needs_review", "row_count": len(rows),
            "eligible_observation_count": valid, "identity_issues": identity_issues,
            "metric_issues": metric_issues, "observations": observations,
            "returned_fields": headers, "scope_note": "Only declared fields and dates validated; no market-state conclusion."}
