"""L3 · anvil CLI 契约测试：命令↔端点映射 + 退出码 0–4（design §7.2/§7.3，SE §4）。

实现：用 stdlib `http.server` 起一个 mock 后端（无额外依赖），记录 CLI 发出的
(method, path)，并可按用例强制返回状态码，据此断言 CLI 的映射与退出码语义。
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import threading
from urllib.parse import urlsplit

import pytest

from tests.conftest import API

ANVIL_BIN = os.environ.get("ANVIL_BIN", "anvil")

# 契约映射表（§7.2 命令 → REST）。args 为 CLI 调用参数（不含 `anvil` 前缀）。
CONTRACT = {
    "task list": (["task", "list"], "GET", f"{API}/tasks"),
    "task show": (["task", "show", "T-0001"], "GET", f"{API}/tasks/T-0001"),
    "task create": (["task", "create", "hello"], "POST", f"{API}/tasks"),
    "task claim": (["task", "claim", "T-0001"], "POST", f"{API}/tasks/T-0001/claim"),
    "task unclaim": (["task", "unclaim", "T-0001"], "POST", f"{API}/tasks/T-0001/unclaim"),
    "task heartbeat": (["task", "heartbeat", "T-0001"], "POST", f"{API}/tasks/T-0001/heartbeat"),
    "task submit": (["task", "submit", "T-0001", "done"], "POST", f"{API}/tasks/T-0001/submit"),
    "task comment": (["task", "comment", "T-0001", "x"], "POST", f"{API}/tasks/T-0001/comment"),
    "task block": (["task", "block", "T-0001", "--on", "T-0002"], "POST", f"{API}/tasks/T-0001/deps"),
    "memory append": (["memory", "append", "x"], "POST", f"{API}/me/memory"),
    "memory search": (["memory", "search", "x"], "GET", f"{API}/me/memory/search"),
    "note write": (["note", "write", "p"], "POST", f"{API}/me/notes"),
    "knowledge search": (["knowledge", "search", "jwt"], "GET",
                         f"{API}/me/knowledge/search"),
    "knowledge show": (["knowledge", "show", "--id", "n1"], "GET",
                       f"{API}/me/knowledge/n1"),
    "knowledge index": (["knowledge", "index"], "GET", f"{API}/me/knowledge/index"),
    "knowledge list": (["knowledge", "list"], "GET", f"{API}/me/knowledge/index"),
    "knowledge reindex": (["knowledge", "reindex"], "POST",
                          f"{API}/me/knowledge/reindex"),
    "lesson propose": (["lesson", "propose", "x"], "POST", f"{API}/lessons/pending"),
    "role list": (["role", "list"], "GET", f"{API}/roles"),
    "self show": (["self", "show"], "GET", f"{API}/me"),
    "self reset-context": (["self", "reset-context", "--task", "T-0001"],
                           "POST", f"{API}/me/session/reset"),
    "reminder list": (["reminder", "list"], "GET", f"{API}/reminders"),
    "reminder add": (["reminder", "add", "--cron", "0"], "POST", f"{API}/reminders"),
    "ws status": (["ws", "status"], "GET", f"{API}/workspaces/0/tree"),
    "task archive": (["task", "archive", "T-0001"], "POST",
                     f"{API}/tasks/T-0001/archive"),
    "task knowledge-list": (["task", "knowledge-list", "T-0001"], "GET",
                            f"{API}/tasks/T-0001/knowledge"),
    "task knowledge-write": (["task", "knowledge-write", "T-0001",
                              "--title", "t", "--tldr", "d", "--kind", "task_finding"],
                             "POST", f"{API}/tasks/T-0001/knowledge"),
    "task reviewers": (["task", "reviewers", "T-0001", "--assignees", "auditor"],
                       "POST", f"{API}/tasks/T-0001/reviewers"),
    "finding list": (["finding", "list"], "GET", f"{API}/tasks/T-0001/findings"),
    "finding show": (["finding", "show", "F-1"], "GET",
                     f"{API}/tasks/T-0001/findings/F-1"),
    "finding submit": (["finding", "submit", "F-1"], "POST",
                       f"{API}/tasks/T-0001/findings/F-1/submit"),
    "finding complete": (["finding", "complete", "--summary", "未发现安全问题"],
                         "POST", f"{API}/tasks/T-0001/findings/complete"),
    "finding delete": (["finding", "delete", "F-1"], "DELETE",
                       f"{API}/tasks/T-0001/findings/F-1"),
    "workspace knowledge list": (["workspace", "knowledge", "list", "W-1"], "GET",
                                 f"{API}/workspaces/W-1/knowledge"),
    "workspace knowledge propose": (["workspace", "knowledge", "propose", "W-1",
                                     "--title", "t", "--tldr", "d",
                                     "--kind", "convention"], "POST",
                                    f"{API}/workspaces/W-1/knowledge"),
    "workspace knowledge review": (["workspace", "knowledge", "review", "W-1"], "GET",
                                   f"{API}/workspaces/W-1/knowledge/review"),
    "knowledge card-template": (["knowledge", "card-template"], "GET",
                                f"{API}/knowledge/card/template"),
}

EXIT = {"ok": 0, "generic": 1, "usage": 2, "conflict": 3, "forbidden": 4}


class _Handler(http.server.BaseHTTPRequestHandler):
    """记录请求；按 forced_status 返回。"""

    forced_status = 200
    forced_body: dict | None = None
    requests: list[tuple[str, str]] = []
    bodies: list[dict | None] = []

    def _handle(self) -> None:
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        type(self).bodies.append(parsed)
        type(self).requests.append((self.command, urlsplit(self.path).path))
        payload = type(self).forced_body
        body = json.dumps({"ok": True} if payload is None else payload).encode()
        self.send_response(type(self).forced_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def mock_backend():
    _Handler.requests = []
    _Handler.bodies = []
    _Handler.forced_status = 200
    _Handler.forced_body = None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    t.join()


def _run(args: list[str], endpoint: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "ANVIL_ENDPOINT": endpoint,
        "ANVIL_TOKEN": "test-token",
        "ANVIL_AGENT_SLUG": "qa-role",
        "ANVIL_TASK_ID": "T-0001",
    }
    return subprocess.run(
        [ANVIL_BIN, *args], env=env, capture_output=True, text=True, timeout=30
    )


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
@pytest.mark.parametrize("name", sorted(CONTRACT))
def test_command_maps_to_endpoint(name, mock_backend):
    """每个命令命中正确 (method, path) 且成功退 0。"""
    args, method, path = CONTRACT[name]
    r = _run(args, mock_backend)
    assert r.returncode == EXIT["ok"], f"{name}: stderr={r.stderr!r}"
    assert (method, path) in _Handler.requests, f"{name}: 实际请求 {_Handler.requests}"


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_version_option_prints_package_version():
    """--version 不依赖后端，直接输出版本号。"""
    from pathlib import Path

    version = (Path(__file__).parents[2] / "VERSION").read_text().strip()
    env = {**os.environ, "ANVIL_ENDPOINT": "http://127.0.0.1:1"}
    r = subprocess.run([ANVIL_BIN, "--version"], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == EXIT["ok"], r.stderr
    assert version in r.stdout, (version, r.stdout)


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_unknown_task_action_lists_reviewers():
    """未知动作提示必须列出全部动作，包括新加的 reviewers。"""
    env = {**os.environ, "ANVIL_ENDPOINT": "http://127.0.0.1:1",
           "ANVIL_TASK_ID": "T-0001"}
    r = subprocess.run([ANVIL_BIN, "task", "bogus"], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == EXIT["usage"]
    assert "reviewers" in r.stderr, r.stderr


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_unknown_knowledge_action_shows_supported_actions():
    """拼错 action 时给出可用动作，避免 Agent 只拿到一句 unknown 后盲试。"""
    env = {**os.environ, "ANVIL_ENDPOINT": "http://127.0.0.1:1"}
    r = subprocess.run(
        [ANVIL_BIN, "knowledge", "bogus"], env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == EXIT["usage"]
    assert "unknown knowledge action: bogus" in r.stderr
    assert "search" in r.stderr and "list" in r.stderr


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_usage_errors_include_actionable_hint():
    """参数错误必须给 hint，避免 Agent 只看到一句 error 后盲试。"""
    env = {**os.environ, "ANVIL_ENDPOINT": "http://127.0.0.1:1", "ANVIL_TASK_ID": ""}
    cases = [
        (["task", "show"], "ANVIL_TASK_ID"),
        (["task", "bogus"], "anvil task -h"),
        (["task", "mention", "T-0001"], "--to"),
        (["knowledge", "bogus"], "anvil knowledge -h"),
    ]
    for args, needle in cases:
        r = subprocess.run([ANVIL_BIN, *args], env=env,
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == EXIT["usage"], (args, r.stderr)
        assert "error:" in r.stderr and "hint:" in r.stderr, (args, r.stderr)
        assert needle in r.stderr, (args, r.stderr)


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_typer_usage_error_appends_hint():
    """Typer 生成的缺参/错参错误也要追加 --help 提示。"""
    r = subprocess.run([ANVIL_BIN, "step", "plan"], capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == EXIT["usage"]
    assert "Missing argument" in r.stderr
    assert "hint:" in r.stderr and "anvil <command> -h" in r.stderr


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_help_contains_rules_and_examples():
    """--help 必须能直接指导执行，而不是只列参数名。"""
    cases = [
        (["task", "-h"], ["示例:", "knowledge-write", "anvil task create", "reviewers",
                          "--issue-board", "anvil task reviewers"]),
        (["step", "plan", "-h"], ["plan.json", "slices", "不得指定角色"]),
        (["role", "-h"], ["role list", "role expand"]),
        (["workflow", "revise", "-h"], ["revision.json", "--preview", "requires_restart"]),
        (["finding", "-h"], ["finding add", "finding complete", "问题板",
                             "anvil finding template"]),
        (["-h"], ["anvil finding template", "anvil task reviewers", "--version",
                  "问题板"]),
    ]
    for args, needles in cases:
        r = subprocess.run([ANVIL_BIN, *args], capture_output=True,
                           text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        for needle in needles:
            assert needle in r.stdout, (args, needle, r.stdout)


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_role_propose_creates_draft_and_mentions_human(mock_backend, tmp_path):
    """role propose = 提交 role_suggestion 草案 + @human，不直接创建角色。"""
    p = tmp_path / "roles.json"
    p.write_text(json.dumps({"suggested_roles": [
        {"slug": "auditor", "display_name": "审计员", "description": "负责审计"}
    ]}, ensure_ascii=False), encoding="utf-8")
    r = _run(["role", "propose", "--task", "T-0001", "--file", str(p)], mock_backend)
    assert r.returncode == EXIT["ok"], r.stderr
    assert ("POST", f"{API}/board/drafts") in _Handler.requests
    assert ("POST", f"{API}/tasks/T-0001/mention") in _Handler.requests
    assert "等待 human 审批" in r.stdout


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_help_has_no_unix_only_temp_paths():
    """示例不能写死 /tmp；Windows 没有该目录。"""
    for args in (["task", "-h"], ["step", "plan", "-h"],
                 ["role", "propose", "-h"], ["note", "-h"]):
        r = subprocess.run([ANVIL_BIN, *args], capture_output=True,
                           text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        assert "/tmp/" not in r.stdout, (args, "/tmp example in help")


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_task_comment_accepts_message_option(mock_backend):
    """comment 除位置参数外接受 --message，避免 Agent 凭直觉传参失败。"""
    r = _run(["task", "comment", "T-0001", "--message", "hello"], mock_backend)
    assert r.returncode == EXIT["ok"], r.stderr
    assert _Handler.bodies[-1] == {"body": "hello"}


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_task_mention_accepts_message_option(mock_backend):
    """mention 同样接受 --message 作为正文。"""
    r = _run(["task", "mention", "T-0001", "--to", "qa-role",
              "--message", "hi"], mock_backend)
    assert r.returncode == EXIT["ok"], r.stderr
    assert _Handler.bodies[-1] == {"target": "qa-role", "message": "hi"}


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_task_create_issue_board_flag(mock_backend):
    """--issue-board 必须映射为 issue_board_enabled，而不是被静默忽略。"""
    r = _run(["task", "create", "安全审计", "--issue-board"], mock_backend)
    assert r.returncode == EXIT["ok"], r.stderr
    assert _Handler.bodies[-1]["issue_board_enabled"] is True


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_finding_add_and_update_read_json_file(mock_backend, tmp_path):
    """finding add/update 从 --file 读取结构化字段；add --submit 继续提交。"""
    p = tmp_path / "finding.json"
    p.write_text(json.dumps({"title": "命令注入", "severity": "high"},
                            ensure_ascii=False), encoding="utf-8")
    _Handler.forced_body = {"id": "F-123", "status": "draft"}
    r = _run(["finding", "add", "--task", "T-0001", "--file", str(p), "--submit"],
             mock_backend)
    assert r.returncode == EXIT["ok"], r.stderr
    assert ("POST", f"{API}/tasks/T-0001/findings") in _Handler.requests
    assert ("POST", f"{API}/tasks/T-0001/findings/F-123/submit") in _Handler.requests
    assert _Handler.bodies[0] == {"title": "命令注入", "severity": "high"}

    r = _run(["finding", "update", "F-123", "--task", "T-0001", "--file", str(p)],
             mock_backend)
    assert r.returncode == EXIT["ok"], r.stderr
    assert ("PATCH", f"{API}/tasks/T-0001/findings/F-123") in _Handler.requests


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_finding_template_contains_required_fields():
    """模板必须包含全部必填字段与枚举，reviewer 才能直接照着填。"""
    env = {**os.environ, "ANVIL_ENDPOINT": "http://127.0.0.1:1"}
    r = subprocess.run([ANVIL_BIN, "finding", "template"], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == EXIT["ok"], r.stderr
    for key in ("risk_type", "severity", "attack_path", "reasoning_chain",
                "poc_status", "fix_priority", "confidence", "line_numbers"):
        assert key in r.stdout, (key, r.stdout)
    assert "critical" in r.stdout and "not_verified" in r.stdout


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_exit_code_conflict_409(mock_backend):
    """409 → 退出码 3（认领失败/依赖未满足，§7.3 关键区分）。"""
    _Handler.forced_status = 409
    r = _run(["task", "claim", "T-0001"], mock_backend)
    assert r.returncode == EXIT["conflict"]


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_exit_code_forbidden_403(mock_backend):
    """403 → 退出码 4（无权限/scope 越界）。"""
    _Handler.forced_status = 403
    r = _run(["task", "submit", "T-0001", "x"], mock_backend)
    assert r.returncode == EXIT["forbidden"]


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_exit_code_bad_request_422(mock_backend):
    """422 → 退出码 2（参数/预算超限）。"""
    _Handler.forced_status = 422
    r = _run(["task", "create", "x"], mock_backend)
    assert r.returncode == EXIT["usage"]


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_exit_code_usage_missing_arg():
    """缺任务 id → 退出码 2（Typer 参数错误，不走网络）。"""
    env = {**os.environ, "ANVIL_ENDPOINT": "http://127.0.0.1:1", "ANVIL_TASK_ID": ""}
    r = subprocess.run(
        [ANVIL_BIN, "task", "claim"], env=env, capture_output=True, text=True, timeout=30
    )
    assert r.returncode == EXIT["usage"]


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_workflow_revise_preview_maps_to_endpoint(mock_backend, tmp_path):
    """修订预览只调用 preview 端点，不创建草案。"""
    intent = {"add_slices": [{"id": "auth2", "title": "认证补充"}]}
    p = tmp_path / "revision.json"
    p.write_text(json.dumps({"rationale": "补切片", "intent": intent},
                            ensure_ascii=False), encoding="utf-8")
    r = _run(["workflow", "revise", "WF-1", "--file", str(p), "--preview"],
             mock_backend)
    assert r.returncode == EXIT["ok"], r.stderr
    assert _Handler.requests == [
        ("POST", f"{API}/workflows/WF-1/revisions/preview")]
    assert _Handler.bodies[-1] == {"intent": intent}


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_workflow_revise_creates_revision_draft(mock_backend, tmp_path):
    """不带 --preview 时提交修订草案，并带上 rationale。"""
    intent = {"patch_steps": [{"step_id": "F1", "fields": {"instructions": "x"}}]}
    p = tmp_path / "revision.json"
    p.write_text(json.dumps({"intent": intent}), encoding="utf-8")
    r = _run(["workflow", "revise", "WF-1", "--file", str(p),
              "--rationale", "补说明"], mock_backend)
    assert r.returncode == EXIT["ok"], r.stderr
    assert ("POST", f"{API}/workflows/WF-1/revisions") in _Handler.requests
    assert _Handler.bodies[-1] == {"intent": intent, "rationale": "补说明"}
    assert "不重启" in r.stdout


@pytest.mark.skipif(not os.environ.get("ANVIL_BIN"), reason="设 ANVIL_BIN 后跑 L3")
def test_step_show_prints_pending_revision(mock_backend):
    """step show 要能看到待审批修订及影响摘要。"""
    _Handler.forced_body = {
        "status": "running",
        "updated_at": "2026-10-29T10:00:00+00:00",
        "steps": [],
        "pending_revision": {
            "draft_id": "D-REV",
            "impact": {"classification": "safe_loop", "summary": "追加 1 个切片"},
        },
    }
    r = _run(["step", "show", "WF-1"], mock_backend)
    assert r.returncode == EXIT["ok"], r.stderr
    assert "待审批修订：D-REV · safe_loop · 追加 1 个切片" in r.stdout
