"""Narrow iFinD adapters: one dated valuation metric or attributed news records."""

import asyncio
import json
from decimal import Decimal
from datetime import date
from urllib.parse import urlparse

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.client.sse import sse_client

from .connections import ProbeError, validate_ifind_config
from .ifind_contract import decode_result, table_rows, parse_quantity


async def call_ifind(settings, tool, arguments, news=False):
    endpoint, headers = validate_ifind_config(settings)
    if news:
        endpoint = settings.get("IFIND_NEWS_URL")
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or parsed.hostname != "api-mcp.51ifind.com" or parsed.username or parsed.password:
            raise ProbeError("news_endpoint_not_configured")
    async def execute(read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await asyncio.wait_for(session.call_tool(tool, arguments), timeout=60)
            return result.model_dump(by_alias=True, exclude_none=True)
    try:
        if settings.get("IFIND_MCP_TRANSPORT") == "sse":
            async with sse_client(endpoint, headers=headers, timeout=20, sse_read_timeout=60) as (read, write):
                return await execute(read, write)
        async with httpx.AsyncClient(headers=headers, timeout=25, follow_redirects=False) as client:
            async with streamable_http_client(endpoint, http_client=client) as (read, write, _):
                return await execute(read, write)
    except ProbeError:
        raise
    except Exception:
        raise ProbeError("ifind_call_unavailable") from None


def single_valuation(result, code, day, metric):
    data = decode_result(result)
    headers, rows = table_rows(data["answer"])
    if len(rows) != 1 or rows[0].get("证券代码") != code or metric not in headers:
        raise ProbeError("valuation_entity_or_field_mismatch")
    params = data["indicators_params"].get(metric)
    compact_day = day.replace("-", "")
    if not isinstance(params, dict) or params.get("交易日期") != compact_day:
        raise ProbeError("valuation_date_unverified")
    if "日期" in rows[0] and rows[0]["日期"] not in (day, compact_day):
        raise ProbeError("valuation_date_conflict")
    value, quality = parse_quantity(rows[0][metric])
    if value is None or value <= Decimal(0) or quality != "numeric":
        raise ProbeError("valuation_not_interpretable")
    # These two explicitly named ratios are dimensionless multiples by definition.
    if metric not in ("市净率（PB）", "市盈率（PE，TTM）"):
        raise ProbeError("unsupported_valuation_definition")
    return {"metric": metric, "value": float(value), "raw_value": rows[0][metric], "unit": "倍", "date": day,
            "source_parameters": params, "date_source": "indicators_params." + metric + ".交易日期",
            "unit_source": "指标定义：市净率/市盈率为价格与账面价值/盈利之比；原响应未单列单位。",
            "limitations": ["仅展示供应商当前指标值，不给高低估结论。", "指数聚合口径和历史分位尚未完成交叉验证。"]}


def news_records(result, start, end):
    try:
        if result.get("isError"):
            raise ValueError
        body = json.loads(result["content"][0]["text"])
        if type(body.get("code")) is not int or body["code"] != 1 or body.get("subCode") not in (None, 0):
            raise ValueError
        values = json.loads(body["data"]["data"])
        if not isinstance(values, list):
            raise ValueError
    except (KeyError, IndexError, TypeError, ValueError):
        raise ProbeError("invalid_news_response") from None
    records = []
    seen = set()
    for row in values:
        if not isinstance(row, dict) or "资讯标题" not in row:
            continue
        title, excerpt, published, url = (row.get(key) for key in ("资讯标题", "资讯内容", "日期", "URL"))
        if not all(isinstance(value, str) for value in (title, excerpt, published, url)):
            continue
        parsed = urlparse(url)
        try:
            date.fromisoformat(published)
        except ValueError:
            continue
        if not start <= published <= end or parsed.scheme != "https" or parsed.username or parsed.password or url in seen:
            continue
        if parsed.hostname not in ("www.bbtnews.com.cn", "bank.cngold.org", "finance.sina.com.cn", "stock.finance.sina.com.cn"):
            continue
        seen.add(url)
        records.append({"title": title[:300], "excerpt": excerpt[:1400], "published_at": published, "url": url,
                        "status": "attributed_report", "contains_plan": any(term in excerpt for term in ("将开展", "拟开展", "计划开展"))})
    if not records:
        raise ProbeError("no_dated_news_records")
    return records[:3]
