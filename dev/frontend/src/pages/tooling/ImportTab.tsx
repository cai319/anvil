import { useEffect, useRef, useState } from "react";
import { api, type Runtime } from "../../api/client";
import { EmptyState } from "../../components/ui";
import { confirmDialog } from "../../components/dialogs";

const directoryProps = { webkitdirectory: "", directory: "" } as Record<string, string>;

interface ImportedItem {
  name: string;
  reason?: string;
}

interface ImportOutcome {
  kind: "skills" | "mcp";
  runtime: string;
  imported: ImportedItem[];
  skipped: ImportedItem[];
}

function normalizeItems(value: unknown): ImportedItem[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    if (typeof item === "string") return { name: item };
    if (item && typeof item === "object") {
      const row = item as Record<string, unknown>;
      return {
        name: String(row.name || row.slug || row.id || "未命名"),
        reason: row.reason == null ? undefined : String(row.reason),
      };
    }
    return { name: String(item) };
  });
}

export function ImportTab() {
  const [runtimes, setRuntimes] = useState<Runtime[]>([]);
  const [runtimeId, setRuntimeId] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"skills" | "mcp" | null>(null);
  const [skillOverwrite, setSkillOverwrite] = useState(false);
  const [mcpOverwrite, setMcpOverwrite] = useState(false);
  const [outcome, setOutcome] = useState<ImportOutcome | null>(null);
  const skillDirRef = useRef<HTMLInputElement>(null);
  const skillArchiveRef = useRef<HTMLInputElement>(null);
  const mcpDirRef = useRef<HTMLInputElement>(null);
  const mcpArchiveRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    api.listRuntimes()
      .then(async (items) => {
        const valid = items.filter((item) => item.status === "ready");
        if (!alive) return;
        setRuntimes(valid);
        let preferred = valid[0]?.id || "";
        try {
          const def = await api.getDefaultRuntime();
          if (alive && def.id && valid.some((item) => item.id === def.id)) {
            preferred = def.id;
          }
        } catch {
          // 旧后端没有默认运行时接口
        }
        if (alive) setRuntimeId(preferred);
      })
      .catch((e) => { if (alive) setError(String(e)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, []);

  const upload = async (kind: "skills" | "mcp", input: HTMLInputElement) => {
    const files = Array.from(input.files || []);
    input.value = "";
    if (!files.length || !runtimeId) return;
    const overwrite = kind === "skills" ? skillOverwrite : mcpOverwrite;
    if (overwrite) {
      const ok = await confirmDialog({
        title: `覆盖同名${kind === "skills" ? " Skill" : " MCP"}？`,
        message: `将导入到 ${runtimeId} 并覆盖同名条目。此操作不可撤销。`,
        confirmLabel: "覆盖导入",
        danger: true,
      });
      if (!ok) return;
    }
    const paths = files.map((file) => (
      (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name
    ));
    setBusy(kind);
    setError(null);
    setOutcome(null);
    try {
      const result = kind === "skills"
        ? await api.importSkillsBundle(runtimeId, files, paths, overwrite)
        : await api.importMcpBundle(runtimeId, files, paths, overwrite);
      setOutcome({
        kind,
        runtime: runtimeId,
        imported: normalizeItems(result.imported),
        skipped: normalizeItems(result.skipped),
      });
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  const renderOutcome = (kind: "skills" | "mcp") => {
    if (!outcome || outcome.kind !== kind) return null;
    return (
      <div className={`tool-import-result ${outcome.skipped.length ? "has-skipped" : ""}`}>
        <div>
          已导入到 <code>{outcome.runtime}</code>：
          {outcome.imported.length
            ? outcome.imported.map((item) => item.name).join("、")
            : "无"}
        </div>
        {outcome.skipped.length > 0 && (
          <div className="muted">
            跳过 {outcome.skipped.length} 项：
            {outcome.skipped.map((item) =>
              `${item.name}${item.reason ? `（${item.reason}）` : ""}`).join("、")}
          </div>
        )}
      </div>
    );
  };

  if (loading) return <p className="muted">加载运行时…</p>;

  return (
    <div className="tooling-scroll">
      {error && <p className="error">{error}</p>}
      {runtimes.length === 0 && !loading ? (
        <EmptyState
          icon="cpu"
          title="没有可用运行时"
          hint="请先在运行时页完成扫描或注册，再导入 Skill / MCP。"
          action={<a className="button-like" href="/runtimes">打开运行时页</a>}
        />
      ) : (
        <>
          <div className="tool-import-runtime">
            <label className="tooling-runtime">
              <span>导入到运行时</span>
              <select
                value={runtimeId}
                onChange={(e) => { setRuntimeId(e.target.value); setOutcome(null); }}
              >
                {runtimes.map((runtime) => (
                  <option key={runtime.id} value={runtime.id}>{runtime.display_name}</option>
                ))}
              </select>
            </label>
            <span className="muted">
              导入的 Skill / MCP 只对该运行时生效。运行时列表在{" "}
              <a href="/runtimes">运行时页</a>维护。
            </span>
          </div>

          <section className="tool-import-grid">
            <div className="tool-import-card">
              <div className="tool-import-head">
                <div>
                  <h2>导入 Skill</h2>
                  <p>支持包含 <code>SKILL.md</code> 的目录，或 zip / tar.gz 压缩包；整个目录会一起复制。</p>
                </div>
                <label className="tool-import-overwrite">
                  <input type="checkbox" checked={skillOverwrite}
                    onChange={(e) => setSkillOverwrite(e.target.checked)} />
                  覆盖同名
                </label>
              </div>
              <div className="card-actions">
                <button className="primary" disabled={busy !== null || !runtimeId}
                  onClick={() => skillDirRef.current?.click()}>导入 Skill 目录</button>
                <button disabled={busy !== null || !runtimeId}
                  onClick={() => skillArchiveRef.current?.click()}>导入压缩包</button>
                {busy === "skills" && <span className="muted">导入中…</span>}
              </div>
              <input ref={skillDirRef} type="file" multiple hidden
                {...directoryProps}
                onChange={(e) => { void upload("skills", e.currentTarget); }} />
              <input ref={skillArchiveRef} type="file" hidden
                accept=".zip,.tar,.tar.gz,.tgz"
                onChange={(e) => { void upload("skills", e.currentTarget); }} />
              {renderOutcome("skills")}
            </div>

            <div className="tool-import-card">
              <div className="tool-import-head">
                <div>
                  <h2>导入 MCP</h2>
                  <p>支持带 <code>mcpServers</code> / <code>servers</code> 配置的目录或压缩包，自动登记到运行时。</p>
                </div>
                <label className="tool-import-overwrite">
                  <input type="checkbox" checked={mcpOverwrite}
                    onChange={(e) => setMcpOverwrite(e.target.checked)} />
                  覆盖同名
                </label>
              </div>
              <div className="card-actions">
                <button className="primary" disabled={busy !== null || !runtimeId}
                  onClick={() => mcpDirRef.current?.click()}>导入 MCP 目录</button>
                <button disabled={busy !== null || !runtimeId}
                  onClick={() => mcpArchiveRef.current?.click()}>导入压缩包</button>
                {busy === "mcp" && <span className="muted">导入中…</span>}
              </div>
              <input ref={mcpDirRef} type="file" multiple hidden
                {...directoryProps}
                onChange={(e) => { void upload("mcp", e.currentTarget); }} />
              <input ref={mcpArchiveRef} type="file" hidden
                accept=".zip,.tar,.tar.gz,.tgz"
                onChange={(e) => { void upload("mcp", e.currentTarget); }} />
              {renderOutcome("mcp")}
            </div>
          </section>
        </>
      )}
    </div>
  );
}
