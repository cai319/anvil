"""沙箱实例的单写者（M0）。

只负责生命周期记录与后端选择；策略、作业、审批在后续里程碑扩展。
DB 表缺失时不阻塞执行（记不到实例而已），避免一个观测表把角色跑挂。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .. import config
from ..db import open_conn
from ..util import now_iso
from ..sandbox.backends import make_backend

_backend: Any = None


def get_backend() -> Any:
    global _backend
    if _backend is None:
        _backend = make_backend()
    return _backend


def backend_name() -> str:
    return str(getattr(get_backend(), "name", "host"))


def reset_for_tests(backend: Any = None) -> None:
    """测试注入 FakeBackend；生产不使用。"""
    global _backend
    _backend = backend


def new_instance_id() -> str:
    return "sbi_" + uuid.uuid4().hex


def create_instance(
    conn,
    *,
    role_slug: str | None,
    scope_kind: str,
    scope_id: str,
    backend: str,
    tier: str = "standard",
    profile: str = "standard",
    capabilities: dict | None = None,
) -> str:
    """插入一条 starting 实例；返回 id，表缺失时返回空串。"""
    iid = new_instance_id()
    now = now_iso()
    generation = 1
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(generation), 0) + 1 AS g FROM sandbox_instances "
            "WHERE scope_kind=? AND scope_id=?", (scope_kind, scope_id)).fetchone()
        if row is not None:
            generation = int(row["g"] or 1)
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "INSERT INTO sandbox_instances "
            "(id, scope_kind, scope_id, role_slug, backend, tier, profile, "
            " capabilities_json, status, generation, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, scope_kind, scope_id, role_slug, backend, tier, profile,
             json.dumps(capabilities or {}, ensure_ascii=False), "starting",
             generation, now, now),
        )
        conn.commit()
    except sqlite3.OperationalError:
        return ""
    return iid


def bind_instance(conn, instance_id: str, *, pid: int | None,
                  native_handle: str | None) -> None:
    if not instance_id:
        return
    try:
        conn.execute(
            "UPDATE sandbox_instances SET pid=?, native_handle=?, status='running', "
            "updated_at=? WHERE id=?",
            (pid, native_handle, now_iso(), instance_id))
        conn.commit()
    except sqlite3.OperationalError:
        pass


def finish_instance(conn, instance_id: str, *, exit_reason: str | None = None) -> None:
    if not instance_id:
        return
    now = now_iso()
    try:
        conn.execute(
            "UPDATE sandbox_instances SET status='exited', exit_reason=?, "
            "ended_at=?, updated_at=? WHERE id=? AND status IN "
            "('starting','running','stopping')",
            (exit_reason, now, now, instance_id))
        conn.commit()
    except sqlite3.OperationalError:
        pass


def finish_instance_standalone(instance_id: str, *, exit_reason: str | None = None) -> None:
    if not instance_id:
        return
    try:
        conn = open_conn(config.SQLITE_PATH)
    except Exception:  # noqa: BLE001 —— 观测失败不影响执行
        return
    try:
        finish_instance(conn, instance_id, exit_reason=exit_reason)
    finally:
        conn.close()


def record_oneshot(*, native_session_id: str | None, pid: int | None,
                   backend: str | None = None) -> str:
    """one-shot 进程的实例记录；任一失败都返回空串，不阻断 spawn。"""
    if not native_session_id:
        return ""
    try:
        conn = open_conn(config.SQLITE_PATH)
    except Exception:  # noqa: BLE001
        return ""
    try:
        iid = create_instance(
            conn,
            role_slug=None,
            scope_kind="one_shot",
            scope_id=native_session_id,
            backend=backend or backend_name(),
            capabilities=_safe_capabilities(),
        )
        bind_instance(conn, iid, pid=pid, native_handle=native_session_id)
        return iid
    finally:
        conn.close()


def reconcile_startup(conn) -> None:
    """启动自愈：上一进程留下的活跃实例一律标 stale。"""
    now = now_iso()
    try:
        conn.execute(
            "UPDATE sandbox_instances SET status='stale', ended_at=?, updated_at=? "
            "WHERE status IN ('starting','running','stopping')", (now, now))
        conn.commit()
    except sqlite3.OperationalError:
        pass


def terminate_process(proc, *, graceful: bool = True) -> None:
    get_backend().kill(proc, graceful=graceful)


def _safe_capabilities() -> dict:
    try:
        return get_backend().capabilities().as_dict()
    except Exception:  # noqa: BLE001
        return {}
