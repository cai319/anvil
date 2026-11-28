import { useEffect, useState } from "react";
import { api, type PocArtifact, type PocJob } from "../api/client";

const TEMPLATE = {
  profile: "safe",
  network: "none",
  resources: {
    timeout_seconds: 3600,
    rss_mib: 8192,
    pids: 512,
    scratch_mib: 10240,
    artifact_mib: 200,
  },
  phases: {
    prepare: [],
    build: [{ name: "build", cmd: "make" }],
    run: [{ name: "poc", cmd: "./poc input.bin" }],
  },
  expect: { phase: "run", signal: "SIGSEGV" },
  artifacts: [{ path: "out/**" }],
  reason: "验证崩溃",
};

const STATUS_LABEL: Record<string, string> = {
  queued: "已排队",
  waiting_approval: "待批准",
  running: "运行中",
  succeeded: "成功",
  failed: "失败",
  canceled: "已取消",
  rejected: "已驳回",
};

type Shown = "log" | "evidence" | "artifacts" | null;

export function PocJobsPanel({ taskId, findingId }: {
  taskId: string;
  findingId: string;
}) {
  const [jobs, setJobs] = useState<PocJob[]>([]);
  const [open, setOpen] = useState(false);
  const [spec, setSpec] = useState(() => JSON.stringify(
    { ...TEMPLATE, task_id: taskId, finding_id: findingId }, null, 2));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [shown, setShown] = useState<Record<string, Shown>>({});
  const [detail, setDetail] = useState<Record<string, PocJob>>({});
  const [log, setLog] = useState<Record<string, string>>({});
  const [evidence, setEvidence] = useState<Record<string, Record<string, unknown>>>({});
  const [artifacts, setArtifacts] = useState<Record<string, PocArtifact[]>>({});

  const refresh = () =>
    api.listPocJobs({ task_id: taskId, finding_id: findingId })
      .then(setJobs)
      .catch((e) => setError(String(e)));

  useEffect(() => {
    void refresh();
  }, [taskId, findingId]);

  useEffect(() => {
    if (!jobs.some((j) => ["queued", "running", "waiting_approval"].includes(j.status))) {
      return;
    }
    const timer = setInterval(() => { void refresh(); }, 5000);
    return () => clearInterval(timer);
  }, [jobs]);

  const load = async (id: string) => {
    try {
      const full = await api.getPocJob(id);
      setDetail((prev) => ({ ...prev, [id]: full }));
    } catch (e) {
      setError(String(e));
    }
  };

  const create = async () => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const parsed = JSON.parse(spec);
      const job = await api.createPocJob(parsed);
      setNotice(job.status === "waiting_approval"
        ? "已提交，等待人工批准（收件箱）。"
        : "已排队；作业在严格档沙箱中执行。");
      setOpen(false);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const show = async (id: string, kind: Exclude<Shown, null>) => {
    if (shown[id] === kind) {
      setShown((prev) => ({ ...prev, [id]: null }));
      return;
    }
    setShown((prev) => ({ ...prev, [id]: kind }));
    try {
      if (kind === "log" && log[id] === undefined) {
        const data = await api.getPocJobLog(id);
        setLog((prev) => ({ ...prev, [id]: data.content || "" }));
      }
      if (kind === "evidence" && evidence[id] === undefined) {
        const data = await api.getPocJobEvidence(id);
        setEvidence((prev) => ({ ...prev, [id]: data }));
      }
      if (kind === "artifacts" && artifacts[id] === undefined) {
        const data = await api.listPocArtifacts(id);
        setArtifacts((prev) => ({ ...prev, [id]: data }));
      }
      await load(id);
    } catch (e) {
      setError(String(e));
    }
  };

  const act = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await refresh();
      if (detail[id]) await load(id);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const download = async (id: string, rel: string) => {
    try {
      const blob = await api.downloadPocArtifact(id, rel);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = rel.split("/").pop() || "artifact";
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <section className="finding-section poc-section">
      <div className="finding-section-head">
        <h3>PoC 运行</h3>
        <button type="button" className="small" onClick={() => setOpen((v) => !v)}>
          {open ? "收起" : "新建运行"}
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      {notice && <p className="muted">{notice}</p>}
      {open && (
        <div className="poc-editor">
          <textarea value={spec} spellCheck={false} rows={14}
            onChange={(e) => setSpec(e.target.value)} />
          <div className="toolbar">
            <button type="button" className="primary" disabled={busy} onClick={create}>
              {busy ? "提交中…" : "提交作业"}
            </button>
            <span className="muted">
              safe 档（断网/默认限额）可直接运行；放宽网络或资源需人工批准。
            </span>
          </div>
        </div>
      )}
      {jobs.length === 0 && !open && <p className="muted">尚无 PoC 运行。</p>}
      <div className="poc-list">
        {jobs.map((job) => {
          const net = job.spec?.network || "none";
          const active = ["queued", "running", "waiting_approval"].includes(job.status);
          const view = detail[job.id];
          return (
            <article key={job.id} className="poc-card">
              <div className="poc-card-head">
                <span className={`poc-status is-${job.status}`}>
                  {STATUS_LABEL[job.status] || job.status}
                </span>
                <span className="muted">
                  {job.profile} · 网络 {net} · {job.created_by}
                </span>
                <span className="muted mono">{job.id.slice(0, 12)}</span>
              </div>
              <div className="poc-card-meta">
                {job.exit_code != null && <span>exit {job.exit_code}</span>}
                {job.signal && <span>{job.signal}</span>}
                {job.failure_reason && <span className="error">{job.failure_reason}</span>}
                <span className="muted">{job.created_at}</span>
              </div>
              <div className="toolbar">
                {job.status === "waiting_approval" && (
                  <>
                    <button className="primary small" disabled={busy}
                      onClick={() => act(job.id, () => api.approvePocJob(job.id))}>批准</button>
                    <button className="small danger" disabled={busy}
                      onClick={() => act(job.id, () => api.rejectPocJob(job.id))}>驳回</button>
                  </>
                )}
                <button className="small" onClick={() => show(job.id, "log")}>日志</button>
                <button className="small" onClick={() => show(job.id, "evidence")}>证据</button>
                <button className="small" onClick={() => show(job.id, "artifacts")}>产物</button>
                {active && (
                  <button className="small danger" disabled={busy}
                    onClick={() => act(job.id, () => api.cancelPocJob(job.id))}>取消</button>
                )}
              </div>
              {shown[job.id] === "log" && (
                <pre className="poc-pre">{log[job.id] ?? "加载中…"}</pre>
              )}
              {shown[job.id] === "evidence" && (
                <pre className="poc-pre">
                  {JSON.stringify(evidence[job.id] ?? {}, null, 2)}
                </pre>
              )}
              {shown[job.id] === "artifacts" && (
                <div className="poc-artifacts">
                  {(artifacts[job.id] ?? []).length === 0 && (
                    <span className="muted">无产物。</span>
                  )}
                  {(artifacts[job.id] ?? []).map((a) => (
                    <button key={a.rel_path} type="button" className="small"
                      onClick={() => download(job.id, a.rel_path)}>
                      {a.rel_path} · {a.size_bytes}B
                    </button>
                  ))}
                  {view?.phases && view.phases.length > 0 && (
                    <span className="muted">
                      phases: {view.phases.map(
                        (p) => `${p.phase}/${p.name}=${p.exit_code}`).join(", ")}
                    </span>
                  )}
                </div>
              )}
            </article>
          );
        })}
      </div>
    </section>
  );
}
