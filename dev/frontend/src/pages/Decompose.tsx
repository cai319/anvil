import { useEffect, useState } from "react";
import { api } from "../api/client";
import { PageHeader } from "../components/ui";

interface Draft {
  id: string;
  goal: string;
  status: "pending" | "approved" | "rejected";
  kind?: "tasks" | "workflow" | "role_suggestion";
  tree?: { title: string; children?: { title: string }[] };
  created_at: string;
}

export function Decompose() {
  const [goal, setGoal] = useState("");
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [templates, setTemplates] = useState<{ name: string; steps: string[] }[]>([]);
  const [template, setTemplate] = useState("design-dev-test");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = () => api.listDrafts().then((d) => setDrafts(d as unknown as Draft[])).catch((e) => setError(String(e)));

  useEffect(() => {
    refresh();
    api.listBoardTemplates().then(setTemplates).catch(() => {});
  }, []);

  const decompose = async () => {
    if (!goal.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.decompose(goal.trim(), template);
      setGoal("");
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const approve = async (id: string) => {
    try {
      await api.approveDrafts([id]);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const reject = async (id: string) => {
    try {
      await api.rejectDraft(id);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div>
      <PageHeader kicker="DECOMPOSE" title="目标拆解" />
      {error && <p className="error">{error}</p>}

      <div className="toolbar">
        <input value={goal} onChange={(e) => setGoal(e.target.value)} placeholder="输入顶层目标（如：实现订单分页）" />
        <select value={template} onChange={(e) => setTemplate(e.target.value)} title="工作流模板（SOP）">
          {templates.map((t) => (
            <option key={t.name} value={t.name}>{t.name} · {t.steps.join(" → ")}</option>
          ))}
        </select>
        <button onClick={decompose} disabled={busy}>{busy ? "拆解中…" : "拆解"}</button>
      </div>

      <ul className="drafts">
        {drafts.filter((d) => d.kind !== "role_suggestion").length === 0 && (
          <li className="muted">暂无拆解 —— 输入目标后点「拆解」，产出任务树草案需你审批落板。</li>
        )}
        {drafts.filter((d) => d.kind !== "role_suggestion").map((d) => (
          <li key={d.id}>
            <strong>{d.goal}</strong>{" "}
            <span className={`status status-${d.status === "pending" ? "in_review" : d.status === "approved" ? "done" : "closed"}`}>
              {d.status}
            </span>
            {d.tree && (
              <ul className="tree">
                <li>{d.tree.title}</li>
                {(d.tree.children ?? []).map((c, i) => (
                  <li key={i} className="muted">└ {c.title}</li>
                ))}
              </ul>
            )}
            {d.status === "pending" && (
              <div className="card-actions">
                <button onClick={() => approve(d.id)}>审批落板</button>
                <button onClick={() => reject(d.id)} className="danger">驳回</button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
