import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, type FeatureFlag, type Runtime } from "../api/client";
import { PageHeader } from "../components/ui";

const directoryProps = { webkitdirectory: "", directory: "" } as Record<string, string>;

const FLAG_GROUPS: { key: string; title: string; hint: string }[] = [
  {
    key: "knowledge",
    title: "知识",
    hint: "控制主题知识库、任务知识和经验捕获是否参与任务执行。",
  },
  {
    key: "context",
    title: "上下文",
    hint: "控制任务上下文如何构造、投影和审计。",
  },
  {
    key: "workspace",
    title: "任务隔离",
    hint: "控制每个任务的工作空间与磁盘清理。",
  },
];

/** 功能开关的详细说明气泡：悬停或键盘聚焦时显示。 */
function FeatureHint({ text }: { text: string }) {
  const ref = useRef<HTMLSpanElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);

  const show = () => {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const width = Math.min(320, window.innerWidth - 24);
    const left = Math.min(
      window.innerWidth - width - 12,
      Math.max(12, rect.left + rect.width / 2 - width / 2),
    );
    const spaceBelow = window.innerHeight - rect.bottom;
    const top = spaceBelow >= 150 ? rect.bottom + 8 : Math.max(12, rect.top - 148);
    setPos({ top, left });
  };

  return (
    <>
      <span
        ref={ref}
        className="field-hint"
        tabIndex={0}
        aria-label={text}
        onMouseEnter={show}
        onMouseLeave={() => setPos(null)}
        onFocus={show}
        onBlur={() => setPos(null)}
      >
        ?
      </span>
      {pos && createPortal(
        <span className="field-hint-bubble feature-hint-bubble"
          style={{ top: pos.top, left: pos.left, width: Math.min(320, window.innerWidth - 24) }}
          role="tooltip">
          {text}
        </span>,
        document.body,
      )}
    </>
  );
}

export function Tooling() {
  const [runtimes, setRuntimes] = useState<Runtime[]>([]);
  const [runtimeId, setRuntimeId] = useState("");
  const [presets, setPresets] = useState<Record<string, { max_tool_tokens: number }>>({});
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<"skills" | "mcp" | null>(null);
  const [skillOverwrite, setSkillOverwrite] = useState(false);
  const [mcpOverwrite, setMcpOverwrite] = useState(false);
  const [flags, setFlags] = useState<FeatureFlag[]>([]);
  const [flagBusy, setFlagBusy] = useState<string | null>(null);
  const [flagError, setFlagError] = useState<string | null>(null);
  const [flagNotice, setFlagNotice] = useState<string | null>(null);
  const skillDirRef = useRef<HTMLInputElement>(null);
  const skillArchiveRef = useRef<HTMLInputElement>(null);
  const mcpDirRef = useRef<HTMLInputElement>(null);
  const mcpArchiveRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    Promise.all([api.listRuntimes(), api.listToolPresets()])
      .then(async ([items, p]) => {
        const valid = items.filter((r) => r.status === "ready");
        setRuntimes(valid);
        setPresets(p);
        let preferred = valid[0]?.id || "";
        try {
          const def = await api.getDefaultRuntime();
          if (def.id && valid.some((r) => r.id === def.id)) preferred = def.id;
        } catch {
          // 旧后端没有默认运行时接口
        }
        setRuntimeId(preferred);
      })
      .catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    api.listFeatureFlags()
      .then((data) => setFlags(data.flags ?? []))
      .catch((e) => setFlagError(String(e)));
  }, []);

  const updateFlag = async (flag: FeatureFlag, value: boolean) => {
    setFlagBusy(flag.key);
    setFlagError(null);
    setFlagNotice(null);
    try {
      const next = await api.setFeatureFlag(flag.key, value);
      setFlags((prev) => prev.map((item) => item.key === next.key ? next : item));
      setFlagNotice(`已${value ? "开启" : "关闭"}「${flag.title}」，立即生效并持久化。`);
    } catch (e) {
      setFlagError(String(e));
    } finally {
      setFlagBusy(null);
    }
  };

  const upload = async (kind: "skills" | "mcp", input: HTMLInputElement) => {
    const files = Array.from(input.files || []);
    input.value = "";
    if (!files.length || !runtimeId) return;
    const paths = files.map((file) => (
      (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name
    ));
    const overwrite = kind === "skills" ? skillOverwrite : mcpOverwrite;
    setBusy(kind);
    setError(null);
    setNotice(null);
    try {
      const result = kind === "skills"
        ? await api.importSkillsBundle(runtimeId, files, paths, overwrite)
        : await api.importMcpBundle(runtimeId, files, paths, overwrite);
      const imported = (result.imported as { name?: string }[])
        .map((item) => item.name).filter(Boolean).join("、") || "无";
      const skipped = (result.skipped as { name?: string; reason?: string }[]);
      setNotice(
        `已导入到 ${runtimeId}：${imported}`
        + (skipped.length ? `；跳过 ${skipped.length} 项（${skipped.map((s) => s.name).join("、")}）` : ""),
      );
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="tooling-page">
      <PageHeader kicker="TOOLING" title="工具治理" />
      {error && <p className="error">{error}</p>}
      {notice && <p className="tooling-notice">{notice}</p>}

      <section className="feature-panel">
        <div className="feature-panel-head">
          <div>
            <h2>功能开关</h2>
            <p>
              修改后立即生效并持久化，重启后保留；只作用于后续任务/运行，不会回改已有数据。
              悬停标题旁的小问号查看完整说明和关闭影响。
            </p>
          </div>
          <span className="feature-summary">
            {flags.filter((flag) => flag.value).length}/{flags.length} 已开启
          </span>
        </div>
        {flagError && <p className="error">{flagError}</p>}
        {flagNotice && <p className="tooling-notice feature-notice">{flagNotice}</p>}
        {flags.length === 0 && !flagError ? (
          <p className="muted">加载中…</p>
        ) : (
          <div className="feature-groups">
            {FLAG_GROUPS.map((group) => {
              const items = flags.filter((flag) => flag.category === group.key);
              if (items.length === 0) return null;
              return (
                <div className="feature-group" key={group.key}>
                  <div className="feature-group-head">
                    <h3>{group.title}</h3>
                    <p>{group.hint}</p>
                  </div>
                  <div className="feature-list">
                    {items.map((flag) => (
                      <div className={`feature-row ${flag.value ? "is-on" : ""}`} key={flag.key}>
                        <div className="feature-copy">
                          <div className="feature-title-line">
                            <strong>{flag.title}</strong>
                            <FeatureHint text={flag.description} />
                            <span className="chip feature-source">
                              默认{flag.default ? "开启" : "关闭"}
                            </span>
                          </div>
                          <p>{flag.summary}</p>
                          {flagBusy === flag.key && (
                            <div className="feature-state">
                              <span className="muted">保存中…</span>
                            </div>
                          )}
                        </div>
                        <label className="feature-switch">
                          <input type="checkbox" checked={flag.value}
                            disabled={flagBusy === flag.key}
                            aria-label={`${flag.title}：${flag.description}`}
                            onChange={(e) => { void updateFlag(flag, e.currentTarget.checked); }} />
                          <span className="feature-switch-track" aria-hidden="true">
                            <span className="feature-switch-thumb" />
                          </span>
                        </label>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      <div className="tool-import-runtime">
        <label className="tooling-runtime">
          <span>导入到运行时</span>
          <select
            value={runtimeId}
            onChange={(e) => { setRuntimeId(e.target.value); setNotice(null); }}
          >
            {runtimes.length === 0 && <option value="">（没有可用运行时）</option>}
            {runtimes.map((r) => (
              <option key={r.id} value={r.id}>{r.display_name}</option>
            ))}
          </select>
        </label>
        <span className="muted">
          {runtimes.length === 0
            ? "请先在「运行时」页完成扫描或注册，再导入工具。"
            : "导入的 Skill / MCP 只对该运行时生效。"}
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
            onChange={(e) => upload("skills", e.currentTarget)} />
          <input ref={skillArchiveRef} type="file" hidden
            accept=".zip,.tar,.tar.gz,.tgz"
            onChange={(e) => upload("skills", e.currentTarget)} />
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
            onChange={(e) => upload("mcp", e.currentTarget)} />
          <input ref={mcpArchiveRef} type="file" hidden
            accept=".zip,.tar,.tar.gz,.tgz"
            onChange={(e) => upload("mcp", e.currentTarget)} />
        </div>
      </section>

      <section className="tooling-presets">
        <h2 className="pane-title">工具预设（tools.yaml）</h2>
        <ul className="drafts">
          {Object.entries(presets).map(([name, p]) => (
            <li key={name}>
              <strong>{name}</strong> <span className="muted">max_tool_tokens={p.max_tool_tokens}</span>
            </li>
          ))}
          {Object.keys(presets).length === 0 && <li className="muted">未发现预设。</li>}
        </ul>
      </section>

    </div>
  );
}
