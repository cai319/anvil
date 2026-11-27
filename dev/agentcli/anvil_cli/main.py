"""anvil CLI (Typer) — maps §7.2 commands to the REST API (§4 / SE v1.0).

Exit codes (design §7.3): 0 ok · 1 generic · 2 usage · 3 conflict · 4 forbidden.
"""
from __future__ import annotations

import codecs
import json
import locale
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import typer

try:
    from . import __version__
except ImportError:  # pragma: no cover - 支持直接执行 main.py 的开发场景
    __version__ = "unknown"

try:  # Typer 0.27+ 内置 click；旧版本仍用外部 click
    from typer._click.core import Exit as ClickExit
    from typer._click.exceptions import UsageError as ClickUsageError
except ImportError:  # pragma: no cover - 兼容 typer<0.27
    from click.exceptions import Exit as ClickExit
    from click.exceptions import UsageError as ClickUsageError

# Windows 管道/重定向输出统一 UTF-8：agent 工具结果与日志按 UTF-8 读取，
# 若 CLI 按系统代码页（中文 Windows cp936）输出就会变成乱码。
_CJK_CODECS = {
    "gbk", "gb2312", "gb18030", "hz",
    "big5", "cp950", "cp932", "shift_jis",
    "cp949", "euc_kr", "johab",
}


def _force_utf8_stdio() -> None:
    if os.name != "nt" or "pytest" in sys.modules:
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            if (stream is not None and hasattr(stream, "reconfigure")
                    and not stream.isatty()):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def _try_decode(raw: bytes, encoding: str) -> str | None:
    try:
        return raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return None


def _is_cjk_char(ch: str) -> bool:
    return (
        "㐀" <= ch <= "䶿"      # CJK 扩展 A
        or "一" <= ch <= "鿿"   # CJK 基本区
        or "　" <= ch <= "〿"   # CJK 标点
        or "＀" <= ch <= "￯"   # 全角字符
        or "぀" <= ch <= "ヿ"   # 日文假名
        or "가" <= ch <= "힯"   # 谚文
        or "𠀀" <= ch <= "𿿿"   # CJK 扩展 B+
    )


def _cjk_stats(text: str) -> tuple[int, int]:
    """返回 (CJK 字符数, 最长连续 CJK 串长度)。"""
    count = 0
    run = 0
    best = 0
    for ch in text:
        if _is_cjk_char(ch):
            count += 1
            run += 1
            best = max(best, run)
        else:
            run = 0
    return count, best


def _fallback_encoding() -> str:
    """系统 ANSI 代码页（规范化 codec 名，忽略 UTF-8 mode）。"""
    try:
        getter = getattr(locale, "getencoding", None)
        encoding = getter() if getter is not None else locale.getpreferredencoding(False)
    except Exception:  # noqa: BLE001
        return "utf-8"
    try:
        return codecs.lookup(encoding or "utf-8").name
    except LookupError:
        return encoding or "utf-8"


def _decode_file_bytes(data: bytes) -> str:
    """文件解码：UTF-8/UTF-16/UTF-32（含 BOM）、无 BOM UTF-16、GBK。"""
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig", errors="replace")
    if data.startswith(codecs.BOM_UTF32_LE) or data.startswith(codecs.BOM_UTF32_BE):
        return data.decode("utf-32", errors="replace")
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        return data.decode("utf-16", errors="replace")
    if len(data) >= 4 and data.count(0) > len(data) // 4:
        even_nuls = data[0::2].count(0)
        odd_nuls = data[1::2].count(0)
        if odd_nuls > even_nuls * 4:
            return data.decode("utf-16-le", errors="replace")
        if even_nuls > odd_nuls * 4:
            return data.decode("utf-16-be", errors="replace")
    text = _try_decode(data, "utf-8")
    if text is not None:
        return text
    pref = _fallback_encoding()
    if pref in _CJK_CODECS:
        text = _try_decode(data, pref)
        if text is not None:
            return text
    high = sum(1 for byte in data if byte >= 0x80)
    if high >= 4:
        text = _try_decode(data, "gb18030")
        if text is not None:
            cjk, run = _cjk_stats(text)
            if cjk >= 2 and run >= 2 and high == 2 * cjk:
                return text
    for enc in (pref, "gb18030", "cp1252"):
        text = _try_decode(data, enc)
        if text is not None:
            return text
    return data.decode("utf-8", errors="replace")


def _read_text_file(path: str) -> str:
    """读文本文件（兼容 UTF-8/UTF-16/GBK）；不可读时抛 OSError。

    与 Path.read_text 一致：\r\n / \r 归一为 \n，避免 Windows 读改写出现 \r\r\n。
    """
    with open(path, "rb") as fh:
        text = _decode_file_bytes(fh.read())
    return text.replace("\r\n", "\n").replace("\r", "\n")


_force_utf8_stdio()

HELP_CTX = {"help_option_names": ["-h", "--help"], "max_content_width": 100}

MAIN_EPILOG = """\b
常用流程:
  anvil task list                     查看任务
  anvil task show <id>                查看任务详情
  anvil task claim <id>               认领任务（工作流步骤不要 claim）
  anvil task comment <id> "进展"      同步进展（不唤醒）
  anvil task mention <id> --to r "…"  同切片复核/追问（复用上下文）
  anvil task dispatch <id> --to r "…" 无关工作 → 来源 task 下的派遣子任务
  anvil inbox check --task <id>       拉取当前切片未读（--task-wide 看全任务）
  anvil step show <workflow-id>       查看工作流步骤
  anvil step submit <wf> <step> --summary "完成"
  anvil role list                     查看已有角色

问题板:
  anvil finding template                      生成 finding JSON 模板与枚举
  anvil finding add --file f.json --submit    reviewer 提交一条 finding
  anvil finding complete --summary "..."      完成问题板，进入 human 最终评审
  anvil task reviewers <id> --assignees r     Cindy/human 指定 reviewer

规则:
  · 工作流步骤用 anvil step，不要用 task claim/submit 代替。
  · 问题板 finding 只有 task reviewer 能写；human 在 Web 逐条确认真实/误报。
  · 身份由 ANVIL_TOKEN / ANVIL_TASK_ID 环境变量决定；不要伪造角色。
  · 删除、审批、重置等人类操作需要 human token。
  · 退出码: 0 成功 / 1 通用失败 / 2 参数错误 / 3 状态冲突 / 4 无权限。
  · 面向 human 的消息（@human、submit 摘要、评审说明）要让非技术读者看懂：
    先写结论与需要人类做什么，给选项/推荐/下一步；交付件列性质、内容简介与绝对路径，
    没有交付件就写「本次无具体交付件」。
  · 每个命令都支持 -h / --help，例如 anvil task -h。
  · 文件参数用当前目录相对路径或绝对路径；Windows 临时目录用 %TEMP%，
    不要写死 /tmp（macOS/Linux 的 /tmp 在 Windows 不存在）。
"""

TASK_EPILOG = """\b
动作:
  list [--type T] [--tag T]             列出任务
  show <id>                             查看任务详情
  delegations <id>                      查看父任务下的委派工单与父 step 状态
  create --title "..." [--assignee r | --assignees a,b] [--issue-board]
                                        创建任务或委派工单（--parent/--parent-step）
  assign <id> --assignees a,b           追加角色；已有 Roster 时自动提案 @human
  reviewers <id> --assignees a,b        指定问题板 reviewer（planner/onboarding/human）
  claim | unclaim <id>                  认领/释放（工作流步骤不要 claim）
  submit <id> "摘要"                    提交任务（不是工作流步骤）
  delegation-accept <id>                派发者验收委派工单
  delegation-reject <id> --feedback "…" 派发者打回委派工单
  comment <id> "正文"                   评论，不唤醒任何人
  note <id> --for-step <step> "正文"    系统备注：带入未来步骤上下文，不唤醒
  mention <id> --to <role> "正文"       @提及并唤醒；自动复用/切换切片上下文
          [--slice S] [--context-mode auto|reuse|switch|dispatch]
  clarify <id> --file <brief.json>      更新规划前澄清简报草稿（不确认、不跳过）
  dispatch <id> --to <role> "正文"      创建来源 task 下的派遣子任务（不阻塞，submit 即 done）
          [--slice S] [--context-mode dispatch] [--acceptance "a;b"]
  archive | unarchive <id>              归档/取消归档（human）
  knowledge-list <id>                   任务共享知识
  knowledge-write <id> --title --tldr --kind   写任务知识（知识收割者仅 proposed）
  knowledge-delete <id> --id <kid>      删除任务知识（human）

示例:
  anvil task create "修复登录超时" --assignee developer --workspace W-1
  anvil task create "安全审计" --issue-board --assignees auditor
  anvil task create "构造 PoC" --parent $ANVIL_TASK_ID --parent-step S3 \\
    --assignee vuln-verifier --acceptance "PoC 可复现" --scope-paths poc/ \\
    --due-at 2026-12-01T18:00:00+08:00
  anvil task delegation-accept <子任务ID>
  anvil task delegation-reject <子任务ID> --feedback "缺少命令输出"
  anvil task reviewers $ANVIL_TASK_ID --assignees auditor
  anvil task comment $ANVIL_TASK_ID "已完成接口联调"
  anvil task mention $ANVIL_TASK_ID --to tester "请复跑回归"        # 同切片复核：复用上下文
  anvil task mention $ANVIL_TASK_ID --to developer --slice A "A 模块异常，请复核"
  anvil task clarify $ANVIL_TASK_ID --file brief.json           # 规划前澄清：更新简报草稿
  anvil task dispatch $ANVIL_TASK_ID --to developer --slice A \\
    -m "修复另一个领域的缓存问题" --acceptance "问题复现;修复完成"

提示: 工作流任务先看 `anvil step show <workflow-id>`；步骤操作不要用 task claim/submit。
任务已有 assignees 时，assign 只追加新角色，并先自动提交提案 @human；批准后重试即可。
@human / submit 摘要要面向非技术读者：先写结论与需要人类做什么，给选项/推荐/下一步；
涉及交付件时列性质、内容简介与绝对路径，没有交付件就写「本次无具体交付件」。
上下文规则:
  · 同 task 同 slice 的复核/追问用 mention，复用当前上下文，不要 dispatch。
  · 同 task 已知 slice 的反馈会自动切回该 slice 上下文。
  · 与当前切片弱相关、需要新上下文时用 dispatch；它是来源 task 下的派遣子任务，
    共享 workspace、不阻塞 workflow，submit 即 done。
  · 现有 `POST /tasks/{id}/dispatch`/“启动”仍是 human 的规划入口，不是派遣。
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
  · 评审/挑战/澄清用 `anvil task mention`；切片外、不影响本步骤完成且需新上下文的工作用
    `anvil task dispatch`；只有验收依赖另一角色独立交付物（如独立 PoC/复现）且 workflow
    未排定时，才用 `anvil task create --parent ... --parent-step ...` 建委派，并在提交前验收。
  · 同一角色不能被委派自己的当前 step（会自锁）；跨角色才会建委派。
  · `anvil inbox check` 默认只看当前切片；要拉全任务需显式 `--task-wide`。
  · submit 摘要、@human 与评审说明要让非技术读者看懂：先写结论与需要人类做什么，
    给选项/推荐/下一步；交付件列性质、内容简介与绝对路径。
"""

# 未知 action 提示与 -h 的动作用同一份清单，避免新增命令后只更新一处。
KNOWLEDGE_ACTIONS = (
    "search", "show", "index", "list", "reindex", "fill",
    "repo-list", "repo-show", "repo-suggest", "repo-create", "repo-cards",
    "card-create", "card-template", "card-validate", "card-approve", "card-reject",
    "quality", "clean", "feedback", "review", "explain",
)

KNOWLEDGE_EPILOG = """\b
动作:
  search "关键词" --task ID             检索自己的角色/任务知识
  show --id <note-id>                   查看一条笔记/知识
  index | list                          角色知识计数与最近笔记
  reindex --dry-run                     文件变更后重建索引
  fill --id <note-id> --title "..." --tldr "..." \
       --content-file body.md --field use_when="..."   补全/精炼角色知识
  repo-list                             知识仓库列表
  repo-show --repo <name>               仓库详情
  repo-suggest "任务标题"               推荐知识仓库
  repo-create <name> --fields '{...}'   创建主题知识仓库
  repo-cards --repo <name>              主题下的知识卡（--kind 过滤状态）
  card-create <repo> --file card.json   创建主题知识卡
  card-template --kind convention       知识卡模板
  card-validate --file card.json        校验知识卡
  quality [--repo <name>]               知识质量概览
  clean --id <card-id> [--apply]        LLM 清洗单卡（默认 dry-run）
  clean --repo <name> [--kind proposed] 批量清洗（默认 dry-run）
  feedback --id <card-id> --signal ...  记录知识反馈
  review [--repo <name>]                查看到期/低反馈/legacy 卡片
  review --id <id> --review-action ...  复核单卡（keep/deprecate/refresh）
  card-approve | card-reject --id <id>  human 审批/驳回主题卡
  explain --task <id>                   解释本任务注入了哪些知识

示例:
  anvil knowledge search "登录 超时" --task $ANVIL_TASK_ID --limit 5
  anvil knowledge quality --repo openssl
  anvil knowledge clean --id K-1 --apply
  anvil knowledge feedback --id K-1 --signal helpful
  anvil knowledge review --repo openssl
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
  reset-context --task <id>           清空当前运行上下文（只清当前切片/task 级槽）

规则:
  · 身份来自 ANVIL_TOKEN / ANVIL_AGENT_SLUG；不要伪造角色。
  · 切片上下文相互隔离；reset-context 不会清同 task 的其他 slice。
  · 上下文接近上限或切换任务/切片时先 reset-context，避免旧上下文污染。
"""

REMINDER_EPILOG = """\b
示例:
  anvil reminder add --in 30m --prompt "检查构建结果"
  anvil reminder add --cron "0 9 * * *" --prompt "每日站会"
  anvil reminder add --when <task-id> --prompt "任务完成后复盘"
  anvil reminder list
  anvil reminder remove <id>
  anvil reminder fire-now <id>

规则: 定时提醒 run 没有任务（task=None），不要 claim/submit；按提示维护知识库后退出。
系统会在 Cindy 规划、human 落板 workflow 后自动创建 when(task done) 知识收割提醒，
无需手工添加；--when 用于额外提醒。
"""

LESSON_EPILOG = """\b
示例:
  anvil lesson propose "正文：观察、证据、结论与执行方式" \\
      --title "一句话结论" --tldr "摘要" --tags review,evidence \\
      --field use_when="任务涉及 X509 时" \\
      --field expected_effect="避免环境差异误判，减少返工" \\
      --field evidence="任务/run/文件/评审记录" \\
      --field observation="观察到的事实" --field lesson="提炼出的结论" \\
      --field applicability="适用任务/仓库/条件" --field counterexamples="反例/不适用"
  anvil lesson fill <lesson-id> --title "补全后的标题" --tldr "摘要" \\
      --field use_when="..." --field expected_effect="..." --field evidence="..." \\
      --field observation="..." --field lesson="..." --field applicability="..." \\
      --field counterexamples="..."

规则: 共享经验进入 human 审批队列；任务结论留在 task snapshot，不要写这里。
title 必须由提交者概括（≤80 字），不要用正文首行代替；详细内容放正文/字段。
知识提交前的必须条款：title/tldr/正文 + use_when/expected_effect/evidence +
observation/lesson/applicability/counterexamples 必须完整；缺项返回 422。
fill 只允许收割角色（source_role）或 human 调用，用于角色用来源任务上下文补全字段。
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


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"anvil {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", callback=_show_version, is_eager=True,
        help="显示 CLI 版本并退出"),
) -> None:
    """Anvil 本地多 Agent 编排服务 CLI。"""


def _usage_error(message: str, hint: str | None = None) -> None:
    """统一的参数错误：一行原因 + 一行可执行提示，退出码 2。"""
    typer.echo(f"error: {message}", err=True)
    if hint:
        typer.echo(f"hint: {hint}", err=True)
    raise typer.Exit(2)

ENDPOINT = os.environ.get("ANVIL_ENDPOINT", "http://127.0.0.1:7317")


def _read_token_file() -> str:
    """人类 CLI 的可选 token 文件；Agent 由 ANVIL_TOKEN 注入运行级 token。"""
    candidates = []
    if os.environ.get("ANVIL_TOKEN_FILE"):
        candidates.append(Path(os.environ["ANVIL_TOKEN_FILE"]))
    if os.environ.get("ANVIL_DATA_DIR"):
        candidates.append(Path(os.environ["ANVIL_DATA_DIR"]) / "auth-token")
    # 源码/打包安装的项目根 data/auth-token（与后端默认 DATA_DIR 一致）。
    candidates.append(
        Path(__file__).resolve().parents[2].parent / "data" / "auth-token")
    candidates.append(Path.cwd() / "data" / "auth-token")
    candidates.append(Path.home() / ".config" / "Anvil" / "token")
    for candidate in candidates:
        try:
            value = candidate.expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return ""


# 不再使用公开默认 token；Agent 靠 ANVIL_TOKEN，人类可用 env 或 token 文件。
TOKEN = os.environ.get("ANVIL_TOKEN", "").strip() or _read_token_file()
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
    if not TOKEN:
        typer.echo("error: 未配置 Anvil token", err=True)
        typer.echo(
            "hint: Agent 使用后端注入的 ANVIL_TOKEN；人类可设置 ANVIL_TOKEN 或把 token "
            "写入 ~/.config/Anvil/token（数据目录 auth-token）。", err=True)
        raise typer.Exit(2)
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
):
    """首启引导：只发现运行时。Cindy 由后端启动时默认创建；不创建频道/角色模板。"""
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

    # 即使没有 ready 运行时也确保内置助理存在；画像由后端定义，CLI 只触发幂等初始化。
    try:
        cindy = _call("POST", "/api/v1/roles/onboarding/ensure") or {}
        typer.echo(f"Cindy 任务助理已就绪：{cindy.get('display_name') or 'Cindy · 规划助理'}")
    except typer.Exit:
        typer.echo("提示：当前后端未提供 Cindy 自动初始化；重启后端后会自动创建。", err=True)

    if not ready:
        return
    typer.echo("下一步：打开界面创建频道和角色；Cindy 已由后端默认启用。")


@app.command("self", help="当前身份、会话与自我维护。", epilog=SELF_EPILOG,
              short_help="身份/会话：show/edit-instruction/propose/reset-context")
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
            content = _read_text_file(file)
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
            proposed = json.loads(_read_text_file(file))
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


@app.command("rsi", help="角色 RSI：观测、触发状态、复盘与回放评估。",
             short_help="RSI：show/trigger/retro/eval")
def rsi_cmd(
    action: str = typer.Argument("show"),
    role: str = typer.Option(None, "--role", help="角色 slug（默认 ANVIL_AGENT_SLUG）"),
    task: str = typer.Option(None, "--task", help="retro 的任务 id（默认 ANVIL_TASK_ID）"),
    file: str = typer.Option(None, "--file", help="retro 内容文件"),
    suite: str = typer.Option(None, "--suite", help="eval 的隐藏回放 suite id"),
    candidate: str = typer.Option(None, "--candidate", help="eval 的候选 id"),
    version: int = typer.Option(None, "--version", help="eval 的候选版本号"),
    baseline: int = typer.Option(None, "--baseline", help="eval 的基线版本号"),
    limit: int = typer.Option(50, "--limit", help="观测条数上限"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON"),
):
    """anvil rsi <show|observations|trigger|retro|candidates|eval>"""
    slug = (role or AGENT_SLUG or "").strip()
    if not slug:
        _usage_error("rsi 需要角色 slug", "传 --role，或由运行时注入 ANVIL_AGENT_SLUG。")
    if action in ("show", "observations"):
        _emit(_call("GET", f"/api/v1/rsi/roles/{slug}/observations",
                    params={"limit": limit}), json_out)
    elif action == "trigger":
        _emit(_call("GET", f"/api/v1/rsi/roles/{slug}/trigger"), json_out)
    elif action == "retro":
        tid = task or DEFAULT_TASK
        if not tid or not file:
            _usage_error("rsi retro 需要 --task 与 --file",
                         "例如 `anvil rsi retro --task T-1 --file retro.md`。")
        try:
            content = _read_text_file(file)
        except OSError as exc:
            _usage_error(f"无法读取 --file: {exc}", "检查文件路径与读权限。")
        _emit(_call("POST", f"/api/v1/rsi/roles/{slug}/retro",
                    {"task_id": tid, "content": content}), json_out)
    elif action == "candidates":
        _emit(_call("GET", "/api/v1/rsi/candidates",
                    params={"role": slug}), json_out)
    elif action == "eval":
        if not suite or (not candidate and not version):
            _usage_error(
                "rsi eval 需要 --suite 与 --candidate 或 --version",
                "例如 `anvil rsi eval --suite default --candidate P-...`。")
        payload = {"suite_id": suite, "candidate_id": candidate,
                   "candidate_version": version, "baseline_version": baseline}
        _emit(_call("POST", f"/api/v1/rsi/roles/{slug}/evaluations",
                    {k: v for k, v in payload.items() if v is not None}),
              json_out)
    else:
        _usage_error(f"unknown rsi action: {action}",
                     "可用动作: show/observations/trigger/retro/candidates/eval。")


@app.command("runtime", help="运行时部件改进提案（RIP/RTP）。",
             short_help="运行时提案：propose/evidence/list/show/export")
def runtime_cmd(
    action: str = typer.Argument("proposals"),
    proposal_id: str = typer.Argument(None),
    component_type: str = typer.Option(None, "--type", help="skill/mcp_server/..."),
    component_key: str = typer.Option(None, "--component", help="组件 key"),
    title: str = typer.Option(None, "--title", help="提案标题"),
    summary_file: str = typer.Option(None, "--summary-file", help="现象/影响摘要文件"),
    proposed_file: str = typer.Option(None, "--proposed-file", help="建议修改文件"),
    evidence_file: str = typer.Option(None, "--evidence-file", help="证据 JSON 文件"),
    severity: str = typer.Option("major", "--severity"),
    status: str = typer.Option(None, "--status"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON"),
):
    """anvil runtime <propose|evidence|proposals|show|export>"""
    if action == "proposals":
        _emit(_call("GET", "/api/v1/runtime-proposals",
                    params={"status": status} if status else None), json_out)
    elif action == "show":
        if not proposal_id:
            _usage_error("runtime show 需要提案 id", "例如 `anvil runtime show RTP-...`。")
        _emit(_call("GET", f"/api/v1/runtime-proposals/{proposal_id}"), json_out)
    elif action == "propose":
        if not component_type or not component_key or not title:
            _usage_error("runtime propose 需要 --type/--component/--title",
                         "用 `anvil runtime propose -h` 查看参数。")
        try:
            summary = _read_text_file(summary_file) if summary_file else ""
            proposed = _read_text_file(proposed_file) if proposed_file else ""
            evidence = (json.loads(_read_text_file(evidence_file))
                        if evidence_file else {})
        except (OSError, json.JSONDecodeError) as exc:
            _usage_error(f"无法读取文件: {exc}", "检查文件路径与 JSON 语法。")
        _emit(_call("POST", "/api/v1/runtime-proposals", {
            "component_type": component_type, "component_key": component_key,
            "title": title, "summary": summary or title,
            "proposed_change_md": proposed or "待补充",
            "severity": severity, "evidence": evidence,
        }), json_out)
    elif action == "evidence":
        if not proposal_id or not evidence_file:
            _usage_error("runtime evidence 需要提案 id 与 --evidence-file",
                         "例如 `anvil runtime evidence RTP-... --evidence-file e.json`。")
        try:
            evidence = json.loads(_read_text_file(evidence_file))
        except (OSError, json.JSONDecodeError) as exc:
            _usage_error(f"无法读取证据 JSON: {exc}", "检查文件路径与 JSON 语法。")
        _emit(_call("POST", f"/api/v1/runtime-proposals/{proposal_id}/evidence",
                    {"evidence": evidence}), json_out)
    elif action == "export":
        if not proposal_id:
            _usage_error("runtime export 需要提案 id", "例如 `anvil runtime export RTP-...`。")
        _emit(_call("POST", f"/api/v1/runtime-proposals/{proposal_id}/export"), json_out)
    else:
        _usage_error(f"unknown runtime action: {action}",
                     "可用动作: propose/evidence/proposals/show/export。")


@app.command("whoami")
def whoami_cmd(json_out: bool = typer.Option(False, "--json", help="输出 JSON")):
    """anvil whoami — self show 的别名（M1 出口命令）。"""
    _emit(_call("GET", "/api/v1/me"), json_out)


# ---- task ---------------------------------------------------------------


@app.command("task",
              help="任务操作入口（动作与示例见下方）。",
              epilog=TASK_EPILOG,
              short_help="任务：列表/创建/认领/评论/提交/派遣/知识")
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
    repo: str = typer.Option(None, "--repo", help="create：主知识主题（知识 repo 名）",
                             rich_help_panel="查询 / 创建"),
    source_repo: str = typer.Option(
        None, "--source-repo", help="create：任务数据来源 Git 仓库（可选，不是知识 repo）",
        rich_help_panel="查询 / 创建"),
    workspace: str = typer.Option(None, "--workspace", help="create：频道/workspace id",
                                  rich_help_panel="查询 / 创建"),
    base_branch: str = typer.Option(None, "--base-branch", help="create：基线分支",
                                    rich_help_panel="查询 / 创建"),
    plan_first: bool = typer.Option(False, "--plan-first",
                                    help="create：先由 planner 规划",
                                    rich_help_panel="查询 / 创建"),
    issue_board: bool = typer.Option(False, "--issue-board",
                                     help="create：启用问题板（仅 human）",
                                     rich_help_panel="查询 / 创建"),
    planner: str = typer.Option(None, "--planner", help="create：planner 角色",
                                rich_help_panel="查询 / 创建"),
    knowledge_repos: str = typer.Option(
        None, "--knowledge-repos", help="create：知识仓库（逗号分隔；人工指定会加锁）",
        rich_help_panel="查询 / 创建"),
    parent: str = typer.Option(None, "--parent", help="父任务 id（create 子任务）",
                               rich_help_panel="查询 / 创建"),
    parent_step: str = typer.Option(
        None, "--parent-step",
        help="create：父 workflow step id（委派工单必须带）",
        rich_help_panel="查询 / 创建"),
    acceptance: str = typer.Option(
        None, "--acceptance",
        help="create：验收标准，分号分隔",
        rich_help_panel="查询 / 创建"),
    scope_paths: str = typer.Option(
        None, "--scope-paths",
        help="create：范围路径，逗号分隔",
        rich_help_panel="查询 / 创建"),
    due_at: str = typer.Option(
        None, "--due-at",
        help="create：委派截止时间，ISO8601（如 2026-12-01T18:00:00+08:00）",
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
    to: str = typer.Option(None, "--to",
                           help="mention/dispatch：目标角色或 human（写给 human 时"
                                "用平实、自包含的说明：诉求/背景/选项/交付件/下一步）",
                           rich_help_panel="执行 / 协作"),
    slice_key: str = typer.Option(
        None, "--slice",
        help="mention/dispatch：来源切片 key；mention 用它标注请求所属切片",
        rich_help_panel="执行 / 协作"),
    context_mode: str = typer.Option(
        None, "--context-mode",
        help="mention/dispatch：auto|reuse|switch|dispatch（默认 auto；"
             "显式覆盖自动路由）",
        rich_help_panel="执行 / 协作"),
    summary: str = typer.Option(
        None, "--summary",
        help="submit：摘要（结论 + 交付件清单；让非技术读者看懂；涉及交付件时"
             "逐项写性质/内容简介/绝对路径）",
        rich_help_panel="执行 / 协作"),
    feedback: str = typer.Option(
        None, "--feedback",
        help="delegation-reject：打回原因",
        rich_help_panel="执行 / 协作"),
    for_step: str = typer.Option(
        None, "--for-step",
        help="note：该备注面向的未来步骤 id",
        rich_help_panel="执行 / 协作"),
    file: str = typer.Option(
        None, "--file", help="clarify：任务简报 JSON 文件",
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
    """anvil task <list|show|delegations|create|assign|reviewers|claim|unclaim|heartbeat|submit|comment|mention|clarify|dispatch|delegation-accept|delegation-reject|cancel|reopen|delete|block|archive|unarchive|knowledge-list|knowledge-write|knowledge-delete> …"""
    tid = target or DEFAULT_TASK
    needs_target = {"show", "delegations", "delegation-list", "note",
                    "claim", "unclaim", "heartbeat", "submit", "comment", "mention",
                    "dispatch", "delegation-accept", "delegation-reject",
                    "cancel", "reopen", "delete", "block", "assign", "reviewers",
                    "archive", "unarchive", "knowledge-list", "knowledge-write",
                    "knowledge-delete", "clarify"}
    if action in needs_target and not tid:
        _usage_error(
            f"task {action} 需要任务 id",
            "传入 <id>，或设置 ANVIL_TASK_ID；例如 `anvil task show T-0001`。")
    if message is not None and action not in ("comment", "mention", "dispatch"):
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
    elif action in ("delegations", "delegation-list"):
        method, path = "GET", f"/api/v1/tasks/{tid}/delegations"
    elif action == "create":
        p = {"title": title or body or "untitled"}
        if type_:
            p["type"] = type_
        if description:
            p["description"] = description
        if parent:
            p["parent_id"] = parent
        if parent_step:
            p["parent_step_id"] = parent_step.strip()
        if acceptance:
            p["acceptance"] = [x.strip() for x in acceptance.split(";") if x.strip()]
        if scope_paths:
            p["scope_paths"] = [x.strip() for x in scope_paths.split(",") if x.strip()]
        if due_at:
            p["due_at"] = due_at.strip()
        if tags:
            p["tags"] = [x.strip() for x in tags.split(",") if x.strip()]
        if repo:
            p["repo"] = repo.strip()
        if source_repo:
            p["source_repo"] = source_repo.strip()
        if workspace:
            p["workspace_id"] = workspace.strip()
        if base_branch:
            p["base_branch"] = base_branch.strip()
        if plan_first:
            p["plan_first"] = True
        if issue_board:
            p["issue_board_enabled"] = True
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
    elif action == "reviewers":
        roles = [x.strip() for x in (assignees or "").split(",") if x.strip()]
        if not roles:
            _usage_error(
                "task reviewers 需要 --assignees a,b",
                "仅接受已存在的角色；例如 `anvil task reviewers T-0001 --assignees auditor`。")
        method, path, payload = "POST", f"/api/v1/tasks/{tid}/reviewers", {"roles": roles}
    elif action == "claim":
        p = {"paths": [x.strip() for x in paths.split(",")]} if paths else {}
        method, path, payload = "POST", f"/api/v1/tasks/{tid}/claim", p
    elif action == "unclaim":
        method, path, payload = "POST", f"/api/v1/tasks/{tid}/unclaim", {}
    elif action == "heartbeat":
        method, path, payload = "POST", f"/api/v1/tasks/{tid}/heartbeat", {}
    elif action == "submit":
        method, path, payload = "POST", f"/api/v1/tasks/{tid}/submit", {"summary": summary or body}
    elif action == "delegation-accept":
        method, path, payload = "POST", f"/api/v1/tasks/{tid}/delegation/accept", {}
    elif action == "delegation-reject":
        reason = (feedback or body_text).strip()
        if not reason:
            _usage_error(
                "task delegation-reject 需要 --feedback \"原因\"",
                "例如 `anvil task delegation-reject <子任务ID> --feedback \"缺少命令输出\"`。")
        method, path, payload = (
            "POST", f"/api/v1/tasks/{tid}/delegation/reject", {"feedback": reason})
    elif action == "dispatch":
        if not to:
            _usage_error(
                "task dispatch 需要 --to <role>",
                "例如 `anvil task dispatch $ANVIL_TASK_ID --to dev "
                "-m \"处理另一个领域的问题\" --slice A`。")
        if not body_text:
            _usage_error(
                "task dispatch 需要请求正文",
                "用位置参数或 -m/--message 传入；它会成为派遣子任务的描述。")
        payload = {
            "target_role": to.strip(),
            "message": body_text,
            "slice_key": (slice_key or "").strip() or None,
        }
        if title:
            payload["title"] = title
        if acceptance:
            payload["acceptance"] = [x.strip() for x in acceptance.split(";") if x.strip()]
        if scope_paths:
            payload["scope_paths"] = [x.strip() for x in scope_paths.split(",") if x.strip()]
        if repo or workspace or base_branch:
            typer.echo(
                "warning: dispatch 子任务继承来源 task 的 repo/workspace/base-branch，"
                "--repo/--workspace/--base-branch 对 dispatch 不生效。", err=True)
        if context_mode:
            payload["context_mode"] = context_mode.strip()
        if force:
            payload["force"] = True
        method, path = "POST", f"/api/v1/tasks/{tid}/dispatches"
    elif action == "note":
        if not body_text:
            _usage_error(
                "task note 需要正文",
                "例如 `anvil task note $ANVIL_TASK_ID --for-step S3 \"该产出已由委派处理\"`。")
        payload = {"body": body_text}
        if for_step:
            payload["for_step"] = for_step.strip()
        method, path = "POST", f"/api/v1/tasks/{tid}/note"
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
        if slice_key:
            payload["slice_key"] = slice_key.strip()
        if context_mode:
            payload["context_mode"] = context_mode.strip()
        if expect_activity:
            payload["expect_activity_id"] = expect_activity
        if force:
            payload["force"] = True
        method, path = "POST", f"/api/v1/tasks/{tid}/mention"
    elif action == "clarify":
        if not file:
            _usage_error(
                "task clarify 需要 --file <brief.json>",
                "例如 `anvil task clarify $ANVIL_TASK_ID --file brief.json`；"
                "只更新简报草稿，不确认、不跳过。")
        try:
            brief = json.loads(_read_text_file(file))
        except (OSError, json.JSONDecodeError) as exc:
            _usage_error(f"无法读取简报 JSON: {exc}",
                         "检查文件路径与 JSON 语法。")
        if not isinstance(brief, dict):
            _usage_error("简报必须是 JSON 对象", "顶层必须是 {...}。")
        method, path, payload = (
            "PUT", f"/api/v1/tasks/{tid}/clarification/draft", {"brief": brief})
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
            "可用动作: list/show/create/assign/reviewers/claim/unclaim/heartbeat/"
            "submit/comment/mention/dispatch/cancel/reopen/delete/block/archive/unarchive/"
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
    durable: bool = typer.Option(False, "--durable",
                                 help="写入 PRINCIPLES.md 耐久准则（默认写经验卡）"),
    tags: str = typer.Option(None, "--tags", help="逗号分隔的经验卡标签"),
    kind: str = typer.Option("personal_lesson", "--kind"),
    repo: str = typer.Option(None, "--repo", help="知识主题 repo"),
    role: str = typer.Option(None, "--role", help="compact 目标角色（human 用）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="compact 只预览不写入"),
    json_out: bool = typer.Option(False, "--json"),
):
    """anvil memory <append|search|compact> <text>"""
    if action == "append":
        body = {"content": text, "durable": durable, "kind": kind}
        for key, value in (("practice_task_id", practice_task),
                           ("practice_run_id", practice_run), ("outcome", outcome)):
            if value:
                body[key] = value
        if source_knowledge:
            body["source_knowledge"] = [
                x.strip() for x in source_knowledge.split(",") if x.strip()]
        if tags:
            body["tags"] = [x.strip() for x in tags.split(",") if x.strip()]
        if repo:
            body["repo"] = repo
        if force:
            body["force"] = True
        _emit(_call("POST", "/api/v1/me/memory", body), json_out)
    elif action == "search":
        _emit(_call("GET", "/api/v1/me/memory/search", params={"q": text}), json_out)
    elif action == "compact":
        body = {"dry_run": dry_run}
        if role:
            body["role"] = role
        _emit(_call("POST", "/api/v1/me/memory/compact", body), json_out)
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
                content = _read_text_file(content_file)
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


@app.command("knowledge", help="知识检索、索引、补全、知识卡与质量审批。",
              epilog=KNOWLEDGE_EPILOG,
              short_help="知识：检索/索引/补全/仓库/质量/审批")
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
    field: list[str] = typer.Option(
        None, "--field", help="fill：结构化字段 key=value，可重复",
        rich_help_panel="索引 / 角色"),
    title: str = typer.Option(None, "--title", help="fill：精炼后的标题",
                              rich_help_panel="索引 / 角色"),
    tldr: str = typer.Option(None, "--tldr", help="fill：精炼后的 TLDR",
                             rich_help_panel="索引 / 角色"),
    content_file: str = typer.Option(
        None, "--content-file", help="fill：精炼后的正文文件",
        rich_help_panel="索引 / 角色"),
    file: str = typer.Option(None, "--file", help="card-validate：知识卡 JSON 文件",
                             rich_help_panel="知识卡"),
    for_approval: bool = typer.Option(False, "--for-approval",
                                      help="card-validate：按 approve 必填项校验",
                                      rich_help_panel="知识卡"),
    apply: bool = typer.Option(False, "--apply",
                               help="clean：应用预览写回 proposed（默认 dry-run）",
                               rich_help_panel="知识质量"),
    runtime: str = typer.Option(None, "--runtime",
                                help="clean：指定 utility 运行时",
                                rich_help_panel="知识质量"),
    signal: str = typer.Option(None, "--signal",
                               help="feedback：applied|helpful|unhelpful|no_effect",
                               rich_help_panel="知识质量"),
    review_action: str = typer.Option("keep", "--review-action",
                                      help="review：keep|deprecate|refresh",
                                      rich_help_panel="知识质量"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON",
                                  rich_help_panel="输出"),
):
    """anvil knowledge <search|show|index|list|reindex|repo-*|card-*|quality|clean|feedback|review|explain> ..."""
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
    elif action == "fill":
        if not note_id:
            _usage_error(
                "knowledge fill 需要 --id <note-id>",
                "例如 `anvil knowledge fill K-1 --title \"...\" --tldr \"...\" "
                "--content-file body.md --field use_when=\"...\"`。")
        payload: dict = {"title": title, "tldr": tldr, "fields": {}}
        for raw in field or []:
            if "=" not in raw:
                _usage_error(
                    f"无效字段 {raw!r}",
                    "使用 --field key=value，例如 --field use_when=\"同类任务\"。")
            key, value = raw.split("=", 1)
            if key.strip():
                payload["fields"][key.strip()] = value.strip()
        if content_file:
            try:
                payload["content"] = _read_text_file(content_file)
            except OSError as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(2)
        from urllib.parse import quote
        params = {"role": role} if role else None
        _emit(_call(
            "POST", f"/api/v1/me/knowledge/{quote(note_id, safe='')}/fill",
            payload, params=params), json_out)
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
    elif action == "repo-create":
        if not query:
            _usage_error(
                "knowledge repo-create 需要主题名",
                "例如 `anvil knowledge repo-create openssl-x509 --fields '{\"description\":\"...\"}'`。")
        payload = {"repo": query}
        if fields:
            try:
                payload.update(json.loads(fields))
            except json.JSONDecodeError as exc:
                _usage_error(f"--fields 不是合法 JSON: {exc}", "传 JSON 对象。")
        _emit(_call("POST", "/api/v1/knowledge/repos", payload), json_out)
    elif action == "repo-cards":
        from urllib.parse import quote
        name = repo or query
        if not name:
            _usage_error("knowledge repo-cards 需要主题名", "传 --repo <name>。")
        params = {"status": kind} if kind else None
        _emit(_call("GET", f"/api/v1/knowledge/repos/{quote(name, safe='')}/cards",
                    params=params), json_out)
    elif action == "card-create":
        from urllib.parse import quote
        name = repo or query
        if not name or not file:
            _usage_error("knowledge card-create 需要主题名和 --file",
                         "例如 `anvil knowledge card-create openssl --file card.json`。")
        try:
            payload = json.loads(_read_text_file(file))
        except (OSError, json.JSONDecodeError) as exc:
            _usage_error(f"无法读取知识卡文件: {exc}", "检查 --file 路径与 JSON 语法。")
        _emit(_call("POST", f"/api/v1/knowledge/repos/{quote(name, safe='')}/cards",
                    payload), json_out)
    elif action == "quality":
        params = {"repo": repo} if repo else None
        _emit(_call("GET", "/api/v1/knowledge/quality/summary",
                    params=params), json_out)
    elif action == "clean":
        import time
        from urllib.parse import quote
        if note_id:
            preview = _call(
                "POST", f"/api/v1/knowledge/repo-cards/{quote(note_id, safe='')}/clean",
                {"dry_run": True, "runtime_id": runtime})
            if apply:
                result = _call(
                    "POST",
                    f"/api/v1/knowledge/repo-cards/{quote(note_id, safe='')}/clean",
                    {"dry_run": False, "runtime_id": runtime, "cleaned": preview})
                _emit(result, json_out)
            else:
                _emit(preview, json_out)
        else:
            name = repo or query
            if not name:
                _usage_error("knowledge clean 需要 --id <card-id> 或 --repo <repo>",
                             "单卡用 --id；小批量用 --repo。默认 dry-run。")
            card_result = _call(
                "GET", f"/api/v1/knowledge/repos/{quote(name, safe='')}/cards",
                params={"status": kind or "proposed", "limit": limit})
            cards = card_result if isinstance(card_result, list) else (
                card_result.get("cards", []) if isinstance(card_result, dict) else [])
            results = []
            for card in cards[:limit]:
                try:
                    preview = _call(
                        "POST",
                        f"/api/v1/knowledge/repo-cards/{quote(card['id'], safe='')}/clean",
                        {"dry_run": True, "runtime_id": runtime})
                    item = {"card_id": card["id"], "ok": True, "preview": preview}
                    if apply:
                        item["applied"] = _call(
                            "POST",
                            f"/api/v1/knowledge/repo-cards/{quote(card['id'], safe='')}/clean",
                            {"dry_run": False, "runtime_id": runtime,
                             "cleaned": preview})
                    results.append(item)
                except Exception as exc:  # noqa: BLE001
                    results.append({"card_id": card.get("id"), "ok": False,
                                    "error": str(exc)})
                time.sleep(0.05)
            _emit({"dry_run": not apply, "count": len(results),
                   "results": results}, json_out)
    elif action == "feedback":
        if not note_id or not signal:
            _usage_error("knowledge feedback 需要 --id <card-id> 和 --signal",
                         "signal: applied|helpful|unhelpful|no_effect。")
        _emit(_call("POST", f"/api/v1/knowledge/repo-cards/{note_id}/feedback",
                    {"signal": signal, "note": query or ""}), json_out)
    elif action == "review":
        if note_id:
            _emit(_call("POST", f"/api/v1/knowledge/repo-cards/{note_id}/review",
                        {"action": review_action}), json_out)
        else:
            params = {"repo": repo} if repo else None
            _emit(_call("GET", "/api/v1/knowledge/review-candidates",
                        params=params), json_out)
    elif action in ("card-approve", "card-reject"):
        if not note_id:
            _usage_error(f"knowledge {action} 需要 --id <card-id>", "先 repo-cards 查询。")
        verb = "approve" if action == "card-approve" else "reject"
        _emit(_call("POST", f"/api/v1/knowledge/repo-cards/{note_id}/{verb}", {}), json_out)
    elif action == "explain":
        task_id = task or DEFAULT_TASK
        if not task_id:
            _usage_error("knowledge explain 需要 --task <task-id>",
                         "传 --task，或设置 ANVIL_TASK_ID。")
        params = {"role": role} if role else None
        _emit(_call("GET", f"/api/v1/tasks/{task_id}/knowledge/explain",
                    params=params), json_out)
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
                payload = json.loads(_read_text_file(file))
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
            "可用动作: " + "/".join(KNOWLEDGE_ACTIONS) +
            "。运行 `anvil knowledge -h` 查看示例。")


@app.command("lesson", help="提议/补全共享经验（human 审批）。", epilog=LESSON_EPILOG,
              short_help="共享经验：propose/fill")
def lesson_cmd(
    action: str = typer.Argument("propose"),
    text: str = typer.Argument(None),
    tags: str = typer.Option(None, "--tags", help="逗号分隔标签"),
    repo: str = typer.Option(None, "--repo", help="归属共享仓库（默认 shared）"),
    title: str = typer.Option(None, "--title", help="一句话结论（≤80 字）"),
    tldr: str = typer.Option(None, "--tldr", help="摘要（≤200 字）"),
    content_file: str = typer.Option(
        None, "--content-file", help="fill 时更新正文（从文件读取）"),
    field: list[str] = typer.Option(
        None, "--field", help="结构化字段 key=value，可重复，如 --field use_when=..."),
    json_out: bool = typer.Option(False, "--json"),
):
    """anvil lesson propose <text> ... | anvil lesson fill <id> ..."""
    fields: dict[str, str] = {}
    for raw in field or []:
        if "=" not in raw:
            _usage_error(
                f"无效字段 {raw!r}",
                "使用 --field key=value，例如 --field use_when=\"任务涉及 X509 时\"。")
        key, value = raw.split("=", 1)
        if key.strip():
            fields[key.strip()] = value.strip()
    if action == "propose":
        if not text:
            _usage_error(
                "lesson propose 需要经验正文",
                "例如 `anvil lesson propose \"评审前先复算证据哈希\" --tags review`。")
        if not title:
            _usage_error(
                "lesson propose 需要 --title（≤80 字，概括知识结论）",
                "标题不能从正文首行代替；例如 --title \"先核对 OpenSSL 版本再判断漏洞\"。")
        _emit(_call("POST", "/api/v1/lessons/pending", {
            "content": text,
            "tags": [t.strip() for t in tags.split(",")] if tags else [],
            "repo": repo, "title": title, "tldr": tldr, "fields": fields,
        }), json_out)
    elif action == "fill":
        if not text:
            _usage_error(
                "lesson fill 需要 lesson id",
                "例如 `anvil lesson fill <id> --title \"...\" --field use_when=\"...\"`。")
        payload = {"title": title, "tldr": tldr, "fields": fields}
        if content_file:
            try:
                payload["content"] = _read_text_file(content_file)
            except OSError as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(2)
        from urllib.parse import quote
        _emit(_call(
            "POST", f"/api/v1/lessons/{quote(text, safe='')}/fill",
            payload), json_out)
    else:
        _usage_error(
            f"unknown lesson action: {action}",
            "支持 propose/fill；例如 `anvil lesson fill <id> --field use_when=\"...\"`。")


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


@role_app.command("export")
def role_export_cmd(
    slug: str = typer.Argument(..., help="角色 slug"),
    version: int = typer.Option(..., "--version", help="版本号"),
    out: str = typer.Option(None, "--out", help="输出 zip 路径"),
    json_out: bool = typer.Option(False, "--json"),
):
    """导出角色@版本为 .anvil.zip（human）。"""
    if not TOKEN:
        _usage_error("未配置 Anvil token", "设置 ANVIL_TOKEN 或 ~/.config/Anvil/token。")
    target = Path(out or f"role-{slug}-v{version}.anvil.zip")
    with _client() as c:
        r = c.get(f"/api/v1/roles/{slug}/versions/{version}/export",
                  headers=_headers())
    if r.status_code >= 300:
        _emit({"error": r.text}, json_out)
        raise typer.Exit(1)
    target.write_bytes(r.content)
    _emit({"path": str(target), "bytes": len(r.content)}, json_out)


@role_app.command("import")
def role_import_cmd(
    file: str = typer.Argument(..., help=".anvil.zip 文件"),
    mode: str = typer.Option("new_role", "--mode", help="new_role|add_version"),
    slug: str = typer.Option(None, "--slug", help="目标 slug"),
    allow_charter_change: bool = typer.Option(False, "--allow-charter-change"),
    preview: bool = typer.Option(False, "--preview", help="只预览，不导入"),
    json_out: bool = typer.Option(False, "--json"),
):
    """导入角色@版本（human）；默认不自动启用/切换。"""
    if not TOKEN:
        _usage_error("未配置 Anvil token", "设置 ANVIL_TOKEN 或 ~/.config/Anvil/token。")
    path = Path(file)
    if not path.is_file():
        _usage_error(f"文件不存在: {file}", "检查路径。")
    data = {"mode": mode}
    if slug:
        data["slug"] = slug
    data["allow_charter_change"] = "true" if allow_charter_change else "false"
    endpoint = "/api/v1/roles/import/preview" if preview else "/api/v1/roles/import"
    with path.open("rb") as fh, _client() as c:
        r = c.post(endpoint, headers=_headers(),
                   files={"file": (path.name, fh, "application/zip")},
                   data=data)
    try:
        payload = r.json()
    except ValueError:
        payload = {"error": r.text}
    if r.status_code >= 300:
        _emit(payload, json_out)
        raise typer.Exit(1)
    _emit(payload, json_out)


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
        payload = json.loads(_read_text_file(file))
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


INBOX_EPILOG = """\b
示例:
  anvil inbox check --task $ANVIL_TASK_ID
  anvil inbox commit --task $ANVIL_TASK_ID --up-to 128
  anvil inbox check --task $ANVIL_TASK_ID --slice A
  anvil inbox check --task $ANVIL_TASK_ID --task-wide

规则:
  · 默认按当前 run 的 slice 过滤：本切片 + task-wide 消息，不串其他切片。
  · --slice 显式指定切片；--task-wide 显式拉取整个任务（会看到其他切片原文）。
  · check 只读不推进 cursor；commit 只推进对应 slice/task-wide 的 cursor。
  · slice 与 task-wide 的 cursor 相互独立。
"""

inbox_app = typer.Typer(help="Agent 收件箱（拉模式）", context_settings=HELP_CTX,
                        epilog=INBOX_EPILOG)
app.add_typer(inbox_app, name="inbox")


@inbox_app.command("check")
def inbox_check(
    task: str = typer.Option(DEFAULT_TASK, "--task", help="任务 id（默认 $ANVIL_TASK_ID）"),
    slice_key: str = typer.Option(None, "--slice", help="只看该切片（默认取当前 run 的切片）"),
    task_wide: bool = typer.Option(False, "--task-wide", help="显式拉取整个任务的消息"),
    limit: int = typer.Option(30, "--limit"),
    json_out: bool = typer.Option(False, "--json"),
):
    """拉取 cursor 之后的未读消息（不推进 cursor）。"""
    params: dict = {"task_id": task, "limit": limit}
    if slice_key:
        params["slice_key"] = slice_key
    if task_wide:
        params["task_wide"] = "true"
    out = _call("GET", "/api/v1/me/inbox", params=params)
    if json_out:
        _emit(out, True)
        return
    scope = f" · 切片 {out.get('slice_key')}" if out.get("slice_key") else " · task-wide"
    typer.echo(f"未读 {out['unread']} 条（cursor={out['cursor']}{scope}）")
    for it in out["items"]:
        typer.echo(f"[{it['id']}] {it['role']} · {it['kind']}: {it['text'][:200]}")
    if not out["items"]:
        typer.echo("（无未读消息）")
    else:
        commit_extra = ""
        if out.get("slice_key"):
            commit_extra += f" --slice {out['slice_key']}"
        elif task_wide:
            commit_extra += " --task-wide"
        typer.echo(f"处理完后：anvil inbox commit --task {task}{commit_extra} "
                   f"--up-to {out['items'][-1]['id']}")


@inbox_app.command("commit")
def inbox_commit(
    up_to: int = typer.Option(..., "--up-to", help="已处理到的最大消息 id"),
    task: str = typer.Option(DEFAULT_TASK, "--task", help="任务 id（默认 $ANVIL_TASK_ID）"),
    slice_key: str = typer.Option(None, "--slice", help="提交该切片的 cursor"),
    task_wide: bool = typer.Option(False, "--task-wide", help="提交整个任务的 cursor"),
    json_out: bool = typer.Option(False, "--json"),
):
    """提交 cursor（只前进；崩溃未提交会重复投递，at-least-once）。"""
    payload: dict = {"task_id": task, "up_to": up_to}
    if slice_key:
        payload["slice_key"] = slice_key
    if task_wide:
        payload["task_wide"] = True
    out = _call("POST", "/api/v1/me/inbox/commit", payload)
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

subagent_app = typer.Typer(
    help="运行时子代理（只读观测；由父运行时管理，Anvil 不调度/不唤醒）",
    context_settings=HELP_CTX)
app.add_typer(subagent_app, name="subagent")


@subagent_app.command("list")
def subagent_list(
    run: str = typer.Option(None, "--run", help="按 run id 查询"),
    role: str = typer.Option(None, "--role", help="按角色查询历史"),
    task: str = typer.Option(None, "--task", help="按任务查询"),
    status: str = typer.Option(
        None, "--status", help="running|completed|interrupted"),
    limit: int = typer.Option(50, "--limit"),
    json_out: bool = typer.Option(False, "--json"),
):
    """列出运行时 subagent 观测记录（只读，不创建/终止/唤醒）。"""
    if run:
        path = f"/api/v1/runs/{run}/subagents"
    elif task:
        path = f"/api/v1/tasks/{task}/subagents"
    elif role:
        path = f"/api/v1/roles/{role}/subagents"
    else:
        _usage_error(
            "subagent list 需要 --run / --task / --role 之一",
            "例如 `anvil subagent list --run <run-id>` 或 "
            "`anvil subagent list --role auditor --status interrupted`。")
    params: dict = {"limit": limit}
    if status:
        params["status"] = status
    out = _call("GET", path, params=params)
    if json_out:
        _emit(out, True)
        return
    if not out:
        typer.echo("（无记录）")
        return
    for it in out:
        result = str(it.get("result_summary") or "").replace("\n", " ")[:120]
        typer.echo(
            f"[{it.get('status')}] {it.get('subagent_type') or it.get('tool_name') or 'subagent'}"
            f" · {it.get('description') or ''}"
            f" · run={it.get('run_id')} step={it.get('step_id') or '-'}"
            + (f" · {result}" if result else "")
            + (f" · {it.get('interrupt_reason')}"
               if it.get("interrupt_reason") else ""))


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
        payload = json.loads(_read_text_file(file))
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
摘要要让非技术读者看懂：先写结论与需要人类做什么，给选项/推荐/下一步。
涉及具体交付件时，--summary 必须逐项列出性质、内容简介与绝对路径；无交付件时写「本次提交无具体交付件」。
""")
def step_submit(
    workflow_id: str = typer.Argument(..., help="工作流 ID"),
    step_id: str = typer.Argument(..., help="步骤 ID（如 S1）"),
    summary: str = typer.Option(
        "", "--summary",
        help="完成摘要；让非技术读者看懂；涉及交付件时逐项写性质、内容简介与绝对路径"),
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
            body["evidence"] = json.loads(_read_text_file(evidence_file))
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
        plan = json.loads(_read_text_file(file))
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
    tree = json.loads(_read_text_file(tree_file))
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


# ---- finding（问题板）-----------------------------------------------------

FINDING_EPILOG = """\b
问题板流程（仅 task_reviewers；human 只能批准/驳回）:
  1. anvil finding template > finding.json     生成模板并补全所有必填字段
  2. anvil finding add --file finding.json --submit
  3. anvil finding list                        检查是否还有 draft/missing 字段
  4. 等整个 workflow（所有切片/步骤）完成后：
     anvil finding complete --summary "未发现安全问题；已覆盖 X/Y/Z"
  5. human 在 Web「问题板」页签逐条确认真实/误报

示例:
  anvil finding add --file finding.json --task $ANVIL_TASK_ID --submit
  anvil finding update F-123 --file finding.json --task $ANVIL_TASK_ID
  anvil finding complete --task $ANVIL_TASK_ID --summary "2 条已提交，PoC 见附件"

规则:
  · 只有 task reviewer 能写；human 不能代填，使用页面做真实性确认。
  · PoC 可豁免，但 poc_status=not_verified/not_applicable 时必须在 poc 写明原因。
  · 所有字段提交时必填；缺字段返回 422 与 missing_fields。
  · 工作流步骤 run 只记录本步骤 findings，不要执行 finding complete；
    等整个 workflow（所有切片/步骤）完成后系统会唤醒问题板收口 run。
  · workflow 仍在 running 时 complete 返回 409；后续切片写入已提前完成的问题板会自动重开。
  · 问题板完成且所有 finding 已确认/误报后，任务才能最终通过。
"""

finding_app = typer.Typer(
    help="问题板 finding：reviewer 填写结构化安全 finding，human 在页面确认。",
    context_settings=HELP_CTX,
    epilog=FINDING_EPILOG,
)
app.add_typer(finding_app, name="finding")

FINDING_TEMPLATE = {
    "title": "一句话问题标题",
    "risk_type": "injection",
    "severity": "high",
    "module": "模块/目录",
    "function_name": "问题函数",
    "file_path": "relative/path/to/file.c",
    "line_numbers": "123-145",
    "preconditions": "触发前置条件",
    "reasoning_chain": "从输入到风险的完整推理链",
    "risk_description": "问题风险描述",
    "attack_path": "入口 -> source -> 传播 -> sink；说明数据流",
    "trigger_point": "具体触发点",
    "poc_status": "not_verified",
    "poc": "PoC 复现步骤；未验证时写清原因",
    "fix_priority": "P1",
    "fix_suggestion": "修复建议",
    "confidence": 80,
}


@finding_app.command("template")
def finding_template(json_out: bool = typer.Option(False, "--json")):
    """输出 finding JSON 模板（含枚举与字段说明）。"""
    body = {
        "template": FINDING_TEMPLATE,
        "enums": {
            "risk_type": ["injection", "authn_authz", "info_disclosure", "ssrf",
                          "path_traversal", "deserialization", "race_condition",
                          "memory_safety", "logic_flaw", "crypto", "supply_chain",
                          "dos", "config", "other"],
            "severity": ["critical", "high", "medium", "low", "info"],
            "fix_priority": ["P0", "P1", "P2", "P3"],
            "poc_status": ["provided", "not_verified", "not_applicable"],
            "confidence": "0-100 整数",
        },
    }
    _emit(body, True)


def _finding_task(task: str | None) -> str:
    tid = task or DEFAULT_TASK
    if not tid:
        _usage_error("finding 命令需要任务 id",
                     "传 --task <id> 或设置 ANVIL_TASK_ID。")
    return tid


def _read_json_file(path: str) -> dict:
    try:
        payload = json.loads(_read_text_file(path))
    except (OSError, json.JSONDecodeError) as exc:
        _usage_error(f"无法读取 finding JSON: {exc}",
                     "检查 --file 路径与 JSON 语法；模板见 `anvil finding template`。")
    if not isinstance(payload, dict):
        _usage_error("finding JSON 必须是对象", "见 `anvil finding template`。")
    return payload


@finding_app.command("add")
def finding_add(
    file: str = typer.Option(..., "--file", help="finding JSON 文件"),
    task: str = typer.Option(None, "--task", help="任务 id（默认 $ANVIL_TASK_ID）"),
    submit: bool = typer.Option(False, "--submit", help="创建后立即提交"),
    json_out: bool = typer.Option(False, "--json"),
):
    """创建一条 finding 草稿；--submit 同时提交给 human 确认。"""
    tid = _finding_task(task)
    out = _call("POST", f"/api/v1/tasks/{tid}/findings", _read_json_file(file))
    if submit:
        out = _call("POST", f"/api/v1/tasks/{tid}/findings/{out['id']}/submit")
    _emit(out, json_out)
    if not json_out:
        typer.echo(f"finding 已{'提交' if submit else '保存为草稿'}：{out.get('id')}")


@finding_app.command("update")
def finding_update(
    finding_id: str = typer.Argument(...),
    file: str = typer.Option(..., "--file", help="finding JSON 文件"),
    task: str = typer.Option(None, "--task", help="任务 id（默认 $ANVIL_TASK_ID）"),
    json_out: bool = typer.Option(False, "--json"),
):
    """更新 draft/changes_requested finding。"""
    tid = _finding_task(task)
    _emit(_call("PATCH", f"/api/v1/tasks/{tid}/findings/{finding_id}",
                _read_json_file(file)), json_out)


@finding_app.command("submit")
def finding_submit(
    finding_id: str = typer.Argument(...),
    task: str = typer.Option(None, "--task", help="任务 id（默认 $ANVIL_TASK_ID）"),
    json_out: bool = typer.Option(False, "--json"),
):
    """提交 finding 给 human 确认；所有必填字段必须完整。"""
    tid = _finding_task(task)
    _emit(_call("POST", f"/api/v1/tasks/{tid}/findings/{finding_id}/submit"), json_out)


@finding_app.command("list")
def finding_list(
    task: str = typer.Option(None, "--task", help="任务 id（默认 $ANVIL_TASK_ID）"),
    status: str = typer.Option(None, "--status"),
    severity: str = typer.Option(None, "--severity"),
    json_out: bool = typer.Option(False, "--json"),
):
    """列出当前任务的 findings。"""
    tid = _finding_task(task)
    params = {k: v for k, v in (("status", status), ("severity", severity)) if v}
    _emit(_call("GET", f"/api/v1/tasks/{tid}/findings", params=params), json_out)


@finding_app.command("show")
def finding_show(
    finding_id: str = typer.Argument(...),
    task: str = typer.Option(None, "--task", help="任务 id（默认 $ANVIL_TASK_ID）"),
    json_out: bool = typer.Option(False, "--json"),
):
    """查看 finding 详情。"""
    tid = _finding_task(task)
    _emit(_call("GET", f"/api/v1/tasks/{tid}/findings/{finding_id}"), json_out)


@finding_app.command("complete")
def finding_complete(
    summary: str = typer.Option(..., "--summary", help="完成说明；无问题也必须写明范围/依据"),
    task: str = typer.Option(None, "--task", help="任务 id（默认 $ANVIL_TASK_ID）"),
    json_out: bool = typer.Option(False, "--json"),
):
    """标记问题板完成，任务随后进入 human 最终评审。"""
    tid = _finding_task(task)
    _emit(_call("POST", f"/api/v1/tasks/{tid}/findings/complete",
                {"summary": summary}), json_out)


@finding_app.command("delete")
def finding_delete(
    finding_id: str = typer.Argument(...),
    task: str = typer.Option(None, "--task", help="任务 id（默认 $ANVIL_TASK_ID）"),
):
    """删除 draft/changes_requested finding。"""
    tid = _finding_task(task)
    _call("DELETE", f"/api/v1/tasks/{tid}/findings/{finding_id}")
    typer.echo(f"finding 已删除：{finding_id}")


if __name__ == "__main__":
    app()
