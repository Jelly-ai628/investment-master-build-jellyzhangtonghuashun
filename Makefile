.PHONY: install test build serve
install:
	uv sync --locked
	cd apps/web && npm ci
test:
	uv run pytest -q
build:
	cd apps/web && npm run build
serve:
	uv run uvicorn market_research.app:create_app --factory --app-dir apps/api --host 127.0.0.1 --port 8000
