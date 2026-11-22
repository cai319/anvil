"""任务状态单写者：所有 status UPDATE 经过 transition_task 的 CAS。"""
from __future__ import annotations

import pytest

from anvil.db import connect
from anvil.services import tasks as tasks_service


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "t.db"))
    yield c
    c.close()


def test_transition_task_cas_and_fields(conn):
    task = tasks_service.create(conn, title="T")
    assert tasks_service.transition_task(
        conn, task["id"], "in_progress",
        from_statuses=("todo",), fields={"assignee": "dev"}) is True

    after = tasks_service.get(conn, task["id"])
    assert after["status"] == "in_progress"
    assert after["assignee"] == "dev"

    # CAS 失败：当前状态不在允许集合，不得改写。
    assert tasks_service.transition_task(
        conn, task["id"], "done", from_statuses=("todo",)) is False
    assert tasks_service.get(conn, task["id"])["status"] == "in_progress"


def test_transition_task_extra_where_is_enforced(conn):
    task = tasks_service.create(conn, title="T2")
    assert tasks_service.transition_task(
        conn, task["id"], "in_progress",
        from_statuses=("todo",), extra_where="assignee IS NULL") is True

    other = tasks_service.create(conn, title="T3")
    conn.execute("UPDATE tasks SET assignee='someone' WHERE id=?", (other["id"],))
    conn.commit()
    assert tasks_service.transition_task(
        conn, other["id"], "in_progress",
        from_statuses=("todo",), extra_where="assignee IS NULL") is False
    assert tasks_service.get(conn, other["id"])["status"] == "todo"


def test_transition_task_raw_sets_preserve_existing_values(conn):
    task = tasks_service.create(conn, title="T4")
    conn.execute(
        "UPDATE tasks SET assignee='owner', claimed_at='2020-01-01T00:00:00+00:00' "
        "WHERE id=?", (task["id"],))
    conn.commit()

    assert tasks_service.transition_task(
        conn, task["id"], "in_progress",
        raw_sets=[
            ("assignee=COALESCE(NULLIF(assignee,''), ?)", "other"),
            ("claimed_at=COALESCE(claimed_at, ?)", "2026-01-01T00:00:00+00:00"),
        ],
        now="2026-01-02T00:00:00+00:00") is True

    after = tasks_service.get(conn, task["id"])
    assert after["status"] == "in_progress"
    assert after["assignee"] == "owner"
    assert after["claimed_at"] == "2020-01-01T00:00:00+00:00"
    assert after["updated_at"] == "2026-01-02T00:00:00+00:00"


def test_task_status_sql_is_centralized():
    """运行期禁止在 tasks.py/db.py 之外直接写 tasks.status。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "backend" / "anvil"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name in ("tasks.py", "db.py"):
            continue
        if "UPDATE tasks SET status" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(root)))
    assert offenders == []


def test_no_runtime_dispatch_status_mirror_writes():
    """dispatch_status 只允许从 task_dispatches 派生；运行期不得回写镜像列。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "backend" / "anvil"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "db.py":  # 历史 migration 保留
            continue
        if "UPDATE tasks SET dispatch_status" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(root)))
    assert offenders == []
