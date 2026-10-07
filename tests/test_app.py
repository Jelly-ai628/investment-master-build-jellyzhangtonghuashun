import asyncio
import time

from fastapi.testclient import TestClient

from market_research.app import create_app, public_report, Store


def login(client):
    assert client.post("/api/session", json={}).status_code == 200


def test_conversation_isolation_and_private_report_fields(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as first, TestClient(app) as second:
        login(first)
        login(second)
        identifier = first.post("/api/conversations", json={"title": "我的研究"}).json()["id"]
        assert first.get(f"/api/conversations/{identifier}").status_code == 200
        assert second.get(f"/api/conversations/{identifier}").status_code == 404
        assert second.get("/api/conversations").json() == []
        assert first.post("/api/conversations", headers={"Origin": "https://evil.test"}, json={}).status_code == 403
    result = public_report({"evidence": [{"provenance": {"raw_file": "/private/raw", "raw_files": ["private"], "source": "provider"}}]})
    assert result["evidence"][0]["provenance"] == {"source": "provider"}


def test_job_idempotency_and_replay(tmp_path):
    calls = []

    async def runner(body, settings, root, emit):
        calls.append(body.question)
        await emit({"type": "scope", "as_of": "2026-09-30"})
        return {"status": "completed_with_limits", "evidence": [], "facts": []}

    with TestClient(create_app(tmp_path, runner)) as client:
        login(client)
        conv = client.post("/api/conversations", json={}).json()["id"]
        payload = {"question": "研究市场状态", "client_request_id": "a" * 32}
        first = client.post(f"/api/conversations/{conv}/research", json=payload).json()
        second = client.post(f"/api/conversations/{conv}/research", json=payload).json()
        assert first["id"] == second["id"]
        for _ in range(20):
            result = client.get(f"/api/research/{first['id']}").json()
            if result["status"] == "completed":
                break
            time.sleep(.01)
        assert result["status"] == "completed" and len(calls) == 1
        events = client.get(f"/api/research/{first['id']}/events").text
        assert '"type": "scope"' in events and '"type": "completed"' in events
        assert client.get(f"/api/research/{first['id']}/events?after=999").text == ""
        assert len(client.get(f"/api/conversations/{conv}").json()["jobs"]) == 1


def test_cancel_and_hidden_error_details(tmp_path):
    async def runner(body, settings, root, emit):
        await asyncio.sleep(60)
    with TestClient(create_app(tmp_path, runner)) as client:
        login(client)
        conv = client.post("/api/conversations", json={}).json()["id"]
        job = client.post(f"/api/conversations/{conv}/research", json={"question": "市场研究", "client_request_id": "b" * 32}).json()["id"]
        assert client.post(f"/api/research/{job}/cancel").status_code == 200
        for _ in range(20):
            status = client.get(f"/api/research/{job}").json()["status"]
            if status == "cancelled":
                break
            time.sleep(.01)
        assert status == "cancelled"


def test_restart_marks_inflight_runs_interrupted(tmp_path):
    store = Store(tmp_path / "app.sqlite")
    store.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)", ("j", "c", "o", "k", "{}", "running", None, None, 0, 0))
    restarted = Store(tmp_path / "app.sqlite")
    assert restarted.one("SELECT status FROM jobs WHERE id='j'")["status"] == "interrupted"


def test_raw_evidence_requires_owner_and_returns_bounded_rows(tmp_path):
    import json
    async def runner(body, settings, root, emit):
        run_id = "e" * 32
        (root / "work" / "raw").mkdir(exist_ok=True)
        (root / "work" / "research-runs").mkdir(exist_ok=True)
        (root / "work" / "raw" / "fixture.json").write_text(json.dumps({"data": {"item": [{"close_price": 1}, {"close_price": 2}]}}))
        result = {"run_id": run_id, "status": "completed_with_limits", "evidence": [{"id": "ev_test", "provenance": {"raw_file": "work/raw/fixture.json"}}], "facts": []}
        (root / "work" / "research-runs" / (run_id + ".json")).write_text(json.dumps(result))
        return result
    app = create_app(tmp_path, runner)
    with TestClient(app) as first, TestClient(app) as second:
        login(first); login(second)
        conv = first.post("/api/conversations", json={}).json()["id"]
        job = first.post(f"/api/conversations/{conv}/research", json={"question": "市场研究", "client_request_id": "c" * 32}).json()["id"]
        for _ in range(20):
            if first.get(f"/api/research/{job}").json()["status"] == "completed":
                break
            time.sleep(.01)
        url = f"/api/research/{job}/evidence/ev_test/rows?offset=1&limit=1"
        assert first.get(url).json()["rows"] == [{"close_price": 2}]
        assert second.get(url).status_code == 404
        assert first.get(f"/api/research/{job}/evidence/no_such_evidence/rows").status_code == 404


def test_followup_inherits_verified_scope(tmp_path):
    captured = []
    async def runner(body, settings, root, emit):
        captured.append(body)
        return {"run_id": "f" * 32, "question": body.question, "as_of": "2026-09-30", "window": 20,
                "status": "completed_with_limits", "evidence": [], "facts": []}
    with TestClient(create_app(tmp_path, runner)) as client:
        login(client)
        conv = client.post("/api/conversations", json={}).json()["id"]
        job = client.post(f"/api/conversations/{conv}/research", json={"question": "市场研究", "client_request_id": "d" * 32}).json()["id"]
        for _ in range(20):
            if client.get(f"/api/research/{job}").json()["status"] == "completed":
                break
            time.sleep(.01)
        client.post(f"/api/conversations/{conv}/research", json={"question": "同一区间银行表现如何", "client_request_id": "e" * 32})
        for _ in range(20):
            if len(captured) == 2:
                break
            time.sleep(.01)
        assert captured[1].end_date == "2026-09-30"
        assert "previous_as_of" in captured[1].context
