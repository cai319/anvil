"""Pydantic schemas (SE v1.0 §3 / design §10)."""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TaskStatus(str, Enum):
    todo = "todo"
    in_progress = "in_progress"
    in_review = "in_review"
    done = "done"
    closed = "closed"


class RunStatus(str, Enum):
    created = "created"
    projecting = "projecting"
    running = "running"
    submitted = "submitted"
    reviewing = "reviewing"
    suspended = "suspended"
    completed = "completed"
    failed = "failed"
    abandoned = "abandoned"


class RuntimeStatus(str, Enum):
    ready = "ready"
    unverified = "unverified"
    unhealthy = "unhealthy"
    missing = "missing"


# ---- Runtimes ------------------------------------------------------------


class RuntimeIn(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{1,63}$")
    display_name: str
    executable_path: str | None = None
    executable: str | None = None  # §2.6 手工注册用「executable」，扁平注册用「executable_path」
    detected_version: str | None = None
    status: RuntimeStatus = RuntimeStatus.unverified
    capabilities: dict[str, Any] = Field(default_factory=dict)
    # §2.6 手工定制扩展字段（可选）
    invocation: dict[str, Any] | None = None
    projection: dict[str, Any] | None = None
    discovery: dict[str, Any] | None = None
    version_range: str | None = None
    version_probe: dict[str, Any] | None = None
    health: dict[str, Any] | None = None


class RuntimeOut(BaseModel):
    id: str
    display_name: str
    executable_path: str
    detected_version: str | None = None
    status: RuntimeStatus
    capabilities: dict[str, Any] = Field(default_factory=dict)
    definition_hash: str | None = None
    last_probe_at: str | None = None
    last_probe_log: str | None = None
    enabled: bool = True


# ---- Roles ---------------------------------------------------------------


class RoleIn(BaseModel):
    slug: str
    display_name: str
    avatar_color: str | None = None
    avatar: str | None = None
    kind: str | None = None
    description: str | None = None
    capabilities: dict[str, Any] | None = None
    working_mode: str | None = None
    skill_description: str | None = None
    runtime: dict[str, Any] = Field(default_factory=dict)
    workspace: dict[str, Any] = Field(default_factory=dict)
    tools: dict[str, Any] = Field(default_factory=dict)
    concurrency: dict[str, Any] = Field(default_factory=dict)
    system_prompt: str | None = None
    responsibilities: list[str] = Field(default_factory=list)


class RoleOut(BaseModel):
    slug: str
    display_name: str
    avatar_color: str | None = None
    avatar: str | None = None
    home_path: str
    enabled: bool = True
    created_at: str
    updated_at: str
    definition: dict[str, Any] = Field(default_factory=dict)  # 角色画像（description/capabilities/system_prompt…）


class ToolsIn(BaseModel):
    preset: str | None = None
    allowlist: list[dict[str, Any]] | None = None
    max_tool_tokens: int | None = None


# ---- Tasks ---------------------------------------------------------------


class KnowledgeRepoIn(BaseModel):
    repo: str
    reason: str | None = None


class TaskIn(BaseModel):
    id: str | None = None
    title: str
    type: str | None = None
    priority: Any = None
    tags: list[str] = Field(default_factory=list)
    repo: str | None = None
    source_repo: str | None = None
    workspace_id: str | None = None
    base_branch: str | None = None
    scope_paths: list[str] = Field(default_factory=list)
    description: str | None = None
    acceptance: list[str] = Field(default_factory=list)
    assignee: str | None = None
    assignees: list[str] = Field(default_factory=list)
    plan_first: bool = False
    planner: str | None = None
    plan_seed: dict[str, Any] | None = None
    created_by: str | None = None
    parent_id: str | None = None
    parent_step_id: str | None = None
    due_at: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    knowledge_repos: list[str | KnowledgeRepoIn] | None = None
    issue_board_enabled: bool = False


class TaskPatch(BaseModel):
    title: str | None = None
    type: str | None = None
    priority: Any = None
    tags: list[str] | None = None
    status: TaskStatus | None = None
    repo: str | None = None
    source_repo: str | None = None
    base_branch: str | None = None
    scope_paths: list[str] | None = None
    description: str | None = None
    acceptance: list[str] | None = None
    assignee: str | None = None
    knowledge_repos: list[str | KnowledgeRepoIn] | None = None
    issue_board_enabled: bool | None = None


class TaskWorkspaceOut(BaseModel):
    source_repo: str | None = None
    path: str | None = None
    kind: str | None = None
    status: str | None = None


class WorkspaceRefOut(BaseModel):
    id: str
    name: str
    repo: str | None = None


class TaskRoleClaimOut(BaseModel):
    role_slug: str
    status: str
    summary: str | None = None
    run_id: str | None = None
    claimed_at: str | None = None
    lease_expires_at: str | None = None
    submitted_at: str | None = None
    release_requested_at: str | None = None


class PlanSeedOut(BaseModel):
    template_id: str | None = None
    version: int | None = None
    name: str | None = None
    set_by: str | None = None
    set_at: str | None = None


class KnowledgeRepoBindingOut(BaseModel):
    repo: str
    source: str
    status: str
    confidence: float = 0.0
    score: float = 0.0
    matched_tags: list[str] = Field(default_factory=list)
    reason: str | None = None
    scope: str = "shared"
    repo_id: str | None = None
    path: str | None = None
    is_primary: bool = False
    created_at: str | None = None
    updated_at: str | None = None


class RunOut(BaseModel):
    id: str
    role_slug: str
    task_id: str | None = None
    status: RunStatus
    profile_snapshot: str | None = None
    worktree_path: str | None = None
    native_session_id: str | None = None
    log_path: str | None = None
    prompt: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    step_id: str | None = None
    workflow_id: str | None = None
    timeout_seconds: int | None = None
    failure_class: str | None = None
    context_reset_count: int = 0
    context_rotation_count: int = 0
    slice_key: str | None = None
    wake_note: str | None = None
    dispatch_id: str | None = None
    origin: str | None = None
    context_return_context_key: str | None = None
    context_return_run_id: str | None = None
    context_return_after_turn: int | None = None
    context_returned_at: str | None = None
    recovery_count: int = 0
    next_retry_at: str | None = None
    recovered_by: str | None = None
    recovery_of_run_id: str | None = None
    recovery_exhausted: int = 0
    context_return_attempts: int = 0
    context_return_last_attempt_at: str | None = None
    context_return_armed_at: str | None = None


class VerificationCellOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    cell: str | None = None
    method: str | None = None
    verifier_role: str | None = None


class DecisionOptionOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    label: str | None = None
    decision: str
    requires_note: bool | None = None


class ReviewEvidenceOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    kind: str | None = None
    ref: str | None = None
    label: str | None = None
    digest: str | None = None
    captured_at: str | None = None
    fresh: bool | None = None


class WorkflowDefinitionOut(BaseModel):
    """定义 JSON 的稳定子集；其余字段（slices/loop 等）原样透传。"""
    model_config = ConfigDict(extra="allow")

    feedback_rules: list[str] | None = None
    verification: list[VerificationCellOut] | None = None


class RevisionImpactOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    classification: str | None = None
    safe: bool | None = None
    summary: str | None = None
    affected_steps: list[str] | None = None


class PendingRevisionOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    draft_id: str
    impact: RevisionImpactOut | None = None
    created_by: str | None = None


class WorkflowStepOut(BaseModel):
    workflow_id: str
    step_id: str
    title: str
    role_slug: str | None = None
    instructions: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    gate: bool = False
    reviewer: str | None = None
    contract: str | None = None
    failure_reason: str | None = None
    last_nudge_at: str | None = None
    nudge_count: int = 0
    recovery_count: int = 0
    next_retry_at: str | None = None
    kind: str = "task"
    on_reject: list[str] = Field(default_factory=list)
    max_rework: int = 0
    rework_count: int = 0
    feedback: str | None = None
    source_step_id: str | None = None
    slice_key: str | None = None
    plan_json: str | None = None
    decision_authority: Literal["ai", "human", "joint", ""] | None = None
    risk_level: str | None = None
    reversible: bool = True
    escalate: list[str] = Field(default_factory=list)
    decision_question: str | None = None
    decision_options: list[DecisionOptionOut] = Field(default_factory=list)
    evidence: list[ReviewEvidenceOut] = Field(default_factory=list)
    evidence_required: bool = False
    subagent_hint: bool | None = None
    packet_id: str | None = None
    submitted_at: str | None = None
    scope_paths: list[str] = Field(default_factory=list)
    lease_id: str | None = None
    fence_token: int = 0
    status: str
    run_id: str | None = None
    summary: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    updated_at: str
    lease_holder: str | None = None
    lease_expires_at: str | None = None
    lease_fence: int | None = None


class WorkflowOut(BaseModel):
    id: str
    task_id: str
    status: Literal["running", "done", "failed", "terminated"]
    created_by: str | None = None
    created_at: str
    approved_at: str | None = None
    updated_at: str
    termination_reason: str | None = None
    terminated_at: str | None = None
    previous_workflow_id: str | None = None
    execution_no: int = 1
    weight: Literal["light", "standard", "strict"] | None = None
    weight_source: str | None = None
    weight_reason: str | None = None
    definition_hash: str | None = None
    finished_at: str | None = None
    template_id: str | None = None
    template_version: int | None = None
    definition: WorkflowDefinitionOut = Field(default_factory=WorkflowDefinitionOut)
    steps: list[WorkflowStepOut] = Field(default_factory=list)
    dispatch: dict[str, Any] | None = None
    pending_revision: PendingRevisionOut | None = None


class WorkflowMetricsDecisionsOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    total: int = 0
    rejected: int = 0


class WorkflowMetricsOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    decisions: WorkflowMetricsDecisionsOut = Field(
        default_factory=WorkflowMetricsDecisionsOut)


class WorkflowHistoryOut(BaseModel):
    id: str
    task_id: str
    status: str
    created_by: str | None = None
    created_at: str
    updated_at: str
    termination_reason: str | None = None
    terminated_at: str | None = None
    previous_workflow_id: str | None = None
    execution_no: int = 1
    template_id: str | None = None
    template_version: int | None = None
    weight: Literal["light", "standard", "strict"] | None = None
    weight_source: str | None = None
    weight_reason: str | None = None
    definition_hash: str | None = None
    finished_at: str | None = None
    step_count: int = 0
    approved_count: int | None = None
    metrics: WorkflowMetricsOut | None = None


class TaskOut(BaseModel):
    id: str
    title: str
    type: str | None = None
    status: TaskStatus
    priority: Any = None
    tags: list[str] = Field(default_factory=list)
    repo: str | None = None
    source_repo: str | None = None
    knowledge_mode: str | None = None
    task_workspace: TaskWorkspaceOut | None = None
    workspace_id: str | None = None
    workspace: WorkspaceRefOut | None = None
    base_branch: str | None = None
    scope_paths: list[str] = Field(default_factory=list)
    description: str | None = None
    acceptance: list[str] = Field(default_factory=list)
    assignee: str | None = None
    assignees: list[str] = Field(default_factory=list)
    plan_first: bool = False
    planner: str | None = None
    plan_seed: PlanSeedOut | None = None
    role_claims: list[TaskRoleClaimOut] = Field(default_factory=list)
    lease_expires_at: str | None = None
    claimed_at: str | None = None
    created_by: str | None = None
    parent_id: str | None = None
    parent_step_id: str | None = None
    source_task_id: str | None = None
    source_slice_key: str | None = None
    source_step_id: str | None = None
    source_workflow_id: str | None = None
    requested_by: str | None = None
    dispatch_status: str | None = None
    dispatch_reason: str | None = None
    dispatch_reason_json: Any = None
    due_at: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    knowledge_repos: list[KnowledgeRepoBindingOut] = Field(default_factory=list)
    knowledge_binding_locked: bool = False
    knowledge_binding_state: str | None = None
    issue_board_enabled: bool = False
    reviewer_roles: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    archived_at: str | None = None


FindingStatusLiteral = Literal[
    "draft", "submitted", "confirmed", "false_positive", "changes_requested"]


class FindingOut(BaseModel):
    id: str
    task_id: str
    author_role: str
    workflow_id: str | None = None
    step_id: str | None = None
    run_id: str | None = None
    title: str
    risk_type: str
    severity: str
    module: str
    function_name: str
    file_path: str
    line_numbers: str
    preconditions: str
    reasoning_chain: str
    risk_description: str
    attack_path: str
    trigger_point: str
    poc_status: str
    poc: str
    fix_priority: str
    fix_suggestion: str
    confidence: int = 0
    status: FindingStatusLiteral
    decided_by: str | None = None
    decided_at: str | None = None
    decision_note: str | None = None
    version: int = 1
    created_at: str
    updated_at: str
    submitted_at: str | None = None
    external_id: str | int | None = None
    external_source: str | None = None
    external_submitted_at: str | None = None


class FindingBoardOut(BaseModel):
    task_id: str
    status: Literal["in_progress", "completed"]
    summary: str | None = None
    created_at: str
    completed_by: str | None = None
    completed_at: str | None = None
    reopened_by: str | None = None
    reopened_at: str | None = None
    updated_at: str


class FindingSummaryOut(BaseModel):
    task_id: str
    task_status: str
    board: FindingBoardOut
    reviewers: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    severity_counts: dict[str, int] = Field(default_factory=dict)
    total: int = 0
    pending: int = 0
    can_complete: bool = False
    all_decided: bool = False
    items: list[FindingOut] | None = None


class FindingFieldSpecOut(BaseModel):
    key: str
    label: str
    required: bool | None = None
    max: int | None = None
    enum: dict[str, str] | None = None
    enum_labels: dict[str, str] | None = None
    int_: bool | None = Field(default=None, alias="int")


class FindingSchemaOut(BaseModel):
    fields: list[FindingFieldSpecOut] = Field(default_factory=list)
    risk_types: dict[str, str] = Field(default_factory=dict)
    severities: dict[str, str] = Field(default_factory=dict)
    fix_priorities: dict[str, str] = Field(default_factory=dict)
    poc_statuses: dict[str, str] = Field(default_factory=dict)
    statuses: list[str] = Field(default_factory=list)
    required: list[str] = Field(default_factory=list)


KnowledgeKindLiteral = Literal[
    "convention", "workflow", "role_requirement", "decision", "insight",
    "task_finding", "personal_lesson"]
KnowledgeAuthorityLiteral = Literal["normative", "reference"]
KnowledgeStatusLiteral = Literal[
    "draft", "proposed", "approved", "rejected", "deprecated", "expired"]
TaskKnowledgeStatusLiteral = Literal["active", "proposed", "archived"]


class QualityDimensionsOut(BaseModel):
    evidence: int = 0
    scenario: int = 0
    effect: int = 0
    readability: int = 0


class QualityReportOut(BaseModel):
    score: int = 0
    grade: str = "D"
    blocking: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    dimensions: QualityDimensionsOut = Field(default_factory=QualityDimensionsOut)
    readable: bool = False
    checked_at: str | None = None
    gate_version: int = 1
    legacy: bool = False
    resolved_fields: dict[str, Any] = Field(default_factory=dict)
    llm: dict[str, Any] | None = None


class TaskKnowledgeOut(BaseModel):
    id: str
    task_id: str
    task_title: str | None = None
    task_status: str | None = None
    workspace_id: str | None = None
    workspace_name: str | None = None
    task_archived_at: str | None = None
    hit_count: int = 0
    last_hit_at: str | None = None
    parent_task_id: str | None = None
    source_subtask_id: str | None = None
    origin: Literal["task", "subtask"]
    kind: KnowledgeKindLiteral
    title: str
    tldr: str
    body_md: str
    fields: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    source_role: str | None = None
    status: TaskKnowledgeStatusLiteral
    completeness: float = 0.0
    quality: QualityReportOut | None = None
    legacy: bool = False
    repo_id: str | None = None
    repo_key: str | None = None
    path: str | None = None
    source_event: str | None = None
    version: int = 1
    content_hash: str | None = None
    review_at: str | None = None
    quality_score: int | None = None
    quality_checked_at: str | None = None
    created_at: str
    updated_at: str


class WorkspaceKnowledgeOut(BaseModel):
    id: str
    workspace_id: str
    workspace_name: str | None = None
    repo: str | None = None
    repo_id: str | None = None
    kind: KnowledgeKindLiteral
    authority: KnowledgeAuthorityLiteral
    title: str
    tldr: str
    body_md: str
    fields: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    source_task_id: str | None = None
    source_subtask_id: str | None = None
    source_knowledge: list[Any] = Field(default_factory=list)
    owner: str | None = None
    related: list[str] = Field(default_factory=list)
    supersedes_id: str | None = None
    content_hash: str | None = None
    status: KnowledgeStatusLiteral
    version: int = 1
    origin: Literal["human", "agent", "system"]
    created_by: str | None = None
    created_at: str
    updated_at: str
    approved_at: str | None = None
    approved_by: str | None = None
    expires_at: str | None = None
    review_at: str | None = None
    conflicts: list[str] = Field(default_factory=list)
    completeness: float = 0.0
    quality: QualityReportOut | None = None
    legacy: bool = False
    hit_count: int = 0
    last_hit_at: str | None = None
    is_expired: bool = False
    freshness: float = 0.0
    template_version: int | None = None
    quality_score: int | None = None
    quality_checked_at: str | None = None


class WorkspaceKnowledgePageOut(BaseModel):
    items: list[WorkspaceKnowledgeOut] = Field(default_factory=list)
    total: int = 0
    limit: int = 0
    offset: int = 0
    has_more: bool = False


class TaskKnowledgePageOut(BaseModel):
    items: list[TaskKnowledgeOut] = Field(default_factory=list)
    total: int = 0
    limit: int = 0
    offset: int = 0
    has_more: bool = False


class DelegationOut(BaseModel):
    """委派工单：声明前端/CLI 使用的稳定字段；其余 task 列透传。"""
    model_config = ConfigDict(extra="allow")

    id: str
    title: str
    type: str | None = None
    status: TaskStatus
    parent_id: str | None = None
    parent_step_id: str | None = None
    step_title: str | None = None
    created_by: str | None = None
    assignee: str | None = None
    assignees: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    scope_paths: list[str] = Field(default_factory=list)
    description: str | None = None
    due_at: str | None = None
    run_id: str | None = None
    run_status: str | None = None
    run_failure_class: str | None = None
    submitted_summary: str | None = None
    created_at: str
    updated_at: str


class TaskDispatchOut(BaseModel):
    """派遣记录：声明展示/操作所需字段；请求哈希等内部列透传。"""
    model_config = ConfigDict(extra="allow")

    id: str
    source_task_id: str
    source_workflow_id: str | None = None
    source_step_id: str | None = None
    source_slice_key: str | None = None
    requester_run_id: str | None = None
    requested_by: str | None = None
    target_task_id: str
    target_role: str | None = None
    target_title: str | None = None
    target_status: str | None = None
    return_context_key: str | None = None
    return_run_id: str | None = None
    return_step_id: str | None = None
    reason_code: str
    reason_json: str = "{}"
    status: Literal[
        "open", "return_ready", "returning", "returned", "no_return", "cancelled"]
    attempts: int = 0
    returned_at: str | None = None
    created_at: str
    updated_at: str


class TaskRoleStateOut(BaseModel):
    role_slug: str
    state: Literal[
        "working", "closed", "done", "rework", "failed", "queued", "retry",
        "waiting_human", "not_involved", "released", "return_pending", "idle",
        "todo",
    ]
    queued: int = 0
    working_here: bool = False
    busy_elsewhere: bool = False


class TaskRoleStatesOut(BaseModel):
    task_id: str
    roles: list[TaskRoleStateOut] = Field(default_factory=list)


class TaskRoleStatesBatchOut(BaseModel):
    tasks: dict[str, list[TaskRoleStateOut]] = Field(default_factory=dict)


class TaskChildrenCountsOut(BaseModel):
    total: int = 0
    delegation: int = 0
    dispatch: int = 0


class TaskChildOut(BaseModel):
    relation_type: Literal["delegation", "dispatch"]
    relation_id: str | None = None
    task_id: str
    title: str | None = None
    status: TaskStatus | None = None
    type: str | None = None
    assignee: str | None = None
    assignees: list[str] = Field(default_factory=list)
    created_by: str | None = None
    parent_step_id: str | None = None
    step_title: str | None = None
    due_at: str | None = None
    acceptance: list[str] = Field(default_factory=list)
    scope_paths: list[str] = Field(default_factory=list)
    description: str | None = None
    submitted_summary: str | None = None
    run_id: str | None = None
    run_status: str | None = None
    run_failure_class: str | None = None
    blocking: bool = False
    dispatch_status: str | None = None
    return_pending: bool = False
    return_context_key: str | None = None
    returned_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class TaskChildrenOut(BaseModel):
    items: list[TaskChildOut] = Field(default_factory=list)
    counts: TaskChildrenCountsOut = Field(default_factory=TaskChildrenCountsOut)


class ClaimIn(BaseModel):
    paths: list[str] | None = None


class SubmitIn(BaseModel):
    summary: str | None = None
    branch: str | None = None
    changed_paths: list[str] | None = None
    force_terminate_workflow: bool = False


class ReviewIn(BaseModel):
    approve: bool
    feedback: str | None = None


class FindingIn(BaseModel):
    """问题板 finding。draft 可部分填写；submit 时由服务层校验完整性。"""
    title: str = ""
    risk_type: str = ""
    severity: str = ""
    module: str = ""
    function_name: str = ""
    file_path: str = ""
    line_numbers: str = ""
    preconditions: str = ""
    reasoning_chain: str = ""
    risk_description: str = ""
    attack_path: str = ""
    trigger_point: str = ""
    poc_status: str = ""
    poc: str = ""
    fix_priority: str = ""
    fix_suggestion: str = ""
    confidence: int = 0
    workflow_id: str | None = None
    step_id: str | None = None
    run_id: str | None = None


class FindingPatch(BaseModel):
    title: str | None = None
    risk_type: str | None = None
    severity: str | None = None
    module: str | None = None
    function_name: str | None = None
    file_path: str | None = None
    line_numbers: str | None = None
    preconditions: str | None = None
    reasoning_chain: str | None = None
    risk_description: str | None = None
    attack_path: str | None = None
    trigger_point: str | None = None
    poc_status: str | None = None
    poc: str | None = None
    fix_priority: str | None = None
    fix_suggestion: str | None = None
    confidence: int | None = None
    workflow_id: str | None = None
    step_id: str | None = None
    run_id: str | None = None


class FindingDecisionIn(BaseModel):
    note: str | None = None


class FindingCompleteIn(BaseModel):
    summary: str


class ReviewersIn(BaseModel):
    roles: list[str] = Field(default_factory=list)


class DepIn(BaseModel):
    depends_on: list[str]


class CommentIn(BaseModel):
    body: str
    expect_activity_id: int | None = None  # Held Draft：发送时依据的最新活动 id
    force: bool = False                    # 知情越过 held


# ---- Memory / notes / proposals / lessons --------------------------------


class MemoryAppendIn(BaseModel):
    content: str
    practice_task_id: str | None = None
    practice_run_id: str | None = None
    outcome: str | None = None
    source_knowledge: list[str] = Field(default_factory=list)
    force: bool = False
    # 默认写经验卡；durable=True 写 PRINCIPLES.md 耐久准则。
    durable: bool = False
    kind: str = "personal_lesson"
    tags: list[str] = Field(default_factory=list)
    repo: str | None = None


class NoteWriteIn(BaseModel):
    path: str
    content: str
    kind: str | None = None
    scope: str | None = None
    task_id: str | None = None
    slice_key: str | None = None
    source_task: str | None = None
    tags: list[str] = Field(default_factory=list)
    repo: str | None = None
    module: str | None = None
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    mode: str = "append"  # append | replace
    practice_task_id: str | None = None
    practice_run_id: str | None = None
    outcome: str | None = None
    source_knowledge: list[str] = Field(default_factory=list)
    force: bool = False
    title: str | None = None
    tldr: str | None = None
    fields: dict | None = None


class ProposalIn(BaseModel):
    field: str
    proposed: Any = Field(default_factory=dict)  # 可以是对象或列表（如 responsibilities）
    rationale: str | None = None
    risk: str | None = None


class LessonIn(BaseModel):
    content: str
    tags: list[str] = Field(default_factory=list)
    repo: str | None = None
    source_task: str | None = None
    title: str | None = None
    tldr: str | None = None
    fields: dict = Field(default_factory=dict)


class InstructionIn(BaseModel):
    content: str
