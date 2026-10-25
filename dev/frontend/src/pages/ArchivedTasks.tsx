import { useEffect, useMemo, useState } from "react";
import { api, type Task } from "../api/client";
import { PageHeader } from "../components/ui";

/** 归档任务：只显示 archived_at 非空的任务，与大厅互不重叠。 */
export function ArchivedTasks() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [q, setQ] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = () =>
    api.listTasks({ archived_only: true }).then(setTasks).catch((e) => setError(String(e)));

  useEffect(() => {
    refresh();
  }, []);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return tasks;
    return tasks.filter((t) => t.title.toLowerCase().includes(needle)
      || t.tags.some((tag) => tag.toLowerCase().includes(needle)));
  }, [tasks, q]);

  const act = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(id);
    setError(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="lobby-page">
      <PageHeader kicker="ARCHIVE" title="归档"
        subtitle={`${tasks.length} 个已归档任务`} />
      {error && <p className="error">{error}</p>}
      <div className="toolbar" style={{ marginBottom: 12 }}>
        <input value={q} onChange={(e) => setQ(e.target.value)}
          placeholder="搜索标题或标签" />
      </div>
      <ul className="roles-list">
        {filtered.length === 0 && <li className="muted">暂无归档任务。</li>}
        {filtered.map((t) => (
          <li key={t.id} style={{ display: "block" }}>
            <a href={`/tasks/${t.id}`}><strong>{t.title}</strong></a>{" "}
            <small>{t.status} · {t.repo || "无 repo"}{t.archived_at ? ` · 归档于 ${t.archived_at.slice(0, 10)}` : ""}</small>
            <div className="toolbar">
              <button className="small" disabled={busy === t.id}
                onClick={() => act(t.id, () => api.unarchiveTask(t.id))}>取消归档</button>
              <button className="small danger" disabled={busy === t.id}
                onClick={() => act(t.id, () => api.deleteTask(t.id))}>删除</button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
