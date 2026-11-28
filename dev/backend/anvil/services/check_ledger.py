"""检查台账与指标报告（v1.62 #12）。

引擎的每次检查运行（红跑 / 绿跑 / 反事实）与 decide 的逐项 verdict 都写入
``check_ledger`` 结构化台账；台账只由引擎与服务端写入，agent 不能覆盖。报告按
``GLOSSARY.md`` 的定义复算指标，绝对数与比例同时输出。

指标定义（与 GLOSSARY.md / ADR-0005 一致）：

- N_total：scope 锁定时登记、能够锁定的全部验收项（test / command / file_assert
  / manual），是 harness 错误红占比与可机械化占比的分母。
- N_qualified：在 base 上得到合法红（``valid_red``）的检查数；剔除 harness 错误
  与平凡的文件缺失红。
- 有效红占比：N_qualified 中在 final 上为绿（绿跑 ``passed``）的数量 ÷ N_qualified。
  签名匹配由锁定集合在 execute 阶段强制，绿跑通过即签名匹配。
- harness 错误红占比：首次红跑判为 harness 错误的检查数 ÷ N_total。
- 可机械化占比：test / command / file_assert 检查数 ÷ N_total，按 kind 分列。
- 假通过输入：decide 放行的切片数与 7 天窗口内人工审计出的缺陷切片数；有人工
  审计记录时输出假通过率，否则只输出输入（``rate=None``）。
- 人工返工：human 发起的 reject / reopen / retry 次数。
- human authority 片占比：decide authority=human 的切片 ÷ 全部切片。
- replan / 弱化：从通用审计 / 事件记录读取（#10 后续写入同一形态即可）；
  没有记录时为 0。

试点执行本身不在本票范围：报告只复算台账与审计记录里已有的事实。
"""
from __future__ import annotations

import json
import sqlite3

from ..util import jdumps, jloads, now_iso

RECORD_RUN = "run"
RECORD_VERDICT = "verdict"
RECORD_AUDIT = "audit"

# GLOSSARY 术语到报告字段的映射，便于报告自解释定义。
DEFINITIONS = {
    "n_total": "检查总数：scope 锁定时登记、能够锁定的全部检查",
    "n_qualified": "合格检查：在 base 上得到合法红的检查，剔除 harness 错误与平凡红",
    "valid_red": "有效红：合格检查在 final 上转为通过且签名匹配 ÷ 合格检查数",
    "harness_error_red": "harness 错误红：首次运行判为 harness 错误的检查 ÷ N_total",
    "mechanizable": "可机械化：test / command / file_assert 检查 ÷ N_total，按 kind 分列",
    "false_pass": "假通过：decide 放行后 7 天内可追溯到 scope_paths 的缺陷切片 ÷ decide 放行切片",
    "human_rework": "人工返工：human 发起的 reject / reopen / retry 次数",
    "human_authority_slices": "human authority 片：decide authority=human 的切片 ÷ 全部切片",
    "replan": "replan 与弱化：显式 replan 次数及其中弱化 diff 的次数",
}


def _ratio(numerator: int | float, denominator: int | float) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _row_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    for key, default in (("junit_summary_json", {}), ("failure_types_json", []),
                         ("evidence_refs_json", []), ("detail_json", {})):
        parsed_key = key[:-5]
        data[parsed_key] = jloads(data.pop(key, None) or jdumps(default), default)
    return data


# ---- writes ---------------------------------------------------------------

def _insert(conn: sqlite3.Connection, fields: dict) -> None:
    columns = list(fields)
    placeholders = ",".join("?" for _ in columns)
    conn.execute(
        f"INSERT OR IGNORE INTO check_ledger ({','.join(columns)}) "
        f"VALUES ({placeholders})",
        [fields[column] for column in columns])


def _workflow_info(conn: sqlite3.Connection, workflow_id: str | None) -> dict:
    if not workflow_id:
        return {}
    row = conn.execute(
        "SELECT task_id, execution_no FROM workflows WHERE id=?",
        (str(workflow_id),)).fetchone()
    return dict(row) if row is not None else {}


def _scope_step_for(conn: sqlite3.Connection, workflow_id: str | None,
                    slice_key: str | None) -> str | None:
    if not workflow_id or not slice_key:
        return None
    row = conn.execute(
        "SELECT step_id FROM workflow_steps WHERE workflow_id=? AND slice_key=? "
        "AND step_id LIKE '%.scope' ORDER BY rowid LIMIT 1",
        (str(workflow_id), str(slice_key))).fetchone()
    return str(row["step_id"]) if row is not None else None


def _slice_key_for_step(conn: sqlite3.Connection, workflow_id: str | None,
                        step_id: str | None) -> str | None:
    if not workflow_id or not step_id:
        return None
    row = conn.execute(
        "SELECT slice_key FROM workflow_steps WHERE workflow_id=? AND step_id=?",
        (str(workflow_id), str(step_id))).fetchone()
    return str(row["slice_key"]) if row is not None and row["slice_key"] else None


def _run_refs(spec: dict) -> tuple[str, dict, dict]:
    """返回 (source, ref, workflow_info 需要的字段)。"""
    green_ref = spec.get("green_ref")
    if isinstance(green_ref, dict) and green_ref:
        return "green", green_ref, {
            "workflow_id": green_ref.get("workflow_id"),
            "slice_key": green_ref.get("slice_key"),
            "scope_step_id": green_ref.get("scope_step_id"),
        }
    counterfactual = spec.get("counterfactual")
    if isinstance(counterfactual, dict) and counterfactual:
        return "counterfactual", counterfactual, {
            "workflow_id": counterfactual.get("workflow_id"),
            "slice_key": counterfactual.get("slice_key"),
            "scope_step_id": None,
        }
    lock_ref = spec.get("lock_ref")
    if isinstance(lock_ref, dict) and lock_ref:
        return "red", lock_ref, {
            "workflow_id": lock_ref.get("workflow_id"),
            "slice_key": None,
            "scope_step_id": lock_ref.get("step_id"),
        }
    return "standalone", {}, {"workflow_id": None, "slice_key": None,
                              "scope_step_id": None}


def record_check_run(conn: sqlite3.Connection, job_row: sqlite3.Row,
                     execution) -> None:
    """引擎检查作业落库后写入一条结构化运行台账（幂等，按 run_id 去重）。"""
    spec = jloads(job_row["spec_json"] or "{}", {})
    if not isinstance(spec, dict):
        spec = {}
    source, _ref, refs = _run_refs(spec)
    workflow_id = refs.get("workflow_id")
    slice_key = refs.get("slice_key")
    scope_step_id = refs.get("scope_step_id")
    if workflow_id and not scope_step_id:
        scope_step_id = _scope_step_for(conn, workflow_id, slice_key)
    if workflow_id and not slice_key:
        slice_key = _slice_key_for_step(conn, workflow_id, scope_step_id)
    info = _workflow_info(conn, workflow_id)
    evidence = getattr(execution, "evidence", None) or {}
    check = evidence.get("check") if isinstance(evidence, dict) else None
    check = check if isinstance(check, dict) else {}
    spec_check = spec.get("check") or {}
    classification = check.get("classification")
    cause = check.get("cause")
    if not check and execution.status != "succeeded":
        # 作业在取证前失败（沙箱崩溃 / 取消）：按 harness 错误记录，不能记成合法红。
        classification = "harness_error"
        cause = cause or "sandbox_error"
    fields = {
        "record_type": RECORD_RUN,
        "task_id": job_row["task_id"],
        "workflow_id": workflow_id,
        "execution_no": info.get("execution_no"),
        "slice_key": slice_key,
        "step_id": scope_step_id,
        "scope_step_id": scope_step_id,
        "check_id": check.get("check_id") or spec_check.get("id"),
        "kind": check.get("kind") or spec_check.get("kind"),
        "source": source,
        "command": check.get("command"),
        "exit_code": check.get("exit_code"),
        "signal": check.get("signal"),
        "timed_out": 1 if check.get("timed_out") else 0,
        "red_exit_code": check.get("red_exit_code"),
        "collected_count": check.get("collected_count"),
        "collected_baseline": check.get("collected_baseline"),
        "junit_summary_json": jdumps(check.get("junit_summary") or {}),
        "failure_types_json": jdumps(check.get("failure_types") or []),
        "classification": classification,
        "cause": cause,
        "env_hash": check.get("env_hash"),
        "dependency_digest": check.get("dependency_digest"),
        "run_id": getattr(execution, "job_id", None) or check.get("run_id"),
        "created_at": now_iso(),
    }
    _insert(conn, fields)


def record_failed_run(conn: sqlite3.Connection, job_row: sqlite3.Row,
                      reason: str) -> None:
    """作业未能执行（沙箱崩溃 / 取消）：记为 harness 错误，保留可复算字段。"""
    spec = jloads(job_row["spec_json"] or "{}", {})
    if not isinstance(spec, dict):
        spec = {}
    source, _ref, refs = _run_refs(spec)
    workflow_id = refs.get("workflow_id")
    slice_key = refs.get("slice_key")
    scope_step_id = refs.get("scope_step_id")
    if workflow_id and not scope_step_id:
        scope_step_id = _scope_step_for(conn, workflow_id, slice_key)
    if workflow_id and not slice_key:
        slice_key = _slice_key_for_step(conn, workflow_id, scope_step_id)
    info = _workflow_info(conn, workflow_id)
    spec_check = spec.get("check") or {}
    _insert(conn, {
        "record_type": RECORD_RUN,
        "task_id": job_row["task_id"],
        "workflow_id": workflow_id,
        "execution_no": info.get("execution_no"),
        "slice_key": slice_key,
        "step_id": scope_step_id,
        "scope_step_id": scope_step_id,
        "check_id": spec_check.get("id"),
        "kind": spec_check.get("kind"),
        "source": source,
        "classification": "harness_error",
        "cause": str(reason or "sandbox_error")[:200],
        "run_id": job_row["id"],
        "created_at": now_iso(),
    })


def record_verdicts(conn: sqlite3.Connection, *, wf: dict, step: dict,
                    decision_id: str, verdicts: list[dict]) -> None:
    """decide 的逐项 verdict 写入台账；引擎与 manual 来源都记录。"""
    info = _workflow_info(conn, wf.get("id"))
    for verdict in verdicts or []:
        if not isinstance(verdict, dict):
            continue
        _insert(conn, {
            "record_type": RECORD_VERDICT,
            "task_id": wf.get("task_id"),
            "workflow_id": wf.get("id"),
            "execution_no": info.get("execution_no")
            or wf.get("execution_no"),
            "slice_key": step.get("slice_key"),
            "step_id": step.get("step_id"),
            "check_id": verdict.get("id"),
            "kind": verdict.get("kind"),
            "source": verdict.get("source"),
            "verdict_status": verdict.get("status"),
            "evidence_refs_json": jdumps(verdict.get("evidence_refs") or []),
            "decision_id": decision_id,
            "created_at": now_iso(),
        })


def record_pilot_audit(conn: sqlite3.Connection, *, workflow_id: str,
                       task_id: str | None = None, slice_key: str | None = None,
                       defect: bool = False,
                       scope_paths: list[str] | None = None,
                       audited_by: str | None = None,
                       within_7d: bool = True) -> None:
    """试点人工审计结论写入同一台账，供假通过率复算（阶段二使用）。"""
    detail = {
        "defect": bool(defect),
        "scope_paths": list(scope_paths or []),
        "audited_by": audited_by,
        "within_7d": bool(within_7d),
    }
    _insert(conn, {
        "record_type": RECORD_AUDIT,
        "task_id": task_id,
        "workflow_id": workflow_id,
        "slice_key": slice_key,
        "source": "human" if audited_by else "audit",
        "detail_json": jdumps(detail),
        "created_at": now_iso(),
    })
    conn.commit()


# ---- reads ----------------------------------------------------------------

def rows(conn: sqlite3.Connection, *, workflow_id: str | None = None,
         task_id: str | None = None, record_type: str | None = None,
         limit: int = 2000) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if workflow_id:
        clauses.append("workflow_id=?")
        params.append(workflow_id)
    if task_id:
        clauses.append("task_id=?")
        params.append(task_id)
    if record_type:
        clauses.append("record_type=?")
        params.append(record_type)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    result = conn.execute(
        f"SELECT * FROM check_ledger{where} ORDER BY id LIMIT ?",
        (*params, max(1, min(int(limit or 2000), 5000)))).fetchall()
    return [_row_dict(row) for row in result]


def _slices(conn: sqlite3.Connection, workflow_id: str) -> list[dict]:
    rows_ = conn.execute(
        "SELECT step_id, slice_key, slice_spec_json, slice_lock_json "
        "FROM workflow_steps WHERE workflow_id=? AND step_id LIKE '%.scope' "
        "AND slice_spec_json IS NOT NULL ORDER BY rowid", (workflow_id,)).fetchall()
    slices: list[dict] = []
    for row in rows_:
        spec = jloads(row["slice_spec_json"] or "{}", {})
        lock = jloads(row["slice_lock_json"] or "{}", {})
        acceptance = [item for item in (spec.get("acceptance") or [])
                      if isinstance(item, dict)]
        slices.append({
            "slice_key": row["slice_key"],
            "scope_step_id": row["step_id"],
            "acceptance": acceptance,
            "manual": lock.get("manual") or {},
        })
    return slices


def _first_red_runs(conn: sqlite3.Connection, workflow_id: str) -> dict[str, dict]:
    """每个 check_id 的首次红跑（base）。"""
    first: dict[str, dict] = {}
    for row in conn.execute(
            "SELECT * FROM check_ledger WHERE workflow_id=? AND record_type='run' "
            "AND source='red' ORDER BY id", (workflow_id,)).fetchall():
        data = _row_dict(row)
        check_id = str(data.get("check_id") or "")
        if check_id and check_id not in first:
            first[check_id] = data
    return first


def _latest_green_runs(conn: sqlite3.Connection,
                       workflow_id: str) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for row in conn.execute(
            "SELECT * FROM check_ledger WHERE workflow_id=? AND record_type='run' "
            "AND source='green' ORDER BY id", (workflow_id,)).fetchall():
        data = _row_dict(row)
        check_id = str(data.get("check_id") or "")
        if check_id:
            latest[check_id] = data
    return latest


def _false_pass_inputs(conn: sqlite3.Connection, workflow_id: str,
                       task_id: str | None) -> dict:
    passed = conn.execute(
        "SELECT DISTINCT s.slice_key FROM decisions d "
        "JOIN workflow_steps s ON s.workflow_id=d.workflow_id "
        "AND s.step_id=d.step_id "
        "WHERE d.workflow_id=? AND d.decision='approve' "
        "AND s.kind='decision' AND s.slice_key IS NOT NULL",
        (workflow_id,)).fetchall()
    passed_slices = {str(row["slice_key"]) for row in passed}

    audit_rows = rows(conn, workflow_id=workflow_id, record_type=RECORD_AUDIT)
    defect_slices: set[str] = set()
    for row in audit_rows:
        detail = row.get("detail") or {}
        if detail.get("defect") and detail.get("within_7d", True):
            if row.get("slice_key"):
                defect_slices.add(str(row["slice_key"]))
    audited = bool(audit_rows)
    denominator = len(passed_slices)
    rate = (_ratio(len(defect_slices), denominator)
            if audited and denominator else None)
    return {
        "decide_passed_slices": denominator,
        "audited_defect_slices": len(defect_slices),
        "audited": audited,
        "rate": rate,
    }


def _human_rework(conn: sqlite3.Connection, workflow_id: str,
                  task_id: str | None) -> dict:
    rejects = conn.execute(
        "SELECT COUNT(*) AS n FROM decisions WHERE workflow_id=? "
        "AND decider_type='human' AND decision='reject'",
        (workflow_id,)).fetchone()["n"]
    retries = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE workflow_id=? "
        "AND event_type='step.retried'", (workflow_id,)).fetchone()["n"]
    reopens = 0
    if task_id:
        reopens = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE task_id=? "
            "AND event_type='audit.reopen'", (task_id,)).fetchone()["n"]
    total = int(rejects or 0) + int(retries or 0) + int(reopens or 0)
    return {
        "human_rejects": int(rejects or 0),
        "retries": int(retries or 0),
        "reopens": int(reopens or 0),
        "count": total,
    }


def _human_authority(conn: sqlite3.Connection, workflow_id: str,
                     slices: list[dict]) -> dict:
    total = len(slices)
    human = 0
    for item in slices:
        authority = (item.get("manual") or {}).get("authority")
        if not authority:
            row = conn.execute(
                "SELECT decision_authority FROM workflow_steps "
                "WHERE workflow_id=? AND slice_key=? AND kind='decision' "
                "ORDER BY rowid LIMIT 1",
                (workflow_id, item["slice_key"])).fetchone()
            authority = row["decision_authority"] if row is not None else None
        if authority == "human":
            human += 1
    return {"human": human, "total": total, "ratio": _ratio(human, total)}


def _replan(conn: sqlite3.Connection, workflow_id: str,
            slices: list[dict]) -> dict:
    """从通用事件 / 决策记录读取 replan 与弱化（#10 后续写入同一形态即可）。"""
    records: list[dict] = []
    for row in conn.execute(
            "SELECT event_type, slice_key, payload_json FROM events "
            "WHERE workflow_id=? AND event_type LIKE '%replan%'",
            (workflow_id,)).fetchall():
        payload = jloads(row["payload_json"] or "{}", {})
        records.append({
            "slice_key": row["slice_key"] or (payload or {}).get("slice_key"),
            "weakening": bool((payload or {}).get("weakening")
                              or (payload or {}).get("weakens")
                              or "weak" in str(row["event_type"]).lower()),
        })
    for row in conn.execute(
            "SELECT step_id, reasons_json FROM decisions "
            "WHERE workflow_id=? AND decision='replan'",
            (workflow_id,)).fetchall():
        reasons = jloads(row["reasons_json"] or "[]", [])
        records.append({
            "slice_key": _slice_key_for_step(conn, workflow_id, row["step_id"]),
            "weakening": "weakening" in [str(r) for r in reasons or []],
        })
    replan_count = len(records)
    weakening = sum(1 for record in records if record["weakening"])
    slice_keys = {str(record["slice_key"]) for record in records
                  if record.get("slice_key")}
    total_slices = len(slices)
    return {
        "count": replan_count,
        "weakening": weakening,
        "weakening_ratio": _ratio(weakening, replan_count),
        "replan_slices": len(slice_keys),
        "slice_ratio": _ratio(len(slice_keys), total_slices),
    }


def _mechanizable(slices: list[dict]) -> dict:
    kinds = {"test": 0, "command": 0, "file_assert": 0, "manual": 0}
    for item in slices:
        for acceptance in item["acceptance"]:
            kind = str(acceptance.get("kind") or "")
            if kind in kinds:
                kinds[kind] += 1
    total = sum(kinds.values())
    mechanical = kinds["test"] + kinds["command"] + kinds["file_assert"]
    result = {kind: {"count": count, "ratio": _ratio(count, total)}
              for kind, count in kinds.items()}
    result["count"] = mechanical
    result["ratio"] = _ratio(mechanical, total)
    return result


def _check_summary(row: dict) -> dict:
    return {
        "check_id": row.get("check_id"),
        "kind": row.get("kind"),
        "source": row.get("source"),
        "classification": row.get("classification"),
        "cause": row.get("cause"),
        "run_id": row.get("run_id"),
        "env_hash": row.get("env_hash"),
        "collected_count": row.get("collected_count"),
        "collected_baseline": row.get("collected_baseline"),
    }


def report(conn: sqlite3.Connection, workflow_id: str) -> dict:
    """从台账与决策 / 审计记录复算指标报告；阈值判定不在本票范围。"""
    wf = conn.execute(
        "SELECT id, task_id, execution_no FROM workflows WHERE id=?",
        (workflow_id,)).fetchone()
    if wf is None:
        raise LookupError(workflow_id)
    task_id = wf["task_id"]
    slices = _slices(conn, workflow_id)
    first_red = _first_red_runs(conn, workflow_id)
    green = _latest_green_runs(conn, workflow_id)
    mechanizable = _mechanizable(slices)
    n_total = sum(kind["count"] for kind in mechanizable.values()
                  if isinstance(kind, dict))

    qualified = [check_id for check_id, row in first_red.items()
                 if row.get("classification") == "valid_red"]
    valid = [check_id for check_id in qualified
             if (green.get(check_id) or {}).get("classification") == "passed"]
    harness = [check_id for check_id, row in first_red.items()
               if row.get("classification") == "harness_error"]

    return {
        "workflow_id": workflow_id,
        "task_id": task_id,
        "definitions": DEFINITIONS,
        "n_total": n_total,
        "n_qualified": len(qualified),
        "valid_red": {
            "count": len(valid),
            "qualified": len(qualified),
            "ratio": _ratio(len(valid), len(qualified)),
        },
        "harness_error_red": {
            "count": len(harness),
            "total": n_total,
            "ratio": _ratio(len(harness), n_total),
        },
        "mechanizable": mechanizable,
        "false_pass": _false_pass_inputs(conn, workflow_id, task_id),
        "human_rework": _human_rework(conn, workflow_id, task_id),
        "human_authority_slices": _human_authority(conn, workflow_id, slices),
        "replan": _replan(conn, workflow_id, slices),
        "slices": [{
            "slice_key": item["slice_key"],
            "scope_step_id": item["scope_step_id"],
            "acceptance_items": len(item["acceptance"]),
            "mechanizable_items": len([
                a for a in item["acceptance"]
                if a.get("kind") in ("test", "command", "file_assert")]),
            "decide_authority": (item.get("manual") or {}).get("authority", "ai"),
        } for item in slices],
        "checks": [_check_summary(row)
                   for row in rows(conn, workflow_id=workflow_id,
                                   record_type=RECORD_RUN)],
    }
