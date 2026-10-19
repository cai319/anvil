"""invocation 模板渲染 + instruction.md managed-block 投影（MVP）。

MCP/skills 投影属 M3，不在本模块范围。只做：渲染运行时 YAML 的 `{{ }}`
占位符，以及把角色 instruction + 任务 + 协议写入 workspace 的 CLAUDE.md
（managed block：块外用户手写内容保留）。
"""
from __future__ import annotations

import re
from pathlib import Path

# §7.4 注入给 Agent 的协议说明（约 400 token）。
PROTOCOL = (
    "你在 Anvil 环境中运行。当前环境变量 ANVIL_TASK_ID 指示你被分配的任务。\n"
    "第一步必须执行 `anvil task claim $ANVIL_TASK_ID` 认领任务（否则无法提交）。\n"
    "再执行 `anvil task show $ANVIL_TASK_ID` 读取完整要求。\n"
    "在当前工作目录（cwd）中完成你的产出（写代码/文档/测试）。\n"
    "开始工作后、完成关键阶段后，执行 `anvil task comment $ANVIL_TASK_ID \"进展说明\"` 在任务会话里同步你的思考与进展。\n"
    "需要与其它角色协作或澄清时，执行 `anvil task mention $ANVIL_TASK_ID --to <角色slug> \"消息\"` @提及对方（角色间直接交互，不经过人类）。\n"
    "如果你 @提及了其它角色等待响应，就**不要 submit**，直接结束进程挂起等待；系统会在对方 @你之后再次唤醒你，你带着最新会话内容继续；只有任务真正完成时才执行 submit。\n"
    "完成后必须执行 `anvil task submit $ANVIL_TASK_ID \"完成摘要\"` 声明完成，仅仅退出进程不算完成。\n"
    "长任务中每隔若干分钟执行 `anvil task heartbeat` 续租，否则任务会被回收。\n"
    "发现自己不适合这个任务时执行 `anvil task unclaim` 并说明理由，这是正常操作。\n"
    "只有需要人类决策（评审）时，系统会 @human 通知自然人；其余事务由角色间 @提及自行协商。\n"
    "只修改 scope 范围内的文件，越界的提交会被拒绝。"
)

# 角色被 @ 且当前没有工作流步骤时的回复协议：只讨论，不 claim/submit 任务。
REPLY_PROTOCOL = (
    "你被 @ 到任务会话中。请阅读消息与最近会话，评估对方提出的问题/反馈，并给出你的判断、"
    "依据与建议；如果你不同意，直接说明反驳理由。\n"
    "本轮只做讨论与回复：不要 claim 或 submit 任务，也不要开始新的工作流步骤。\n"
    "但若人类明确要求你执行操作（如启动程序、运行验证命令、准备演示环境），可以在当前工作目录"
    "执行并把结果回复给人类。\n"
    "如果对方需要根据你的结论继续工作，**必须**用 "
    "`anvil task mention $ANVIL_TASK_ID --to <对方角色slug> \"结论\"` 回复（这会自动唤醒对方）；"
    "只有在纯旁注、不需要对方行动时才用 `anvil task comment`。\n"
    "回复后结束进程；如果需要对方处理后给你回执，你会在被 @ 时再次唤醒。"
)

# 工作流步骤协议：角色执行一个 workflow step（不是整个 task）。
STEP_PROTOCOL = (
    "你正在执行一个任务工作流中的「步骤」，不是整个任务。步骤说明里给出了工作流 ID、步骤 ID、"
    "你的角色、依赖、验收标准与具体指令。\n"
    "先执行 `anvil task show $ANVIL_TASK_ID` 了解任务背景，然后按步骤说明完成产出。\n"
    "## 协作与挑战（必须遵守）\n"
    "· 在关键产出后，主动用 `anvil task mention $ANVIL_TASK_ID --to <角色slug> \"消息\"` "
    "@受影响的角色做交叉确认/挑战：例如实现完成后 @测试 确认验收口径、设计完成后 @开发 确认接口、"
    "测试发现缺陷时 @开发 给出复现与依据。\n"
    "· 如果你发现上游步骤产出、其他角色的实现或任务假设有问题（与验收标准冲突、接口不一致、明显缺陷），"
    "不要默认接受：直接 @ 相关角色指出具体问题、依据与建议；必要时 @human 升级。\n"
    "· 收到别人 @ 你的消息时，把反馈当作当前步骤的输入：评估、采纳或给出反驳理由，"
    "并在 `anvil step submit` 的摘要里说明你如何处理了这些反馈。\n"
    "· 需要人类决策时 @human，然后结束进程挂起等待。\n"
    "## 完成方式\n"
    "步骤完成后**必须**执行步骤说明中给出的 `anvil step submit <工作流ID> <步骤ID> --summary \"...\"`；"
    "不要调用 `anvil task submit` 或 `anvil task claim`。\n"
    "只修改本步骤范围内的文件；越界提交会被拒绝。"
)

# P1-0 计划先行：规划者（leader）的专用协议。规划 → 草案 → @human → 挂起；不亲自实现。
PLANNING_PROTOCOL = (
    "你是本任务的工作流规划者（人类助理 / leader）。先规划、后协调，不亲自实现具体产出。\n"
    "第一步：执行 `anvil task claim $ANVIL_TASK_ID` 认领，`anvil task show $ANVIL_TASK_ID` 读取任务全文。\n"
    "第二步：分析任务，设计一个**工作流（workflow）**——由若干步骤组成的状态机，明确「谁在什么时候做什么」：\n"
    "  · 每个步骤：id、title、role（Roster 中的参与角色）、instructions（该角色具体要做什么、产出什么）、"
    "depends_on（步骤 id 列表，表达顺序与并行）、acceptance（验收标准）、gate（是否人工审核门）。\n"
    "  · 跨角色可并行（如开发实现时测试写用例），但同一角色的多个步骤要按依赖顺序排列（引擎会串行调度）。\n"
    "  · gate=true 用于关键里程碑：需求/设计基线、最终验收。其余步骤自动推进。\n"
    "  · 每个步骤的 instructions 要写明需要与哪些角色交叉确认/挑战（如设计完成后 @开发 确认接口、"
    "实现完成后 @测试 确认验收），以及交付前必须回应的反馈点；gate 步骤应要求提供交叉评审证据。\n"
    "  · 树级给出 feedback_rules（出问题 @谁、何时升级 human）。\n"
    "第三步：把工作流写入 JSON 文件并执行 "
    "`anvil board draft --task $ANVIL_TASK_ID --goal \"<目标一句话>\" --kind workflow --tree-file <workflow.json>` 提交。"
    "JSON 格式：{\"feedback_rules\":[...], \"steps\":[{\"id\":\"S1\",\"title\":...,\"role\":...,"
    "\"instructions\":...,\"depends_on\":[],\"acceptance\":[...],\"gate\":true}]}。\n"
    "第四步：执行 `anvil task mention $ANVIL_TASK_ID --to human \"工作流草案已生成，请审阅批准\"`，"
    "然后直接结束进程挂起等待。人工批准后执行引擎会按依赖自动推进步骤。\n"
    "禁止：不要实现具体步骤；不要 submit 主任务；草案被驳回时按人工意见修订后重新提交。"
)

_BEGIN = "<!-- ANVIL:BEGIN -->"
_END = "<!-- ANVIL:END -->"
_MANAGED = re.compile(re.escape(_BEGIN) + r".*?" + re.escape(_END), re.DOTALL)

# 角色工作流（role definition 的 workflow 字段）：规划确认 vs 直接实施。
WORKFLOW_INSTRUCTION = {
    "plan_confirm_execute": (
        "你负责规划/设计：先产出方案/设计文档，然后执行 "
        "`anvil task mention $ANVIL_TASK_ID --to human \"方案已产出，请确认\"` @human 请确认；"
        "收到 human 确认后再继续实施。\n"
    ),
    "direct_execute": "",
}


def workflow_instruction(workflow: str) -> str:
    return WORKFLOW_INSTRUCTION.get(workflow, "")


def render_template(template: str, ctx: dict[str, str]) -> str:
    """把 `{{ key }}` 替换为 ctx 值；未命中的键保留原样。"""
    return re.sub(
        r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}",
        lambda m: str(ctx.get(m.group(1), m.group(0))),
        template,
    )


def project_instruction(target: Path, instruction_md: str, task: dict,
                        system_prompt: str = "", responsibilities: list[str] | None = None,
                        workflow: str = "direct_execute", memory: str = "",
                        notes: list[str] | None = None, lessons: list[str] | None = None,
                        prompt: str = "") -> None:
    """把 system prompt + 职责 + 工作流指令 + instruction + 记忆 + 任务 + 协议写入 target。

    managed block 策略：只覆盖 `<!-- ANVIL:BEGIN -->` 与 `<!-- ANVIL:END -->` 之间。
    """
    responsibilities = responsibilities or []
    notes = notes or []
    lessons = lessons or []
    duties = "\n".join(f"- {r}" for r in responsibilities)
    wf = workflow_instruction(workflow)
    task_title = task.get("title") or ("定时提醒" if prompt else "")
    task_body = task.get("description") or prompt
    inner = "\n".join(
        p for p in (
            system_prompt.strip() if system_prompt else "",
            instruction_md.strip(),
            f"## 职责\n{duties}" if duties else "",
            wf.strip() if wf else "",
            f"## 记忆\n{memory.strip()}" if memory.strip() else "",
            "## 相关笔记\n" + "\n".join(f"- {n.strip()}" for n in notes) if notes else "",
            "## 共享经验\n" + "\n".join(f"- {l.strip()}" for l in lessons) if lessons else "",
            f"## 任务：{task_title}",
            task_body or "",
            "## 协议",
            PROTOCOL,
        ) if p
    )
    block = f"{_BEGIN}\n{inner}\n{_END}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        existing = target.read_text(encoding="utf-8")
        replaced = _MANAGED.sub(lambda _: block, existing) if _BEGIN in existing else existing.rstrip() + "\n\n" + block + "\n"
    else:
        replaced = block + "\n"
    target.write_text(replaced, encoding="utf-8")
