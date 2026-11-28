"""切片验收逐项判定（v1.62 #9）。

- 引擎检查（test / command / file_assert）的 verdict 来自 verify 绿跑证据：
  通过 → ``met``，合法红 / 平凡红 → ``unmet``，harness 错误 / 未跑完 → ``unknown``。
- manual 项由 decide 判定：只有 decide 提交的 verdict 才生效，未提交为 ``unknown``。
- 任何 ``unknown`` 都不允许 decide 通过；``met / unmet / n.a.`` 与证据引用随
  决策记录与评审包持久化。
"""
from __future__ import annotations

import sqlite3

from ..util import jloads, now_iso
from . import slice_spec

MANUAL_VERDICTS = ("met", "unmet", "n.a.")
MANUAL_RATIO_LIMIT = 0.30
_ENGINE = "engine"
_MANUAL = "manual"


class VerdictError(ValueError):
    """非法 verdict 或对引擎 verdict 的越权覆盖。"""


def manual_ratio(spec: dict) -> dict:
    """manual 验收项占比；超过 30% 时该片 decide authority 切为 human。"""
    items = [item for item in (spec or {}).get("acceptance") or []
             if isinstance(item, dict)]
    total = len(items)
    manual = sum(1 for item in items if item.get("kind") == "manual")
    ratio = round(manual / total, 4) if total else 0.0
    return {
        "manual": manual,
        "total": total,
        "ratio": ratio,
        "authority": "human" if ratio > MANUAL_RATIO_LIMIT else "ai",
    }


def _scope_step(conn: sqlite3.Connection, workflow_id: str,
                slice_key: str) -> dict | None:
    row = conn.execute(
        "SELECT step_id, slice_spec_json FROM workflow_steps "
        "WHERE workflow_id=? AND slice_key=? AND step_id LIKE '%.scope' "
        "AND slice_spec_json IS NOT NULL ORDER BY rowid LIMIT 1",
        (workflow_id, slice_key)).fetchone()
    return dict(row) if row is not None else None


def slice_spec_for(conn: sqlite3.Connection, workflow_id: str,
                   step: dict) -> tuple[dict, dict] | None:
    """返回决策步骤所属切片的 (scope 步骤, 切片 spec)；没有则 None。"""
    slice_key = str(step.get("slice_key") or "").strip()
    if not slice_key:
        return None
    scope = _scope_step(conn, workflow_id, slice_key)
    if scope is None:
        return None
    spec = jloads(scope.get("slice_spec_json") or "{}", {})
    if not isinstance(spec, dict) or not spec:
        return None
    return scope, spec


def _engine_checks(scope_step_id: str, spec: dict) -> list[dict]:
    checks: list[dict] = []
    for item in slice_spec.engine_checks(spec):
        checks.append({
            "id": item["id"],
            "kind": item["kind"],
            "criterion": item["criterion"],
            "check_id": f"{scope_step_id}.{item['id']}",
        })
    return checks


def _green_runs(conn: sqlite3.Connection, task_id: str, workflow_id: str,
                slice_key: str) -> dict[str, dict]:
    """每个检查 id 的最新绿跑作业（按创建顺序取最新）。"""
    rows = conn.execute(
        "SELECT id, status, spec_json, evidence_json, finished_at "
        "FROM sandbox_jobs WHERE task_id=? AND kind='check' "
        "ORDER BY created_at DESC, rowid DESC", (str(task_id),)).fetchall()
    latest: dict[str, dict] = {}
    for row in rows:
        try:
            spec = jloads(row["spec_json"] or "{}", {})
        except (TypeError, ValueError):
            continue
        ref = spec.get("green_ref") if isinstance(spec, dict) else None
        if not isinstance(ref, dict):
            continue
        if (str(ref.get("workflow_id") or "") != str(workflow_id)
                or str(ref.get("slice_key") or "") != str(slice_key)):
            continue
        check_id = str(ref.get("check_id") or "")
        if not check_id or check_id in latest:
            continue
        latest[check_id] = {
            "id": row["id"],
            "status": row["status"],
            "check": (jloads(row["evidence_json"] or "{}", {}) or {}).get("check") or {},
            "finished_at": row["finished_at"],
        }
    return latest


def _green_outcome(job: dict | None) -> str:
    if job is None:
        return "unknown"
    status = str(job.get("status") or "")
    check = job.get("check") or {}
    classification = check.get("classification")
    if status in ("queued", "running", "waiting_approval"):
        return "unknown"
    if classification == "passed" or status == "succeeded":
        return "met"
    if classification in ("valid_red", "trivial_red"):
        return "unmet"
    # harness_error、sandbox 崩溃、取消等都无法证明目标行为满足。
    return "unknown"


def normalize_manual_verdicts(manual_items: list[dict],
                              raw) -> dict[str, dict]:
    """校验 decide 提交的 manual 判定；不允许覆盖引擎 verdict。"""
    allowed = {str(item["id"]) for item in manual_items}
    out: dict[str, dict] = {}
    if raw is None:
        return out
    if not isinstance(raw, list):
        raise VerdictError("verdicts must be a list")
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise VerdictError(f"verdicts[{index}] must be an object")
        verdict_id = str(item.get("id") or "").strip()
        if not verdict_id:
            raise VerdictError(f"verdicts[{index}].id is required")
        status = str(item.get("status") or "").strip().lower()
        if status not in MANUAL_VERDICTS:
            raise VerdictError(
                f"verdicts[{index}].status must be one of {MANUAL_VERDICTS}")
        if verdict_id not in allowed:
            raise VerdictError(
                f"verdict for {verdict_id!r} is not a manual acceptance item; "
                "engine verdicts are engine-owned and cannot be overridden")
        refs = [str(ref) for ref in item.get("evidence_refs") or []
                if str(ref).strip()]
        out[verdict_id] = {"status": status, "evidence_refs": refs}
    return out


def build_verdicts(conn: sqlite3.Connection, wf: dict, step: dict,
                   manual_verdicts=None) -> list[dict] | None:
    """逐项判定；没有切片 spec 时返回 None（沿用普通 acceptance 逻辑）。"""
    found = slice_spec_for(conn, wf["id"], step)
    if found is None:
        return None
    scope, spec = found
    manual_items = slice_spec.manual_items(spec)
    supplied = normalize_manual_verdicts(manual_items, manual_verdicts)
    runs = _green_runs(conn, str(wf["task_id"]), wf["id"],
                       str(step.get("slice_key") or ""))
    verdicts: list[dict] = []
    for check in _engine_checks(scope["step_id"], spec):
        status = _green_outcome(runs.get(check["check_id"]))
        verdicts.append({
            "id": check["id"],
            "kind": check["kind"],
            "criterion": check["criterion"],
            "status": status,
            "source": _ENGINE,
            "evidence_refs": ([f"check:{check['check_id']}"]
                              if status != "unknown" else []),
        })
    for item in manual_items:
        got = supplied.get(str(item["id"]))
        verdicts.append({
            "id": item["id"],
            "kind": "manual",
            "criterion": item["criterion"],
            "status": got["status"] if got else "unknown",
            "source": _MANUAL,
            "evidence_refs": got["evidence_refs"] if got else [],
        })
    return verdicts


def blocking_verdicts(verdicts: list[dict] | None, *, approve: bool) -> str | None:
    """返回阻止通过的 ConflictError 文案；不阻止时 None。"""
    if not approve or not verdicts:
        return None
    unknown = [str(v["id"]) for v in verdicts if v.get("status") == "unknown"]
    if unknown:
        return ("verdict_unknown: acceptance item(s) still unknown: "
                + ", ".join(unknown))
    unmet = [str(v["id"]) for v in verdicts if v.get("status") == "unmet"]
    if unmet:
        return "acceptance_unmet: " + ", ".join(unmet)
    return None


def engine_evidence(conn: sqlite3.Connection, wf: dict,
                    step: dict) -> list[dict]:
    """评审包里的绿跑证据与 manual 占比说明。"""
    found = slice_spec_for(conn, wf["id"], step)
    if found is None:
        return []
    scope, spec = found
    slice_key = str(step.get("slice_key") or "")
    runs = _green_runs(conn, str(wf["task_id"]), wf["id"], slice_key)
    out: list[dict] = []
    kind_label = {"test": "test", "command": "command", "file_assert": "artifact"}
    for check in _engine_checks(scope["step_id"], spec):
        job = runs.get(check["check_id"])
        if job is None:
            continue
        evidence_kind = kind_label.get(check["kind"], "note")
        outcome = _green_outcome(job)
        provenance = (job.get("check") or {}).get("classification") or job.get("status")
        out.append({
            "id": f"check:{check['check_id']}",
            "kind": evidence_kind,
            "ref": job["id"],
            "label": f"{provenance} · {check['criterion']}"[:240],
            "digest": None,
            "captured_at": job.get("finished_at") or now_iso(),
            "fresh": True,
        })
    summary = manual_ratio(spec)
    if summary["total"]:
        percent = round(summary["ratio"] * 100)
        out.append({
            "id": "engine:manual_ratio",
            "kind": "note",
            "ref": "slice_lock.manual",
            "label": (f"manual {summary['manual']}/{summary['total']} "
                      f"({percent}%) · decide authority {summary['authority']}")[:240],
            "digest": None,
            "captured_at": now_iso(),
            "fresh": True,
        })
    return out
