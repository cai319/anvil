import type { WorkflowTemplateDefinition } from "../api/client";

export type TemplateDiffLine = {
  kind: "added" | "removed" | "changed";
  text: string;
  nodeId?: string;
};

const short = (value: unknown, max = 42): string => {
  if (value == null) return "—";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  const chars = Array.from(text);
  return chars.length > max ? `${chars.slice(0, max).join("")}…` : text;
};

function compareField(lines: TemplateDiffLine[], nodeId: string, field: string,
                      before: unknown, after: unknown, label = field) {
  if (JSON.stringify(before) === JSON.stringify(after)) return;
  lines.push({
    kind: "changed",
    nodeId,
    text: `${nodeId} · ${label}: ${short(before)} → ${short(after)}`,
  });
}

/** 粗略的语义 diff：以步骤/槽位/loop 为主要单位，供人工审阅 AI 原稿与修订。 */
export function diffTemplate(before: WorkflowTemplateDefinition | null | undefined,
                             after: WorkflowTemplateDefinition | null | undefined): TemplateDiffLine[] {
  const lines: TemplateDiffLine[] = [];
  if (!before || !after) {
    return before ? [{ kind: "removed", text: "整个模板被移除" }]
      : after ? [{ kind: "added", text: "整个模板被新增" }] : [];
  }
  const beforeSlots = new Map((before.role_slots || []).map((slot) => [slot.key, slot]));
  const afterSlots = new Map((after.role_slots || []).map((slot) => [slot.key, slot]));
  for (const [key, slot] of afterSlots) {
    const old = beforeSlots.get(key);
    if (!old) lines.push({ kind: "added", text: `角色槽位 + ${slot.label} (${key})` });
    else compareField(lines, key, "label", old.label, slot.label, `角色槽位 ${key} 名称`);
  }
  for (const [key, slot] of beforeSlots) {
    if (!afterSlots.has(key)) lines.push({ kind: "removed", text: `角色槽位 − ${slot.label} (${key})` });
  }
  const beforeSteps = new Map((before.steps || []).map((step) => [step.id, step]));
  const afterSteps = new Map((after.steps || []).map((step) => [step.id, step]));
  for (const [id, step] of afterSteps) {
    const old = beforeSteps.get(id);
    if (!old) {
      lines.push({ kind: "added", nodeId: id, text: `节点 + ${id} ${step.title}` });
      continue;
    }
    for (const field of ["title", "role_slot", "kind", "gate", "reviewer_slot",
      "instructions", "acceptance", "contract", "depends_on", "on_reject", "max_rework"] as const) {
      compareField(lines, id, field, old[field], step[field]);
    }
  }
  for (const [id, step] of beforeSteps) {
    if (!afterSteps.has(id)) lines.push({ kind: "removed", nodeId: id, text: `节点 − ${id} ${step.title}` });
  }
  const beforeLoop = before.loop || null;
  const afterLoop = after.loop || null;
  if (Boolean(beforeLoop) !== Boolean(afterLoop)) {
    lines.push({
      kind: afterLoop ? "added" : "removed",
      text: afterLoop ? "新增 loop 模板" : "移除 loop 模板",
    });
  } else if (beforeLoop && afterLoop) {
    if (beforeLoop.from !== afterLoop.from) {
      lines.push({ kind: "changed", nodeId: afterLoop.from, text: `loop.from: ${beforeLoop.from} → ${afterLoop.from}` });
    }
    for (const phase of ["scope", "execute", "verify", "fix", "verify2", "decide"] as const) {
      const old = beforeLoop[phase];
      const current = afterLoop[phase];
      if (!old && current) lines.push({ kind: "added", text: `loop.${phase} + ${current.title || current.role_slot}` });
      else if (old && !current) lines.push({ kind: "removed", text: `loop.${phase} − ${old.title || old.role_slot}` });
      else if (old && current) {
        compareField(lines, afterLoop.from, phase, old.role_slot, current.role_slot, `loop.${phase}.role_slot`);
        compareField(lines, afterLoop.from, phase, old.title, current.title, `loop.${phase}.title`);
        compareField(lines, afterLoop.from, phase, old.instructions, current.instructions, `loop.${phase}.instructions`);
        compareField(lines, afterLoop.from, phase, old.max_rework, current.max_rework, `loop.${phase}.max_rework`);
      }
    }
  }
  compareField(lines, "template", "feedback_rules", before.feedback_rules, after.feedback_rules);
  compareField(lines, "template", "verification", before.verification, after.verification);
  return lines;
}
