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
        summary="Agent 之间 @ 且路由判定为独立工作时，自动创建派遣子任务。",
        description=(
            "人类 @ 始终按上下文路由执行；显式 context_mode=dispatch 始终可用。"
            "此开关只控制 Agent 之间的自动路由。开启后，Agent 的无关 @ 会在来源 task "
            "下创建非阻塞的派遣子任务；关闭后，退化为当前 task 的新切片/回复 run，"
            "不创建子任务。"
        ),
        category="collaboration",
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
    setattr(config, flag.attr, enabled)
    now = now_iso()
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (_setting_key(flag), "1" if enabled else "0", now))
    conn.execute(
        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (actor, None, "feature.toggle", jdumps({
            "key": key, "value": enabled, "previous": previous,
        }), now))
    conn.commit()
    logger.info(
        "feature toggle actor=%s key=%s previous=%s value=%s",
        actor, key, previous, enabled)
    return _flag_payload(flag, value=enabled, overridden=True)


def reset_flag(conn: sqlite3.Connection, key: str, *,
               actor: str = "human") -> dict:
    flag = _BY_KEY.get(key)
    if flag is None:
        raise LookupError(key)
    default = _BASE_VALUES[flag.attr]
    conn.execute("DELETE FROM settings WHERE key=?", (_setting_key(flag),))
    setattr(config, flag.attr, default)
    conn.execute(
        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (actor, None, "feature.reset", jdumps({"key": key, "value": default}),
         now_iso()))
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
