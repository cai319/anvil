"""Task replay log rendering."""
from __future__ import annotations

from anvil.db import connect
from anvil.services import replay, tasks as tasks_service
from anvil.services import workflow as wf_service
from anvil.util import now_iso


def _task(conn, tid="T1"):
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,tags_json,created_at,updated_at) "
        "VALUES (?,?, 'todo',?,?,?)",
        (tid, "回放任务", '["replay","demo"]', now, now))
    conn.execute(
        "INSERT INTO task_assignees (task_id, role_slug, joined_at) VALUES (?,?,?)",
        (tid, "dev", now))
    conn.commit()


def test_render_replay_log_includes_workflow_and_events(tmp_path):
    conn = connect(str(tmp_path / "r.db"))
    _task(conn)
    tasks_service.comment(conn, "T1", "dev", "第一行\n第二行")
    tasks_service.mention(conn, "T1", "dev", "human", "请确认口径")
    wf = wf_service.create_workflow(conn, "T1", {"steps": [
        {"id": "G1", "title": "设计评审", "role": "qa", "gate": True,
         "reviewer": "lead", "acceptance": ["契约稳定"]}]})
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "run-1"})
    wf_service.submit_step(conn, wf["id"], "G1", "qa", "设计完成")
    wf_service.review_step(conn, wf["id"], "G1", True, "通过")

    text = replay.render_log(conn, "T1")
    assert "# Anvil Task Replay Log" in text
    assert "Title      : 回放任务" in text
    assert "Assignees  : dev" in text
    assert "## Execution 1 · done" in text
    assert "G1 [approved] qa · 设计评审" in text
    assert "message.comment" in text
    assert "第一行" in text and "第二行" in text
    assert "@human: 请确认口径" in text
    assert "PACKET" in text
    assert "DECISION approve" in text
    assert "End of replay log" in text
    assert replay.log_filename("T1").startswith("replay-T1-")
    assert replay.log_filename("T1").endswith(".log")


def test_render_replay_log_falls_back_to_activities(tmp_path):
    conn = connect(str(tmp_path / "r.db"))
    _task(conn)
    tasks_service.comment(conn, "T1", "dev", "仅活动投影")
    conn.execute("DELETE FROM events WHERE task_id='T1'")
    conn.commit()
    text = replay.render_log(conn, "T1")
    assert "activity.comment" in text
    assert "仅活动投影" in text
