import { useEffect, useRef, useState } from "react";
import { api, isOnboardingRole, type Role, type Runtime } from "../api/client";

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

// slug 会进入 agents/<slug>/ 路径、分支名与 CLI 参数；只允许 ASCII。
const SLUG_RE = /^[a-z0-9][a-z0-9_-]{1,63}$/;
const SLUG_HINT = "slug 只能使用小写英文字母、数字、-、_（2-64 位，且以字母或数字开头）；"
  + "不支持中文、大写、空格或其它特殊字符。";

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
  const [catalogLoading, setCatalogLoading] = useState(false);
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
  const [runtimes, setRuntimes] = useState<Runtime[]>([]);
  const [runtimeId, setRuntimeId] = useState("");
  const [roleRuntimeId, setRoleRuntimeId] = useState("");
  const generateSeq = useRef(0);

  const refresh = () => api.listRoles().then(setRoles).catch((e) => setError(String(e)));

  useEffect(() => {
    refresh();
  }, []);

  // 运行时候选：优先默认运行时，否则第一个 ready。
  useEffect(() => {
    (async () => {
      try {
        const items = await api.listRuntimes();
        setRuntimes(items);
        let preferred = items.find((r) => r.status === "ready")?.id || items[0]?.id || "";
        try {
          const def = await api.getDefaultRuntime();
          if (def.id && items.some((r) => r.id === def.id)) preferred = def.id;
        } catch {
          // 旧后端没有默认运行时接口
        }
        setRuntimeId((prev) => prev || preferred);
      } catch {
        // 运行时接口不可用时仍允许查看已有角色
      }
    })();
  }, []);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    // 切换角色时立刻清空上一个角色的状态，避免串数据 / 旧请求回填。
    generateSeq.current += 1;
    setConfig(null);
    setRoleRuntimeId("");
    setCatalog(null);
    setCatalogLoading(false);
    setSelSkills([]);
    setSelMcp([]);
    setProfile({ system_prompt: "", responsibilities: [] });
    const roleDef = roles.find((r) => r.slug === selected)?.definition as Record<string, any> | undefined;
    setGenResult(
      roleDef && (roleDef.working_mode || roleDef.skill_description)
        ? { working_mode: roleDef.working_mode, skill_description: roleDef.skill_description }
        : null,
    );
    setBrief("");
    setGenBusy(false);
    setVersions([]);
    setMetrics(null);
    setHomeFiles([]);
    setHomeFile(null);
    setMessage("");
    setToolsForm({ preset: "", max_tool_tokens: "", allowlist: "[]" });
    setError(null);
    api.getEffectiveConfig(selected).then((c) => {
      if (cancelled) return;
      setConfig(c);
      const rt = (c.projection as Record<string, any> | undefined)?.runtime;
      setRoleRuntimeId(typeof rt === "string" ? rt : (rt?.id ?? ""));
      const tools = (c.projection as Record<string, any> | undefined)?.tools ?? {};
      setToolsForm({
        preset: tools.preset ?? "",
        max_tool_tokens: tools.max_tool_tokens == null ? "" : String(tools.max_tool_tokens),
        allowlist: JSON.stringify(tools.allowlist ?? [], null, 2),
      });
    }).catch((e) => { if (!cancelled) setError(String(e)); });
    api.getRoleProfile(selected).then((p) => { if (!cancelled) setProfile(p); }).catch(() => {});
    api.getRoleVersions(selected).then((v) => { if (!cancelled) setVersions(v); }).catch(() => {});
    api.getRoleMetrics(selected).then((m) => { if (!cancelled) setMetrics(m); }).catch(() => {});
    api.getRoleHome(selected).then((h) => { if (!cancelled) setHomeFiles(h.files); }).catch(() => {});
    return () => { cancelled = true; };
  }, [selected]);

  // 切到「技能/MCP」tab 时，拉运行时可用目录 + 该角色当前选择。
  // 默认 selections 为空；每个角色独立保存，切换角色时先清空，避免旧状态残留。
  useEffect(() => {
    if (tab !== "catalog" || !selected) return;
    let cancelled = false;
    setCatalog(null);
    setSelSkills([]);
    setSelMcp([]);
    const rtId = (config?.projection as Record<string, any> | undefined)?.runtime?.id;
    if (!rtId) {
      setCatalogLoading(false);
      return () => { cancelled = true; };
    }
    setCatalogLoading(true);
    Promise.allSettled([
      api.getRuntimeCatalog(rtId).then((c) => { if (!cancelled) setCatalog(c); })
        .catch((e) => { if (!cancelled) setError(String(e)); }),
      api.getRoleCatalog(selected).then((r) => {
        if (!cancelled) { setSelSkills(r.skills); setSelMcp(r.mcp); }
      }).catch((e) => { if (!cancelled) setError(String(e)); }),
    ]).finally(() => { if (!cancelled) setCatalogLoading(false); });
    return () => { cancelled = true; };
  }, [tab, selected, config]);

  const create = async () => {
    if (!slug || !displayName) return;
    if (!SLUG_RE.test(slug)) {
      setError(SLUG_HINT);
      return;
    }
    if (!runtimeId) {
      setError("请先选择一个可用运行时（可先在「运行时」页扫描）");
      return;
    }
    try {
      const r = await api.createRole(slug, displayName, runtimeId);
      setSlug("");
      setDisplayName("");
      await refresh();
      setSelected(r.slug);
      setTab("basic");
    } catch (e) {
      setError(String(e));
    }
  };

  const autoCreate = async () => {
    if (!autoBrief.trim()) return;
    setAutoBusy(true);
    setError(null);
    try {
      const r = await api.autoCreateRole(autoBrief.trim(), runtimeId || undefined);
      setAutoBrief("");
      setGenResult(null);
      setProfile({ system_prompt: "", responsibilities: [] });
      setBrief("");
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

  const clearCatalog = async () => {
    if (!selected) return;
    setSelSkills([]);
    setSelMcp([]);
    try {
      await api.updateRoleCatalog(selected, [], []);
    } catch (e) {
      setError(String(e));
    }
  };

  const saveRuntime = async () => {
    if (!selected || !roleRuntimeId) return;
    try {
      await api.updateRoleRuntime(selected, roleRuntimeId);
      const c = await api.getEffectiveConfig(selected);
      setConfig(c);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const toggleSel = (list: string[], v: string, set: (x: string[]) => void) =>
    set(list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);

  const generate = async () => {
    if (!selected || !brief.trim()) return;
    const seq = ++generateSeq.current;
    setGenBusy(true);
    setError(null);
    try {
      const r = await api.generateRole(selected, brief.trim());
      if (seq !== generateSeq.current) return; // 已切换角色，丢弃旧响应
      setGenResult(r);
      const hasSystemPrompt = typeof r.system_prompt === "string" && r.system_prompt.trim().length > 0;
      const hasResponsibilities = Array.isArray(r.responsibilities)
        && r.responsibilities.some((x) => String(x).trim().length > 0);
      // 只覆盖有值的字段；生成结果不完整时保留人工已填/已保存的内容。
      setProfile((prev) => ({
        ...prev,
        ...(hasSystemPrompt ? { system_prompt: r.system_prompt as string } : {}),
        ...(hasResponsibilities ? { responsibilities: r.responsibilities as string[] } : {}),
      }));
      const missing: string[] = [];
      if (!hasSystemPrompt) missing.push("系统提示");
      if (!hasResponsibilities) missing.push("职责");
      if (typeof r.working_mode !== "string" || !r.working_mode.trim()) missing.push("工作模式");
      if (typeof r.skill_description !== "string" || !r.skill_description.trim()) missing.push("技能描述");
      if (missing.length) {
        setError(`生成结果不完整：${missing.join("、")} 为空；系统提示与职责已保留原内容。`);
      }
    } catch (e) {
      if (seq === generateSeq.current) setError(String(e));
    } finally {
      if (seq === generateSeq.current) setGenBusy(false);
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
  const slugInvalid = slug.length > 0 && !SLUG_RE.test(slug);

  return (
    <div className="roles">
      <h1>角色工坊</h1>
      {error && <p className="error">{error}</p>}

      <div className="toolbar">
        <input
          value={slug}
          onChange={(e) => setSlug(e.target.value)}
          placeholder="slug（小写字母/数字/-/_）"
          pattern="[a-z0-9][a-z0-9_-]{1,63}"
          maxLength={64}
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          title={SLUG_HINT}
          aria-invalid={slugInvalid}
        />
        <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="显示名" />
        <select value={runtimeId} onChange={(e) => setRuntimeId(e.target.value)} title="角色绑定的运行时">
          {runtimes.length === 0 && <option value="">（无可用运行时）</option>}
          {runtimeId && !runtimes.some((r) => r.id === runtimeId) && (
            <option value={runtimeId}>{runtimeId}（未注册）</option>
          )}
          {runtimes.map((r) => (
            <option key={r.id} value={r.id}>{r.display_name} · {r.status}</option>
          ))}
        </select>
        <button onClick={create} disabled={!slug || !displayName || !runtimeId || slugInvalid}>
          新建角色
        </button>
      </div>
      <p className={slugInvalid ? "error role-slug-hint" : "muted role-slug-hint"}>
        {slugInvalid ? SLUG_HINT : "slug：小写字母、数字、-、_，2–64 位；不支持中文/大写/空格。"}
      </p>
      <div className="toolbar">
        <input
          value={autoBrief}
          onChange={(e) => setAutoBrief(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && autoCreate()}
          placeholder="一句话描述角色，AI 生成能力与定位后创建：如「负责后端 API、数据库迁移和性能优化」"
          style={{ flex: 1 }}
        />
        <button className="primary" onClick={autoCreate} disabled={autoBusy || !autoBrief.trim() || !runtimeId}>
          {autoBusy ? "生成中…" : "AI 生成并创建"}
        </button>
      </div>
      {runtimes.length === 0 && (
        <p className="muted">没有可用运行时：请先到「运行时」页扫描，或在角色创建前新增自定义运行时。</p>
      )}

      <div className="roles-layout">
        <ul className="roles-list">
          {roles.length === 0 && <li className="muted">暂无角色，先在右侧新建。</li>}
          {roles.map((r) => (
            <li key={r.slug} className={selected === r.slug ? "active" : ""}>
              <a href="#" onClick={(e) => { e.preventDefault(); setSelected(r.slug); setTab("basic"); }}>
                {r.display_name} <small>({r.slug})</small>
                {isOnboardingRole(r) && <span className="badge badge-in_progress" title="默认助理，不可删除">默认助理</span>}
                {!isOnboardingRole(r) && !SLUG_RE.test(r.slug) && (
                  <span className="badge wf-gate" title="slug 含中文/非法字符，建议删除并重建">
                    slug 不合规
                  </span>
                )}
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
                  <p className="muted">生成会调用角色绑定的运行时；默认超时 120 秒（ANVIL_UTILITY_TIMEOUT）。</p>
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
                  <p className="muted">
                    默认全部不勾选；选择按角色独立保存，互不影响。
                    {roleRuntimeId
                      ? <> 可用项来自运行时 <strong>{roleRuntimeId}</strong>。</>
                      : " 请先在「运行时」tab 绑定运行时。"}
                  </p>
                  {catalogLoading ? (
                    <p className="muted">加载中…</p>
                  ) : !catalog ? (
                    <p className="muted">请先在「运行时」tab 绑定运行时，或该运行时没有可用的 skill/MCP 目录。</p>
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
                      <div className="toolbar">
                        <button onClick={clearCatalog}>全部不选并保存</button>
                        <button className="primary" onClick={saveCatalog}>保存选择</button>
                      </div>
                    </>
                  )}
                </div>
              )}
              {tab === "runtime" && (
                <div>
                  <h3>绑定运行时</h3>
                  <p className="muted">角色执行与「生成画像」会使用该运行时。</p>
                  <div className="toolbar">
                    <select value={roleRuntimeId} onChange={(e) => setRoleRuntimeId(e.target.value)}>
                      {runtimes.length === 0 && <option value="">（无可用运行时）</option>}
                      {roleRuntimeId && !runtimes.some((r) => r.id === roleRuntimeId) && (
                        <option value={roleRuntimeId}>{roleRuntimeId}（未注册）</option>
                      )}
                      {runtimes.map((r) => (
                        <option key={r.id} value={r.id}>{r.display_name} · {r.status}</option>
                      ))}
                    </select>
                    <button className="primary" onClick={saveRuntime} disabled={!roleRuntimeId}>保存绑定</button>
                  </div>
                  <dl>
                    <dt>当前</dt><dd>{JSON.stringify(projection.runtime ?? {})}</dd>
                  </dl>

                  <h3 style={{ marginTop: 16 }}>会话上下文</h3>
                  <p className="muted">
                    执行失败/挂起后，如果怀疑上下文已污染，先重置会话再重新派发；
                    MEMORY.md 与 notes 会保留。
                  </p>
                  <div className="toolbar">
                    <button
                      className="danger"
                      onClick={async () => {
                        if (!window.confirm("重置 Agent 会话会终止该角色的常驻进程并丢弃当前对话上下文（保留 MEMORY.md 与 notes）。确定？")) return;
                        try {
                          await api.resetRoleSession(current.slug);
                          setError(null);
                          await refresh();
                        } catch (e) {
                          setError(String(e));
                        }
                      }}
                    >重置 Agent 会话</button>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
