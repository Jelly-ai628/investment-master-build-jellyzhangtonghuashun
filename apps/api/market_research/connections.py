"""Bounded read-only provider probes. Never persist credentials or raw responses."""

from __future__ import annotations

import asyncio
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from dotenv import dotenv_values

SHANGHAI = ZoneInfo("Asia/Shanghai")
DEEPSEEK = "https://api.deepseek.com"
FUYAO = "https://fuyao.aicubes.cn"
KEYS = (
    "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "FUYAO_API_KEY", "IFIND_MCP_URL",
    "IFIND_MCP_TRANSPORT", "IFIND_MCP_AUTH_HEADER", "IFIND_MCP_AUTH_VALUE",
    "IFIND_NEWS_URL",
)


class ProbeError(Exception):
    """Only controlled error categories may be written to the report."""

    def __init__(self, category: str):
        self.category = category
        super().__init__(category)


@dataclass(frozen=True, repr=False)
class Settings:
    values: dict[str, str]

    @classmethod
    def load(cls, root: Path) -> "Settings":
        # dotenv interpolation is disabled: a literal secret must not be expanded.
        local = dotenv_values(root / ".env.local", interpolate=False)
        return cls({key: os.environ.get(key, local.get(key) or "") for key in KEYS})

    def get(self, name: str) -> str:
        return self.values.get(name, "").strip()

    def ready(self, provider: str) -> bool:
        required = {
            "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"),
            "fuyao": ("FUYAO_API_KEY",),
            "ifind": ("IFIND_MCP_URL",),
        }[provider]
        return all(self.get(key) for key in required)


async def request_json(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> dict:
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.TimeoutException:
        raise ProbeError("timeout") from None
    except httpx.RequestError:
        raise ProbeError("network_error") from None
    check_http_status(response.status_code)
    try:
        body = response.json()
    except ValueError:
        raise ProbeError("invalid_json") from None
    if not isinstance(body, dict):
        raise ProbeError("invalid_schema")
    return body


def check_http_status(code: int) -> None:
    if code in (401, 403):
        raise ProbeError("authentication_or_permission_denied")
    if code == 429:
        raise ProbeError("rate_limited")
    if 300 <= code < 400:
        raise ProbeError("redirect_not_followed")
    if code >= 400:
        raise ProbeError("upstream_http_error")


def fuyao_items(body: dict) -> list[dict]:
    if type(body.get("code")) is not int:
        raise ProbeError("invalid_schema")
    category = {
        2001: "authentication_required", 2003: "permission_denied",
        3001: "instrument_not_found", 3002: "data_not_ready",
        3004: "unsupported_capability", 4001: "rate_limited",
        5001: "upstream_error", 5002: "upstream_timeout", 5003: "source_unavailable",
    }
    if body["code"] != 0:
        raise ProbeError(category.get(body["code"], "provider_business_error"))
    data = body.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("item"), list):
        raise ProbeError("invalid_schema")
    items = data["item"]
    if not items:
        raise ProbeError("empty_data")
    if not all(isinstance(item, dict) for item in items):
        raise ProbeError("invalid_schema")
    return items


def calendar_dates(items: list[dict], now: datetime) -> list[datetime]:
    dates = []
    for item in items:
        try:
            day = datetime.strptime(item["date"], "%Y%m%d").replace(tzinfo=SHANGHAI)
            stamp = item["date_ms"]
            if type(stamp) not in (int, float) or stamp != int(day.timestamp() * 1000):
                raise ValueError
            if day.date() > now.date():
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ProbeError("invalid_calendar") from None
        dates.append(day)
    if dates != sorted(set(dates)):
        raise ProbeError("invalid_calendar")
    return dates


def validate_bars(items: list[dict], start: int, end: int) -> dict:
    stamps = []
    for row in items:
        stamp, close = row.get("date_ms"), row.get("close_price")
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or not start <= stamp <= end:
            raise ProbeError("invalid_bar_time")
        if type(close) not in (int, float) or not math.isfinite(close) or close <= 0:
            raise ProbeError("invalid_close_price")
        stamps.append(stamp)
    if len(set(stamps)) != len(stamps):
        raise ProbeError("duplicate_bar_time")
    return {"row_count": len(items), "earliest_ms": min(stamps), "latest_ms": max(stamps)}


async def probe_fuyao(settings: Settings, client: httpx.AsyncClient) -> dict:
    headers = {"X-api-key": settings.get("FUYAO_API_KEY")}
    now = datetime.now(SHANGHAI)
    calendar = fuyao_items(await request_json(client, "GET", FUYAO + "/api/a-share/calendar/trading-days", headers=headers))
    days = calendar_dates(calendar, now)
    # Daily probe is deliberately conservative: never treat today's bar as finalized.
    completed = [day for day in days if day.date() < now.date()]
    if not completed:
        raise ProbeError("no_completed_trading_day")
    if len(completed) < 20:
        raise ProbeError("insufficient_calendar_history")
    last = completed[-1]
    if (now.date() - last.date()).days > 14:
        raise ProbeError("stale_calendar")
    matches = fuyao_items(await request_json(
        client, "GET", FUYAO + "/api/meta/tickers/search", headers=headers,
        params={"q": "000300.SH", "asset_type": "a-share-index", "limit": 10},
    ))
    if not any(row.get("thscode") == "000300.SH" and row.get("asset_type") == "a-share-index" for row in matches):
        raise ProbeError("index_not_resolved")
    first = completed[max(0, len(completed) - 20)]
    start = int(first.timestamp() * 1000)
    end = int((last + timedelta(days=1)).timestamp() * 1000) - 1
    bars = fuyao_items(await request_json(
        client, "GET", FUYAO + "/api/a-share-index/prices/historical", headers=headers,
        params={"thscode": "000300.SH", "interval": "1d", "start": start, "end": end},
    ))
    summary = validate_bars(bars, start, end)
    actual_dates = {datetime.fromtimestamp(row["date_ms"] / 1000, SHANGHAI).date() for row in bars}
    expected_dates = {day.date() for day in completed if day >= first}
    if actual_dates != expected_dates:
        raise ProbeError("incomplete_or_unexpected_trading_days")
    return {"capabilities": ["calendar", "index_resolution", "index_daily_history"],
            "instrument": "000300.SH", "as_of": last.date().isoformat(), **summary,
            "scope_note": "Only CSI300 daily-history smoke test; breadth and other capabilities unverified."}


def first_message(body: dict) -> dict:
    try:
        message = body["choices"][0]["message"]
        if not isinstance(message, dict):
            raise TypeError
        return message
    except (KeyError, IndexError, TypeError):
        raise ProbeError("invalid_model_response") from None


def verify_probe_call(message: dict) -> dict:
    try:
        calls = message["tool_calls"]
        if len(calls) != 1:
            raise ValueError
        call = calls[0]
        if call["type"] != "function" or call["function"]["name"] != "connection_probe" or not call["id"]:
            raise ValueError
        if json.loads(call["function"]["arguments"]) != {"value": "connectivity-check"}:
            raise ValueError
        return call
    except (KeyError, TypeError, ValueError, IndexError):
        raise ProbeError("invalid_tool_call") from None


async def probe_deepseek(settings: Settings, client: httpx.AsyncClient) -> dict:
    headers = {"Authorization": "Bearer " + settings.get("DEEPSEEK_API_KEY")}
    model = settings.get("DEEPSEEK_MODEL")
    models = await request_json(client, "GET", DEEPSEEK + "/models", headers=headers)
    if not isinstance(models.get("data"), list):
        raise ProbeError("invalid_model_catalog")
    if model not in [row.get("id") for row in models["data"] if isinstance(row, dict)]:
        raise ProbeError("configured_model_not_available")
    messages = [{"role": "user", "content": "Connectivity test only, no financial analysis. Call connection_probe with value connectivity-check. After receiving its result, output precisely CONNECTION_OK."}]
    payload = {
        "model": model, "messages": messages, "max_tokens": 512,
        "thinking": {"type": "disabled"},
        "tools": [{"type": "function", "function": {
            "name": "connection_probe", "description": "Local deterministic connectivity check; no financial data.",
            "parameters": {"type": "object", "properties": {"value": {"type": "string", "enum": ["connectivity-check"]}},
                           "required": ["value"], "additionalProperties": False},
        }}],
        "tool_choice": {"type": "function", "function": {"name": "connection_probe"}},
    }
    message = first_message(await request_json(client, "POST", DEEPSEEK + "/chat/completions", headers=headers, json=payload))
    call = verify_probe_call(message)
    # Only the allowed deterministic function is executed, never generated code.
    messages.extend([message, {"role": "tool", "tool_call_id": call["id"],
                              "content": json.dumps({"result": "CONNECTION_OK", "source": "local_synthetic_probe"})}])
    stream_payload = {"model": model, "messages": messages, "max_tokens": 128,
                      "thinking": {"type": "disabled"}, "stream": True}
    pieces = []
    finished = False
    done = False
    try:
        async with client.stream("POST", DEEPSEEK + "/chat/completions", headers=headers, json=stream_payload) as response:
            check_http_status(response.status_code)
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                value = line[5:].strip()
                if value == "[DONE]":
                    done = True
                    break
                try:
                    event = json.loads(value)
                    if "error" in event:
                        raise ProbeError("model_stream_error")
                    for choice in event.get("choices", []):
                        token = choice.get("delta", {}).get("content")
                        if token is not None:
                            if not isinstance(token, str):
                                raise ValueError
                            pieces.append(token)
                        if choice.get("finish_reason") == "stop":
                            finished = True
                except (ValueError, TypeError, AttributeError):
                    raise ProbeError("invalid_stream_event") from None
                if sum(map(len, pieces)) > 4096:
                    raise ProbeError("model_output_limit")
    except httpx.TimeoutException:
        raise ProbeError("timeout") from None
    except httpx.RequestError:
        raise ProbeError("network_error") from None
    if not done or not finished:
        raise ProbeError("incomplete_model_stream")
    if "".join(pieces).strip() != "CONNECTION_OK":
        raise ProbeError("unexpected_probe_response")
    return {"model": model, "capabilities": ["model_catalog", "tool_call", "tool_result_roundtrip", "streaming"],
            "financial_analysis_tested": False}


def validate_ifind_config(settings: Settings) -> tuple[str, dict[str, str]]:
    url = settings.get("IFIND_MCP_URL")
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not (host == "51ifind.com" or host.endswith(".51ifind.com")) or parsed.username or parsed.password or parsed.fragment:
        raise ProbeError("ifind_endpoint_requires_review")
    if settings.get("IFIND_MCP_TRANSPORT") not in ("sse", "streamable_http"):
        raise ProbeError("unsupported_mcp_transport")
    name, value = settings.get("IFIND_MCP_AUTH_HEADER"), settings.get("IFIND_MCP_AUTH_VALUE")
    if bool(name) != bool(value):
        raise ProbeError("incomplete_auth_header")
    if any(char in name + value for char in "\r\n"):
        raise ProbeError("invalid_auth_header")
    return url, {name: value} if name else {}


async def probe_ifind(settings: Settings, client: httpx.AsyncClient) -> dict:
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamable_http_client

    url, headers = validate_ifind_config(settings)

    async def discover(read: Any, write: Any) -> dict:
        async with ClientSession(read, write) as session:
            await session.initialize()
            count = 0
            cursor = None
            for _ in range(10):
                result = await session.list_tools(cursor=cursor)
                count += len(result.tools)
                # No raw schema/descriptions/URLs enter this public-safe summary.
                cursor = result.nextCursor
                if not cursor:
                    break
            if cursor:
                raise ProbeError("tool_catalog_page_limit")
            if not count:
                raise ProbeError("empty_tool_catalog")
            return {"tool_count": count, "capabilities": ["mcp_initialize", "tools_list"],
                    "financial_data_call_tested": False,
                    "scope_note": "Catalog only. Inspect schemas and select a read-only data query before enabling research."}

    # Never print key-bearing URLs. The pinned SDK restricts redirects to the same origin.
    if settings.get("IFIND_MCP_TRANSPORT") == "streamable_http":
        async with httpx.AsyncClient(headers=headers, timeout=20, follow_redirects=False) as mcp_http:
            async with streamable_http_client(url, http_client=mcp_http) as (read, write, _):
                return await discover(read, write)
    else:
        async with sse_client(url, headers=headers, timeout=20, sse_read_timeout=30) as (read, write):
            return await discover(read, write)


async def run_checks(settings: Settings, providers: list[str], live: bool) -> dict:
    report: dict[str, Any] = {
        "checked_at": datetime.now(SHANGHAI).isoformat(),
        "mode": "live" if live else "configuration_only", "providers": {},
    }
    probes = {"deepseek": probe_deepseek, "fuyao": probe_fuyao, "ifind": probe_ifind}
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        for provider in providers:
            if not settings.ready(provider):
                report["providers"][provider] = {"status": "not_configured", "live_verified": False}
                continue
            if not live:
                report["providers"][provider] = {"status": "configured_unverified", "live_verified": False}
                continue
            try:
                details = await asyncio.wait_for(probes[provider](settings, client), timeout=60)
                report["providers"][provider] = {"status": "passed", "live_verified": True, **details}
            except ProbeError as exc:
                report["providers"][provider] = {"status": "failed", "live_verified": False, "reason": exc.category}
            except (TimeoutError, asyncio.TimeoutError):
                report["providers"][provider] = {"status": "failed", "live_verified": False, "reason": "total_timeout"}
            except Exception:
                # SDK exceptions can include key-bearing URLs. Never serialize their text.
                report["providers"][provider] = {"status": "failed", "live_verified": False, "reason": "protocol_or_config_error"}
    return report
