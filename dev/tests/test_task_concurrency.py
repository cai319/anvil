"""并发闸门与 claim 归属：以活跃 run 计数，空闲 claim 不占额度。"""
from __future__ import annotations

import pytest

from anvil.db import connect
from anvil.services import roles as roles_service
from anvil.services import runs as runs_service
from anvil.services import tasks as tasks_service
from anvil.util import ConflictError


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "c.db"))
    roles_service.create(c, slug="worker", display_name="Worker", definition={
        "runtime": {"id": "claude-code"},
        "concurrency": {"max_parallel": 2},
        "workspace": {"repos": []},
    })
    yield c
    c.close()


def _task(conn, tid):
    return tasks_service.create(conn, id=tid, title=tid, created_by="human")


def test_idle_claims_do_not_consume_concurrency(conn):
    """两个没有 run 的 claim 不应占满 max_parallel=2，第三个 claim 仍可成功。"""
    for tid in ("T-A", "T-B", "T-C"):
        _task(conn, tid)
    tasks_service.claim(conn, "T-A", "worker")
    tasks_service.claim(conn, "T-B", "worker")
    tasks_service.claim(conn, "T-C", "worker")
    assert tasks_service.get(conn, "T-C")["assignee"] == "worker"


def test_active_runs_consume_concurrency(conn):
    """活跃 run 才占并发额度：max_parallel=1 时，已有 run 的角色不能再 claim。"""
    roles_service.create(conn, slug="solo", display_name="Solo", definition={
        "runtime": {"id": "claude-code"},
        "concurrency": {"max_parallel": 1},
        "workspace": {"repos": []},
    })
    _task(conn, "T-RUN")
    _task(conn, "T-NEXT")
    runs_service.create_run(conn, "solo", "T-RUN")
    with pytest.raises(ConflictError, match="concurrency limit"):
        tasks_service.claim(conn, "T-NEXT", "solo")


def test_assigned_roles_include_single_assignee(conn):
    """用 --assignee 创建/预分配的子任务，调度器也要能派发给该角色。"""
    from anvil.services import governance

    _task(conn, "T-CHILD")
    conn.execute("UPDATE tasks SET assignee='worker' WHERE id='T-CHILD'")
    conn.commit()
    assert governance._assigned_roles(conn, "T-CHILD") == ["worker"]


def test_claim_error_explains_exclusive_owner(conn):
    """被其他角色独占 claim 时，错误信息要给出 unclaim / team assignee 的解法。"""
    roles_service.create(conn, slug="other", display_name="Other", definition={
        "runtime": {"id": "claude-code"},
        "workspace": {"repos": []},
    })
    _task(conn, "T-OWN")
    tasks_service.claim(conn, "T-OWN", "worker")
    with pytest.raises(ConflictError, match="exclusively claimed by worker"):
        tasks_service.claim(conn, "T-OWN", "other")
