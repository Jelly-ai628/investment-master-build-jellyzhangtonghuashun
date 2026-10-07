"""Read-only probe of why a stock name is not found: search variants, the ticker list endpoint and its docs. The API key is never written."""

import json
from pathlib import Path
import re
import sys

import httpx

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "apps" / "api"))
from market_research.connections import FUYAO, Settings  # noqa: E402

name, code = (sys.argv[1:3] + ["中国移动", "600941"][len(sys.argv[1:3]):])[:2]
headers = {"X-api-key": Settings.load(root).get("FUYAO_API_KEY")}
probes = [
    ("search_name_typed", "/api/meta/tickers/search", {"q": name, "asset_type": "a-share", "limit": 20}),
    ("search_name_untyped", "/api/meta/tickers/search", {"q": name, "limit": 50}),
    ("search_ticker_typed", "/api/meta/tickers/search", {"q": code, "asset_type": "a-share", "limit": 20}),
    ("search_ticker_untyped", "/api/meta/tickers/search", {"q": code, "limit": 50}),
    ("search_thscode", "/api/meta/tickers/search", {"q": code + (".SH" if code.startswith("6") else ".SZ"), "limit": 20}),
    ("search_prefix", "/api/meta/tickers/search", {"q": name[:2], "asset_type": "a-share", "limit": 50}),
    ("list_a_share", "/api/meta/tickers/list", {"asset_type": "a-share", "limit": 5}),
]
results = {"name": name, "code": code}
with httpx.Client(timeout=30, follow_redirects=False) as client:
    for label, path, params in probes:
        try:
            response = client.get(FUYAO + path, headers=headers, params=params)
            try:
                body = response.json()
            except ValueError:
                body = response.text[:2000]
            results[label] = {"path": path, "params": params, "http_status": response.status_code, "body": body}
        except httpx.HTTPError as error:
            results[label] = {"path": path, "params": params, "error": type(error).__name__}
    docs = {}
    for page in ("ticker-list", "tickers-list", "ticker-search"):
        try:
            response = client.get(f"https://fuyao.aicubes.cn/docs/api-reference/{page}/", follow_redirects=True)
            body = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", response.text, flags=re.S)
            docs[page] = {"http_status": response.status_code, "text": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))[:20000]}
        except httpx.HTTPError as error:
            docs[page] = {"error": type(error).__name__}
    try:
        text = client.get("https://fuyao.aicubes.cn/llms-full.txt", follow_redirects=True).text
        docs["llms_full_excerpts"] = [text[max(0, m.start() - 300):m.start() + 4000] for m in re.finditer(r"tickers/list", text)][:3]
    except httpx.HTTPError as error:
        docs["llms_full_excerpts"] = {"error": type(error).__name__}
    results["docs"] = docs
output = root / "work" / "fuyao-ticker-search-probe.json"
output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
output.chmod(0o600)
print(output)
for label, _, _ in probes:
    item = results[label]
    body = item.get("body")
    rows = (body.get("data") or {}).get("item") if isinstance(body, dict) and isinstance(body.get("data"), dict) else None
    print(label, item.get("http_status", item.get("error")), body.get("code") if isinstance(body, dict) else "", [(r.get("thscode"), r.get("name"), r.get("asset_type")) for r in rows[:5]] if isinstance(rows, list) else rows)
