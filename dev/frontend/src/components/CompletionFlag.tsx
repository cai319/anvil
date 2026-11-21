import type { KnowledgeCompletion } from "../api/client";
import { completionLabel } from "../knowledgeCard";

/** 智能补全进度小标记：统一放在标题末尾。 */
export function CompletionFlag({
  completion,
  className = "",
}: {
  completion?: KnowledgeCompletion | null;
  className?: string;
}) {
  if (!completion) return null;
  const tone = completion.state === "failed"
    ? "is-error"
    : completion.state === "completed" ? "is-ready" : "is-completion";
  return (
    <span className={`inbox-gate-flag ${tone} ${className}`.trim()}>
      {completionLabel(completion)}
    </span>
  );
}
