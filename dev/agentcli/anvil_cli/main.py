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
    else:
        # 人类 CLI：共享 token 只代表 human，不能冒充 agent。
        h["X-Anvil-Role"] = "human"
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
    task: str = typer.Option(None, "--task", help="reset-context 的任务 id（默认 $ANVIL_TASK_ID）"),
    reason: str = typer.Option(None, "--reason", help="reset-context 原因"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON"),
):
    """anvil self <show|edit-instruction|propose|reset-context>"""
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
    elif action == "reset-context":
        tid = task or DEFAULT_TASK
        if not tid:
            typer.echo("self reset-context 需要 --task 或 $ANVIL_TASK_ID", err=True)
            raise typer.Exit(2)
        _emit(_call("POST", "/api/v1/me/session/reset", {
            "task_id": tid, "reason": reason or "",
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
    tags: str = typer.Option(None, "--tags", help="create 的标签（逗号分隔）"),
    repo: str = typer.Option(None, "--repo", help="create 的执行仓库（单值）"),
    workspace: str = typer.Option(None, "--workspace", help="create 的频道/workspace id"),
    base_branch: str = typer.Option(None, "--base-branch", help="create 的基线分支"),
    plan_first: bool = typer.Option(False, "--plan-first", help="创建后先由 planner 规划"),
    planner: str = typer.Option(None, "--planner", help="create 的 planner 角色"),
    knowledge_repos: str = typer.Option(
        None, "--knowledge-repos", help="create 的知识仓库（逗号分隔；人工指定会加锁）"),
    parent: str = typer.Option(None, "--parent", help="父任务 id"),
    title: str = typer.Option(None, "--title", help="任务标题（create 用）"),
    description: str = typer.Option(None, "--description", help="任务描述（create 用）"),
    assignee: str = typer.Option(None, "--assignee",
                                 help="子任务执行角色（create 用；单负责人）"),
    assignees: str = typer.Option(None, "--assignees",
                                  help="团队角色（create / assign 用；逗号分隔；assign 仅限现有角色）"),
    summary: str = typer.Option(None, "--summary", help="submit 摘要"),
    message: str = typer.Option(None, "--message", "-m",
                                help="comment/mention 正文（等价位置参数 body）"),
    paths: str = typer.Option(None, "--paths", help="认领范围路径（逗号分隔）"),
    force: bool = typer.Option(False, "--force", help="delete 强制；comment/mention 知情越过 held"),
    expect_activity: int = typer.Option(None, "--expect-activity",
                                        help="Held Draft：发送时依据的最新活动 id"),
    kind: str = typer.Option(None, "--kind", help="knowledge-write 的知识类型"),
    tldr: str = typer.Option(None, "--tldr", help="knowledge-write 的一句话摘要"),
    fields: str = typer.Option(None, "--fields", help="knowledge-write 的字段 JSON"),
    knowledge_id: str = typer.Option(None, "--id", help="knowledge-delete 的知识 id"),
    include_subtasks: bool = typer.Option(False, "--include-subtasks",
                                         help="knowledge-list 包含直接子任务知识"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON"),
):
    """anvil task <list|show|create|assign|claim|unclaim|heartbeat|submit|comment|mention|cancel|reopen|delete|block|archive|unarchive|knowledge-list|knowledge-write|knowledge-delete> …"""
    tid = target or DEFAULT_TASK
    needs_target = {"show", "claim", "unclaim", "heartbeat", "submit", "comment", "mention",
                    "cancel", "reopen", "delete", "block", "assign", "archive", "unarchive",
                    "knowledge-list", "knowledge-write", "knowledge-delete"}
    if action in needs_target and not tid:
        typer.echo(f"task {action} 需要任务 id", err=True)
        raise typer.Exit(2)
    if message is not None and action not in ("comment", "mention"):
        typer.echo(f"task {action} 不支持 --message；请用位置参数或对应选项", err=True)
        raise typer.Exit(2)
    body_text = body if body is not None else message
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
        if tags:
            p["tags"] = [x.strip() for x in tags.split(",") if x.strip()]
        if repo:
            p["repo"] = repo.strip()
        if workspace:
            p["workspace_id"] = workspace.strip()
        if base_branch:
            p["base_branch"] = base_branch.strip()
        if plan_first:
            p["plan_first"] = True
        if planner:
            p["planner"] = planner.strip()
        if knowledge_repos:
            p["knowledge_repos"] = [
                x.strip() for x in knowledge_repos.split(",") if x.strip()]
        if assignee:
            p["assignee"] = assignee.strip()
        if assignees:
            p["assignees"] = [x.strip() for x in assignees.split(",") if x.strip()]
        if AGENT_SLUG:
            p["created_by"] = f"agent:{AGENT_SLUG}"
        method, path, payload = "POST", "/api/v1/tasks", p
    elif action == "assign":
        roles = [x.strip() for x in (assignees or "").split(",") if x.strip()]
        if not roles:
            typer.echo("task assign 需要 --assignees a,b（仅限现有角色）", err=True)
            raise typer.Exit(2)
        method, path, payload = "POST", f"/api/v1/tasks/{tid}/assign-existing", {"roles": roles}
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
        if body_text and "@" in body_text:
            typer.echo(
                "warning: 评论中的 @ 只是文本，不会唤醒角色；请改用 "
                "`anvil task mention <task-id> --to <role> \"...\"`。", err=True)
        payload = {"body": body_text}
        if expect_activity:
            payload["expect_activity_id"] = expect_activity
        if force:
            payload["force"] = True
        method, path = "POST", f"/api/v1/tasks/{tid}/comment"
    elif action == "mention":
        payload = {"target": to, "message": body_text}
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
    elif action == "archive":
        method, path, payload = "POST", f"/api/v1/tasks/{tid}/archive", {}
    elif action == "unarchive":
        method, path, payload = "POST", f"/api/v1/tasks/{tid}/unarchive", {}
    elif action == "knowledge-list":
        method, path = "GET", f"/api/v1/tasks/{tid}/knowledge"
        params = {"include_subtasks": "true"} if include_subtasks else None
    elif action == "knowledge-write":
        if not (title and tldr and kind):
            typer.echo("task knowledge-write 需要 --title --tldr --kind", err=True)
            raise typer.Exit(2)
        parsed_fields: dict = {}
        if fields:
            try:
                parsed_fields = json.loads(fields)
            except json.JSONDecodeError as exc:
                typer.echo(f"--fields 必须是 JSON 对象: {exc}", err=True)
                raise typer.Exit(2)
        payload = {"title": title, "tldr": tldr, "kind": kind, "fields": parsed_fields}
        if tags:
            payload["tags"] = [x.strip() for x in tags.split(",") if x.strip()]
        method, path = "POST", f"/api/v1/tasks/{tid}/knowledge"
    elif action == "knowledge-delete":
        if not knowledge_id:
            typer.echo("task knowledge-delete 需要 --id <knowledge-id>", err=True)
            raise typer.Exit(2)
        method, path, payload = "DELETE", f"/api/v1/tasks/{tid}/knowledge/{knowledge_id}", None
    else:
        typer.echo(f"unknown task action: {action}", err=True)
        raise typer.Exit(2)
    _emit(_call(method, path, payload, params), json_out)


# ---- memory / note / lesson ---------------------------------------------


@app.command("memory")
def memory_cmd(
    action: str = typer.Argument("append"),
    text: str = typer.Argument(None),
    practice_task: str = typer.Option(None, "--practice-task",
                                      help="成功实践的任务 id（个人知识门控）"),
    practice_run: str = typer.Option(None, "--practice-run"),
    outcome: str = typer.Option(None, "--outcome", help="实践结果/提炼结论"),
    source_knowledge: str = typer.Option(None, "--source-knowledge",
                                         help="只读参考过的知识 id（逗号分隔）"),
    force: bool = typer.Option(False, "--force", help="human 强制写入（Agent 不允许）"),
    json_out: bool = typer.Option(False, "--json"),
):
    """anvil memory <append|search> <text>"""
    if action == "append":
        body = {"content": text}
        for key, value in (("practice_task_id", practice_task),
                           ("practice_run_id", practice_run), ("outcome", outcome)):
            if value:
                body[key] = value
        if source_knowledge:
            body["source_knowledge"] = [
                x.strip() for x in source_knowledge.split(",") if x.strip()]
        if force:
            body["force"] = True
        _emit(_call("POST", "/api/v1/me/memory", body), json_out)
    elif action == "search":
        _emit(_call("GET", "/api/v1/me/memory/search", params={"q": text}), json_out)
    else:
        raise typer.Exit(2)


@app.command("note")
def note_cmd(
    action: str = typer.Argument("write"),
    path: str = typer.Argument(None),
    content_file: str = typer.Option(None, "--content-file", help="从文件读取笔记内容"),
    kind: str = typer.Option(None, "--kind", help="task_snapshot|vulnerability_pattern|failure_scenario|module_knowledge|decision|note"),
    scope: str = typer.Option(None, "--scope", help="role|task"),
    task: str = typer.Option(None, "--task", help="任务 id（scope=task 时必填，默认 $ANVIL_TASK_ID）"),
    source_task: str = typer.Option(None, "--source-task", help="可复用知识来自哪个任务"),
    tags: str = typer.Option(None, "--tags", help="逗号分隔标签"),
    repo: str = typer.Option(None, "--repo"),
    module: str = typer.Option(None, "--module"),
    confidence: float = typer.Option(0.5, "--confidence", min=0.0, max=1.0),
    replace: bool = typer.Option(False, "--replace", help="用完整内容替换同 path 旧笔记"),
    practice_task: str = typer.Option(None, "--practice-task",
                                      help="成功实践的任务 id（个人知识门控）"),
    practice_run: str = typer.Option(None, "--practice-run"),
    outcome: str = typer.Option(None, "--outcome", help="实践结果/提炼结论"),
    source_knowledge: str = typer.Option(None, "--source-knowledge",
                                         help="只读参考过的知识 id（逗号分隔）"),
    force: bool = typer.Option(False, "--force", help="human 强制写入（Agent 不允许）"),
    json_out: bool = typer.Option(False, "--json"),
):
    """anvil note write <path> [--content-file f] [--kind/--scope/--tags/...]"""
    if action == "write":
        content = ""
        if content_file:
            try:
                with open(content_file, encoding="utf-8") as fh:
                    content = fh.read()
            except OSError as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(2)
        payload: dict = {"path": path, "content": content, "confidence": confidence,
                         "mode": "replace" if replace else "append"}
        effective_scope = scope
        if kind == "task_snapshot" and not effective_scope:
            effective_scope = "task"
        effective_task = task or (DEFAULT_TASK if effective_scope == "task" else None)
        if effective_scope == "task" and not effective_task:
            typer.echo("note: scope=task 需要 --task 或 ANVIL_TASK_ID", err=True)
            raise typer.Exit(2)
        for key, value in (
            ("kind", kind), ("scope", effective_scope), ("task_id", effective_task),
            ("source_task", source_task), ("repo", repo), ("module", module),
            ("practice_task_id", practice_task), ("practice_run_id", practice_run),
            ("outcome", outcome),
        ):
            if value:
                payload[key] = value
        if tags:
            payload["tags"] = [x.strip() for x in tags.split(",") if x.strip()]
        if source_knowledge:
            payload["source_knowledge"] = [
                x.strip() for x in source_knowledge.split(",") if x.strip()]
        if force:
            payload["force"] = True
        _emit(_call("POST", "/api/v1/me/notes", payload), json_out)
    else:
        raise typer.Exit(2)


@app.command("knowledge")
def knowledge_cmd(
    action: str = typer.Argument("search"),
    query: str = typer.Argument(None),
    note_id: str = typer.Option(None, "--id", help="show 的 note id"),
    task: str = typer.Option(None, "--task", help="任务 id（默认 $ANVIL_TASK_ID）"),
    scope: str = typer.Option("auto", "--scope", help="auto|role|task"),
    kind: str = typer.Option(None, "--kind", help="逗号分隔 kind"),
    tags: str = typer.Option(None, "--tags", help="逗号分隔标签"),
    repo: str = typer.Option(None, "--repo"),
    repos: str = typer.Option(None, "--repos", help="限定知识仓库（逗号分隔）"),
    module: str = typer.Option(None, "--module"),
    include_snapshots: bool = typer.Option(False, "--include-snapshots"),
    include_unclassified: bool = typer.Option(False, "--include-unclassified"),
    limit: int = typer.Option(8, "--limit"),
    role: str = typer.Option(None, "--role", help="human 浏览角色知识时指定角色"),
    self_only: bool = typer.Option(False, "--self", help="reindex 当前角色"),
    all_roles: bool = typer.Option(False, "--all", help="reindex 所有角色（human）"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    prune: bool = typer.Option(False, "--prune"),
    fields: str = typer.Option(None, "--fields", help="card-validate 的字段 JSON"),
    file: str = typer.Option(None, "--file", help="card-validate 的知识卡 JSON 文件"),
    for_approval: bool = typer.Option(False, "--for-approval",
                                      help="card-validate 按 approve 必填项校验"),
    json_out: bool = typer.Option(False, "--json"),
):
    """anvil knowledge <search|show|index|list|reindex|repo-list|repo-show|repo-suggest|card-template|card-validate> ..."""
    if action == "search":
        params = {
            "q": query or "", "scope": scope, "limit": limit,
            "include_snapshots": include_snapshots,
            "include_unclassified": include_unclassified,
        }
        task_id = task or DEFAULT_TASK
        if task_id:
            params["task_id"] = task_id
        for key, value in (("kind", kind), ("tags", tags),
                           ("repo", repo), ("repos", repos),
                           ("module", module), ("role", role)):
            if value:
                params[key] = value
        _emit(_call("GET", "/api/v1/me/knowledge/search", params=params), json_out)
    elif action == "repo-list":
        _emit(_call("GET", "/api/v1/knowledge/repos"), json_out)
    elif action == "repo-show":
        name = repo or query
        if not name:
            typer.echo("knowledge repo-show 需要 --repo <name>", err=True)
            raise typer.Exit(2)
        from urllib.parse import quote
        _emit(_call("GET", f"/api/v1/knowledge/repos/{quote(name, safe='')}"), json_out)
    elif action == "repo-suggest":
        if not query:
            typer.echo("knowledge repo-suggest 需要任务标题", err=True)
            raise typer.Exit(2)
        _emit(_call("POST", "/api/v1/knowledge/repos/suggest", {
            "title": query,
            "tags": [x.strip() for x in tags.split(",")] if tags else [],
            "repo": repo,
        }), json_out)
    elif action == "show":
        if not note_id:
            typer.echo("knowledge show 需要 --id <note-id>", err=True)
            raise typer.Exit(2)
        params = {}
        if task or DEFAULT_TASK:
            params["task_id"] = task or DEFAULT_TASK
        if role:
            params["role"] = role
        _emit(_call("GET", f"/api/v1/me/knowledge/{note_id}", params=params), json_out)
    elif action in ("index", "list"):
        params = {}
        if task or DEFAULT_TASK:
            params["task_id"] = task or DEFAULT_TASK
        if role:
            params["role"] = role
        _emit(_call("GET", "/api/v1/me/knowledge/index", params=params), json_out)
    elif action == "reindex":
        body = {"dry_run": dry_run, "prune": prune}
        if all_roles or role:
            body["role"] = role
            if all_roles:
                body.pop("role", None)
            _emit(_call("POST", "/api/v1/knowledge/reindex", body), json_out)
        else:
            _emit(_call("POST", "/api/v1/me/knowledge/reindex", body), json_out)
    elif action == "card-template":
        _emit(_call("GET", "/api/v1/knowledge/card/template",
                    params={"kind": kind} if kind else None), json_out)
    elif action == "card-validate":
        payload: dict = {}
        if file:
            try:
                with open(file, encoding="utf-8") as fh:
                    payload = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                typer.echo(f"无法读取知识卡文件: {exc}", err=True)
                raise typer.Exit(2)
        if not isinstance(payload, dict):
            typer.echo("知识卡文件必须是 JSON 对象", err=True)
            raise typer.Exit(2)
        if kind:
            payload["kind"] = kind
        if fields:
            payload["fields"] = _fields_json(fields)
        payload["for_approval"] = for_approval
        result = _call("POST", "/api/v1/knowledge/card/validate", payload)
        _emit(result, json_out)
        if result.get("errors"):
            raise typer.Exit(3)
    else:
        typer.echo(
            f"unknown knowledge action: {action}\n"
            "可用动作: search, show, index (别名 list), reindex, repo-list, "
            "repo-show, repo-suggest, card-template, card-validate",
            err=True,
        )
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


# ---- workspace（频道）与频道知识 ------------------------------------------


workspace_app = typer.Typer(help="频道（任务归属 + 知识作用域 + 资产）")
app.add_typer(workspace_app, name="workspace")
ws_knowledge_app = typer.Typer(help="频道知识卡（Agent 只能 propose，human 批准）")
workspace_app.add_typer(ws_knowledge_app, name="knowledge")


def _fields_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        typer.echo(f"--fields 必须是 JSON 对象: {exc}", err=True)
        raise typer.Exit(2)
    if not isinstance(value, dict):
        typer.echo("--fields 必须是 JSON 对象", err=True)
        raise typer.Exit(2)
    return value


@workspace_app.command("list")
def workspace_list(json_out: bool = typer.Option(False, "--json")):
    """列出频道。"""
    _emit(_call("GET", "/api/v1/workspaces"), json_out)


@workspace_app.command("create")
def workspace_create(
    name: str = typer.Argument(...),
    repo: str = typer.Option(None, "--repo", help="频道默认/领域 repo"),
    knowledge_repo: str = typer.Option(None, "--knowledge-repo"),
    description: str = typer.Option(None, "--description"),
    json_out: bool = typer.Option(False, "--json"),
):
    """创建频道（human）。"""
    body = {"name": name}
    for key, value in (("repo", repo), ("knowledge_repo", knowledge_repo),
                       ("description", description)):
        if value:
            body[key] = value
    _emit(_call("POST", "/api/v1/workspaces", body), json_out)


@ws_knowledge_app.command("list")
def ws_knowledge_list(
    ws_id: str = typer.Argument(..., help="频道 id"),
    status: str = typer.Option(None, "--status"),
    kind: str = typer.Option(None, "--kind"),
    sort: str = typer.Option(None, "--sort", help="freshness"),
    json_out: bool = typer.Option(False, "--json"),
):
    params = {k: v for k, v in (("status", status), ("kind", kind), ("sort", sort)) if v}
    _emit(_call("GET", f"/api/v1/workspaces/{ws_id}/knowledge", params=params), json_out)


@ws_knowledge_app.command("propose")
def ws_knowledge_propose(
    ws_id: str = typer.Argument(..., help="频道 id"),
    title: str = typer.Option(..., "--title"),
    tldr: str = typer.Option(..., "--tldr"),
    kind: str = typer.Option(..., "--kind",
                             help="convention|decision|workflow|role_requirement|insight"),
    fields: str = typer.Option(None, "--fields", help="知识卡字段 JSON"),
    authority: str = typer.Option(None, "--authority", help="normative|reference"),
    tags: str = typer.Option(None, "--tags", help="逗号分隔"),
    source_task: str = typer.Option(None, "--source-task"),
    review_at: str = typer.Option(None, "--review-at", help="YYYY-MM-DD"),
    expires_at: str = typer.Option(None, "--expires-at"),
    json_out: bool = typer.Option(False, "--json"),
):
    """提交频道知识；Agent 写入为 proposed，human 写入为 approved。"""
    body = {"title": title, "tldr": tldr, "kind": kind, "fields": _fields_json(fields)}
    for key, value in (("authority", authority), ("source_task_id", source_task),
                       ("review_at", review_at), ("expires_at", expires_at)):
        if value:
            body[key] = value
    if tags:
        body["tags"] = [x.strip() for x in tags.split(",") if x.strip()]
    _emit(_call("POST", f"/api/v1/workspaces/{ws_id}/knowledge", body), json_out)


@ws_knowledge_app.command("approve")
def ws_knowledge_approve(
    ws_id: str = typer.Argument(..., help="频道 id"),
    knowledge_id: str = typer.Argument(..., help="知识卡 id"),
    expected_version: int = typer.Option(None, "--expected-version"),
    json_out: bool = typer.Option(False, "--json"),
):
    """批准知识卡（仅 human）。"""
    body = {}
    if expected_version is not None:
        body["expected_version"] = expected_version
    _emit(_call("POST", f"/api/v1/workspaces/{ws_id}/knowledge/{knowledge_id}/approve",
                body), json_out)


@ws_knowledge_app.command("harvest")
def ws_knowledge_harvest(
    ws_id: str = typer.Argument(..., help="频道 id"),
    task_id: str = typer.Argument(..., help="done/closed 任务 id"),
    json_out: bool = typer.Option(False, "--json"),
):
    """从终态任务收割知识（Agent 生成 proposed，human 可直接 approved）。"""
    _emit(_call("POST", f"/api/v1/workspaces/{ws_id}/knowledge/harvest",
                {"task_id": task_id}), json_out)


@ws_knowledge_app.command("review")
def ws_knowledge_review(
    ws_id: str = typer.Argument(..., help="频道 id"),
    json_out: bool = typer.Option(False, "--json"),
):
    """知识复盘：过期、复核到期、长期未命中、冲突未裁决。"""
    _emit(_call("GET", f"/api/v1/workspaces/{ws_id}/knowledge/review"), json_out)


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
    evidence_file: str = typer.Option(
        "", "--evidence-file", help="证据 JSON 文件（数组）"),
    changed_paths: str = typer.Option(
        "", "--changed-paths", help="本步骤改动的路径，逗号分隔"),
    json_out: bool = typer.Option(False, "--json"),
):
    """提交当前工作流步骤（完成后必须调用；不要用 task submit）。"""
    body: dict = {"summary": summary, "changed_paths": _csv(changed_paths)}
    if evidence_file:
        with open(evidence_file, encoding="utf-8") as fh:
            body["evidence"] = json.load(fh)
    out = _call("POST", f"/api/v1/workflows/{workflow_id}/steps/{step_id}/submit", body)
    if json_out:
        _emit(out, True)
    else:
        typer.echo(f"步骤 {step_id} 已提交（{'等待人工审核' if any(s['step_id'] == step_id and s['status'] == 'submitted' and s['gate'] for s in out.get('steps', [])) else '已通过，工作流继续推进'}）")


def _csv(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


@step_app.command("decide")
def step_decide(
    workflow_id: str = typer.Argument(..., help="工作流 ID"),
    step_id: str = typer.Argument(..., help="decision 步骤 ID"),
    approve: bool = typer.Option(False, "--approve", help="通过"),
    reject: bool = typer.Option(False, "--reject", help="打回重做"),
    feedback: str = typer.Option("", "--feedback", help="决策结论 / 打回原因"),
    rework_targets: str = typer.Option(
        "", "--rework-targets", help="打回范围，逗号分隔的步骤 ID"),
    reasons: str = typer.Option("", "--reason", help="结构化原因，逗号分隔"),
    override_reason: str = typer.Option(
        "", "--override-reason", help="人工覆盖 AI/联合决策时的理由"),
    packet_id: str = typer.Option("", "--packet", help="决策针对的 packet ID"),
    acknowledge_stale: bool = typer.Option(
        False, "--acknowledge-stale", help="确认接受过期证据"),
    json_out: bool = typer.Option(False, "--json"),
):
    """决策角色通过或打回 decision 步骤（只能二选一）。"""
    if approve == reject:
        typer.echo("必须且只能给 --approve 或 --reject", err=True)
        raise typer.Exit(2)
    body = {
        "approve": approve,
        "feedback": feedback,
        "rework_targets": _csv(rework_targets),
        "reasons": _csv(reasons),
        "override_reason": override_reason or None,
        "packet_id": packet_id or None,
        "acknowledge_stale": acknowledge_stale,
    }
    out = _call("POST", f"/api/v1/workflows/{workflow_id}/steps/{step_id}/decide", body)
    if json_out:
        _emit(out, True)
    else:
        typer.echo(f"决策步骤 {step_id} 已{'提交' if approve else '打回重做'}")


@step_app.command("escalate")
def step_escalate(
    workflow_id: str = typer.Argument(..., help="工作流 ID"),
    step_id: str = typer.Argument(..., help="decision 步骤 ID"),
    reason: str = typer.Option(..., "--reason", help="升级原因"),
    triggers: str = typer.Option("", "--triggers", help="触发原因，逗号分隔"),
    proposed_decision: str = typer.Option(
        "", "--proposed-decision", help="建议结论 approve|reject"),
    proposed_feedback: str = typer.Option("", "--proposed-feedback"),
    json_out: bool = typer.Option(False, "--json"),
):
    """证据不足/风险超限时把决策升级给自然人。"""
    out = _call("POST", f"/api/v1/workflows/{workflow_id}/steps/{step_id}/escalate", {
        "reason": reason,
        "triggers": _csv(triggers),
        "proposed_decision": proposed_decision or None,
        "proposed_feedback": proposed_feedback or None,
    })
    if json_out:
        _emit(out, True)
    else:
        typer.echo(f"决策步骤 {step_id} 已升级人工")


@step_app.command("packet")
def step_packet(
    workflow_id: str = typer.Argument(..., help="工作流 ID"),
    step_id: str = typer.Argument(..., help="步骤 ID"),
    json_out: bool = typer.Option(False, "--json"),
):
    """查看 gate/decision 的 review packet 与历史决策。"""
    out = _call("GET", f"/api/v1/workflows/{workflow_id}/steps/{step_id}/packet")
    if json_out:
        _emit(out, True)
        return
    packet = out.get("packet")
    if not packet:
        typer.echo("该步骤尚未生成 packet（可能还未提交或不在审核中）")
        return
    typer.echo(f"问题：{packet.get('question')}")
    typer.echo(f"权限：{packet.get('authority')} · 状态：{packet.get('status')}")
    for e in packet.get("evidence") or []:
        mark = "新鲜" if e.get("fresh", True) else "过期"
        typer.echo(f"- [{e.get('kind')}] {e.get('ref')} ({mark})")
    for d in out.get("decisions") or []:
        typer.echo(f"决策：{d.get('decider')} {d.get('decision')} {d.get('feedback') or ''}")


@step_app.command("plan")
def step_plan(
    workflow_id: str = typer.Argument(..., help="工作流 ID"),
    step_id: str = typer.Argument(..., help="plan_slices 步骤 ID"),
    file: str = typer.Option(..., "--file", help="切片计划 JSON 文件"),
    json_out: bool = typer.Option(False, "--json"),
):
    """提交切片计划（plan_slices 步骤）：服务端按 loop 模板生成每片小闭环。"""
    with open(file, encoding="utf-8") as f:
        plan = json.load(f)
    out = _call("POST", f"/api/v1/workflows/{workflow_id}/steps/{step_id}/plan", plan)
    if json_out:
        _emit(out, True)
        return
    generated = [s for s in out.get("steps", []) if s.get("source_step_id") == step_id]
    typer.echo(f"切片计划已提交：生成 {len(generated)} 个切片步骤")


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
        kind = ""
        if st.get("kind") == "decision":
            kind = " [decision]"
        elif st.get("kind") == "plan_slices":
            kind = " [plan_slices]"
        slice_info = f" slice={st['slice_key']}" if st.get("slice_key") else ""
        rework = ""
        if st.get("kind") == "decision":
            rework = f" rework={st.get('rework_count', 0)}/{st.get('max_rework', 0)}"
        on_reject = ""
        if st.get("on_reject"):
            on_reject = " on_reject=" + ",".join(st["on_reject"])
        typer.echo(
            f"{st['step_id']:4s} {st['status']:10s} {st['role_slug']:12s} "
            f"{st['title']}{gate}{kind}{slice_info}{rework}{on_reject}")


# ---- lease（step/path 租约与路径预留）------------------------------------


lease_app = typer.Typer(help="执行租约与路径预留")
app.add_typer(lease_app, name="lease")


@lease_app.command("list")
def lease_list(
    task: str = typer.Option(DEFAULT_TASK, "--task", help="任务 id"),
    json_out: bool = typer.Option(False, "--json"),
):
    """查看任务当前的执行租约与路径预留。"""
    out = _call("GET", f"/api/v1/tasks/{task}/leases")
    if json_out:
        _emit(out, True)
        return
    active_steps = [s for s in out.get("steps") or [] if s.get("lease_status") == "active"]
    for s in active_steps:
        typer.echo(f"step {s['step_id']}: {s.get('holder')} fence={s.get('fence_token')} "
                   f"until {s.get('expires_at')}")
    for p in out.get("paths") or []:
        if p.get("status") == "active":
            path = str(p.get("resource_key", "")).split(":", 1)[-1]
            typer.echo(f"path {path}: {p.get('holder')} until {p.get('expires_at')}")
    if not active_steps and not any(p.get("status") == "active" for p in out.get("paths") or []):
        typer.echo("没有活跃租约")


@lease_app.command("reserve")
def lease_reserve(
    paths: list[str] = typer.Argument(..., help="要预留的相对路径，可多个"),
    task: str = typer.Option(DEFAULT_TASK, "--task", help="任务 id"),
    ttl: float = typer.Option(0, "--ttl", help="租约秒数（0=默认）"),
    json_out: bool = typer.Option(False, "--json"),
):
    """预留路径，避免多个角色同时写同一文件。"""
    body = {"paths": paths, "ttl_seconds": ttl or None}
    out = _call("POST", f"/api/v1/tasks/{task}/reservations", body)
    if json_out:
        _emit(out, True)
    else:
        typer.echo(f"已预留 {len(paths)} 个路径")


@lease_app.command("release")
def lease_release(
    paths: list[str] = typer.Argument(..., help="要释放的相对路径"),
    task: str = typer.Option(DEFAULT_TASK, "--task", help="任务 id"),
    json_out: bool = typer.Option(False, "--json"),
):
    """释放当前角色持有的路径预留。"""
    out = _call("DELETE", f"/api/v1/tasks/{task}/reservations", {"paths": paths})
    if json_out:
        _emit(out, True)
    else:
        typer.echo(f"已释放 {out.get('released', 0)} 个路径")


# ---- board（P1-0 计划先行：工作流草案）------------------------------------


board_app = typer.Typer(help="工作流草案（计划先行）")
app.add_typer(board_app, name="board")


@board_app.command("draft")
def board_draft(
    task: str = typer.Option(..., "--task", help="关联的主任务 id（通常为 $ANVIL_TASK_ID）"),
    goal: str = typer.Option(..., "--goal", help="目标一句话"),
    tree_file: str = typer.Option(..., "--tree-file", help="工作流/子任务树 JSON 文件路径"),
    kind: str = typer.Option(
        "tasks", "--kind",
        help="workflow（步骤状态机）/ role_suggestion（角色建议）/ tasks（旧子任务树）"),
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
        kind = d.get("kind") or "tasks"
        typer.echo(
            f"{d['id']}  {d['status']:9s}  {kind:15s}  task={src}  "
            f"{d['goal']}  by={d['created_by']}")


if __name__ == "__main__":
    app()
