import { useEffect, useState } from "react";
import type { Lesson, Role } from "../../api/client";
import { RoleAvatar } from "../../components/RoleAvatar";
import {
  completionActionLabel,
  completionBlocked,
  completionLabel,
} from "../../knowledgeCard";
import { formatDateTime } from "../../time";
import { KnowledgeDetailModal } from "./KnowledgeDetail";

const LESSON_STATUS_LABEL: Record<Lesson["status"], string> = {
  pending: "待审批",
  approved: "已批准",
  rejected: "已拒绝",
};

export function canonicalLessonFields(fields?: Record<string, unknown> | null) {
  const out = { ...(fields || {}) };
  if (!String(out.counterexamples ?? "").trim() && out.exclusions) {
    out.counterexamples = out.exclusions;
  }
  delete out.exclusions;
  return out;
}

/** 兼容旧后端：早期 lesson 只有 content，没有 title/tldr/fields。 */
export function normalizeLesson(lesson: Lesson): Lesson {
  return {
    ...lesson,
    title: lesson.title || (lesson.content || "").split("\n")[0] || "",
    tldr: lesson.tldr || "",
    fields: canonicalLessonFields(lesson.fields),
    tags: lesson.tags || [],
  };
}

export function LessonTags({ tags }: { tags: string[] }) {
  if (tags.length === 0) return <span className="muted">无标签</span>;
  return (
    <span className="lesson-tags">
      {tags.map((tag) => (
        <span className="inbox-meta-chip" key={tag}>#{tag}</span>
      ))}
    </span>
  );
}

/** 共享经验的提出者：角色头像；旧数据没有 source_role 时用中性占位头像。 */
export function LessonSource({ lesson, roles, showName = false }: {
  lesson: Lesson;
  roles: Role[];
  showName?: boolean;
}) {
  const slug = lesson.source_role;
  const role = slug ? roles.find((r) => r.slug === slug) : undefined;
  const human = slug === "human";
  const unknown = !slug;
  const name = unknown ? "未知来源" : human ? "人类" : (role?.display_name ?? slug);
  return (
    <span className="lesson-source">
      <RoleAvatar
        slug={unknown ? "unknown" : human ? "human" : slug}
        name={unknown ? "?" : name}
        avatar={unknown || human ? "initials" : role?.avatar}
        color={unknown ? "#8b8496" : human ? "#22d3ee" : role?.avatar_color}
        size={24}
        label={`提供者：${name}`}
      />
      {showName && <span className="lesson-source-name">{name}</span>}
    </span>
  );
}

export function LessonDetailModal({
  lesson, roles, error, onClose, onApprove, onReject, onDelete,
  onRequestCompletion,
}: {
  lesson: Lesson;
  roles: Role[];
  error: string | null;
  onClose: () => void;
  onApprove: () => void;
  onReject: () => void;
  onDelete: () => void;
  onRequestCompletion?: () => Promise<{
    status: string; role?: string; queued?: boolean;
  }>;
}) {
  const [requesting, setRequesting] = useState(false);
  const [completionNotice, setCompletionNotice] = useState("");
  const completionText = completionLabel(lesson.completion);
  const completionDisabled = completionBlocked(lesson.ready, lesson.completion);

  useEffect(() => {
    setCompletionNotice("");
  }, [lesson.id]);

  const displayTitle = lesson.title.length > 80
    ? `${lesson.title.slice(0, 80)}…`
    : lesson.title;

  const meta = [
    { label: "状态", value: LESSON_STATUS_LABEL[lesson.status] },
    { label: "提供者", value: <LessonSource lesson={lesson} roles={roles} showName /> },
    { label: "标签", value: <LessonTags tags={lesson.tags} /> },
    { label: "仓库", value: <span className="mono">{lesson.repo || "shared"}</span> },
    {
      label: "命中",
      value: lesson.status === "approved"
        ? `${lesson.hit_count ?? 0} 次${lesson.last_hit_at ? ` · 最近 ${formatDateTime(lesson.last_hit_at)}` : ""}`
        : "批准后开始统计",
    },
    {
      label: "来源任务",
      value: lesson.source_task
        ? <a className="mono" href={`/tasks/${lesson.source_task}`}>{lesson.source_task}</a>
        : "—",
    },
    { label: "创建时间", value: <span className="mono">{formatDateTime(lesson.created_at)}</span> },
    {
      label: "批准时间",
      value: <span className="mono">{lesson.approved_at ? formatDateTime(lesson.approved_at) : "—"}</span>,
    },
  ];

  const requestCompletion = async () => {
    if (!onRequestCompletion) return;
    setRequesting(true);
    setCompletionNotice("");
    try {
      const result = await onRequestCompletion();
      setCompletionNotice(
        result.status === "already_complete"
          ? "字段已经完整，无需补全。"
          : result.status === "already_queued"
            ? "补全请求已在排队/处理中，完成后会更新状态。"
            : result.queued
              ? `收割角色 ${result.role || lesson.source_role || ""} 正在工作，补全请求已排队；`
                + "它会在当前工作结束后使用来源任务上下文补全。"
              : `已通知收割角色 ${result.role || lesson.source_role || ""} 补全；`
                + "它会使用来源任务上下文填写，完成后刷新详情即可查看。");
    } catch {
      // 错误由父组件的 error 提示。
    } finally {
      setRequesting(false);
    }
  };

  return (
    <KnowledgeDetailModal
      heading="共享经验详情"
      kind="insight"
      title={displayTitle || "共享经验"}
      gate={lesson.ready === undefined ? undefined : {
        ready: lesson.ready,
        errors: lesson.submission_errors || [],
      }}
      tldr={lesson.tldr}
      fields={lesson.fields}
      body={lesson.content}
      quality={lesson.quality}
      meta={meta}
      error={error}
      onClose={onClose}
      actions={(
        <>
          <button type="button" onClick={onClose}>关闭</button>
          {onRequestCompletion && lesson.status !== "rejected" && lesson.source_role && (
            <button type="button" onClick={() => { void requestCompletion(); }}
              disabled={requesting || completionDisabled}
              title={lesson.ready ? "已满足入库标准"
                : lesson.completion?.active ? completionText : undefined}>
              {requesting ? "通知中…"
                : completionActionLabel(
                  lesson.ready, lesson.completion,
                  "让收割角色补全", "已满足入库标准")}
            </button>
          )}
          {lesson.status === "pending" && (
            <>
              <button type="button" className="danger" onClick={onReject}>
                拒绝
              </button>
              <button type="button" className="primary" onClick={onApprove}
                disabled={lesson.ready === false}
                title={lesson.ready === false
                  ? "未达到知识入库标准：请先让收割角色补全字段/缩短标题"
                  : undefined}>
                批准
              </button>
            </>
          )}
          {lesson.status === "approved" && (
            <button type="button" className="danger" onClick={onDelete}>删除</button>
          )}
        </>
      )}
    >
      {completionText && (
        <p className={`lesson-completion-notice ${lesson.completion?.state === "failed" ? "is-error" : ""}`}>
          {completionText}
          {lesson.completion?.role_slug ? ` · ${lesson.completion.role_slug}` : ""}
        </p>
      )}
      {completionNotice && <p className="lesson-completion-notice">{completionNotice}</p>}
    </KnowledgeDetailModal>
  );
}
