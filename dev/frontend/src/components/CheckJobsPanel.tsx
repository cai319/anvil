import { useEffect, useState } from "react";
import { api, type PocArtifact, type PocJob } from "../api/client";

const TEMPLATE = {
  task_id: "T-...",
  network: "none",
  check: {
    id: "c1",
    kind: "test",
    cmd: "pytest -q --junit-xml=/out/junit.xml",
    junit: ["junit.xml"],
  },
};

const STATUS_LABEL: Record<string, string> = {
  queued: "已排队",
  waiting_approval: "待批准",
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  canceled: "已取消",
  rejected: "已驳回",
};

const CHECK_STATUS_LABEL: Record<string, string> = {
  passed: "通过",
  failed: "未通过",
  signal: "信号终止",
  timeout: "超时",
  error: "运行错误",
};

const CHECK_CLASSIFICATION_LABEL: Record<string, string> = {
  passed: "通过",
  valid_red: "合法红",
  harness_error: "harness 错误",
};

export function CheckJobsPanel({ taskId }: { taskId: string }) {
  const [jobs, setJobs] = useState<PocJob[]>([]);
  const [open, setOpen] = useState(false);
  const [spec, setSpec] = useState(() => JSON.stringify(
    { ...TEMPLATE, task_id: taskId }, null, 2));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [log, setLog] = useState<Record<string, string>>({});
  const [artifacts, setArtifacts] = useState<Record<string, PocArtifact[]>>({});
  const [shown, setShown] = useState<Record<string, "log" | "artifacts" | null>>({});

  const refresh = () =>
    api.listCheckJobs({ task_id: taskId })
      .then(setJobs)
      .catch((e) => setError(String(e)));

  useEffect(() => {
    void refresh();
  }, [taskId]);

  useEffect(() => {
    if (!jobs.some((j) => ["queued", "running", "waiting_approval"].includes(j.status))) {
      return;
    }
    const timer = setInterval(() => { void refresh(); }, 5000);
    return () => clearInterval(timer);
  }, [jobs]);

  const create = async () => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const parsed = JSON.parse(spec);
      const job = await api.createCheckJob(parsed);
      setNotice(job.status === "waiting_approval"
        ? "已提交，等待人工批准（收件箱）。"
        : "已排队；引擎将在沙箱中执行检查。");
      setOpen(false);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const show = async (id: string, kind: "log" | "artifacts") => {
    if (shown[id] === kind) {
      setShown((prev) => ({ ...prev, [id]: null }));
      return;
    }
    setShown((prev) => ({ ...prev, [id]: kind }));
    try {
      if (kind === "log" && log[id] === undefined) {
        const data = await api.getCheckJobLog(id);
        setLog((prev) => ({ ...prev, [id]: data.content || "" }));
      }
      if (kind === "artifacts" && artifacts[id] === undefined) {
        const data = await api.listCheckArtifacts(id);
        setArtifacts((prev) => ({ ...prev, [id]: data }));
      }
    } catch (e) {
      setError(String(e));
    }
  };

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="finding-section poc-section">
      <div className="finding-section-head">
        <h3>检查作业</h3>
        <button type="button" className="small" onClick={() => setOpen((v) => !v)}>
          {open ? "收起" : "新建检查"}
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      {notice && <p className="muted">{notice}</p>}
      {open && (
        <div className="poc-editor">
          <textarea value={spec} spellCheck={false} rows={12}
            onChange={(e) => setSpec(e.target.value)} />
          <div className="toolbar">
            <button type="button" className="primary" disabled={busy} onClick={create}>
              {busy ? "提交中…" : "提交检查"}
            </button>
            <span className="muted">
              标准档、默认断网；放宽网络或自定义镜像需人工批准。
            </span>
          </div>
        </div>
      )}
      {jobs.length === 0 && !open && <p className="muted">尚无检查作业。</p>}
      <div className="poc-list">
        {jobs.map((job) => {
          const result = job.check_result;
          const junit = result?.junit_summary;
          const active = ["queued", "running", "waiting_approval"].includes(job.status);
          return (
            <article key={job.id} className="poc-card">
              <div className="poc-card-head">
                <span className={`poc-status is-${job.status}`}>
                  {STATUS_LABEL[job.status] || job.status}
                </span>
                {result && (
                  <span className={`poc-status check-${result.status}`}>
                    {CHECK_STATUS_LABEL[result.status || ""] || result.status}
                  </span>
                )}
                {result?.classification && (
                  <span className={`poc-status check-${result.classification}`}>
                    {CHECK_CLASSIFICATION_LABEL[result.classification]
                      || result.classification}
                  </span>
                )}
                <span className="muted">
                  {job.spec?.check?.kind || "check"} · 网络 {job.spec?.network || "none"} · {job.tier || "standard"}
                </span>
                <span className="muted mono">{job.id.slice(0, 14)}</span>
              </div>
              <div className="poc-card-meta">
                {result?.check_id && <span className="mono">{result.check_id}</span>}
                {result?.exit_code != null && <span>exit {result.exit_code}</span>}
                {result?.signal && <span>{result.signal}</span>}
                {result?.timed_out && <span className="error">超时</span>}
                {result?.collected_count != null && (
                  <span>收集 {result.collected_count} 项</span>
                )}
                {result?.collected_baseline != null && (
                  <span>基线 {result.collected_baseline} 项</span>
                )}
                {result?.cause && <span className="error">{result.cause}</span>}
                {junit && (
                  <span>
                    junit {junit.tests}/{junit.failures}F/{junit.errors}E/{junit.skipped}S
                  </span>
                )}
                {result?.env_hash && (
                  <span className="muted mono" title={result.env_hash}>
                    env {result.env_hash.slice(0, 14)}
                  </span>
                )}
                {job.failure_reason && <span className="error">{job.failure_reason}</span>}
                <span className="muted">{job.created_at}</span>
              </div>
              <div className="toolbar">
                {job.status === "waiting_approval" && (
                  <>
                    <button className="primary small" disabled={busy}
                      onClick={() => act(() => api.approveCheckJob(job.id))}>批准</button>
                    <button className="small danger" disabled={busy}
                      onClick={() => act(() => api.rejectCheckJob(job.id))}>驳回</button>
                  </>
                )}
                <button className="small" onClick={() => show(job.id, "log")}>日志</button>
                <button className="small" onClick={() => show(job.id, "artifacts")}>产物</button>
                {active && (
                  <button className="small danger" disabled={busy}
                    onClick={() => act(() => api.cancelCheckJob(job.id))}>取消</button>
                )}
              </div>
              {shown[job.id] === "log" && (
                <pre className="poc-pre">{log[job.id] ?? "加载中…"}</pre>
              )}
              {shown[job.id] === "artifacts" && (
                <div className="poc-artifacts">
                  {(artifacts[job.id] ?? []).length === 0 && (
                    <span className="muted">无产物。</span>
                  )}
                  {(artifacts[job.id] ?? []).map((a) => (
                    <span key={a.rel_path} className="muted mono">
                      {a.rel_path} · {a.size_bytes}B
                    </span>
                  ))}
                </div>
              )}
            </article>
          );
        })}
      </div>
    </section>
  );
}
