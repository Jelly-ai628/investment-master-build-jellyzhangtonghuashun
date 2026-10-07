"""Save selected public Fuyao API reference pages as plain text under work/ for offline reading."""

import html
import json
from pathlib import Path
import re

import httpx

root = Path(__file__).resolve().parents[1]
pages = ["valuations", "prices", "stock-basics", "ticker-search"]
results = {}
with httpx.Client(timeout=30, follow_redirects=True) as client:
    for page in pages:
        response = client.get(f"https://fuyao.aicubes.cn/docs/api-reference/{page}/")
        body = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", response.text, flags=re.S)
        text = html.unescape(re.sub(r"<[^>]+>", " ", body))
        results[page] = {"http_status": response.status_code, "text": re.sub(r"\s+", " ", text)[:60000]}
output = root / "work" / "fuyao-docs-stock.json"
output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
print(output, {page: item["http_status"] for page, item in results.items()})
