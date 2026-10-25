const BASE = "/api/v1";

export interface KnowledgeRepoBinding {
  repo: string;
  source: "auto" | "cindy" | "human" | "inherited" | "agent";
  status: "applied" | "suggested" | "dismissed";
  confidence: number;
  score: number;
  matched_tags: string[];
  reason: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface KnowledgeRepoProfile {
  repo: string;
  key: string;
  description?: string;
  top_tags?: string[];
  counts?: Record<string, number>;
  aliases?: string[];
}

export interface KnowledgeRepoScope {
  mode: "multi" | "legacy" | "shared" | "any";
  repos: string[] | null;
  repo: string | null;
}

export interface Task {
  id: string;
  title: string;
  type: string | null;
  status: "todo" | "in_progress" | "in_review" | "done" | "closed";
  priority: unknown;
  tags: string[];
  repo: string | null;
  workspace_id?: string | null;
  base_branch?: string | null;
  knowledge_repos?: KnowledgeRepoBinding[];
  knowledge_binding_locked?: boolean;
  knowledge_binding_state?: "applied" | "suggested" | "needs_cindy" | "fallback" | "locked" | "off";
  scope_paths: string[];
  description: string | null;
  acceptance: string[];
  assignee: string | null;
  assignees: string[];
  plan_first: boolean;
  planner: string | null;
  plan_seed?: {
    template_id: string;
    version: number;
    name?: string;
    set_by?: string;
    set_at?: string;
  } | null;
  role_claims: {
    role_slug: string;
    status: "pending" | "working" | "submitted" | "released" | "approved";
    summary: string | null;
    run_id: string | null;
    claimed_at: string | null;
    lease_expires_at: string | null;
    submitted_at: string | null;
  }[];
  depends_on: string[];
  parent_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface Runtime {
  id: string;
  display_name: string;
  executable_path: string;
  detected_version: string | null;
  status: string;
  last_probe_at: string | null;
  last_probe_log: string | null;
}

export interface Role {
  slug: string;
  display_name: string;
  avatar?: string | null;
  avatar_color?: string | null;
  home_path: string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
  definition?: Record<string, unknown>;
}

export interface RoleTaskContext {
  task_id: string | null;
  task_title: string;
  task_status: string | null;
  has_session: boolean;
  session_id: string;
  context_tokens: number;
  last_active_at: string | null;
  status: string;
  cwd: string | null;
  workspace_path: string | null;
}

export interface RoleRuntimeStatus {
  role_slug: string;
  alive: boolean;
  status: string;
  busy_since: string | null;
  current_task_id: string | null;
  current_task: { id: string; title: string; status: string | null } | null;
  current_run_id: string | null;
  current_step_id: string | null;
  context_task_id: string | null;
  queue_len: number;
  queue_tasks: { id: string; title: string; status: string | null; count: number }[];
  contexts: RoleTaskContext[];
  switching?: boolean;
  context_tokens: number;
  model_id: string | null;
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    // FastAPI 的错误原因在响应体 detail 里；只显示状态码会丢失最有用的诊断信息。
    const raw = await res.text().catch(() => "");
    let detail = raw.trim();
    try {
      const parsed = JSON.parse(raw);
      if (typeof parsed?.detail === "string") detail = parsed.detail;
      else if (parsed?.detail != null) detail = JSON.stringify(parsed.detail);
    } catch {
      // 非 JSON 响应：保留原文
    }
    throw new Error(
      detail ? `${res.status} ${res.statusText}: ${detail}` : `${res.status} ${res.statusText}`,
    );
  }
  return res.json() as Promise<T>;
}

// 本地共享 token（首启配置；agent 面端点轻校验用）。
// 生产设置了 ANVIL_AUTH_TOKEN 时，用 VITE_ANVIL_TOKEN 构建或写入 localStorage 覆盖。
const ENV_TOKEN = (import.meta as unknown as { env?: Record<string, string> }).env?.VITE_ANVIL_TOKEN;
const STORED_TOKEN = typeof localStorage !== "undefined" ? localStorage.getItem("anvil-session") : null;
const SESSION = ENV_TOKEN || STORED_TOKEN || "anvil-web";

const nativeFetch = typeof window !== "undefined" ? window.fetch.bind(window) : fetch;

/** 所有 /api/v1 请求统一带人类会话头；显式传入的 role 优先。 */
function authFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
  if (url.startsWith(BASE)) {
    const headers = new Headers(init.headers || {});
    if (!headers.has("X-Anvil-Session")) headers.set("X-Anvil-Session", SESSION);
    if (!headers.has("X-Anvil-Role")) headers.set("X-Anvil-Role", "human");
    init = { ...init, headers };
  }
  return nativeFetch(input, init);
}

function req(method: string, path: string, body?: unknown, role?: string): Promise<Response> {
  const headers: Record<string, string> = { "Content-Type": "application/json", "X-Anvil-Session": SESSION };
  if (role) headers["X-Anvil-Role"] = role;
  return authFetch(`${BASE}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export const api = {
  // tasks
  listTasks: (): Promise<Task[]> => authFetch(`${BASE}/tasks`).then((r) => json<Task[]>(r)),
  getTask: (id: string): Promise<Task> => authFetch(`${BASE}/tasks/${id}`).then((r) => json<Task>(r)),
  createTask: (title: string, opts?: {
    description?: string; assignees?: string[]; plan_first?: boolean; planner?: string;
    plan_seed?: { template_id: string; version: number; name?: string } | null;
    tags?: string[]; repo?: string | null; workspace_id?: string | null;
    base_branch?: string | null; knowledge_repos?: (string | { repo: string; reason?: string })[];
  }): Promise<Task> =>
    req("POST", "/tasks", { title, scope_paths: ["**"], ...opts }).then((r) => json<Task>(r)),
  patchTask: (id: string, patch: {
    knowledge_repos?: (string | { repo: string; reason?: string })[];
    tags?: string[];
  }): Promise<Task> =>
    req("PATCH", `/tasks/${id}`, patch).then((r) => json<Task>(r)),
  getTaskKnowledgeRepos: (id: string): Promise<{
    task_id: string;
    locked: boolean;
    state: Task["knowledge_binding_state"] | null;
    repos: KnowledgeRepoBinding[];
    scope: KnowledgeRepoScope;
  }> => authFetch(`${BASE}/tasks/${id}/knowledge-repos`).then((r) => json(r)),
  reautoTaskKnowledgeRepos: (id: string): Promise<{
    task_id: string; locked: boolean; state: string | null;
    repos: KnowledgeRepoBinding[]; scope: KnowledgeRepoScope;
  }> => req("POST", `/tasks/${id}/knowledge-repos/auto`).then((r) => json(r)),
  acceptTaskKnowledgeRepo: (id: string, repo: string): Promise<unknown> =>
    req("POST", `/tasks/${id}/knowledge-repos/${encodeURIComponent(repo)}/accept`).then((r) => json(r)),
  dismissTaskKnowledgeRepo: (id: string, repo: string): Promise<unknown> =>
    req("POST", `/tasks/${id}/knowledge-repos/${encodeURIComponent(repo)}/dismiss`).then((r) => json(r)),
  listKnowledgeRepos: (): Promise<{ repos: KnowledgeRepoProfile[]; total: number; enabled: boolean }> =>
    authFetch(`${BASE}/knowledge/repos`).then((r) => json(r)),
  suggestKnowledgeRepos: (body: {
    title: string; description?: string; tags?: string[]; repo?: string | null;
    workspace_id?: string | null; task_id?: string | null; limit?: number;
  }): Promise<{ candidates: (KnowledgeRepoProfile & { score: number; confidence: number; reason: string })[] }> =>
    req("POST", "/knowledge/repos/suggest", body).then((r) => json(r)),
  listTaskChildren: (parentId: string): Promise<Task[]> =>
    authFetch(`${BASE}/tasks?parent_id=${encodeURIComponent(parentId)}`).then((r) => json<Task[]>(r)),
  listTaskRuns: (taskId: string): Promise<Run[]> =>
    authFetch(`${BASE}/runs?task_id=${encodeURIComponent(taskId)}`).then((r) => json<Run[]>(r)),
  getTaskWorkflow: (taskId: string): Promise<Workflow> =>
    authFetch(`${BASE}/tasks/${taskId}/workflow`).then((r) => json<Workflow>(r)),
  getWorkflow: (wid: string): Promise<Workflow> =>
    authFetch(`${BASE}/workflows/${wid}`).then((r) => json<Workflow>(r)),
  listTaskWorkflows: (taskId: string): Promise<WorkflowHistoryItem[]> =>
    authFetch(`${BASE}/tasks/${taskId}/workflows`).then((r) => json<WorkflowHistoryItem[]>(r)),
  getWorkflowMetrics: (wid: string): Promise<WorkflowMetrics> =>
    authFetch(`${BASE}/workflows/${wid}/metrics`).then((r) => json<WorkflowMetrics>(r)),
  getWorkflowGraph: (wid: string, mode: "planned" | "actual" | "diff"): Promise<WorkflowGraphResponse> =>
    authFetch(`${BASE}/workflows/${wid}/graph?mode=${mode}`).then((r) => json<WorkflowGraphResponse>(r)),
  getWorkflowDossier: (wid: string): Promise<WorkflowDossier> =>
    authFetch(`${BASE}/workflows/${wid}/dossier`).then((r) => json<WorkflowDossier>(r)),
  rebuildWorkflowDossier: (wid: string): Promise<WorkflowDossier> =>
    req("POST", `/workflows/${wid}/dossier/rebuild`, {}).then((r) => json<WorkflowDossier>(r)),
  downloadWorkflowDossier: async (wid: string, format: "json" | "md"): Promise<Blob> => {
    const res = await authFetch(`${BASE}/workflows/${wid}/dossier?format=${format}`);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.blob();
  },
  downloadTaskReplayLog: async (taskId: string): Promise<Blob> => {
    const res = await authFetch(`${BASE}/tasks/${taskId}/replay/log`);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.blob();
  },
  setTaskWorkflowWeight: (taskId: string, weight: WorkflowWeight, reason?: string): Promise<TaskWeightResult> =>
    req("POST", `/tasks/${taskId}/workflow-weight`, { weight, reason }).then((r) => json<TaskWeightResult>(r)),
  clearTaskWorkflowWeight: (taskId: string): Promise<TaskWeightResult> =>
    req("DELETE", `/tasks/${taskId}/workflow-weight`).then((r) => json<TaskWeightResult>(r)),
  restartTaskWorkflow: (taskId: string, reason?: string, note?: string): Promise<Workflow> =>
    req("POST", `/tasks/${taskId}/workflow/restart`, { reason, note }).then((r) => json<Workflow>(r)),
  listTaskLeases: (taskId: string): Promise<TaskLeases> =>
    authFetch(`${BASE}/tasks/${taskId}/leases`).then((r) => json<TaskLeases>(r)),
  reserveTaskPaths: (taskId: string, paths: string[], holder?: string): Promise<TaskLeases> =>
    req("POST", `/tasks/${taskId}/reservations`, { paths, holder }).then((r) => json<TaskLeases>(r)),
  releaseTaskPaths: (taskId: string, paths: string[], holder?: string): Promise<{ released: number }> =>
    req("DELETE", `/tasks/${taskId}/reservations`, { paths, holder }).then((r) => json<{ released: number }>(r)),
  reviewWorkflowStep: (wid: string, sid: string, approve: boolean, feedback?: string,
    opts?: { rework_targets?: string[]; reasons?: string[]; packet_id?: string; override_reason?: string }): Promise<Workflow> =>
    req("POST", `/workflows/${wid}/steps/${sid}/review`, {
      approve, feedback, ...opts,
    }).then((r) => json<Workflow>(r)),
  decideWorkflowStep: (wid: string, sid: string, body: {
    approve: boolean; feedback?: string; packet_id?: string;
    rework_targets?: string[]; reasons?: string[]; override_reason?: string;
    acknowledge_stale?: boolean;
  }): Promise<Workflow> =>
    req("POST", `/workflows/${wid}/steps/${sid}/decide`, body).then((r) => json<Workflow>(r)),
  escalateWorkflowStep: (wid: string, sid: string, body: {
    reason: string; triggers?: string[]; proposed_decision?: string; proposed_feedback?: string;
  }): Promise<Workflow> =>
    req("POST", `/workflows/${wid}/steps/${sid}/escalate`, body).then((r) => json<Workflow>(r)),
  getStepPacket: (wid: string, sid: string): Promise<StepPacketResponse> =>
    authFetch(`${BASE}/workflows/${wid}/steps/${sid}/packet`)
      .then((r) => json<StepPacketResponse>(r)),
  refreshStepPacket: (wid: string, sid: string): Promise<{ packet: ReviewPacket; decisions: DecisionRecord[] }> =>
    req("POST", `/workflows/${wid}/steps/${sid}/packet/refresh`, {})
      .then((r) => json<{ packet: ReviewPacket; decisions: DecisionRecord[] }>(r)),
  retryWorkflowStep: (wid: string, sid: string): Promise<Workflow> =>
    req("POST", `/workflows/${wid}/steps/${sid}/retry`, {}).then((r) => json<Workflow>(r)),
  planTask: (id: string): Promise<Run> =>
    req("POST", `/tasks/${id}/plan`).then((r) => json<Run>(r)),
  dispatchTask: (id: string): Promise<{
    mode: "planning" | "pending_review" | "direct";
    planner?: string; run?: Run; runs?: Run[]; draft_id?: string;
    draft_kind?: "tasks" | "workflow" | "role_suggestion";
  }> => req("POST", `/tasks/${id}/dispatch`).then((r) => json(r)),
  autoCreateRole: (brief: string, runtimeId?: string, avatar?: string | null): Promise<Role> =>
    req("POST", "/roles/auto", {
      brief,
      runtime_id: runtimeId || undefined,
      avatar: avatar ?? undefined,
    }).then((r) => json<Role>(r)),
  approveActionCard: (id: string): Promise<unknown> =>
    req("POST", `/action-cards/${id}/approve`, {}).then((r) => json(r)),
  rejectActionCard: (id: string): Promise<unknown> =>
    req("POST", `/action-cards/${id}/reject`, {}).then((r) => json(r)),
  getInbox: (): Promise<{ count: number; items: InboxItem[] }> =>
    authFetch(`${BASE}/inbox`).then((r) => json<{ count: number; items: InboxItem[] }>(r)),
  rejectDraftById: (id: string, feedback?: string): Promise<unknown> =>
    req("POST", "/board/drafts/reject", { draft_id: id, feedback }).then((r) => json(r)),
  approveDraft: (draftIds: string[], options: {
    restart_running?: boolean; restart_reason?: string; restart_note?: string;
  } = {}): Promise<{
    landed: string[]; replans?: unknown[]; warnings?: string[]; runs?: Run[];
  }> =>
    req("POST", "/board/drafts/approve", { draft_ids: draftIds, ...options })
      .then((r) => json(r)),
  assignRoles: (id: string, roles: string[]): Promise<Task> =>
    req("POST", `/tasks/${id}/assign`, { roles }).then((r) => json<Task>(r)),
  assignTask: (id: string, assignee: string): Promise<Task> =>
    req("PATCH", `/tasks/${id}`, { assignee }).then((r) => json<Task>(r)),
  claimTask: (id: string, role: string): Promise<Task> =>
    req("POST", `/tasks/${id}/claim`, {}, role).then((r) => json<Task>(r)),
  unclaimTask: (id: string): Promise<Task> =>
    req("POST", `/tasks/${id}/unclaim`, {}).then((r) => json<Task>(r)),
  submitTask: (id: string): Promise<Task> =>
    req("POST", `/tasks/${id}/submit`, { summary: "web submit" }).then((r) => json<Task>(r)),
  reviewTask: (id: string, approve: boolean): Promise<Task> =>
    req("POST", `/tasks/${id}/review`, { approve }).then((r) => json<Task>(r)),
  getTaskTimeline: (id: string): Promise<TimelineEvent[]> =>
    authFetch(`${BASE}/tasks/${id}/timeline`).then((r) => json<TimelineEvent[]>(r)),
  commentTask: (id: string, body: string, role?: string): Promise<Task> =>
    req("POST", `/tasks/${id}/comment`, { body }, role).then((r) => json<Task>(r)),
  mentionTask: (id: string, target: string, message: string, role?: string): Promise<Task> =>
    req("POST", `/tasks/${id}/mention`, { target, message }, role).then((r) => json<Task>(r)),

  // runtimes
  listRuntimes: (): Promise<Runtime[]> => authFetch(`${BASE}/runtimes`).then((r) => json<Runtime[]>(r)),
  discoverRuntimes: (): Promise<Runtime[]> =>
    req("POST", "/runtimes/discover").then((r) => json<Runtime[]>(r)),
  getDefaultRuntime: (): Promise<{ id: string; runtime: Runtime | null }> =>
    authFetch(`${BASE}/runtimes/default`).then((r) => json(r)),
  setDefaultRuntime: (id: string): Promise<{ id: string; runtime: Runtime | null }> =>
    req("PUT", "/runtimes/default", { id }).then((r) => json(r)),
  getRuntimeDefinition: (id: string): Promise<{ id: string; yaml: string; source: string; builtin: boolean }> =>
    authFetch(`${BASE}/runtimes/${encodeURIComponent(id)}/definition`).then((r) => json(r)),
  putRuntimeDefinition: (id: string, yaml: string): Promise<Runtime> =>
    req("PUT", `/runtimes/${encodeURIComponent(id)}/definition`, { yaml }).then((r) => json(r)),
  checkRuntimeHealth: (id: string): Promise<{ ok: boolean | null; status: string; detail?: string; log?: string }> =>
    req("POST", `/runtimes/${id}/health`, {}).then((r) => json(r)),

  // runs（运行生命周期）
  listRuns: (): Promise<Run[]> => authFetch(`${BASE}/runs`).then((r) => json<Run[]>(r)),
  listActiveRuns: (): Promise<Run[]> => authFetch(`${BASE}/runs?active=true`).then((r) => json<Run[]>(r)),
  startRun: (roleSlug: string, taskId: string): Promise<Run> =>
    req("POST", `/runs`, { role_slug: roleSlug, task_id: taskId }).then((r) => json<Run>(r)),
  listRoleRuns: (roleSlug: string): Promise<Run[]> =>
    authFetch(`${BASE}/runs?role_slug=${encodeURIComponent(roleSlug)}`).then((r) => json<Run[]>(r)),
  getRun: (id: string): Promise<Run> => authFetch(`${BASE}/runs/${id}`).then((r) => json<Run>(r)),
  getRunTimeline: (id: string): Promise<TimelineEvent[]> =>
    authFetch(`${BASE}/runs/${id}/timeline`).then((r) => json<TimelineEvent[]>(r)),
  getRunLog: (id: string): Promise<{ run_id: string; exists: boolean; content: string }> =>
    authFetch(`${BASE}/runs/${id}/log`).then((r) => json(r)),
  getRunSnapshot: (id: string): Promise<Record<string, unknown>> =>
    authFetch(`${BASE}/runs/${id}/snapshot`).then((r) => json(r)),
  restartRun: (id: string): Promise<Run> => req("POST", `/runs/${id}/restart`, {}).then((r) => json(r)),
  sessionResetRun: (id: string): Promise<Run> => req("POST", `/runs/${id}/session-reset`, {}).then((r) => json(r)),
  fullResetRun: (id: string): Promise<Run> => req("POST", `/runs/${id}/full-reset`, {}).then((r) => json(r)),
  releaseRun: (id: string): Promise<void> =>
    req("DELETE", `/runs/${id}`).then((r) => {
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    }),

  // roles
  listRoles: (): Promise<Role[]> => authFetch(`${BASE}/roles`).then((r) => json<Role[]>(r)),
  getRole: (slug: string): Promise<Role> => authFetch(`${BASE}/roles/${slug}`).then((r) => json<Role>(r)),
  getEffectiveConfig: (slug: string): Promise<Record<string, unknown>> =>
    authFetch(`${BASE}/roles/${slug}/effective-config`).then((r) => json(r)),
  getRuntimeCatalog: (id: string): Promise<{
    runtime: string;
    skills: { name: string; description: string }[];
    mcp_servers: {
      name: string; description?: string; transport?: string;
      hint?: string; tool_count?: number;
    }[];
  }> => authFetch(`${BASE}/runtimes/${id}/catalog`).then((r) => json(r)),
  getRoleCatalog: (slug: string): Promise<{ skills: string[]; mcp: string[] }> =>
    authFetch(`${BASE}/roles/${slug}/catalog`).then((r) => json(r)),
  updateRoleCatalog: (slug: string, skills: string[], mcp: string[]): Promise<{ skills: string[]; mcp: string[] }> =>
    req("PUT", `/roles/${slug}/catalog`, { skills, mcp }).then((r) => json(r)),
  generateRole: (slug: string, brief: string): Promise<{ working_mode?: string; skill_description?: string; responsibilities?: string[]; system_prompt?: string }> =>
    req("POST", `/roles/${slug}/generate`, { brief }).then((r) => json(r)),
  updateRoleTools: (slug: string, body: Record<string, unknown>): Promise<Record<string, unknown>> =>
    req("PUT", `/roles/${slug}/tools`, body).then((r) => json(r)),
  getRoleProfile: (slug: string): Promise<{ system_prompt: string; responsibilities: string[] }> =>
    authFetch(`${BASE}/roles/${slug}/profile`).then((r) => json(r)),
  updateRoleProfile: (slug: string, system_prompt: string, responsibilities: string[]): Promise<{ system_prompt: string; responsibilities: string[] }> =>
    req("PUT", `/roles/${slug}/profile`, { system_prompt, responsibilities }).then((r) => json(r)),
  createRole: (slug: string, displayName: string, runtimeId?: string, avatar?: string | null): Promise<Role> =>
    req("POST", "/roles", {
      slug,
      display_name: displayName,
      avatar: avatar ?? undefined,
      runtime: runtimeId ? { id: runtimeId } : {},
      workspace: { home: `agents/${slug}/`, repos: [] },
      tools: {},
      concurrency: {},
    }).then((r) => json<Role>(r)),
  updateRoleAvatar: (slug: string, avatar: string | null): Promise<Role> =>
    req("PUT", `/roles/${slug}/avatar`, { avatar }).then((r) => json<Role>(r)),
  updateRoleRuntime: (slug: string, runtimeId: string): Promise<Role> =>
    req("PUT", `/roles/${slug}/runtime`, { id: runtimeId }).then((r) => json<Role>(r)),
  getRoleRuntime: (slug: string): Promise<RoleRuntimeStatus> =>
    authFetch(`${BASE}/roles/${slug}/runtime`).then((r) => json<RoleRuntimeStatus>(r)),
  listRoleRuntimes: (): Promise<RoleRuntimeStatus[]> =>
    authFetch(`${BASE}/roles/runtime`).then((r) => json<RoleRuntimeStatus[]>(r)),
  resetRoleSession: (slug: string): Promise<{ slug: string; reset: string }> =>
    req("POST", `/roles/${slug}/session-reset`, {}).then((r) => json(r)),
  deleteRole: (slug: string): Promise<void> => {
    return req("DELETE", `/roles/${slug}`).then((r) => {
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    });
  },

  // governance
  getFreeze: (): Promise<{ frozen: boolean }> => authFetch(`${BASE}/control/freeze`).then((r) => json(r)),
  freezeAll: (): Promise<{ frozen: boolean }> => req("POST", "/control/freeze-all").then((r) => json(r)),
  resumeAll: (): Promise<{ frozen: boolean }> => req("POST", "/control/resume-all").then((r) => json(r)),
  getTokens: (): Promise<{
    input_tokens: number; output_tokens: number; total_tokens: number;
    today_total_tokens: number;
    by_role: { role: string; input_tokens: number; output_tokens: number; total_tokens: number }[];
    by_day: { day: string; input_tokens: number; output_tokens: number; total_tokens: number }[];
  }> => authFetch(`${BASE}/tokens`).then((r) => json(r)),
  getBudget: (): Promise<{
    role: string | null;
    input_tokens: number; output_tokens: number; total_tokens: number;
    today_total_tokens: number;
    max_parallel_system?: number;
    max_parallel_runtime?: number;
    daily_token_budget?: number | null;
  }> => authFetch(`${BASE}/budget`).then((r) => json(r)),
  listAudit: (opts?: { limit?: number; role?: string; action?: string }): Promise<unknown[]> => {
    const q = new URLSearchParams();
    if (opts?.limit) q.set("limit", String(opts.limit));
    if (opts?.role) q.set("role", opts.role);
    if (opts?.action) q.set("action", opts.action);
    const qs = q.toString();
    return authFetch(`${BASE}/audit${qs ? `?${qs}` : ""}`).then((r) => json(r));
  },
  listTaskAudit: (taskId: string): Promise<unknown[]> =>
    authFetch(`${BASE}/audit?task_id=${taskId}`).then((r) => json(r)),

  // board (目标拆解)
  decompose: (goal: string, template?: string): Promise<Record<string, unknown>> =>
    req("POST", "/board/decompose", { goal, template }).then((r) => json(r)),
  listBoardTemplates: (): Promise<{ name: string; steps: string[] }[]> =>
    authFetch(`${BASE}/board/templates`).then((r) => json(r)),
  listDrafts: (sourceTask?: string): Promise<BoardDraft[]> =>
    req("GET", `/board/drafts${sourceTask ? `?source_task=${sourceTask}` : ""}`).then((r) => json<BoardDraft[]>(r)),
  approveDrafts: (ids: string[], options: {
    restart_running?: boolean; restart_reason?: string; restart_note?: string;
  } = {}): Promise<Record<string, unknown>> =>
    req("POST", "/board/drafts/approve", { draft_ids: ids, ...options }).then((r) => json(r)),
  rejectDraft: (id: string): Promise<Record<string, unknown>> =>
    req("POST", "/board/drafts/reject", { draft_id: id }).then((r) => json(r)),
  getDraft: (id: string): Promise<BoardDraft> =>
    authFetch(`${BASE}/board/drafts/${id}`).then((r) => json<BoardDraft>(r)),
  updateDraft: (id: string, body: Record<string, unknown>): Promise<BoardDraft> =>
    req("PATCH", `/board/drafts/${id}`, body).then((r) => json<BoardDraft>(r)),

  // workflow templates
  listWorkflowTemplates: (q?: string, status?: string): Promise<WorkflowTemplate[]> => {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (status) params.set("status", status);
    const qs = params.toString();
    return authFetch(`${BASE}/workflow-templates${qs ? `?${qs}` : ""}`)
      .then((r) => json<WorkflowTemplate[]>(r));
  },
  getWorkflowTemplate: (id: string): Promise<WorkflowTemplate> =>
    authFetch(`${BASE}/workflow-templates/${id}`).then((r) => json<WorkflowTemplate>(r)),
  createWorkflowTemplate: (body: Record<string, unknown>): Promise<WorkflowTemplate> =>
    req("POST", "/workflow-templates", body).then((r) => json<WorkflowTemplate>(r)),
  saveWorkflowTemplateDraft: (id: string, body: Record<string, unknown>): Promise<WorkflowTemplate> =>
    req("PUT", `/workflow-templates/${id}/draft`, body).then((r) => json<WorkflowTemplate>(r)),
  validateWorkflowTemplate: (id: string, body: Record<string, unknown> = {}): Promise<{ issues: TemplateValidationIssue[] }> =>
    req("POST", `/workflow-templates/${id}/validate`, body).then((r) => json(r)),
  publishWorkflowTemplate: (id: string, changeNote?: string): Promise<WorkflowTemplate> =>
    req("POST", `/workflow-templates/${id}/publish`, { change_note: changeNote }).then((r) => json<WorkflowTemplate>(r)),
  archiveWorkflowTemplate: (id: string): Promise<WorkflowTemplate> =>
    req("POST", `/workflow-templates/${id}/archive`, {}).then((r) => json<WorkflowTemplate>(r)),
  getWorkflowTemplateVersion: (id: string, version: number): Promise<WorkflowTemplateVersion> =>
    authFetch(`${BASE}/workflow-templates/${id}/versions/${version}`)
      .then((r) => json<WorkflowTemplateVersion>(r)),
  rollbackWorkflowTemplate: (id: string, version: number): Promise<WorkflowTemplate> =>
    req("POST", `/workflow-templates/${id}/rollback`, { version }).then((r) => json<WorkflowTemplate>(r)),
  duplicateWorkflowTemplate: (id: string, name?: string): Promise<WorkflowTemplate> =>
    req("POST", `/workflow-templates/${id}/duplicate`, { name }).then((r) => json<WorkflowTemplate>(r)),
  listTemplateImportSources: (): Promise<{
    workflows: { id: string; task_id: string; task_title: string; status: string }[];
    drafts: { id: string; source_task: string | null; task_title?: string | null; goal: string; created_at: string }[];
    sops: { name: string; steps: string[] }[];
  }> => authFetch(`${BASE}/workflow-templates/import-sources`).then((r) => json(r)),
  importWorkflowTemplate: (sourceKind: "workflow" | "draft", sourceId: string, name?: string): Promise<WorkflowTemplate> =>
    req("POST", "/workflow-templates/import", {
      source_kind: sourceKind, source_id: sourceId, name,
    }).then((r) => json<WorkflowTemplate>(r)),
  previewApplyTemplate: (id: string, body: Record<string, unknown>): Promise<Record<string, unknown>> =>
    req("POST", `/workflow-templates/${id}/preview-apply`, body).then((r) => json(r)),
  applyTemplateToTask: (taskId: string, body: Record<string, unknown>): Promise<Record<string, unknown>> =>
    req("POST", `/tasks/${taskId}/workflow/from-template`, body).then((r) => json(r)),
  setPlanSeed: (taskId: string, templateId: string, version: number): Promise<Record<string, unknown>> =>
    req("POST", `/tasks/${taskId}/plan-seed`, { template_id: templateId, version }).then((r) => json(r)),
  clearPlanSeed: (taskId: string): Promise<void> =>
    req("DELETE", `/tasks/${taskId}/plan-seed`).then((r) => {
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    }),

  // channels (workspaces)
  listChannels: (): Promise<Channel[]> => authFetch(`${BASE}/workspaces`).then((r) => json<Channel[]>(r)),
  createChannel: (name: string): Promise<Channel> =>
    req("POST", "/workspaces", { name }).then((r) => json<Channel>(r)),
  updateChannel: (id: string, patch: {
    name?: string; repo?: string | null; description?: string | null;
  }): Promise<Channel> =>
    req("PATCH", `/workspaces/${id}`, patch).then((r) => json<Channel>(r)),
  listMembers: (wsId: string): Promise<Member[]> =>
    authFetch(`${BASE}/workspaces/${wsId}/members`).then((r) => json<Member[]>(r)),
  addMember: (wsId: string, roleSlug: string, access: string): Promise<Member[]> =>
    req("POST", `/workspaces/${wsId}/members`, { role_slug: roleSlug, access }).then((r) => json<Member[]>(r)),
  updateMember: (wsId: string, roleSlug: string, access: string): Promise<Member[]> =>
    req("PATCH", `/workspaces/${wsId}/members/${roleSlug}`, { access }).then((r) => json<Member[]>(r)),
  removeMember: (wsId: string, roleSlug: string): Promise<void> =>
    req("DELETE", `/workspaces/${wsId}/members/${roleSlug}`).then((r) => {
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    }),

  // tasks（补齐 cancel/reopen）
  cancelTask: (id: string): Promise<Task> => req("POST", `/tasks/${id}/cancel`, {}).then((r) => json<Task>(r)),
  reopenTask: (id: string): Promise<Task> => req("POST", `/tasks/${id}/reopen`, {}).then((r) => json<Task>(r)),
  deleteTask: (id: string, force = false): Promise<void> =>
    req("DELETE", `/tasks/${id}${force ? "?force=true" : ""}`).then((r) => {
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    }),

  // reminders
  listReminders: (roleSlug?: string): Promise<Reminder[]> =>
    authFetch(`${BASE}/reminders${roleSlug ? `?role_slug=${encodeURIComponent(roleSlug)}` : ""}`).then((r) => json(r)),
  addReminder: (body: Record<string, unknown>): Promise<Reminder> =>
    req("POST", "/reminders", body).then((r) => json(r)),
  deleteReminder: (id: string): Promise<void> =>
    req("DELETE", `/reminders/${id}`).then((r) => {
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    }),
  fireReminder: (id: string): Promise<Run> =>
    req("POST", `/reminders/${id}/fire-now`, {}).then((r) => json(r)),

  // lessons
  listLessons: (status?: string): Promise<Lesson[]> =>
    authFetch(`${BASE}/lessons${status ? `?status=${encodeURIComponent(status)}` : ""}`).then((r) => json(r)),
  proposeLesson: (content: string, tags: string[], repo: string): Promise<Lesson> =>
    req("POST", "/lessons/pending", { content, tags, repo }).then((r) => json(r)),
  approveLesson: (id: string): Promise<Lesson> => req("POST", `/lessons/${id}/approve`, {}).then((r) => json(r)),
  rejectLesson: (id: string): Promise<Lesson> => req("POST", `/lessons/${id}/reject`, {}).then((r) => json(r)),
  deleteLesson: (id: string): Promise<void> =>
    req("DELETE", `/lessons/${id}`).then((r) => {
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    }),

  // proposals
  listProposals: (status?: string): Promise<Proposal[]> =>
    authFetch(`${BASE}/proposals${status ? `?status=${encodeURIComponent(status)}` : ""}`).then((r) => json(r)),
  approveProposal: (id: string): Promise<Proposal> =>
    req("POST", `/proposals/${id}/approve`, {}).then((r) => json(r)),
  rejectProposal: (id: string): Promise<Proposal> =>
    req("POST", `/proposals/${id}/reject`, {}).then((r) => json(r)),

  // skills / MCP / presets
  importSkillsBundle: (
    runtime: string, files: File[], paths: string[], overwrite: boolean,
  ): Promise<{ runtime: string; imported: unknown[]; skipped: unknown[] }> => {
    const form = new FormData();
    files.forEach((file) => form.append("files", file, file.name));
    form.append("paths", JSON.stringify(paths));
    form.append("runtime", runtime);
    form.append("overwrite", String(overwrite));
    return authFetch(`${BASE}/tooling/import/skills`, { method: "POST", body: form })
      .then((r) => json(r));
  },
  importMcpBundle: (
    runtime: string, files: File[], paths: string[], overwrite: boolean,
  ): Promise<{ runtime: string; imported: unknown[]; skipped: unknown[] }> => {
    const form = new FormData();
    files.forEach((file) => form.append("files", file, file.name));
    form.append("paths", JSON.stringify(paths));
    form.append("runtime", runtime);
    form.append("overwrite", String(overwrite));
    return authFetch(`${BASE}/tooling/import/mcp`, { method: "POST", body: form })
      .then((r) => json(r));
  },
  listSkills: (): Promise<Skill[]> => authFetch(`${BASE}/skills`).then((r) => json(r)),
  createSkill: (body: Record<string, unknown>): Promise<Skill> =>
    req("POST", "/skills", body).then((r) => json(r)),
  deleteSkill: (id: string): Promise<void> =>
    req("DELETE", `/skills/${id}`).then((r) => {
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    }),
  listMcpServers: (): Promise<McpServer[]> => authFetch(`${BASE}/mcp/servers`).then((r) => json(r)),
  createMcpServer: (body: Record<string, unknown>): Promise<McpServer> =>
    req("POST", "/mcp/servers", body).then((r) => json(r)),
  deleteMcpServer: (id: string): Promise<void> =>
    req("DELETE", `/mcp/servers/${id}`).then((r) => {
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    }),
  testMcpServer: (id: string): Promise<{ ok: boolean | null; tools: { name: string; description?: string }[]; detail?: string }> =>
    req("POST", `/mcp/servers/${id}/test`, {}).then((r) => json(r)),
  listToolPresets: (): Promise<Record<string, { max_tool_tokens: number; allowlist: unknown[] }>> =>
    authFetch(`${BASE}/tools/presets`).then((r) => json(r)),

  // role extras
  getRoleHome: (slug: string): Promise<{ slug: string; home_path: string; files: string[] }> =>
    authFetch(`${BASE}/roles/${slug}/home`).then((r) => json(r)),
  getRoleVersions: (slug: string): Promise<{ version: number; display_name: string; source: string; created_at: string }[]> =>
    authFetch(`${BASE}/roles/${slug}/versions`).then((r) => json(r)),
  rollbackRole: (slug: string, version: number): Promise<Role> =>
    req("POST", `/roles/${slug}/rollback/${version}`, {}).then((r) => json(r)),
  getRoleHomeFile: (slug: string, path: string): Promise<{ path: string; content: string }> =>
    authFetch(`${BASE}/roles/${slug}/home/file?path=${encodeURIComponent(path)}`).then((r) => json(r)),
  getRoleMetrics: (slug: string): Promise<{
    runs: number; tasks_total: number; tasks_done: number;
    input_tokens: number; output_tokens: number; total_tokens: number;
    today_total_tokens: number;
  }> => authFetch(`${BASE}/roles/${slug}/metrics`).then((r) => json(r)),
  sendRoleMessage: (slug: string, message: string): Promise<Run> =>
    req("POST", `/roles/${slug}/message`, { message }).then((r) => json(r)),

  // governance extras
  updateBudget: (body: Record<string, unknown>): Promise<Record<string, unknown>> =>
    req("PUT", "/budget", body).then((r) => json(r)),
};

export interface Channel {
  id: string;
  name: string;
  repo: string | null;
  description: string | null;
  member_count: number;
  task_count: number;
  created_at: string;
}

export interface Member {
  workspace_id: string;
  role_slug: string;
  access: string;
  joined_at: string;
}

export type RunStatus =
  | "created"
  | "projecting"
  | "running"
  | "submitted"
  | "reviewing"
  | "completed"
  | "failed"
  | "abandoned"
  | "suspended";

export interface Run {
  id: string;
  role_slug: string;
  task_id: string | null;
  status: RunStatus;
  native_session_id: string | null;
  log_path: string | null;
  worktree_path: string | null;
  profile_snapshot: string | null;
  prompt: string | null;
  timeout_seconds: number | null;
  failure_class: string | null;
  started_at: string | null;
  ended_at: string | null;
}

export interface AnvilEvent {
  topic: string;
  payload: Record<string, unknown>;
  at: string;
}

/** onboarding 助理（Cindy）是系统默认角色，不参与任务成员选择。 */
export function isOnboardingRole(r: Role): boolean {
  return (r.definition as Record<string, unknown> | undefined)?.kind === "onboarding";
}

export interface InboxDraftMeta {
  draft_kind?: "tasks" | "workflow" | "role_suggestion";
  assessment?: string;
  missing_capabilities?: string[];
  suggested_roles?: { slug: string; display_name?: string; description?: string }[];
  step_count?: number;
  tags?: string[];
  knowledge_repos?: { repo: string; reason?: string | null }[];
  binding_locked?: boolean;
  running_workflow?: boolean;
  running_workflow_id?: string | null;
  running_execution_no?: number | null;
  // gate / decision review metadata
  workflow_id?: string;
  step_id?: string;
  packet_id?: string | null;
  authority?: DecisionAuthority;
  question?: string;
  evidence_count?: number;
  waiting_seconds?: number;
  deadline_at?: string | null;
  recommendation?: PacketRecommendation;
  task_title?: string;
}

export interface InboxItem {
  id: string;
  kind: "review" | "mention" | "draft" | "lesson" | "proposal" | "gate" | "decision" | "action";
  title: string;
  detail: string;
  task_id: string | null;
  created_at: string;
  meta?: InboxDraftMeta;
}

export type DecisionAuthority = "ai" | "human" | "joint";

export interface ReviewEvidence {
  id: string;
  kind: string;
  ref: string;
  label: string;
  digest?: string | null;
  captured_at?: string | null;
  fresh: boolean;
}

export interface ReviewAcceptance {
  criterion: string;
  status: string;
  evidence_refs: string[];
}

export interface ReviewUpstream {
  step_id: string;
  title?: string | null;
  summary?: string | null;
  decision?: string | null;
}

export interface PacketRecommendation {
  decision?: string;
  actor?: string;
  feedback?: string | null;
  confidence?: number | null;
  triggers?: string[];
  escalated?: boolean;
  at?: string;
}

export interface ReviewPacket {
  id: string;
  task_id: string;
  workflow_id: string;
  execution_no: number;
  step_id: string;
  kind: "gate" | "decision";
  revision: number;
  status: "pending_human" | "pending_ai" | "decided" | "superseded";
  authority: DecisionAuthority;
  question: string;
  options: { id?: string; label?: string; decision: string; requires_note?: boolean }[];
  acceptance: ReviewAcceptance[];
  evidence: ReviewEvidence[];
  upstream: ReviewUpstream[];
  risks: string[];
  recommendation: PacketRecommendation;
  evidence_digest?: string | null;
  sla_seconds?: number | null;
  deadline_at?: string | null;
  created_at: string;
}

export interface DecisionRecord {
  id: string;
  packet_id: string;
  step_id: string;
  authority: DecisionAuthority;
  decider_type: string;
  decider: string;
  decision: string;
  feedback?: string | null;
  reasons: string[];
  rework_targets: string[];
  override_reason?: string | null;
  latency_seconds?: number | null;
  created_at: string;
}

export interface StepPacketResponse {
  step: WorkflowStep | null;
  packet: ReviewPacket | null;
  decisions: DecisionRecord[];
  can_decide: {
    human: boolean;
    role: boolean;
    authority: DecisionAuthority;
    stale_evidence: boolean;
  };
}

export interface WorkflowStep {
  workflow_id: string;
  step_id: string;
  title: string;
  role_slug: string;
  instructions: string | null;
  depends_on: string[];
  acceptance: string[];
  gate: boolean;
  reviewer: string | null;
  contract: string | null;
  kind?: "task" | "decision" | "plan_slices";
  on_reject?: string[];
  max_rework?: number;
  rework_count?: number;
  feedback?: string | null;
  source_step_id?: string | null;
  slice_key?: string | null;
  plan_json?: string | null;
  decision_authority?: DecisionAuthority | "";
  risk_level?: "normal" | "high" | "critical";
  reversible?: boolean;
  escalate?: string[];
  decision_question?: string | null;
  decision_options?: { id?: string; label?: string; decision: string; requires_note?: boolean }[];
  evidence_required?: boolean;
  evidence?: ReviewEvidence[];
  packet_id?: string | null;
  submitted_at?: string | null;
  scope_paths?: string[];
  lease_id?: string | null;
  fence_token?: number;
  lease_holder?: string | null;
  lease_expires_at?: string | null;
  lease_fence?: number | null;
  failure_reason: string | null;
  status: "pending" | "running" | "submitted" | "approved" | "rejected" | "failed" | "skipped";
  run_id: string | null;
  summary: string | null;
  started_at: string | null;
  ended_at: string | null;
  updated_at: string;
}

export interface Workflow {
  id: string;
  task_id: string;
  status: "running" | "done" | "failed" | "terminated";
  definition: {
    feedback_rules?: string[];
    verification?: { cell: string; method: string; verifier_role?: string }[];
  };
  steps: WorkflowStep[];
  created_at: string;
  updated_at: string;
  execution_no?: number;
  previous_workflow_id?: string | null;
  termination_reason?: string | null;
  terminated_at?: string | null;
  weight?: WorkflowWeight;
  weight_source?: string;
  weight_reason?: string | null;
  definition_hash?: string | null;
  finished_at?: string | null;
}

export type WorkflowWeight = "light" | "standard" | "strict";

export interface WorkflowMetrics {
  workflow_id: string;
  task_id: string;
  execution_no: number;
  status: Workflow["status"];
  weight: WorkflowWeight;
  weight_source: string;
  created_at: string | null;
  finished_at: string | null;
  duration_seconds: number;
  steps: {
    total: number;
    done: number;
    running: number;
    submitted: number;
    failed: number;
    max_depth: number;
    parallel_roles: number;
    generated_slices: number;
    by_status: Record<string, number>;
  };
  gates: { total: number; pending: number };
  decisions: {
    total: number;
    recorded?: number;
    legacy?: number;
    human: number;
    human_reviews?: number;
    human_interventions?: number;
    rejected: number;
    pending_human: number;
    rework_rate: number;
  };
  waiting: { human_seconds: number; pending_human_seconds: number };
  evidence: { packets: number };
  tokens: {
    input_tokens?: number;
    output_tokens?: number;
    cache_read_tokens?: number;
    cache_creation_tokens?: number;
    total_tokens?: number;
    records?: number;
    task_input_tokens?: number;
    task_output_tokens?: number;
    task_cache_read_tokens?: number;
    task_cache_creation_tokens?: number;
    task_total_tokens?: number;
    task_records?: number;
  };
}

export interface WorkflowGraphNode {
  step_id: string;
  title: string;
  role_slug: string;
  depends_on: string[];
  kind: string;
  gate: boolean;
  decision_authority: string;
  status?: string;
  run_id?: string | null;
  slice_key?: string | null;
  source_step_id?: string | null;
  rework_count?: number;
  origin: string;
}

export interface WorkflowGraphResponse {
  mode: "planned" | "actual" | "diff";
  workflow_id: string;
  nodes?: WorkflowGraphNode[];
  added?: WorkflowGraphNode[];
  missing?: WorkflowGraphNode[];
  changed?: { step_id: string; field: string; planned: unknown; actual: unknown }[];
}

export interface WorkflowDossier {
  workflow: Record<string, unknown>;
  metrics: WorkflowMetrics;
  steps: WorkflowStep[];
  packets: ReviewPacket[];
  planned: WorkflowGraphResponse;
  actual: WorkflowGraphResponse;
  diff: WorkflowGraphResponse;
  event_counts: Record<string, number>;
  digest: string;
  generated_at: string;
}

export interface LeaseRecord {
  id: string;
  resource_type: "step" | "slice" | "path" | "task";
  resource_key: string;
  holder_type: string;
  holder: string | null;
  run_id: string | null;
  fence: number;
  status: "active" | "released" | "expired";
  acquired_at: string;
  renewed_at: string | null;
  expires_at: string | null;
  meta_json?: string;
}

export interface TaskLeaseStep {
  workflow_id: string;
  step_id: string;
  status: string;
  lease_id: string | null;
  fence_token: number;
  holder: string | null;
  expires_at: string | null;
  lease_status: string | null;
}

export interface TaskLeases {
  paths: LeaseRecord[];
  steps: TaskLeaseStep[];
}

export interface TaskWeightResult {
  task_id: string;
  weight: WorkflowWeight | null;
  requires_restart: boolean;
  running_workflow_id: string | null;
  running_execution_no: number | null;
  running_weight: WorkflowWeight | null;
}

export interface WorkflowHistoryItem {
  id: string;
  task_id: string;
  status: Workflow["status"];
  created_by: string | null;
  created_at: string;
  updated_at: string;
  termination_reason: string | null;
  terminated_at: string | null;
  previous_workflow_id: string | null;
  execution_no: number;
  template_id: string | null;
  template_version: number | null;
  step_count: number;
  approved_count: number | null;
  weight?: WorkflowWeight;
  weight_source?: string;
  weight_reason?: string | null;
  definition_hash?: string | null;
  finished_at?: string | null;
  metrics?: WorkflowMetrics;
}

export interface TemplateRoleSlot {
  key: string;
  label: string;
  /** 通用英文 role 名，例如 tester；key 应为 role_<role>。 */
  role?: string;
  /** 可选：预先创建的角色 slug；套用时优先绑定。 */
  bound_role?: string | null;
  description?: string;
  /** 兼容旧模板：不再作为新模板的主要配置方式。 */
  suggested_roles?: string[];
}

export interface TemplateStep {
  id: string;
  title: string;
  role_slot: string;
  kind?: "task" | "decision" | "plan_slices";
  depends_on?: string[];
  instructions?: string;
  acceptance?: string[];
  gate?: boolean;
  reviewer_slot?: string | null;
  contract?: string | null;
  on_reject?: string[];
  max_rework?: number;
}

export interface TemplateLoopPhase {
  role_slot: string;
  title?: string;
  instructions?: string;
  acceptance?: string[];
  max_rework?: number;
}

export interface TemplateLoop {
  from: string;
  scope?: TemplateLoopPhase;
  execute?: TemplateLoopPhase;
  verify?: TemplateLoopPhase;
  fix?: TemplateLoopPhase;
  verify2?: TemplateLoopPhase;
  decide?: TemplateLoopPhase;
}

export interface WorkflowTemplateDefinition {
  schema_version?: number;
  role_slots: TemplateRoleSlot[];
  steps: TemplateStep[];
  loop?: TemplateLoop | null;
  feedback_rules?: string[];
  verification?: { cell: string; method: string; verifier_slot?: string }[];
}

export interface TemplateValidationIssue {
  severity: "error" | "warning";
  code: string;
  message: string;
  node_id?: string | null;
  slot_key?: string | null;
}

export interface WorkflowTemplateVersion {
  template_id: string;
  version: number;
  state: "draft" | "published";
  name: string;
  description: string;
  tags: string[];
  definition: WorkflowTemplateDefinition;
  origin_definition?: WorkflowTemplateDefinition | null;
  layout: Record<string, { x: number; y: number }>;
  validation: TemplateValidationIssue[];
  revision: number;
  based_on_version?: number | null;
  created_at: string;
  published_at?: string | null;
}

export interface WorkflowTemplate {
  id: string;
  name: string;
  description: string;
  tags: string[];
  use_mode: "seed" | "manual" | "both";
  status: "active" | "archived";
  source_kind: string;
  source_ref: Record<string, unknown>;
  draft_version?: number | null;
  published_version?: number | null;
  created_by?: string | null;
  created_at: string;
  updated_at: string;
  has_draft?: boolean;
  has_published?: boolean;
  usage_count?: number;
  draft?: WorkflowTemplateVersion | null;
  published?: WorkflowTemplateVersion | null;
  versions?: { version: number; state: string; revision: number; created_at: string; published_at?: string | null }[];
  warnings?: string[];
}

export interface BoardDraft {
  id: string;
  goal: string;
  status: "pending" | "approved" | "rejected";
  kind?: "tasks" | "workflow" | "role_suggestion";
  created_by: string;
  source_task: string | null;
  tree: {
    leader?: string;
    tags?: string[];
    knowledge_repos?: (string | { repo: string; reason?: string })[];
    feedback_rules?: string[];
    verification?: { cell: string; method: string; verifier_role?: string }[];
    assessment?: string;
    missing_capabilities?: string[];
    suggested_roles?: {
      slug: string;
      display_name?: string;
      description?: string;
      runtime?: { id?: string };
      capabilities?: Record<string, number>;
      responsibilities?: string[];
    }[];
    steps?: {
      id?: string;
      title?: string;
      role?: string;
      kind?: string;
      depends_on?: string[];
      acceptance?: string[];
      gate?: boolean;
      reviewer?: string;
      contract?: string;
    }[];
    loop?: Record<string, unknown>;
    children?: {
      id?: string;
      title?: string;
      type?: string;
      role?: string;
      depends_on?: string[];
      acceptance?: string[];
      scope_paths?: string[];
      tags?: string[];
      knowledge_repos?: (string | { repo: string; reason?: string })[];
    }[];
  };
  created_at: string;
}

export interface TimelineEvent {
  key: string;
  ts: string;
  kind: string;
  role: string | null;
  task_id: string | null;
  run_id: string | null;
  seq?: number | null;
  workflow_id?: string | null;
  step_id?: string | null;
  slice_key?: string | null;
  execution_no?: number | null;
  payload: Record<string, unknown>;
}

export interface Reminder {
  id: string;
  role_slug: string | null;
  kind: "cron" | "at" | "when";
  cron_expr: string | null;
  fire_at: string | null;
  when_condition: string | null;
  prompt: string | null;
  status: string;
  next_fire_at: string | null;
  created_at: string;
}

export interface Lesson {
  id: string;
  content: string;
  tags: string[];
  source_task: string | null;
  source_role: string | null;
  repo: string;
  status: "pending" | "approved" | "rejected";
  created_at: string;
  approved_at: string | null;
}

export interface Proposal {
  id: string;
  role_slug: string;
  field: string;
  proposed: unknown;
  rationale: string | null;
  risk: string | null;
  status: "pending" | "approved" | "rejected";
  created_at: string;
  decided_at: string | null;
}

export interface Skill {
  id: string;
  name: string;
  description: string | null;
  runtime: string | null;
  updated_at: string;
  content?: string;
}

export interface McpServer {
  id: string;
  name: string;
  description?: string | null;
  transport: string;
  command: string | null;
  args: string[];
  env: Record<string, string>;
  url: string | null;
  tools: { name: string; description?: string }[];
  enabled: boolean;
  updated_at: string;
}

/** 订阅 SSE 事件流（§11.2），返回取消订阅函数。 */
// 全应用共享一条 SSE 连接：HTTP/1.1 每域名最多 6 个连接，
// 每个页面各开一条会把连接池占满，导致后续请求排队、页面像卡死。
type EventListener = (ev: AnvilEvent) => void;
let sharedES: EventSource | null = null;
const listeners = new Set<EventListener>();

function ensureEventSource(): EventSource {
  if (sharedES) return sharedES;
  const es = new EventSource(`${BASE}/events?token=${encodeURIComponent(SESSION)}`);
  es.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data) as AnvilEvent;
      listeners.forEach((fn) => fn(ev));
    } catch {
      /* 忽略无法解析的心跳/其他行 */
    }
  };
  sharedES = es;
  return es;
}

export function subscribeEvents(onEvent: EventListener): () => void {
  ensureEventSource();
  listeners.add(onEvent);
  return () => {
    listeners.delete(onEvent);
    if (listeners.size === 0 && sharedES) {
      sharedES.close();
      sharedES = null;
    }
  };
}
