import { useEffect, useState } from "react";
import { api, isOnboardingRole, type Role } from "../api/client";

type Tab = "basic" | "instruction" | "tools" | "catalog" | "home" | "permissions" | "runtime";

const TABS: { key: Tab; label: string }[] = [
  { key: "basic", label: "基本" },
  { key: "instruction", label: "指令" },
  { key: "tools", label: "工具" },
  { key: "catalog", label: "技能/MCP" },
  { key: "home", label: "主页" },
  { key: "permissions", label: "权限" },
  { key: "runtime", label: "运行时" },
];

export function Roles() {
  const [roles, setRoles] = useState<Role[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("basic");
  const [config, setConfig] = useState<Record<string, any> | null>(null);
  const [slug, setSlug] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [autoBrief, setAutoBrief] = useState("");
  const [autoBusy, setAutoBusy] = useState(false);
  const [catalog, setCatalog] = useState<{ skills: { name: string; description: string }[]; mcp_servers: { name: string }[] } | null>(null);
  const [selSkills, setSelSkills] = useState<string[]>([]);
  const [selMcp, setSelMcp] = useState<string[]>([]);
  const [profile, setProfile] = useState<{ system_prompt: string; responsibilities: string[] }>({ system_prompt: "", responsibilities: [] });
  const [brief, setBrief] = useState("");
  const [genResult, setGenResult] = useState<{ working_mode?: string; skill_description?: string } | null>(null);
  const [genBusy, setGenBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [homeFiles, setHomeFiles] = useState<string[]>([]);
  const [homeFile, setHomeFile] = useState<{ path: string; content: string } | null>(null);
  const [message, setMessage] = useState("");
  const [versions, setVersions] = useState<{ version: number; display_name: string; source: string; created_at: string }[]>([]);
  const [metrics, setMetrics] = useState<{ runs: number; tasks_total: number; tasks_done: number; cost_used_usd: number } | null>(null);
  const [toolsForm, setToolsForm] = useState({ preset: "", max_tool_tokens: "", allowlist: "[]" });

  const refresh = () => api.listRoles().then(setRoles).catch((e) => setError(String(e)));

  useEffect(() => {
    refresh();
  }, []);

  useEffect(() => {
    if (!selected) return;
    api.getEffectiveConfig(selected).then((c) => {
      setConfig(c);
      const tools = (c.projection as Record<string, any> | undefined)?.tools ?? {};
      setToolsForm({
        preset: tools.preset ?? "",
        max_tool_tokens: tools.max_tool_tokens == null ? "" : String(tools.max_tool_tokens),
        allowlist: JSON.stringify(tools.allowlist ?? [], null, 2),
      });
    }).catch((e) => setError(String(e)));
    api.getRoleProfile(selected).then(setProfile).catch(() => {});
    api.getRoleVersions(selected).then(setVersions).catch(() => {});
    api.getRoleMetrics(selected).then(setMetrics).catch(() => {});
    api.getRoleHome(selected).then((h) => setHomeFiles(h.files)).catch(() => {});
  }, [selected]);

  // 切到「技能/MCP」tab 时，拉运行时默认 skill/MCP 目录 + 角色当前选择
  useEffect(() => {
    if (tab !== "catalog" || !selected) return;
    const rtId = (config?.projection as Record<string, any> | undefined)?.runtime?.id;
    if (rtId) api.getRuntimeCatalog(rtId).then(setCatalog).catch((e) => setError(String(e)));
    api.getRoleCatalog(selected).then((r) => { setSelSkills(r.skills); setSelMcp(r.mcp); }).catch(() => {});
  }, [tab, selected, config]);

  const create = async () => {
    if (!slug || !displayName) return;
    try {
      await api.createRole(slug, displayName);
      setSlug("");
      setDisplayName("");
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const autoCreate = async () => {
    if (!autoBrief.trim()) return;
    setAutoBusy(true);
    setError(null);
    try {
      const r = await api.autoCreateRole(autoBrief.trim());
      setAutoBrief("");
      await refresh();
      setSelected(r.slug);
      setTab("basic");
    } catch (e) {
      setError(String(e));
    } finally {
      setAutoBusy(false);
    }
  };

  const remove = async (r: Role) => {
    if (!window.confirm(`确定删除角色「${r.display_name}（${r.slug}）」？该操作不可恢复。`)) return;
    try {
      await api.deleteRole(r.slug);
      if (selected === r.slug) setSelected(null);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const saveCatalog = async () => {
    if (!selected) return;
    try {
      await api.updateRoleCatalog(selected, selSkills, selMcp);
    } catch (e) {
      setError(String(e));
    }
  };

  const toggleSel = (list: string[], v: string, set: (x: string[]) => void) =>
    set(list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);

  const generate = async () => {
    if (!selected || !brief.trim()) return;
    setGenBusy(true);
    setError(null);
    try {
      const r = await api.generateRole(selected, brief.trim());
      setGenResult(r);
      setProfile({ system_prompt: r.system_prompt ?? "", responsibilities: r.responsibilities ?? [] });
    } catch (e) {
      setError(String(e));
    } finally {
      setGenBusy(false);
    }
  };

  const saveProfile = async () => {
    if (!selected) return;
    try {
      await api.updateRoleProfile(selected, profile.system_prompt, profile.responsibilities);
    } catch (e) {
      setError(String(e));
    }
  };

  const projection = (config?.projection ?? {}) as Record<string, any>;
  const current = roles.find((r) => r.slug === selected);

  return (
    <div className="roles">
      <h1>角色工坊</h1>
      {error && <p className="error">{error}</p>}

      <div className="toolbar">
        <input value={slug} onChange={(e) => setSlug(e.target.value)} placeholder="slug" />
        <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="显示名" />
        <button onClick={create}>新建角色</button>
      </div>
      <div className="toolbar">
        <input
          value={autoBrief}
          onChange={(e) => setAutoBrief(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && autoCreate()}
          placeholder="一句话描述角色，AI 生成能力与定位后创建：如「负责后端 API、数据库迁移和性能优化」"
          style={{ flex: 1 }}
        />
        <button className="primary" onClick={autoCreate} disabled={autoBusy || !autoBrief.trim()}>
          {autoBusy ? "生成中…" : "AI 生成并创建"}
        </button>
      </div>

      <div className="roles-layout">
        <ul className="roles-list">
          {roles.length === 0 && <li className="muted">暂无角色，先在右侧新建。</li>}
          {roles.map((r) => (
            <li key={r.slug} className={selected === r.slug ? "active" : ""}>
              <a href="#" onClick={(e) => { e.preventDefault(); setSelected(r.slug); setTab("basic"); }}>
                {r.display_name} <small>({r.slug})</small>
                {isOnboardingRole(r) && <span className="badge badge-in_progress" title="默认助理，不可删除">默认助理</span>}
              </a>
              {!isOnboardingRole(r) && (
                <button onClick={() => remove(r)} className="danger">删</button>
              )}
            </li>
          ))}
        </ul>

        <div className="roles-detail">
          {!current ? (
            <p className="muted">选择左侧角色查看详情。</p>
          ) : (
            <>
              <div className="tabs">
                {TABS.map((t) => (
                  <button key={t.key} className={tab === t.key ? "active" : ""} onClick={() => setTab(t.key)}>
                    {t.label}
                  </button>
                ))}
              </div>

              {tab === "basic" && (
                <div>
                  <dl>
                    <dt>slug</dt><dd>{current.slug}</dd>
                    <dt>显示名</dt><dd>{current.display_name}</dd>
                    <dt>home</dt><dd className="mono">{current.home_path}</dd>
                    <dt>启用</dt><dd>{current.enabled ? "是" : "否"}</dd>
                  </dl>

                  <h3>执行指标</h3>
                  {metrics ? (
                    <dl>
                      <dt>运行次数</dt><dd>{metrics.runs}</dd>
                      <dt>任务</dt><dd>{metrics.tasks_done} done / {metrics.tasks_total} 总</dd>
                      <dt>成本</dt><dd>${metrics.cost_used_usd.toFixed(4)}</dd>
                    </dl>
                  ) : <p className="muted">加载中…</p>}

                  <h3>配置版本</h3>
                  <ul className="drafts">
                    {versions.length === 0 && <li className="muted">暂无版本记录。</li>}
                    {versions.map((v) => (
                      <li key={v.version}>
                        v{v.version} · {v.source} · <span className="muted mono">{v.created_at.slice(0, 19)}</span>{" "}
                        <button
                          className="small"
                          onClick={async () => {
                            try {
                              await api.rollbackRole(current.slug, v.version);
                              await refresh();
                              const c = await api.getEffectiveConfig(current.slug);
                              setConfig(c);
                            } catch (e) {
                              setError(String(e));
                            }
                          }}
                        >回滚到此版</button>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {tab === "instruction" && (
                <div>
                  <h3>生成画像（调大模型）</h3>
                  <div className="toolbar">
                    <input value={brief} onChange={(e) => setBrief(e.target.value)} placeholder="一句话描述角色，如：负责后端 API 和数据库" style={{ flex: 1 }} />
                    <button onClick={generate} disabled={genBusy || !brief.trim()}>{genBusy ? "生成中…" : "生成"}</button>
                  </div>
                  {genResult && (
                    <dl>
                      <dt>工作模式</dt><dd>{genResult.working_mode || "—"}</dd>
                      <dt>技能描述</dt><dd>{genResult.skill_description || "—"}</dd>
                    </dl>
                  )}

                  <h3>系统提示（system prompt）</h3>
                  <textarea value={profile.system_prompt} onChange={(e) => setProfile({ ...profile, system_prompt: e.target.value })} rows={4} style={{ width: "100%" }} placeholder="该角色的系统级行为准则" />

                  <h3>职责（responsibilities）</h3>
                  <textarea value={profile.responsibilities.join("\n")} onChange={(e) => setProfile({ ...profile, responsibilities: e.target.value.split("\n").filter(Boolean) })} rows={4} style={{ width: "100%" }} placeholder="每行一条职责" />

                  <div className="toolbar" style={{ marginTop: "0.6rem" }}>
                    <button className="primary" onClick={saveProfile}>保存</button>
                  </div>
                </div>
              )}
              {tab === "tools" && (
                <div>
                  <dl>
                    <dt>裁剪精度</dt>
                    <dd>
                      {projection.runtime_capabilities?.tool_level_grant === "exact" && "✅ 精确裁剪"
                        || projection.runtime_capabilities?.tool_level_grant === "server" && "⚠️ 服务器级"
                        || projection.runtime_capabilities?.tool_level_grant === "inherit" && "⛔ 继承全局（Anvil 无法约束）"
                        || "未知"}
                    </dd>
                  </dl>
                  <div className="toolbar">
                    <input
                      value={toolsForm.preset}
                      onChange={(e) => setToolsForm({ ...toolsForm, preset: e.target.value })}
                      placeholder="preset（如 backend-default）"
                    />
                    <input
                      value={toolsForm.max_tool_tokens}
                      onChange={(e) => setToolsForm({ ...toolsForm, max_tool_tokens: e.target.value })}
                      placeholder="max_tool_tokens"
                    />
                    <button
                      className="primary"
                      onClick={async () => {
                        try {
                          let allowlist: unknown = [];
                          try {
                            allowlist = JSON.parse(toolsForm.allowlist || "[]");
                          } catch {
                            throw new Error("allowlist 不是合法 JSON");
                          }
                          await api.updateRoleTools(current.slug, {
                            preset: toolsForm.preset || null,
                            max_tool_tokens: toolsForm.max_tool_tokens === "" ? null : Number(toolsForm.max_tool_tokens),
                            allowlist,
                          });
                          setError(null);
                        } catch (e) {
                          setError(String(e));
                        }
                      }}
                    >保存工具配置</button>
                  </div>
                  <p className="muted">allowlist 结构：<code>[{`{"server":"name","tools":["tool1"]}`}]</code></p>
                  <textarea
                    value={toolsForm.allowlist}
                    onChange={(e) => setToolsForm({ ...toolsForm, allowlist: e.target.value })}
                    rows={6}
                    style={{ width: "100%" }}
                  />
                </div>
              )}
              {tab === "home" && (
                <div>
                  <p className="muted">私有工作区默认只读。直接改文件会破坏它的组织方式，请用下方输入框让它自己改。</p>
                  <ul className="roles-list">
                    {homeFiles.length === 0 && <li className="muted">暂无文件。</li>}
                    {homeFiles.map((f) => (
                      <li key={f}>
                        <a href="#" onClick={(e) => {
                          e.preventDefault();
                          api.getRoleHomeFile(current.slug, f).then(setHomeFile).catch((err) => setError(String(err)));
                        }}>{f}</a>
                      </li>
                    ))}
                  </ul>
                  {homeFile && (
                    <>
                      <h3 className="mono">{homeFile.path}</h3>
                      <pre className="run-log">{homeFile.content}</pre>
                    </>
                  )}
                  <h3>给该角色发指令</h3>
                  <div className="toolbar">
                    <input
                      value={message}
                      onChange={(e) => setMessage(e.target.value)}
                      placeholder="例如：把本周经验整理进 MEMORY.md"
                      style={{ flex: 1 }}
                    />
                    <button
                      className="primary"
                      disabled={!message.trim()}
                      onClick={async () => {
                        try {
                          await api.sendRoleMessage(current.slug, message.trim());
                          setMessage("");
                        } catch (e) {
                          setError(String(e));
                        }
                      }}
                    >发送并唤醒</button>
                  </div>
                </div>
              )}
              {tab === "permissions" && (
                <dl>
                  <dt>home</dt><dd className="mono">{projection.workspace?.home ?? "—"}</dd>
                  <dt>repos</dt><dd>{JSON.stringify(projection.workspace?.repos ?? [])}</dd>
                </dl>
              )}
              {tab === "catalog" && (
                <div>
                  {!catalog ? (
                    <p className="muted">加载运行时默认 skill/MCP 目录中…（请先在「运行时」tab 选择运行时）</p>
                  ) : (
                    <>
                      <h3>技能（skill）</h3>
                      {catalog.skills.length === 0 && <p className="muted">该运行时无可用技能。</p>}
                      <div className="catalog-list">
                        {catalog.skills.map((s) => (
                          <label key={s.name} className="role-check">
                            <input type="checkbox" checked={selSkills.includes(s.name)} onChange={() => toggleSel(selSkills, s.name, setSelSkills)} />
                            <span>{s.name}</span>
                            {s.description && <small className="muted">— {s.description}</small>}
                          </label>
                        ))}
                      </div>
                      <h3>MCP 服务器</h3>
                      {catalog.mcp_servers.length === 0 && <p className="muted">该运行时无 MCP 服务器。</p>}
                      <div className="catalog-list">
                        {catalog.mcp_servers.map((m) => (
                          <label key={m.name} className="role-check">
                            <input type="checkbox" checked={selMcp.includes(m.name)} onChange={() => toggleSel(selMcp, m.name, setSelMcp)} />
                            <span>{m.name}</span>
                          </label>
                        ))}
                      </div>
                      <button className="primary" onClick={saveCatalog}>保存选择</button>
                    </>
                  )}
                </div>
              )}
              {tab === "runtime" && (
                <dl>
                  <dt>运行时</dt><dd>{JSON.stringify(projection.runtime ?? {})}</dd>
                </dl>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
