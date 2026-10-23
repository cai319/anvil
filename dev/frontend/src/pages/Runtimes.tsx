import { useEffect, useState } from "react";
import { api, type Runtime } from "../api/client";
import { PageHeader } from "../components/ui";

const STATUS_LABEL: Record<string, string> = {
  ready: "就绪",
  unverified: "未验证",
  unhealthy: "异常",
  missing: "缺失",
};

const VERSION_REGEX = "regex:(\\d+\\.\\d+\\.\\d+)";

/** Claude 风格的最小运行时模板；用户可按自己的 CLI 调整 executable / args。 */
function runtimeTemplate(id = "codeagent", command = "codeagent"): string {
  return `id: ${id}
display_name: ${id}
version_range: ">=0.1.0"

discovery:
  strategies:
    - type: path_lookup
      command: ${command}
  version_probe:
    args: ["--version"]
    parse: '${VERSION_REGEX}'

invocation:
  args:
    - "--print"
    - "--permission-mode"
    - "{{ permission_mode }}"
    - "{{ prompt }}"
  cwd: "{{ workspace_path }}"
  timeout_seconds: 3600
  exit_code_success: [0]

capabilities:
  managed_mcp: false
  managed_skills: false
  tool_level_grant: inherit
  session_resume: false
  streaming_output: false

projection:
  instruction:
    target: "{{ workspace_path }}/CLAUDE.md"
    format: markdown
    strategy: overwrite_managed_block

health:
  probe_prompt: "Reply with exactly: ANVIL_OK"
  expect_contains: "ANVIL_OK"
  timeout_seconds: 60
  args: ["--print", "{{probe_prompt}}"]
`;
}

interface EditorState {
  id: string;
  yaml: string;
  isNew: boolean;
  builtin?: boolean;
}

export function Runtimes() {
  const [runtimes, setRuntimes] = useState<Runtime[]>([]);
  const [defaultId, setDefaultId] = useState<string | null>(null);
  const [defaultAvailable, setDefaultAvailable] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [health, setHealth] = useState<Record<string, string>>({});
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [editorError, setEditorError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const refresh = async () => {
    try {
      setRuntimes(await api.listRuntimes());
    } catch (e) {
      setError(String(e));
      return;
    }
    // 旧后端没有 /runtimes/default；不要让这个 404 阻断运行时列表与扫描。
    try {
      const def = await api.getDefaultRuntime();
      setDefaultId(def.id || null);
      setDefaultAvailable(true);
    } catch {
      setDefaultId(null);
      setDefaultAvailable(false);
    }
  };

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

  const setDefault = async (id: string) => {
    setError(null);
    try {
      const def = await api.setDefaultRuntime(id);
      setDefaultId(def.id);
    } catch (e) {
      setError(String(e));
    }
  };

  const openNew = () => {
    setEditorError(null);
    setEditor({ id: "codeagent", yaml: runtimeTemplate("codeagent", "codeagent"), isNew: true });
  };

  const openEdit = async (id: string) => {
    setEditorError(null);
    try {
      const def = await api.getRuntimeDefinition(id);
      setEditor({ id, yaml: def.yaml, isNew: false, builtin: def.builtin });
    } catch (e) {
      setError(String(e));
    }
  };

  const changeEditorId = (next: string) => {
    setEditor((prev) => {
      if (!prev) return prev;
      const yaml = prev.isNew
        ? prev.yaml.replace(/^id:.*$/m, `id: ${next}`)
        : prev.yaml;
      return { ...prev, id: next, yaml };
    });
  };

  const saveEditor = async () => {
    if (!editor) return;
    const id = editor.id.trim();
    if (!id) {
      setEditorError("运行时 ID 不能为空");
      return;
    }
    setSaving(true);
    setEditorError(null);
    try {
      await api.putRuntimeDefinition(id, editor.yaml);
      setEditor(null);
      await refresh();
    } catch (e) {
      setEditorError(String(e));
    } finally {
      setSaving(false);
    }
  };

  const defaultRuntime = runtimes.find((r) => r.id === defaultId);

  return (
    <div>
      <PageHeader
        kicker="RUNTIMES"
        title="运行时"
        subtitle={
          <>
            AI 生成画像/角色会复用默认运行时的登录态，无需单独配置 API Key。当前默认：
            <strong>{defaultRuntime?.display_name ?? defaultId ?? "未设置"}</strong>
          </>
        }
      />
      <div className="toolbar">
        <label className="field" style={{ marginBottom: 0, minWidth: 320 }}>
          <span>默认运行时（无角色参与的 AI 调用使用）</span>
          <select
            value={defaultId ?? ""}
            onChange={(e) => setDefault(e.target.value)}
            disabled={!defaultAvailable || runtimes.length === 0}
          >
            {runtimes.length === 0 && <option value="">（暂无运行时）</option>}
            {defaultId && !runtimes.some((r) => r.id === defaultId) && (
              <option value={defaultId}>{defaultId}（未注册）</option>
            )}
            {runtimes.map((r) => (
              <option key={r.id} value={r.id}>{r.display_name} · {r.status}</option>
            ))}
          </select>
        </label>
      </div>
      {!defaultAvailable && (
        <p className="muted">
          当前后端未提供默认运行时接口（可能是旧版本）。请重启后端；运行时列表与扫描仍可正常使用。
        </p>
      )}
      {error && <p className="error">{error}</p>}
      <div className="toolbar">
        <button onClick={discover} disabled={busy}>{busy ? "扫描中…" : "扫描本地运行时"}</button>
        <button onClick={openNew}>新增自定义运行时</button>
      </div>
      <div className="table-scroll">
      <table className="board">
        <thead>
          <tr><th>名称</th><th>ID</th><th>状态</th><th>默认</th><th>版本</th><th>路径</th><th>操作</th></tr>
        </thead>
        <tbody>
          {runtimes.length === 0 && (
            <tr><td colSpan={7} className="muted">暂无运行时 —— 点击上方「扫描本地运行时」。</td></tr>
          )}
          {runtimes.map((r) => (
            <tr key={r.id}>
              <td>{r.display_name}</td>
              <td>{r.id}</td>
              <td>
                <span className={`status status-${r.status}`}>{STATUS_LABEL[r.status] ?? r.status}</span>
                {r.last_probe_log && (
                  <div className="muted" style={{ fontSize: 12, marginTop: 4, maxWidth: 420, whiteSpace: "normal" }}>
                    {r.last_probe_log}
                  </div>
                )}
              </td>
              <td>
                {!defaultAvailable ? (
                  <span className="muted">需重启后端</span>
                ) : defaultId === r.id ? (
                  <span className="status status-ready">默认</span>
                ) : (
                  <button className="small" onClick={() => setDefault(r.id)}>设为默认</button>
                )}
              </td>
              <td>{r.detected_version ?? "—"}</td>
              <td className="mono">{r.executable_path}</td>
              <td>
                <button className="small" onClick={() => openEdit(r.id)}>编辑</button>
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

      {editor && (
        <div className="modal-overlay" onMouseDown={(e) => e.target === e.currentTarget && setEditor(null)}>
          <div className="modal" role="dialog" aria-label="运行时 YAML">
            <div className="modal-head">
              <h2>{editor.isNew ? "新增自定义运行时" : `编辑运行时 · ${editor.id}`}</h2>
              <button className="modal-close" onClick={() => setEditor(null)} aria-label="关闭">×</button>
            </div>

            {editor.isNew && (
              <label className="field">
                <span>运行时 ID</span>
                <input value={editor.id} onChange={(e) => changeEditorId(e.target.value)}
                  placeholder="例如 codeagent" />
              </label>
            )}

            <label className="field">
              <span>运行时 YAML</span>
              <textarea
                className="mono"
                rows={20}
                spellCheck={false}
                value={editor.yaml}
                onChange={(e) => setEditor((prev) => prev ? { ...prev, yaml: e.target.value } : prev)}
              />
            </label>

            <small className="muted">
              保存后会写入用户运行时目录（Windows <code>%APPDATA%\Anvil\runtimes</code>，
              macOS <code>~/.config/Anvil/runtimes</code>）。
              {editor.builtin ? " 当前是内置定义；保存将创建用户覆盖。" : ""}
              <br />
              模板按 Claude 风格编写；如果 <code>codeagent</code> 的启动参数不同，请调整
              <code> discovery.strategies</code> 与 <code>invocation.args</code>（必须包含
              <code> {"{{ prompt }}"}</code>）。
            </small>

            {editorError && <p className="error">{editorError}</p>}
            <div className="modal-actions">
              {editor.isNew && (
                <button onClick={() => setEditor((prev) => prev ? {
                  ...prev,
                  yaml: runtimeTemplate(prev.id || "codeagent", prev.id || "codeagent"),
                } : prev)}>重置为 Claude 风格模板</button>
              )}
              <button onClick={() => setEditor(null)} disabled={saving}>取消</button>
              <button className="primary" onClick={saveEditor} disabled={saving}>
                {saving ? "保存并探测…" : "保存并探测"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
