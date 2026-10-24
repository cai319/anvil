"""Pydantic schemas (SE v1.0 §3 / design §10)."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


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
    depends_on: list[str] = Field(default_factory=list)
    knowledge_repos: list[str | KnowledgeRepoIn] | None = None


class TaskPatch(BaseModel):
    title: str | None = None
    type: str | None = None
    priority: Any = None
    tags: list[str] | None = None
    status: TaskStatus | None = None
    repo: str | None = None
    base_branch: str | None = None
    scope_paths: list[str] | None = None
    description: str | None = None
    acceptance: list[str] | None = None
    assignee: str | None = None
    knowledge_repos: list[str | KnowledgeRepoIn] | None = None


class TaskOut(BaseModel):
    id: str
    title: str
    type: str | None = None
    status: TaskStatus
    priority: Any = None
    tags: list[str] = Field(default_factory=list)
    repo: str | None = None
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
    role_claims: list[dict[str, Any]] = Field(default_factory=list)
    lease_expires_at: str | None = None
    claimed_at: str | None = None
    created_by: str | None = None
    parent_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    knowledge_repos: list[dict[str, Any]] = Field(default_factory=list)
    knowledge_binding_locked: bool = False
    knowledge_binding_state: str | None = None
    created_at: str
    updated_at: str


class ClaimIn(BaseModel):
    paths: list[str] | None = None


class SubmitIn(BaseModel):
    summary: str | None = None
    branch: str | None = None
    changed_paths: list[str] | None = None


class ReviewIn(BaseModel):
    approve: bool


class DepIn(BaseModel):
    depends_on: list[str]


class CommentIn(BaseModel):
    body: str
    expect_activity_id: int | None = None  # Held Draft：发送时依据的最新活动 id
    force: bool = False                    # 知情越过 held


# ---- Memory / notes / proposals / lessons --------------------------------


class MemoryAppendIn(BaseModel):
    content: str


class NoteWriteIn(BaseModel):
    path: str
    content: str
    kind: str | None = None
    scope: str | None = None
    task_id: str | None = None
    source_task: str | None = None
    tags: list[str] = Field(default_factory=list)
    repo: str | None = None
    module: str | None = None
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    mode: str = "append"  # append | replace


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


class InstructionIn(BaseModel):
    content: str
