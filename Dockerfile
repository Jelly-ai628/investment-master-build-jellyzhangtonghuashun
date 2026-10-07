FROM node:22-bookworm-slim AS web
WORKDIR /app/apps/web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
WORKDIR /app
ENV UV_LINK_MODE=copy PYTHONUNBUFFERED=1 MARKET_PUBLIC=1
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY apps/api/ ./apps/api/
COPY research-skills/ ./research-skills/
COPY --from=web /app/apps/web/dist ./apps/web/dist
RUN useradd --create-home researcher && mkdir -p /app/work && chown researcher:researcher /app/work
USER researcher
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "market_research.app:create_app", "--factory", "--app-dir", "apps/api", "--host", "0.0.0.0", "--port", "8000"]
