import { useEffect, useState } from "react";
import { api, type Runtime } from "../api/client";

const STATUS_LABEL: Record<string, string> = {
  ready: "就绪",
  unverified: "未验证",
  unhealthy: "异常",
  missing: "缺失",
};

export function Runtimes() {
  const [runtimes, setRuntimes] = useState<Runtime[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [health, setHealth] = useState<Record<string, string>>({});

  const refresh = () => api.listRuntimes().then(setRuntimes).catch((e) => setError(String(e)));

  useEffect(() => {
    refresh();
  }, []);

  const discover = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.discoverRuntimes();
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <h1>运行时</h1>
      {error && <p className="error">{error}</p>}
      <div className="toolbar">
        <button onClick={discover} disabled={busy}>{busy ? "扫描中…" : "扫描本地运行时"}</button>
      </div>
      <table className="board">
        <thead>
          <tr><th>名称</th><th>ID</th><th>状态</th><th>版本</th><th>路径</th><th>操作</th></tr>
        </thead>
        <tbody>
          {runtimes.length === 0 && (
            <tr><td colSpan={6} className="muted">暂无运行时 —— 点击上方「扫描本地运行时」。</td></tr>
          )}
          {runtimes.map((r) => (
            <tr key={r.id}>
              <td>{r.display_name}</td>
              <td>{r.id}</td>
              <td><span className={`status status-${r.status}`}>{STATUS_LABEL[r.status] ?? r.status}</span></td>
              <td>{r.detected_version ?? "—"}</td>
              <td className="mono">{r.executable_path}</td>
              <td>
                <button
                  className="small"
                  onClick={async () => {
                    try {
                      const h = await api.checkRuntimeHealth(r.id);
                      setHealth((prev) => ({
                        ...prev,
                        [r.id]: h.ok ? "健康检查通过" : `检查失败：${h.detail ?? h.log ?? "未知"}`,
                      }));
                      await refresh();
                    } catch (e) {
                      setError(String(e));
                    }
                  }}
                >健康检查</button>
                {health[r.id] && <div className="muted">{health[r.id]}</div>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
