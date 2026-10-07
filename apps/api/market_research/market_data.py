"""Bounded provider collection with private raw evidence and explicit cache age."""

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlparse

import httpx
import pyarrow.parquet as pq

from .connections import Settings, ProbeError, FUYAO, SHANGHAI, request_json, fuyao_items, calendar_dates
from .market_metrics import market_breadth, index_metrics, number
from .ifind_runtime import call_ifind, single_valuation, news_records
from .sector_ranking import rank_sectors

INDICES = {"000300.SH": "沪深300", "000905.SH": "中证500", "000852.SH": "中证1000", "000001.SH": "上证指数", "399006.SZ": "创业板指"}


# Exchange short names carry status prefixes (XD ex-dividend, XR ex-rights, DR both, N/C new listing) and suffixes (-U, -W) that are not part of the name.
MARKERS = re.compile(r"^(?:XD|XR|DR|N|C)(?=[\u4e00-\u9fa5*])|-(?:U|W|UW)$")


def short_name(name: str) -> str:
    return MARKERS.sub("", re.sub(r"\s", "", name)).upper()


def names_agree(wanted: str, listed: str) -> bool:
    listed = short_name(listed)
    return wanted in listed or (len(listed) >= 3 and listed in wanted)


def private_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def evidence(kind: str, data: dict, provenance: dict) -> dict:
    identifier = hashlib.sha256(json.dumps({"kind": kind, "data": data, "provenance": provenance}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    return {"id": "ev_" + identifier, "kind": kind, "data": data, "provenance": provenance}


class MarketData:
    def __init__(self, settings: Settings, root: Path, client: httpx.AsyncClient):
        self.settings, self.root, self.client = settings, root, client
        self.work = root / "work"
        self.headers = {"X-api-key": settings.get("FUYAO_API_KEY")}
        self.calendar = []
        self.expected_day = None
        self.evidence = {}
        self.index_names = dict(INDICES)
        self.industry_names = {}
        self.securities = {}
        self.matches, self.ambiguous = {}, {}
        self.index_results = {}
        self.on_progress = None

    async def initialize(self, end_date: str | None = None):
        if not self.settings.ready("fuyao"):
            raise ProbeError("fuyao_not_configured")
        body = await request_json(self.client, "GET", FUYAO + "/api/a-share/calendar/trading-days", headers=self.headers)
        now = datetime.now(SHANGHAI)
        days = calendar_dates(fuyao_items(body), now)
        completed = [day for day in days if day.date() < now.date() or now.hour >= 16]
        if not completed or (now.date() - completed[-1].date()).days > 14:
            raise ProbeError("calendar_not_current")
        self.latest_day = completed[-1].date().isoformat()
        if end_date:
            try:
                requested = datetime.strptime(end_date, "%Y-%m-%d").date()
            except ValueError:
                raise ProbeError("invalid_end_date") from None
            if requested > now.date():
                raise ProbeError("future_research_date")
            completed = [day for day in completed if day.date() <= requested]
        if not completed:
            raise ProbeError("date_outside_calendar")
        self.calendar = completed
        self.expected_day = completed[-1].date().isoformat()
        self.calendar_request_id = body.get("request_id")

    def remember(self, item: dict) -> dict:
        self.evidence[item["id"]] = item
        return item

    async def search_stocks(self, query: str, typed: bool = True) -> list[dict]:
        params = {"q": query, "limit": 20, **({"asset_type": "a-share"} if typed else {})}
        try:
            rows = fuyao_items(await request_json(self.client, "GET", FUYAO + "/api/meta/tickers/search", headers=self.headers, params=params))
        except ProbeError as error:
            # An empty search is an answer ("not found"), not a provider failure.
            if error.category == "empty_data":
                return []
            raise
        return [row for row in rows if row.get("asset_type") == "a-share" and isinstance(row.get("thscode"), str) and isinstance(row.get("name"), str)]

    async def listed_code(self, ticker: str) -> str | None:
        # Confirms only that a six-digit code is a listed A-share in the provider's snapshot universe, not which company it is.
        suffixes = {"6": ["SH"], "0": ["SZ"], "3": ["SZ"], "4": ["BJ"], "8": ["BJ"], "9": ["BJ", "SH"]}.get(ticker[0], [])
        codes = [ticker + "." + suffix for suffix in suffixes]
        if not codes:
            return None
        try:
            rows = fuyao_items(await request_json(self.client, "GET", FUYAO + "/api/a-share/prices/snapshot", headers=self.headers, params={"thscodes": ",".join(codes)}))
        except ProbeError as error:
            if error.category in ("empty_data", "instrument_not_found"):
                return None
            raise
        found = [row["thscode"] for row in rows if row.get("thscode") in codes]
        return found[0] if len(found) == 1 else None

    async def resolve_security(self, entity: str, hint: str | None = None) -> str:
        # Every accepted code comes from the provider's code table; how it was matched is recorded and shown to the user.
        wanted = re.sub(r"\s", "", entity).upper()
        rows = await self.search_stocks(entity) or await self.search_stocks(entity, typed=False)
        via = "名称或代码检索"
        if not rows and len(wanted) >= 4 and not wanted.isdigit():
            # The table holds exchange short names, which can be cut to four characters with a prefix ("XD中国移"), so a full name finds nothing.
            rows = await self.search_stocks(wanted[:3])
            via = "名称前几个字检索并核对简称"
        unverified = None
        if not rows and hint and re.fullmatch(r"\d{6}", hint):
            # The model may know the ticker of a well-known name; the provider's own name for that ticker must agree.
            listed = [row for row in await self.search_stocks(hint) + await self.search_stocks(hint, typed=False)
                      if str(row.get("ticker")) == hint or row["thscode"].startswith(hint + ".")]
            rows = [row for row in listed if names_agree(wanted, row["name"])]
            via = "按代码" + hint + "检索并核对名称"
            if not listed:
                # Live 2026-10-07: neither "中国移动" nor its ticker came back from the search, although the market snapshot lists the code.
                unverified = await self.listed_code(hint)
        if unverified:
            self.securities[unverified] = entity
            self.index_names[unverified] = entity
            self.matches[unverified] = {"query": entity, "name": entity, "code": unverified, "exact": False, "name_verified": False,
                                        "via": f"DeepSeek给出的代码{hint}（扶摇行情快照中有该代码，但代码表检索没有返回名称，未能核对是否就是“{entity}”）"}
            return unverified
        exact = {row["thscode"]: row["name"] for row in rows if wanted in (short_name(row["name"]), row["name"].upper(), str(row.get("ticker")), row["thscode"].upper())}
        partial = {row["thscode"]: row["name"] for row in rows if names_agree(wanted, row["name"])}
        matches = exact or partial
        if not matches:
            raise ProbeError("security_not_resolved")
        if len(matches) > 1:
            self.ambiguous[entity] = list(matches.values())[:4]
            raise ProbeError("ambiguous_security")
        code, listed_name = next(iter(matches.items()))
        # A truncated or marked short name ("XD中国移") is shown under the name the user gave, with the table's own name kept in the match note.
        marked = short_name(listed_name) != listed_name.upper()
        truncated = len(short_name(listed_name)) < len(wanted) and short_name(listed_name) in wanted
        name = entity.strip() if marked or truncated else listed_name
        self.securities[code] = name
        self.index_names[code] = name
        self.matches[code] = {"query": entity, "name": name, "code": code, "listed_name": listed_name, "exact": bool(exact) and name == listed_name,
                              "via": via + ("" if name == listed_name else f"（代码表简称为“{listed_name}”）")}
        return code

    async def get_index_history(self, code: str, window: int = 20) -> dict:
        if code not in self.index_names or window not in (5, 10, 20, 60):
            raise ProbeError("unsupported_index_request")
        cache_key = (code, window)
        if cache_key in self.index_results:
            return self.index_results[cache_key]
        days = self.calendar[-max(61, window + 1):]
        stock = code in self.securities
        if not stock:
            resolved = fuyao_items(await request_json(self.client, "GET", FUYAO + "/api/meta/tickers/search", headers=self.headers,
                                                    params={"q": code, "asset_type": "a-share-index", "limit": 10}))
            if not any(row.get("thscode") == code and row.get("asset_type") == "a-share-index" for row in resolved):
                raise ProbeError("index_not_resolved")
        params = {"thscode": code, "interval": "1d", "start": int(days[0].timestamp() * 1000),
                  "end": int((days[-1] + timedelta(days=1)).timestamp() * 1000) - 1}
        endpoint = "/api/a-share/prices/historical" if stock else "/api/a-share-index/prices/historical"
        if stock:
            params["adjust"] = "forward"
        body = await request_json(self.client, "GET", FUYAO + endpoint, headers=self.headers, params=params)
        rows = fuyao_items(body)
        if stock and (body.get("data") or {}).get("adjust") != "forward":
            raise ProbeError("stock_adjustment_unverified")
        calculated = index_metrics(rows, expected_days=[day.date().isoformat() for day in days], window=window, allow_gaps=stock)
        calculated.update({"code": code, "name": self.index_names[code], "instrument_type": "stock" if stock else "industry" if code not in INDICES else "index"})
        if stock:
            calculated.update({"unit": "元（前复权）", "return_basis": "前复权收盘价首末之比，按供应商口径处理除权除息；不是未来收益预测。"})
        fetched = datetime.now(SHANGHAI).isoformat()
        raw_name = f"{'stock' if stock else 'index'}-{code}-{self.expected_day}-{time.time_ns()}.json"
        private_json(self.work / "raw" / raw_name, body)
        item = self.remember(evidence("index_history", calculated, {
            "source": "扶摇金融数据API", "endpoint": endpoint, "request_id": body.get("request_id"),
            "parameters": params, "retrieved_at": fetched, "observation_date": self.expected_day,
            "raw_file": "work/raw/" + raw_name, "raw_fields": ["data.item[].date_ms", "data.item[].close_price"] + (["data.adjust"] if stock else []),
            "calculation_version": "index-metrics-v1", "unit": "元（前复权）/ %" if stock else "点 / %", "cache_used": False,
        }))
        self.index_results[cache_key] = item
        return item

    async def load_industries(self):
        body = await request_json(self.client, "GET", FUYAO + "/api/a-share-index/catalog/ths-index-list", headers=self.headers, params={"tag": "industry"})
        rows = fuyao_items(body)
        for row in rows:
            if not isinstance(row.get("name"), str) or not isinstance(row.get("thscode"), str):
                raise ProbeError("invalid_industry_catalog")
            if row["name"] in self.industry_names and self.industry_names[row["name"]] != row["thscode"]:
                raise ProbeError("ambiguous_industry_name")
            self.industry_names[row["name"]] = row["thscode"]
            self.index_names[row["thscode"]] = row["name"]
        return self.industry_names

    async def get_industry_history(self, name: str, window: int):
        if not self.industry_names:
            await self.load_industries()
        if name not in self.industry_names:
            raise ProbeError("industry_not_resolved")
        return await self.get_index_history(self.industry_names[name], window)

    async def compare_history(self, code: str, window: int, comparison_end: str):
        current = await self.get_index_history(code, window)
        older = MarketData(self.settings, self.root, self.client)
        await older.initialize(comparison_end)
        older.index_names.update(self.index_names)
        older.securities.update(self.securities)
        if older.expected_day == self.expected_day:
            raise ProbeError("comparison_periods_identical")
        previous = await older.get_index_history(code, window)
        left, right = current["data"], previous["data"]
        return self.remember(evidence("historical_comparison", {
            "name": self.index_names[code], "code": code, "current": left, "previous": right,
            "return_difference_pp": left["window_return_pct"] - right["window_return_pct"],
            "limitations": ["只比较已发生的指数行情，不以历史相似推断未来表现。", "没有使用当前成分股回填历史宽度；历史事件和估值环境仍需另行核对。"],
        }, {"source": "扶摇金融数据API", "endpoint": "/api/a-share-index/prices/historical",
            "retrieved_at": datetime.now(SHANGHAI).isoformat(), "observation_date": self.expected_day,
            "raw_files": [current["provenance"]["raw_file"], previous["provenance"]["raw_file"]],
            "raw_fields": ["data.item[].date_ms", "data.item[].close_price"], "calculation_version": "historical-comparison-v1", "unit": "% / 百分点",
            "period_sources": [current["provenance"], previous["provenance"]]}))

    async def get_sector_ranking(self, window):
        if not self.industry_names:
            await self.load_industries()
        universe = [(name, code) for name, code in self.industry_names.items() if code.startswith("881")]
        if not universe or len(universe) > 150:
            raise ProbeError("sector_universe_requires_review")
        universe = sorted(universe, key=lambda item: item[1])
        cache = self.work / f"sector-ranking-{self.expected_day}-{window}.json"
        if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
            item = json.loads(cache.read_text())
            if item["data"]["universe_codes"] == [code for _, code in universe]:
                item["provenance"]["cache_used"] = True
                return self.remember(item)
        days = self.calendar[-max(window + 1, 6):]
        expected = [day.date().isoformat() for day in days]
        if len(days) < window + 1:
            raise ProbeError("insufficient_sector_history")
        benchmark = await self.get_index_history("000300.SH", window)
        start = int(days[0].timestamp() * 1000)
        end = int((days[-1] + timedelta(days=1)).timestamp() * 1000) - 1
        semaphore, rate_lock = asyncio.Semaphore(3), asyncio.Lock()
        next_start, finished = 0.0, 0
        successes, failures = [], []
        sources, source_labels = [], []
        async def fetch_one(name, code):
            nonlocal next_start, finished
            async with semaphore:
                async with rate_lock:
                    wait = max(0, next_start - time.monotonic())
                    if wait:
                        await asyncio.sleep(wait)
                    next_start = time.monotonic() + .4
                try:
                    body = await request_json(self.client, "GET", FUYAO + "/api/a-share-index/prices/historical", headers=self.headers,
                        params={"thscode": code, "interval": "1d", "start": start, "end": end})
                    rows = fuyao_items(body)
                    metrics = index_metrics(rows, expected_days=expected, window=window)
                    ordered = sorted(rows, key=lambda row: row["date_ms"])
                    five_day = (ordered[-1]["close_price"] / ordered[-6]["close_price"] - 1) * 100
                    raw_name = f"sector-{code}-{self.expected_day}-{time.time_ns()}.json"
                    private_json(self.work / "raw" / raw_name, body)
                    sources.append("work/raw/" + raw_name)
                    source_labels.append(name + "原始日线")
                    successes.append({"code": code, "name": name, "start_date": metrics["start_date"], "end_date": metrics["end_date"],
                        "window_return_pct": metrics["window_return_pct"], "five_day_return_pct": five_day,
                        "max_drawdown_pct": metrics["max_drawdown_pct"], "request_id": body.get("request_id")})
                except ProbeError as error:
                    failures.append({"code": code, "name": name, "reason": error.category})
                finished += 1
                if self.on_progress and (finished % 10 == 0 or finished == len(universe)):
                    await self.on_progress(f"已完成行业取数 {finished}/{len(universe)}，有效 {len(successes)}")
        await asyncio.gather(*(fetch_one(name, code) for name, code in universe))
        ranking = rank_sectors(successes, benchmark["data"]["window_return_pct"], len(universe))
        manifest = f"sector-ranking-rows-{time.time_ns()}.json"
        private_json(self.work / "raw" / manifest, {"data": {"item": ranking}, "universe": universe,
            "formula": "window_return_pct=(end_close/start_close-1)*100; relative_return_pp=sector_return-benchmark_return"})
        item = self.remember(evidence("sector_ranking", {
            "window": window, "date": self.expected_day, "start_date": ranking[0]["start_date"], "end_date": self.expected_day,
            "scope": "同花顺行业目录中的881系列指数；不混入884细分系列或概念主题。",
            "expected_count": len(universe), "valid_count": len(ranking), "failures": failures,
            "coverage_pct": len(ranking) / len(universe) * 100, "ranking": ranking,
            "benchmark": {"name": "沪深300", "return_pct": benchmark["data"]["window_return_pct"]},
            "universe_codes": [code for _, code in universe],
            "limitations": ["排名衡量已经发生的区间相对表现，不是预测或操作建议。", "近5日表现是辅助观察，排名依据界面所选交易日窗口。", "如有缺失序列，排名仅限有效样本，不宣称全市场最强。"],
        }, {"source": "扶摇金融数据API", "endpoint": "/api/a-share-index/catalog/ths-index-list + prices/historical",
            "retrieved_at": datetime.now(SHANGHAI).isoformat(), "observation_date": self.expected_day,
            "raw_files": ["work/raw/" + manifest] + sources + [benchmark["provenance"]["raw_file"]],
            "source_labels": ["排名计算表"] + source_labels + ["沪深300基准原始日线"],
            "raw_fields": ["catalog.item[].thscode", "catalog.item[].name", "data.item[].date_ms", "data.item[].close_price"],
            "calculation_version": "sector-relative-strength-v1", "unit": "% / 百分点", "cache_used": False}))
        private_json(cache, item)
        return item

    async def get_risk_context(self):
        if self.expected_day != self.latest_day:
            raise ProbeError("historical_sentiment_validation_unavailable")
        daily = self.work / "fuyao-market-10d.parquet"
        if not daily.exists():
            await self.get_market_breadth()
        bars = await asyncio.to_thread(lambda: pq.read_table(daily).to_pylist())
        matched = {row["thscode"]: row for row in bars if datetime.fromtimestamp(row["date_ms"] / 1000, SHANGHAI).date().isoformat() == self.expected_day}
        pools, requests, raw_files = {}, [], []
        date_ms = int(datetime.strptime(self.expected_day, "%Y-%m-%d").replace(tzinfo=SHANGHAI).timestamp() * 1000)
        for pool in ("limit-up", "limit-down"):
            records, page, total = [], 1, None
            while total is None or len(records) < total:
                if page > 10:
                    raise ProbeError("sentiment_page_limit")
                body = await request_json(self.client, "GET", FUYAO + f"/api/a-share/special-data/{pool}-pool", headers=self.headers,
                                          params={"date_ms": date_ms, "page": page, "size": 200})
                data = body.get("data") or {}
                pagination = data.get("pagination") or {}
                if body.get("code") != 0 or type(pagination.get("total")) is not int or not isinstance(data.get("item"), list):
                    raise ProbeError("invalid_sentiment_response")
                if total is not None and total != pagination["total"]:
                    raise ProbeError("changing_sentiment_count")
                total = pagination["total"]
                if total < 0 or (not data["item"] and len(records) < total):
                    raise ProbeError("incomplete_sentiment_pool")
                records.extend(data["item"])
                requests.append(body.get("request_id"))
                name = f"{pool}-{self.expected_day}-{time.time_ns()}.json"
                private_json(self.work / "raw" / name, body)
                raw_files.append("work/raw/" + name)
                page += 1
            if len(records) != total or len({r.get("thscode") for r in records}) != total:
                raise ProbeError("duplicate_sentiment_records")
            for row in records:
                bar = matched.get(row.get("thscode"))
                if bar is None or not number(row.get("last_price")) or not number(bar.get("close_price")) or abs(row["last_price"] - bar["close_price"]) > .011:
                    raise ProbeError("sentiment_date_link_unverified")
            pools[pool] = len(records)
        return self.remember(evidence("sentiment", {
            "date": self.expected_day, "limit_up_count": pools["limit-up"], "limit_down_count": pools["limit-down"],
            "limitations": ["涨跌停分类采用供应商规则，不等于全部上涨/下跌家数。", "请求日期已明确，并用同日收盘价逐条核对；单日极端交易分布不证明后续涨跌。", "利率与杠杆数据尚未核实，本项不能替代完整流动性分析。"],
        }, {"source": "扶摇金融数据API", "endpoint": "/api/a-share/special-data/limit-up-pool + limit-down-pool",
            "retrieved_at": datetime.now(SHANGHAI).isoformat(), "observation_date": self.expected_day, "request_ids": requests,
            "raw_files": raw_files, "raw_fields": ["data.pagination.total", "data.item[].thscode", "data.item[].last_price"],
            "calculation_version": "sentiment-pool-v1", "unit": "家"}))

    async def get_valuation_context(self, code="000300.SH"):
        if code not in INDICES:
            raise ProbeError("valuation_index_not_supported")
        if not self.settings.ready("ifind"):
            raise ProbeError("ifind_not_configured")
        items, failures, raw_files = [], [], []
        for metric, abbreviation in [("市净率（PB）", "PB"), ("市盈率（PE，TTM）", "PE(TTM)")]:
            query = f"只查询{INDICES[code]}指数{code}的{abbreviation}一个指标。交易日期参数必须固定为{self.expected_day.replace('-', '')}。请返回数值和实际指标参数，单位倍。不要查询其他指标，不要用最新交易日期替代指定日期。"
            try:
                raw = await call_ifind(self.settings, "index_data", {"query": query})
                record = single_valuation(raw, code, self.expected_day, metric)
                name = f"valuation-{code}-{abbreviation[:2]}-{time.time_ns()}.json"
                original_data = json.loads(raw["content"][0]["text"])["data"]
                private_json(self.work / "raw" / name, {"data": {"item": [original_data]}, "mcp_response": raw, "query": query})
                raw_files.append("work/raw/" + name)
                items.append(record)
            except ProbeError as error:
                failures.append({"metric": metric, "reason": error.category})
        if not items:
            raise ProbeError("no_verified_valuation_metric")
        return self.remember(evidence("valuation", {
            "name": INDICES[code], "code": code, "date": self.expected_day, "items": items, "missing_metrics": failures,
            "limitations": ["单位按PB/PE指标定义明确为倍，并保留原响应参数；不隐瞒原文未单列单位。", "仅当前指标水平，不提供历史分位或高低估结论。"],
        }, {"source": "iFinD MCP", "endpoint": "index_data", "retrieved_at": datetime.now(SHANGHAI).isoformat(), "observation_date": self.expected_day,
            "raw_files": raw_files, "raw_fields": ["data.answer", "data.indicators_params"], "calculation_version": "single-valuation-v1", "unit": "倍"}))

    async def get_stock_valuation(self, code: str):
        if code not in self.securities:
            raise ProbeError("valuation_security_not_resolved")
        # The provider only serves the latest snapshot; it cannot stand in for an earlier research date.
        if self.expected_day != self.latest_day:
            raise ProbeError("historical_stock_valuation_unavailable")
        params = {"thscodes": code}
        body = await request_json(self.client, "GET", FUYAO + "/api/a-share/valuations/snapshot", headers=self.headers, params=params)
        rows = fuyao_items(body)
        stamp = (body.get("data") or {}).get("timestamp")
        if len(rows) != 1 or rows[0].get("thscode") != code or type(stamp) is not int:
            raise ProbeError("valuation_entity_or_time_mismatch")
        snapshot_day = datetime.fromtimestamp(stamp / 1000, SHANGHAI).date().isoformat()
        if snapshot_day < self.expected_day:
            raise ProbeError("valuation_snapshot_stale")
        items, missing = [], []
        for field, metric in (("pe_ttm", "市盈率（PE，TTM）"), ("pe_mrq", "市盈率（PE，MRQ）"), ("pb_mrq", "市净率（PB，MRQ）")):
            value = rows[0].get(field)
            if number(value):
                items.append({"metric": metric, "value": value, "raw_value": str(value), "unit": "倍", "field": field,
                              "date_source": "data.timestamp（上游指标元数据最大有效时间）", "unit_source": "扶摇估值接口定义为倍数"})
            else:
                missing.append({"metric": metric, "reason": "provider_null"})
        if not items:
            raise ProbeError("no_verified_valuation_metric")
        raw_name = f"stock-valuation-{code}-{time.time_ns()}.json"
        private_json(self.work / "raw" / raw_name, body)
        return self.remember(evidence("valuation", {
            "name": self.securities[code], "code": code, "date": snapshot_day, "snapshot": True, "items": items, "missing_metrics": missing,
            "limitations": ["扶摇只提供最新估值快照，不提供历史估值或分位，不能判断高低估。", "TTM与MRQ使用的财务周期不同；负值或空值原样呈现，不补零。"],
        }, {"source": "扶摇金融数据API", "endpoint": "/api/a-share/valuations/snapshot", "request_id": body.get("request_id"), "parameters": params,
            "retrieved_at": datetime.now(SHANGHAI).isoformat(), "observation_date": snapshot_day, "raw_file": "work/raw/" + raw_name,
            "raw_fields": ["data.timestamp", "data.item[].pe_ttm", "data.item[].pe_mrq", "data.item[].pb_mrq"], "calculation_version": "stock-valuation-snapshot-v1", "unit": "倍"}))

    async def get_event_context(self):
        import html
        import re
        start = self.calendar[-min(7, len(self.calendar))].date().isoformat()
        args = {"query": "中国人民银行 公开市场操作 逆回购", "time_start": start, "time_end": self.expected_day, "size": 2}
        raw = await call_ifind(self.settings, "search_news", args, news=True)
        records = news_records(raw, start, self.expected_day)
        verified = []
        for record in records:
            if not any(term in record["title"] + record["excerpt"] for term in ("人民银行", "央行", "逆回购")):
                continue
            try:
                async with self.client.stream("GET", record["url"]) as response:
                    if response.status_code != 200:
                        continue
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 700_000:
                            raise ProbeError("news_page_size_limit")
                        chunks.append(chunk)
                    page = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                title = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
                actual = re.sub(r"\s", "", html.unescape(title[1])) if title else ""
                wanted = re.sub(r"\s", "", record["title"])
                y, m, day = record["published_at"].split("-")
                date_forms = [record["published_at"], f"{y}年{int(m)}月{int(day)}日", f"{y}/{m}/{day}"]
                if wanted not in actual or not any(date in page for date in date_forms):
                    continue
                verified.append({**record, "original_title_checked": True, "date_check": "MCP日期与原网页日期文字核对", "source_scope": "媒体报道，不替代央行原始公告"})
            except (httpx.HTTPError, ProbeError):
                continue
        if not verified:
            raise ProbeError("news_original_not_verified")
        name = f"policy-news-{time.time_ns()}.json"
        private_json(self.work / "raw" / name, {"data": {"item": verified}, "mcp_response": raw, "query": args})
        return self.remember(evidence("events", {
            "date": self.expected_day, "start_date": start, "records": verified,
            "limitations": ["报道的发布时间与拟实施时间不同，未来操作安排不等于已经实施。", "仅流动性相关新闻检索，不能代表全部重要事件，也不能据此证明价格变化的因果。"],
        }, {"source": "iFinD MCP / 报道源网页", "endpoint": "search_news + 原文标题核对", "retrieved_at": datetime.now(SHANGHAI).isoformat(),
            "observation_date": self.expected_day, "raw_file": "work/raw/" + name,
            "raw_fields": ["资讯标题", "资讯内容", "日期", "URL"], "calculation_version": "attributed-events-v1", "unit": "报道 / 日期"}))

    async def get_market_breadth(self) -> dict:
        if self.expected_day != self.latest_day:
            raise ProbeError("historical_snapshot_breadth_unavailable")
        snapshots = self.work / "fuyao-full-snapshot.json"
        daily = self.work / "fuyao-market-10d.parquet"
        self.work.mkdir(parents=True, exist_ok=True)
        cache_used = snapshots.exists() and daily.exists() and max(time.time() - snapshots.stat().st_mtime, time.time() - daily.stat().st_mtime) < 900
        if not cache_used:
            signed = await request_json(self.client, "GET", FUYAO + "/api/dump/market-dumps/daily-k-10d/download-url", headers=self.headers)
            if signed.get("code") != 0:
                raise ProbeError("market_dump_unavailable")
            url = (signed.get("data") or {}).get("presigned_url")
            if not isinstance(url, str) or urlparse(url).scheme != "https" or urlparse(url).hostname != "o.thsi.cn":
                raise ProbeError("market_dump_host_requires_review")
            temp = daily.with_suffix(".part")
            try:
                async with self.client.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise ProbeError("market_dump_download_failed")
                    size = 0
                    with temp.open("wb") as output:
                        temp.chmod(0o600)
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 50_000_000:
                                raise ProbeError("market_dump_size_limit")
                            output.write(chunk)
                temp.replace(daily)
            finally:
                temp.unlink(missing_ok=True)
            pages, total, offset = [], None, 0
            while total is None or offset < total:
                if len(pages) >= 20:
                    raise ProbeError("snapshot_page_limit")
                body = await request_json(self.client, "GET", FUYAO + "/api/a-share/prices/snapshot", headers=self.headers, params={"limit": 500, "offset": offset})
                rows = fuyao_items(body)
                current_total = body["data"].get("total")
                if type(current_total) is not int or current_total <= 0 or (total is not None and current_total != total):
                    raise ProbeError("changing_snapshot_universe")
                total = current_total
                offset += len(rows)
                pages.append(body)
                await asyncio.sleep(.25)
            private_json(snapshots, {"pages": pages})
        pages = json.loads(snapshots.read_text())["pages"]
        # Pure computation can run outside the event loop when the API is serving clients.
        def calculate():
            return market_breadth(pages, pq.read_table(daily).to_pylist(), self.expected_day)
        calculated = await asyncio.to_thread(calculate)
        stamp = datetime.fromtimestamp(snapshots.stat().st_mtime, SHANGHAI).isoformat()
        # Preserve per-run source copies: later refreshes must not replace the evidence behind a claim.
        import shutil
        snapshot_hash = hashlib.sha256(snapshots.read_bytes()).hexdigest()[:16]
        daily_hash = hashlib.sha256(daily.read_bytes()).hexdigest()[:16]
        raw_dir = self.work / "raw"
        raw_dir.mkdir(exist_ok=True)
        for source, name in [(snapshots, f"market-{snapshot_hash}.json"), (daily, f"market-{daily_hash}.parquet")]:
            target = raw_dir / name
            if not target.exists():
                shutil.copyfile(source, target)
                target.chmod(0o600)
        return self.remember(evidence("market_breadth", calculated, {
            "source": "扶摇金融数据API", "endpoint": "/api/a-share/prices/snapshot + 最近10交易日日K导出",
            "request_ids": [page.get("request_id") for page in pages], "retrieved_at": stamp,
            "observation_date": self.expected_day, "cache_used": cache_used,
            "raw_files": [f"work/raw/market-{snapshot_hash}.json", f"work/raw/market-{daily_hash}.parquet"],
            "raw_fields": ["data.item[].thscode", "data.item[].price_change_ratio_pct", "data.item[].prev_price", "data.item[].last_price", "Parquet.date_ms", "Parquet.OHLC", "Parquet.volume", "Parquet.turnover"],
            "calculation_version": "breadth-v1", "unit": "家 / % / CNY",
        }))
