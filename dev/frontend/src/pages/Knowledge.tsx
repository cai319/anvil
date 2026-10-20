import { useEffect, useState } from "react";
import { api, type Lesson, type Proposal } from "../api/client";

export function Knowledge() {
  const [pending, setPending] = useState<Lesson[]>([]);
  const [approved, setApproved] = useState<Lesson[]>([]);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [content, setContent] = useState("");
  const [tags, setTags] = useState("");
  const [repo, setRepo] = useState("shared");
  const [error, setError] = useState<string | null>(null);

  const refresh = () =>
    Promise.all([
      api.listLessons("pending"),
      api.listLessons("approved"),
      api.listProposals(),
    ])
      .then(([p, a, props]) => {
        setPending(p);
        setApproved(a);
        setProposals(props);
      })
      .catch((e) => setError(String(e)));

  useEffect(() => {
    refresh();
  }, []);

  const propose = async () => {
    if (!content.trim()) return;
    try {
      await api.proposeLesson(
        content.trim(),
        tags.split(",").map((t) => t.trim()).filter(Boolean),
        repo.trim() || "shared",
      );
      setContent("");
      setTags("");
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div>
      <h1>知识库与提案</h1>
      {error && <p className="error">{error}</p>}

      <section>
        <h2 className="pane-title">共享经验 · 待审批 {pending.length}</h2>
        <div className="toolbar">
          <input value={content} onChange={(e) => setContent(e.target.value)} placeholder="经验内容" style={{ flex: 1 }} />
          <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="标签（逗号分隔）" />
          <input value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="repo（默认 shared）" />
          <button onClick={propose} disabled={!content.trim()}>提议升格</button>
        </div>
        <ul className="drafts">
          {pending.length === 0 && <li className="muted">没有待审经验。</li>}
          {pending.map((l) => (
            <li key={l.id}>
              <strong>{l.content}</strong>
              <div className="muted">#{l.tags.join(" #")} · repo={l.repo}</div>
              <div className="card-actions">
                <button onClick={() => act(() => api.approveLesson(l.id))}>批准</button>
                <button className="danger" onClick={() => act(() => api.rejectLesson(l.id))}>拒绝</button>
              </div>
            </li>
          ))}
        </ul>

        <h2 className="pane-title">已批准共享经验 · {approved.length}</h2>
        <ul className="drafts">
          {approved.length === 0 && <li className="muted">暂无已批准经验。</li>}
          {approved.map((l) => (
            <li key={l.id}>
              {l.content}
              <div className="muted">#{l.tags.join(" #")} · repo={l.repo} · {l.approved_at?.slice(0, 10)}</div>
              <button className="small danger" onClick={() => { if (window.confirm("确定删除该条经验？删除后不可恢复。")) act(() => api.deleteLesson(l.id)); }}>删除</button>
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h2 className="pane-title">角色演化提案 · {proposals.filter((p) => p.status === "pending").length} 待审</h2>
        <ul className="drafts">
          {proposals.length === 0 && <li className="muted">暂无提案 —— Agent 可用 `anvil self propose` 提交。</li>}
          {proposals.map((p) => (
            <li key={p.id}>
              <strong>{p.role_slug}</strong> · <span className="mono">{p.field}</span>{" "}
              <span className={`status status-${p.status === "pending" ? "in_review" : p.status === "approved" ? "done" : "closed"}`}>
                {p.status}
              </span>
              <pre className="run-log">{JSON.stringify(p.proposed, null, 2)}</pre>
              {p.rationale && <div className="muted">依据：{p.rationale}</div>}
              {p.risk && <div className="muted">风险：{p.risk}</div>}
              {p.status === "pending" && (
                <div className="card-actions">
                  <button onClick={() => act(() => api.approveProposal(p.id))}>批准并应用</button>
                  <button className="danger" onClick={() => act(() => api.rejectProposal(p.id))}>拒绝</button>
                </div>
              )}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
