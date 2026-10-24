import { useEffect, useState } from "react";
import { api, subscribeEvents, type Run, type TimelineEvent } from "../api/client";
import { formatClock, formatDateTime, parseTime, useNow } from "../time";
import { Icon } from "../components/Icon";

const RUN_LABEL: Record<string, string> = {
  created: "已创建",
  projecting: "准备中",
  running: "运行中",
  submitted: "已提交",
  reviewing: "评审中",
  completed: "已完成",
  failed: "失败",
  abandoned: "已放弃",
  suspended: "已挂起",
};

function fmtDuration(start: string | null, end: string | null, now = Date.now()): string {
  const a = parseTime(start);
  if (!a) return "—";
  const b = parseTime(end)?.getTime() ?? now;
  const s = Math.max(0, Math.round((b - a.getTime()) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

export function RunSession({ id }: { id: string }) {
  const [run, setRun] = useState<Run | null>(null);
  const [timeline, setTimeline] = useState<TimelineEvent[]>([]);
  const [snapshot, setSnapshot] = useState<Record<string, unknown> | null>(null);
  const [log, setLog] = useState<{ exists: boolean; content: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const now = useNow(30_000); // 运行时长按系统当前时间刷新

  const refresh = () =>
    Promise.all([api.getRun(id), api.getRunTimeline(id), api.getRunSnapshot(id), api.getRunLog(id)])
      .then(([r, tl, snap, lg]) => {
        setRun(r);
        setTimeline(tl);
        setSnapshot(snap);
        setLog(lg);
      })
      .catch((e) => setError(String(e)));

  useEffect(() => {
    refresh();
    return subscribeEvents((ev) => {
      if (ev.topic === "tick" || ev.topic === "task") refresh();
    });
  }, [id]);

  if (error) return <p className="error">{error}</p>;
  if (!run) return <p className="muted">加载中…</p>;

  return (
    <div className="session">
      <p className="session-back">
        <a href={`/agents/${run.role_slug}`}><Icon name="chevron-left" /> {run.role_slug}</a>
      </p>

      <div className="session-head">
        <div className="session-title">
          <h1 className="mono">run {run.id.slice(0, 8)}</h1>
          <span
            className={`status status-${run.status}`}
            title={run.status === "projecting"
              ? "正在把角色说明、任务上下文、知识库和工具配置写入工作区"
              : undefined}
          >{RUN_LABEL[run.status] ?? run.status}</span>
          {run.failure_class && (
            <span className="badge badge-in_progress" title="失败分类（P0-4）">{run.failure_class}</span>
          )}
        </div>
        <div className="session-meta">
          <span>角色 <a href={`/agents/${run.role_slug}`}>{run.role_slug}</a></span>
          {run.task_id && <span>任务 <a href={`/tasks/${run.task_id}`}>{run.task_id}</a></span>}
          <span className="mono muted">{formatDateTime(run.started_at)}</span>
          <span className="muted">{fmtDuration(run.started_at, run.ended_at, now)}</span>
        </div>
        <div className="session-meta">
          <span className="muted mono">session {run.native_session_id ?? "—"}</span>
          <span className="muted mono">worktree {run.worktree_path ?? "—"}</span>
        </div>
      </div>

      {run.status === "projecting" && (
        <p className="muted">
          准备中：正在把角色说明、任务上下文、知识库和工具配置写入工作区，完成后会自动启动角色进程。
        </p>
      )}

      <div className="toolbar">
        <button onClick={() => api.restartRun(id).then(refresh).catch((e) => setError(String(e)))}>重启进程（保留上下文）</button>
        <button onClick={() => {
          if (window.confirm("重置会话会丢弃当前对话上下文（保留 MEMORY.md 与 notes），确定？")) {
            api.sessionResetRun(id).then(refresh).catch((e) => setError(String(e)));
          }
        }}>重置会话（新上下文）</button>
        <button
          className="danger"
          onClick={() => {
            if (window.confirm("全量重置会清空该角色的 MEMORY.md 与 notes（自动先备份 home），确定？")) {
              api.fullResetRun(id).then(refresh).catch((e) => setError(String(e)));
            }
          }}
        >全量重置（清记忆）</button>
        <button className="danger" onClick={() => {
          if (window.confirm("释放进程会中止当前运行，确定？")) {
            api.releaseRun(id).then(refresh).catch((e) => setError(String(e)));
          }
        }}>释放进程</button>
      </div>
      <p className="muted">
        失败后如果怀疑上下文污染，用「重置会话」；只想重启进程但保留上下文，用「重启进程」。
      </p>

      <section>
        <h2 className="pane-title">生命周期</h2>
        {timeline.length === 0 && <p className="muted">无迁移事件。</p>}
        <ul className="run-timeline">
          {timeline.map((e) => (
            <li key={e.key}>
              <span className={`status status-${String(e.payload.status)}`}>{RUN_LABEL[String(e.payload.status)] ?? e.payload.status}</span>
              <span className="mono muted">{formatClock(e.ts)}</span>
              {e.payload.prev ? <span className="muted">由 {RUN_LABEL[String(e.payload.prev)] ?? e.payload.prev}</span> : null}
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h2 className="pane-title">输出日志（stdout/stderr）</h2>
        {!log?.exists ? (
          <p className="muted">无日志内容。</p>
        ) : (
          <pre className="run-log">{log.content}</pre>
        )}
      </section>

      <details className="session-details">
        <summary>配置快照（profile snapshot）</summary>
        <pre className="run-log">{JSON.stringify(snapshot ?? {}, null, 2)}</pre>
      </details>
    </div>
  );
}
