"""GET /work-items：四类 durable work item 的统一清单（人工可读）。"""
from __future__ import annotations

import sqlite3

import pytest

from anvil.util import now_iso
from tests.conftest import API, auth_headers, human_headers

pytestmark = pytest.mark.usefixtures("client")


def _seed(app) -> None:
    now = now_iso()
    conn = sqlite3.connect(app.state.db_path)
    try:
        conn.execute(
            "INSERT INTO wake_requests (task_id,role_slug,reason,note,status,"
            "attempts,created_at) VALUES ('T1','dev','delegation.submitted','n',"
            "'pending',0,?)", (now,))
        conn.execute(
            "INSERT INTO reminders (id,role_slug,kind,prompt,status,created_at) "
            "VALUES ('R1','dev','when','check inbox','pending',?)", (now,))
        conn.execute(
            "INSERT INTO action_cards (id,role_slug,task_id,kind,payload_json,"
            "status,created_at) VALUES ('A1','dev','T1','task.reopen','{}',"
            "'pending',?)", (now,))
        conn.execute(
            "INSERT INTO board_drafts (id,goal,tree_json,status,created_at) "
            "VALUES ('D1','目标','{}','pending',?)", (now,))
        conn.commit()
    finally:
        conn.close()


async def test_work_items_endpoint(client, app):
    _seed(app)
    r = await client.get(f"{API}/work-items", headers=human_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    by_kind: dict[str, list[dict]] = {}
    for item in body["items"]:
        by_kind.setdefault(item["kind"], []).append(item)
    assert set(by_kind) == {"wake", "reminder", "action_card", "board_draft"}
    assert body["counts"]["total"] == len(body["items"]) == sum(
        body["counts"][k] for k in by_kind)
    assert any("delegation.submitted" in i["summary"] for i in by_kind["wake"])
    assert any(i["summary"] == "check inbox" for i in by_kind["reminder"])
    assert any(i["summary"] == "task.reopen" for i in by_kind["action_card"])
    assert any(i["summary"] == "目标" for i in by_kind["board_draft"])


async def test_work_items_requires_human(client, app):
    r = await client.get(f"{API}/work-items", headers=auth_headers("dev"))
    assert r.status_code == 403
