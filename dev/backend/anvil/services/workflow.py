"""Workflow（工作流）模型与执行引擎。

- **Task** 是人类的高层工作空间；**Workflow** 是 Cindy 生成的步骤状态机，属于一个 Task。
- 步骤不是独立 task 行；跨角色可并行，**同一角色的多个步骤由引擎串行调度**。
- 状态：pending → running → submitted → approved / rejected / failed；gate=1 的步骤
  提交后进入收件箱等待自然人评审（HITL）。
- 依赖满足且角色空闲时，advance() 通过 starter 创建 run；scheduler 每轮推进。

实现按职责拆分（P1.9），本模块保留原有公开 API 作为兼容门面：
- workflow_common：状态查询/锁/活动/提及/委派
- workflow_definition：定义、切片与创建
- workflow_revision：修订预览与应用
- workflow_execution：推进、提交、重试/重启、失败收口
- workflow_review：决策/人工评审与任务最终收口
"""
from __future__ import annotations

# 兼容：调用方/测试仍可能访问 workflow.reviews 等子模块属性。
from .. import config  # noqa: F401
from . import dossier, leases, ledger, reviews, weights  # noqa: F401
from .workflow_common import (  # noqa: F401
    RUN_ACTIVE, STEP_DONE, _steps, get_workflow, get_for_task, list_for_task,
    _assert_current_run, _step_activity, _mention, open_delegations,
    child_task_wait_state, _assert_delegations_done, _close_child_task_rows,
    close_step_delegations, _transitive_downstream, _held_lifecycles,
    workflow_lifecycle_lock, logger
)

from .workflow_definition import (  # noqa: F401
    _as_list, _hint_flag, _role_runtime_id, _runtime_supports_subagents,
    _effective_hint, _expand_slices, normalize_definition, _plan_steps, LOOP_PHASES,
    LOOP_HINT_PHASES, _require_loop_subagent_decisions, _validate_loop, SLICE_ID_RE,
    LOOP_RESERVED_IDS, _merge_unique, SLICE_COLLAB_RULE, _append_collab_rule,
    _slice_context, _loop_spec, _join_instructions, _dynamic_slice_steps,
    _validate_generated_deps, apply_slice_plan, _apply_slice_plan_locked, _phase_role,
    _workflow_roles, _validate_issue_board, _participant_roles, _definition_roles,
    definition_roles,
    preview_participant_sync, sync_workflow_participants, validate_role_coverage,
    validate_definition, terminate_running_workflows, _terminate_running_workflow,
    _clear_restarted_slice_contexts, create_workflow, _create_workflow_locked
)

from .workflow_revision import (  # noqa: F401
    RevisionConflict, _step_is_mutable, _slice_last_step, _step_fields_from_patch,
    _external_revision_context, _revision_context, preview_revision,
    _validate_dep_graph, _validate_runtime_graph, _insert_revision_steps,
    _apply_external_revision, apply_revision, _revision_introduced_roles,
    _sync_revision_participants, _apply_revision_locked
)

from .workflow_execution import (  # noqa: F401
    explain_dispatch, step_prompt, advance, _advance_locked, resume_runs_for_task,
    renew_step_lease_for_run, submit_step, _submit_step_locked, retry_step,
    _retry_step_locked, check_step_run_restart, rebind_run_to_step,
    _retry_backoff_seconds, _end_run_for_step, fail_step
)

from .workflow_review import (  # noqa: F401
    decide_step, _decide_step_locked, escalate_step, _escalate_step_locked,
    review_step, _review_step_locked, gates, task_review_blocker,
    finalize_task_review_if_ready, _finalize_if_done
)

__all__ = [
    'RUN_ACTIVE', 'STEP_DONE', 'get_workflow', 'get_for_task', 'list_for_task',
    'open_delegations', 'child_task_wait_state', 'close_step_delegations',
    'workflow_lifecycle_lock', 'logger', 'normalize_definition', 'LOOP_PHASES',
    'LOOP_HINT_PHASES', 'SLICE_ID_RE', 'LOOP_RESERVED_IDS', 'SLICE_COLLAB_RULE',
    'apply_slice_plan', 'preview_participant_sync', 'sync_workflow_participants',
    'definition_roles', 'validate_role_coverage', 'validate_definition',
    'terminate_running_workflows',
    'create_workflow', 'RevisionConflict', 'preview_revision', 'apply_revision',
    'explain_dispatch', 'step_prompt', 'advance', 'resume_runs_for_task',
    'renew_step_lease_for_run', 'submit_step', 'retry_step', 'check_step_run_restart',
    'rebind_run_to_step', 'fail_step', 'decide_step', 'escalate_step', 'review_step',
    'gates', 'task_review_blocker', 'finalize_task_review_if_ready'
]
