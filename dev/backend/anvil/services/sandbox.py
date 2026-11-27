"""沙箱实例的单写者（M0）。

只负责生命周期记录与后端选择；策略、作业、审批在后续里程碑扩展。
DB 表缺失时不阻塞执行（记不到实例而已），避免一个观测表把角色跑挂。
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from typing import Any

from .. import config
from ..db import open_conn
from ..util import now_iso
from ..sandbox import capabilities as sandbox_capabilities
from ..sandbox import monitor as sandbox_monitor
from ..sandbox.backends import make_backend

logger = logging.getLogger("anvil.sandbox")
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
    root_dir: str | None = None,
    rss_limit_mib: int | None = None,
    pids_limit: int | None = None,
    disk_limit_mib: int | None = None,
    workspace_path: str | None = None,
    instance_id: str | None = None,
) -> str:
    """插入一条 starting 实例；返回 id，表缺失时返回空串。"""
    iid = instance_id or new_instance_id()
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
            " capabilities_json, status, generation, root_dir, rss_limit_mib, "
            " pids_limit, disk_limit_mib, workspace_path, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, scope_kind, scope_id, role_slug, backend, tier, profile,
             json.dumps(capabilities or {}, ensure_ascii=False), "starting",
             generation, root_dir, rss_limit_mib, pids_limit, disk_limit_mib,
             workspace_path, now, now),
        )
        try:
            from . import ledger
            ledger.emit_audit(
                conn, role_slug=role_slug, task_id=None,
                action="sandbox.instance.spawned",
                payload={"instance_id": iid, "backend": backend, "tier": tier,
                         "profile": profile, "scope_kind": scope_kind,
                         "scope_id": scope_id})
        except Exception:  # noqa: BLE001 —— 审计失败不阻断执行
            pass
        conn.commit()
    except sqlite3.OperationalError:
        return ""
    return iid


def bind_instance(conn, instance_id: str, *, pid: int | None,
                  native_handle: str | None, root_dir: str | None = None) -> None:
    if not instance_id:
        return
    try:
        conn.execute(
            "UPDATE sandbox_instances SET pid=?, native_handle=?, status='running', "
            "root_dir=COALESCE(?, root_dir), updated_at=? WHERE id=?",
            (pid, native_handle, root_dir, now_iso(), instance_id))
        conn.commit()
    except sqlite3.OperationalError:
        pass


def finish_instance(conn, instance_id: str, *, exit_reason: str | None = None) -> None:
    if not instance_id:
        return
    now = now_iso()
    try:
        row = conn.execute(
            "SELECT role_slug, backend, profile FROM sandbox_instances WHERE id=?",
            (instance_id,)).fetchone()
        cur = conn.execute(
            "UPDATE sandbox_instances SET status='exited', exit_reason=?, "
            "ended_at=?, updated_at=? WHERE id=? AND status IN "
            "('starting','running','stopping')",
            (exit_reason, now, now, instance_id))
        if cur.rowcount and row is not None:
            try:
                from . import ledger
                ledger.emit_audit(
                    conn, role_slug=row["role_slug"], task_id=None,
                    action="sandbox.instance.exited",
                    payload={"instance_id": instance_id,
                             "backend": row["backend"], "profile": row["profile"],
                             "exit_reason": exit_reason})
            except Exception:  # noqa: BLE001
                pass
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


def get_backend_by_name(name: str | None) -> Any:
    current = get_backend()
    if not name or name == getattr(current, "name", None):
        return current
    if name == "host":
        from ..sandbox.backends.host import HostBackend

        return HostBackend()
    return current


# ---- 状态 / 自检 -----------------------------------------------------------

def status(conn=None) -> dict:
    backend = get_backend()
    try:
        caps = backend.capabilities().as_dict()
    except Exception:  # noqa: BLE001
        caps = {}
    substrates = sandbox_capabilities.substrates()
    warnings: list[str] = []
    if not config.SANDBOX_ENABLED:
        warnings.append("sandbox_disabled")
    if config.SANDBOX_MODE == "host-unsafe":
        warnings.append("host_unsafe")
    if config.SANDBOX_MODE == "enforce" and backend.name == "host":
        warnings.append("standard_degraded")
    if not substrates.get("docker") and not substrates.get("wsl"):
        warnings.append("strict_unavailable")
    counts: dict = {}
    if conn is not None:
        try:
            for state in ("starting", "running", "exited", "stale", "killed"):
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM sandbox_instances WHERE status=?",
                    (state,)).fetchone()
                counts[state] = int(row["n"] or 0) if row else 0
        except sqlite3.OperationalError:
            pass
    return {
        "mode": config.SANDBOX_MODE,
        "enabled": bool(config.SANDBOX_ENABLED),
        "standard": {
            "available": True,
            "backend": backend.name,
            "capabilities": caps,
        },
        "strict": {
            "available": False,
            "backend": None,
            "detail": "strict tier is not implemented in v1.60 M1",
        },
        "substrates": substrates,
        "warnings": warnings,
        "counts": counts,
    }


def self_check() -> dict:
    backend = get_backend()
    try:
        caps = backend.capabilities().as_dict()
    except Exception:  # noqa: BLE001
        caps = {}
    checks: dict = {"backend": backend.name, "capabilities": caps}
    ok = True
    if backend.name == "seatbelt":
        try:
            from ..sandbox.backends.seatbelt import _preflight
            checks["profile_preflight"] = bool(_preflight("(version 1)(allow default)"))
        except Exception:  # noqa: BLE001
            checks["profile_preflight"] = False
        ok = ok and bool(checks["profile_preflight"])
    if config.SANDBOX_MODE == "host-unsafe":
        ok = False
        checks["host_unsafe"] = True
    return {"ok": ok, "checks": checks, "warnings": status().get("warnings", [])}


def run_view(conn, run: dict) -> dict:
    instance_id = run.get("sandbox_instance_id")
    row = None
    if instance_id:
        try:
            row = conn.execute(
                "SELECT * FROM sandbox_instances WHERE id=?", (instance_id,)).fetchone()
        except sqlite3.OperationalError:
            row = None
    inst = dict(row) if row is not None else None
    return {
        "run_id": run.get("id"),
        "instance_id": instance_id,
        "instance": inst,
        "backend": inst.get("backend") if inst else None,
        "profile": inst.get("profile") if inst else None,
        "network": "host",
        "capabilities": json.loads(inst.get("capabilities_json") or "{}") if inst else {},
        "peak": json.loads(inst.get("peak_json") or "{}") if inst else {},
        "exit_reason": inst.get("exit_reason") if inst else None,
    }


# ---- 资源 supervisor -------------------------------------------------------

_supervisor_thread: threading.Thread | None = None
_supervisor_stop = threading.Event()
_last_disk_check: dict[str, float] = {}


def start_supervisor() -> None:
    global _supervisor_thread
    if not config.SANDBOX_ENABLED:
        return
    if _supervisor_thread is not None and _supervisor_thread.is_alive():
        return
    _supervisor_stop.clear()
    _supervisor_thread = threading.Thread(
        target=_supervisor_loop, name="anvil-sandbox-supervisor", daemon=True)
    _supervisor_thread.start()


def stop_supervisor() -> None:
    _supervisor_stop.set()
    thread = _supervisor_thread
    if thread is not None:
        thread.join(timeout=2)


def _supervisor_loop() -> None:
    while not _supervisor_stop.wait(5):
        try:
            conn = open_conn(config.SQLITE_PATH)
        except Exception:  # noqa: BLE001
            continue
        try:
            sample_active(conn)
        except Exception:  # noqa: BLE001 —— 监控线程不能崩
            logger.debug("sandbox supervisor tick failed", exc_info=True)
        finally:
            conn.close()


def sample_active(conn, *, now: float | None = None) -> int:
    """采样活跃实例；超限杀树、标记 killed、失败对应 run。返回 breach 数。"""
    now = now or time.time()
    try:
        rows = conn.execute(
            "SELECT * FROM sandbox_instances WHERE status='running' AND pid IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    breaches = 0
    for row in rows:
        backend = get_backend_by_name(row["backend"])
        try:
            caps = backend.capabilities()
        except Exception:  # noqa: BLE001
            caps = None
        if caps is not None and caps.resource_caps:
            continue  # Job Object / 容器已有硬限
        pid = int(row["pid"] or 0)
        if pid <= 0:
            continue
        sample = sandbox_monitor.sample_tree(pid)
        if not sample:
            continue
        disk = None
        disk_limit = int(row["disk_limit_mib"] or 0)
        root = row["workspace_path"] or row["root_dir"]
        if disk_limit and root:
            last = _last_disk_check.get(row["id"], 0)
            if now - last >= 60:
                disk = sandbox_monitor.disk_mib(root)
                _last_disk_check[row["id"]] = now
        peak = _merge_peak(row["peak_json"], sample, disk)
        reason = None
        if int(row["rss_limit_mib"] or 0) and sample["rss_mib"] > int(row["rss_limit_mib"]):
            reason = "resource_rss"
        elif int(row["pids_limit"] or 0) and sample["pids"] > int(row["pids_limit"]):
            reason = "resource_pids"
        elif disk_limit and disk is not None and disk > disk_limit:
            reason = "resource_disk"
        conn.execute(
            "UPDATE sandbox_instances SET peak_json=?, updated_at=? WHERE id=?",
            (peak, now_iso(), row["id"]))
        if reason:
            breaches += 1
            _kill_breached(conn, row, reason, peak, sample, disk)
    conn.commit()
    return breaches


def _merge_peak(raw: str | None, sample: dict, disk: int | None) -> str:
    try:
        peak = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        peak = {}
    peak["rss_mib"] = max(int(peak.get("rss_mib") or 0), int(sample.get("rss_mib") or 0))
    peak["pids"] = max(int(peak.get("pids") or 0), int(sample.get("pids") or 0))
    if disk is not None:
        peak["disk_mib"] = max(int(peak.get("disk_mib") or 0), int(disk))
    peak["sampled_at"] = now_iso()
    return json.dumps(peak, ensure_ascii=False)


def _kill_breached(conn, row, reason: str, peak: str,
                   sample: dict, disk: int | None) -> None:
    _kill_pid(row)
    now = now_iso()
    conn.execute(
        "UPDATE sandbox_instances SET status='killed', exit_reason=?, peak_json=?, "
        "ended_at=?, updated_at=? WHERE id=? AND status='running'",
        (reason, peak, now, now, row["id"]))
    run = None
    try:
        from . import runs as runs_service
        run = conn.execute(
            "SELECT id, task_id FROM runs WHERE sandbox_instance_id=? "
            "AND status IN ('created','projecting','running','suspended') "
            "ORDER BY started_at DESC LIMIT 1", (row["id"],)).fetchone()
        if run is not None:
            runs_service.set_status(
                conn, run["id"], "failed", ended_at=now,
                failure_class=f"sandbox_{reason}",
                from_statuses=runs_service.ACTIVE_RUN_STATUSES)
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import ledger
        ledger.emit_audit(
            conn, role_slug=row["role_slug"],
            task_id=run["task_id"] if run is not None else None,
            action="sandbox.instance.breached",
            payload={"instance_id": row["id"], "reason": reason,
                     "sample": sample, "disk_mib": disk,
                     "run_id": run["id"] if run is not None else None},
        )
    except Exception:  # noqa: BLE001
        pass


def _kill_pid(row) -> None:
    pid = int(row["pid"] or 0)
    if pid <= 0:
        return
    backend_name = row["backend"]
    if backend_name in ("host", "seatbelt"):
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        return
    try:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001
        pass
