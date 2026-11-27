"""运行时功能开关：默认值来自 config/env，用户覆盖持久化到 settings 表。

只允许修改这里登记的开关；其它配置项（端口、路径、模型窗口等）仍需环境变量
并重启。覆盖值在每次 create_app 初始化时重新应用到 config，因此重启后保留。
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from .. import config
from ..util import jdumps, now_iso
from . import ledger

_SETTING_PREFIX = "feature."
logger = logging.getLogger("anvil.features")


@dataclass(frozen=True)
class FeatureFlag:
    key: str
    attr: str
    title: str
    summary: str
    description: str
    category: str


FLAGS: tuple[FeatureFlag, ...] = (
    FeatureFlag(
        key="experience_capture",
        attr="EXPERIENCE_CAPTURE_ENABLED",
        title="自动经验捕获",
        summary="任务完成、失败、返工或评审驳回时，自动生成待审批知识草稿。",
        description=(
            "开启后：任务完成、步骤实质失败、返工或评审驳回时，自动生成 proposed 知识草稿。"
            "任务已绑定主题知识库时写入该主题库，否则写入任务级知识库的 drafts/。"
            "捕获不调用 LLM、不读取角色私有 notes，也不会自动批准。"
            "关闭后：不会自动生成草稿，只能由角色/人类显式写入或手动晋升。"
        ),
        category="knowledge",
    ),
    FeatureFlag(
        key="knowledge_harvest_reminder",
        attr="KNOWLEDGE_HARVEST_REMINDER_ENABLED",
        title="任务完成知识收割提醒",
        summary="任务 done 后自动提醒 Cindy 收割任务/主题/频道知识。",
        description=(
            "开启后：Cindy 规划完成且 human 落板 workflow 时自动生成一条 when(task done) 提醒，"
            "任务完成时触发 Cindy 用任务上下文整理任务知识、主题知识与频道知识；Cindy 只写 "
            "proposed/待审草稿，不自动批准，人工/任务参与者批准后生效。取消/删除任务会移除"
            "未触发提醒，重新打开已落板任务会补回。关闭后：不再自动生成提醒，已有提醒保留；"
            "仍可手工创建 when 提醒。"
        ),
        category="knowledge",
    ),
    FeatureFlag(
        key="knowledge_llm_cleaning",
        attr="KNOWLEDGE_LLM_CLEANING_ENABLED",
        title="LLM 知识清洗",
        summary="人工按需把 proposed 知识卡整理成“场景/效果/证据”结构。",
        description=(
            "开启后：可在主题知识库审批页对单张 proposed 卡执行“LLM 清洗预览”，"
            "查看字段/正文 diff 和引用后再决定是否应用；也可用 CLI 小批量 dry-run。"
            "清洗只写回 proposed，不自动批准，不编造证据，失败时保留原卡。"
            "关闭后：隐藏清洗入口，API 返回 409；质量门仍生效。"
        ),
        category="knowledge",
    ),
    FeatureFlag(
        key="knowledge_repos",
        attr="KNOWLEDGE_REPOS_ENABLED",
        title="主题知识库自动绑定",
        summary="建任务时自动识别并绑定主题知识库；低相关时创建任务级知识库。",
        description=(
            "开启后：创建任务时按标题、描述和标签自动识别并绑定最多 3 个主题知识库；"
            "相关性低于阈值且有主题信号时，在任务工作空间内创建任务级知识库。"
            "关闭后：不自动绑定或创建，已有绑定保留，新任务回退到 shared/legacy 检索范围。"
        ),
        category="knowledge",
    ),
    FeatureFlag(
        key="task_knowledge",
        attr="TASK_KNOWLEDGE_ENABLED",
        title="任务知识注入",
        summary="把当前任务的任务共享知识注入角色上下文。",
        description=(
            "开启后：当前任务的任务共享知识会进入 CIC / one-shot 上下文，任务参与角色可读写。"
            "关闭后：已有任务知识保留，但不再注入新上下文；写入与审批数据不受影响。"
        ),
        category="knowledge",
    ),
    FeatureFlag(
        key="channel_knowledge",
        attr="CHANNEL_KNOWLEDGE_ENABLED",
        title="频道知识注入",
        summary="让绑定频道的任务在上下文与规划阶段读取频道知识。",
        description=(
            "开启后：绑定频道的任务在 CIC 和规划阶段会读取该频道已批准的频道知识。"
            "关闭后：频道知识仍可创建、审批和存储，但不会进入任务上下文。"
        ),
        category="knowledge",
    ),
    FeatureFlag(
        key="context_manifest",
        attr="CONTEXT_MANIFEST_ENABLED",
        title="上下文来源清单",
        summary="记录每次上下文注入了哪些知识、来源和字符数。",
        description=(
            "开启后：每次 CIC 和运行时投影都会记录上下文块（层级、来源 ID、字符数、"
            "是否注入、丢弃原因），用于回答“为什么这条知识进入了上下文”以及审计跨任务污染。"
            "关闭后：不写清单，少量减少数据库写入，但失去上下文可观测性。"
        ),
        category="context",
    ),
    FeatureFlag(
        key="deferred_projection",
        attr="DEFERRED_PROJECTION",
        title="延迟投影",
        summary="持久会话在实际投递前才写 CLAUDE.md，避免任务间互相覆盖。",
        description=(
            "开启后：持久会话在实际投递前才写 CLAUDE.md / .claude，避免任务 B 的投影"
            "覆盖正在执行的任务 A。关闭后：回到 run 创建时投影，任务并行时可能出现跨任务"
            "文件覆盖，不建议关闭。"
        ),
        category="context",
    ),
    FeatureFlag(
        key="task_workspace",
        attr="TASK_WORKSPACE_ENABLED",
        title="任务独立工作空间",
        summary="每个任务使用独立目录，隔离代码、产物和任务级知识草稿。",
        description=(
            "开启后：每个任务使用独立 data/task-workspaces/<task-id>/，代码、产物和任务级"
            "知识草稿互不串扰。关闭后：回退角色 home，多个任务共享目录和投影文件。"
            "仅影响后续新任务/新 run，已有工作空间不会删除。"
        ),
        category="workspace",
    ),
    FeatureFlag(
        key="task_workspace_cleanup",
        attr="TASK_WORKSPACE_CLEANUP_ENABLED",
        title="工作空间自动清理",
        summary="按保留天数清理已归档任务的工作空间，活跃任务永不清理。",
        description=(
            "开启后：调度器按 ANVIL_TASK_WORKSPACE_RETENTION_DAYS（默认 30 天）清理"
            "已归档任务的工作空间；active 任务永不清理。关闭后：不自动删除，磁盘占用会"
            "持续增长，需要人工清理。"
        ),
        category="workspace",
    ),
    FeatureFlag(
        key="dispatch_auto_from_mention",
        attr="DISPATCH_AUTO_FROM_MENTION",
        title="Agent 间 @ 自动派遣",
        summary="Agent 间 @ 判定为独立工作时自动创建派遣子任务；关闭则退回当前 task 回复 run。",
        description=(
            "人类 @ 和显式 context_mode=dispatch 始终可派遣；此开关只控制 Agent 之间的"
            "自动升级。开启后：Agent 对无关请求的 @ 会创建不阻塞当前 workflow 的派遣"
            "子任务（独立 task 上下文，完成后可自动回切）。关闭后：不创建 child task，"
            "也不新建 slice；改为在当前 task 的已有 slice 或任务级上下文创建/复用一个"
            " @ 回复 run。关闭不停止执行，只是不独立建卡；适合先观察 router 判断，再"
            "决定是否开启自动派遣。派遣/委派都是子任务：不修改 workflow、不需要 Cindy "
            "介入；派遣提交即完成（无人工评审），委派由派发角色验收并释放父步骤；"
            "父 workflow 的最终人工审批不受影响。"
        ),
        category="collaboration",
    ),
    FeatureFlag(
        key="role_rsi",
        attr="ROLE_RSI_ENABLED",
        title="角色 RSI（总开关）",
        summary="角色 RSI 总开关：从任务中复盘、积累经验并产生演进候选。",
        description=(
            "开启后：任务完成写回执；满足条件时在角色空闲窗口反思，产出经验卡、候选和"
            "运行时提案；所有晋级仍需人工。关闭后：不产生观测/候选，现有行为不变。"
            "建议先按角色 definition.rsi.enabled 试点。"
        ),
        category="evolution",
    ),
    FeatureFlag(
        key="role_rsi_autonomous_knowledge",
        attr="ROLE_RSI_AUTONOMOUS_KNOWLEDGE",
        title="RSI 自主知识卡",
        summary="允许 RSI 把成功任务的经验直接写成角色私有经验卡。",
        description=(
            "开启后：成功任务提炼的经验卡可自主写入，仍受质量、去重和实践门控制；"
            "失败/返工只写 draft。关闭后：所有知识卡都要人工批准。"
            "不改系统提示、工具或权限。"
        ),
        category="evolution",
    ),
    FeatureFlag(
        key="role_rsi_reflection",
        attr="ROLE_RSI_REFLECTION",
        title="RSI 反思 run",
        summary="让角色在空闲窗口复盘近期任务，生成候选与经验。",
        description=(
            "开启后：满足触发条件时创建一次性反思 run，消耗角色 runtime token；"
            "任务唤醒会抢占。关闭后：只积累 observation，不调用模型。"
        ),
        category="evolution",
    ),
    FeatureFlag(
        key="role_rsi_evaluation",
        attr="ROLE_RSI_EVALUATION",
        title="RSI 隐藏回放评估",
        summary="像一次离线考试：用真实历史任务对比候选与当前角色。",
        description=(
            "开启后：候选和当前版本在同一批真实任务上各跑一遍，比较通过率和成本；"
            "需要至少 8+8 条带确定性验收的任务，数据不足只会返回 inconclusive。"
            "默认关闭：回放耗 token、耗时间，且角色看不到评测集。"
            "当前真正控制回放执行的是本开关。"
        ),
        category="evolution",
    ),
    FeatureFlag(
        key="role_rsi_auto_rollback",
        attr="ROLE_RSI_AUTO_ROLLBACK",
        title="RSI SOAKING 自动回滚",
        summary="晋级后观察窗内表现变差时，自动回到上一批准版本。",
        description=(
            "开启后：新版本晋级后持续看实际成功率和返工，连续退化超过噪声阈值就切回"
            "上一个曾人工批准的版本，并写审计。只回到人工批准过的状态，不会自动晋级；"
            "关闭后只告警，由人工回滚。"
        ),
        category="evolution",
    ),
    FeatureFlag(
        key="role_rsi_rubric_judge",
        attr="ROLE_RSI_RUBRIC_JUDGE",
        title="RSI rubric judge",
        summary="没有确定性验收时，用固定评分器给出诊断。",
        description=(
            "开启后：可用冻结 rubric judge 产出诊断，但需要人工校准（κ≥0.6）和抽检；"
            "结论仍封顶 inconclusive，不能单独作为晋级依据。默认关闭；"
            "优先使用确定性 verifier。"
        ),
        category="evolution",
    ),
    FeatureFlag(
        key="role_rsi_lock_strategy",
        attr="ROLE_RSI_LOCK_STRATEGY",
        title="锁定 Strategy 直写",
        summary="agent 不能直接改 instruction/principles，只能提候选。",
        description=(
            "开启后：agent 直写 instruction 会被拒绝；追加准则会转成待审批候选；"
            "human 直写仍可用。正式启用 RSI 的角色建议开启，避免绕过反思与评估。"
        ),
        category="evolution",
    ),
    FeatureFlag(
        key="role_rsi_replay_l2",
        attr="ROLE_RSI_REPLAY_L2",
        title="RSI 任务回放 L2",
        summary="预留开关：在隔离环境重跑历史任务。",
        description=(
            "设计上用于让候选在临时角色 home/workspace 中执行真实任务；"
            "当前版本的 ReplayRunner 由“RSI 隐藏回放评估”控制，本开关尚未单独生效，"
            "保留给后续拆分 L1/L2 门控。"
        ),
        category="evolution",
    ),
    FeatureFlag(
        key="runtime_proposals",
        attr="RUNTIME_PROPOSALS_ENABLED",
        title="运行时改进提案",
        summary="把角色遇到的运行时问题整理成给人类执行的修复提案。",
        description=(
            "角色遇到 skill 加载失败、MCP 连不上、runtime 配置/超时等自己不能改的问题时，"
            "生成带证据和修复建议的 Markdown 提案。默认开启：只生成提案，不会自动应用；"
            "角色只能建 draft/追加证据，流转和导出由 human 完成。"
        ),
        category="evolution",
    ),
    FeatureFlag(
        key="runtime_proposal_detector",
        attr="RUNTIME_PROPOSAL_DETECTOR",
        title="运行时提案自动检测",
        summary="系统巡检员：扫描运行时健康和失败 run，自动创建 draft。",
        description=(
            "定期检查 runtime 是否 missing/unhealthy，以及超时/启动失败/上下文溢出的 run，"
            "自动创建 draft 并附证据。不会改运行时、审批或推进生命周期。"
            "默认关闭：运行时不稳定时可能一次生成很多 draft；当前只覆盖这几类信号，"
            "其他问题仍靠角色/人工提案。"
        ),
        category="evolution",
    ),
    FeatureFlag(
        key="cindy_clarification",
        attr="CINDY_CLARIFICATION_ENABLED",
        title="Cindy 规划前澄清",
        summary="Cindy 首次规划任务前先澄清意图、确认任务简报与流程强度。",
        description=(
            "开启后：Cindy 在首次规划阶段先进入澄清对话（每轮 1-3 个带推荐答案的问题），"
            "产出结构化任务简报；human 在任务会话的简报卡确认或跳过后，才允许创建 workflow "
            "草案并启动 planner。流程强度在澄清阶段选择，未确认简报时服务端拒绝 workflow 草案。"
            "只影响 Cindy 的首次规划阶段；已有 workflow、已执行、非 todo 任务完全不受影响。"
            "关闭后：Cindy 回到直接规划，已有澄清状态不影响已启动的 run。"
        ),
        category="workflow",
    ),
    FeatureFlag(
        key="sandbox",
        attr="SANDBOX_ENABLED",
        title="执行沙箱",
        summary="角色执行与 PoC 运行使用 OS 级沙箱边界。",
        description=(
            "开启后：角色进程经平台沙箱启动（macOS Seatbelt / Windows 命名 Job），"
            "工作区写范围、资源上限与 Anvil 秘密读拒绝生效；严格档缺失时阻止 PoC/动态验证。"
            "关闭后：退回宿主直跑（等同 host-unsafe），安全边界消失；仅用于排障。"
            "环境变量 ANVIL_SANDBOX=host-unsafe 优先于本开关。"
        ),
        category="security",
    ),
)

_BY_KEY = {flag.key: flag for flag in FLAGS}
# 进程启动时的默认值（来自环境变量/config）；用户覆盖在 DB settings 中。
_BASE_VALUES = {flag.attr: bool(getattr(config, flag.attr)) for flag in FLAGS}


def _bool(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _setting_key(flag: FeatureFlag) -> str:
    return f"{_SETTING_PREFIX}{flag.key}"


def _stored_values(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE key LIKE ?",
        (f"{_SETTING_PREFIX}%",)).fetchall()
    return {str(row["key"])[len(_SETTING_PREFIX):]: str(row["value"]) for row in rows}


def _flag_payload(flag: FeatureFlag, *, value: bool, overridden: bool) -> dict:
    return {
        "key": flag.key,
        "title": flag.title,
        "summary": flag.summary,
        "description": flag.description,
        "category": flag.category,
        "config_attr": flag.attr,
        "value": value,
        "default": _BASE_VALUES[flag.attr],
        "overridden": overridden,
    }


def list_flags(conn: sqlite3.Connection) -> list[dict]:
    stored = _stored_values(conn)
    result = []
    for flag in FLAGS:
        overridden = flag.key in stored
        value = _bool(stored[flag.key]) if overridden else bool(getattr(config, flag.attr))
        result.append(_flag_payload(flag, value=value, overridden=overridden))
    return result


def set_flag(conn: sqlite3.Connection, key: str, value: object, *,
             actor: str = "human") -> dict:
    flag = _BY_KEY.get(key)
    if flag is None:
        raise LookupError(key)
    enabled = _bool(value)
    previous = bool(getattr(config, flag.attr))
    default = _BASE_VALUES[flag.attr]
    setattr(config, flag.attr, enabled)
    now = now_iso()
    # 切换回默认值等价于恢复默认：删除持久化覆盖，避免“值已回默认但
    # overridden 仍为 true”的隐性状态。
    restored_default = enabled == default
    if restored_default:
        conn.execute("DELETE FROM settings WHERE key=?", (_setting_key(flag),))
    else:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (_setting_key(flag), "1" if enabled else "0", now))
    ledger.emit_audit(
        conn, role_slug=actor,
        task_id=None,
        action="feature.toggle",
        payload=jdumps({
            "key": key, "value": enabled, "previous": previous,
            "restored_default": restored_default,
        }),
        now=now)
    conn.commit()
    logger.info(
        "feature toggle actor=%s key=%s previous=%s value=%s restored_default=%s",
        actor, key, previous, enabled, restored_default)
    return _flag_payload(flag, value=enabled, overridden=not restored_default)


def reset_flag(conn: sqlite3.Connection, key: str, *,
               actor: str = "human") -> dict:
    flag = _BY_KEY.get(key)
    if flag is None:
        raise LookupError(key)
    default = _BASE_VALUES[flag.attr]
    conn.execute("DELETE FROM settings WHERE key=?", (_setting_key(flag),))
    setattr(config, flag.attr, default)
    ledger.emit_audit(
        conn, role_slug=actor,
        task_id=None,
        action="feature.reset",
        payload=jdumps({"key": key, "value": default}),
        now=now_iso())
    conn.commit()
    logger.info("feature reset actor=%s key=%s value=%s", actor, key, default)
    return _flag_payload(flag, value=default, overridden=False)


def apply_overrides(conn: sqlite3.Connection) -> None:
    """启动时把持久化覆盖应用到 config；没有覆盖的开关恢复 env 默认值。"""
    stored = _stored_values(conn)
    for flag in FLAGS:
        if flag.key in stored:
            setattr(config, flag.attr, _bool(stored[flag.key]))
        else:
            setattr(config, flag.attr, _BASE_VALUES[flag.attr])


def reset_runtime() -> None:
    """测试/维护用：把当前进程的开关恢复到启动默认值（不写 DB）。"""
    for flag in FLAGS:
        setattr(config, flag.attr, _BASE_VALUES[flag.attr])
