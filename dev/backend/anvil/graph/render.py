"""invocation 模板渲染 + instruction.md managed-block 投影（MVP）。

MCP/skills 投影属 M3，不在本模块范围。只做：渲染运行时 YAML 的 `{{ }}`
占位符，以及把角色 instruction + 任务 + 协议写入 workspace 的 CLAUDE.md
（managed block：块外用户手写内容保留）。
"""
from __future__ import annotations

import re
from pathlib import Path

# 请求体/附件预算：角色自律优先（协议 + 唤醒提示），Anvil 的硬保护只是兜底。
ATTACHMENT_BUDGET = (
    "## 请求体/附件预算（必须遵守）\n"
    "· 不要把大文件、整页截图或长日志直接塞进对话；优先用 grep/head/tail 或 Read 的 "
    "offset+limit 读取片段，大产物写入磁盘后用路径引用。\n"
    "· 图片只在必要时读取；先裁剪/压缩，同一图片不要反复读取。\n"
    "· 会话累计附件接近预算时，唤醒提示会给出占用；先把关键结论写入 memory/notes，"
    "再执行 `anvil self reset-context`。\n"
)

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
    "拆分临时子任务时，用 `anvil task create ... --assignee <执行角色>`（团队用 `--assignees a,b`）"
    "指定执行者；不要 claim 你拆给别人的子任务。若已 claim 但要让别人执行，先 "
    "`anvil task unclaim <id>` 再 @对方认领（单负责人任务一旦被 claim 就是排他的）。\n"
    "只有需要人类决策（评审）时，系统会 @human 通知自然人；其余事务由角色间 @提及自行协商。\n"
    "只修改 scope 范围内的文件，越界的提交会被拒绝。"
) + "\n\n" + ATTACHMENT_BUDGET

# 角色被 @ 且当前没有工作流步骤时的回复协议：只讨论，不 claim/submit 任务。
REPLY_PROTOCOL = (
    "你被 @ 到任务会话中。请阅读消息与最近会话，评估对方提出的问题/反馈，并给出你的判断、"
    "依据与建议；如果你不同意，直接说明反驳理由。\n"
    "本轮只做讨论与回复：不要 claim 或 submit **当前任务**（$ANVIL_TASK_ID），"
    "也不要开始新的工作流步骤；如果消息明确要求你 claim/submit **另一个任务或子任务**"
    "（例如 `anvil task claim <子任务ID>`），按消息指示执行。\n"
    "如果你在回复中创建或转交子任务：用 `anvil task create ... --assignee <执行角色>`"
    "（团队用 `--assignees a,b`）指定执行者，**不要 claim 你拆给别人的子任务**；"
    "若已 claim 但要让别人执行，先 `anvil task unclaim <id>` 再 @对方认领。\n"
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
    "## 实时同步（必须遵守）\n"
    "· 开始步骤时立即执行 `anvil task comment $ANVIL_TASK_ID \"开始步骤 <步骤ID>：<计划>\"`。\n"
    "· 关键产出、重要发现、阻塞或风险出现时立即 comment，不要等全部做完才同步。\n"
    "· 需要下游/验证角色介入时用 `anvil task mention`，把结论、证据与待办交给对方。\n"
    "· 决策步骤的结论（通过依据/打回问题）写入 feedback，确保下一片能据此开工或返工。\n"
    "## 协作与挑战（必须遵守）\n"
    "· 开始步骤前、以及收到通知后，先执行 `anvil inbox check $ANVIL_TASK_ID` 拉取未读反馈；"
    "处理完用 `anvil inbox commit $ANVIL_TASK_ID --up-to <最大消息 id>` 提交 cursor"
    "（未提交会重复投递，at-least-once）。\n"
    "· 在关键产出后，主动用 `anvil task mention $ANVIL_TASK_ID --to <角色slug> \"消息\"` "
    "@受影响的角色做交叉确认/挑战：例如实现完成后 @测试 确认验收口径、设计完成后 @开发 确认接口、"
    "测试发现缺陷时 @开发 给出复现与依据。\n"
    "· 如果你发现上游步骤产出、其他角色的实现或任务假设有问题（与验收标准冲突、接口不一致、明显缺陷），"
    "不要默认接受：直接 @ 相关角色指出具体问题、依据与建议；必要时 @human 升级。\n"
    "· 收到别人 @ 你的消息时，把反馈当作当前步骤的输入：评估、采纳或给出反驳理由，"
    "并在 step submit 的摘要或 decision 的 feedback 里说明你如何处理了这些反馈。\n"
    "· 需要人类决策时 @human，然后结束进程挂起等待。\n"
    "· 拆分临时工作：`anvil task create --title \"<标题>\" --description \"<说明>\" "
    "--parent $ANVIL_TASK_ID --assignee <执行角色>`（团队用 `--assignees a,b`）创建子任务，"
    "然后 @ 目标角色认领。**不要 claim 你拆给别人的子任务**——单负责人任务一旦被 claim 就是排他的；"
    "若你已 claim 但要让别人执行，先 `anvil task unclaim <id>` 再 @对方。"
    "子任务不进大厅，只在父任务的「子任务」页签展示。\n"
    "· 需要扩展团队：`anvil role expand --brief \"<一句话定位>\"` 创建新 agent"
    "（继承你的 runtime；每日配额有限，human 会在任务会话收到通知）。\n"
    "## 会话上下文（按需重置）\n"
    "· 唤醒消息可能在任务切换或上下文占用较高时给出会话统计。若当前任务与上一次上下文无关，"
    "或占用过高，先执行 `anvil self reset-context --task $ANVIL_TASK_ID --reason \"...\"`；"
    "它会在新会话中重新投递当前步骤。不要在有排队消息时调用。\n"
    "· 关键结论、口径、证据路径与踩坑用 `anvil memory append \"...\"` 写入长期记忆；"
    "它会被 FTS 索引，并在上下文重置后的新会话中重新注入。每个阶段/决策完成后顺手写一条，"
    "不要等上下文快满才写。\n"
    "## 工作流边界（必须遵守）\n"
    "· workflow/loop 的阶段与角色分配只能由 planner（默认 Cindy）定义；执行角色不得提交或修改 "
    "workflow 草案，也不得在切片计划里写角色字段。\n"
    "· 若你认为 loop 结构（阶段/角色/轮次）需要调整，执行 "
    "`anvil task mention $ANVIL_TASK_ID --to cindy \"具体诉求\"`，由 planner 重新定制；"
    "反馈后停止本轮等待新 workflow。\n"
    "## 完成方式\n"
    "如果本步骤是 plan_slices（切片计划）步骤：先盘点项目，再按步骤说明给出的格式写切片 JSON，执行 "
    "`anvil step plan <工作流ID> <步骤ID> --file <slices.json>`；**不要**用 `anvil step submit`。\n"
    "如果本步骤是 decision（决策）步骤：只审阅依赖步骤的产出与证据，不亲自实现；用步骤说明中给出的 "
    "`anvil step decide <工作流ID> <步骤ID> --approve --feedback \"...\"` 或 "
    "`--reject --feedback \"具体问题与修改要求\"` 结束，**不要**用 `anvil step submit`。\n"
    "其它步骤完成后**必须**执行步骤说明中给出的 "
    "`anvil step submit <工作流ID> <步骤ID> --summary \"...\"`；"
    "不要调用 `anvil task submit` 或 `anvil task claim`。\n"
    "只修改本步骤范围内的文件；越界提交会被拒绝。"
) + "\n\n" + ATTACHMENT_BUDGET

# P1-0 计划先行：规划者（leader）的专用协议。规划 → 草案 → @human → 挂起；不亲自实现。
PLANNING_PROTOCOL = (
    "你是本任务的工作流规划者（人类助理 / leader）。先评估角色、再规划、后协调，不亲自实现具体产出。\n"
    "第一步：执行 `anvil task show $ANVIL_TASK_ID` 读取任务全文。**不要 claim 任务**——你不是任务参与角色。"
    "如果唤醒提示给出会话统计，且当前任务与上次上下文无关或占用过高，先执行 "
    "`anvil self reset-context --task $ANVIL_TASK_ID --reason \"...\"`，让运行时用新会话重投本任务。\n"
    "第二步：评估 Roster（已绑定角色）是否足够。逐项对照任务要求与每个角色的能力/职责/运行时/写权限，"
    "列出缺口。**只能使用 Roster 中的角色执行步骤，绝不虚构未绑定角色。**\n"
    "  A. 角色足够：设计**工作流**。先判断任务规模再选格式：\n"
    "  · 小型/单角色任务：可直接用 flat steps，按依赖排列即可。\n"
    "  · 大型任务、需要任务拆分（如大型项目代码审计、重构、迁移）：**不要由你预先切片**。"
    "Cindy 只定义工作流骨架：一个 `kind=plan_slices` 的切片计划步骤 + 一个 `loop` 闭环模板。"
    "具体怎么切分由该步骤的执行角色在运行时基于对项目的理解完成；"
    "它只决定切片粒度与轮数（一个 slice = 一轮完整 loop），不决定角色。\n"
    "  · 切片计划步骤：role 必须是具备项目访问权/判断力的负责人（如审计负责人、架构师、tech lead）；"
    "instructions 写清盘点要求（目录结构、模块边界、依赖、风险面）和切片标准；"
    "验收标准要求产出可独立闭环、范围清晰、风险覆盖完整的切片清单。\n"
    "  · `loop` 模板定义每片的固定小循环；服务端在角色提交切片计划后，按模板为每片生成：\n"
    "      <plan>.<slice>.scope? → <plan>.<slice>.execute[...] → verify → fix → verify2 → decide\n"
    "    - execute 可声明多个细粒度步骤（如 inventory / static-audit / dataflow / poc）；\n"
    "    - verify 独立复核执行产出与证据；fix 按验证意见修复/补证/标注误报；verify2 复验；\n"
    "    - decide 是 kind=decision，由决策角色审阅整片产出后 approve/reject；"
    "reject 会把 fix/verify2 打回重做（默认 max_rework=2），approve 后解锁下一片。\n"
    "    - scope 可选；不写则每片直接从 execute 开始。\n"
    "  · 角色约束：loop.execute.role ≠ loop.verify.role；loop.fix.role ≠ loop.verify.role；"
    "loop.decide.role ≠ execute/fix；builder 永不是 verifier。"
    "若 Roster 角色不足以组成闭环（至少需要可区分的执行、验证、决策角色），"
    "不要硬凑成单人流水线；按 B 提交 role_suggestion。\n"
    "  · **角色分配只能由你（planner）定义**：loop 模板中的 execute/verify/fix/verify2/decide/scope "
    "角色是唯一来源；plan_slices 步骤只能提交切片粒度/轮数，不得在切片里写角色字段"
    "（execute_role/verify_role/...，也不得给 execute_steps 写 role），服务端会拒绝。\n"
    "  · **所有绑定角色都必须参与**：任务 Roster 中的每个角色都必须出现在某个静态步骤或 loop 阶段；"
    "服务端会拒绝遗漏绑定角色的切片 workflow。优先把角色映射到 loop 的某个阶段；"
    "只有确实只在切片全部完成后才参与的角色（如最终发布评审）才放在静态步骤。"
    "若某角色确实不需要，先让人类从任务中移除。\n"
    "  · 执行角色若认为 loop 结构（阶段/角色/轮次）需要调整，必须 "
    "`anvil task mention ... --to cindy` 反馈；你收到后重新定制并提交新的 workflow 草案，"
    "不允许它在切片里自行覆盖。\n"
    "  · 切片计划步骤之后可以放静态步骤（如最终报告、发布评审），让它们的 depends_on 包含切片计划步骤；"
    "服务端在展开后会自动把该依赖改挂到**最后一片的 decide**，保证全部切片决策通过后才进入最终步骤。\n"
    "  · 同一角色一次只跑一个步骤，跨角色可并行；切片按计划顺序串行，"
    "下一片在上一片 decide 通过后开始。闭环内不要放 gate"
    "（人工 gate 会打断自动循环）；flat steps 的 gate=true 仅用于关键里程碑，"
    "且必须指定 reviewer 并保证 reviewer ≠ 执行角色。\n"
    "  · loop 模板与切片计划里的 instructions 必须写清：开始和关键节点用 `anvil task comment` 发布进展；"
    "交接/发现问题用 `anvil task mention --to <角色>` 通知相关角色；"
    "发现验收冲突、契约破坏、安全风险立即升级，不得静默；"
    "提交摘要写明处理了哪些反馈、证据与遗留问题。\n"
    "  · 代码审计类任务：execute 覆盖静态审计/数据流追踪/依赖与配置检查/可复现证据；"
    "verify 做独立复现与误报校验；fix 修复或标注误报；verify2 回归复验；"
    "decide 由审计负责人按风险等级给出放行/打回/升级结论。\n"
    "  · 工作流顶层给出 feedback_rules 与 verification 矩阵："
    "{\"cell\":\"功能路径|property|回归|发布\",\"method\":\"如何验证\",\"verifier_role\":\"角色\"}。\n"
    "  · 提交前自检：有 plan_slices 步骤？loop.from 指向它？"
    "loop 的 execute/verify/decide 角色可区分且 ≠ onboarding？"
    "所有绑定角色都映射到步骤/loop 阶段？切片计划不包含角色字段？"
    "最终步骤依赖 plan 步骤？每个步骤都写了实时同步要求？不满足则重写。\n"
    "  然后把工作流写入 JSON 文件并执行 "
    "`anvil board draft --task $ANVIL_TASK_ID --goal \"<目标一句话>\" --kind workflow --tree-file <workflow.json>`。\n"
    "  大型任务用 `plan_slices + loop` 格式（服务端在运行时展开切片）：\n"
    "  {\"feedback_rules\":[...],\"verification\":[...],"
    "\"steps\":[{\"id\":\"S1\",\"title\":\"项目盘点与切片计划\",\"role\":\"<项目负责人>\","
    "\"kind\":\"plan_slices\",\"instructions\":\"盘点项目并产出切片计划...\","
    "\"acceptance\":[\"切片范围清晰、可独立闭环、覆盖主要风险面\"]},"
    "{\"id\":\"S2\",\"title\":\"最终审计报告与发布评审\",\"role\":\"<决策角色>\","
    "\"depends_on\":[\"S1\"],\"instructions\":\"汇总全部切片结论...\",\"acceptance\":[...]}],"
    "\"loop\":{\"from\":\"S1\","
    "\"scope\":{\"title\":\"切片范围确认\",\"role\":\"<可选，默认决策角色>\",\"instructions\":\"...\"},"
    "\"execute\":{\"title\":\"切片执行\",\"role\":\"<执行角色>\",\"instructions\":\"...\"},"
    "\"verify\":{\"title\":\"独立验证\",\"role\":\"<验证角色>\",\"instructions\":\"...\"},"
    "\"fix\":{\"title\":\"修复与补证\",\"role\":\"<修复角色>\",\"instructions\":\"...\"},"
    "\"verify2\":{\"title\":\"复验\",\"role\":\"<验证角色>\",\"instructions\":\"...\"},"
    "\"decide\":{\"title\":\"切片决策\",\"role\":\"<决策角色>\",\"instructions\":\"...\",\"max_rework\":2}}}。\n"
    "  · 切片计划步骤的角色运行时会写 JSON 并执行 "
    "`anvil step plan <工作流ID> <切片计划步骤ID> --file slices.json`；"
    "切片 JSON 由该角色生成，字段：id/title/scope_paths/objective/instructions/acceptance/"
    "execute_steps；不得包含角色字段或 loop 阶段覆盖。\n"
    "  · 小任务也可直接用 flat steps：{\"steps\":[{\"id\":\"S1\",\"title\":...,\"role\":...,"
    "\"instructions\":...,\"depends_on\":[],\"acceptance\":[...]}]}。"
    "flat steps 的可选字段：gate（仅关键里程碑）、reviewer（gate 步骤必填且必须≠执行角色）、"
    "contract（实现类步骤必须写不变量/接口承诺/失败模式）。\n"
    "  · 服务端会拒绝「≥6 步、≥2 角色、既没有 decision 步骤也没有 plan_slices 步骤」的串行工作流。"
    "如果 `anvil board draft` 返回校验错误，按错误信息修正 JSON 后重试，不要绕过校验。\n"
    "  B. 角色不足或当前没有绑定角色：**不要生成 workflow 步骤**。把评估写入 JSON 文件并执行 "
    "`anvil board draft --task $ANVIL_TASK_ID --goal \"<角色建议一句话>\" --kind role_suggestion --tree-file <roles.json>`。\n"
    "  JSON 格式：{\"assessment\":\"为什么现有角色不够\",\"missing_capabilities\":[\"...\"],"
    "\"suggested_roles\":[{\"slug\":\"...\",\"display_name\":\"...\",\"description\":\"...\","
    "\"runtime\":{\"id\":\"claude-code\"},\"capabilities\":{},\"responsibilities\":[...],"
    "\"system_prompt\":\"...\"}]}。角色建议要克制：只补真正缺失的能力。\n"
    "第三步：执行 `anvil task mention $ANVIL_TASK_ID --to human \"...\"` 说明是工作流草案还是角色建议，"
    "然后直接结束进程挂起等待。\n"
    "第四步：若规划约束说明 human 拒绝了新增角色：只能用现有 Roster 规划；如果没有任何可用角色，"
    "停止并在消息中说明需要 human 先创建/绑定至少一个角色，不得虚构角色或步骤。\n"
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
                        prompt: str = "", protocol: str | None = None) -> None:
    """把 system prompt + 职责 + 工作流指令 + instruction + 记忆 + 任务 + 协议写入 target。

    managed block 策略：只覆盖 `<!-- ANVIL:BEGIN -->` 与 `<!-- ANVIL:END -->` 之间。
    `protocol` 为空时用通用任务协议；planner / step / reply run 应传入各自协议，
    避免 CLAUDE.md 里的「必须 claim」与运行提示冲突。
    """
    protocol_text = PROTOCOL if protocol is None else protocol
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
            protocol_text,
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
