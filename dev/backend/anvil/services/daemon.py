"""常驻角色运行时（persistent role daemon）。

每个角色一个长期存活的 CLI 会话进程（claude stream-json 双向流）：
- 唤醒 = 往进程 stdin 投递一条用户消息（同一上下文，不重启进程）；
- 空闲 = 阻塞等待下一条消息（CPU 为 0）；
- 空闲超时（默认 15 分钟）= 关闭 stdin 让进程退出，保留 session_id；
- 下次唤醒用 `--resume <session_id>` 恢复同一上下文（上下文持久在磁盘，不常驻内存）；
- 忙时收到新消息 = 排队，当前轮 `result` 事件后合并投递（coalescing）。

设计变更（owner 2026-10-19）：取代 design.md §1.2「进程不持久」的 one-shot 模型；
one-shot 仍作为不支持流式协议的运行时的回退路径。
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config
from ..db import open_conn
from ..util import jdumps, jloads, now_iso

# 不泄漏给子进程的编排器会话变量（与 platform/darwin.py 保持一致）
_SESSION_ENV_PREFIXES = ("CLAUDE_CODE_",)
_SESSION_ENV_KEYS = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ATTENDED"}


def _clean_env(extra: dict[str, str]) -> dict[str, str]:
    merged = {
        k: v for k, v in os.environ.items()
        if not k.startswith(_SESSION_ENV_PREFIXES) and k not in _SESSION_ENV_KEYS
    }
    merged.update(extra)
    return merged


def extract_usage(ev: dict) -> dict:
    """从 result 事件提取 usage；兼容 modelUsage（camelCase）聚合。"""
    usage = ev.get("usage")
    if isinstance(usage, dict) and usage:
        return usage
    mu = ev.get("modelUsage")
    if isinstance(mu, dict):
        mapping = {
            "input_tokens": "inputTokens",
            "output_tokens": "outputTokens",
            "cache_read_input_tokens": "cacheReadInputTokens",
            "cache_creation_input_tokens": "cacheCreationInputTokens",
        }
        agg = {}
        for snake, camel in mapping.items():
            agg[snake] = sum(
                int((v or {}).get(camel) or 0) for v in mu.values() if isinstance(v, dict))
        if any(agg.values()):
            return agg
    return {}


def frame_user_message(text: str) -> str:
    """stream-json 输入帧（claude --input-format stream-json）。"""
    return json.dumps(
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}},
        ensure_ascii=False,
    ) + "\n"


def clear_session(conn, role: str, note: str = "session reset") -> None:
    """清空角色常驻会话；下一次唤醒新建会话（不 --resume）。"""
    conn.execute(
        "UPDATE role_sessions SET session_id='', status='dead', pid=NULL, "
        "last_context_tokens=NULL, last_error=?, updated_at=? WHERE role_slug=?",
        (note, now_iso(), role),
    )
    conn.commit()


@dataclass
class _Role:
    role: str
    proc: subprocess.Popen | None = None
    session_id: str | None = None
    token: str | None = None
    busy: bool = False
    busy_since: float | None = None
    last_active: float = 0.0
    queue: list[tuple[str, str | None, str | None]] = field(default_factory=list)
    current_run_id: str | None = None
    current_task_id: str | None = None
    context_tokens: int = 0
    cic_pending: bool = False
    suppress_exit: bool = False
    current_log_path: str | None = None
    log_lock: threading.Lock = field(default_factory=threading.Lock)
    lock: threading.Lock = field(default_factory=threading.Lock)
    reader: threading.Thread | None = None
    stop: bool = False


class DaemonManager:
    """角色常驻进程管理器（单进程内全局单例；由 create_app 初始化）。"""

    def __init__(self, db_path: str, enabled: bool = True,
                 idle_release_minutes: float | None = None) -> None:
        self.db_path = db_path
        self.enabled = enabled
        self.idle_release_minutes = (
            idle_release_minutes if idle_release_minutes is not None
            else config.ROLE_IDLE_RELEASE_MINUTES
        )
        self.on_exit = None  # 回调(role, exit_code)：由 create_app 注入，泵 process_exit
        self._roles: dict[str, _Role] = {}
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._shutting_down = False
        self._idle_thread: threading.Thread | None = None
        if enabled:
            self._heal_db()
            self._idle_thread = threading.Thread(
                target=self._idle_loop, daemon=True, name="anvil-role-idle")

    # ---- 对外 API --------------------------------------------------------

    def ensure_and_deliver(self, conn, role: str, *, executable: str, args_new: list[str],
                           args_resume: list[str], ctx: dict, cwd: str, env: dict,
                           prompt: str, run_id: str | None = None,
                           task_id: str | None = None) -> dict:
        """确保角色进程存活（必要时 --resume/新建）并投递一条消息。"""
        r = self._role(role)
        with r.lock:
            if r.proc is None or r.proc.poll() is not None:
                self._spawn(conn, r, executable, args_new, args_resume, ctx, cwd, env)
            self._deliver_locked(conn, r, prompt, run_id, task_id)
            return {"session_id": r.session_id, "pid": r.proc.pid, "alive": True}

    def deliver(self, conn, role: str, prompt: str, run_id: str | None = None,
                task_id: str | None = None) -> bool:
        """向已存在的常驻进程投递消息（无进程返回 False，由调用方走 spawn 路径）。"""
        r = self._roles.get(role)
        if r is None or r.proc is None or r.proc.poll() is not None:
            return False
        with r.lock:
            self._deliver_locked(conn, r, prompt, run_id, task_id)
            return True

    def status(self, role: str) -> dict:
        r = self._roles.get(role)
        if r is None:
            return {"alive": False, "status": "dead", "busy_since": None, "pid": None}
        alive = r.proc is not None and r.proc.poll() is None
        return {
            "alive": alive,
            "status": "busy" if (alive and r.busy) else ("idle" if alive else "dead"),
            "busy_since": datetime.fromtimestamp(r.busy_since, tz=timezone.utc).isoformat()
            if (alive and r.busy and r.busy_since) else None,
            "pid": r.proc.pid if r.proc else None,
            "session_id": r.session_id,
        }

    def kill(self, role: str) -> None:
        r = self._roles.get(role)
        if r is None:
            return
        with r.lock:
            # 主动终止不算 agent 失败，不触发 on_exit / 自动清会话。
            r.suppress_exit = True
            self._kill_locked(r, graceful=True)

    def restart_process(self, role: str) -> None:
        """终止常驻进程但保留 session_id；下一次唤醒 --resume 同一上下文。"""
        r = self._roles.get(role)
        if r is None:
            return
        with r.lock:
            r.suppress_exit = True
            r.queue.clear()
            self._kill_locked(r, graceful=True)
            r.busy = False
            r.busy_since = None
            r.current_run_id = None
            r.current_task_id = None
            r.current_log_path = None

    def reset_session(self, role: str) -> None:
        """终止常驻进程并清空 session_id；下一次唤醒新建上下文。"""
        r = self._roles.get(role)
        if r is not None:
            with r.lock:
                r.suppress_exit = True
                r.queue.clear()
                self._kill_locked(r, graceful=True)
                r.session_id = None
                r.context_tokens = 0
                r.cic_pending = False
                r.busy = False
                r.busy_since = None
                r.current_run_id = None
                r.current_task_id = None
                r.current_log_path = None
        conn = open_conn(self.db_path)
        try:
            clear_session(conn, role, "session reset")
        finally:
            conn.close()

    def shutdown(self) -> None:
        # 停机路径：不触发 on_exit（否则会把 run 标 failed/abandoned，与 suspend 语义冲突）
        self._shutting_down = True
        self._stop.set()
        for r in list(self._roles.values()):
            with r.lock:
                self._kill_locked(r, graceful=True)

    # ---- 内部 ------------------------------------------------------------

    def _role(self, role: str) -> _Role:
        with self._guard:
            return self._roles.setdefault(role, _Role(role=role))

    def _spawn(self, conn, r: _Role, executable: str, args_new: list[str],
               args_resume: list[str], ctx: dict, cwd: str, env: dict) -> None:
        from ..graph.render import render_template

        row = conn.execute(
            "SELECT session_id, token, last_context_tokens FROM role_sessions WHERE role_slug=?",
            (r.role,)).fetchone()
        resume = bool(row and row["session_id"])
        if resume and row["last_context_tokens"] and \
                int(row["last_context_tokens"]) >= config.CIC_THRESHOLD_TOKENS:
            # 会话过大：不 resume，按 CIC 构造新会话首输入（见 _build_cic）
            resume = False
            r.cic_pending = True
        sid = (row["session_id"] if resume else str(uuid.uuid4()))
        token = (row["token"] if row else None) or self._issue_token(conn, r.role)

        tpl = args_resume if resume else args_new
        full_ctx = dict(ctx, session_id=sid, session_token=token)
        args = [render_template(str(a), full_ctx) for a in tpl]
        merged_env = _clean_env(dict(env, ANVIL_TOKEN=token, ANVIL_AGENT_SLUG=r.role))
        # ANVIL_TASK_ID 保留 spawn 时的任务 id（跨任务时以消息前缀为准），
        # 避免 agent 执行 `anvil inbox check` 等命令时 task_id 为空。

        kwargs: dict = dict(
            cwd=cwd, env=merged_env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        try:
            proc = subprocess.Popen([executable, *args], start_new_session=True, **kwargs)
        except TypeError:  # Windows 不支持 start_new_session
            proc = subprocess.Popen([executable, *args], **kwargs)
        r.proc = proc
        r.session_id = sid
        r.token = token
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

    def _deliver_locked(self, conn, r: _Role, prompt: str,
                        run_id: str | None, task_id: str | None) -> None:
        prefix = f"当前任务 ID：{task_id}\n（环境变量 ANVIL_TASK_ID 可能已过期，请以此为准）\n\n" if task_id else ""
        if r.cic_pending:
            prefix = self._build_cic(conn, r.role, task_id) + "\n\n" + prefix
            r.cic_pending = False
        text = prefix + prompt
        r.queue.append((text, run_id, task_id))
        if not r.busy:
            self._flush_next_locked(conn, r)

    def _flush_next_locked(self, conn, r: _Role) -> None:
        if not r.queue:
            r.busy = False
            r.busy_since = None
            r.current_run_id = None
            r.current_task_id = None
            r.current_log_path = None
            self._db_upsert(conn, r, status="idle")
            return
        text, run_id, task_id = r.queue.pop(0)
        r.busy = True
        r.busy_since = time.time()
        r.current_run_id = run_id
        r.current_task_id = task_id
        row = conn.execute("SELECT log_path FROM runs WHERE id=?", (run_id,)).fetchone() if run_id else None
        r.current_log_path = row["log_path"] if row else None
        self._append_run_log(r, f"\n===== USER（上下文/提示词） @ {now_iso()} =====\n{text}\n")
        self._db_upsert(conn, r, status="busy")
        try:
            if r.proc and r.proc.stdin:
                r.proc.stdin.write(frame_user_message(text))
                r.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            r.last_active = time.time()
            self._db_upsert(conn, r, status="dead", error=str(exc))

    def _reader_loop(self, r: _Role) -> None:
        proc = r.proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if not line.startswith("{"):
                self._log(r.role, line)  # claude 的警告行（如 unrecognized_model）
                if "Prompt is too long" in line:
                    self._reset_for_cic(r)
                    break
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                self._log(r.role, line)
                continue
            self._append_run_log(r, self._format_event(ev))
            etype = ev.get("type")
            if etype == "system" and ev.get("session_id"):
                r.session_id = ev["session_id"]
            elif etype == "result":
                if "Prompt is too long" in line:
                    self._reset_for_cic(r)
                    break
                self._on_turn_result(r, ev)
            elif etype == "assistant":
                self._log(r.role, self._assistant_text(ev))
                usage = (ev.get("message") or {}).get("usage") or {}
                if usage:
                    try:
                        ctx = sum(int(usage.get(k) or 0) for k in (
                            "input_tokens", "cache_read_input_tokens",
                            "cache_creation_input_tokens"))
                        if ctx:
                            r.context_tokens = ctx
                        self._record_usage(r, usage)
                    except (TypeError, ValueError):
                        pass
        # 进程退出。stdout EOF 时 poll() 可能还是 None，wait 一下拿真实退出码，
        # 否则非零退出会被误判成正常退出，失败后仍会 --resume 旧上下文。
        code = proc.poll()
        if code is None:
            try:
                code = proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                code = None
        # 失败后默认丢弃上下文：非零退出时下一次唤醒新建 session，
        # 避免坏 context 被 --resume 反复复现（可用 ANVIL_RESET_SESSION_ON_FAILURE=0 关闭）。
        auto_reset = (
            code not in (None, 0)
            and not r.suppress_exit
            and not self._shutting_down
            and config.RESET_SESSION_ON_FAILURE
        )
        with r.lock:
            if r.proc is proc:
                r.proc = None
                r.busy = False
                r.busy_since = None
                if auto_reset:
                    r.session_id = None
                    r.context_tokens = 0
                    r.cic_pending = False
        conn = open_conn(self.db_path)
        try:
            note = f"auto reset after exit code {code}" if auto_reset else None
            self._db_upsert(conn, r, status="dead", pid=None, error=note)
            if auto_reset:
                clear_session(conn, r.role, note or "auto reset after failure")
        finally:
            conn.close()
        if self.on_exit and not self._shutting_down and not r.suppress_exit:
            try:
                self.on_exit(r.role, 0 if code is None else code)
            except Exception:  # noqa: BLE001
                pass

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
            out = [f"[result] usage={jdumps(usage)} cost={ev.get('total_cost_usd')}"]
            if ev.get("result") is not None:
                out.append(str(ev.get("result")))
            return "\n".join(out)
        return None

    def _record_usage(self, r: _Role, usage: dict) -> None:
        try:
            from . import tokens as tokens_service
            conn = open_conn(self.db_path)
            try:
                tokens_service.record(conn, r.role, r.current_task_id, r.current_run_id, usage)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass

    def _reset_for_cic(self, r: _Role) -> None:
        """Prompt 过长：本会话已不可用，终止进程并在下次唤醒用 CIC 新会话。"""
        r.cic_pending = True
        r.suppress_exit = True
        try:
            r.context_tokens = max(int(r.context_tokens or 0),
                                   int(config.CIC_THRESHOLD_TOKENS) + 1)
        except Exception:  # noqa: BLE001
            pass
        proc = r.proc
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        self._log(r.role, "[anvil] context too long → 将在下次唤醒用 CIC 新会话")

    def _on_turn_result(self, r: _Role, ev: dict) -> None:
        conn = open_conn(self.db_path)
        try:
            r.last_active = time.time()
            usage = extract_usage(ev)
            try:
                tokens = sum(int(usage.get(k) or 0) for k in (
                    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                if tokens:
                    r.context_tokens = tokens

            except (TypeError, ValueError):
                pass
            cost = ev.get("total_cost_usd")
            if cost:
                try:
                    from . import governance
                    governance.report_cost(conn, r.current_task_id, r.role, float(cost), "reported")
                except Exception:  # noqa: BLE001
                    pass
            # 一轮结束仍未 submit → run 进入 suspended（等待下一条消息）
            if r.current_run_id:
                row = conn.execute("SELECT status FROM runs WHERE id=?", (r.current_run_id,)).fetchone()
                if row and row["status"] == "running":
                    from . import runs as runs_service
                    runs_service.set_status(conn, r.current_run_id, "suspended")
            with r.lock:
                self._flush_next_locked(conn, r)
        finally:
            conn.close()

    def _idle_loop(self) -> None:
        while not self._stop.wait(30):
            now = time.time()
            for r in list(self._roles.values()):
                if r.proc is None or r.busy:
                    continue
                idle_minutes = (now - (r.last_active or now)) / 60.0
                if idle_minutes >= self.idle_release_minutes:
                    with r.lock:
                        self._kill_locked(r, graceful=True)

    def _kill_locked(self, r: _Role, graceful: bool = True) -> None:
        proc = r.proc
        if proc is None:
            return
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
        r.proc = None
        r.busy = False
        r.busy_since = None
        conn = open_conn(self.db_path)
        try:
            self._db_upsert(conn, r, status="dead", pid=None)
        finally:
            conn.close()

    def _build_cic(self, conn, role: str, task_id: str | None) -> str:
        """Constructed Initial Context（简化 3 块）：MEMORY 头 / 最近消息 / 最近动作。

        三条负规则：不宣布重置、不列未决问题清单、超过 2 小时的内容只给指针。
        """
        blocks: list[str] = []
        mem_path = Path(config.AGENTS_DIR) / role / "MEMORY.md"
        try:
            mem = mem_path.read_text(encoding="utf-8")[:4000].strip()
        except OSError:
            mem = ""
        if mem:
            blocks.append("## 记忆（MEMORY.md 头部，无需重读全文）\n" + mem)
        if task_id:
            rows = conn.execute(
                "SELECT id, role_slug, kind, payload_json FROM activities "
                "WHERE task_id=? AND kind IN ('comment','mention','step') "
                "ORDER BY id DESC LIMIT 25", (task_id,)
            ).fetchall()
            lines = []
            for r in reversed(rows):
                p = jloads(r["payload_json"], {})
                if r["kind"] == "comment":
                    txt = str(p.get("body", ""))
                elif r["kind"] == "mention":
                    txt = f"@{p.get('target','')}: {p.get('message','')}"
                else:
                    txt = f"步骤 {p.get('step','')} → {p.get('status','')}"
                lines.append(f"- [{r['role_slug'] or 'system'}] {txt[:400]}")
            if lines:
                blocks.append("## 最近 25 条消息\n" + "\n".join(lines))
            acts = conn.execute(
                "SELECT role_slug, action FROM audit WHERE task_id=? ORDER BY id DESC LIMIT 12",
                (task_id,)
            ).fetchall()
            if acts:
                blocks.append("## 最近动作\n" + "\n".join(
                    f"- {a['role_slug'] or 'system'}: {a['action']}" for a in reversed(acts)))
        return "\n\n".join(blocks)

    def _db_upsert(self, conn, r: _Role, status: str, pid: int | None = -1,
                   error: str | None = None) -> None:
        if pid == -1:
            pid = r.proc.pid if r.proc else None
        conn.execute(
            "INSERT INTO role_sessions (role_slug, session_id, token, pid, status, busy_since, "
            "last_active_at, last_context_tokens, last_error, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(role_slug) DO UPDATE SET session_id=excluded.session_id, token=excluded.token, "
            "pid=excluded.pid, status=excluded.status, busy_since=excluded.busy_since, "
            "last_active_at=excluded.last_active_at, "
            "last_context_tokens=COALESCE(excluded.last_context_tokens, role_sessions.last_context_tokens), "
            "last_error=excluded.last_error, updated_at=excluded.updated_at",
            (r.role, r.session_id or "", r.token, pid, status,
             datetime.fromtimestamp(r.busy_since, tz=timezone.utc).isoformat() if r.busy_since else None,
             datetime.fromtimestamp(r.last_active, tz=timezone.utc).isoformat() if r.last_active else None,
             r.context_tokens or None, error, now_iso()),
        )
        conn.commit()

    def _heal_db(self) -> None:
        """启动自愈：终止上一实例遗留的孤儿角色进程，session_id 保留供 resume。"""
        conn = open_conn(self.db_path)
        try:
            rows = conn.execute(
                "SELECT role_slug, pid FROM role_sessions WHERE pid IS NOT NULL").fetchall()
            for r in rows:
                pid = r["pid"]
                if not pid:
                    continue
                try:
                    # 只终止确实是 claude 的进程，避免 PID 复用误杀
                    out = subprocess.run(
                        ["ps", "-o", "command=", "-p", str(pid)],
                        capture_output=True, text=True, timeout=3).stdout
                    if "claude" in out:
                        os.kill(int(pid), 15)
                        self._log(r["role_slug"],
                                  f"[anvil] 启动自愈：终止上一实例遗留进程 pid={pid}")
                except Exception:  # noqa: BLE001
                    pass
            conn.execute("UPDATE role_sessions SET status='dead', pid=NULL, updated_at=? "
                         "WHERE status IN ('starting','idle','busy')", (now_iso(),))
            conn.commit()
        finally:
            conn.close()

    def _log(self, role: str, text: str) -> None:
        if not text:
            return
        try:
            p = Path(config.AGENTS_DIR) / role / "logs" / "role-session.log"
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(f"[{now_iso()}] {text}\n")
        except OSError:
            pass

    @staticmethod
    def _assistant_text(ev: dict) -> str:
        try:
            blocks = (ev.get("message") or {}).get("content") or []
            return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        except Exception:  # noqa: BLE001
            return ""


# ---- 全局单例（create_app 初始化；测试不启用） -------------------------------

_manager: DaemonManager | None = None


def init_manager(db_path: str, enabled: bool) -> DaemonManager:
    global _manager
    _manager = DaemonManager(db_path, enabled=enabled)
    return _manager


def get_manager() -> DaemonManager | None:
    return _manager
