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

try:  # Typer 0.27+ 内置 click；旧版本仍用外部 click
    from typer._click.core import Exit as ClickExit
    from typer._click.exceptions import UsageError as ClickUsageError
except ImportError:  # pragma: no cover - 兼容 typer<0.27
    from click.exceptions import Exit as ClickExit
    from click.exceptions import UsageError as ClickUsageError

HELP_CTX = {"help_option_names": ["-h", "--help"], "max_content_width": 100}

MAIN_EPILOG = """\b
常用流程:
  anvil task list                     查看任务
  anvil task show <id>                查看任务详情
  anvil task claim <id>               认领任务（工作流步骤不要 claim）
  anvil task comment <id> "进展"      同步进展（不唤醒）
  anvil step show <workflow-id>       查看工作流步骤
  anvil step submit <wf> <step> --summary "完成"
  anvil role list                     查看已有角色

规则:
  · 工作流步骤用 anvil step，不要用 task claim/submit 代替。
  · 身份由 ANVIL_TOKEN / ANVIL_TASK_ID 环境变量决定；不要伪造角色。
  · 删除、审批、重置等人类操作需要 human token。
  · 退出码: 0 成功 / 1 通用失败 / 2 参数错误 / 3 状态冲突 / 4 无权限。
  · 每个命令都支持 -h / --help，例如 anvil task -h。
  · 文件参数用当前目录相对路径或绝对路径；Windows 临时目录用 %TEMP%，
    不要写死 /tmp（macOS/Linux 的 /tmp 在 Windows 不存在）。
"""

TASK_EPILOG = """\b
动作:
  list [--type T] [--tag T]             列出任务
  show <id>                             查看任务详情
  create --title "..." [--assignee r]   创建任务或子任务
  assign <id> --assignees a,b           追加角色；已有 Roster 时自动提案 @human
  claim | unclaim <id>                  认领/释放（工作流步骤不要 claim）
  submit <id> "摘要"                    提交任务（不是工作流步骤）
  comment <id> "正文"                   评论，不唤醒任何人
  mention <id> --to <role> "正文"       @提及并唤醒目标角色
  archive | unarchive <id>              归档/取消归档（human）
  knowledge-list <id>                   任务共享知识
  knowledge-write <id> --title --tldr --kind
  knowledge-delete <id> --id <kid>      删除任务知识（human）

示例:
  anvil task create "修复登录超时" --assignee developer --workspace W-1
  anvil task comment $ANVIL_TASK_ID "已完成接口联调"
  anvil task mention $ANVIL_TASK_ID --to tester "请复跑回归"

提示: 工作流任务先看 `anvil step show <workflow-id>`；步骤操作不要用 task claim/submit。
任务已有 assignees 时，assign 只追加新角色，并先自动提交提案 @human；批准后重试即可。
"""

STEP_EPILOG = """\b
典型顺序:
  anvil step show <workflow-id>                 查看步骤状态
  anvil step plan <wf> <plan-step> --file plan.json
  anvil step submit <wf> <step> --summary "完成"
  anvil step decide <wf> <decision-step> --approve --feedback "通过"
  anvil step escalate <wf> <decision-step> --reason "证据不足"

规则:
  · 只对 running 步骤操作；409 时先用 step show 确认状态，不要反复重试。
  · plan_slices 用 step plan；decision 用 step decide；普通步骤用 step submit。
  · 工作流步骤不要 task claim / task submit，也不要在切片里指定角色。
"""

KNOWLEDGE_EPILOG = """\b
动作:
  search "关键词" --task ID             检索自己的角色/任务知识
  show --id <note-id>                   查看一条笔记/知识
  index | list                          角色知识计数与最近笔记
  reindex --dry-run                     文件变更后重建索引
  repo-list                             知识仓库列表
  repo-show --repo <name>               仓库详情
  repo-suggest "任务标题"               推荐知识仓库
  card-template --kind convention       知识卡模板
  card-validate --file card.json        校验知识卡

示例:
  anvil knowledge search "登录 超时" --task $ANVIL_TASK_ID --limit 5
  anvil knowledge list
  anvil knowledge card-template --kind sop
"""

NOTE_EPILOG = """\b
示例:
  anvil note write notes/login-timeout.md --content-file note-content.md \\
    --kind module_knowledge --scope role --tags auth,timeout
  anvil note write notes/task.md --content-file note-content.md --scope task \\
    --task $ANVIL_TASK_ID --practice-task $ANVIL_TASK_ID --practice-run $ANVIL_RUN_ID

规则:
  · 文件是真相层；写入会同步文件与索引。
  · 角色知识必须有成功实践与提炼证据；无证据只能写 draft。
  · 不要直接导入其他角色的 notes；参考可以，写入自己的知识库必须经过实践。
"""

SELF_EPILOG = """\b
动作:
  show | whoami                       当前身份、任务与会话
  edit-instruction --file <path>      修改自己的指令（human 审批）
  propose --field <f> --file <path>   提交角色演化提案
  reset-context --task <id>           清空当前任务上下文

规则:
  · 身份来自 ANVIL_TOKEN / ANVIL_AGENT_SLUG；不要伪造角色。
  · 上下文接近上限或切换任务时先 reset-context，避免旧上下文污染。
"""

REMINDER_EPILOG = """\b
示例:
  anvil reminder add --in 30m --prompt "检查构建结果"
  anvil reminder add --cron "0 9 * * *" --prompt "每日站会"
  anvil reminder list
  anvil reminder remove <id>
  anvil reminder fire-now <id>

规则: 定时提醒 run 没有任务（task=None），不要 claim/submit；按提示维护知识库后退出。
"""

LESSON_EPILOG = """\b
示例:
  anvil lesson propose "评审前先复算证据哈希" --tags review,evidence
  anvil lesson propose "跨角色交接只认 step submit 的冻结 hash" --repo shared

规则: 共享经验进入 human 审批队列；任务结论留在 task snapshot，不要写这里。
"""

WORKSPACE_EPILOG = """\b
示例:
  anvil workspace list
  anvil workspace create main --repo gomoku
  anvil workspace knowledge list <workspace-id>
  anvil workspace knowledge propose <id> --title "..." --tldr "..." --kind convention
  anvil workspace knowledge approve <id> <knowledge-id>    # human

规则: 频道是任务归属 + 知识作用域，不是聊天室；任务通过 `task create --workspace` 绑定。
"""

ROLE_EPILOG = """\b
示例:
  anvil role list --json
  anvil role list | python3 -c "import sys,json; print([r['slug'] for r in json.load(sys.stdin)])"
  anvil role propose --task $ANVIL_TASK_ID --file roles.json
  anvil role expand --brief "需要一个安全评审角色" --slug security-auditor --task $ANVIL_TASK_ID

规则:
  · role propose 先提交提案 @human；批准后再 role expand 创建、task assign 加入任务。
  · role expand 只能创建 human 已批准提案中的 slug；多角色提案用 --slug 指定。
  · role expand 继承你的 runtime/permission_mode，新角色为普通 member。
  · 每日扩展配额有限；创建后会在关联任务 @human。
"""

class GuidedTyper(typer.Typer):
    """Typer 参数错误统一追加 --help 提示，减少 Agent 凭直觉猜命令。"""

    def __call__(self, *args, **kwargs):
        kwargs.setdefault("standalone_mode", False)
        try:
            return super().__call__(*args, **kwargs)
        except ClickUsageError as exc:
            exc.show()
            typer.echo("hint: 运行 `anvil <command> -h` 查看完整用法与示例。",
                       err=True)
            raise SystemExit(exc.exit_code)
        except ClickExit as exc:
            raise SystemExit(exc.exit_code)


app = GuidedTyper(help="Anvil 本地多 Agent 编排服务 CLI", no_args_is_help=True,
                  context_settings=HELP_CTX, epilog=MAIN_EPILOG)


def _usage_error(message: str, hint: str | None = None) -> None:
    """统一的参数错误：一行原因 + 一行可执行提示，退出码 2。"""
    typer.echo(f"error: {message}", err=True)
    if hint:
        typer.echo(f"hint: {hint}", err=True)
    raise typer.Exit(2)

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


def _server_hint(status: int) -> str | None:
    if status in (400, 422):
        return "参数不合法；运行 `anvil <command> --help` 查看用法与示例。"
    if status == 401:
        return "会话缺失或过期；检查 ANVIL_TOKEN 是否与后端一致。"
    if status == 403:
        return "权限不足；人类操作需 human token，角色操作需对应 run token。"
    if status == 404:
        return "目标不存在或不属于当前任务；检查 id 是否正确。"
    if status == 409:
        return "状态冲突；先用 task show / step show 确认当前状态，不要盲目重试。"
    if status >= 500:
        return "后端内部错误；查看 anvil 服务端日志。"
    return None


def _call(method: str, path: str, body: Any = None, params: dict | None = None) -> Any:
    try:
        with _client() as c:
            r = c.request(method, path, json=body, params=params, headers=_headers())
    except httpx.HTTPError as exc:
        typer.echo(f"error: 无法连接后端: {exc}", err=True)
        typer.echo("hint: 确认后端已启动（cd dev/backend && python run_server.py），"
                   "或设置 ANVIL_ENDPOINT。", err=True)
        raise typer.Exit(1)
    if r.status_code >= 300:
        detail = r.text
        try:
            payload = r.json()
            if isinstance(payload, dict):
                detail = payload.get("detail") or payload.get("message") or r.text
        except ValueError:
            pass
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, ensure_ascii=False)
        typer.echo(f"error: {detail}", err=True)
        hint = _server_hint(r.status_code)
        if hint:
            typer.echo(f"hint: {hint}", err=True)
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


@app.command("self", help="当前身份、会话与自我维护。", epilog=SELF_EPILOG,
              short_help="身份/会话：show/reset-context")
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
            _usage_error(
                "self edit-instruction 需要 --file",
                "传入本地文件路径；例如 `anvil self edit-instruction --file instruction.md`。")
        try:
            content = open(file, encoding="utf-8").read()
        except OSError as exc:
            _usage_error(f"无法读取 --file: {exc}", "检查文件路径与读权限。")
        _emit(_call("POST", "/api/v1/me/instruction", {"content": content}), json_out)
    elif action == "propose":
        if not field or not file:
            _usage_error(
                "self propose 需要 --field 与 --file",
                "例如 `anvil self propose --field responsibilities --file proposal.json`；"
                "文件内容必须是 JSON。")
        try:
            proposed = json.loads(open(file, encoding="utf-8").read())
        except (OSError, json.JSONDecodeError) as exc:
            _usage_error(f"无法读取提案 JSON: {exc}", "检查 --file 路径与 JSON 语法。")
        _emit(_call("POST", "/api/v1/proposals", {
            "field": field, "proposed": proposed,
            "rationale": rationale, "risk": risk,
        }), json_out)
    elif action == "reset-context":
        tid = task or DEFAULT_TASK
        if not tid:
            _usage_error(
                "self reset-context 需要任务 id",
                "传 --task <id>，或设置 ANVIL_TASK_ID。")
        _emit(_call("POST", "/api/v1/me/session/reset", {
            "task_id": tid, "reason": reason or "",
        }), json_out)
    else:
        _usage_error(
            f"unknown self action: {action}",
            "可用动作: show/edit-instruction/propose/reset-context。运行 `anvil self -h`。")


@app.command("whoami")
def whoami_cmd(json_out: bool = typer.Option(False, "--json", help="输出 JSON")):
    """anvil whoami — self show 的别名（M1 出口命令）。"""
    _emit(_call("GET", "/api/v1/me"), json_out)


# ---- task ---------------------------------------------------------------


@app.command("task",
              help="任务操作入口（动作与示例见下方）。",
              epilog=TASK_EPILOG,
              short_help="任务：列表/创建/认领/评论/提交/知识")
def task_cmd(
    action: str = typer.Argument("list"),
    target: str = typer.Argument(None),
    body: str = typer.Argument(None),
    type_: str = typer.Option(None, "--type", help="list 过滤：任务类型",
                              rich_help_panel="查询 / 创建"),
    tag: str = typer.Option(None, "--tag", help="list 过滤：标签",
                            rich_help_panel="查询 / 创建"),
    tags: str = typer.Option(None, "--tags", help="create：标签（逗号分隔）",
                             rich_help_panel="查询 / 创建"),
    repo: str = typer.Option(None, "--repo", help="create：执行仓库（单值）",
                             rich_help_panel="查询 / 创建"),
    workspace: str = typer.Option(None, "--workspace", help="create：频道/workspace id",
                                  rich_help_panel="查询 / 创建"),
    base_branch: str = typer.Option(None, "--base-branch", help="create：基线分支",
                                    rich_help_panel="查询 / 创建"),
    plan_first: bool = typer.Option(False, "--plan-first",
                                    help="create：先由 planner 规划",
                                    rich_help_panel="查询 / 创建"),
    planner: str = typer.Option(None, "--planner", help="create：planner 角色",
                                rich_help_panel="查询 / 创建"),
    knowledge_repos: str = typer.Option(
        None, "--knowledge-repos", help="create：知识仓库（逗号分隔；人工指定会加锁）",
        rich_help_panel="查询 / 创建"),
    parent: str = typer.Option(None, "--parent", help="父任务 id（create 子任务）",
                               rich_help_panel="查询 / 创建"),
    title: str = typer.Option(None, "--title", help="create：任务标题",
                              rich_help_panel="查询 / 创建"),
    description: str = typer.Option(None, "--description", help="create：任务描述",
                                    rich_help_panel="查询 / 创建"),
    assignee: str = typer.Option(None, "--assignee",
                                 help="create：单负责人角色",
                                 rich_help_panel="查询 / 创建"),
    assignees: str = typer.Option(None, "--assignees",
                                  help="create/assign：团队角色（逗号分隔；assign 仅限现有角色）",
                                  rich_help_panel="查询 / 创建"),
    on: str = typer.Option(None, "--on", help="block：依赖的任务 id",
                           rich_help_panel="执行 / 协作"),
    to: str = typer.Option(None, "--to", help="mention：目标角色或 human",
                           rich_help_panel="执行 / 协作"),
    summary: str = typer.Option(None, "--summary", help="submit：摘要",
                                rich_help_panel="执行 / 协作"),
    message: str = typer.Option(None, "--message", "-m",
                                help="comment/mention：正文（等价位置参数 body）",
                                rich_help_panel="执行 / 协作"),
    paths: str = typer.Option(None, "--paths", help="claim：认领范围路径（逗号分隔）",
                              rich_help_panel="执行 / 协作"),
    force: bool = typer.Option(False, "--force",
                               help="delete 强制；comment/mention 知情越过 held",
                               rich_help_panel="执行 / 协作"),
    expect_activity: int = typer.Option(None, "--expect-activity",
                                        help="Held Draft：发送时依据的最新活动 id",
                                        rich_help_panel="执行 / 协作"),
    kind: str = typer.Option(None, "--kind", help="knowledge-write：知识类型",
                             rich_help_panel="任务知识"),
    tldr: str = typer.Option(None, "--tldr", help="knowledge-write：一句话摘要",
                             rich_help_panel="任务知识"),
    fields: str = typer.Option(None, "--fields", help="knowledge-write：字段 JSON",
                               rich_help_panel="任务知识"),
    knowledge_id: str = typer.Option(None, "--id", help="knowledge-delete：知识 id",
                                     rich_help_panel="任务知识"),
    include_subtasks: bool = typer.Option(False, "--include-subtasks",
                                         help="knowledge-list：包含直接子任务知识",
                                         rich_help_panel="任务知识"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON",
                                  rich_help_panel="输出"),
):
    """anvil task <list|show|create|assign|claim|unclaim|heartbeat|submit|comment|mention|cancel|reopen|delete|block|archive|unarchive|knowledge-list|knowledge-write|knowledge-delete> …"""
    tid = target or DEFAULT_TASK
    needs_target = {"show", "claim", "unclaim", "heartbeat", "submit", "comment", "mention",
                    "cancel", "reopen", "delete", "block", "assign", "archive", "unarchive",
                    "knowledge-list", "knowledge-write", "knowledge-delete"}
    if action in needs_target and not tid:
        _usage_error(
            f"task {action} 需要任务 id",
            "传入 <id>，或设置 ANVIL_TASK_ID；例如 `anvil task show T-0001`。")
    if message is not None and action not in ("comment", "mention"):
        _usage_error(
            f"task {action} 不支持 --message",
            "comment/mention 用位置参数或 --message；submit 用 --summary；"
            "create 用 --description。运行 `anvil task -h` 查看示例。")
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
            _usage_error(
                "task assign 需要 --assignees a,b",
                "只接受已存在的角色；例如 `anvil task assign T-0001 --assignees pm,tester`。")
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
        if not to:
            _usage_error(
                "task mention 需要 --to <role|human>",
                "例如 `anvil task mention T-0001 --to tester \"请复核\"`；"
                "@ 文本不会唤醒角色。")
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
        if not on:
            _usage_error(
                "task block 需要 --on <task-id>",
                "例如 `anvil task block T-0002 --on T-0001` 表示 T-0002 依赖 T-0001。")
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
            _usage_error(
                "task knowledge-write 缺少必填参数",
                "需要 --title、--tldr、--kind；例如 "
                "`anvil task knowledge-write T-0001 --title \"登录超时\" "
                "--tldr \"token 过期未刷新\" --kind task_finding`。")
        parsed_fields: dict = {}
        if fields:
            try:
                parsed_fields = json.loads(fields)
            except json.JSONDecodeError as exc:
                _usage_error(f"--fields 不是合法 JSON: {exc}",
                             "传 JSON 对象，例如 --fields '{\"finding\":\"...\"}'。")
            if not isinstance(parsed_fields, dict):
                _usage_error("--fields 必须是 JSON 对象",
                             "不要传数组或字符串；例如 --fields '{\"finding\":\"...\"}'。")
        payload = {"title": title, "tldr": tldr, "kind": kind, "fields": parsed_fields}
        if tags:
            payload["tags"] = [x.strip() for x in tags.split(",") if x.strip()]
        method, path = "POST", f"/api/v1/tasks/{tid}/knowledge"
    elif action == "knowledge-delete":
        if not knowledge_id:
            _usage_error(
                "task knowledge-delete 需要 --id <knowledge-id>",
                "先 `anvil task knowledge-list <task-id>` 找到 id，再删除。")
        method, path, payload = "DELETE", f"/api/v1/tasks/{tid}/knowledge/{knowledge_id}", None
    else:
        _usage_error(
            f"unknown task action: {action}",
            "可用动作: list/show/create/assign/claim/unclaim/heartbeat/submit/"
            "comment/mention/cancel/reopen/delete/block/archive/unarchive/"
            "knowledge-list/knowledge-write/knowledge-delete。运行 `anvil task -h` 查看示例。")
    result = _call(method, path, payload, params)
    if action == "assign" and isinstance(result, dict) \
            and result.get("status") == "pending_review":
        if json_out:
            _emit(result, True)
        else:
            typer.echo(result.get("message") or "角色追加提案已提交，等待 human 审批。")
            if result.get("draft_id"):
                typer.echo(f"提案 id: {result['draft_id']}；"
                           "human 反馈/批准后重新执行本命令。")
        return
    _emit(result, json_out)


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


@app.command("note", help="写入角色笔记（文件为真相，实践门控）。", epilog=NOTE_EPILOG,
              short_help="角色笔记：写入（实践门控）")
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


@app.command("knowledge", help="知识检索、索引与知识卡。", epilog=KNOWLEDGE_EPILOG,
              short_help="知识：检索/索引/仓库/知识卡")
def knowledge_cmd(
    action: str = typer.Argument("search"),
    query: str = typer.Argument(None),
    note_id: str = typer.Option(None, "--id", help="show：笔记 id",
                                rich_help_panel="检索"),
    task: str = typer.Option(None, "--task", help="任务 id（默认 $ANVIL_TASK_ID）",
                             rich_help_panel="检索"),
    scope: str = typer.Option("auto", "--scope", help="auto|role|task",
                              rich_help_panel="检索"),
    kind: str = typer.Option(None, "--kind", help="过滤 kind（逗号分隔）",
                             rich_help_panel="检索"),
    tags: str = typer.Option(None, "--tags", help="过滤标签（逗号分隔）",
                             rich_help_panel="检索"),
    repo: str = typer.Option(None, "--repo", help="过滤单个知识仓库",
                             rich_help_panel="检索"),
    repos: str = typer.Option(None, "--repos", help="限定知识仓库（逗号分隔）",
                              rich_help_panel="检索"),
    module: str = typer.Option(None, "--module", help="过滤模块",
                               rich_help_panel="检索"),
    include_snapshots: bool = typer.Option(False, "--include-snapshots",
                                           help="包含任务快照",
                                           rich_help_panel="检索"),
    include_unclassified: bool = typer.Option(False, "--include-unclassified",
                                              help="包含未分类笔记",
                                              rich_help_panel="检索"),
    limit: int = typer.Option(8, "--limit", help="最多返回条数",
                              rich_help_panel="检索"),
    role: str = typer.Option(None, "--role", help="human 浏览角色知识时指定角色",
                             rich_help_panel="索引 / 角色"),
    self_only: bool = typer.Option(False, "--self", help="reindex 当前角色",
                                   rich_help_panel="索引 / 角色"),
    all_roles: bool = typer.Option(False, "--all", help="reindex 所有角色（human）",
                                   rich_help_panel="索引 / 角色"),
    dry_run: bool = typer.Option(False, "--dry-run", help="reindex 只预览不写入",
                                 rich_help_panel="索引 / 角色"),
    prune: bool = typer.Option(False, "--prune", help="reindex 清理磁盘已删除文件的行",
                               rich_help_panel="索引 / 角色"),
    fields: str = typer.Option(None, "--fields", help="card-validate：字段 JSON",
                               rich_help_panel="知识卡"),
    file: str = typer.Option(None, "--file", help="card-validate：知识卡 JSON 文件",
                             rich_help_panel="知识卡"),
    for_approval: bool = typer.Option(False, "--for-approval",
                                      help="card-validate：按 approve 必填项校验",
                                      rich_help_panel="知识卡"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON",
                                  rich_help_panel="输出"),
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
            _usage_error(
                "knowledge repo-show 需要仓库名",
                "传 --repo <name>，或 `anvil knowledge repo-list` 先查看可用仓库。")
        from urllib.parse import quote
        _emit(_call("GET", f"/api/v1/knowledge/repos/{quote(name, safe='')}"), json_out)
    elif action == "repo-suggest":
        if not query:
            _usage_error(
                "knowledge repo-suggest 需要任务标题",
                "例如 `anvil knowledge repo-suggest \"修复登录超时\"`。")
        _emit(_call("POST", "/api/v1/knowledge/repos/suggest", {
            "title": query,
            "tags": [x.strip() for x in tags.split(",")] if tags else [],
            "repo": repo,
        }), json_out)
    elif action == "show":
        if not note_id:
            _usage_error(
                "knowledge show 需要 --id <note-id>",
                "先用 `anvil knowledge search \"关键词\"` 或 `anvil knowledge list` 找到 id。")
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
                _usage_error(f"无法读取知识卡文件: {exc}",
                             "检查 --file 路径与 JSON 语法；模板用 "
                             "`anvil knowledge card-template --kind <kind>`。")
        if not isinstance(payload, dict):
            _usage_error("知识卡文件必须是 JSON 对象",
                         "顶层必须是对象；模板用 `anvil knowledge card-template`。")
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
        _usage_error(
            f"unknown knowledge action: {action}",
            "可用动作: search/show/index/list/reindex/repo-list/repo-show/"
            "repo-suggest/card-template/card-validate。运行 `anvil knowledge -h` 查看示例。")


@app.command("lesson", help="提议一条共享经验（human 审批）。", epilog=LESSON_EPILOG,
              short_help="共享经验：propose")
def lesson_cmd(
    action: str = typer.Argument("propose"),
    text: str = typer.Argument(None),
    tags: str = typer.Option(None, "--tags", help="逗号分隔标签"),
    repo: str = typer.Option(None, "--repo", help="归属共享仓库（默认 shared）"),
    json_out: bool = typer.Option(False, "--json"),
):
    """anvil lesson propose <text> [--tags a,b] [--repo name]"""
    if action == "propose":
        if not text:
            _usage_error(
                "lesson propose 需要经验正文",
                "例如 `anvil lesson propose \"评审前先复算证据哈希\" --tags review`。")
        _emit(_call("POST", "/api/v1/lessons/pending", {
            "content": text,
            "tags": [t.strip() for t in tags.split(",")] if tags else [],
            "repo": repo,
        }), json_out)
    else:
        _usage_error(
            f"unknown lesson action: {action}",
            "只支持 propose；例如 `anvil lesson propose \"...\"`。")


# ---- reminder / ws ------------------------------------------------------


@app.command("reminder", help="定时提醒（cron / 延时 / 条件）。", epilog=REMINDER_EPILOG,
              short_help="定时提醒：add/list/remove/fire-now")
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
        if sum(bool(x) for x in (in_, when, cron)) != 1:
            _usage_error(
                "reminder add 需要且只能指定 --in / --when / --cron 之一",
                "示例: --in 30m、--when <task-id>、--cron \"0 9 * * *\"。")
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
            _usage_error("reminder remove 需要 id",
                         "先 `anvil reminder list` 查看 id，再 `anvil reminder remove <id>`。")
        _emit(_call("DELETE", f"/api/v1/reminders/{target}"), json_out)
    elif action == "fire-now":
        if not target:
            _usage_error("reminder fire-now 需要 id",
                         "先 `anvil reminder list` 查看 id，再 `anvil reminder fire-now <id>`。")
        _emit(_call("POST", f"/api/v1/reminders/{target}/fire-now", {}), json_out)
    else:
        _usage_error(
            f"unknown reminder action: {action}",
            "可用动作: add/list/remove/fire-now。运行 `anvil reminder -h` 查看示例。")


def _parse_duration(text: str) -> int:
    """解析 30m / 2h / 1d。"""
    units = {"m": 60, "h": 3600, "d": 86400, "s": 1}
    try:
        unit = text[-1].lower()
        return int(float(text[:-1]) * units[unit])
    except (KeyError, ValueError, IndexError):
        _usage_error(f"无法解析时长: {text}",
                     "使用 <数字><单位>，单位支持 s/m/h/d；例如 30m、2h、1d。")


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
        _usage_error(
            f"unknown ws action: {action}",
            "可用动作: status/diff；例如 `anvil ws status 0`。")


# ---- workspace（频道）与频道知识 ------------------------------------------


workspace_app = typer.Typer(help="频道（任务归属 + 知识作用域 + 资产）",
                            context_settings=HELP_CTX, epilog=WORKSPACE_EPILOG)
app.add_typer(workspace_app, name="workspace")
ws_knowledge_app = typer.Typer(help="频道知识卡（Agent 只能 propose，human 批准）",
                               context_settings=HELP_CTX, epilog="""\b
示例:
  anvil workspace knowledge list W-1 --status proposed
  anvil workspace knowledge propose W-1 --title "发布门禁" --tldr "P0 全过才可发布" \\
    --kind convention --authority normative
  anvil workspace knowledge approve W-1 K-1 --expected-version 1    # human
  anvil workspace knowledge harvest W-1 T-1
  anvil workspace knowledge review W-1

规则: Agent 写入为 proposed；approve 需要 human；harvest 只接受 done/closed 任务。
""")
workspace_app.add_typer(ws_knowledge_app, name="knowledge")


def _fields_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        _usage_error(f"--fields 不是合法 JSON: {exc}",
                     "传 JSON 对象，例如 --fields '{\"rule\":\"...\"}'。")
    if not isinstance(value, dict):
        _usage_error("--fields 必须是 JSON 对象",
                     "不要传数组或字符串；例如 --fields '{\"rule\":\"...\"}'。")
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


role_app = typer.Typer(help="角色（列出现有角色 / Agent 创建新 agent）",
                       context_settings=HELP_CTX, epilog=ROLE_EPILOG)
app.add_typer(role_app, name="role")


@role_app.command("list")
def role_list(json_out: bool = typer.Option(False, "--json")):
    """列出当前系统中的角色（slug、名称、状态、画像）。"""
    _emit(_call("GET", "/api/v1/roles"), json_out)


@role_app.command("propose", epilog="""\b
roles.json:
  {"rationale":"缺少安全审计能力",
   "suggested_roles":[{"slug":"security-auditor","display_name":"安全审计员",
     "description":"负责代码安全审计","responsibilities":["审计代码"],
     "capabilities":{"security-audit":0.9}}]}

示例:
  anvil role propose --task $ANVIL_TASK_ID --file roles.json
  anvil role propose --task T-0001 --file roles.json --json

流程: 提案 @human → human 反馈/批准 → Cindy 执行 role expand 创建 → task assign 加入任务。
""")
def role_propose(
    file: str = typer.Option(..., "--file", help="角色提案 JSON 文件"),
    task: str = typer.Option(DEFAULT_TASK, "--task", help="关联任务 id"),
    goal: str = typer.Option(None, "--goal", help="提案目标一句话"),
    json_out: bool = typer.Option(False, "--json"),
):
    """提交新角色提案给 human；批准后再创建并加入任务。"""
    if not task:
        _usage_error("role propose 需要任务 id",
                     "传 --task <id> 或设置 ANVIL_TASK_ID。")
    try:
        with open(file, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _usage_error(f"无法读取角色提案: {exc}",
                     "检查 --file 路径与 JSON 语法；格式见 `anvil role propose -h`。")
    if isinstance(payload, list):
        roles = payload
        rationale = goal or "角色追加提案"
    elif isinstance(payload, dict):
        roles = payload.get("suggested_roles") or payload.get("roles") or []
        rationale = goal or str(payload.get("rationale") or "角色追加提案")
    else:
        _usage_error("角色提案必须是 JSON 对象或数组",
                     "格式见 `anvil role propose -h`。")
    if not isinstance(roles, list) or not roles:
        _usage_error("角色提案缺少 suggested_roles",
                     "格式见 `anvil role propose -h`。")
    normalized = []
    for item in roles:
        if not isinstance(item, dict):
            _usage_error("suggested_roles 的每一项必须是对象",
                         "至少包含 slug 与 display_name。")
        slug = str(item.get("slug") or "").strip()
        display = str(item.get("display_name") or "").strip()
        if not slug or not display:
            _usage_error("每个角色需要 slug 与 display_name",
                         "格式见 `anvil role propose -h`。")
        normalized.append({**item, "slug": slug, "display_name": display})
    tree = {"apply_mode": "authorize", "rationale": rationale,
            "requested_assignees": [r["slug"] for r in normalized],
            "suggested_roles": normalized}
    draft = _call("POST", "/api/v1/board/drafts", {
        "goal": rationale, "source_task": task, "kind": "role_suggestion",
        "tree": tree,
    }) or {}
    draft_id = str(draft.get("id") or "")
    summary = "、".join(r["slug"] for r in normalized)
    _call("POST", f"/api/v1/tasks/{task}/mention", {
        "target": "human",
        "message": f"角色提案已提交（{summary}，draft {draft_id or '?'}）。"
                   "请审批或反馈；批准后再创建并加入任务。",
    })
    if json_out:
        _emit(draft, True)
    else:
        typer.echo(f"角色提案已提交：{draft_id or '?'}（{summary}），等待 human 审批。")
        typer.echo("批准后执行 role expand --slug <已批准 slug> 创建角色，再 task assign 加入任务。")


@role_app.command("expand")
def role_expand(
    brief: str = typer.Option(..., "--brief", help="新 agent 的一句话定位"),
    task: str = typer.Option(DEFAULT_TASK, "--task", help="关联任务 id（会通知 human）"),
    slug: str = typer.Option(None, "--slug",
                             help="提案中 human 已批准的角色 slug（多角色提案时必填）"),
    json_out: bool = typer.Option(False, "--json"),
):
    """创建 human 已批准的新 agent（继承你的 runtime；每日配额有限）。"""
    payload = {"brief": brief, "task_id": task}
    if slug:
        payload["slug"] = slug
    out = _call("POST", "/api/v1/roles/expand", payload)
    if json_out:
        _emit(out, True)
    else:
        typer.echo(f"新 agent 已创建：{out['display_name']}（{out['slug']}）")


# ---- action（Action Cards：Agent 准备，人类执行）---------------------------


action_app = typer.Typer(help="Action Cards（危险操作先申请，人类批准后执行）",
                         context_settings=HELP_CTX)
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


inbox_app = typer.Typer(help="Agent 收件箱（拉模式）", context_settings=HELP_CTX)
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


REVISE_EPILOG = """\b
示例:
  anvil workflow revise WF-1 --file revision.json --preview
  anvil workflow revise WF-1 --file revision.json

revision.json 只描述变更 intent（add_slices/remove_slices/update_slices/
patch_steps）；后端会重新计算影响。安全修订进入收件箱等 human 批准，
requires_restart 时请改交完整 workflow 草案，由 human 决定是否重启。
"""

workflow_app = typer.Typer(
    help="运行中 workflow 的增量修订（不重启执行）",
    context_settings=HELP_CTX,
    epilog=REVISE_EPILOG)
app.add_typer(workflow_app, name="workflow")


@workflow_app.command("revise", epilog=REVISE_EPILOG)
def workflow_revise(
    workflow_id: str = typer.Argument(..., help="工作流 ID"),
    file: str = typer.Option(..., "--file", help="修订 intent JSON 文件"),
    preview: bool = typer.Option(False, "--preview", help="只分析影响，不创建草案"),
    rationale: str = typer.Option(None, "--rationale", help="修订原因/目标"),
    json_out: bool = typer.Option(False, "--json"),
):
    """提交或预览运行中 workflow 的增量修订。"""
    try:
        with open(file, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _usage_error(f"无法读取修订文件: {exc}",
                     "检查 --file 路径与 JSON 语法；示例见 `anvil workflow revise -h`。")
    if not isinstance(payload, dict):
        _usage_error("修订文件必须是 JSON 对象",
                     "顶层包含 intent；示例见 `anvil workflow revise -h`。")
    intent = payload.get("intent") if isinstance(payload.get("intent"), dict) else payload
    reason = rationale or payload.get("rationale") or payload.get("goal")
    if preview:
        out = _call("POST", f"/api/v1/workflows/{workflow_id}/revisions/preview",
                    {"intent": intent})
        if json_out:
            _emit(out, True)
            return
        typer.echo(f"classification: {out.get('classification')}  "
                   f"safe: {out.get('safe')}")
        typer.echo(out.get("summary") or "")
        for item in out.get("blocked_by") or []:
            typer.echo(f"  blocked {item.get('step_id')}: "
                       f"{item.get('status')} · {item.get('reason')}")
        return
    draft = _call("POST", f"/api/v1/workflows/{workflow_id}/revisions",
                  {"intent": intent, "rationale": reason}) or {}
    impact = (draft.get("tree") or {}).get("impact") or {}
    if json_out:
        _emit(draft, True)
    else:
        typer.echo(f"修订草案已提交：{draft.get('id')}（等待 human 批准）")
        typer.echo(impact.get("summary") or "")
        typer.echo("批准后原地应用，不重启当前执行。")


step_app = typer.Typer(help="工作流步骤（show/plan/submit/decide/escalate）",
                       context_settings=HELP_CTX, epilog=STEP_EPILOG)
app.add_typer(step_app, name="step")


@step_app.command("submit", epilog="""\b
示例:
  anvil step submit WF-1 S2 --summary "接口完成" --evidence-file evidence.json
  anvil step submit WF-1 S2 --summary "修复完成" --changed-paths src/auth.ts,src/api.ts

规则: 只对 running 步骤提交；不要用 task submit。409 时先 `anvil step show WF-1`。
""")
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
        try:
            with open(evidence_file, encoding="utf-8") as fh:
                body["evidence"] = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            _usage_error(f"无法读取 --evidence-file: {exc}",
                         "传 JSON 数组文件；例如 "
                         "[{\"kind\":\"test\",\"ref\":\"pytest tests/ -q\",\"label\":\"全绿\"}]。")
        if not isinstance(body["evidence"], list):
            _usage_error("--evidence-file 顶层必须是 JSON 数组",
                         "例如 [{\"kind\":\"file\",\"ref\":\"src/auth.ts\"}]。")
    out = _call("POST", f"/api/v1/workflows/{workflow_id}/steps/{step_id}/submit", body)
    if json_out:
        _emit(out, True)
    else:
        typer.echo(f"步骤 {step_id} 已提交（{'等待人工审核' if any(s['step_id'] == step_id and s['status'] == 'submitted' and s['gate'] for s in out.get('steps', [])) else '已通过，工作流继续推进'}）")


def _csv(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


@step_app.command("decide", epilog="""\b
示例:
  anvil step decide WF-1 S6 --approve --feedback "证据充分，通过"
  anvil step decide WF-1 S6 --reject --feedback "缺少回归证据" --rework-targets S3,S4

规则: 必须且只能给 --approve 或 --reject；reject 未指定范围时只重做当前步骤。
""")
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
        _usage_error(
            "必须且只能给 --approve 或 --reject",
            "例如 `anvil step decide WF-1 S6 --approve --feedback \"通过\"`。")
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


@step_app.command("escalate", epilog="""\b
示例:
  anvil step escalate WF-1 S6 --reason "高风险发布，证据不足" --proposed-decision reject
  anvil step escalate WF-1 S6 --reason "需要人工确认" --triggers risk,compliance

规则: 只用于 decision 步骤；升级后 packet 留给自然人最终决定。
""")
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


@step_app.command("packet", epilog="""\b
示例:
  anvil step packet WF-1 S6
  anvil step packet WF-1 S6 --json

用于查看 gate/decision 的 review packet、证据与历史决策。
""")
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


@step_app.command("plan", epilog="""\b
plan.json 最小结构:
  {"rationale":"按模块切片",
   "slices":[{"id":"auth","title":"认证模块",
              "scope_paths":["src/auth/**"],"objective":"...",
              "instructions":"...","acceptance":["..."],
              "execute_steps":[{"id":"static","title":"静态审计",
                                "instructions":"..."}]}]}

示例:
  anvil step plan WF-1 S1 --file plan.json

规则: 切片只描述范围/粒度/轮次，不得指定角色或 loop 阶段；角色由 Cindy 的 loop 模板定义。
""")
def step_plan(
    workflow_id: str = typer.Argument(..., help="工作流 ID"),
    step_id: str = typer.Argument(..., help="plan_slices 步骤 ID"),
    file: str = typer.Option(..., "--file", help="切片计划 JSON 文件"),
    json_out: bool = typer.Option(False, "--json"),
):
    """提交切片计划（plan_slices 步骤）：服务端按 loop 模板生成每片小闭环。"""
    try:
        with open(file, encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        _usage_error(f"无法读取切片计划: {exc}",
                     "检查 --file 路径与 JSON 语法；最小结构见 `anvil step plan -h`。")
    if not isinstance(plan, dict) or not isinstance(plan.get("slices"), list):
        _usage_error("切片计划必须是包含 slices 数组的 JSON 对象",
                     "最小结构见 `anvil step plan -h`；空项目可用 \"slices\": []。")
    out = _call("POST", f"/api/v1/workflows/{workflow_id}/steps/{step_id}/plan", plan)
    if json_out:
        _emit(out, True)
        return
    generated = [s for s in out.get("steps", []) if s.get("source_step_id") == step_id]
    typer.echo(f"切片计划已提交：生成 {len(generated)} 个切片步骤")


@step_app.command("show", epilog="""\b
示例:
  anvil step show WF-1
  anvil step show WF-1 --json

输出每个步骤的状态、角色、依赖与 summary；pending 步骤下方会给出未被激活的原因
（依赖未完成、退避、角色忙、活跃 run、lease 未释放、治理闸门等）。
""")
def step_show(
    workflow_id: str = typer.Argument(..., help="工作流 ID"),
    json_out: bool = typer.Option(False, "--json"),
):
    """查看工作流各步骤状态。"""
    out = _call("GET", f"/api/v1/workflows/{workflow_id}")
    if json_out:
        _emit(out, True)
        return
    dispatch = out.get("dispatch") or {}
    typer.echo(f"工作流 {workflow_id}  状态 {out.get('status')}  "
               f"更新 {out.get('updated_at') or '-'}")
    if dispatch.get("note"):
        typer.echo(f"注意：{dispatch['note']}")
    revision = out.get("pending_revision")
    if revision:
        impact = revision.get("impact") or {}
        typer.echo(f"待审批修订：{revision.get('draft_id')} · "
                   f"{impact.get('classification')} · {impact.get('summary')}")
    reasons_by_step = {
        item["step_id"]: item.get("reasons") or []
        for item in dispatch.get("pending") or []
    }
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
        if st.get("status") == "pending":
            reasons = reasons_by_step.get(st["step_id"]) or []
            if reasons:
                typer.echo(f"     ↳ {'; '.join(reasons)}")


# ---- lease（step/path 租约与路径预留）------------------------------------


lease_app = typer.Typer(help="执行租约与路径预留", context_settings=HELP_CTX)
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


board_app = typer.Typer(help="工作流草案（计划先行）", context_settings=HELP_CTX)
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
