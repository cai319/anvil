"""P0.6 队列统一接口：wake / reminder / action_card / board_draft 归一化。

不合并表：四类 durable work item 各自保留权威表与处理语义，只统一读取契约。
"""
from __future__ import annotations

from anvil.db import connect
from anvil.services import work_items
from anvil.util import now_iso


def _seed(conn) -> None:
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T1','t','todo',?,?)", (now, now))
    conn.execute(
        "INSERT INTO roles (slug,display_name,home_path,definition_json,created_at,"
        "updated_at,enabled) VALUES ('dev','DEV','/tmp/dev','{}',?,?,1)", (now, now))
    conn.execute(
        "INSERT INTO wake_requests (task_id,role_slug,reason,note,status,attempts,"
        "created_at) VALUES ('T1','dev','delegation.submitted','n','pending',2,?)",
        (now,))
    conn.execute(
        "INSERT INTO wake_requests (task_id,role_slug,reason,note,status,attempts,"
        "created_at) VALUES ('T1','dev','old','old note','done',1,?)", (now,))
    conn.execute(
        "INSERT INTO reminders (id,role_slug,kind,prompt,status,next_fire_at,"
        "created_at) VALUES ('R1','dev','cron','daily review','pending',?,?)",
        ("2026-12-01T00:00:00+00:00", now))
    conn.execute(
        "INSERT INTO action_cards (id,role_slug,task_id,kind,payload_json,reason,"
        "status,created_at) VALUES ('A1','dev','T1','task.cancel','{\"x\":1}',"
        "'no longer needed','pending',?)", (now,))
    conn.execute(
        "INSERT INTO board_drafts (id,goal,tree_json,status,created_at) "
        "VALUES ('D1','拆解目标','{\"tasks\":[]}','pending',?)", (now,))
    conn.commit()


def test_lists_pending_work_items_across_queues(tmp_path):
    conn = connect(str(tmp_path / "w.db"))
    _seed(conn)

    out = work_items.list_items(conn)

    assert out["counts"] == {
        "wake": 1, "reminder": 1, "action_card": 1, "board_draft": 1, "total": 4}
    by_kind = {i["kind"]: i for i in out["items"]}

    wake = by_kind["wake"]
    assert wake["id"] == "1"
    assert wake["status"] == "pending" and wake["pending"] is True
    assert wake["attempts"] == 2
    assert wake["role_slug"] == "dev" and wake["task_id"] == "T1"
    assert "delegation.submitted" in wake["summary"]
    assert wake["payload"]["note"] == "n"

    reminder = by_kind["reminder"]
    assert reminder["id"] == "R1"
    assert reminder["due_at"] == "2026-12-01T00:00:00+00:00"
    assert reminder["summary"] == "daily review"
    assert reminder["payload"]["kind"] == "cron"

    card = by_kind["action_card"]
    assert card["task_id"] == "T1"
    assert card["summary"] == "task.cancel"
    assert card["payload"]["x"] == 1
    assert card["payload"]["reason"] == "no longer needed"

    draft = by_kind["board_draft"]
    assert draft["summary"] == "拆解目标"
    assert draft["payload"]["goal"] == "拆解目标"


def test_filters_by_kind_role_task_and_pending(tmp_path):
    conn = connect(str(tmp_path / "w2.db"))
    _seed(conn)

    only = work_items.list_items(conn, kinds=["wake", "action_card"])
    assert {i["kind"] for i in only["items"]} == {"wake", "action_card"}
    assert only["counts"]["total"] == 2

    by_role = work_items.list_items(conn, role="dev")
    assert by_role["counts"]["total"] == 3  # board_draft 无 role
    by_task = work_items.list_items(conn, task_id="T1")
    assert {i["kind"] for i in by_task["items"]} == {"wake", "action_card"}

    everything = work_items.list_items(conn, pending_only=False)
    assert everything["counts"] == {
        "wake": 2, "reminder": 1, "action_card": 1, "board_draft": 1, "total": 5}
    done = next(i for i in everything["items"] if i["kind"] == "wake"
                and i["status"] == "done")
    assert done["pending"] is False
