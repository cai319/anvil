"""沙箱实例的单写者（M0）。

只负责生命周期记录与后端选择；策略、作业、审批在后续里程碑扩展。
DB 表缺失时不阻塞执行（记不到实例而已），避免一个观测表把角色跑挂。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .. import config
from ..db import open_conn
from ..util import jloads, now_iso
from ..sandbox import capabilities as sandbox_capabilities
from ..sandbox import jobs as sandbox_jobs
from ..sandbox import monitor as sandbox_monitor
from ..sandbox.backends import make_backend
from ..sandbox.backends import docker as docker_backend
from ..sandbox.backends import strict as strict_backend
from ..sandbox.errors import QuotaExceeded, SandboxUnavailable

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


def spawn_stdio(conn, *, executable: str, args: list[str], cwd: str,
                env: dict[str, str], role_slug: str | None = None,
                scope_id: str | None = None, policy=None):
    """按标准档启动一个 stdio 子进程（MCP 探针）；返回 SandboxProcess。"""
    from ..sandbox import env as sandbox_env
    from ..sandbox import instances as sandbox_instances
    from ..sandbox.ports import SpawnRequest

    backend = get_backend()
    iid = new_instance_id()
    root_dir = ""
    if config.SANDBOX_ENABLED and config.SANDBOX_MODE != "host-unsafe":
        try:
            root_dir = str(sandbox_instances.prepare(role_slug or "probes", iid))
            home, tmp = sandbox_env.prepare_home(cwd, cwd, root_dir)
            env = sandbox_env.apply_standard_env(env, home=home, tmp=tmp)
        except Exception:  # noqa: BLE001 —— 环境重定向失败不阻断探针
            pass
    try:
        caps = backend.capabilities().as_dict()
    except Exception:  # noqa: BLE001
        caps = {}
    instance_id = create_instance(
        conn, role_slug=role_slug, scope_kind="mcp_probe",
        scope_id=scope_id or executable, backend=getattr(backend, "name", "host"),
        capabilities=caps, root_dir=root_dir or None,
        rss_limit_mib=512, pids_limit=64, workspace_path=cwd, instance_id=iid)
    req = SpawnRequest(
        executable=executable, args=args, cwd=cwd, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        instance_id=instance_id or None, instance_dir=root_dir or None,
        workspace_path=cwd, role_slug=role_slug, policy=policy)
    try:
        sp = backend.spawn_stdio(req)
    except Exception:
        if instance_id:
            try:
                finish_instance(conn, instance_id, exit_reason="spawn_error")
            except Exception:  # noqa: BLE001
                pass
        raise
    if instance_id:
        try:
            bind_instance(conn, instance_id, pid=sp.popen.pid,
                          native_handle=sp.native_handle,
                          root_dir=root_dir or None)
        except Exception:  # noqa: BLE001
            pass
    return sp


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
    strict_runner, strict_ok, strict_detail = strict_backend.select_runner()
    if not strict_ok:
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
            "available": bool(strict_ok),
            "backend": strict_runner.name if strict_ok else None,
            "substrate": strict_runner.name,
            "detail": strict_detail,
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


# ---- 严格档作业（M2a） -----------------------------------------------------

def strict_available() -> tuple[bool, str]:
    _runner, ok, detail = strict_backend.select_runner()
    return ok, detail


def provision_strict(conn, *, consent: bool) -> dict:
    """Windows WSL 一次性准备（human 显式同意，root 配置，记审计）。"""
    if not consent:
        raise ValueError("provisioning requires explicit consent")
    runner, _ok, _detail = strict_backend.select_runner()
    if getattr(runner, "name", None) != "wsl_bwrap":
        raise SandboxUnavailable("provisioning is only required for Windows WSL")
    try:
        result = runner.provision()
    except RuntimeError as exc:
        raise SandboxUnavailable(str(exc)) from exc
    try:
        from . import ledger
        ledger.emit_audit(
            conn, role_slug="human", task_id=None, action="sandbox.provision",
            payload={"ok": result.get("ok"), "distro": result.get("distro"),
                     "returncode": result.get("returncode"),
                     "log_tail": str(result.get("log") or "")[-1000:]})
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
    return result


def _job_dict(row) -> dict | None:
    if row is None:
        return None
    job = dict(row)
    job["spec"] = jloads(job.get("spec_json") or "{}", {})
    job["evidence"] = jloads(job.get("evidence_json") or "{}", {})
    return job


def create_job(conn, *, spec_raw: dict, actor_role: str | None,
               actor_is_human: bool, check_available: bool = True) -> dict:
    spec = sandbox_jobs.parse_spec(spec_raw)
    if not actor_is_human and not spec.get("task_id") and not spec.get("finding_id"):
        raise PermissionError("agent PoC jobs must be bound to a task or finding")
    task_id = spec.get("task_id")
    if spec.get("finding_id"):
        try:
            row = conn.execute(
                "SELECT id, task_id FROM task_findings WHERE id=?",
                (spec["finding_id"],)).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is None:
            raise LookupError("finding not found")
        if task_id and task_id != row["task_id"]:
            raise LookupError("finding does not belong to the task")
        task_id = task_id or row["task_id"]
        spec["task_id"] = task_id
    if task_id:
        row = conn.execute(
            "SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise LookupError("task not found")
    if check_available:
        ok, detail = strict_available()
        if not ok:
            raise SandboxUnavailable(detail)
    _check_quota(conn, task_id=task_id, actor_role=actor_role)
    job_id = "poc_" + uuid.uuid4().hex
    now = now_iso()
    status = "queued"
    approval = "not_required"
    if not actor_is_human and spec["profile"] != "safe":
        status = "waiting_approval"
        approval = "pending"
    conn.execute(
        "INSERT INTO sandbox_jobs "
        "(id, kind, task_id, finding_id, created_by, created_by_type, tier, "
        " profile, spec_json, spec_hash, status, approval_status, timeout_seconds, "
        " created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, "poc", task_id, spec.get("finding_id"),
         actor_role or "human", "human" if actor_is_human else "role",
         "strict", spec["profile"], json.dumps(spec, ensure_ascii=False),
         sandbox_jobs.spec_hash(spec), status, approval,
         int(spec["resources"]["timeout_seconds"]), now, now),
    )
    try:
        from . import ledger
        ledger.emit_audit(
            conn, role_slug=actor_role or "human", task_id=spec.get("task_id"),
            action="sandbox.job.created",
            payload={"job_id": job_id, "profile": spec["profile"],
                     "network": spec["network"],
                     "finding_id": spec.get("finding_id")})
    except Exception:  # noqa: BLE001
        pass
    conn.commit()
    return _job_dict(conn.execute(
        "SELECT * FROM sandbox_jobs WHERE id=?", (job_id,)).fetchone())  # type: ignore[return-value]


def _check_quota(conn, *, task_id: str | None, actor_role: str | None) -> None:
    running_limit, queued_limit = 2, 20

    def _count(clause: str, value) -> tuple[int, int]:
        row = conn.execute(
            "SELECT "
            "SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running, "
            "SUM(CASE WHEN status IN ('queued','waiting_approval') THEN 1 ELSE 0 END) AS queued "
            f"FROM sandbox_jobs WHERE {clause}", (value,)).fetchone()
        return int(row["running"] or 0), int(row["queued"] or 0)

    checks = []
    if task_id:
        checks.append(("task", *_count("task_id=?", task_id)))
    if actor_role and actor_role != "human":
        checks.append(("role", *_count("created_by=?", actor_role)))
    for scope, running, queued in checks:
        if running >= running_limit:
            raise QuotaExceeded(f"{scope} has {running} running PoC jobs (limit {running_limit})")
        if queued >= queued_limit:
            raise QuotaExceeded(f"{scope} has {queued} queued PoC jobs (limit {queued_limit})")


def approve_job(conn, job_id: str, *, actor_role: str = "human",
                note: str | None = None) -> dict:
    row = conn.execute(
        "SELECT status FROM sandbox_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise LookupError("job not found")
    if row["status"] != "waiting_approval":
        raise ValueError("job is not waiting for approval")
    now = now_iso()
    cur = conn.execute(
        "UPDATE sandbox_jobs SET status='queued', approval_status='approved', "
        "approved_by=?, approved_at=?, approval_note=?, updated_at=? "
        "WHERE id=? AND status='waiting_approval'",
        (actor_role, now, note, now, job_id))
    if not cur.rowcount:
        raise ValueError("job is not waiting for approval")
    try:
        from . import ledger
        ledger.emit_audit(
            conn, role_slug=actor_role, task_id=None,
            action="sandbox.job.approved",
            payload={"job_id": job_id, "note": note})
    except Exception:  # noqa: BLE001
        pass
    conn.commit()
    return get_job(conn, job_id)  # type: ignore[return-value]


def reject_job(conn, job_id: str, *, actor_role: str = "human",
               note: str | None = None) -> dict:
    row = conn.execute(
        "SELECT status FROM sandbox_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise LookupError("job not found")
    if row["status"] != "waiting_approval":
        raise ValueError("job is not waiting for approval")
    now = now_iso()
    conn.execute(
        "UPDATE sandbox_jobs SET status='rejected', approval_status='rejected', "
        "approved_by=?, approved_at=?, approval_note=?, finished_at=?, updated_at=? "
        "WHERE id=? AND status='waiting_approval'",
        (actor_role, now, note, now, now, job_id))
    try:
        from . import ledger
        ledger.emit_audit(
            conn, role_slug=actor_role, task_id=None,
            action="sandbox.job.rejected",
            payload={"job_id": job_id, "note": note})
    except Exception:  # noqa: BLE001
        pass
    conn.commit()
    return get_job(conn, job_id)  # type: ignore[return-value]


def list_jobs(conn, *, task_id: str | None = None,
              finding_id: str | None = None, status: str | None = None,
              limit: int = 100) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if task_id:
        clauses.append("task_id=?"); params.append(task_id)
    if finding_id:
        clauses.append("finding_id=?"); params.append(finding_id)
    if status:
        clauses.append("status=?"); params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM sandbox_jobs{where} ORDER BY created_at DESC LIMIT ?",
        (*params, max(1, min(int(limit or 100), 500)))).fetchall()
    return [_job_dict(r) for r in rows if r is not None]  # type: ignore[misc]


def get_job(conn, job_id: str) -> dict | None:
    return _job_dict(conn.execute(
        "SELECT * FROM sandbox_jobs WHERE id=?", (job_id,)).fetchone())


def claim_next_job(conn) -> dict | None:
    row = conn.execute(
        "SELECT * FROM sandbox_jobs WHERE status='queued' "
        "ORDER BY created_at, id LIMIT 1").fetchone()
    if row is None:
        return None
    native = sandbox_jobs.container_name(row["id"])
    now = now_iso()
    cur = conn.execute(
        "UPDATE sandbox_jobs SET status='running', started_at=?, updated_at=?, "
        "native_job_id=? WHERE id=? AND status='queued'",
        (now, now, native, row["id"]))
    conn.commit()
    if not cur.rowcount:
        return None
    return get_job(conn, row["id"])


def record_execution(conn, job_id: str, execution) -> bool:
    row = conn.execute(
        "SELECT status, task_id, created_by FROM sandbox_jobs WHERE id=?",
        (job_id,)).fetchone()
    if row is None or row["status"] != "running":
        return False  # 已取消/被接管；不得覆盖
    now = now_iso()
    conn.execute("DELETE FROM sandbox_job_phases WHERE job_id=?", (job_id,))
    for phase in execution.phases:
        conn.execute(
            "INSERT INTO sandbox_job_phases "
            "(job_id, seq, phase, name, cmd, status, exit_code, signal, "
            " timed_out, duration_ms, started_at, finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, phase["seq"], phase["phase"], phase["name"],
             phase["name"], phase["status"], phase.get("exit_code"),
             phase.get("signal"), 1 if phase.get("timed_out") else 0,
             phase.get("duration_ms"), execution.started_at, execution.finished_at))
    conn.execute("DELETE FROM sandbox_job_artifacts WHERE job_id=?", (job_id,))
    for artifact in execution.artifacts:
        conn.execute(
            "INSERT INTO sandbox_job_artifacts "
            "(job_id, rel_path, size_bytes, sha256, created_at) "
            "VALUES (?,?,?,?,?)",
            (job_id, artifact["rel_path"], artifact["size_bytes"],
             artifact["sha256"], now))
    conn.execute(
        "UPDATE sandbox_jobs SET status=?, finished_at=?, exit_code=?, signal=?, "
        "failure_reason=?, evidence_json=?, log_dir=?, artifact_dir=?, updated_at=? "
        "WHERE id=? AND status='running'",
        (execution.status, execution.finished_at, execution.exit_code,
         execution.signal, execution.failure_reason,
         json.dumps(execution.evidence, ensure_ascii=False),
         execution.log_dir, execution.artifact_dir, now, job_id))
    try:
        from . import ledger
        ledger.emit_audit(
            conn, role_slug=row["created_by"], task_id=row["task_id"],
            action=("sandbox.job.succeeded" if execution.status == "succeeded"
                    else "sandbox.job.failed"),
            payload={"job_id": job_id, "status": execution.status,
                     "failure_reason": execution.failure_reason,
                     "exit_code": execution.exit_code,
                     "signal": execution.signal})
    except Exception:  # noqa: BLE001
        pass
    conn.commit()
    return True


def fail_job(conn, job_id: str, reason: str) -> None:
    now = now_iso()
    row = conn.execute(
        "SELECT task_id, created_by FROM sandbox_jobs WHERE id=?",
        (job_id,)).fetchone()
    conn.execute(
        "UPDATE sandbox_jobs SET status='failed', failure_reason=?, "
        "finished_at=?, updated_at=? WHERE id=? AND status IN ('queued','running')",
        (reason[:500], now, now, job_id))
    try:
        from . import ledger
        ledger.emit_audit(
            conn, role_slug=row["created_by"] if row else None,
            task_id=row["task_id"] if row else None,
            action="sandbox.job.failed",
            payload={"job_id": job_id, "failure_reason": reason[:500]})
    except Exception:  # noqa: BLE001
        pass
    conn.commit()


def _kill_job_runner(job: dict) -> None:
    runner = strict_backend.select_runner()[0]
    if getattr(runner, "name", None) == "docker":
        docker_backend.kill_container(job.get("native_job_id"))
    elif hasattr(runner, "kill"):
        runner.kill(job.get("id"))


def cancel_job(conn, job_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM sandbox_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise LookupError("job not found")
    if row["status"] in ("queued", "waiting_approval"):
        now = now_iso()
        conn.execute(
            "UPDATE sandbox_jobs SET status='canceled', finished_at=?, updated_at=? "
            "WHERE id=? AND status IN ('queued','waiting_approval')",
            (now, now, job_id))
        conn.commit()
    elif row["status"] == "running":
        try:
            _kill_job_runner(dict(row))
        except Exception:  # noqa: BLE001 —— 取消失败仍要落状态
            pass
        now = now_iso()
        conn.execute(
            "UPDATE sandbox_jobs SET status='canceled', finished_at=?, "
            "failure_reason='canceled', updated_at=? WHERE id=? AND status='running'",
            (now, now, job_id))
        conn.commit()
    try:
        from . import ledger
        ledger.emit_audit(
            conn, role_slug=None, task_id=None, action="sandbox.job.canceled",
            payload={"job_id": job_id})
    except Exception:  # noqa: BLE001
        pass
    return get_job(conn, job_id)


def reconcile_jobs_startup(conn) -> None:
    now = now_iso()
    try:
        conn.execute(
            "UPDATE sandbox_jobs SET status='failed', "
            "failure_reason='controller_restart', finished_at=?, updated_at=? "
            "WHERE status='running'", (now, now))
        conn.commit()
    except sqlite3.OperationalError:
        pass


def job_view(conn, job_id: str) -> dict | None:
    job = get_job(conn, job_id)
    if job is None:
        return None
    job["phases"] = [dict(r) for r in conn.execute(
        "SELECT * FROM sandbox_job_phases WHERE job_id=? ORDER BY seq",
        (job_id,)).fetchall()]
    job["artifacts"] = [dict(r) for r in conn.execute(
        "SELECT * FROM sandbox_job_artifacts WHERE job_id=? ORDER BY rel_path",
        (job_id,)).fetchall()]
    return job


def job_log(conn, job_id: str, *, tail_bytes: int = 200_000) -> dict:
    job = get_job(conn, job_id)
    if job is None:
        raise LookupError("job not found")
    log_dir = job.get("log_dir")
    path = Path(log_dir) / "container.log" if log_dir else None
    if path is None or not path.exists():
        return {"job_id": job_id, "exists": False, "content": ""}
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - max(1, tail_bytes)))
        data = fh.read()
    return {"job_id": job_id, "exists": True,
            "truncated": size > len(data),
            "content": data.decode("utf-8", errors="replace")}


def job_artifacts(conn, job_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM sandbox_job_artifacts WHERE job_id=? ORDER BY rel_path",
        (job_id,)).fetchall()]


def cleanup_task_jobs(conn, task_id: str) -> int:
    """任务归档/删除：取消活跃作业并删除产物/日志目录（保留元数据与 evidence）。"""
    try:
        rows = conn.execute(
            "SELECT id, status FROM sandbox_jobs WHERE task_id=?", (task_id,)
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    removed = 0
    for row in rows:
        if row["status"] in ("queued", "waiting_approval", "running"):
            try:
                cancel_job(conn, row["id"])
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(config.SANDBOX_JOBS_DIR / row["id"], ignore_errors=True)
        conn.execute(
            "UPDATE sandbox_job_artifacts SET removed_at=? "
            "WHERE job_id=? AND removed_at IS NULL",
            (now_iso(), row["id"]))
        removed += 1
    conn.commit()
    return removed


def cleanup_orphans(conn) -> dict:
    """每日清理：过期作业目录、过期实例目录；保留 DB 元数据。"""
    now = now_iso()
    jobs = 0
    try:
        rows = conn.execute(
            "SELECT id, finished_at FROM sandbox_jobs "
            "WHERE status IN ('succeeded','failed','canceled','rejected') "
            "AND finished_at IS NOT NULL").fetchall()
        for row in rows:
            finished = str(row["finished_at"] or "")
            if _older_than_days(finished, config.SANDBOX_JOB_RETENTION_DAYS):
                shutil.rmtree(config.SANDBOX_JOBS_DIR / row["id"], ignore_errors=True)
                conn.execute(
                    "UPDATE sandbox_job_artifacts SET removed_at=? "
                    "WHERE job_id=? AND removed_at IS NULL", (now, row["id"]))
                jobs += 1
    except sqlite3.OperationalError:
        pass
    instances = 0
    try:
        rows = conn.execute(
            "SELECT id, role_slug, root_dir, ended_at FROM sandbox_instances "
            "WHERE status IN ('exited','killed','stale') AND ended_at IS NOT NULL"
        ).fetchall()
        for row in rows:
            if not _older_than_days(str(row["ended_at"] or ""),
                                    config.SANDBOX_INSTANCE_RETENTION_DAYS):
                continue
            root = row["root_dir"]
            if root:
                shutil.rmtree(root, ignore_errors=True)
            instances += 1
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return {"jobs": jobs, "instances": instances}


def _older_than_days(stamp: str, days: int) -> bool:
    if not stamp:
        return False
    try:
        from datetime import datetime, timedelta, timezone
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed < datetime.now(timezone.utc) - timedelta(days=max(0, days))
    except ValueError:
        return False


def artifact_file(conn, job_id: str, rel_path: str) -> Path:
    row = conn.execute(
        "SELECT * FROM sandbox_job_artifacts WHERE job_id=? AND rel_path=?",
        (job_id, rel_path)).fetchone()
    job = get_job(conn, job_id)
    if row is None or job is None or not job.get("artifact_dir"):
        raise LookupError("artifact not found")
    base = Path(job["artifact_dir"]).resolve()
    target = (base / rel_path).resolve()
    if base != target and base not in target.parents:
        raise LookupError("artifact not found")
    if target.is_symlink() or not target.is_file():
        raise LookupError("artifact not found")
    return target


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
