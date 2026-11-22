"""运行时子代理观测：跟踪、迟到结果与持久化。"""
from __future__ import annotations

import uuid

from ..db import open_conn
from ..util import now_iso
from .daemon_common import _Role, slice_key_of


class SubagentTrackerMixin:
    def _track_subagent(self, r: _Role, ev: dict, proc=None) -> None:
        """观测运行时 Task/Agent tool_use；Anvil 不主动创建子代理。

        记录完整父运行时归属（role/run/task + proc_generation/native_session_id +
        parent_tool_use_id + slice/step），但绝不参与调度决策。
        """
        msg = ev.get("message") or {}
        blocks = msg.get("content")
        if not isinstance(blocks, list):
            return
        with r.lock:
            if proc is not None and r.proc is not proc:
                return
            run_id = r.current_run_id
            task_id = r.current_task_id
            generation = int(r.proc_generation or 0)
            session_id = r.session_id
            step_id = r.current_step_id
            slice_key = slice_key_of(r.context_key) if r.current_task_id else None
        if not run_id:
            return
        parent_tool_use_id = str(ev.get("parent_tool_use_id") or "") or None
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = str(block.get("name") or "")
                inp = block.get("input") or {}
                if name not in ("Task", "Agent") and "subagent_type" not in inp:
                    continue
                tool_id = str(block.get("id") or "")
                if not tool_id:
                    continue
                info = {
                    "tool_use_id": tool_id,
                    "tool_name": name or "subagent",
                    "subagent_type": str(inp.get("subagent_type") or name or "subagent"),
                    "description": str(inp.get("description") or "")[:300],
                    "prompt_summary": str(inp.get("prompt") or "")[:800],
                    "status": "running",
                    "started_at": now_iso(),
                    "ended_at": None,
                    "result_summary": None,
                    "context_available": int(bool(parent_tool_use_id)),
                    "proc_generation": generation,
                    "native_session_id": session_id,
                    "parent_tool_use_id": parent_tool_use_id,
                    "slice_key": slice_key,
                    "step_id": step_id,
                }
                if self._subagent_terminal(run_id, tool_id):
                    # 重复/重放事件：DB 行已是终态，不能再塞回内存 active。
                    continue
                with r.lock:
                    r.active_subagents[tool_id] = info
                self._persist_subagent(r, run_id, task_id, info)
            elif block.get("type") == "tool_result":
                tool_id = str(block.get("tool_use_id") or "")
                with r.lock:
                    info = r.active_subagents.get(tool_id)
                if info is None:
                    # 晚到结果：内存已清（turn 结束/进程退出），回查 DB 做 CAS。
                    content = block.get("content")
                    if isinstance(content, list):
                        content = "\n".join(
                            str(x.get("text") or "")
                            for x in content if isinstance(x, dict))
                    self._late_subagent_result(
                        run_id, tool_id, str(content or "")[:1200],
                        generation=generation, session_id=session_id)
                    continue
                content = block.get("content")
                if isinstance(content, list):
                    content = "\n".join(
                        str(x.get("text") or "") for x in content if isinstance(x, dict))
                info = {**info, "status": "completed", "ended_at": now_iso(),
                        "result_summary": str(content or "")[:1200],
                        "context_available": int(bool(parent_tool_use_id)
                                                 or info.get("context_available"))}
                with r.lock:
                    r.active_subagents.pop(tool_id, None)
                self._persist_subagent(r, run_id, task_id, info)

    def _subagent_terminal(self, run_id: str, tool_id: str) -> bool:
        try:
            conn = open_conn(self.db_path)
            try:
                row = conn.execute(
                    "SELECT status FROM run_subagents WHERE run_id=? AND tool_use_id=?",
                    (run_id, tool_id)).fetchone()
                return row is not None and row["status"] in (
                    "completed", "interrupted")
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 —— 查询失败按未终态处理
            return False

    def _late_subagent_result(self, run_id: str, tool_id: str, content: str,
                              *, generation: int | None = None,
                              session_id: str | None = None) -> None:
        """tool_result 晚于内存清理到达：running → completed；interrupted 只补结果。

        只接受同一父运行时代次/会话的结果，避免旧会话/旧进程的迟到事件污染新记录。
        """
        try:
            conn = open_conn(self.db_path)
            try:
                now = now_iso()
                match = (
                    " AND (proc_generation IS NULL OR ? IS NULL OR proc_generation=?)"
                    " AND (native_session_id IS NULL OR ? IS NULL OR native_session_id=?)")
                params = (generation, generation, session_id, session_id)
                cur = conn.execute(
                    "UPDATE run_subagents SET status='completed', "
                    "ended_at=COALESCE(ended_at, ?), result_summary=?, updated_at=? "
                    "WHERE run_id=? AND tool_use_id=? AND status='running'" + match,
                    (now, content, now, run_id, tool_id, *params))
                if cur.rowcount == 0:
                    conn.execute(
                        "UPDATE run_subagents SET "
                        "result_summary=COALESCE(result_summary, ?), updated_at=? "
                        "WHERE run_id=? AND tool_use_id=? AND status='interrupted'"
                        + match,
                        (content, now, run_id, tool_id, *params))
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 —— 观测失败不影响角色执行
            pass

    def _finish_subagents(self, conn, r: _Role, run_id: str | None,
                          status: str = "interrupted",
                          reason: str = "turn_ended") -> None:
        """turn 结束：仍 running 的子代理随 turn 终止（不会跨 turn 存活）。"""
        if not run_id:
            return
        try:
            conn.execute(
                "UPDATE run_subagents SET status=?, interrupt_reason=?, "
                "ended_at=COALESCE(ended_at, ?), updated_at=? "
                "WHERE run_id=? AND status='running'",
                (status, None if status == "completed" else reason,
                 now_iso(), now_iso(), run_id))
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
        with r.lock:
            r.active_subagents.clear()

    def _persist_subagent(self, r: _Role, run_id: str, task_id: str | None,
                          info: dict) -> None:
        try:
            conn = open_conn(self.db_path)
            try:
                now = now_iso()
                run = conn.execute(
                    "SELECT native_session_id, slice_key, step_id FROM runs WHERE id=?",
                    (run_id,)).fetchone()
                conn.execute(
                    "INSERT INTO run_subagents "
                    "(id, run_id, role_slug, task_id, tool_use_id, tool_name, "
                    " description, prompt_summary, status, started_at, ended_at, "
                    " result_summary, context_available, subagent_type, "
                    " proc_generation, native_session_id, parent_tool_use_id, "
                    " slice_key, step_id, interrupt_reason, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(run_id, tool_use_id) DO UPDATE SET "
                    # 终态不可被重复 tool_use 重置；仅 running 行接受新状态。
                    "status=CASE WHEN run_subagents.status='running' "
                    "  THEN excluded.status ELSE run_subagents.status END, "
                    "ended_at=CASE WHEN run_subagents.status='running' "
                    "  THEN excluded.ended_at ELSE run_subagents.ended_at END, "
                    "result_summary=CASE WHEN run_subagents.status='running' "
                    "  THEN excluded.result_summary ELSE run_subagents.result_summary END, "
                    "interrupt_reason=CASE WHEN run_subagents.status='running' "
                    "  THEN excluded.interrupt_reason ELSE run_subagents.interrupt_reason END, "
                    "description=COALESCE(NULLIF(excluded.description,''), "
                    "  run_subagents.description), "
                    "prompt_summary=COALESCE(NULLIF(excluded.prompt_summary,''), "
                    "  run_subagents.prompt_summary), "
                    "subagent_type=COALESCE(excluded.subagent_type, "
                    "  run_subagents.subagent_type), "
                    "proc_generation=COALESCE(excluded.proc_generation, "
                    "  run_subagents.proc_generation), "
                    "native_session_id=COALESCE(excluded.native_session_id, "
                    "  run_subagents.native_session_id), "
                    "parent_tool_use_id=COALESCE(excluded.parent_tool_use_id, "
                    "  run_subagents.parent_tool_use_id), "
                    "slice_key=COALESCE(excluded.slice_key, run_subagents.slice_key), "
                    "step_id=COALESCE(excluded.step_id, run_subagents.step_id), "
                    "context_available=MAX(run_subagents.context_available, "
                    "  excluded.context_available), "
                    "updated_at=excluded.updated_at",
                    (uuid.uuid4().hex, run_id, r.role, task_id,
                     info["tool_use_id"], info.get("tool_name"),
                     info.get("description"), info.get("prompt_summary"),
                     info.get("status") or "running", info.get("started_at") or now,
                     info.get("ended_at"), info.get("result_summary"),
                     int(info.get("context_available") or 0),
                     info.get("subagent_type"),
                     info.get("proc_generation"),
                     info.get("native_session_id") or (
                         run["native_session_id"] if run else None),
                     info.get("parent_tool_use_id"),
                     info.get("slice_key") or (run["slice_key"] if run else None),
                     info.get("step_id") or (run["step_id"] if run else None),
                     info.get("interrupt_reason"), now))
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 —— 观测失败不影响角色执行
            pass

    def active_subagent_ids(self, run_id: str) -> set[str]:
        """该 run 在当前进程内仍存活的 tool_use id；Anvil 只用于观测收尾判定。"""
        with self._guard:
            roles = list(self._roles.values())
        for r in roles:
            with r.lock:
                if r.current_run_id == run_id:
                    return {
                        tid for tid, info in r.active_subagents.items()
                        if info.get("status") == "running"}
        return set()

    def _heal_subagents(self) -> None:
        """daemon 重启后不存在任何存活子代理：残留 running 统一标 interrupted。"""
        try:
            conn = open_conn(self.db_path)
            try:
                now = now_iso()
                conn.execute(
                    "UPDATE run_subagents SET status='interrupted', "
                    "interrupt_reason='daemon_restart', ended_at=COALESCE(ended_at, ?), "
                    "updated_at=? WHERE status='running'", (now, now))
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass
