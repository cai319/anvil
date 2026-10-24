"""常驻角色运行时：同一进程复用、忙时排队、空闲退出后 --resume 恢复同一会话。"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import time

import pytest

from anvil import config
from anvil.db import connect, open_conn

pytest.importorskip("anvil.graph")
from anvil.services import runs as runs_service  # noqa: E402
from anvil.services import tasks as tasks_service  # noqa: E402
from anvil.services.daemon import (  # noqa: E402
    DaemonManager, _SpawnSpec, _clean_env, frame_user_message,
)

FAKE_RUNTIME = '''
import json, os, sys, time
mode = "resume" if "--resume" in sys.argv else "new"
sid = sys.argv[sys.argv.index("--resume" if mode == "resume" else "--session-id") + 1]
with open(os.environ["FAKE_ARGV_LOG"], "a") as f:
    f.write(mode + " " + sid + "\\n")
init_model = os.environ.get("FAKE_MODEL", "fake-1m")
switch_model = os.environ.get("FAKE_SWITCH_MODEL", "")
result_usage = os.environ.get("FAKE_RESULT_USAGE", "")
context_tokens = int(os.environ.get("FAKE_CONTEXT_TOKENS", "0"))
turn_delay = float(os.environ.get("FAKE_TURN_DELAY", "0.2"))
context_error = os.environ.get("FAKE_CONTEXT_ERROR", "")
attachment_chars = int(os.environ.get("FAKE_ATTACHMENT_CHARS", "0"))
result_text = os.environ.get("FAKE_RESULT_TEXT", "OK")
warn_line = os.environ.get("FAKE_WARN_LINE", "")
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid,
                  "model": init_model}), flush=True)
if warn_line:
    print(warn_line, flush=True)
turn = 0
for line in sys.stdin:
    turn += 1
    time.sleep(turn_delay)
    model = switch_model if (switch_model and turn >= 2) else init_model
    usage = {"input_tokens": 10, "output_tokens": 2}
    if context_tokens:
        usage = {"input_tokens": 1, "cache_read_input_tokens": context_tokens - 1,
                 "output_tokens": 2}
    print(json.dumps({"type": "assistant", "message": {
        "model": model,
        "content": [
            {"type": "text", "text": "working"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
        "usage": usage}}), flush=True)
    if context_error == "plain":
        print("Prompt is too long", flush=True)
        continue
    if context_error == "synthetic":
        print(json.dumps({"type": "assistant", "isApiErrorMessage": True, "message": {
            "model": "<synthetic>",
            "content": [{"type": "text", "text": "Prompt is too long"}]}}), flush=True)
        continue
    if context_error == "result":
        print(json.dumps({"type": "result", "subtype": "error", "is_error": True,
                          "result": "Prompt is too long"}), flush=True)
        continue
    if context_error == "payload":
        print("Request too large (max 32mb): accumulated images and attachments "
              "in the conversation pushed the request over the limit. "
              "Remove older images or compact the conversation.", flush=True)
        continue
    tool_content = "file.txt"
    if attachment_chars:
        tool_content = [
            {"type": "text", "text": "file.txt"},
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png",
                                         "data": "A" * attachment_chars}},
        ]
    print(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": tool_content}]}}), flush=True)
    result = {"type": "result", "subtype": "success", "result": result_text}
    if result_usage:
        result["usage"] = json.loads(result_usage)
    print(json.dumps(result), flush=True)
'''


def test_frame_format():
    assert frame_user_message("hi") == (
        '{"type": "user", "message": {"role": "user", "content": '
        '[{"type": "text", "text": "hi"}]}}\n')


def test_clean_env_drops_session_identity_but_keeps_cli_config(monkeypatch):
    """只屏蔽编排器会话身份；CLAUDE_CODE_* 配置项（自动压缩窗口等）必须传给角色。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-session")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "parent-token")
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "786432")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    env = _clean_env({})
    assert "CLAUDE_CODE_SESSION_ID" not in env
    assert "CLAUDE_CODE_MESSAGING_TOKEN" not in env
    assert "CLAUDE_CODE_CHILD_SESSION" not in env
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "786432"
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "max"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    fake = tmp_path / "fake_runtime.py"
    fake.write_text(FAKE_RUNTIME, encoding="utf-8")
    log = tmp_path / "argv.log"
    db_path = str(tmp_path / "t.db")
    conn = connect(db_path)
    mgr = DaemonManager(db_path, enabled=True, idle_release_minutes=60)
    yield conn, mgr, fake, log
    try:
        mgr.shutdown()
    except Exception:  # noqa: BLE001
        pass
    conn.close()


def _wait(predicate, timeout=6.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _run_status(db_path, rid):
    c = open_conn(db_path)
    try:
        r = runs_service.get_run(c, rid)
        return r["status"] if r else None
    finally:
        c.close()


def _deliver(mgr, conn, fake, log, run_id, text="hello", task_id=None, extra_env=None):
    runs_service.set_status(conn, run_id, "running")  # 等价于 resume→spawn 的状态流转
    env = {"FAKE_ARGV_LOG": str(log), **(extra_env or {})}
    args = {
        "executable": sys.executable,
        "args_new": [str(fake), "--session-id", "{{session_id}}"],
        "args_resume": [str(fake), "--resume", "{{session_id}}"],
        "ctx": {},
        "cwd": str(fake.parent),
        "env": env,
        "prompt": text,
        "run_id": run_id,
        "task_id": task_id,
    }
    return mgr.ensure_and_deliver(conn, "qa-role", **args)


def test_result_usage_does_not_overwrite_context_size(env):
    """result.usage 是会话累计消耗，不是当前上下文大小；不能覆盖 assistant 实测值。"""
    conn, mgr, fake, log = env
    run = runs_service.create_run(conn, "qa-role", task_id=None)
    _deliver(mgr, conn, fake, log, run["id"], "first", extra_env={
        "FAKE_RESULT_USAGE": json.dumps({
            "input_tokens": 5_000_000,
            "cache_read_input_tokens": 9_000_000,
            "output_tokens": 2,
        }),
    })
    assert _wait(lambda: _run_status(mgr.db_path, run["id"]) == "suspended")
    assert mgr.status("qa-role")["context_tokens"] == 10
    # Token 记账只认 result：input 不重复累计，output 不再为 0。
    check = open_conn(mgr.db_path)
    try:
        rows = check.execute(
            "SELECT input_tokens, output_tokens, cache_read_tokens FROM token_usage"
        ).fetchall()
    finally:
        check.close()
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 5_000_000
    assert rows[0]["cache_read_tokens"] == 9_000_000
    assert rows[0]["output_tokens"] == 2


def test_hard_context_threshold_rotates_before_api_error(env):
    """单轮工具循环内上下文接近窗口时，主动换新会话，而不是等 API 报错。"""
    conn, mgr, fake, log = env
    seen: list[tuple] = []
    mgr.on_context_overflow = lambda role, run_id, reason: seen.append((role, run_id, reason))
    run = runs_service.create_run(conn, "qa-role", task_id=None)
    _deliver(mgr, conn, fake, log, run["id"], "first",
             extra_env={"FAKE_CONTEXT_TOKENS": "950000"})
    assert _wait(lambda: seen), "应触发上下文硬阈值回调"
    assert seen[0][0] == "qa-role"
    assert seen[0][1] == run["id"]
    assert seen[0][2] == "threshold"
    assert _wait(lambda: mgr.status("qa-role")["alive"] is False)
    assert mgr._role("qa-role").cic_pending is True


@pytest.mark.parametrize("mode", ["plain", "synthetic", "result"])
def test_context_overflow_error_forms_trigger_recovery(env, mode):
    """各 CLI 输出形态（裸文本 / synthetic assistant / result）都要识别为溢出。"""
    conn, mgr, fake, log = env
    seen: list[tuple] = []
    mgr.on_context_overflow = lambda role, run_id, reason: seen.append((role, run_id, reason))
    run = runs_service.create_run(conn, "qa-role", task_id=None)
    _deliver(mgr, conn, fake, log, run["id"], "first",
             extra_env={"FAKE_CONTEXT_ERROR": mode})
    assert _wait(lambda: seen), f"{mode} 形态未触发溢出恢复"
    assert seen[0][1] == run["id"]
    assert seen[0][2] == "api_error"
    assert mgr._role("qa-role").cic_pending is True


def test_context_rotation_saves_knowledge_snapshot(env):
    """换会话前自动把任务决策/进度写入 notes/knowledge，供新会话恢复判断依据。"""
    conn, mgr, fake, log = env
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at,updated_at) "
        "VALUES ('T-K','OpenSSL audit','in_progress',?,?)", (now, now))
    conn.execute(
        "INSERT INTO workflows (id,task_id,definition_json,status,created_at,updated_at) "
        "VALUES ('W-K','T-K','{}','running',?,?)", (now, now))
    conn.execute(
        "INSERT INTO workflow_steps "
        "(workflow_id,step_id,title,role_slug,status,summary,updated_at) "
        "VALUES ('W-K','S1','基线核查','qa-role','approved','S1 通过：72 字段一致',?)",
        (now,))
    conn.commit()
    tasks_service.comment(conn, "T-K", "qa-role", "关键结论：strict 口径")

    run = runs_service.create_run(conn, "qa-role", task_id="T-K")
    _deliver(mgr, conn, fake, log, run["id"], "first", task_id="T-K",
             extra_env={"FAKE_CONTEXT_TOKENS": "850000"})
    assert _wait(lambda: _run_status(mgr.db_path, run["id"]) == "suspended")

    files = list((config.AGENTS_DIR / "qa-role" / "notes" / "knowledge").glob("**/*.md"))
    assert files, "高水位换会话前应写入知识快照"
    body = files[0].read_text(encoding="utf-8")
    assert "关键结论：strict 口径" in body
    assert "S1 通过：72 字段一致" in body


def test_payload_error_triggers_fresh_cic(env):
    """request too large (32MB 图片/附件) 也要识别，并换新会话。"""
    conn, mgr, fake, log = env
    seen: list[tuple] = []
    mgr.on_context_overflow = lambda role, run_id, reason: seen.append((role, run_id, reason))
    run = runs_service.create_run(conn, "qa-role", task_id=None)
    _deliver(mgr, conn, fake, log, run["id"], "first",
             extra_env={"FAKE_CONTEXT_ERROR": "payload"})
    assert _wait(lambda: seen), "payload 超限应触发恢复"
    assert seen[0][1] == run["id"]
    assert seen[0][2] == "payload"
    assert mgr._role("qa-role").cic_pending is True
    assert mgr._role("qa-role").session_id is None  # 强制新会话，不能再 resume


def test_payload_guard_rotates_before_api_error(env, monkeypatch):
    """累计图片/附件达到阈值时主动换会话，不等 32MB 报错。"""
    monkeypatch.setattr(config, "PAYLOAD_HARD_RESET_CHARS", 1000)
    conn, mgr, fake, log = env
    seen: list[tuple] = []
    mgr.on_context_overflow = lambda role, run_id, reason: seen.append((role, run_id, reason))
    run = runs_service.create_run(conn, "qa-role", task_id=None)
    _deliver(mgr, conn, fake, log, run["id"], "first",
             extra_env={"FAKE_ATTACHMENT_CHARS": "2000"})
    assert _wait(lambda: seen), "附件累计超阈值应主动轮换"
    assert seen[0][2] == "payload"
    assert mgr._role("qa-role").cic_pending is True


def test_new_session_clears_payload_counter(env, monkeypatch):
    monkeypatch.setattr(config, "PAYLOAD_HARD_RESET_CHARS", 10_000_000)
    conn, mgr, fake, log = env
    run = runs_service.create_run(conn, "qa-role", task_id=None)
    _deliver(mgr, conn, fake, log, run["id"], "first",
             extra_env={"FAKE_ATTACHMENT_CHARS": "5000"})
    assert _wait(lambda: _run_status(mgr.db_path, run["id"]) == "suspended")
    assert mgr._role("qa-role").payload_chars >= 5000
    mgr.reset_session("qa-role", cic=True)
    _deliver(mgr, conn, fake, log, run["id"], "second")
    assert _wait(lambda: _run_status(mgr.db_path, run["id"]) == "suspended")
    assert mgr._role("qa-role").payload_chars == 0


def test_api_error_adapts_context_window_downward(env):
    """API 报错说明真实窗口可能更小：按最后成功上下文收紧阈值，避免反复撞墙。"""
    conn, mgr, fake, log = env
    run = runs_service.create_run(conn, "qa-role", task_id=None)
    _deliver(mgr, conn, fake, log, run["id"], "first", extra_env={
        "FAKE_CONTEXT_TOKENS": "850000",
        "FAKE_CONTEXT_ERROR": "plain",
    })
    assert _wait(lambda: mgr._role("qa-role").cic_pending is True)
    st = mgr.status("qa-role")
    assert st["active_context_window"] == 807_500
    assert st["hard_limit_tokens"] < 800_000


def test_context_phrases_in_normal_output_do_not_trigger_recovery(env):
    """警告/最终回答里提到上下文措辞不能误判为溢出（否则正常会话会被反复终止）。"""
    conn, mgr, fake, log = env
    seen: list[tuple] = []
    mgr.on_context_overflow = lambda role, run_id, reason: seen.append((role, run_id, reason))
    run = runs_service.create_run(conn, "qa-role", task_id=None)
    _deliver(mgr, conn, fake, log, run["id"], "first", extra_env={
        "FAKE_WARN_LINE": (
            "Claude Code doesn't recognize the model, so it assumes a context "
            "window of 1000000 tokens."),
        "FAKE_RESULT_TEXT": (
            "分析结论：prompt is too long 是上下文窗口 it assumes 导致的。"),
    })
    assert _wait(lambda: _run_status(mgr.db_path, run["id"]) == "suspended")
    assert seen == []
    assert mgr.status("qa-role")["alive"] is True


def test_high_context_after_turn_starts_fresh_session_next_wake(env):
    """轮次结束时已过高水位：主动释放进程，下一次唤醒用 CIC 新会话，不续用大上下文。"""
    conn, mgr, fake, log = env
    run = runs_service.create_run(conn, "qa-role", task_id=None)
    _deliver(mgr, conn, fake, log, run["id"], "first",
             extra_env={"FAKE_CONTEXT_TOKENS": "850000"})
    assert _wait(lambda: _run_status(mgr.db_path, run["id"]) == "suspended")
    assert _wait(lambda: mgr.status("qa-role")["alive"] is False), "高水位应释放进程"
    assert mgr._role("qa-role").cic_pending is True

    _deliver(mgr, conn, fake, log, run["id"], "second",
             extra_env={"FAKE_CONTEXT_TOKENS": "850000"})
    assert _wait(lambda: _run_status(mgr.db_path, run["id"]) == "suspended")
    modes = [line.split()[0] for line in log.read_text().splitlines()]
    sids = [line.split()[1] for line in log.read_text().splitlines()]
    assert modes == ["new", "new"]
    assert sids[0] != sids[1]


def test_same_process_reused_and_resume_after_exit(env):
    conn, mgr, fake, log = env
    db_path = mgr.db_path
    run = runs_service.create_run(conn, "qa-role", task_id=None)
    runs_service.set_status(conn, run["id"], "running")  # 等价于 spawn_node 的状态流转

    # 第一次唤醒：新建会话进程
    _deliver(mgr, conn, fake, log, run["id"], "first")
    assert _wait(lambda: _run_status(db_path, run["id"]) == "suspended"), "一轮结束应转 suspended"
    st1 = mgr.status("qa-role")
    assert st1["alive"] and st1["status"] == "idle"
    assert st1["model_id"] == "fake-1m"
    assert st1["context_window"] == 1_000_000
    pid1 = st1["pid"]

    # 第二次唤醒：复用同一进程（核心要求：不新起进程）
    _deliver(mgr, conn, fake, log, run["id"], "second")
    assert _wait(lambda: _run_status(db_path, run["id"]) == "suspended")
    st2 = mgr.status("qa-role")
    assert st2["alive"] and st2["pid"] == pid1

    # 空闲释放后再次唤醒：--resume 同一 session_id 恢复上下文
    mgr.kill("qa-role")
    assert _wait(lambda: not mgr.status("qa-role")["alive"])
    _deliver(mgr, conn, fake, log, run["id"], "third")
    assert _wait(lambda: _run_status(db_path, run["id"]) == "suspended")
    # run 日志记录完整上下文与模型/工具交互
    c = open_conn(db_path)
    run_row = runs_service.get_run(c, run["id"])
    c.close()
    run_log = open(run_row["log_path"], encoding="utf-8").read()
    assert "USER（上下文/提示词）" in run_log and "first" in run_log
    assert "[tool_use] Bash" in run_log
    assert "[tool_result] file.txt" in run_log
    assert "[result]" in run_log

    modes = [line.split()[0] for line in log.read_text().splitlines()]
    assert modes == ["new", "resume"]
    sids = {line.split()[1] for line in log.read_text().splitlines()}
    assert len(sids) == 1  # 同一会话 id


def _session_row(db_path, role="qa-role"):
    c = open_conn(db_path)
    try:
        return c.execute(
            "SELECT session_id, status FROM role_sessions WHERE role_slug=?", (role,)
        ).fetchone()
    finally:
        c.close()


def _deliver_script(mgr, conn, script, log, run_id, text="hello"):
    runs_service.set_status(conn, run_id, "running")
    return mgr.ensure_and_deliver(
        conn, "qa-role",
        executable=sys.executable,
        args_new=[str(script), "--session-id", "{{session_id}}"],
        args_resume=[str(script), "--resume", "{{session_id}}"],
        ctx={}, cwd=str(script.parent), env={"FAKE_ARGV_LOG": str(log)},
        prompt=text, run_id=run_id, task_id=None,
    )


def test_reset_session_starts_new_context(env):
    conn, mgr, fake, log = env
    run = runs_service.create_run(conn, "qa-role", task_id=None)
    _deliver(mgr, conn, fake, log, run["id"], "first")
    assert _wait(lambda: _run_status(mgr.db_path, run["id"]) == "suspended")
    sid1 = mgr.status("qa-role")["session_id"]

    mgr.reset_session("qa-role")
    assert _wait(lambda: not mgr.status("qa-role")["alive"])
    row = _session_row(mgr.db_path)
    assert row["session_id"] == ""
    assert row["status"] == "dead"

    _deliver(mgr, conn, fake, log, run["id"], "second")
    assert _wait(lambda: _run_status(mgr.db_path, run["id"]) == "suspended")
    modes = [line.split()[0] for line in log.read_text().splitlines()]
    sids = [line.split()[1] for line in log.read_text().splitlines()]
    assert modes == ["new", "new"]
    assert len(set(sids)) == 2
    assert sid1 not in sids[1:]


def test_context_hint_warns_when_attachments_near_budget(env):
    """附件接近预算时，唤醒提示要提醒角色自律：别带大图、先固化结论。"""
    conn, mgr, fake, log = env
    r = mgr._role("qa-role")
    r.model_id = "test-200k"
    r.context_tokens = 100
    r.payload_chars = 9_000_000
    hint = mgr._context_hint(r, None)
    assert "图片/附件" in hint
    assert "预算" in hint
    assert "片段读取" in hint
    assert "memory/notes" in hint


def test_context_hint_only_on_task_switch_or_high_usage(env):
    conn, mgr, fake, log = env
    r = mgr._role("qa-role")
    r.model_id = "test-200k"
    r.context_tokens = 100
    r.last_task_id = "T1"
    r.last_step_id = "S1"
    assert mgr._context_hint(r, "T1") == ""

    # 200k 窗口 × 50% = 100k；120k 触发高占用提示
    r.context_tokens = 120000
    hint = mgr._context_hint(r, "T2")
    assert "上一次处理：T1 / 步骤 S1" in hint
    assert "当前任务：T2" in hint
    assert "模型窗口约 200k" in hint
    assert "Anvil 冷唤醒阈值 160k" in hint
    assert "reset-context --task T2" in hint
    assert "memory append" in hint  # 重置前先固化长期知识
    assert "任务已切换" in hint and "上下文占用较高" in hint

    same = mgr._context_hint(r, "T1")
    assert "上下文占用较高" in same
    assert "任务已切换" not in same

    # 模型池更小时，提示按保守窗口计算
    r.model_pool = ["flash-128k"]
    r.context_tokens = 70000
    pooled = mgr._context_hint(r, "T2")
    assert "按模型池/下限保守预算 128k" in pooled
    assert "Anvil 冷唤醒阈值 102k" in pooled


def test_model_switch_updates_window_and_audits(env):
    conn, mgr, fake, log = env
    run = runs_service.create_run(conn, "qa-role", task_id=None)
    _deliver(mgr, conn, fake, log, run["id"], "first",
             extra_env={"FAKE_SWITCH_MODEL": "fake-200k"})
    assert _wait(lambda: _run_status(mgr.db_path, run["id"]) == "suspended")
    st = mgr.status("qa-role")
    assert st["model_id"] == "fake-1m"
    assert st["context_window"] == 1_000_000

    _deliver(mgr, conn, fake, log, run["id"], "second",
             extra_env={"FAKE_SWITCH_MODEL": "fake-200k"})
    assert _wait(lambda: _run_status(mgr.db_path, run["id"]) == "suspended")
    st = mgr.status("qa-role")
    assert st["model_id"] == "fake-200k"
    assert st["served_model_id"] == "fake-200k"
    assert st["context_window"] == 200_000

    c = open_conn(mgr.db_path)
    try:
        row = c.execute(
            "SELECT payload_json FROM audit WHERE role_slug='qa-role' "
            "AND action='session.model_switch' ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        c.close()
    assert row is not None
    assert '"to": "fake-200k"' in row["payload_json"]


def test_model_variant_suffix_is_not_treated_as_switch(env):
    """init 的 deepseek-flash[1m] 与 served 的 deepseek-flash 同族，保留 1M 窗口。"""
    conn, mgr, fake, log = env
    run = runs_service.create_run(conn, "qa-role", task_id=None)
    envs = {"FAKE_MODEL": "deepseek-flash[1m]",
            "FAKE_SWITCH_MODEL": "deepseek-flash"}
    _deliver(mgr, conn, fake, log, run["id"], "first", extra_env=envs)
    assert _wait(lambda: _run_status(mgr.db_path, run["id"]) == "suspended")
    _deliver(mgr, conn, fake, log, run["id"], "second", extra_env=envs)
    assert _wait(lambda: _run_status(mgr.db_path, run["id"]) == "suspended")

    st = mgr.status("qa-role")
    assert st["model_id"] == "deepseek-flash[1m]"
    assert st["served_model_id"] == "deepseek-flash"
    assert st["context_window"] == 1_000_000

    c = open_conn(mgr.db_path)
    try:
        row = c.execute(
            "SELECT 1 FROM audit WHERE role_slug='qa-role' "
            "AND action='session.model_switch' LIMIT 1").fetchone()
    finally:
        c.close()
    assert row is None


def test_model_context_window_and_thresholds(env, monkeypatch):
    conn, mgr, fake, log = env
    r = mgr._role("qa-role")

    r.model_id = "deepseek-flash[1m]"
    assert mgr._context_window(r) == 1_000_000
    assert mgr._cic_threshold(r) == 800_000
    assert mgr._hint_threshold(r) == 500_000

    # 模型池里最小的模型决定保守策略窗口
    r.model_pool = ["flash-128k"]
    assert mgr._context_window(r) == 128_000
    assert mgr._cic_threshold(r) == 102_400
    assert mgr._hint_threshold(r) == 64_000
    r.model_pool = []

    # 角色级窗口下限
    r.context_window_floor = 64_000
    assert mgr._context_window(r) == 64_000
    r.context_window_floor = None

    r.model_id = "unknown-model"
    assert mgr._context_window(r) == config.DEFAULT_CONTEXT_WINDOW

    monkeypatch.setattr(config, "MODEL_CONTEXT_WINDOWS", {"custom-model": 100_000})
    r.model_id = "custom-model"
    assert mgr._context_window(r) == 100_000

    monkeypatch.setattr(config, "CIC_THRESHOLD_TOKENS", 128_000)
    assert mgr._cic_threshold(r) == 128_000


def test_reset_session_with_cic_starts_new_context(env):
    conn, mgr, fake, log = env
    run = runs_service.create_run(conn, "qa-role", task_id=None)
    _deliver(mgr, conn, fake, log, run["id"], "first")
    assert _wait(lambda: _run_status(mgr.db_path, run["id"]) == "suspended")
    sid1 = mgr.status("qa-role")["session_id"]

    mgr.reset_session("qa-role", cic=True)
    assert _wait(lambda: not mgr.status("qa-role")["alive"])
    assert mgr._role("qa-role").cic_pending is True

    _deliver(mgr, conn, fake, log, run["id"], "second")
    assert _wait(lambda: _run_status(mgr.db_path, run["id"]) == "suspended")
    modes = [line.split()[0] for line in log.read_text().splitlines()]
    sids = [line.split()[1] for line in log.read_text().splitlines()]
    assert modes == ["new", "new"]
    assert sids[0] != sids[1]
    assert sid1 == sids[0]


def test_restart_process_preserves_session(env):
    conn, mgr, fake, log = env
    run = runs_service.create_run(conn, "qa-role", task_id=None)
    _deliver(mgr, conn, fake, log, run["id"], "first")
    assert _wait(lambda: _run_status(mgr.db_path, run["id"]) == "suspended")
    sid1 = mgr.status("qa-role")["session_id"]

    mgr.restart_process("qa-role")
    assert _wait(lambda: not mgr.status("qa-role")["alive"])

    _deliver(mgr, conn, fake, log, run["id"], "second")
    assert _wait(lambda: _run_status(mgr.db_path, run["id"]) == "suspended")
    modes = [line.split()[0] for line in log.read_text().splitlines()]
    sids = [line.split()[1] for line in log.read_text().splitlines()]
    assert modes == ["new", "resume"]
    assert sids == [sid1, sid1]


def test_delete_task_kills_persistent_role_process(env, monkeypatch):
    """删除任务必须终止常驻角色进程（native_session_id=rs:<role>）。"""
    conn, mgr, fake, log = env
    from anvil.services import daemon as daemon_service
    from anvil.services import tasks as tasks_service

    monkeypatch.setattr(daemon_service, "get_manager", lambda: mgr)
    task = tasks_service.create(conn, title="delete me")
    run = runs_service.create_run(conn, "qa-role", task_id=task["id"])
    runs_service.mark_spawned(conn, run["id"], "rs:qa-role")
    _deliver(mgr, conn, fake, log, run["id"], "work", task_id=task["id"])
    assert _wait(lambda: mgr.status("qa-role")["alive"])
    # 一轮结束后进程空闲；删除任务也应立即释放它。
    assert _wait(lambda: mgr.status("qa-role")["status"] == "idle")

    tasks_service.delete(conn, task["id"], force=True)

    assert tasks_service.get(conn, task["id"]) is None
    assert _wait(lambda: not mgr.status("qa-role")["alive"])


def test_delete_completed_task_kills_idle_persistent_role_process(env, monkeypatch):
    """删除已完成任务也应释放该任务的空闲角色进程（无需 force）。"""
    conn, mgr, fake, log = env
    from anvil.services import daemon as daemon_service
    from anvil.services import tasks as tasks_service

    monkeypatch.setattr(daemon_service, "get_manager", lambda: mgr)
    task = tasks_service.create(conn, title="completed task")
    run = runs_service.create_run(conn, "qa-role", task_id=task["id"])
    runs_service.mark_spawned(conn, run["id"], "rs:qa-role")
    _deliver(mgr, conn, fake, log, run["id"], "work", task_id=task["id"])
    assert _wait(lambda: mgr.status("qa-role")["status"] == "idle")
    runs_service.set_status(conn, run["id"], "completed")

    tasks_service.delete(conn, task["id"])

    assert tasks_service.get(conn, task["id"]) is None
    assert _wait(lambda: not mgr.status("qa-role")["alive"])


def test_cancel_task_keeps_persistent_role_process(env):
    """cancel 可 reopen，故不杀常驻进程；reopen 后仍复用同一会话。"""
    conn, mgr, fake, log = env
    from anvil.services import tasks as tasks_service

    task = tasks_service.create(conn, title="cancel me")
    run = runs_service.create_run(conn, "qa-role", task_id=task["id"])
    runs_service.mark_spawned(conn, run["id"], "rs:qa-role")
    _deliver(mgr, conn, fake, log, run["id"], "work", task_id=task["id"])
    assert _wait(lambda: mgr.status("qa-role")["status"] == "idle")
    pid = mgr.status("qa-role")["pid"]
    sid = mgr.status("qa-role")["session_id"]

    tasks_service.cancel(conn, task["id"])

    assert tasks_service.get(conn, task["id"])["status"] == "closed"
    st = mgr.status("qa-role")
    assert st["alive"] and st["pid"] == pid

    tasks_service.reopen(conn, task["id"])
    assert tasks_service.get(conn, task["id"])["status"] == "todo"
    _deliver(mgr, conn, fake, log, run["id"], "again", task_id=task["id"])
    assert _wait(lambda: mgr.status("qa-role")["status"] == "idle")
    st = mgr.status("qa-role")
    assert st["alive"] and st["pid"] == pid and st["session_id"] == sid


FAIL_RUNTIME = '''
import json, os, sys
mode = "resume" if "--resume" in sys.argv else "new"
sid = sys.argv[sys.argv.index("--resume" if mode == "resume" else "--session-id") + 1]
with open(os.environ["FAKE_ARGV_LOG"], "a") as f:
    f.write(mode + " " + sid + "\\n")
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
for line in sys.stdin:
    sys.exit(2)
'''


def test_spawn_uses_binary_utf8_pipes(env, monkeypatch):
    """stdin 用 UTF-8 字节帧（LF），stdout 走二进制管道由 reader 统一解码。"""
    conn, mgr, fake, log = env
    captured: dict = {}

    class FakeProc:
        pid = 12345
        stdin = None
        stdout = io.BytesIO(b"")

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass

    def fake_popen(args, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr("anvil.services.daemon.subprocess.Popen", fake_popen)
    r = mgr._role("qa-role")
    mgr._spawn(conn, r, _SpawnSpec(
        executable=sys.executable,
        args_new=["--version"], args_resume=["--version"],
        ctx={}, cwd=str(fake.parent), env={},
    ))

    assert "text" not in captured or captured["text"] is False
    assert "encoding" not in captured
    assert captured["stdin"] == subprocess.PIPE
    assert captured["stdout"] == subprocess.PIPE
    frame = frame_user_message("中文").encode("utf-8")
    assert frame.endswith(b"\n") and b"\r\n" not in frame


def test_reader_crash_is_logged_and_process_terminated(env):
    """reader 解码/管道异常不能静默死掉；要记录 traceback 并终止子进程。"""
    conn, mgr, fake, log = env

    class BrokenStdout:
        def __iter__(self):
            return self

        def __next__(self):
            raise UnicodeDecodeError("gbk", b"\x80", 0, 1, "invalid start byte")

    class BrokenProc:
        pid = 1
        stdin = None
        stdout = BrokenStdout()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass

    r = mgr._role("qa-role")
    r.proc = BrokenProc()
    r.session_id = "sid"
    r.token = "tok"
    mgr._reader_loop(r)

    log_path = config.AGENTS_DIR / "qa-role" / "logs" / "role-session.log"
    text = log_path.read_text(encoding="utf-8")
    assert "reader crashed" in text
    assert "UnicodeDecodeError" in text


def test_failure_clears_session_for_next_wake(env, monkeypatch):
    conn, mgr, fake, log = env
    monkeypatch.setattr(config, "RESET_SESSION_ON_FAILURE", True)
    fail = fake.parent / "fail_runtime.py"
    fail.write_text(FAIL_RUNTIME, encoding="utf-8")

    run = runs_service.create_run(conn, "qa-role", task_id=None)
    _deliver_script(mgr, conn, fail, log, run["id"], "boom")
    assert _wait(lambda: not mgr.status("qa-role")["alive"])
    assert _wait(lambda: _session_row(mgr.db_path)["session_id"] == "")

    _deliver(mgr, conn, fake, log, run["id"], "retry")
    assert _wait(lambda: _run_status(mgr.db_path, run["id"]) == "suspended")
    modes = [line.split()[0] for line in log.read_text().splitlines()]
    assert modes == ["new", "new"]


# ---- 多任务上下文槽 ---------------------------------------------------------

def _role_log(run_id, conn):
    row = runs_service.get_run(conn, run_id)
    return open(row["log_path"], encoding="utf-8").read()


def _argv_lines(log):
    return [line.split() for line in log.read_text().splitlines() if line.strip()]


def test_task_switch_uses_separate_session_per_task(env):
    """A/B 两个任务各自新建会话；再回 A/B 时分别 resume 自己的 session。"""
    conn, mgr, fake, log = env
    ta = tasks_service.create(conn, title="Task A")
    tb = tasks_service.create(conn, title="Task B")
    ra = runs_service.create_run(conn, "qa-role", ta["id"])
    rb = runs_service.create_run(conn, "qa-role", tb["id"])

    _deliver(mgr, conn, fake, log, ra["id"], "A1", task_id=ta["id"])
    assert _wait(lambda: _run_status(mgr.db_path, ra["id"]) == "suspended")
    assert mgr.status("qa-role")["context_task_id"] == ta["id"]

    _deliver(mgr, conn, fake, log, rb["id"], "B1", task_id=tb["id"])
    assert _wait(lambda: _run_status(mgr.db_path, rb["id"]) == "suspended")
    assert mgr.status("qa-role")["context_task_id"] == tb["id"]

    lines = _argv_lines(log)
    assert [x[0] for x in lines] == ["new", "new"]
    sid_a, sid_b = lines[0][1], lines[1][1]
    assert sid_a != sid_b

    _deliver(mgr, conn, fake, log, ra["id"], "A2", task_id=ta["id"])
    assert _wait(lambda: _run_status(mgr.db_path, ra["id"]) == "suspended")
    assert _argv_lines(log)[2] == ["resume", sid_a]

    _deliver(mgr, conn, fake, log, rb["id"], "B2", task_id=tb["id"])
    assert _wait(lambda: _run_status(mgr.db_path, rb["id"]) == "suspended")
    assert _argv_lines(log)[3] == ["resume", sid_b]

    c = open_conn(mgr.db_path)
    try:
        rows = {
            r["task_key"]: r["session_id"]
            for r in c.execute(
                "SELECT task_key, session_id FROM role_task_sessions "
                "WHERE role_slug='qa-role'").fetchall()
        }
    finally:
        c.close()
    assert rows[ta["id"]] == sid_a
    assert rows[tb["id"]] == sid_b


def test_busy_role_queues_other_task_until_turn_end(env):
    """角色忙 A 时 B 的消息排队；A 的 turn 结束后才切换上下文处理 B。"""
    conn, mgr, fake, log = env
    ta = tasks_service.create(conn, title="Task A")
    tb = tasks_service.create(conn, title="Task B")
    ra = runs_service.create_run(conn, "qa-role", ta["id"])
    rb = runs_service.create_run(conn, "qa-role", tb["id"])

    _deliver(mgr, conn, fake, log, ra["id"], "A1", task_id=ta["id"],
             extra_env={"FAKE_TURN_DELAY": "1.0"})
    assert _wait(lambda: mgr.status("qa-role")["status"] == "busy", timeout=2)
    _deliver(mgr, conn, fake, log, rb["id"], "B1", task_id=tb["id"])
    st = mgr.status("qa-role")
    assert st["context_task_id"] == ta["id"]
    assert st["queue_len"] == 1
    assert st["queue"][0]["task_id"] == tb["id"]

    assert _wait(lambda: _run_status(mgr.db_path, rb["id"]) == "suspended", timeout=6)
    assert mgr.status("qa-role")["context_task_id"] == tb["id"]
    assert [x[0] for x in _argv_lines(log)] == ["new", "new"]


def test_reset_session_only_clears_target_task(env):
    """run/任务级 reset 只清该任务槽；角色在别的任务上的上下文保持可恢复。"""
    conn, mgr, fake, log = env
    ta = tasks_service.create(conn, title="Task A")
    tb = tasks_service.create(conn, title="Task B")
    ra = runs_service.create_run(conn, "qa-role", ta["id"])
    rb = runs_service.create_run(conn, "qa-role", tb["id"])

    _deliver(mgr, conn, fake, log, ra["id"], "A1", task_id=ta["id"])
    assert _wait(lambda: _run_status(mgr.db_path, ra["id"]) == "suspended")
    _deliver(mgr, conn, fake, log, rb["id"], "B1", task_id=tb["id"])
    assert _wait(lambda: _run_status(mgr.db_path, rb["id"]) == "suspended")
    sid_a, sid_b = [x[1] for x in _argv_lines(log)[:2]]

    # 当前加载 B；清 A 不应杀进程，也不应影响 B。
    mgr.reset_session("qa-role", task_id=ta["id"])
    assert mgr.status("qa-role")["alive"] is True
    assert mgr.status("qa-role")["context_task_id"] == tb["id"]

    c = open_conn(mgr.db_path)
    try:
        keys = {r["task_key"] for r in c.execute(
            "SELECT task_key FROM role_task_sessions WHERE role_slug='qa-role'").fetchall()}
    finally:
        c.close()
    assert ta["id"] not in keys and tb["id"] in keys

    _deliver(mgr, conn, fake, log, ra["id"], "A2", task_id=ta["id"])
    assert _wait(lambda: _run_status(mgr.db_path, ra["id"]) == "suspended")
    latest_a = [x for x in _argv_lines(log) if x[1] != sid_b][-1]
    assert latest_a[0] == "new"  # A 的旧上下文已清，不能 resume

    _deliver(mgr, conn, fake, log, rb["id"], "B2", task_id=tb["id"])
    assert _wait(lambda: _run_status(mgr.db_path, rb["id"]) == "suspended")
    assert _argv_lines(log)[-1] == ["resume", sid_b]


def test_idle_release_keeps_each_task_session_resumable(env):
    """空闲释放进程后，A/B 仍能各自 resume 自己的会话。"""
    conn, mgr, fake, log = env
    ta = tasks_service.create(conn, title="Task A")
    tb = tasks_service.create(conn, title="Task B")
    ra = runs_service.create_run(conn, "qa-role", ta["id"])
    rb = runs_service.create_run(conn, "qa-role", tb["id"])

    _deliver(mgr, conn, fake, log, ra["id"], "A1", task_id=ta["id"])
    assert _wait(lambda: _run_status(mgr.db_path, ra["id"]) == "suspended")
    _deliver(mgr, conn, fake, log, rb["id"], "B1", task_id=tb["id"])
    assert _wait(lambda: _run_status(mgr.db_path, rb["id"]) == "suspended")
    sid_a, sid_b = [x[1] for x in _argv_lines(log)[:2]]

    mgr.kill("qa-role")
    assert _wait(lambda: not mgr.status("qa-role")["alive"])

    _deliver(mgr, conn, fake, log, ra["id"], "A2", task_id=ta["id"])
    assert _wait(lambda: _run_status(mgr.db_path, ra["id"]) == "suspended")
    assert _argv_lines(log)[-1] == ["resume", sid_a]

    _deliver(mgr, conn, fake, log, rb["id"], "B2", task_id=tb["id"])
    assert _wait(lambda: _run_status(mgr.db_path, rb["id"]) == "suspended")
    assert _argv_lines(log)[-1] == ["resume", sid_b]


def test_first_entry_to_task_builds_cic_with_task_history(env):
    """首次进入某任务：新会话前缀包含该任务的消息（CIC），而不是 A 的上下文。"""
    conn, mgr, fake, log = env
    ta = tasks_service.create(conn, title="Task A")
    tb = tasks_service.create(conn, title="Task B")
    tasks_service.comment(conn, tb["id"], "human", "SPECIAL-B-CONTEXT")
    ra = runs_service.create_run(conn, "qa-role", ta["id"])
    rb = runs_service.create_run(conn, "qa-role", tb["id"])

    _deliver(mgr, conn, fake, log, ra["id"], "A1", task_id=ta["id"])
    assert _wait(lambda: _run_status(mgr.db_path, ra["id"]) == "suspended")
    _deliver(mgr, conn, fake, log, rb["id"], "B1", task_id=tb["id"])
    assert _wait(lambda: _run_status(mgr.db_path, rb["id"]) == "suspended")
    body = _role_log(rb["id"], conn)
    assert "SPECIAL-B-CONTEXT" in body
    assert "当前任务 ID：" + tb["id"] in body


def test_resume_task_injects_delta_since_last_visit(env):
    """回到任务 A 时恢复会话，并补上离开期间的新评论。"""
    conn, mgr, fake, log = env
    ta = tasks_service.create(conn, title="Task A")
    tb = tasks_service.create(conn, title="Task B")
    ra = runs_service.create_run(conn, "qa-role", ta["id"])
    rb = runs_service.create_run(conn, "qa-role", tb["id"])

    _deliver(mgr, conn, fake, log, ra["id"], "A1", task_id=ta["id"])
    assert _wait(lambda: _run_status(mgr.db_path, ra["id"]) == "suspended")
    _deliver(mgr, conn, fake, log, rb["id"], "B1", task_id=tb["id"])
    assert _wait(lambda: _run_status(mgr.db_path, rb["id"]) == "suspended")

    tasks_service.comment(conn, ta["id"], "human", "AFTER-LEAVING-A")
    _deliver(mgr, conn, fake, log, ra["id"], "A2", task_id=ta["id"])
    assert _wait(lambda: _run_status(mgr.db_path, ra["id"]) == "suspended")
    body = _role_log(ra["id"], conn)
    assert "你离开任务期间的新进展" in body
    assert "AFTER-LEAVING-A" in body


def test_cancel_active_task_continues_with_queued_other_task(env):
    """删除正忙任务 A：不能把已删槽又写回，且队列里的 B 应继续处理。"""
    conn, mgr, fake, log = env
    ta = tasks_service.create(conn, title="Task A")
    tb = tasks_service.create(conn, title="Task B")
    ra = runs_service.create_run(conn, "qa-role", ta["id"])
    rb = runs_service.create_run(conn, "qa-role", tb["id"])

    _deliver(mgr, conn, fake, log, ra["id"], "A1", task_id=ta["id"],
             extra_env={"FAKE_TURN_DELAY": "1.0"})
    assert _wait(lambda: mgr.status("qa-role")["status"] == "busy", timeout=2)
    _deliver(mgr, conn, fake, log, rb["id"], "B1", task_id=tb["id"])

    assert mgr.cancel_task("qa-role", ta["id"]) is True
    assert _wait(lambda: _run_status(mgr.db_path, rb["id"]) == "suspended", timeout=6)
    st = mgr.status("qa-role")
    assert st["context_task_id"] == tb["id"]

    c = open_conn(mgr.db_path)
    try:
        keys = {r["task_key"] for r in c.execute(
            "SELECT task_key FROM role_task_sessions WHERE role_slug='qa-role'").fetchall()}
    finally:
        c.close()
    assert ta["id"] not in keys  # 已删除任务不能被切回时重新写回
    assert tb["id"] in keys


def test_reminder_run_releases_temp_process(env):
    """知识管理提醒临时拉起的进程：本轮结束立即回收，run 标为 completed。"""
    conn, mgr, fake, log = env
    run = runs_service.create_run(conn, "qa-role", task_id=None, prompt="daily review")
    conn.execute(
        "INSERT INTO reminder_wakes (run_id, role_slug, was_alive, created_at) "
        "VALUES (?,?,?,?)",
        (run["id"], "qa-role", 0, "2026-10-24T09:00:00+08:00"),
    )
    conn.commit()
    _deliver(mgr, conn, fake, log, run["id"], "daily review")
    assert _wait(lambda: _run_status(mgr.db_path, run["id"]) == "completed")
    assert _wait(lambda: mgr.status("qa-role")["alive"] is False)
    assert mgr.status("qa-role")["current_run_id"] is None
    check = open_conn(mgr.db_path)
    try:
        left = check.execute(
            "SELECT COUNT(*) FROM reminder_wakes WHERE run_id=?", (run["id"],)
        ).fetchone()[0]
    finally:
        check.close()
    assert left == 0


def test_reminder_run_keeps_already_running_process(env):
    """触发前已在干活的角色：提醒结束不杀进程，继续原来的工作。"""
    conn, mgr, fake, log = env
    warm = runs_service.create_run(conn, "qa-role", task_id=None)
    _deliver(mgr, conn, fake, log, warm["id"], "warm")
    assert _wait(lambda: _run_status(mgr.db_path, warm["id"]) == "suspended")
    assert mgr.status("qa-role")["alive"] is True

    run = runs_service.create_run(conn, "qa-role", task_id=None, prompt="daily review")
    conn.execute(
        "INSERT INTO reminder_wakes (run_id, role_slug, was_alive, created_at) "
        "VALUES (?,?,?,?)",
        (run["id"], "qa-role", 1, "2026-10-24T09:00:00+08:00"),
    )
    conn.commit()
    _deliver(mgr, conn, fake, log, run["id"], "daily review")
    assert _wait(lambda: _run_status(mgr.db_path, run["id"]) == "completed")
    assert mgr.status("qa-role")["alive"] is True
