"""P0.4 子任务统一投影：delegation 与 dispatch 走同一条 child 列表。

行为差异（阻塞/验收 vs 非阻塞/回切）保留在各自服务中；投影只做归一化，
供 API 与 UI 共用一套字段和状态词表。
"""
from __future__ import annotations

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import child_tasks
from anvil.services import dispatch
from anvil.services import tasks as tasks_service
from anvil.util import now_iso

pytest.importorskip("anvil.graph")


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "AGENTS_DIR", data / "agents")
    from anvil.services import daemon as daemon_service
    monkeypatch.setattr(daemon_service, "get_manager", lambda: None)
    conn = connect(str(tmp_path / "t.db"))
    now = now_iso()
    for slug in ("owner", "worker"):
        conn.execute(
            "INSERT INTO roles (slug,display_name,home_path,definition_json,"
            "created_at,updated_at,enabled) VALUES (?,?,?,'{}',?,?,1)",
            (slug, slug.upper(), str(data / "agents" / slug), now, now))
    conn.commit()
    yield conn
    conn.close()


def _insert_delegation(conn, source_id: str, *, child_id: str = "C-DEL",
                       status: str = "in_review") -> None:
    now = now_iso()
    conn.execute(
        "INSERT INTO tasks (id,title,status,type,parent_id,parent_step_id,assignee,"
        "created_by,acceptance_json,scope_paths_json,due_at,created_at,updated_at) "
        "VALUES (?,?,?,'delegation',?, 'S1','worker',"
        "'agent:owner','[\"PoC 可复现\"]','[\"poc/\"]','2026-12-01T00:00:00+00:00',?,?)",
        (child_id, "构造 PoC", status, source_id, now, now))
    conn.commit()


def _create_dispatch(conn, source_id: str):
    now = now_iso()
    conn.execute(
        "INSERT INTO role_sessions "
        "(role_slug,session_id,status,current_task_id,current_context_key,"
        " current_slice_key,updated_at) VALUES ('worker','sid','busy',?,?,?,?)",
        (source_id, f"{source_id}::slice::A", "A", now))
    conn.commit()
    return dispatch.create_dispatch(
        conn, source_task_id=source_id, target_role="worker",
        message="独立处理缓存问题", requester="owner",
        source_slice_key="A", acceptance=["问题复现"],
        scope_paths=["src/cache"], reason_code="explicit")


def test_list_children_unifies_delegation_and_dispatch(env):
    conn = env
    source = tasks_service.create(
        conn, title="来源任务", assignee="owner", assignees=["owner", "worker"],
        repo="original-repo")
    _insert_delegation(conn, source["id"])
    result = _create_dispatch(conn, source["id"])

    out = child_tasks.list_children(conn, source["id"])

    assert out["counts"] == {"total": 2, "delegation": 1, "dispatch": 1}
    items = out["items"]
    assert len(items) == 2
    by_type = {item["relation_type"]: item for item in items}

    delegation = by_type["delegation"]
    assert delegation["task_id"] == "C-DEL"
    assert delegation["relation_id"] is None
    assert delegation["title"] == "构造 PoC"
    assert delegation["status"] == "in_review"
    assert delegation["blocking"] is True
    assert delegation["assignee"] == "worker"
    assert delegation["created_by"] == "agent:owner"
    assert delegation["step_title"] is None
    assert delegation["acceptance"] == ["PoC 可复现"]
    assert delegation["scope_paths"] == ["poc/"]
    assert delegation["dispatch_status"] is None
    assert delegation["return_pending"] is False

    dispatched = by_type["dispatch"]
    assert dispatched["task_id"] == result["task"]["id"]
    assert dispatched["relation_id"] == result["dispatch"]["id"]
    assert dispatched["title"] == result["task"]["title"]
    assert dispatched["status"] == "todo"
    assert dispatched["blocking"] is False
    assert dispatched["assignee"] == "worker"
    assert dispatched["acceptance"] == ["问题复现"]
    assert dispatched["scope_paths"] == ["src/cache"]
    assert dispatched["dispatch_status"] == "open"
    assert dispatched["return_pending"] is False
    assert dispatched["return_context_key"]
    assert dispatched["run_status"] in ("created", "projecting", "running", "suspended")


def test_list_children_marks_return_pending(env):
    conn = env
    source = tasks_service.create(
        conn, title="来源任务", assignee="owner", assignees=["owner", "worker"])
    result = _create_dispatch(conn, source["id"])
    conn.execute(
        "UPDATE task_dispatches SET status='return_ready' WHERE id=?",
        (result["dispatch"]["id"],))
    conn.commit()

    out = child_tasks.list_children(conn, source["id"])
    assert out["items"][0]["dispatch_status"] == "return_ready"
    assert out["items"][0]["return_pending"] is True


def test_list_children_matches_legacy_lists(env):
    """统一投影不改变旧接口的成员范围：委派列表不含 dispatch。"""
    conn = env
    source = tasks_service.create(
        conn, title="来源任务", assignee="owner", assignees=["owner", "worker"])
    _insert_delegation(conn, source["id"])
    _create_dispatch(conn, source["id"])

    delegations = tasks_service.list_delegations(conn, source["id"])
    dispatches = dispatch.list_dispatches(conn, source["id"])
    out = child_tasks.list_children(conn, source["id"])

    assert {item["task_id"] for item in out["items"]} == (
        {d["id"] for d in delegations} | {d["target_task_id"] for d in dispatches})
    assert len(out["items"]) == len(delegations) + len(dispatches)
