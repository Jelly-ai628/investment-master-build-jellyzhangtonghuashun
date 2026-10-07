"""Same-origin local Web API: isolated conversations, durable jobs and SSE progress."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import urlparse
import uuid

from fastapi import FastAPI, HTTPException, Request, Response, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .connections import Settings, ProbeError
from .research import ResearchRequest, run_research

ROOT = Path(__file__).resolve().parents[3]
COOKIE = "market_session"


class ConversationInput(BaseModel):
    title: str = Field(default="新的市场研究", min_length=1, max_length=120)


class JobInput(ResearchRequest):
    client_request_id: str = Field(min_length=16, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


class LoginInput(BaseModel):
    code: str = Field(default="", max_length=256)


def public_report(result):
    copied = json.loads(json.dumps(result))
    def strip(value):
        if isinstance(value, dict):
            value.pop("raw_file", None)
            value.pop("raw_files", None)
            for item in value.values():
                strip(item)
        elif isinstance(value, list):
            for item in value:
                strip(item)
    strip(copied)
    copied.pop("events", None)
    return copied


class Store:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, created REAL);
                CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, owner TEXT, title TEXT, created REAL, updated REAL);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, conversation TEXT, owner TEXT, client_key TEXT,
                  request TEXT, status TEXT, result TEXT, error TEXT, created REAL, updated REAL,
                  UNIQUE(conversation, client_key));
                CREATE TABLE IF NOT EXISTS events(job TEXT, seq INTEGER, payload TEXT, PRIMARY KEY(job,seq));
            """)
            db.execute("UPDATE jobs SET status='interrupted',error='服务重启，本轮研究中断' WHERE status IN ('queued','running')")
        path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def execute(self, sql, params=()):
        with self.connect() as db:
            db.execute(sql, params)

    def one(self, sql, params=()):
        with self.connect() as db:
            row = db.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql, params=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    def event(self, job, item):
        with self.connect() as db:
            seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE job=?", (job,)).fetchone()[0]
            item = {**item, "seq": seq}
            db.execute("INSERT INTO events VALUES(?,?,?)", (job, seq, json.dumps(item, ensure_ascii=False)))


def create_app(root=ROOT, runner=run_research):
    access_code = os.environ.get("MARKET_ACCESS_CODE", "")
    public_mode = os.environ.get("MARKET_PUBLIC", "") == "1"
    if public_mode and len(access_code) < 12:
        raise RuntimeError("Public deployment requires MARKET_ACCESS_CODE with at least 12 characters")
    store = Store(root / "work" / "app.sqlite")
    tasks = {}
    lock = asyncio.Semaphore(1)

    @asynccontextmanager
    async def lifespan(app):
        yield
        for task in list(tasks.values()):
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)

    app = FastAPI(title="市场研判", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store = store

    @app.middleware("http")
    async def protect(request, call_next):
        # Local default cannot be silently exposed by binding a public address.
        if not public_mode and request.client and request.client.host not in ("127.0.0.1", "::1", "testclient"):
            return Response(status_code=403)
        if request.method in ("POST", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return Response(status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def owner(request):
        token = request.cookies.get(COOKIE, "")
        key = hashlib.sha256(token.encode()).hexdigest()
        if not token or not store.one("SELECT id FROM sessions WHERE id=? AND created>?", (key, time.time() - 30 * 86400)):
            raise HTTPException(401, "请先建立会话")
        return key

    def conversation(request, identifier):
        row = store.one("SELECT * FROM conversations WHERE id=? AND owner=?", (identifier, owner(request)))
        if not row:
            raise HTTPException(404, "对话不存在")
        return row

    def job_for(request, identifier):
        row = store.one("SELECT * FROM jobs WHERE id=? AND owner=?", (identifier, owner(request)))
        if not row:
            raise HTTPException(404, "研究不存在")
        return row

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "access_required": bool(access_code), "scope": "行情与宽度研究，其他维度按证据可用性呈现"}

    @app.post("/api/session")
    async def session(body: LoginInput, request: Request, response: Response):
        try:
            owner(request)
            return {"authenticated": True}
        except HTTPException:
            pass
        if access_code and not secrets.compare_digest(body.code, access_code):
            raise HTTPException(401, "体验码不正确")
        token = secrets.token_urlsafe(32)
        store.execute("INSERT INTO sessions VALUES(?,?)", (hashlib.sha256(token.encode()).hexdigest(), time.time()))
        response.set_cookie(COOKIE, token, httponly=True, secure=public_mode, samesite="strict", max_age=30 * 86400)
        return {"authenticated": True}

    @app.get("/api/conversations")
    async def list_conversations(request: Request):
        return store.all("SELECT id,title,created,updated FROM conversations WHERE owner=? ORDER BY updated DESC", (owner(request),))

    @app.post("/api/conversations")
    async def new_conversation(body: ConversationInput, request: Request):
        identifier = uuid.uuid4().hex
        now = time.time()
        store.execute("INSERT INTO conversations VALUES(?,?,?,?,?)", (identifier, owner(request), body.title, now, now))
        return {"id": identifier, "title": body.title}

    @app.patch("/api/conversations/{identifier}")
    async def rename_conversation(identifier: str, body: ConversationInput, request: Request):
        conversation(request, identifier)
        store.execute("UPDATE conversations SET title=?,updated=? WHERE id=?", (body.title, time.time(), identifier))
        return {"id": identifier, "title": body.title}

    @app.get("/api/conversations/{identifier}")
    async def read_conversation(identifier: str, request: Request):
        row = conversation(request, identifier)
        jobs = store.all("SELECT id,request,status,result,error,created FROM jobs WHERE conversation=? ORDER BY created", (identifier,))
        for job in jobs:
            job["request"] = json.loads(job["request"])
            job["result"] = json.loads(job["result"]) if job["result"] else None
        return {"id": row["id"], "title": row["title"], "jobs": jobs}

    async def execute_job(identifier, body):
        async def emit(event):
            # The model's 'completed' progress event is not durable job completion.
            if event["type"] != "completed":
                store.event(identifier, event)
        try:
            async with lock:
                store.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), identifier))
                result = await asyncio.wait_for(runner(body, Settings.load(root), root, emit), timeout=240)
                safe = public_report(result)
                store.execute("UPDATE jobs SET status='completed',result=?,updated=? WHERE id=?", (json.dumps(safe, ensure_ascii=False), time.time(), identifier))
                store.event(identifier, {"type": "completed", "result_status": result["status"]})
        except asyncio.CancelledError:
            store.execute("UPDATE jobs SET status='cancelled',error='用户停止了本次研究',updated=? WHERE id=?", (time.time(), identifier))
            store.event(identifier, {"type": "cancelled"})
            raise
        except Exception as error:
            reason = error.category if isinstance(error, ProbeError) else "research_timeout" if isinstance(error, TimeoutError) else "research_failed"
            store.execute("UPDATE jobs SET status='failed',error=?,updated=? WHERE id=?", (reason, time.time(), identifier))
            store.event(identifier, {"type": "failed", "reason": reason})
        finally:
            tasks.pop(identifier, None)

    @app.post("/api/conversations/{identifier}/research")
    async def start_research(identifier: str, body: JobInput, request: Request):
        row = conversation(request, identifier)
        existing = store.one("SELECT id,status FROM jobs WHERE conversation=? AND client_key=?", (identifier, body.client_request_id))
        if existing:
            return existing
        active = store.one("SELECT id FROM jobs WHERE owner=? AND status IN ('queued','running')", (row["owner"],))
        if active:
            raise HTTPException(409, "已有研究正在进行，请等待或停止后重试")
        recent = store.one("SELECT COUNT(*) AS count FROM jobs WHERE owner=? AND created>?", (row["owner"], time.time() - 3600))
        if recent["count"] >= 20:
            raise HTTPException(429, "本小时研究次数已达上限")
        identifier_job = uuid.uuid4().hex
        payload = body.model_dump(exclude={"client_request_id"})
        previous = store.one("SELECT request,result FROM jobs WHERE conversation=? AND status='completed' AND result IS NOT NULL ORDER BY created DESC LIMIT 1", (identifier,))
        if previous:
            prior = json.loads(previous["result"])
            payload["context"] = json.dumps({"previous_question": prior.get("question"), "previous_as_of": prior.get("as_of"), "previous_window": prior.get("window"),
                "previous_indices": [e["data"].get("name") for e in prior.get("evidence", []) if e.get("kind") == "index_history"]}, ensure_ascii=False)[:2500]
            if not payload.get("end_date") and re.search("同一|那|继续|相比|上个月", payload["question"]) and "最新" not in payload["question"]:
                payload["end_date"] = prior.get("as_of")
        store.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)", (identifier_job, identifier, row["owner"], body.client_request_id, json.dumps(payload, ensure_ascii=False), "queued", None, None, time.time(), time.time()))
        store.execute("UPDATE conversations SET updated=? WHERE id=?", (time.time(), identifier))
        store.event(identifier_job, {"type": "queued", "label": "研究已排队"})
        tasks[identifier_job] = asyncio.create_task(execute_job(identifier_job, ResearchRequest(**payload)))
        return {"id": identifier_job, "status": "queued"}

    @app.get("/api/research/{identifier}")
    async def read_job(identifier: str, request: Request):
        row = job_for(request, identifier)
        body = {"id": row["id"], "status": row["status"], "error": row["error"], "request": json.loads(row["request"]), "result": json.loads(row["result"]) if row["result"] else None}
        if row["status"] in ("queued", "running"):
            # Proxies such as tunnels may buffer the event stream; polling still carries recent progress.
            body["progress"] = [json.loads(item["payload"]) for item in store.all("SELECT payload FROM events WHERE job=? ORDER BY seq DESC LIMIT 40", (identifier,))][::-1]
        return body

    @app.post("/api/research/{identifier}/cancel")
    async def cancel_job(identifier: str, request: Request):
        row = job_for(request, identifier)
        task = tasks.get(identifier)
        if task:
            store.execute("UPDATE jobs SET status='cancelled',error='用户停止了本次研究',updated=? WHERE id=?", (time.time(), identifier))
            task.cancel()
        return {"id": identifier, "status": "cancelling" if task else row["status"]}

    @app.get("/api/research/{identifier}/evidence/{evidence_id}/rows")
    async def evidence_rows(identifier: str, evidence_id: str, request: Request, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), source: int = Query(0, ge=0)):
        row = job_for(request, identifier)
        if not row["result"]:
            raise HTTPException(404, "研究尚未完成")
        public = json.loads(row["result"])
        run_id = public.get("run_id", "")
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise HTTPException(404, "证据不存在")
        archive = root / "work" / "research-runs" / (run_id + ".json")
        if not archive.exists():
            raise HTTPException(404, "原始证据暂不可用")
        private = json.loads(archive.read_text())
        item = next((item for item in private.get("evidence", []) if item.get("id") == evidence_id), None)
        if not item:
            raise HTTPException(404, "证据不存在")
        provenance = item["provenance"]
        files = provenance.get("raw_files") or [provenance.get("raw_file")]
        if source >= len(files) or not files[source]:
            raise HTTPException(404, "原始记录不存在")
        path = (root / files[source]).resolve()
        if not path.is_relative_to((root / "work" / "raw").resolve()) or not path.is_file():
            raise HTTPException(404, "原始记录不可用")
        def load_rows():
            if path.suffix == ".parquet":
                import pyarrow.parquet as pq
                from datetime import datetime
                from .connections import SHANGHAI
                stamp = int(datetime.strptime(public["as_of"], "%Y-%m-%d").replace(tzinfo=SHANGHAI).timestamp() * 1000)
                return pq.read_table(path, filters=[("date_ms", "=", stamp)]).to_pylist()
            raw = json.loads(path.read_text())
            if "pages" in raw:
                return [item for page in raw["pages"] for item in page["data"]["item"]]
            return raw.get("data", {}).get("item", [])
        items = await asyncio.to_thread(load_rows)
        return {"offset": offset, "total": len(items), "source_index": source, "source_count": len(files), "rows": items[offset:offset + limit]}

    @app.get("/api/research/{identifier}/events")
    async def events(identifier: str, request: Request, after: int = 0):
        job_for(request, identifier)
        last_id = request.headers.get("last-event-id", "")
        if last_id.isdigit():
            after = max(after, int(last_id))
        async def stream():
            cursor = max(after, 0)
            while not await request.is_disconnected():
                found = store.all("SELECT seq,payload FROM events WHERE job=? AND seq>? ORDER BY seq", (identifier, cursor))
                for event in found:
                    cursor = event["seq"]
                    yield f"id: {cursor}\ndata: {event['payload']}\n\n"
                state = store.one("SELECT status FROM jobs WHERE id=?", (identifier,))
                if state["status"] not in ("queued", "running"):
                    break
                await asyncio.sleep(.3)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    dist = root / "apps" / "web" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")
        @app.get("/")
        async def index():
            return FileResponse(dist / "index.html")
    return app

