"""P1 RSI API tests: observations / trigger / retro + role self boundary."""
from __future__ import annotations

import json

import pytest

from anvil import config
from anvil.db import open_conn
from anvil.services import role_eval, role_space


@pytest.fixture(autouse=True)
def _rsi_enabled(app, monkeypatch):
    # create_app applies feature overrides; set the flag after app construction.
    monkeypatch.setattr(config, "ROLE_RSI_ENABLED", True)
from anvil.util import now_iso
from tests.conftest import (
    API, app_factory, auth_headers, human_headers, role_payload, runtime_payload,
)


@pytest.fixture
def app(tmp_path, monkeypatch):
    data = tmp_path / "data"
    agents = data / "agents"
    repos = data / "repos"
    runtimes = data / "runtimes"
    for d in (agents, repos, runtimes):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    monkeypatch.setattr(config, "REPOS_DIR", repos)
    monkeypatch.setattr(config, "USER_RUNTIMES_DIR", runtimes)
    return app_factory.create_app(
        {"db_url": f"sqlite+aiosqlite:///{tmp_path}/test.db", "test_auth": True}
    )


async def _seed(client, app):
    await client.post(f"{API}/runtimes", json=runtime_payload(executable_path="/bin/echo"),
                      headers=auth_headers())
    await client.post(f"{API}/roles", json=role_payload(), headers=auth_headers())
    conn = open_conn(app.state.db_path)
    try:
        row = conn.execute(
            "SELECT definition_json FROM roles WHERE slug='qa-role'").fetchone()
        definition = json.loads(row["definition_json"] or "{}")
        definition["rsi"] = {"enabled": True}
        conn.execute("UPDATE roles SET definition_json=? WHERE slug='qa-role'",
                     (json.dumps(definition),))
        conn.execute(
            "INSERT INTO tasks (id, title, type, status, assignee, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?)",
            ("T-RSI-API", "API 观测", "feature", "done", "qa-role",
             now_iso(), now_iso()))
        conn.execute(
            "INSERT INTO task_assignees (task_id, role_slug, joined_at) "
            "VALUES (?,?,?)", ("T-RSI-API", "qa-role", now_iso()))
        conn.execute(
            "INSERT INTO task_role_claims (task_id, role_slug, status, updated_at) "
            "VALUES (?,?,?,?)", ("T-RSI-API", "qa-role", "approved", now_iso()))
        conn.commit()
    finally:
        conn.close()


async def test_observations_trigger_and_retro(client, app):
    await _seed(client, app)
    obs = await client.get(f"{API}/rsi/roles/qa-role/observations",
                           headers=auth_headers("qa-role"))
    assert obs.status_code == 200
    assert obs.json()["count"] == 0
    retro = await client.post(
        f"{API}/rsi/roles/qa-role/retro",
        json={"task_id": "T-RSI-API", "content": "复盘：先跑验收"},
        headers=auth_headers("qa-role"))
    assert retro.status_code == 200
    assert retro.json()["event_id"]
    obs = await client.get(f"{API}/rsi/roles/qa-role/observations",
                           headers=auth_headers("qa-role"))
    assert obs.status_code == 200
    trig = await client.get(f"{API}/rsi/roles/qa-role/trigger",
                            headers=auth_headers("qa-role"))
    assert trig.status_code == 200
    assert "due" in trig.json()
    # Human can read. Agent-role 403 is covered by the real-session security
    # suite; ANVIL_TEST_AUTH deliberately bypasses object-level auth here.
    assert (await client.get(f"{API}/rsi/roles/qa-role/observations",
                             headers=human_headers())).status_code == 200
    suite = await client.get(f"{API}/rsi/suite/status", headers=human_headers())
    assert suite.status_code == 200
    assert "suites" in suite.json()
    evals = await client.get(f"{API}/rsi/roles/qa-role/evaluations",
                             headers=human_headers())
    assert evals.status_code == 200
    assert evals.json()["items"] == []


async def test_ensure_baseline_endpoint_is_idempotent(client, app):
    await _seed(client, app)
    first = await client.post(f"{API}/roles/qa-role/versions/baseline",
                              headers=human_headers())
    assert first.status_code == 200
    assert first.json()["status"] == "active"
    assert first.json()["source"] == "baseline"
    again = await client.post(f"{API}/roles/qa-role/versions/baseline",
                              headers=human_headers())
    assert again.status_code == 200
    assert again.json()["version"] == first.json()["version"]


async def test_version_diff_preview(client, app):
    await _seed(client, app)
    conn = open_conn(app.state.db_path)
    try:
        version = role_space.snapshot(
            conn, "qa-role", kind="evolution", source="baseline",
            status="active")
    finally:
        conn.close()
    resp = await client.get(
        f"{API}/roles/qa-role/versions/{version['version']}/diff",
        headers=human_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["target_version"] == version["version"]
    assert body["active_version"] == version["version"]
    assert body["knowledge"]["restore"] == 0


async def test_enqueue_evaluation_creates_queued_run(client, app):
    await _seed(client, app)
    conn = open_conn(app.state.db_path)
    try:
        baseline = role_space.snapshot(
            conn, "qa-role", kind="evolution", source="baseline",
            status="active")
        candidate = role_space.snapshot(
            conn, "qa-role", kind="evolution", source="rsi_candidate",
            status="candidate", force=True)
    finally:
        conn.close()
    resp = await client.post(
        f"{API}/rsi/roles/qa-role/evaluations",
        json={"suite_id": "s1", "candidate_version": candidate["version"]},
        headers=human_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["baseline_version"] == baseline["version"]
    conn = open_conn(app.state.db_path)
    try:
        run = role_eval.get_run(conn, body["run_id"])
        assert run["status"] == "queued"
        assert run["request"]["noise_floor"] == 0.10
    finally:
        conn.close()
    bad = await client.post(
        f"{API}/rsi/roles/qa-role/evaluations",
        json={"suite_id": "s1", "candidate_version": candidate["version"],
              "candidate_id": "P-x"},
        headers=human_headers())
    assert bad.status_code == 422
