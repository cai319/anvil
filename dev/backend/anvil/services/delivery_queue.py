"""投递队列：合并、上下文切换、发送与失败重试。"""
from __future__ import annotations

import time

from ..db import open_conn
from ..util import jdumps, now_iso
from . import ledger
from .daemon_common import (
    _DeliveryError, _Queued, _Role, _SpawnSpec, clear_session, context_key,
    context_parts, frame_user_message, logger, task_id_of,
)


class DeliveryQueueMixin:
    def ensure_and_deliver(self, conn, role: str, *, executable: str, args_new: list[str],
                           args_resume: list[str], ctx: dict, cwd: str, env: dict,
                           prompt: str, run_id: str | None = None,
                           task_id: str | None = None,
                           slice_key: str | None = None,
                           stage: str | None = None,
                           priority: int = 100,
                           model_pool: list[str] | None = None,
                           context_window_floor: int | None = None,
                           runtime_id: str | None = None,
                           workspace_path: str | None = None,
                           sandbox_policy: dict | None = None) -> dict:
        """确保角色进程存活（必要时 --resume/新建）并投递一条消息。

        消息按 task_id 进入角色队列；如果角色正忙（或前面排着别的任务），
        返回 `queued=True`，由调用方决定 run 的展示状态。真正投递时才决定
        是否切换上下文槽。
        """
        spec = _SpawnSpec(
            executable=executable, args_new=list(args_new), args_resume=list(args_resume),
            ctx=dict(ctx or {}), cwd=str(cwd), env=dict(env or {}),
            model_pool=[str(m).strip() for m in (model_pool or []) if str(m).strip()],
            context_window_floor=(int(context_window_floor)
                                  if context_window_floor not in (None, "", 0) else None),
            runtime_id=runtime_id, workspace_path=workspace_path,
            sandbox_policy=dict(sandbox_policy or {}),
            sandbox_policy_digest=(str(sandbox_policy.get("policy_digest"))
                                   if isinstance(sandbox_policy, dict)
                                   and sandbox_policy.get("policy_digest") else None),
        )
        r = self._role(role)
        with r.lock:
            self._deliver_locked(conn, r, _Queued(
                text=prompt, run_id=run_id, task_id=task_id, spec=spec,
                slice_key=slice_key, stage=stage, priority=priority,
                wake_note=self._run_wake_note(conn, run_id)))
            queued = any(item.run_id == run_id for item in r.queue) if run_id else bool(r.queue)
            run_status = None
            delivery_error = None
            if run_id:
                from . import runs as runs_service
                row = conn.execute(
                    "SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
                run_status = row["status"] if row else None
                # 投递失败/被门禁丢弃时，spawn_node 不能继续把 run 标成 running。
                if (not queued and run_status is not None
                        and not runs_service.is_dispatchable_status(run_status)):
                    delivery_error = f"run delivery failed (status={run_status})"
            return {
                "session_id": r.session_id,
                "pid": r.proc.pid if r.proc else None,
                "alive": bool(r.proc),
                "queued": queued,
                "queue_len": len(r.queue),
                "task_id": task_id,
                "run_status": run_status,
                "error": delivery_error,
            }

    def deliver(self, conn, role: str, prompt: str, run_id: str | None = None,
                task_id: str | None = None, slice_key: str | None = None,
                stage: str | None = None, priority: int = 100) -> bool:
        """向已存在的常驻进程投递消息（无进程返回 False，由调用方走 spawn 路径）。"""
        r = self._roles.get(role)
        if r is None or r.proc is None or r.proc.poll() is not None:
            return False
        with r.lock:
            spec = r.loaded_spec or _SpawnSpec(
                executable="", args_new=[], args_resume=[], ctx={}, cwd="", env={})
            self._deliver_locked(conn, r, _Queued(
                text=prompt, run_id=run_id, task_id=task_id, spec=spec,
                slice_key=slice_key, stage=stage, priority=priority,
                wake_note=self._run_wake_note(conn, run_id)))
            return True

    def cancel_task(self, role: str, task_id: str) -> bool:
        """任务删除：丢弃该任务的消息、删除其上下文槽。

        - 当前加载/正忙于此任务：杀进程、清内存上下文，然后处理剩余队列；
        - 加载/正忙其它任务：不动进程，只删该任务槽与队列项。
        返回是否终止了进程。
        """
        r = self._roles.get(role)
        conn = open_conn(self.db_path)
        try:
            kill = False
            if r is not None:
                with r.lock:
                    r.queue = [item for item in r.queue if item.task_id != task_id]
                    if task_id_of(r.context_key) == task_id:
                        r.suppress_exit = True
                        self._slot_persist(r, status="idle", conn=conn)
                        self._clear_current_locked(r)
                        self._kill_locked(r, graceful=True)
                        r.suppress_exit = False
                        self._slot_clear_memory(r)
                        r.cic_pending = False
                        kill = True
                        self._db_upsert(conn, r, status="idle")
                        if r.queue:
                            # 当前槽已删除；把队列里属于其它任务的消息继续处理。
                            self._flush_next_locked(conn, r)
            clear_session(conn, role, "task deleted", task_key_value=task_id,
                          all_slices=True)
            return kill
        finally:
            conn.close()

    def clear_run(self, role: str, run_id: str) -> bool:
        """run 进入终态后清掉角色上对应的 busy/current 状态。

        run 可能先被 scheduler/process_exit 标为 failed，而常驻进程仍在等待；
        此时必须主动终止该进程，否则 UI 会长期显示“进行中”，且 new run 无法投递。
        只清当前 run 精确匹配的状态，避免误伤已切到新 run 的进程。
        """
        r = self._roles.get(role)
        if r is None:
            # daemon 未加载该角色（服务重启/仅 API 模式）：至少清掉 DB 镜像里的
            # 幽灵 current_*，否则详情页会一直显示旧的 current_task/run。
            conn = open_conn(self.db_path)
            try:
                cur = conn.execute(
                    "UPDATE role_sessions SET status='dead', pid=NULL, busy_since=NULL, "
                    "current_task_id=NULL, current_run_id=NULL, current_step_id=NULL, "
                    "current_context_key=NULL, current_slice_key=NULL, current_stage=NULL, "
                    "updated_at=? WHERE role_slug=? AND current_run_id=?",
                    (now_iso(), role, run_id))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()
        with r.lock:
            if r.current_run_id != run_id:
                return False
            logger.warning(
                "clearing terminal run from role state role=%s run=%s", role, run_id)
            self._log(r.role, f"[anvil] clearing terminal run {run_id} from role state")
            self._slot_persist(r)
            r.suppress_exit = True
            try:
                if r.proc is not None and r.proc.poll() is None:
                    self._kill_locked(r, graceful=True)
            finally:
                r.suppress_exit = False
            self._clear_current_locked(r)
            conn = open_conn(self.db_path)
            try:
                self._db_upsert(conn, r, status="idle")
                # 失败 run 之后如果还有排队消息，继续处理下一条。
                if r.queue:
                    self._flush_next_locked(conn, r)
            finally:
                conn.close()
            return True

    def clear_current(self, role: str, run_id: str) -> bool:
        """清掉角色上指向 run 的当前绑定，但不要求 run 已终态。

        用于 reaper 把死进程的 running run 转 suspended 后，清除角色镜像里残留的
        current_task/current_run；不杀进程、不清队列。
        """
        r = self._roles.get(role)
        if r is None:
            conn = open_conn(self.db_path)
            try:
                cur = conn.execute(
                    "UPDATE role_sessions SET status='dead', busy_since=NULL, "
                    "current_task_id=NULL, current_run_id=NULL, current_step_id=NULL, "
                    "current_context_key=NULL, current_slice_key=NULL, current_stage=NULL, "
                    "updated_at=? WHERE role_slug=? AND current_run_id=?",
                    (now_iso(), role, run_id))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()
        with r.lock:
            if r.current_run_id != run_id:
                return False
            self._clear_current_locked(r)
            conn = open_conn(self.db_path)
            try:
                status = "idle" if (r.proc is not None and r.proc.poll() is None) else "dead"
                self._db_upsert(conn, r, status=status)
            finally:
                conn.close()
            return True

    def flush_queue(self, role: str) -> None:
        """重启后补投递角色队列（目标 run 已先 pump 时使用）。"""
        r = self._roles.get(role)
        if r is None:
            return
        with r.lock:
            if r.busy or not r.queue:
                return
            conn = open_conn(self.db_path)
            try:
                self._flush_next_locked(conn, r)
            finally:
                conn.close()

    def _deliver_locked(self, conn, r: _Role, item: _Queued) -> None:
        # 同一 run 只保留一条队列项：scheduler/mention/回切可能并发 pump 同一个
        # suspended run；重复投递会造成同一 turn 执行两次。新文本可能包含刚追加的
        # wake_note，用最新 text/spec 覆盖旧项。
        if item.run_id:
            for queued in r.queue:
                if queued.run_id == item.run_id:
                    queued.text = item.text
                    queued.spec = item.spec
                    queued.slice_key = item.slice_key
                    queued.stage = item.stage
                    queued.task_id = item.task_id
                    queued.wake_note = item.wake_note
                    queued.priority = min(int(queued.priority), int(item.priority))
                    r.queue.sort(key=lambda q: int(q.priority))
                    if not r.busy:
                        self._flush_next_locked(conn, r)
                    return
        r.queue.append(item)
        # 稳定排序：同 priority 内保持 FIFO；return/step 可以插到普通 mention 前。
        r.queue.sort(key=lambda q: int(q.priority))
        if not r.busy:
            self._flush_next_locked(conn, r)

    @staticmethod
    def _clear_current_locked(r: _Role) -> None:
        """清掉角色当前执行绑定；调用方必须持有 r.lock。"""
        r.busy = False
        r.busy_since = None
        r.current_run_id = None
        r.current_task_id = None
        r.current_step_id = None
        r.current_log_path = None
        r.active_subagents.clear()

    def _mark_idle_locked(self, conn, r: _Role) -> None:
        self._clear_current_locked(r)
        if r.context_key:
            self._slot_persist(r, status="idle", conn=conn)
        self._db_upsert(conn, r, status="idle")

    def _run_dispatchable(self, conn, run_id: str | None) -> bool:
        """队列项对应的 run 是否仍可投递。无 run_id 的角色级消息始终可投递。"""
        if not run_id:
            return True
        row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        from . import runs as runs_service
        return bool(row and runs_service.is_dispatchable_status(row["status"]))

    def _queue_drop(self, conn, r: _Role, item: _Queued, reason: str) -> None:
        self._log(
            r.role,
            f"[anvil] dropping queued message run={item.run_id} task={item.task_id}: {reason}",
        )
        self._audit_session(
            conn, r.role, item.task_id, "queue.dropped",
            {"run_id": item.run_id, "reason": reason},
        )

    def _delivery_failed(self, conn, r: _Role, item: _Queued, error: Exception) -> None:
        from . import runs as runs_service
        self._log(
            r.role,
            f"[anvil] delivery failed run={item.run_id} task={item.task_id}: {error}",
        )
        self._clear_current_locked(r)
        if item.run_id:
            runs_service.set_status(
                conn, item.run_id, "failed", ended_at=now_iso(),
                failure_class="delivery_error",
                from_statuses=runs_service.ACTIVE_RUN_STATUSES,
            )
        self._audit_session(
            conn, r.role, item.task_id, "queue.delivery_failed",
            {"run_id": item.run_id, "error": str(error)[:500]},
        )
        if r.proc is not None and r.proc.poll() is None:
            r.suppress_exit = True
            try:
                self._kill_locked(r, graceful=True)
            finally:
                r.suppress_exit = False
        self._db_upsert(conn, r, status="dead", error=str(error)[:500])

    def _flush_next_locked(self, conn, r: _Role) -> None:
        """FIFO 处理队列；跳过已终态/不可投递 run，单条投递失败不阻塞后续消息。"""
        while r.queue:
            item = r.queue.pop(0)
            if not self._run_dispatchable(conn, item.run_id):
                self._queue_drop(conn, r, item, "run is terminal or not dispatchable")
                continue
            target_key = context_key(item.task_id, item.slice_key, stage=item.stage)
            same_context = (
                r.proc is not None
                and r.proc.poll() is None
                and r.context_key == target_key
                and r.sandbox_policy_digest == item.spec.sandbox_policy_digest
            )
            try:
                if same_context:
                    delivered = self._send_locked(conn, r, item)
                else:
                    delivered = self._switch_context_locked(conn, r, item)
            except Exception as exc:  # noqa: BLE001 —— 单条消息失败不能阻塞队列
                self._delivery_failed(conn, r, item, exc)
                continue
            if delivered:
                return
            # run 在投递前变成不可投递（并发失败/取消）：继续下一条。
            self._queue_drop(conn, r, item, "run became non-dispatchable")
        self._mark_idle_locked(conn, r)

    def _project_before_delivery(self, conn, r: _Role, item: _Queued) -> None:
        """实际投递前投影当前任务；失败不阻断投递（spawn 仍可用旧投影）。"""
        if not item.run_id or self.on_project is None:
            return
        if r.last_projected_run_id == item.run_id:
            return
        try:
            self.on_project(
                r.role, item.run_id, item.task_id,
                item.spec.workspace_path or item.spec.cwd)
            r.last_projected_run_id = item.run_id
        except Exception:  # noqa: BLE001 —— 投影失败不阻断消息
            self._log(r.role, f"[anvil] projection failed for run {item.run_id}")
            try:
                self._audit_session(
                    conn, r.role, item.task_id, "projection.error",
                    {"run_id": item.run_id})
            except Exception:  # noqa: BLE001
                pass

    def _switch_context_locked(self, conn, r: _Role, item: _Queued) -> bool:
        """在 turn 边界切换任务上下文：保存当前槽 → 重开进程 → 恢复目标槽。"""
        target_key = context_key(item.task_id, item.slice_key, stage=item.stage)
        # 1) 保存当前上下文
        if r.context_key:
            self._slot_persist(r, status="idle", conn=conn)
        # 2) 计划内切换：旧进程退出不能触发 on_exit / 自动判失败。
        #    switching=True 让调度器在「杀旧进程 → 拉新进程」窗口内不要把它当死进程回收。
        r.switching = True
        try:
            return self._switch_context_inner(conn, r, item, target_key)
        finally:
            r.switching = False

    def _switch_context_inner(self, conn, r: _Role, item: _Queued,
                              target_key: str) -> bool:
        if r.proc is not None and r.proc.poll() is None:
            old_run_id = r.current_run_id
            r.suppress_exit = True
            self._kill_locked(r, graceful=True)
            r.suppress_exit = False
            self._clear_current_locked(r)
            # 计划内切走旧上下文：旧 run 不能永久停在 running，否则依赖它
            # 结束的 resolver/看门狗会一直等一个已经不在执行的 turn。
            if old_run_id:
                from . import runs as runs_service
                runs_service.set_status(
                    conn, old_run_id, "suspended", from_statuses=("running",))
            # 旧槽只是释放进程，session 仍可恢复：槽状态回到 idle。
            if r.context_key:
                self._slot_persist(r, status="idle", conn=conn)
        # 3) 加载目标槽
        row = self._slot_load(conn, r.role, target_key)
        r.context_key = target_key
        r.loaded_spec = item.spec
        r.slot_seen_activity = int(row["last_seen_activity_id"] or 0) if row else 0
        r.model_id = (row["model_id"] if row else None)
        r.served_model_id = None
        r.context_tokens = int(row["context_tokens"] or 0) if row else 0
        r.payload_chars = 0
        r.handoff_pending = ""
        resume = bool(row and row["session_id"])
        rotate_reason = None
        if resume:
            old_cwd = str(row["cwd"] or "")
            new_cwd = str(item.spec.cwd or "")
            if old_cwd and new_cwd and old_cwd != new_cwd:
                # Claude Code 的会话按 cwd 分目录；cwd 变了就不能 --resume。
                self._audit_session(
                    conn, r.role, item.task_id, "session.cwd_changed_rotate",
                    {"from": old_cwd, "to": new_cwd})
                rotate_reason = "cwd-changed"
                resume = False
            elif int(row["context_tokens"] or 0) >= self._cic_threshold(r):
                rotate_reason = "context-high-on-resume"
                resume = False
        if rotate_reason:
            # 换新上下文前先固化该切片的决策/进度，CIC 才能接得上。
            try:
                from . import memory as memory_service
                memory_service.consolidate_context(
                    conn, r.role, item.task_id, rotate_reason, None,
                    slice_key=context_parts(target_key)[1])
            except Exception:  # noqa: BLE001 —— 快照失败不阻断恢复
                self._log(r.role, f"[anvil] knowledge snapshot failed ({rotate_reason})")
        r.session_id = row["session_id"] if resume else None
        r.cic_pending = not resume
        if not resume:
            # 新会话/CIC 的上下文从 0 重新计量；不能沿用旧槽的 context_tokens，
            # 否则首个无 usage 的 synthetic/API 事件会再次触发硬阈值轮换，形成死循环。
            r.context_tokens = 0
        if resume:
            target_task_id, target_slice_key = context_parts(target_key)
            r.handoff_pending = self._slot_handoff(
                conn, r.role, target_task_id, target_slice_key, r.slot_seen_activity)
        self._log(
            r.role,
            f"[anvil] context switch → {target_key} "
            f"({'resume ' + str(r.session_id) if resume else 'new session + CIC'})")
        # 4) 拉起目标槽并投递（投递前投影当前任务，避免排队任务互相覆盖 CLAUDE.md）
        self._project_before_delivery(conn, r, item)
        self._spawn(conn, r, item.spec)
        return self._send_locked(conn, r, item)

    def _step_wake_is_stale(self, conn, item: _Queued) -> bool:
        """步骤已 approved/pending/rebound 时，排队中的旧唤醒不应再投递。"""
        if not item.run_id:
            return False
        run = conn.execute(
            "SELECT workflow_id, step_id FROM runs WHERE id=?",
            (item.run_id,)).fetchone()
        if run is None or not run["workflow_id"] or not run["step_id"]:
            return False
        step = conn.execute(
            "SELECT status, run_id FROM workflow_steps "
            "WHERE workflow_id=? AND step_id=?",
            (run["workflow_id"], run["step_id"])).fetchone()
        return (step is None or step["status"] != "running"
                or str(step["run_id"] or "") != str(item.run_id))

    @staticmethod
    def _run_wake_note(conn, run_id: str | None) -> str | None:
        if not run_id:
            return None
        row = conn.execute("SELECT wake_note FROM runs WHERE id=?",
                           (run_id,)).fetchone()
        return row["wake_note"] if row else None

    @staticmethod
    def _refresh_unread_block(conn, r: _Role, item: _Queued, text: str) -> str:
        """投递前按当前 cursor 重算“未读会话”，去掉排队期间已处理的消息。"""
        from . import agent_inbox
        start_marker = "<!-- ANVIL:UNREAD_START -->"
        end_marker = "<!-- ANVIL:UNREAD_END -->"
        start = text.find(start_marker)
        if start < 0:
            return text
        end = text.find(end_marker, start + len(start_marker))
        if end < 0:
            return text
        end += len(end_marker)
        if not item.task_id:
            return text[:start] + text[end:]
        unread = agent_inbox.unread_count(
            conn, r.role, item.task_id, slice_key=item.slice_key)
        if not unread:
            return text[:start] + text[end:]
        items = agent_inbox.items(
            conn, r.role, item.task_id, limit=3, slice_key=item.slice_key)
        lines = "\n".join(f"- [{i['role']}] {i['text'][:300]}" for i in items)
        block = (
            "<!-- ANVIL:UNREAD_START -->\n"
            f"## 未读会话（{unread} 条，最近 3 条）\n{lines}\n\n"
            f"处理完反馈后执行 `anvil inbox commit --task {item.task_id} --up-to <最大消息 id>`；"
            f"完整未读用 `anvil inbox check --task {item.task_id}` 按需拉取（拉模式）。\n"
            "<!-- ANVIL:UNREAD_END -->")
        return text[:start] + block + text[end:]

    def _send_locked(self, conn, r: _Role, item: _Queued) -> bool:
        # 状态门禁：队列项可能在排队期间被 scheduler/治理置为终态或 HITL 态。
        from . import runs as runs_service
        if item.run_id:
            row = conn.execute(
                "SELECT status FROM runs WHERE id=?", (item.run_id,)).fetchone()
            if row is None or not runs_service.is_dispatchable_status(row["status"]):
                return False
        # 步骤已推进/重绑：丢弃旧 step 唤醒，避免角色收到“提交 S1”后才去查状态。
        if self._step_wake_is_stale(conn, item):
            self._audit_session(
                conn, r.role, item.task_id, "delivery.stale_step_wake",
                {"run_id": item.run_id})
            return False
        self._project_before_delivery(conn, r, item)
        spec = item.spec
        r.loaded_spec = spec
        if spec.model_pool:
            r.model_pool = list(spec.model_pool)
        if spec.context_window_floor:
            r.context_window_floor = spec.context_window_floor
        # 上下文已按任务切换；_context_hint 不应再提示“任务已切换，建议重置”。
        r.last_task_id = item.task_id
        r.last_slice_key = item.slice_key
        task_prefix = (
            f"当前任务 ID：{item.task_id}\n"
            "（环境变量 ANVIL_TASK_ID 可能已过期，请以此为准）\n\n"
            if item.task_id else "")
        if item.slice_key:
            task_prefix += (f"当前切片：{item.slice_key}\n"
                            "（本切片的会话上下文与其他切片隔离）\n\n")
        if r.cic_pending:
            prefix = self._build_cic(
                conn, r.role, item.task_id, run_id=item.run_id,
                slice_key=item.slice_key) + "\n\n" + task_prefix
            r.cic_pending = False
        else:
            prefix = (r.handoff_pending
                      + self._context_hint(r, item.task_id, item.slice_key)
                      + task_prefix)
        r.handoff_pending = ""
        # 入队时的 text 只是快照；真正写 stdin 前重算一遍，纳入排队期间新增的
        # wake_note / 未读 / continuation 头。渲染失败退回快照，并保留 wake_note
        # 供下一次投递消费（at-least-once）。
        rendered_ok = not item.run_id
        rendered_note = self._run_wake_note(conn, item.run_id) if item.run_id else None
        text = prefix + item.text
        if item.run_id:
            if self.on_render is None:
                # 没有重渲染回调：入队快照就是权威内容（仍做未读块刷新）。
                rendered_ok = True
            else:
                try:
                    rendered = self.on_render(item.run_id)
                except Exception as exc:  # noqa: BLE001
                    self._audit_session(
                        conn, r.role, item.task_id, "delivery.render_failed",
                        {"run_id": item.run_id, "error": str(exc)[:300]})
                    rendered = None
                if rendered:
                    text = prefix + rendered
                    rendered_ok = True
        if self.on_render is None or not rendered_ok:
            text = self._refresh_unread_block(conn, r, item, text)

        # 排队期间被标记 suspended/created 的 run，真正投递时恢复 running。
        # 已 running 的排队 run 不重复写；状态迁移必须走 set_status 以写 ledger。
        # 必须在设置 r.busy/current_* 之前完成 CAS，失败时不要留下幽灵 busy。
        if item.run_id:
            current = conn.execute(
                "SELECT status FROM runs WHERE id=?", (item.run_id,)).fetchone()
            if current is not None and current["status"] != "running":
                if not runs_service.set_status(
                        conn, item.run_id, "running",
                        from_statuses=("created", "projecting", "suspended")):
                    again = conn.execute(
                        "SELECT status FROM runs WHERE id=?", (item.run_id,)).fetchone()
                    if again is None or again["status"] != "running":
                        return False
            conn.execute(
                "UPDATE runs SET started_at=COALESCE(started_at, ?), ended_at=NULL "
                "WHERE id=?", (now_iso(), item.run_id))
            conn.commit()

        r.busy = True
        r.busy_since = time.time()
        r.current_run_id = item.run_id
        r.current_task_id = item.task_id
        if item.run_id and r.sandbox_instance_id:
            try:
                runs_service.set_sandbox_instance(
                    conn, item.run_id, r.sandbox_instance_id)
            except Exception:  # noqa: BLE001 —— 观测列写入失败不阻断投递
                pass
        row = (conn.execute("SELECT log_path, step_id FROM runs WHERE id=?",
                            (item.run_id,)).fetchone() if item.run_id else None)
        r.current_log_path = row["log_path"] if row else None
        r.current_step_id = row["step_id"] if row else None
        r.last_step_id = r.current_step_id
        # 投递成功后才推进「已看到」游标；失败时保留旧游标，下次恢复能补上未送达事件。
        seen = max(
            int(r.slot_seen_activity or 0),
            self._slot_seen_activity(conn, r.role, item.task_id, item.slice_key))
        self._slot_persist(r, status="busy", conn=conn)
        self._append_run_log(r, f"\n===== USER（上下文/提示词） @ {now_iso()} =====\n{text}\n")
        self._db_upsert(conn, r, status="busy")
        try:
            if r.proc and r.proc.stdin:
                r.proc.stdin.write(frame_user_message(text).encode("utf-8"))
                r.proc.stdin.flush()
                r.slot_seen_activity = seen
                self._slot_persist(r, status="busy", conn=conn)
                if item.run_id and rendered_ok:
                    # wake_note 是“投递即消费”的一次性 continuation；渲染失败时
                    # 快照可能没包含它，保守保留。按入队/渲染时读到的原文做 CAS，
                    # 不清掉渲染后追加的新 note。
                    expected = rendered_note if self.on_render is not None else item.wake_note
                    conn.execute(
                        "UPDATE runs SET wake_note=NULL WHERE id=? "
                        "AND COALESCE(wake_note,'')=COALESCE(?,'')",
                        (item.run_id, expected))
                    conn.commit()
            else:
                raise _DeliveryError("role process is not writable")
        except (BrokenPipeError, OSError, _DeliveryError) as exc:
            self._log(r.role, f"[anvil] stdin write failed: {exc}")
            r.last_active = time.time()
            r.suppress_exit = True
            self._kill_locked(r, graceful=True)
            r.suppress_exit = False
            self._clear_current_locked(r)
            raise _DeliveryError(str(exc)) from exc
        return True

    def _audit_session(self, conn, role: str, task_id: str | None, action: str,
                       payload: dict) -> None:
        try:
            ledger.emit_audit(
                conn, role_slug=role,
                task_id=task_id,
                action=action,
                payload=jdumps(payload),
                now=now_iso())
            conn.commit()
        except Exception:  # noqa: BLE001 —— 审计失败不阻断运行
            pass
