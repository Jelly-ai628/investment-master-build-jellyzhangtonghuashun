import asyncio
import json
from datetime import datetime, timedelta

import httpx
import pytest

from market_research.connections import (
    ProbeError, Settings, SHANGHAI, calendar_dates, fuyao_items,
    probe_deepseek, probe_fuyao, probe_ifind, request_json, run_checks,
    validate_bars, validate_ifind_config, verify_probe_call,
)


def run(coroutine):
    return asyncio.run(coroutine)


def envelope(items, code=0):
    return {"code": code, "data": {"item": items}}


@pytest.mark.parametrize("code,category", [
    (2001, "authentication_required"), (2003, "permission_denied"),
    (3002, "data_not_ready"), (4001, "rate_limited"), (5002, "upstream_timeout"),
    (5003, "source_unavailable"),
])
def test_http_200_business_failure_is_not_success(code, category):
    with pytest.raises(ProbeError, match=category):
        fuyao_items(envelope([], code))


@pytest.mark.parametrize("body", [{}, {"code": False}, {"code": 0, "data": None}, envelope([None])])
def test_bad_schema_rejected(body):
    with pytest.raises(ProbeError, match="invalid_schema"):
        fuyao_items(body)


def test_empty_data_not_neutral():
    with pytest.raises(ProbeError, match="empty_data"):
        fuyao_items(envelope([]))


@pytest.mark.parametrize("status,category", [(401, "authentication"), (429, "rate_limited"), (302, "redirect_not_followed"), (503, "upstream_http_error")])
def test_http_errors_do_not_leak_response_body(status, category):
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status, text="fake-secret-do-not-print"))) as client:
            with pytest.raises(ProbeError, match=category) as error:
                await request_json(client, "GET", "https://example.test")
            assert "fake-secret" not in str(error.value)
    run(check())


def test_network_error_does_not_leak_url():
    def handler(request):
        raise httpx.ConnectError("https://example.test?key=fake-secret", request=request)
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ProbeError, match="network_error") as error:
                await request_json(client, "GET", "https://example.test")
            assert "fake-secret" not in str(error.value)
    run(check())


@pytest.mark.parametrize("price", [None, "3200", True, 0, -1, float("nan"), float("inf")])
def test_invalid_prices_are_not_coerced_to_normal(price):
    with pytest.raises(ProbeError, match="invalid_close_price"):
        validate_bars([{"date_ms": 100, "close_price": price}], 50, 150)


def test_duplicate_and_future_bar_time_rejected():
    with pytest.raises(ProbeError, match="duplicate_bar_time"):
        validate_bars([{"date_ms": 100, "close_price": 1}] * 2, 50, 150)
    with pytest.raises(ProbeError, match="invalid_bar_time"):
        validate_bars([{"date_ms": 200, "close_price": 1}], 50, 150)


def test_calendar_cross_checks_timestamp_date_and_order():
    now = datetime(2026, 10, 7, tzinfo=SHANGHAI)
    good = {"date": "20260930", "date_ms": int(datetime(2026, 9, 30, tzinfo=SHANGHAI).timestamp() * 1000)}
    assert calendar_dates([good], now)[0].day == 30
    with pytest.raises(ProbeError, match="invalid_calendar"):
        calendar_dates([good, good], now)
    with pytest.raises(ProbeError, match="invalid_calendar"):
        calendar_dates([{**good, "date_ms": 0}], now)


def test_configuration_check_never_calls_network(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Offline preflight must not call network")
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    settings = Settings({"DEEPSEEK_API_KEY": "fake-secret", "DEEPSEEK_MODEL": "test-model"})
    report = run(run_checks(settings, ["deepseek", "fuyao", "ifind"], False))
    assert report["providers"]["deepseek"]["status"] == "configured_unverified"
    assert report["providers"]["fuyao"]["status"] == "not_configured"
    assert "fake-secret" not in json.dumps(report)


def test_dotenv_load_is_literal_and_environment_wins(tmp_path, monkeypatch):
    (tmp_path / ".env.local").write_text('DEEPSEEK_API_KEY="literal-${HOME}"\nDEEPSEEK_MODEL=local-model\n')
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_MODEL", "env-model")
    settings = Settings.load(tmp_path)
    assert settings.get("DEEPSEEK_API_KEY") == "literal-${HOME}"
    assert settings.get("DEEPSEEK_MODEL") == "env-model"


@pytest.mark.parametrize("url", ["http://mcp.51ifind.com", "https://51ifind.com.evil.test/mcp", "https://user:secret@mcp.51ifind.com/mcp", "https://127.0.0.1/mcp"])
def test_ifind_credentials_only_go_to_reviewed_provider_domain(url):
    with pytest.raises(ProbeError, match="ifind_endpoint_requires_review"):
        validate_ifind_config(Settings({"IFIND_MCP_URL": url, "IFIND_MCP_TRANSPORT": "sse"}))


def test_ifind_header_is_not_guessed():
    settings = Settings({"IFIND_MCP_URL": "https://mcp.51ifind.com/example", "IFIND_MCP_TRANSPORT": "sse", "IFIND_MCP_AUTH_HEADER": "X-Test"})
    with pytest.raises(ProbeError, match="incomplete_auth_header"):
        validate_ifind_config(settings)


def model_message(name="connection_probe"):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": "test-call-1", "type": "function", "function": {
            "name": name, "arguments": json.dumps({"value": "connectivity-check"}),
        },
    }]}


def test_arbitrary_model_tool_is_never_executed():
    with pytest.raises(ProbeError, match="invalid_tool_call"):
        verify_probe_call(model_message("run_shell"))


@pytest.mark.parametrize("complete", [True, False])
def test_deepseek_tool_result_roundtrip_and_stream_completion(complete):
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path == "/models":
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})
        body = json.loads(request.content)
        if not body.get("stream"):
            return httpx.Response(200, json={"choices": [{"message": model_message()}]})
        result = body["messages"][-1]
        assert result["role"] == "tool" and result["tool_call_id"] == "test-call-1"
        assert json.loads(result["content"])["source"] == "local_synthetic_probe"
        events = ['data: {"choices":[{"delta":{"content":"CONNECTION_OK"}}]}\n\n']
        if complete:
            events += ['data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n', 'data: [DONE]\n\n']
        return httpx.Response(200, text="".join(events), headers={"Content-Type": "text/event-stream"})
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            settings = Settings({"DEEPSEEK_API_KEY": "test-key", "DEEPSEEK_MODEL": "test-model"})
            if complete:
                assert (await probe_deepseek(settings, client))["financial_analysis_tested"] is False
            else:
                with pytest.raises(ProbeError, match="incomplete_model_stream"):
                    await probe_deepseek(settings, client)
    run(check())
    assert len(requests) == 3


def test_ifind_official_sdk_initialize_and_paginated_discovery(monkeypatch):
    original_client = httpx.AsyncClient
    methods = []

    def handler(request):
        assert request.headers["X-Test-Auth"] == "test-secret"
        if request.method == "GET":
            return httpx.Response(405)
        message = json.loads(request.content)
        method = message["method"]
        methods.append(method)
        if "id" not in message:
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": message["params"]["protocolVersion"],
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "local-fixture", "version": "1"}}
        elif method == "tools/list":
            second_page = bool(message.get("params", {}).get("cursor"))
            result = {"tools": [{"name": "second_tool" if second_page else "first_tool", "inputSchema": {"type": "object"}}]}
            if not second_page:
                result["nextCursor"] = "page-two"
        else:
            raise AssertionError("Discovery must not invoke data tools")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    class MockClient(original_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    settings = Settings({"IFIND_MCP_URL": "https://mcp.51ifind.com/local-fixture",
                         "IFIND_MCP_TRANSPORT": "streamable_http",
                         "IFIND_MCP_AUTH_HEADER": "X-Test-Auth", "IFIND_MCP_AUTH_VALUE": "test-secret"})
    result = run(probe_ifind(settings, None))
    assert result["tool_count"] == 2
    assert result["financial_data_call_tested"] is False
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/list"]
    assert "test-secret" not in json.dumps(result)


@pytest.mark.parametrize("missing_bar", [False, True])
def test_fuyao_index_daily_chain_and_missing_trading_day(missing_bar):
    # Synthetic fixture: these dates are a mock calendar, not claims about real trading days.
    now = datetime.now(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
    dates = [now - timedelta(days=i) for i in range(20, 0, -1)]
    calendar = [{"date": day.strftime("%Y%m%d"), "date_ms": int(day.timestamp() * 1000)} for day in dates]
    bars = [{"date_ms": row["date_ms"], "close_price": 3000.0} for row in calendar]
    requests = []
    def handler(request):
        requests.append(request)
        assert request.headers["X-api-key"] == "test-key"
        if request.url.path.endswith("trading-days"):
            return httpx.Response(200, json=envelope(calendar))
        if request.url.path.endswith("search"):
            assert request.url.params["asset_type"] == "a-share-index"
            return httpx.Response(200, json=envelope([{"thscode": "000300.SH", "asset_type": "a-share-index"}]))
        assert request.url.params["thscode"] == "000300.SH"
        assert "adjust" not in request.url.params
        return httpx.Response(200, json=envelope(bars[:-1] if missing_bar else bars))
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            if missing_bar:
                with pytest.raises(ProbeError, match="incomplete_or_unexpected_trading_days"):
                    await probe_fuyao(Settings({"FUYAO_API_KEY": "test-key"}), client)
            else:
                result = await probe_fuyao(Settings({"FUYAO_API_KEY": "test-key"}), client)
                assert result["row_count"] == 20
    run(check())
    assert len(requests) == 3
