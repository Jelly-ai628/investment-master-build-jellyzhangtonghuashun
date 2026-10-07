"""Read-only probe of Fuyao single-stock capabilities; responses go to work/, the API key never does."""

from datetime import datetime, timedelta
import json
from pathlib import Path
import sys

import httpx

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "apps" / "api"))
from market_research.connections import FUYAO, SHANGHAI, Settings  # noqa: E402

settings = Settings.load(root)
headers = {"X-api-key": settings.get("FUYAO_API_KEY")}
end = datetime.now(SHANGHAI)
window = {"start": int((end - timedelta(days=45)).timestamp() * 1000), "end": int(end.timestamp() * 1000)}
probes = [
    ("search_name", "/api/meta/tickers/search", {"q": "贵州茅台", "limit": 10}),
    ("search_name_a_share", "/api/meta/tickers/search", {"q": "贵州茅台", "asset_type": "a-share", "limit": 10}),
    ("search_code", "/api/meta/tickers/search", {"q": "600519", "limit": 10}),
    ("stock_history", "/api/a-share/prices/historical", {"thscode": "600519.SH", "interval": "1d", **window}),
    ("stock_history_forward", "/api/a-share/prices/historical", {"thscode": "600519.SH", "interval": "1d", "adjusted": "forward", **window}),
    ("stock_valuation", "/api/a-share/valuation/snapshot", {"thscode": "600519.SH"}),
    ("openapi", "/openapi.json", {}),
    ("api_docs", "/v3/api-docs", {}),
]
results = {}
with httpx.Client(timeout=30, follow_redirects=False) as client:
    for label, path, params in probes:
        try:
            response = client.get(FUYAO + path, headers=headers, params=params)
            try:
                body = response.json()
            except ValueError:
                body = response.text[:4000]
            results[label] = {"path": path, "params": params, "http_status": response.status_code, "body": body}
        except httpx.HTTPError as error:
            results[label] = {"path": path, "params": params, "error": type(error).__name__}
    for label, path in [("docs_index", "/docs/")]:
        try:
            response = client.get(FUYAO + path, follow_redirects=True)
            results[label] = {"path": path, "http_status": response.status_code, "body": response.text[:200000]}
        except httpx.HTTPError as error:
            results[label] = {"path": path, "error": type(error).__name__}
output = root / "work" / "fuyao-stock-probe.json"
output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
output.chmod(0o600)
print(output, {label: item.get("http_status", item.get("error")) for label, item in results.items()})
