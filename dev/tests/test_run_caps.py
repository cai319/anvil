"""v1.62 #13：提交记录 numstat；diff / 任务时长超限升级人工，不硬失败。"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import inbox, reviews, run_caps
from anvil.services import workflow as wf_service
from anvil.util import ConflictError, now_iso


def _git(root, *args):
    subprocess.run(
        ["git", "-C", str(root), *args], check=True,
        capture_output=True, text=True, encoding="utf-8")


def _worktree(tmp_path, *, added: int = 6, deleted: int = 1):
    root = tmp_path / "wt"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src" / "app.py").write_text(
        "\n".join(f"old {i}" for i in range(deleted)) + "\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    (root / "src" / "app.py").write_text(
        "\n".join(f"new {i}" for i in range(added)) + "\n", encoding="utf-8")
    return root


def _setup(tmp_path, *, worktree, with_decision: bool = False):
    conn = connect(str(tmp_path / "caps.db"))
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T1','caps','in_progress',?,?)", (now, now))
    conn.commit()
    steps = [{"id": "S1", "title": "实现", "role": "dev", "gate": True,
              "slice_key": "auth", "scope_paths": ["src/**", "tests/**"]}]
    if with_decision:
        steps.append({"id": "S0", "title": "准备", "role": "lead"})
        steps.append({"id": "D1", "title": "决策", "role": "lead",
                      "kind": "decision", "depends_on": ["S1", "S0"],
                      "on_reject": ["S0"], "max_rework": 2,
                      "slice_key": "auth"})
    wf = wf_service.create_workflow(conn, "T1", {"steps": steps})
    wf_service.advance(conn, wf["id"], starter=lambda *a: {"id": "r1"})
    conn.execute(
        "INSERT INTO runs (id,role_slug,task_id,status,worktree_path,started_at) "
        "VALUES ('r1','dev','T1','running',?,?)", (str(worktree), now))
    conn.commit()
    return conn, wf["id"]


def _step(conn, wid, sid="S1"):
    return next(s for s in wf_service.get_workflow(conn, wid)["steps"]
                if s["step_id"] == sid)


def _submit(conn, wid, changed):
    return wf_service.submit_step(
        conn, wid, "S1", "dev", "完成", changed_paths=changed)


def test_parse_numstat_sums_added_deleted_and_ignores_binary():
    stats = run_caps.parse_numstat([
        "12\t3\tsrc/a.py",
        "0\t7\tsrc/b.py",
        "-\t-\tassets/logo.png",
    ])
    assert stats["added"] == 12
    assert stats["deleted"] == 10
    assert stats["total"] == 22
    assert set(stats["files"]) == {"src/a.py", "src/b.py"}


def test_submit_records_numstat_and_escalates_diff_cap_to_inbox_and_packet(
        tmp_path, monkeypatch):
    worktree = _worktree(tmp_path)
    conn, wid = _setup(tmp_path, worktree=worktree, with_decision=True)
    monkeypatch.setattr(config, "RUN_CAP_ENFORCE", True)
    monkeypatch.setattr(config, "DIFF_CAP_LINES", 5)
    monkeypatch.setattr(config, "TASK_DURATION_CAP_SECONDS", 0)

    wf = _submit(conn, wid, ["src/app.py"])

    step = _step(conn, wid)
    assert step["diff_stats"]["added"] == 6
    assert step["diff_stats"]["deleted"] == 1
    # 超限不硬失败：步骤照常提交（gate 等待人工，而不是 failed/异常）。
    assert step["status"] == "submitted"
    packet = reviews.latest_packet(conn, wid, "S1")
    assert packet is not None
    assert "diff_cap_exceeded" in packet["risks"]
    titles = [i["title"] for i in inbox.collect(conn)["items"]
              if i["kind"] == "mention"]
    assert any("diff" in title for title in titles)
    # 同切片的超限也进入 decide 评审包，AI 不能当作正常通过。
    wf_view = wf_service.get_workflow(conn, wid)
    decide_step = _step(conn, wid, "D1")
    decide_packet = reviews.ensure_packet(
        conn, wf_view, decide_step, created_by="test")
    assert "diff_cap_exceeded" in decide_packet["risks"]
    assert "diff_cap_exceeded" in reviews.escalation_triggers(
        decide_step, decide_packet)
    # fix 轮次上限仍是 2；升级不改变返工预算。
    assert _step(conn, wid, "D1")["max_rework"] == 2


def test_enforcement_off_still_records_numstat(tmp_path, monkeypatch):
    worktree = _worktree(tmp_path)
    conn, wid = _setup(tmp_path, worktree=worktree)
    monkeypatch.setattr(config, "RUN_CAP_ENFORCE", False)
    monkeypatch.setattr(config, "DIFF_CAP_LINES", 1)
    monkeypatch.setattr(config, "TASK_DURATION_CAP_SECONDS", 1)

    _submit(conn, wid, ["src/app.py"])

    assert _step(conn, wid)["diff_stats"]["total"] == 7
    packet = reviews.latest_packet(conn, wid, "S1")
    assert packet is not None
    assert "diff_cap_exceeded" not in packet["risks"]
    assert not any(i["kind"] == "mention" for i in inbox.collect(conn)["items"])


def test_task_duration_cap_escalates_to_human(tmp_path, monkeypatch):
    worktree = _worktree(tmp_path)
    conn, wid = _setup(tmp_path, worktree=worktree)
    monkeypatch.setattr(config, "RUN_CAP_ENFORCE", True)
    monkeypatch.setattr(config, "DIFF_CAP_LINES", 0)
    monkeypatch.setattr(config, "TASK_DURATION_CAP_SECONDS", 1)
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    conn.execute("UPDATE workflows SET created_at=? WHERE id=?", (old, wid))
    conn.commit()

    _submit(conn, wid, ["src/app.py"])

    packet = reviews.latest_packet(conn, wid, "S1")
    assert packet is not None
    assert "task_duration_cap_exceeded" in packet["risks"]
    titles = [i["title"] for i in inbox.collect(conn)["items"]
              if i["kind"] == "mention"]
    assert any("时长" in title or "duration" in title.lower() for title in titles)


def test_cap_does_not_bypass_path_lock(tmp_path, monkeypatch):
    worktree = _worktree(tmp_path)
    conn, wid = _setup(tmp_path, worktree=worktree)
    monkeypatch.setattr(config, "RUN_CAP_ENFORCE", True)
    monkeypatch.setattr(config, "DIFF_CAP_LINES", 1)
    monkeypatch.setattr(config, "TASK_DURATION_CAP_SECONDS", 0)
    conn.execute(
        "INSERT INTO workflow_steps (workflow_id, step_id, title, role_slug, "
        "slice_key, slice_lock_json, status, updated_at) "
        "VALUES (?,?,?,?,?,?, 'approved', ?)",
        (wid, "P1.auth.scope", "scope", "lead", "auth",
         json.dumps({"version": 1, "paths": ["tests/test_auth.py"],
                     "test_files": ["tests/test_auth.py"]}), now_iso()))
    conn.commit()

    with pytest.raises(ConflictError, match="lock_violation"):
        _submit(conn, wid, ["tests/test_auth.py"])
