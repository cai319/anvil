"""anvil CLI (Typer) — maps §7.2 commands to the REST API (§4 / SE v1.0).

Exit codes (design §7.3): 0 ok · 1 generic · 2 usage · 3 conflict · 4 forbidden.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx
import typer

app = typer.Typer(help="Anvil 本地多 Agent 编排服务 CLI", no_args_is_help=True)

ENDPOINT = os.environ.get("ANVIL_ENDPOINT", "http://127.0.0.1:7317")
# 手动运行时（未注入 ANVIL_TOKEN）也带一个非空 session 头，满足本地轻校验；
# 配置了 ANVIL_AUTH_TOKEN 的环境需显式设置 ANVIL_TOKEN。
TOKEN = os.environ.get("ANVIL_TOKEN", "") or "anvil"
AGENT_SLUG = os.environ.get("ANVIL_AGENT_SLUG", "")
DEFAULT_TASK = os.environ.get("ANVIL_TASK_ID", "")


def _client() -> httpx.Client:
    return httpx.Client(base_url=ENDPOINT, timeout=30.0)


def _headers() -> dict[str, str]:
    h = {}
    if TOKEN:
        h["X-Anvil-Session"] = TOKEN
    if AGENT_SLUG:
        h["X-Anvil-Role"] = AGENT_SLUG
    return h


def _exit_for(status: int) -> int:
    if 200 <= status < 300:
        return 0
    if status in (400, 422):
        return 2
    if status in (401, 403):
        return 4
    if status == 409:
        return 3
    return 1


def _call(method: str, path: str, body: Any = None, params: dict | None = None) -> Any:
    try:
        with _client() as c:
            r = c.request(method, path, json=body, params=params, headers=_headers())
    except httpx.HTTPError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    if r.status_code >= 300:
        typer.echo(r.text, err=True)
        raise typer.Exit(_exit_for(r.status_code))
    if not r.content:
        return None
    return r.json()


def _emit(data: Any, as_json: bool) -> None:
    if as_json or data is None or isinstance(data, (dict, list)):
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        typer.echo(data)


# ---- self ---------------------------------------------------------------


@app.command("init")
def init_cmd(
    json_out: bool = typer.Option(False, "--json"),
    with_roles: bool = typer.Option(False, "--with-roles", help="创建默认角色模板"),
    workspace: str = typer.Option("main", "--workspace", help="默认频道/工作区名称"),
):
    """首启引导：发现运行时 → 建默认频道 → （可选）导入角色模板。"""
    try:
        runtimes = _call("POST", "/api/v1/runtimes/discover") or []
    except typer.Exit:
        typer.echo("后端未启动：请先双击 anvil-server 再运行 anvil init。", err=True)
        raise
    ready = [r for r in runtimes if r.get("status") == "ready"]
    typer.echo(f"发现 {len(runtimes)} 个运行时，其中 {len(ready)} 个 ready：")
    for rt in runtimes:
        typer.echo(f"  - {rt.get('display_name')} ({rt.get('id')}) → {rt.get('status')}")
    if not ready:
        typer.echo("提示：没有 ready 的运行时，可用 `anvil` 手工注册或安装后重试。", err=True)

    if not ready:
        return
    runtime_id = ready[0]["id"]

    channels = _call("GET", "/api/v1/workspaces") or []
    if not any(c.get("name") == workspace for c in channels):
        _call("POST", "/api/v1/workspaces", {"name": workspace})
        typer.echo(f"已创建默认频道：{workspace}")
    else:
        typer.echo(f"频道已存在：{workspace}")

    if with_roles:
        defaults = [
            ("developer", "开发工程师"),
            ("tester", "测试工程师"),
            ("reviewer", "评审员"),
        ]
        for slug, name in defaults:
            if _role_exists(slug):
                continue
            _call("POST", "/api/v1/roles", {
                "slug": slug, "display_name": name,
                "runtime": {"id": runtime_id},
                "workspace": {"home": f"agents/{slug}/", "repos": []},
                "tools": {"preset": "backend-default", "max_tool_tokens": 3500},
                "concurrency": {"max_parallel": 2},
            })
            typer.echo(f"  已导入角色：{name} ({slug})")

    typer.echo("下一步：打开界面创建第一个任务，或 `anvil task create --title ...`。")


def _role_exists(slug: str) -> bool:
    try:
        with _client() as c:
            return c.get(f"/api/v1/roles/{slug}", headers=_headers()).status_code < 300
    except httpx.HTTPError:
        return False


@app.command("self")
def self_cmd(
    action: str = typer.Argument("show"),
    file: str = typer.Option(None, "--file", help="instruction / proposal 内容文件"),
    field: str = typer.Option(None, "--field", help="propose 的字段（tools/responsibilities/...）"),
    rationale: str = typer.Option(None, "--rationale", help="提案依据"),
    risk: str = typer.Option(None, "--risk", help="提案风险"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON"),
):
    """anvil self <show|edit-instruction|propose>"""
    if action == "show":
        _emit(_call("GET", "/api/v1/me"), json_out)
    elif action == "edit-instruction":
        if not file:
            typer.echo("self edit-instruction 需要 --file", err=True)
            raise typer.Exit(2)
        try:
            content = open(file, encoding="utf-8").read()
        except OSError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2)
        _emit(_call("POST", "/api/v1/me/instruction", {"content": content}), json_out)
    elif action == "propose":
        if not field or not file:
            typer.echo("self propose 需要 --field 与 --file", err=True)
            raise typer.Exit(2)
        try:
            proposed = json.loads(open(file, encoding="utf-8").read())
        except (OSError, json.JSONDecodeError) as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2)
        _emit(_call("POST", "/api/v1/proposals", {
            "field": field, "proposed": proposed,
            "rationale": rationale, "risk": risk,
        }), json_out)
    else:
        typer.echo(f"unknown self action: {action}", err=True)
        raise typer.Exit(2)


@app.command("whoami")
def whoami_cmd(json_out: bool = typer.Option(False, "--json", help="输出 JSON")):
    """anvil whoami — self show 的别名（M1 出口命令）。"""
    _emit(_call("GET", "/api/v1/me"), json_out)


# ---- task ---------------------------------------------------------------


@app.command("task")
def task_cmd(
    action: str = typer.Argument("list"),
    target: str = typer.Argument(None),
    body: str = typer.Argument(None),
    on: str = typer.Option(None, "--on", help="依赖的任务 id"),
    to: str = typer.Option(None, "--to", help="@提及的目标角色"),
    type_: str = typer.Option(None, "--type", help="任务类型"),
    tag: str = typer.Option(None, "--tag", help="按标签过滤 list"),
    parent: str = typer.Option(None, "--parent", help="父任务 id"),
    title: str = typer.Option(None, "--title", help="任务标题（create 用）"),
    description: str = typer.Option(None, "--description", help="任务描述（create 用）"),
    summary: str = typer.Option(None, "--summary", help="submit 摘要"),
    paths: str = typer.Option(None, "--paths", help="认领范围路径（逗号分隔）"),
    force: bool = typer.Option(False, "--force", help="delete 强制；comment/mention 知情越过 held"),
    expect_activity: int = typer.Option(None, "--expect-activity",
                                        help="Held Draft：发送时依据的最新活动 id"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON"),
):
    """anvil task <list|show|create|claim|unclaim|heartbeat|submit|comment|mention|cancel|reopen|delete|block> …"""
    tid = target or DEFAULT_TASK
    needs_target = {"show", "claim", "unclaim", "heartbeat", "submit", "comment", "mention",
                    "cancel", "reopen", "delete", "block"}
    if action in needs_target and not tid:
        typer.echo(f"task {action} 需要任务 id", err=True)
        raise typer.Exit(2)
    method, path, payload = "GET", "/api/v1/tasks", None
    params = None
    if action == "list":
        params = {k: v for k, v in (("type", type_), ("tag", tag),
                                    ("parent_id", parent)) if v}
    elif action == "show":
        method, path = "GET", f"/api/v1/tasks/{tid}"
    elif action == "create":
        p = {"title": title or body or "untitled"}
        if type_:
            p["type"] = type_
        if description:
            p["description"] = description
        if parent:
            p["parent_id"] = parent
        if AGENT_SLUG:
            p["created_by"] = f"agent:{AGENT_SLUG}"
        method, path, payload = "POST", "/api/v1/tasks", p
    elif action == "claim":
        p = {"paths": [x.strip() for x in paths.split(",")]} if paths else {}
        method, path, payload = "POST", f"/api/v1/tasks/{tid}/claim", p
    elif action == "unclaim":
        method, path, payload = "POST", f"/api/v1/tasks/{tid}/unclaim", {}
    elif action == "heartbeat":
        method, path, payload = "POST", f"/api/v1/tasks/{tid}/heartbeat", {}
    elif action == "submit":
        method, path, payload = "POST", f"/api/v1/tasks/{tid}/submit", {"summary": summary or body}
    elif action == "comment":
        payload = {"body": body}
        if expect_activity:
            payload["expect_activity_id"] = expect_activity
        if force:
            payload["force"] = True
        method, path = "POST", f"/api/v1/tasks/{tid}/comment"
    elif action == "mention":
        payload = {"target": to, "message": body}
        if expect_activity:
            payload["expect_activity_id"] = expect_activity
        if force:
            payload["force"] = True
        method, path = "POST", f"/api/v1/tasks/{tid}/mention"
    elif action == "cancel":
        method, path, payload = "POST", f"/api/v1/tasks/{tid}/cancel", {}
    elif action == "reopen":
        method, path, payload = "POST", f"/api/v1/tasks/{tid}/reopen", {}
    elif action == "delete":
        method, path, payload = "DELETE", f"/api/v1/tasks/{tid}", None
        if force:
            params = {"force": "true"}
    elif action == "block":
        method, path, payload = "POST", f"/api/v1/tasks/{tid}/deps", {"depends_on": [on]}
    else:
        typer.echo(f"unknown task action: {action}", err=True)
        raise typer.Exit(2)
    _emit(_call(method, path, payload, params), json_out)


# ---- memory / note / lesson ---------------------------------------------


@app.command("memory")
def memory_cmd(
    action: str = typer.Argument("append"),
    text: str = typer.Argument(None),
    json_out: bool = typer.Option(False, "--json"),
):
    """anvil memory <append|search> <text>"""
    if action == "append":
        _emit(_call("POST", "/api/v1/me/memory", {"content": text}), json_out)
    elif action == "search":
        _emit(_call("GET", "/api/v1/me/memory/search", params={"q": text}), json_out)
    else:
        raise typer.Exit(2)


@app.command("note")
def note_cmd(
    action: str = typer.Argument("write"),
    path: str = typer.Argument(None),
    content_file: str = typer.Option(None, "--content-file", help="从文件读取笔记内容"),
    json_out: bool = typer.Option(False, "--json"),
):
    """anvil note write <path> [--content-file f]"""
    if action == "write":
        content = ""
        if content_file:
            try:
                with open(content_file, encoding="utf-8") as fh:
                    content = fh.read()
            except OSError as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(2)
        _emit(_call("POST", "/api/v1/me/notes", {"path": path, "content": content}), json_out)
    else:
        raise typer.Exit(2)


@app.command("lesson")
def lesson_cmd(
    action: str = typer.Argument("propose"),
    text: str = typer.Argument(None),
    tags: str = typer.Option(None, "--tags", help="逗号分隔标签"),
    repo: str = typer.Option(None, "--repo", help="归属共享仓库（默认 shared）"),
    json_out: bool = typer.Option(False, "--json"),
):
    """anvil lesson propose <text> [--tags a,b] [--repo name]"""
    if action == "propose":
        _emit(_call("POST", "/api/v1/lessons/pending", {
            "content": text,
            "tags": [t.strip() for t in tags.split(",")] if tags else [],
            "repo": repo,
        }), json_out)
    else:
        raise typer.Exit(2)


# ---- reminder / ws ------------------------------------------------------


@app.command("reminder")
def reminder_cmd(
    action: str = typer.Argument("list"),
    target: str = typer.Argument(None, help="remove 时的 reminder id"),
    cron: str = typer.Option(None, "--cron"),
    in_: str = typer.Option(None, "--in", help="延时，如 2h / 30m"),
    when: str = typer.Option(None, "--when", help="条件：任务 id（done 时触发）"),
    prompt: str = typer.Option(None, "--prompt", help="到期时注入给角色的提示"),
    role: str = typer.Option(None, "--role", help="角色 slug（默认取 ANVIL_AGENT_SLUG）"),
    json_out: bool = typer.Option(False, "--json"),
):
    """anvil reminder <add|list|remove|fire-now>"""
    role_slug = role or AGENT_SLUG or None
    if action == "add":
        payload: dict[str, Any] = {"role_slug": role_slug, "prompt": prompt}
        if in_:
            seconds = _parse_duration(in_)
            from datetime import datetime, timedelta
            payload.update({"kind": "at", "fire_at": (datetime.now().astimezone() + timedelta(seconds=seconds)).isoformat()})
        elif when:
            payload.update({"kind": "when", "when_condition": when})
        else:
            payload.update({"kind": "cron", "cron_expr": cron})
        _emit(_call("POST", "/api/v1/reminders", payload), json_out)
    elif action == "list":
        _emit(_call("GET", "/api/v1/reminders", params={"role_slug": role_slug} if role_slug else None), json_out)
    elif action == "remove":
        if not target:
            typer.echo("reminder remove 需要 id", err=True)
            raise typer.Exit(2)
        _emit(_call("DELETE", f"/api/v1/reminders/{target}"), json_out)
    elif action == "fire-now":
        if not target:
            typer.echo("reminder fire-now 需要 id", err=True)
            raise typer.Exit(2)
        _emit(_call("POST", f"/api/v1/reminders/{target}/fire-now", {}), json_out)
    else:
        raise typer.Exit(2)


def _parse_duration(text: str) -> int:
    """解析 30m / 2h / 1d。"""
    units = {"m": 60, "h": 3600, "d": 86400, "s": 1}
    try:
        unit = text[-1].lower()
        return int(float(text[:-1]) * units[unit])
    except (KeyError, ValueError, IndexError):
        typer.echo(f"无法解析时长: {text}（示例：30m / 2h / 1d）", err=True)
        raise typer.Exit(2)


@app.command("ws")
def ws_cmd(
    action: str = typer.Argument("status"),
    ws_id: str = typer.Argument("0"),
    json_out: bool = typer.Option(False, "--json"),
):
    """anvil ws <status|diff>"""
    if action == "status":
        _emit(_call("GET", f"/api/v1/workspaces/{ws_id}/tree"), json_out)
    elif action == "diff":
        _emit(_call("GET", f"/api/v1/workspaces/{ws_id}/diff"), json_out)
    else:
        raise typer.Exit(2)


# ---- role（Agent 自行扩展团队）--------------------------------------------


role_app = typer.Typer(help="角色扩展（Agent 创建新 agent）")
app.add_typer(role_app, name="role")


@role_app.command("expand")
def role_expand(
    brief: str = typer.Option(..., "--brief", help="新 agent 的一句话定位"),
    task: str = typer.Option(DEFAULT_TASK, "--task", help="关联任务 id（会通知 human）"),
    json_out: bool = typer.Option(False, "--json"),
):
    """创建一个新 agent（继承你的 runtime；每日配额有限）。"""
    out = _call("POST", "/api/v1/roles/expand", {"brief": brief, "task_id": task})
    if json_out:
        _emit(out, True)
    else:
        typer.echo(f"新 agent 已创建：{out['display_name']}（{out['slug']}）")


# ---- action（Action Cards：Agent 准备，人类执行）---------------------------


action_app = typer.Typer(help="Action Cards（危险操作先申请，人类批准后执行）")
app.add_typer(action_app, name="action")


@action_app.command("prepare")
def action_prepare(
    kind: str = typer.Option(..., "--kind", help="task.cancel | task.reopen"),
    task: str = typer.Option(DEFAULT_TASK, "--task", help="任务 id"),
    reason: str = typer.Option("", "--reason", help="申请原因"),
    json_out: bool = typer.Option(False, "--json"),
):
    """准备一张操作卡片，等待人类批准（不会直接执行）。"""
    out = _call("POST", "/api/v1/action-cards", {
        "kind": kind, "task_id": task, "reason": reason,
    })
    if json_out:
        _emit(out, True)
    else:
        typer.echo(f"操作卡片已创建：{out['id']}（pending，等待人类批准）")


@action_app.command("list")
def action_list(
    status: str = typer.Option("", "--status", help="pending|rejected|executed|failed"),
    json_out: bool = typer.Option(False, "--json"),
):
    """列出操作卡片。"""
    params = {"status": status} if status else None
    cards = _call("GET", "/api/v1/action-cards", params=params)
    if json_out:
        _emit(cards, True)
        return
    for c in cards:
        typer.echo(f"{c['id'][:8]}  {c['status']:9s}  {c['kind']:12s}  {c.get('reason') or ''}")


# ---- inbox（Agent 拉模式收件箱）-------------------------------------------


inbox_app = typer.Typer(help="Agent 收件箱（拉模式）")
app.add_typer(inbox_app, name="inbox")


@inbox_app.command("check")
def inbox_check(
    task: str = typer.Option(DEFAULT_TASK, "--task", help="任务 id（默认 $ANVIL_TASK_ID）"),
    limit: int = typer.Option(30, "--limit"),
    json_out: bool = typer.Option(False, "--json"),
):
    """拉取 cursor 之后的未读消息（不推进 cursor）。"""
    out = _call("GET", "/api/v1/me/inbox", params={"task_id": task, "limit": limit})
    if json_out:
        _emit(out, True)
        return
    typer.echo(f"未读 {out['unread']} 条（cursor={out['cursor']}）")
    for it in out["items"]:
        typer.echo(f"[{it['id']}] {it['role']} · {it['kind']}: {it['text'][:200]}")
    if not out["items"]:
        typer.echo("（无未读消息）")
    else:
        typer.echo(f"处理完后：anvil inbox commit --task {task} --up-to {out['items'][-1]['id']}")


@inbox_app.command("commit")
def inbox_commit(
    up_to: int = typer.Option(..., "--up-to", help="已处理到的最大消息 id"),
    task: str = typer.Option(DEFAULT_TASK, "--task", help="任务 id（默认 $ANVIL_TASK_ID）"),
    json_out: bool = typer.Option(False, "--json"),
):
    """提交 cursor（只前进；崩溃未提交会重复投递，at-least-once）。"""
    out = _call("POST", "/api/v1/me/inbox/commit", {"task_id": task, "up_to": up_to})
    if json_out:
        _emit(out, True)
    else:
        typer.echo(f"cursor 已提交：{out['cursor']}")


# ---- step（工作流步骤）----------------------------------------------------


step_app = typer.Typer(help="工作流步骤")
app.add_typer(step_app, name="step")


@step_app.command("submit")
def step_submit(
    workflow_id: str = typer.Argument(..., help="工作流 ID"),
    step_id: str = typer.Argument(..., help="步骤 ID（如 S1）"),
    summary: str = typer.Option("", "--summary", help="完成摘要"),
    json_out: bool = typer.Option(False, "--json"),
):
    """提交当前工作流步骤（完成后必须调用；不要用 task submit）。"""
    out = _call("POST", f"/api/v1/workflows/{workflow_id}/steps/{step_id}/submit",
                {"summary": summary})
    if json_out:
        _emit(out, True)
    else:
        typer.echo(f"步骤 {step_id} 已提交（{'等待人工审核' if any(s['step_id'] == step_id and s['status'] == 'submitted' and s['gate'] for s in out.get('steps', [])) else '已通过，工作流继续推进'}）")


@step_app.command("show")
def step_show(
    workflow_id: str = typer.Argument(..., help="工作流 ID"),
    json_out: bool = typer.Option(False, "--json"),
):
    """查看工作流各步骤状态。"""
    out = _call("GET", f"/api/v1/workflows/{workflow_id}")
    if json_out:
        _emit(out, True)
        return
    for st in out.get("steps", []):
        gate = " [gate]" if st.get("gate") else ""
        typer.echo(f"{st['step_id']:4s} {st['status']:10s} {st['role_slug']:12s} {st['title']}{gate}")


# ---- board（P1-0 计划先行：工作流草案）------------------------------------


board_app = typer.Typer(help="工作流草案（计划先行）")
app.add_typer(board_app, name="board")


@board_app.command("draft")
def board_draft(
    task: str = typer.Option(..., "--task", help="关联的主任务 id（通常为 $ANVIL_TASK_ID）"),
    goal: str = typer.Option(..., "--goal", help="目标一句话"),
    tree_file: str = typer.Option(..., "--tree-file", help="工作流/子任务树 JSON 文件路径"),
    kind: str = typer.Option("tasks", "--kind", help="workflow（步骤状态机）或 tasks（旧子任务树）"),
    json_out: bool = typer.Option(False, "--json"),
):
    """planner 提交工作流草案（步骤/分工/依赖/审核门/反馈路径）。"""
    with open(tree_file, encoding="utf-8") as f:
        tree = json.load(f)
    created_by = f"agent:{AGENT_SLUG}" if AGENT_SLUG else "human"
    out = _call("POST", "/api/v1/board/drafts", {
        "goal": goal, "tree": tree, "source_task": task, "created_by": created_by,
        "kind": kind,
    })
    if json_out:
        _emit(out, True)
    else:
        typer.echo(f"草案已创建：{out['id']}（pending，等待人工批准）")


@board_app.command("drafts")
def board_drafts(
    task: str = typer.Option("", "--task", help="按关联任务过滤"),
    json_out: bool = typer.Option(False, "--json"),
):
    """列出工作流草案。"""
    params = {"source_task": task} if task else None
    drafts = _call("GET", "/api/v1/board/drafts", params=params)
    if json_out:
        _emit(drafts, True)
        return
    if not drafts:
        typer.echo("（无草案）")
        return
    for d in drafts:
        src = d.get("source_task") or "-"
        typer.echo(f"{d['id']}  {d['status']:9s}  task={src}  {d['goal']}  by={d['created_by']}")


if __name__ == "__main__":
    app()
