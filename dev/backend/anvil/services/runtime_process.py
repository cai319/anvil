"""角色进程生命周期：spawn、stdio 读取、退出与空闲回收。"""
from __future__ import annotations

import json
import secrets
import subprocess
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone

from .. import config
from ..db import open_conn
from ..util import decode_child_output, jdumps, now_iso
from .daemon_common import (
    _Role, _SpawnSpec, _attachment_chars, _clean_env,
    _is_context_overflow, _is_payload_overflow, clear_session,
    extract_turn_usage, logger,
)


class RuntimeProcessMixin:
    def kill(self, role: str) -> None:
        r = self._roles.get(role)
        if r is None:
            return
        with r.lock:
            # 主动终止不算 agent 失败，不触发 on_exit / 自动清会话。
            r.suppress_exit = True
            self._slot_persist(r)
            self._kill_locked(r, graceful=True)
            r.suppress_exit = False

    def restart_process(self, role: str, *, flush_queue: bool = True) -> None:
        """终止常驻进程但保留所有任务上下文槽；下一次唤醒恢复对应任务。

        `flush_queue=False` 时先保留队列：调用方（restart 端点）应先 pump 被
        重启的目标 run，再调用 `flush_queue`，避免旧的 mention/reply 队列项
        抢在目标步骤 run 之前投递。
        """
        r = self._roles.get(role)
        if r is None:
            return
        with r.lock:
            r.suppress_exit = True
            # 计划内重启的杀人窗口：scheduler.monitor 不能把这当成进程死亡。
            r.switching = True
            try:
                self._slot_persist(r)
                # 重启只结束当前进程，不丢其它任务/后续消息的队列。
                self._clear_current_locked(r)
                self._kill_locked(r, graceful=True)
            finally:
                r.suppress_exit = False
                r.switching = False
            r.context_key = None
            r.loaded_spec = None
            r.session_id = None
            conn = open_conn(self.db_path)
            try:
                self._db_upsert(conn, r, status="dead")
                if flush_queue and r.queue:
                    self._flush_next_locked(conn, r)
            finally:
                conn.close()

    def shutdown(self) -> None:
        # 停机路径：不触发 on_exit（否则会把 run 标 failed/abandoned，与 suspend 语义冲突）
        self._shutting_down = True
        self._stop.set()
        for r in list(self._roles.values()):
            with r.lock:
                self._slot_persist(r)
                self._kill_locked(r, graceful=True)

    def _spawn(self, conn, r: _Role, spec: _SpawnSpec) -> None:
        from ..graph.render import render_template

        row = conn.execute(
            "SELECT token FROM role_sessions WHERE role_slug=?", (r.role,)
        ).fetchone()
        resume = bool(r.session_id)
        if resume and int(r.context_tokens or 0) >= self._cic_threshold(r):
            # 会话过大：不 resume，按 CIC 构造新会话首输入（见 _build_cic）
            resume = False
            r.cic_pending = True
        sid = r.session_id if resume else str(uuid.uuid4())
        if not resume:
            r.payload_chars = 0  # 新会话不再携带旧图片/附件
        token = (row["token"] if row else None) or self._issue_token(conn, r.role)

        tpl = spec.args_resume if resume else spec.args_new
        full_ctx = dict(spec.ctx, session_id=sid, session_token=token)
        args = [render_template(str(a), full_ctx) for a in tpl]
        merged_env = _clean_env(dict(spec.env, ANVIL_TOKEN=token, ANVIL_AGENT_SLUG=r.role))
        # ANVIL_TASK_ID 保留 spawn 时的任务 id（跨任务时以消息前缀为准），
        # 避免 agent 执行 `anvil inbox check` 等命令时 task_id 为空。

        # 二进制管道：由 _reader_loop 统一按 UTF-8/本地编码回退解码，
        # 避免 Windows text 模式按 GBK 解码或把 \n 改写成 \r\n。
        kwargs: dict = dict(
            cwd=spec.cwd, env=merged_env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            proc = subprocess.Popen(
                [spec.executable, *args], start_new_session=True, **kwargs)
        except TypeError:  # Windows 不支持 start_new_session
            proc = subprocess.Popen([spec.executable, *args], **kwargs)
        r.proc = proc
        r.proc_generation += 1
        # 新进程不继承上一代的溢出标记。
        r.context_overflow = False
        r.context_overflow_reason = None
        r.context_overflow_proc = None
        self._log(r.role,
                  f"[anvil] spawn {spec.executable} {' '.join(str(a) for a in args)}")
        r.session_id = sid
        r.token = token
        r.loaded_spec = spec
        r.model_pool = list(spec.model_pool or [])
        r.context_window_floor = spec.context_window_floor
        r.busy = False
        r.last_active = time.time()
        r.stop = False
        r.suppress_exit = False
        self._db_upsert(conn, r, status="starting")
        r.reader = threading.Thread(
            target=self._reader_loop, args=(r,), daemon=True, name=f"anvil-role-{r.role}")
        r.reader.start()
        if self._idle_thread is not None and not self._idle_thread.is_alive():
            self._idle_thread.start()

    def _issue_token(self, conn, role: str) -> str:
        token = secrets.token_urlsafe(24)
        expires = (datetime.now(timezone.utc)
                   + timedelta(days=config.ROLE_SESSION_TOKEN_DAYS)).isoformat()
        conn.execute("DELETE FROM sessions WHERE role_slug=? AND task_id IS NULL", (role,))
        conn.execute(
            "INSERT INTO sessions (token, role_slug, run_id, task_id, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?)", (token, role, None, None, now_iso(), expires))
        conn.commit()
        return token

    def _reader_loop(self, r: _Role) -> None:
        proc = r.proc
        assert proc is not None and proc.stdout is not None
        try:
            for raw in proc.stdout:
                # 进程已被计划内上下文切换替换：不再处理旧进程的缓冲事件。
                if r.proc is not proc:
                    return
                line = decode_child_output(raw).strip()
                if not line:
                    continue
                if not line.startswith("{"):
                    self._log(r.role, line)  # claude 的警告行（如 unrecognized_model）
                    if _is_payload_overflow(line):
                        if r.proc is proc:
                            self._begin_context_overflow(r, "payload", proc)
                        break
                    if _is_context_overflow(line):
                        if r.proc is proc:
                            self._begin_context_overflow(r, "api_error", proc)
                        break
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    self._log(r.role, line)
                    continue
                if r.proc is not proc:
                    return
                self._track_subagent(r, ev, proc)
                self._append_run_log(r, self._format_event(ev))
                # 图片/附件不计 token，但会计入请求体；累计到阈值时主动换新会话，
                # 避免撞上 “request too large (max 32mb)”。
                added_chars = _attachment_chars(ev)
                payload_hit = False
                if added_chars:
                    with r.lock:
                        if r.proc is not proc:
                            return
                        r.payload_chars = int(r.payload_chars or 0) + added_chars
                        payload_hit = bool(
                            r.current_run_id and config.PAYLOAD_HARD_RESET_CHARS > 0
                            and r.payload_chars >= config.PAYLOAD_HARD_RESET_CHARS)
                    if payload_hit:
                        self._begin_context_overflow(r, "payload", proc)
                        break
                etype = ev.get("type")
                if etype == "system" and ev.get("session_id"):
                    with r.lock:
                        if r.proc is not proc:
                            return
                        r.session_id = ev["session_id"]
                        model = ev.get("model") or ev.get("model_id")
                        if model:
                            r.model_id = str(model)
                elif etype == "result":
                    # Token 记账以 result 事件为准：assistant 事件没有 output_tokens，
                    # 且同一轮会重复带 input。每轮只记一次。
                    turn_usage = extract_turn_usage(ev)
                    if turn_usage:
                        with r.lock:
                            usage_task_id = r.current_task_id
                            usage_run_id = r.current_run_id
                        self._record_usage(r, turn_usage, usage_task_id, usage_run_id)
                    # 只认错误结果里的文本；正常 result 是 agent 的最终回答，
                    # 里面可能出现 “prompt is too long” 等字样（例如正在分析本 bug）。
                    result_text = str(ev.get("result") or ev.get("error") or "")
                    if ev.get("is_error") and _is_payload_overflow(result_text):
                        if r.proc is proc:
                            self._begin_context_overflow(r, "payload", proc)
                        break
                    if ev.get("is_error") and _is_context_overflow(result_text):
                        if r.proc is proc:
                            self._begin_context_overflow(r, "api_error", proc)
                        break
                    if r.proc is not proc:
                        return
                    self._on_turn_result(r, ev, proc=proc)
                elif etype == "assistant":
                    msg = ev.get("message") or {}
                    model = msg.get("model") or ev.get("model")
                    old_model = None
                    usage: dict = {}
                    switched = False
                    hard = False
                    is_api_error = bool(
                        ev.get("isApiErrorMessage") or model == "<synthetic>")
                    with r.lock:
                        if r.proc is not proc:
                            return
                        old_model = r.model_id
                        if model:
                            served = str(model)
                            r.served_model_id = served
                            if not old_model:
                                r.model_id = served
                            elif self._model_family(old_model) == self._model_family(served):
                                # init 别名可能更具体（deepseek-flash[1m] vs served deepseek-flash）；
                                # 同族时优先保留有明确窗口的变体，二者都明确则取较小窗口。
                                old_w, old_known = self._model_window_info(old_model)
                                new_w, new_known = self._model_window_info(served)
                                if new_known and (not old_known or new_w < old_w):
                                    r.model_id = served
                            else:
                                r.model_id = served
                        usage = msg.get("usage") or {}
                        if usage:
                            try:
                                ctx = sum(int(usage.get(k) or 0) for k in (
                                    "input_tokens", "cache_read_input_tokens",
                                    "cache_creation_input_tokens"))
                                if ctx:
                                    r.context_tokens = ctx
                            except (TypeError, ValueError):
                                pass
                        task_id, run_id = r.current_task_id, r.current_run_id
                        switched = bool(
                            old_model and r.model_id and r.model_id != old_model)
                        hard = bool(
                            not is_api_error
                            and r.current_run_id
                            and int(r.context_tokens or 0) >= self._hard_threshold(r))
                    self._log(r.role, self._assistant_text(ev))
                    if switched:
                        self._on_model_switch(r, old_model, r.model_id, task_id)
                    # 结构化 API 错误（synthetic assistant 消息）也要识别，避免只依赖
                    # 原始文本；同时检查单轮硬阈值——一次唤醒可能连续跑几百次工具调用，
                    # CLI 的自动压缩来不及介入。
                    if is_api_error:
                        blocks = msg.get("content") or []
                        err_text = "".join(
                            b.get("text", "") for b in blocks if isinstance(b, dict))
                        if _is_payload_overflow(err_text):
                            if r.proc is proc:
                                self._begin_context_overflow(r, "payload", proc)
                            break
                        if _is_context_overflow(err_text):
                            if r.proc is proc:
                                self._begin_context_overflow(r, "api_error", proc)
                            break
                        # 网络/API 不可达等 synthetic 错误不能按上下文水位轮换；
                        # CLI 常在错误后继续等待/重试，表现为角色一直“进行中”。
                        # 快速结束本轮，让 process_exit → 自动恢复/失败处理接管。
                        self._log(
                            r.role,
                            "[anvil] transient API error; stopping role process: "
                            + (err_text[:300] or "<no message>"),
                        )
                        try:
                            if r.proc is proc:
                                proc.terminate()
                        except Exception:  # noqa: BLE001
                            pass
                        break
                    if hard:
                        if r.proc is not proc:
                            return
                        self._begin_context_overflow(r, "threshold", proc)
                        break
        except Exception:  # noqa: BLE001 —— 解码/管道异常不能静默杀死 reader
            self._log(r.role, "[anvil] reader crashed:\n" + traceback.format_exc())
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        # 进程退出。stdout EOF 时 poll() 可能还是 None，wait 一下拿真实退出码，
        # 否则非零退出会被误判成正常退出，失败后仍会 --resume 旧上下文。
        code = proc.poll()
        if code is None:
            try:
                code = proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                code = None
        # reader 已结束但进程仍不退出（例如 API 错误后 CLI 卡住）：强制回收，
        # 避免留下不受 daemon 管理的孤儿进程。
        if code is None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                code = proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                code = None
        # 失败后默认丢弃上下文：非零退出时下一次唤醒新建 session，
        # 避免坏 context 被 --resume 反复复现（可用 ANVIL_RESET_SESSION_ON_FAILURE=0 关闭）。
        overflow_run_id: str | None = None
        overflow_reason: str | None = None
        exit_run_id: str | None = None
        was_current = False
        suppress = False
        reset_key: str | None = None
        with r.lock:
            # 进程可能已被一次计划内上下文切换替换：旧 reader 只负责安静退出。
            if r.proc is not proc:
                return
            was_current = True
            suppress = bool(r.suppress_exit)
            auto_reset = (
                code not in (None, 0)
                and not suppress
                and not self._shutting_down
                and config.RESET_SESSION_ON_FAILURE
            )
            exit_run_id = r.current_run_id
            r.proc = None
            self._clear_current_locked(r)
            if auto_reset:
                reset_key = r.context_key
                r.session_id = None
                r.context_tokens = 0
                # 崩溃后的重试用 CIC：保留 MEMORY/工作流决策/知识快照，
                # 不从一张白纸重新开始。
                r.cic_pending = True
            # 只消费属于本进程的溢出标记；旧 reader 不能偷走新进程的标记。
            if (r.context_overflow
                    and r.context_overflow_proc in (None, proc)):
                overflow_run_id = exit_run_id
                overflow_reason = r.context_overflow_reason or "threshold"
                reset_key = r.context_key
                r.context_overflow = False
                r.context_overflow_reason = None
                r.context_overflow_proc = None
            elif (r.context_overflow and r.context_overflow_proc is not None
                  and r.context_overflow_proc.poll() is not None):
                # 标记属于已经死掉的旧进程：清掉，避免永久抑制后续溢出检测。
                r.context_overflow = False
                r.context_overflow_reason = None
                r.context_overflow_proc = None
        conn = open_conn(self.db_path)
        try:
            note = f"auto reset after exit code {code}" if auto_reset else None
            if overflow_run_id:
                note = f"context overflow ({overflow_reason}) run={overflow_run_id}"
            self._db_upsert(conn, r, status="dead", pid=None, error=note)
            if reset_key:
                # 只清当前任务的槽；其它任务的上下文不受影响。
                clear_session(conn, r.role, note or "auto reset after failure",
                              task_key_value=reset_key)
        finally:
            conn.close()
        if not was_current:
            return
        if overflow_run_id and not self._shutting_down and self.on_context_overflow:
            # 不在 reader 线程里同步跑编排器：投递/拉起新进程会重新获取 r.lock。
            threading.Thread(
                target=self._notify_context_overflow,
                args=(r.role, overflow_run_id, overflow_reason or "threshold"),
                daemon=True, name=f"anvil-overflow-{r.role}").start()
        elif self.on_exit and not self._shutting_down and not suppress:
            try:
                # 只把实际执行的 run 交给编排器；排队 run 由 queue recovery 处理。
                self.on_exit(r.role, 0 if code is None else code, exit_run_id)
            except Exception:  # noqa: BLE001
                pass
        # 进程退出时如果还有排队消息（例如运行时每个 turn 后自行退出），
        # 不能只把角色标成 dead 等下一次外部唤醒；否则队列会永久悬空。
        if (not self._shutting_down and not suppress and not overflow_run_id
                and r.queue):
            threading.Thread(
                target=self._resume_queue_after_exit, args=(r.role,),
                daemon=True, name=f"anvil-queue-{r.role}").start()

    def _resume_queue_after_exit(self, role: str) -> None:
        """进程退出后恢复遗留队列：短暂等待 on_exit 的回调投递，再接管剩余消息。"""
        if self._shutting_down or self._stop.is_set():
            return
        # 给 on_exit 回调一个同步投递的机会；若它已拉起新进程，下面会检测到并跳过。
        if self._stop.wait(0.2):
            return
        r = self._roles.get(role)
        if r is None:
            return
        with r.lock:
            if (self._shutting_down or r.proc is not None or r.busy
                    or r.switching or not r.queue):
                return
            queued = len(r.queue)
            logger.info(
                "role queue resume after process exit role=%s queued=%s",
                r.role, queued,
            )
            self._log(
                r.role,
                f"[anvil] process exit left {queued} queued message(s); resuming queue",
            )
        conn = open_conn(self.db_path)
        try:
            with r.lock:
                if (self._shutting_down or r.proc is not None or r.busy
                        or r.switching or not r.queue):
                    return
                self._flush_next_locked(conn, r)
        except Exception:  # noqa: BLE001 —— 恢复失败不能让 daemon 崩溃
            self._log(r.role, "[anvil] queued resume failed:\n"
                              + traceback.format_exc())
        finally:
            conn.close()

    def _append_run_log(self, r: _Role, text: str | None) -> None:
        if not text or not r.current_log_path:
            return
        try:
            with r.log_lock:
                with open(r.current_log_path, "a", encoding="utf-8") as f:
                    f.write(text if text.endswith("\n") else text + "\n")
        except OSError:
            pass

    @staticmethod
    def _format_event(ev: dict) -> str | None:
        """把 stream-json 事件格式化为可读的「上下文/模型/工具」记录。"""
        t = ev.get("type")
        if t == "system":
            if ev.get("subtype") == "init":
                return (f"[system] init session={ev.get('session_id')} "
                        f"model={ev.get('model') or ev.get('model_id') or '-'}")
            return None
        if t in ("assistant", "user"):
            msg = ev.get("message") or {}
            blocks = msg.get("content")
            if not isinstance(blocks, list):
                return None
            out: list[str] = []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    out.append(f"[{t}] {b.get('text', '')}")
                elif bt == "tool_use":
                    out.append(f"[tool_use] {b.get('name')} {jdumps(b.get('input') or {})}")
                elif bt == "tool_result":
                    content = b.get("content")
                    if isinstance(content, list):
                        content = "\n".join(
                            x.get("text", "") for x in content if isinstance(x, dict))
                    out.append(f"[tool_result] {content}")
            return "\n".join(out) if out else None
        if t == "result":
            usage = ev.get("usage") or {}
            out = [f"[result] usage={jdumps(usage)}"]
            if ev.get("result") is not None:
                out.append(str(ev.get("result")))
            return "\n".join(out)
        return None

    def _release_reminder_process(self, conn, r: _Role, run_id: str | None,
                                  reminder_row) -> None:
        """提醒结束后回收临时进程。

        触发前角色没有进程（was_alive=0）且没有新的当前任务/排队消息时，立即停止
        进程；触发前已在干活、或已有排队任务的角色保持不动，继续原来的工作。
        """
        if not run_id or reminder_row is None:
            return
        conn.execute("DELETE FROM reminder_wakes WHERE run_id=?", (run_id,))
        conn.commit()
        if reminder_row["was_alive"]:
            return
        if (r.proc is None or r.busy or r.switching or r.queue
                or r.current_task_id):
            return
        self._slot_persist(r, status="idle", conn=conn)
        self._kill_locked(r, graceful=True)

    def _idle_loop(self) -> None:
        while not self._stop.wait(30):
            now = time.time()
            for r in list(self._roles.values()):
                with r.lock:
                    # 锁内重查：扫描到上锁之间可能已完成一次上下文切换/新唤醒。
                    if r.switching:
                        continue
                    if not r.busy and r.queue:
                        # 兜底：正常情况下 queue 会在 turn 边界或进程退出时刷新；
                        # 若因异常窗口留下“空闲但有队列”，不能在 idle 扫描里忽略。
                        logger.warning(
                            "stale role queue detected role=%s queued=%s; resuming",
                            r.role, len(r.queue),
                        )
                        self._log(
                            r.role,
                            f"[anvil] stale queue detected ({len(r.queue)}); resuming",
                        )
                        conn = open_conn(self.db_path)
                        try:
                            self._flush_next_locked(conn, r)
                        except Exception:  # noqa: BLE001
                            self._log(r.role, "[anvil] stale queue resume failed:\n"
                                              + traceback.format_exc())
                        finally:
                            conn.close()
                        continue
                    if r.proc is None or r.busy:
                        continue
                    idle_minutes = (now - (r.last_active or now)) / 60.0
                    if idle_minutes >= self.idle_release_minutes:
                        self._slot_persist(r, status="idle")
                        self._kill_locked(r, graceful=True)
                        self._slot_persist(r, status="idle")

    def _kill_locked(self, r: _Role, graceful: bool = True) -> None:
        proc = r.proc
        if proc is None:
            return
        # 先与进程失联：旧 reader 仍可能有缓冲事件，必须立即停止回写共享状态，
        # 否则上下文切换后旧 turn 的 result 会修改新任务的 current_run/tokens。
        r.proc = None
        if r.context_overflow_proc is proc:
            r.context_overflow = False
            r.context_overflow_reason = None
            r.context_overflow_proc = None
        r.busy = False
        r.busy_since = None
        try:
            if graceful and proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        conn = open_conn(self.db_path)
        try:
            self._db_upsert(conn, r, status="dead", pid=None)
        finally:
            conn.close()

    @staticmethod
    def _assistant_text(ev: dict) -> str:
        try:
            blocks = (ev.get("message") or {}).get("content") or []
            return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        except Exception:  # noqa: BLE001
            return ""
