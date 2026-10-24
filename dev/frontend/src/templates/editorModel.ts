import type {
  TemplateLoop,
  TemplateRoleSlot,
  TemplateStep,
  WorkflowTemplateDefinition,
} from "../api/client";

export type TemplateLayout = Record<string, { x: number; y: number }>;

export const ROLE_NAME_RE = /^[a-z][a-z0-9_-]{1,31}$/;

export function roleNameOf(slot: TemplateRoleSlot): string {
  if (slot.role) return slot.role;
  if (slot.key.startsWith("role_")) return slot.key.slice(5);
  return slot.suggested_roles?.[0] || slot.key;
}

export function meepleKey(role: string): string {
  return `role_${role}`;
}

export const DEFAULT_SLOTS: TemplateRoleSlot[] = [
  { key: "role_executor", label: "执行者", role: "executor", suggested_roles: ["developer"] },
  { key: "role_verifier", label: "验证者", role: "verifier", suggested_roles: ["tester"] },
  { key: "role_decider", label: "决策者", role: "decider", suggested_roles: ["pm"] },
];

export function blankDefinition(): WorkflowTemplateDefinition {
  return { schema_version: 1, role_slots: [], steps: [] };
}

export function cloneDefinition(definition: WorkflowTemplateDefinition): WorkflowTemplateDefinition {
  return JSON.parse(JSON.stringify(definition)) as WorkflowTemplateDefinition;
}

export function stepMap(definition: WorkflowTemplateDefinition): Map<string, TemplateStep> {
  return new Map((definition.steps || []).map((step) => [step.id, step]));
}

export function autoLayout(definition: WorkflowTemplateDefinition): TemplateLayout {
  const steps = definition.steps || [];
  const byId = new Map(steps.map((step) => [step.id, step]));
  const layers = new Map<string, number>();
  const visiting = new Set<string>();
  const layerOf = (id: string): number => {
    if (layers.has(id)) return layers.get(id)!;
    if (visiting.has(id)) return 0;
    visiting.add(id);
    const deps = (byId.get(id)?.depends_on || []).filter((dep) => byId.has(dep));
    const value = deps.length ? Math.max(...deps.map((dep) => layerOf(dep) + 1)) : 0;
    visiting.delete(id);
    layers.set(id, value);
    return value;
  };
  steps.forEach((step) => layerOf(step.id));
  const columns = new Map<number, string[]>();
  for (const step of steps) {
    const layer = layers.get(step.id) ?? 0;
    const col = columns.get(layer) ?? [];
    col.push(step.id);
    columns.set(layer, col);
  }
  const layout: TemplateLayout = {};
  [...columns.entries()].sort(([a], [b]) => a - b).forEach(([layer, ids]) => {
    ids.forEach((id, row) => {
      layout[id] = { x: 48 + layer * 320, y: 48 + row * 152 };
    });
  });
  return layout;
}

export function ensureLayout(definition: WorkflowTemplateDefinition,
                             layout: TemplateLayout | null | undefined): TemplateLayout {
  const fallback = autoLayout(definition);
  const out: TemplateLayout = { ...fallback };
  Object.entries(layout || {}).forEach(([id, point]) => {
    if (byId(definition, id) && Number.isFinite(point.x) && Number.isFinite(point.y)) {
      out[id] = { x: point.x, y: point.y };
    }
  });
  return out;
}

function byId(definition: WorkflowTemplateDefinition, id: string) {
  return (definition.steps || []).find((step) => step.id === id);
}

export function usedSlots(definition: WorkflowTemplateDefinition): string[] {
  const out = new Set<string>();
  for (const step of definition.steps || []) {
    if (step.role_slot) out.add(step.role_slot);
    if (step.reviewer_slot) out.add(step.reviewer_slot);
  }
  const loop = definition.loop;
  if (loop) {
    for (const key of LOOP_PHASE_ORDER) {
      const spec = loop[key];
      if (spec?.role_slot) out.add(spec.role_slot);
    }
  }
  return [...out];
}

export const LOOP_PHASE_ORDER = ["scope", "execute", "verify", "fix", "verify2", "decide"] as const;

export function addStep(definition: WorkflowTemplateDefinition,
                        kind: TemplateStep["kind"] = "task"): WorkflowTemplateDefinition {
  const next = cloneDefinition(definition);
  if (!next.role_slots.length) {
    next.role_slots = [{ key: "role_executor", label: "执行者", role: "executor",
      suggested_roles: ["developer"] }];
  }
  const prefix = kind === "decision" ? "D" : kind === "plan_slices" ? "P" : "S";
  let n = 1;
  while (next.steps.some((step) => step.id === `${prefix}${n}`)) n += 1;
  const id = `${prefix}${n}`;
  next.steps.push({
    id,
    title: kind === "decision" ? "新增决策" : kind === "plan_slices" ? "新增切片计划" : "新增步骤",
    role_slot: next.role_slots[0]?.key || "role_executor",
    kind,
    depends_on: [],
    acceptance: [],
  } as TemplateStep);
  return next;
}

export function addLoop(definition: WorkflowTemplateDefinition): WorkflowTemplateDefinition {
  if (definition.steps.some((step) => step.kind === "plan_slices")) {
    throw new Error("模板只能有一个切片计划节点");
  }
  let next = cloneDefinition(definition);
  // Loop 需要可区分的执行/验证/决策 Meeple；缺什么补什么，避免默认生成非法模板。
  next.role_slots = [...next.role_slots];
  for (const slot of DEFAULT_SLOTS) {
    if (!next.role_slots.some((item) => item.key === slot.key)) {
      next.role_slots.push({ ...slot });
    }
  }
  const slots = next.role_slots;
  const executor = slots.find((slot) => roleNameOf(slot) === "executor")!.key;
  const verifier = slots.find((slot) => roleNameOf(slot) === "verifier")!.key;
  const decider = slots.find((slot) => roleNameOf(slot) === "decider")!.key;
  next = addStep(next, "plan_slices");
  const plan = next.steps[next.steps.length - 1];
  plan.title = "项目盘点与切片计划";
  plan.role_slot = decider;
  next.loop = {
    from: plan.id,
    scope: { role_slot: decider, title: "切片范围确认" },
    execute: { role_slot: executor, title: "切片执行" },
    verify: { role_slot: verifier, title: "独立验证" },
    fix: { role_slot: executor, title: "修复与补证" },
    verify2: { role_slot: verifier, title: "复验" },
    decide: { role_slot: decider, title: "切片决策", max_rework: 2 },
  };
  return next;
}

export function removeStep(definition: WorkflowTemplateDefinition,
                           id: string): WorkflowTemplateDefinition {
  const next = cloneDefinition(definition);
  next.steps = next.steps.filter((step) => step.id !== id);
  for (const step of next.steps) {
    step.depends_on = (step.depends_on || []).filter((dep) => dep !== id);
    step.on_reject = (step.on_reject || []).filter((dep) => dep !== id);
  }
  if (next.loop?.from === id) next.loop = undefined;
  return next;
}

export function connectStep(definition: WorkflowTemplateDefinition,
                            source: string, target: string): WorkflowTemplateDefinition {
  if (source === target) throw new Error("不能连接自身");
  const next = cloneDefinition(definition);
  const from = byId(next, source);
  const to = byId(next, target);
  if (!from || !to) throw new Error("节点不存在");
  const deps = new Set(to.depends_on || []);
  if (deps.has(source)) return next;
  deps.add(source);
  to.depends_on = [...deps];
  if (hasCycle(next)) throw new Error("连接会形成环");
  return next;
}

export function disconnectStep(definition: WorkflowTemplateDefinition,
                               source: string, target: string): WorkflowTemplateDefinition {
  const next = cloneDefinition(definition);
  const to = byId(next, target);
  if (to) to.depends_on = (to.depends_on || []).filter((dep) => dep !== source);
  return next;
}

/** 所有直接/间接上游节点。决策节点的打回目标必须是该集合中的非审核门节点。 */
export function ancestorsOf(definition: WorkflowTemplateDefinition, id: string): Set<string> {
  const byId = stepMap(definition);
  const seen = new Set<string>();
  const stack = [...(byId.get(id)?.depends_on || [])];
  while (stack.length) {
    const current = stack.pop()!;
    if (seen.has(current) || !byId.has(current)) continue;
    seen.add(current);
    stack.push(...(byId.get(current)?.depends_on || []));
  }
  return seen;
}

export function hasCycle(definition: WorkflowTemplateDefinition): boolean {
  const state = new Map<string, number>();
  const visit = (id: string): boolean => {
    if (state.get(id) === 1) return true;
    if (state.get(id) === 2) return false;
    state.set(id, 1);
    for (const dep of byId(definition, id)?.depends_on || []) {
      if (byId(definition, dep) && visit(dep)) return true;
    }
    state.set(id, 2);
    return false;
  };
  return (definition.steps || []).some((step) => visit(step.id));
}

export function defaultLoopFor(stepId: string, slots: TemplateRoleSlot[]): TemplateLoop {
  const executor = slots.find((slot) => roleNameOf(slot) === "executor")?.key
    || slots[0]?.key || "role_executor";
  const verifier = slots.find((slot) => roleNameOf(slot) === "verifier")?.key || executor;
  const decider = slots.find((slot) => roleNameOf(slot) === "decider")?.key || executor;
  return {
    from: stepId,
    scope: { role_slot: decider, title: "切片范围确认" },
    execute: { role_slot: executor, title: "切片执行" },
    verify: { role_slot: verifier, title: "独立验证" },
    fix: { role_slot: executor, title: "修复与补证" },
    verify2: { role_slot: verifier, title: "复验" },
    decide: { role_slot: decider, title: "切片决策", max_rework: 2 },
  };
}

/** 把运行时/草案里的具体角色 definition 转成可编辑的 role-slot 模板。 */
export function definitionFromConcrete(concrete: Record<string, unknown>): {
  definition: WorkflowTemplateDefinition;
  roleMapping: Record<string, string>;
} {
  const rawSteps = (concrete.steps || concrete.children || []) as Record<string, unknown>[];
  const rawLoop = concrete.loop as Record<string, unknown> | undefined;
  const roleToSlot = new Map<string, string>();
  const keyToRole = new Map<string, string>();
  const slots: TemplateRoleSlot[] = [];
  const roleOf = (spec: Record<string, unknown> | undefined) =>
    String(spec?.role || spec?.role_slug || "").trim();
  const addSlot = (role: string, _preferred: string): string => {
    if (!role) return "";
    if (roleToSlot.has(role)) return roleToSlot.get(role)!;
    const base = `role_${role.replace(/[^A-Za-z0-9_-]/g, "_")}`;
    let key = base;
    let n = 2;
    while (keyToRole.has(key) && keyToRole.get(key) !== role) {
      key = `${base}_${n}`;
      n += 1;
    }
    keyToRole.set(key, role);
    roleToSlot.set(role, key);
    slots.push({ key, label: role, role, suggested_roles: [role] });
    return key;
  };
  const loopSlots = new Map<string, string>();
  if (rawLoop) {
    const phase = (name: string) => rawLoop[name] as Record<string, unknown> | undefined;
    const executeRole = roleOf(phase("execute"));
    const verifyRole = roleOf(phase("verify"));
    const decideRole = roleOf(phase("decide"));
    const fixRole = roleOf(phase("fix")) || executeRole;
    const verify2Role = roleOf(phase("verify2")) || verifyRole;
    const scopeRole = roleOf(phase("scope")) || decideRole;
    const preferred: Record<string, string> = {
      execute: "executor",
      verify: "verifier",
      fix: fixRole === executeRole ? "executor" : "fixer",
      verify2: verify2Role === verifyRole ? "verifier" : "verifier2",
      decide: "decider",
      scope: scopeRole === decideRole ? "decider" : "scope_owner",
    };
    for (const name of ["scope", "execute", "verify", "fix", "verify2", "decide"]) {
      const role = roleOf(phase(name));
      if (role) loopSlots.set(name, addSlot(role, preferred[name]));
    }
  }
  const steps: TemplateStep[] = rawSteps.map((raw) => {
    const role = roleOf(raw);
    const reviewer = String(raw.reviewer || "").trim();
    const step: TemplateStep = {
      id: String(raw.id || ""),
      title: String(raw.title || ""),
      role_slot: role ? addSlot(role, `role_${role.replace(/[^A-Za-z0-9_-]/g, "_")}`) : "",
      kind: (raw.kind as TemplateStep["kind"]) || "task",
      depends_on: (raw.depends_on as string[]) || [],
      instructions: raw.instructions ? String(raw.instructions) : undefined,
      acceptance: (raw.acceptance as string[]) || [],
      gate: Boolean(raw.gate),
      contract: raw.contract ? String(raw.contract) : null,
      on_reject: (raw.on_reject as string[]) || [],
      max_rework: raw.max_rework == null ? undefined : Number(raw.max_rework),
    };
    if (reviewer) step.reviewer_slot = addSlot(reviewer, `reviewer_${reviewer}`);
    return step;
  });
  const loop = rawLoop ? {
    from: String(rawLoop.from || ""),
    ...Object.fromEntries(
      ["scope", "execute", "verify", "fix", "verify2", "decide"]
        .flatMap((name) => {
          const spec = rawLoop[name] as Record<string, unknown> | undefined;
          if (!spec) return [];
          return [[name, {
            role_slot: loopSlots.get(name) || "",
            title: spec.title ? String(spec.title) : undefined,
            instructions: spec.instructions ? String(spec.instructions) : undefined,
            acceptance: (spec.acceptance as string[]) || [],
            max_rework: spec.max_rework == null ? undefined : Number(spec.max_rework),
          }]];
        }),
    ),
  } as NonNullable<WorkflowTemplateDefinition["loop"]> : undefined;
  const verification = (concrete.verification as Record<string, unknown>[] | undefined)?.map((raw) => {
    const role = String(raw.verifier_role || "");
    return {
      cell: String(raw.cell || ""),
      method: String(raw.method || ""),
      ...(role ? { verifier_slot: addSlot(role, `verifier_${role}`) } : {}),
    };
  }) || [];
  const definition: WorkflowTemplateDefinition = {
    schema_version: 1,
    role_slots: slots,
    steps,
    feedback_rules: (concrete.feedback_rules as string[]) || [],
    verification,
    ...(loop ? { loop } : {}),
  };
  return { definition, roleMapping: Object.fromEntries(keyToRole) };
}

export function compileTemplate(definition: WorkflowTemplateDefinition,
                                roleMapping: Record<string, string>): Record<string, unknown> {
  const missing = usedSlots(definition).filter((key) => !roleMapping[key]);
  if (missing.length) throw new Error(`Meeple未映射：${missing.join("、")}`);
  const compiled = JSON.parse(JSON.stringify(definition)) as Record<string, unknown>;
  delete compiled.role_slots;
  delete compiled.schema_version;
  const steps = (compiled.steps as Record<string, unknown>[]) || [];
  for (const step of steps) {
    const roleSlot = String(step.role_slot || "");
    delete step.role_slot;
    if (roleSlot) step.role = roleMapping[roleSlot];
    const reviewerSlot = String(step.reviewer_slot || "");
    delete step.reviewer_slot;
    if (reviewerSlot) step.reviewer = roleMapping[reviewerSlot];
  }
  const loop = compiled.loop as Record<string, Record<string, unknown>> | undefined;
  if (loop) {
    for (const name of ["scope", "execute", "verify", "fix", "verify2", "decide"]) {
      const spec = loop[name];
      if (!spec) continue;
      const slot = String(spec.role_slot || "");
      delete spec.role_slot;
      if (slot) spec.role = roleMapping[slot];
    }
  }
  for (const raw of (compiled.verification as Record<string, unknown>[]) || []) {
    const slot = String(raw.verifier_slot || "");
    delete raw.verifier_slot;
    if (slot) raw.verifier_role = roleMapping[slot];
  }
  const substitute = (value: unknown): unknown => {
    if (typeof value === "string") {
      return value.replace(/\{\{slot:([A-Za-z0-9_-]+)\}\}/g,
        (full, key: string) => roleMapping[key] || full);
    }
    if (Array.isArray(value)) return value.map(substitute);
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, substitute(item)]));
    }
    return value;
  };
  return substitute(compiled) as Record<string, unknown>;
}

/** 修改 Meeple 的 role 名时同步所有引用，避免步骤/loop/token 指向旧 key。 */
export function renameMeepleKey(definition: WorkflowTemplateDefinition,
                                oldKey: string, newKey: string): WorkflowTemplateDefinition {
  const next = cloneDefinition(definition);
  const slot = next.role_slots.find((item) => item.key === oldKey);
  if (slot) slot.key = newKey;
  for (const step of next.steps || []) {
    if (step.role_slot === oldKey) step.role_slot = newKey;
    if (step.reviewer_slot === oldKey) step.reviewer_slot = newKey;
  }
  if (next.loop) {
    for (const phase of ["scope", "execute", "verify", "fix", "verify2", "decide"] as const) {
      const spec = next.loop[phase];
      if (spec?.role_slot === oldKey) spec.role_slot = newKey;
    }
  }
  for (const item of next.verification || []) {
    if (item.verifier_slot === oldKey) item.verifier_slot = newKey;
  }
  const replace = (value: unknown): unknown => {
    if (typeof value === "string") {
      return value.replaceAll(`{{slot:${oldKey}}}`, `{{slot:${newKey}}}`);
    }
    if (Array.isArray(value)) return value.map(replace);
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, replace(v)]));
    }
    return value;
  };
  return replace(next) as WorkflowTemplateDefinition;
}
