"""Build a source-only release with a content manifest and exact-secret check."""

import hashlib
import json
from pathlib import Path
import zipfile

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = [
    "README.md", "PRODUCT.md", "DESIGN.md", "pyproject.toml", "uv.lock",
    "Dockerfile", ".dockerignore", ".gitignore", ".env.example", "Makefile",
    "01_AI驱动的股票市场大势研判.md", "apps/web/package.json", "apps/web/package-lock.json",
    "apps/web/tsconfig.json", "apps/web/vite.config.ts", "apps/web/index.html", "echart/SKILL.md",
]
SOURCE_DIRS = ["apps/api", "apps/web/src", "research-skills", "scripts", "tests", "docs"]


def main():
    paths = [ROOT / name for name in ROOT_FILES]
    for directory in SOURCE_DIRS:
        paths.extend(p for p in (ROOT / directory).rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    paths.extend((ROOT / "deliverables").glob("*.md"))
    paths = sorted(set(paths))
    local = dotenv_values(ROOT / ".env.local", interpolate=False)
    secrets = [local.get(key) for key in ("DEEPSEEK_API_KEY", "FUYAO_API_KEY", "IFIND_MCP_AUTH_VALUE")]
    manifest = []
    for path in paths:
        data = path.read_bytes()
        if any(secret and len(secret) >= 8 and secret.encode() in data for secret in secrets):
            raise RuntimeError("Credential found in release input: " + str(path.relative_to(ROOT)))
        manifest.append({"path": str(path.relative_to(ROOT)), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    release = ROOT / "deliverables" / "release"
    release.mkdir(exist_ok=True)
    archive = release / "market-research-source.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in paths:
            output.write(path, str(path.relative_to(ROOT)))
        output.writestr("release-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    (release / "manifest.json").write_text(json.dumps({"archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "files": manifest}, ensure_ascii=False, indent=2))
    print(json.dumps({"archive": str(archive), "file_count": len(paths), "bytes": archive.stat().st_size, "exact_secret_check": "passed"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
