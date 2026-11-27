import { useEffect, useRef, useState } from "react";
import { api, isOnboardingRole, type Role, type RoleVersion, type RoleVersionDiff, type RsiCandidate, type Runtime } from "../api/client";
import { AvatarPicker } from "../components/AvatarPicker";
import { Icon } from "../components/Icon";
import { RoleAvatar } from "../components/RoleAvatar";
import { formatDateTime } from "../time";
import { PageHeader } from "../components/ui";
import { confirmDialog, promptDialog } from "../components/dialogs";

type Tab = "basic" | "instruction" | "tools" | "catalog" | "home" | "permissions" | "runtime" | "evolution";

const TABS: { key: Tab; label: string }[] = [
  { key: "basic", label: "基本" },
  { key: "instruction", label: "指令" },
  { key: "tools", label: "工具" },
  { key: "catalog", label: "技能/MCP" },
  { key: "home", label: "主页" },
  { key: "permissions", label: "权限" },
  { key: "runtime", label: "运行时" },
  { key: "evolution", label: "演化" },
];

const EFFORT_LABELS: Record<string, string> = {
  low: "低", medium: "中", high: "高", max: "最高",
};

// slug 会进入 agents/<slug>/ 路径、分支名与 CLI 参数；只允许 ASCII。
const SLUG_RE = /^[a-z0-9][a-z0-9_-]{1,63}$/;
const SLUG_HINT = "slug 只能使用小写英文字母、数字、-、_（2-64 位，且以字母或数字开头）；"
  + "不支持中文、大写、空格或其它特殊字符。";

async function copyText(value: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    // fall through to the legacy path
  }
  try {
    const area = document.createElement("textarea");
    area.value = value;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}

function VersionDiffSummary({ diff }: { diff: RoleVersionDiff }) {
  const added = diff.files.filter((f) => f.status === "added").length;
  const modified = diff.files.filter((f) => f.status === "modified").length;
  const unchanged = diff.files.filter((f) => f.status === "unchanged").length;
  return (
    <div className="version-diff">
      <p>
        当前 {diff.active_version ? `v${diff.active_version}` : "无 active 版本"}
        {" → "}目标 <strong>v{diff.target_version}</strong>
      </p>
      <div className="version-diff-stats">
        <span>文件：新增 {added} · 修改 {modified} · 未变 {unchanged}</span>
        <span>知识卡：恢复 {diff.knowledge.restore}</span>
        <span>归档 {diff.knowledge.archive.length}</span>
        <span>保留人工卡 {diff.knowledge.preserve_human.length}</span>
      </div>
      {diff.charter_changed && (
        <p className="version-diff-warn">Charter 有差异，应用版本默认不恢复 Charter。</p>
      )}
      <p className="muted">不会重新跑评估；确认后下一次 run 生效。</p>
      {diff.files.length > 0 && (
        <div className="version-diff-list">
          {diff.files.slice(0, 20).map((f) => (
            <div className="version-diff-row" key={f.path}>
              <span className={`version-diff-status ${f.status}`}>{f.status}</span>
              <code title={f.path}>{f.path}</code>
              {f.preserved_human && <span className="muted">保留人工修改</span>}
            </div>
          ))}
          {diff.files.length > 20 && (
            <p className="muted">还有 {diff.files.length - 20} 个文件未展开。</p>
          )}
        </div>
      )}
      {diff.knowledge.archive.length > 0 && (
        <details className="version-diff-details">
          <summary>将归档 {diff.knowledge.archive.length} 张 RSI 知识卡</summary>
          <div className="version-diff-list">
            {diff.knowledge.archive.slice(0, 10).map((path) => (
              <div className="version-diff-row" key={path}><code>{path}</code></div>
            ))}
          </div>
        </details>
      )}
      {diff.knowledge.preserve_human.length > 0 && (
        <details className="version-diff-details">
          <summary>保留 {diff.knowledge.preserve_human.length} 张人工修改/人工卡</summary>
          <div className="version-diff-list">
            {diff.knowledge.preserve_human.slice(0, 10).map((path) => (
              <div className="version-diff-row" key={path}><code>{path}</code></div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

export function Roles() {
  const [roles, setRoles] = useState<Role[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("basic");
  const [config, setConfig] = useState<Record<string, any> | null>(null);
  const [slug, setSlug] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [autoBrief, setAutoBrief] = useState("");
  const [autoBusy, setAutoBusy] = useState(false);
  const [catalog, setCatalog] = useState<{
    skills: { name: string; description: string }[];
    mcp_servers: {
      name: string; description?: string; hint?: string;
      transport?: string; tool_count?: number;
    }[];
  } | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [selSkills, setSelSkills] = useState<string[]>([]);
  const [selMcp, setSelMcp] = useState<string[]>([]);
  const [profile, setProfile] = useState<{ system_prompt: string; responsibilities: string[] }>({ system_prompt: "", responsibilities: [] });
  const [brief, setBrief] = useState("");
  const [genResult, setGenResult] = useState<{ working_mode?: string; skill_description?: string } | null>(null);
  const [genBusy, setGenBusy] = useState(false);
  const [newAvatar, setNewAvatar] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [avatarOpen, setAvatarOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [homeFiles, setHomeFiles] = useState<string[]>([]);
  const [homeFile, setHomeFile] = useState<{ path: string; content: string } | null>(null);
  const [message, setMessage] = useState("");
  const [versions, setVersions] = useState<RoleVersion[]>([]);
  const [versionKind, setVersionKind] = useState<"evolution" | "config">("evolution");
  const [versionStatus, setVersionStatus] = useState("");
  const [versionSelected, setVersionSelected] = useState<number[]>([]);
  const [versionPageItems, setVersionPageItems] = useState<RoleVersion[]>([]);
  const [versionTotal, setVersionTotal] = useState(0);
  const [versionPage, setVersionPage] = useState(0);
  const [versionSummary, setVersionSummary] = useState<{
    active_evolution?: number | null;
    counts?: {
      kind?: Record<string, number>; status?: Record<string, number>;
      deleted?: number;
    };
  } | null>(null);
  const [workingSet, setWorkingSet] = useState<{
    base_version: number | null; count: number; hash: string;
    cards: { path: string; sha256: string; human_modified: boolean }[];
  } | null>(null);
  const [copiedSlug, setCopiedSlug] = useState(false);
  const copyTimerRef = useRef<number | null>(null);
  const [candidates, setCandidates] = useState<RsiCandidate[]>([]);
  const [evalSuite, setEvalSuite] = useState("default");
  const [evalBusy, setEvalBusy] = useState<string | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importPreview, setImportPreview] = useState<Record<string, any> | null>(null);
  const [importMode, setImportMode] = useState("new_role");
  const [importSlug, setImportSlug] = useState("");
  const [importAllow, setImportAllow] = useState(false);
  const [importBusy, setImportBusy] = useState(false);
  const [metrics, setMetrics] = useState<{ runs: number; tasks_total: number; tasks_done: number; input_tokens: number; output_tokens: number; total_tokens: number; today_total_tokens: number } | null>(null);
  const [toolsForm, setToolsForm] = useState({ preset: "", max_tool_tokens: "", allowlist: "[]" });
  const [runtimes, setRuntimes] = useState<Runtime[]>([]);
  const [runtimeId, setRuntimeId] = useState("");
  const [roleRuntimeId, setRoleRuntimeId] = useState("");
  const [roleEffort, setRoleEffort] = useState("high");
  const generateSeq = useRef(0);

  const refresh = () => api.listRoles().then((items) => {
    // Cindy（内置 onboarding 助理）始终置顶；其余角色保持接口返回顺序。
    const ordered = [...items].sort(
      (a, b) => Number(isOnboardingRole(b)) - Number(isOnboardingRole(a)));
    setRoles(ordered);
    setSelected((prev) => {
      // 默认选中列表第一个角色；刷新后保留当前选择（角色被删除时回退到第一个）。
      if (prev && ordered.some((r) => r.slug === prev)) return prev;
      return ordered[0]?.slug ?? null;
    });
  }).catch((e) => setError(String(e)));

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
    setAvatarOpen(false);
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
    setVersionSummary(null);
    setCopiedSlug(false);
    setCandidates([]);
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
      setRoleEffort(String((typeof rt === "object" && rt?.effort) || "high"));
      const tools = (c.projection as Record<string, any> | undefined)?.tools ?? {};
      setToolsForm({
        preset: tools.preset ?? "",
        max_tool_tokens: tools.max_tool_tokens == null ? "" : String(tools.max_tool_tokens),
        allowlist: JSON.stringify(tools.allowlist ?? [], null, 2),
      });
    }).catch((e) => { if (!cancelled) setError(String(e)); });
    api.getRoleProfile(selected).then((p) => { if (!cancelled) setProfile(p); }).catch(() => {});
    api.getRoleVersions(selected)
      .then((v) => { if (!cancelled) setVersions(Array.isArray(v) ? v : []); })
      .catch(() => {});
    api.getRoleVersionSummary(selected)
      .then((s) => { if (!cancelled) setVersionSummary(s as typeof versionSummary); })
      .catch(() => {});
    api.listRsiCandidates(selected)
      .then((r) => { if (!cancelled) setCandidates(Array.isArray(r?.items) ? r.items : []); })
      .catch(() => {});
    setVersionSelected([]);
    setVersionPage(0);
    setWorkingSet(null);
    api.getRoleWorkingSet(selected).then((w) => { if (!cancelled) setWorkingSet(w); }).catch(() => {});
    api.getRoleMetrics(selected).then((m) => { if (!cancelled) setMetrics(m); }).catch(() => {});
    api.getRoleHome(selected).then((h) => { if (!cancelled) setHomeFiles(h.files); }).catch(() => {});
    return () => { cancelled = true; };
  }, [selected]);

  useEffect(() => () => {
    if (copyTimerRef.current != null) window.clearTimeout(copyTimerRef.current);
  }, []);

  useEffect(() => {
    if (!selected) {
      setVersionPageItems([]);
      setVersionTotal(0);
      return;
    }
    let cancelled = false;
    api.getRoleVersionsPage(selected, {
      kind: versionKind,
      status: versionStatus || undefined,
      limit: 50,
      offset: versionPage * 50,
    }).then((page) => {
      if (cancelled) return;
      // 兼容旧后端：分页参数未生效时可能返回数组；不能让 undefined 进入渲染。
      const items = Array.isArray(page) ? page : (page?.items ?? []);
      setVersionPageItems(Array.isArray(items) ? items : []);
      setVersionTotal(Array.isArray(page) ? items.length : (page?.total ?? items.length));
    }).catch(() => {
      if (!cancelled) {
        setVersionPageItems([]);
        setVersionTotal(0);
      }
    });
    return () => { cancelled = true; };
  }, [selected, versionKind, versionStatus, versionPage]);

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
      const r = await api.createRole(slug, displayName, runtimeId, newAvatar);
      setSlug("");
      setDisplayName("");
      setNewAvatar(null);
      await refresh();
      setSelected(r.slug);
      setTab("basic");
      setCreateOpen(false);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  };

  const autoCreate = async () => {
    if (!autoBrief.trim()) return;
    setAutoBusy(true);
    setError(null);
    try {
      const r = await api.autoCreateRole(autoBrief.trim(), runtimeId || undefined, newAvatar);
      setAutoBrief("");
      setNewAvatar(null);
      setGenResult(null);
      setProfile({ system_prompt: "", responsibilities: [] });
      setBrief("");
      await refresh();
      setSelected(r.slug);
      setTab("basic");
      setCreateOpen(false);
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setAutoBusy(false);
    }
  };

  const remove = async (r: Role) => {
    const ok = await confirmDialog({
      title: "删除角色？",
      message: `将删除「${r.display_name}（${r.slug}）」，该操作不可恢复。`
        + " 若该角色仍有活跃会话/运行，请先释放；删除会被拒绝。",
      confirmLabel: "删除角色",
      danger: true,
    });
    if (!ok) return;
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

  const selectAllCatalog = () => {
    if (!catalog) return;
    setSelSkills(catalog.skills.map((s) => s.name));
    setSelMcp(catalog.mcp_servers.map((m) => m.name));
  };

  const clearCatalog = () => {
    setSelSkills([]);
    setSelMcp([]);
  };

  const saveRuntime = async () => {
    if (!selected || !roleRuntimeId) return;
    try {
      await api.updateRoleRuntime(selected, roleRuntimeId, roleEffort);
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
  const copySlug = async (value: string) => {
    if (!(await copyText(value))) {
      setError("复制失败，请手动选择 Slug 文本。");
      return;
    }
    setCopiedSlug(true);
    if (copyTimerRef.current != null) window.clearTimeout(copyTimerRef.current);
    copyTimerRef.current = window.setTimeout(() => setCopiedSlug(false), 1500);
  };

  const projection = (config?.projection ?? {}) as Record<string, any>;
  const currentRuntime = typeof projection.runtime === "string"
    ? projection.runtime
    : projection.runtime?.id ?? "—";
  const current = roles.find((r) => r.slug === selected);
  const permissionHome = String(
    projection.workspace?.home || current?.home_path || "—");
  const permissionRepos = Array.isArray(projection.workspace?.repos)
    ? projection.workspace.repos
    : [];
  const permissionRepoText = permissionRepos.map((r: any) => (
    typeof r === "string"
      ? r
      : `${r.name ?? r.repo ?? r.slug ?? ""}${r.access ? `（${r.access}）` : ""}`
  )).filter(Boolean).join("、");
  const slugInvalid = slug.length > 0 && !SLUG_RE.test(slug);
  const visibleVersions = versionPageItems;
  const configCount = versionSummary?.counts?.kind?.config ?? 0;
  const deletedCount = versionSummary?.counts?.deleted ?? 0;
  const rsiRoleEnabled = Boolean(
    (current?.definition as Record<string, any> | undefined)?.rsi?.enabled);
  const currentEvolution = versions.find(
    (v) => (v.kind || "evolution") === "evolution" && v.status === "active");
  const latestConfig = versions.find(
    (v) => (v.kind || "evolution") === "config" && v.status !== "deleted");
  const toggleVersion = (version: number) => {
    setVersionSelected((prev) => prev.includes(version)
      ? prev.filter((x) => x !== version)
      : [...prev, version]);
  };
  const refreshVersions = async () => {
    if (!selected) return;
    const [v, w, page, summary] = await Promise.all([
      api.getRoleVersions(selected),
      api.getRoleWorkingSet(selected),
      api.getRoleVersionsPage(selected, {
        kind: versionKind, status: versionStatus || undefined,
        limit: 50, offset: versionPage * 50,
      }),
      api.getRoleVersionSummary(selected),
    ]);
    const items = Array.isArray(page) ? page : (page?.items ?? []);
    setVersions(Array.isArray(v) ? v : []);
    setVersionSummary(summary as typeof versionSummary);
    setWorkingSet(w ?? null);
    setVersionPageItems(Array.isArray(items) ? items : []);
    setVersionTotal(Array.isArray(page) ? items.length : (page?.total ?? items.length));
    setVersionSelected([]);
  };
  const refreshCandidates = async () => {
    if (!selected) return;
    try {
      const r = await api.listRsiCandidates(selected);
      setCandidates(Array.isArray(r?.items) ? r.items : []);
    } catch (e) {
      setError(String(e));
    }
  };
  const downloadVersion = async (version: number) => {
    if (!selected) return;
    try {
      const blob = await api.exportRoleVersion(selected, version);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `role-${selected}-v${version}.anvil.zip`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(String(e));
    }
  };
  const purgeSelected = async () => {
    if (!selected || versionSelected.length === 0) return;
    try {
      const dry = await api.purgeRoleVersions(selected, versionSelected, true);
      const skipped = dry.skipped.length
        ? `\n跳过：${dry.skipped.map((x) => `v${x.version}(${x.reason})`).join("、")}`
        : "";
      if (dry.deleted.length === 0) {
        setError(`没有可删除版本${skipped}`);
        return;
      }
      if (!(await confirmDialog({
        title: "批量删除版本",
        message: `将删除 ${dry.deleted.length} 个版本（soft delete + 删快照）${skipped}`,
        confirmLabel: "删除",
        danger: true,
      }))) return;
      await api.purgeRoleVersions(selected, versionSelected, false);
      await refreshVersions();
    } catch (e) {
      setError(String(e));
    }
  };
  const dedupeConfigVersions = async () => {
    if (!selected) return;
    try {
      const dry = await api.dedupeConfigVersions(selected, true);
      if (dry.candidates === 0) {
        setMessage("没有重复的配置修订。");
        return;
      }
      const skipped = dry.skipped.length
        ? `\n跳过：${dry.skipped.map((x) => `v${x.version}(${x.reason})`).join("、")}`
        : "";
      if (!(await confirmDialog({
        title: "清理重复配置修订",
        message: `检测到 ${dry.duplicate_groups} 组重复；将 soft delete `
          + `${dry.deleted.length} 条旧修订，每组保留最新一条（共保留 ${dry.kept.length} 条）。`
          + skipped,
        confirmLabel: "清理重复项",
        danger: true,
      }))) return;
      await api.dedupeConfigVersions(selected, false);
      await refreshVersions();
      setMessage("重复配置修订已清理（soft delete，可审计）。");
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="roles roles-page">
      <PageHeader
        kicker="CREW WORKSHOP"
        title="角色工坊"
        subtitle={`${roles.length} 个角色`}
        actions={(
          <button type="button" className="primary" onClick={() => setCreateOpen(true)}>
            <Icon name="plus" /> 新建角色
          </button>
        )}
      />
      {error && !createOpen && <p className="error">{error}</p>}

      {createOpen && (
        <div
          className="modal-overlay"
          onMouseDown={(e) => e.target === e.currentTarget && setCreateOpen(false)}
        >
          <div className="modal create-role-modal modal-flex" role="dialog" aria-modal="true" aria-label="新建角色">
            <div className="modal-head">
              <h2>新建角色</h2>
              <button
                type="button"
                className="modal-close"
                onClick={() => setCreateOpen(false)}
                aria-label="关闭"
              ><Icon name="x" size={12} /></button>
            </div>
            <div className="modal-flex-body">
            {error && <p className="error">{error}</p>}
      <section className="create-role-panel">
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
          <input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="显示名"
          />
          <select
            value={runtimeId}
            onChange={(e) => setRuntimeId(e.target.value)}
            title="角色绑定的运行时"
          >
            {runtimes.length === 0 && <option value="">（无可用运行时）</option>}
            {runtimeId && !runtimes.some((r) => r.id === runtimeId) && (
              <option value={runtimeId}>{runtimeId}（未注册）</option>
            )}
            {runtimes.map((r) => (
              <option key={r.id} value={r.id}>{r.display_name} · {r.status}</option>
            ))}
          </select>
        </div>
        <p className={slugInvalid ? "error role-slug-hint" : "muted role-slug-hint"}>
          {slugInvalid ? SLUG_HINT : "slug：小写字母、数字、-、_，2–64 位；不支持中文/大写/空格。"}
        </p>
        <div className="toolbar">
          <input
            value={autoBrief}
            onChange={(e) => setAutoBrief(e.target.value)}
            onKeyDown={(e) => {
              if (e.nativeEvent.isComposing || e.keyCode === 229) return;
              if (e.key === "Enter") autoCreate();
            }}
            placeholder="一句话描述角色，AI 生成能力与定位后创建：如「负责后端 API、数据库迁移和性能优化」"
            style={{ flex: 1 }}
          />
          <button
            className="primary"
            onClick={autoCreate}
            disabled={autoBusy || !autoBrief.trim() || !runtimeId}
          >
            {autoBusy ? "生成中…" : "AI 生成并创建"}
          </button>
        </div>
        {runtimes.length === 0 && (
          <p className="muted">
            没有可用运行时：请先到「运行时」页扫描，或在角色创建前新增自定义运行时。
          </p>
        )}
        <AvatarPicker
          mode="inline"
          slug={slug || displayName || "new-role"}
          name={displayName || "新角色"}
          value={newAvatar}
          onChange={setNewAvatar}
        />
      </section>
            </div>
        <div className="role-create-footer modal-actions">
          <button type="button" onClick={() => setCreateOpen(false)}>取消</button>
          <button
            type="button"
            className="primary"
            onClick={create}
            disabled={!slug || !displayName || !runtimeId || slugInvalid}
          >
            创建角色
          </button>
        </div>
          </div>
        </div>
      )}

      <div className="roles-layout">
        <ul className="roles-list role-cards">
          {roles.length === 0 && <li className="muted">暂无角色，点击右上角「新建角色」创建。</li>}
          {roles.map((r) => (
            <li key={r.slug} className={selected === r.slug ? "active" : ""}>
              <a
                href="#"
                className="role-list-main"
                onClick={(e) => { e.preventDefault(); setSelected(r.slug); setTab("basic"); }}
              >
                <RoleAvatar
                  slug={r.slug}
                  name={r.display_name}
                  avatar={r.avatar}
                  color={r.avatar_color}
                  size={40}
                />
                <span className="role-list-text">
                  <span className="role-list-name">{r.display_name}</span>
                  <span className="role-list-sub">
                    <span className="role-list-slug">{r.slug}</span>
                    {isOnboardingRole(r) && <span className="badge badge-in_progress" title="默认助理，不可删除">默认助理</span>}
                    {!isOnboardingRole(r) && !SLUG_RE.test(r.slug) && (
                      <span className="badge wf-gate" title="slug 含中文/非法字符，建议删除并重建">slug 不合规</span>
                    )}
                  </span>
                </span>
              </a>
              {!isOnboardingRole(r) && (
                <button
                  type="button"
                  onClick={() => remove(r)}
                  className="danger role-delete-btn"
                  aria-label={`删除角色 ${r.display_name}`}
                  title={`删除 ${r.display_name}`}
                >删</button>
              )}
            </li>
          ))}
        </ul>

        <div className="roles-detail">
          {!current ? (
            <p className="muted">
              {roles.length === 0
                ? "暂无角色，点击右上角「新建角色」创建。"
                : "选择左侧角色查看详情。"}
            </p>
          ) : (
            <>
              <div className="role-detail-head">
                <button
                  type="button"
                  className="role-detail-avatar"
                  onClick={() => setAvatarOpen(true)}
                  aria-label="更换角色头像"
                  title="更换头像"
                >
                  <RoleAvatar
                    slug={current.slug}
                    name={current.display_name}
                    avatar={current.avatar}
                    color={current.avatar_color}
                    size={52}
                  />
                </button>
                <div className="role-detail-title">
                  <h2>{current.display_name}</h2>
                  <span className="mono">{current.slug}</span>
                </div>
              </div>
              <div className="tabs">
                {TABS.map((t) => (
                  <button key={t.key} className={tab === t.key ? "active" : ""} onClick={() => setTab(t.key)}>
                    {t.label}
                  </button>
                ))}
              </div>

              <div className="role-detail-body">
              {tab === "basic" && (
                <div className="role-basic">
                  <section className="role-info-card">
                    <div className="role-info-grid role-info-grid-basic">
                      <div className="role-info-item role-info-item-slug">
                        <span>Slug</span>
                        <div className="role-info-code-row">
                          <code tabIndex={0} title={current.slug}>{current.slug}</code>
                          <button
                            type="button"
                            className="small role-copy-btn"
                            onClick={() => { void copySlug(current.slug); }}
                            aria-label={copiedSlug ? "Slug 已复制" : `复制 Slug ${current.slug}`}
                            title="复制 Slug"
                          >{copiedSlug ? "已复制" : "复制"}</button>
                          <span role="status" aria-live="polite" className="role-copy-status">
                            {copiedSlug ? "Slug 已复制到剪贴板" : ""}
                          </span>
                        </div>
                      </div>
                      <div className="role-info-item">
                        <span>显示名</span>
                        <strong>{current.display_name}</strong>
                      </div>
                      <div className="role-info-item">
                        <span>状态</span>
                        <strong className={current.enabled ? "role-state on" : "role-state off"}>
                          {current.enabled ? "已启用" : "已停用"}
                        </strong>
                      </div>
                      <div className="role-info-item role-info-wide">
                        <span>工作目录</span>
                        <code title={current.home_path}>{current.home_path}</code>
                      </div>
                    </div>
                  </section>

                  <section className="role-detail-section">
                    <div className="role-section-head">
                      <h3>执行指标</h3>
                    </div>
                    {metrics ? (
                      <div className="role-metrics">
                        <div className="role-metric">
                          <span>运行次数</span>
                          <strong>{metrics.runs}</strong>
                        </div>
                        <div className="role-metric">
                          <span>完成任务</span>
                          <strong>{metrics.tasks_done}<small> / {metrics.tasks_total}</small></strong>
                        </div>
                        <div className="role-metric">
                          <span>累计 Token</span>
                          <strong>{metrics.total_tokens.toLocaleString()}</strong>
                        </div>
                      </div>
                    ) : <p className="muted">加载中…</p>}
                  </section>

                  <section className="role-detail-section">
                    <div className="role-section-head">
                      <h3>角色空间</h3>
                      <button className="small" onClick={() => setTab("evolution")}>
                        前往演化
                      </button>
                    </div>
                    <div className="role-metrics">
                      <div className="role-metric">
                        <span>当前演进版本</span>
                        <strong>
                          {currentEvolution ? `v${currentEvolution.version}` : "未建立"}
                        </strong>
                      </div>
                      <div className="role-metric">
                        <span>工作集</span>
                        <strong>{workingSet?.count ?? 0}<small> 张未版本化知识卡</small></strong>
                      </div>
                      <div className="role-metric">
                        <span>配置修订</span>
                        <strong>{configCount}<small> 个（默认折叠）</small></strong>
                      </div>
                      <div className="role-metric">
                        <span>最近配置修订</span>
                        <strong>
                          {latestConfig
                            ? `v${latestConfig.version}`
                            : "—"}
                          {latestConfig && (
                            <small> {latestConfig.source} · {formatDateTime(latestConfig.created_at)}</small>
                          )}
                        </strong>
                      </div>
                    </div>
                    <p className="muted">
                      基本信息页只做摘要。版本切换、配置恢复、导出/导入和批量清理统一在
                      “演化”页处理；配置修订默认不进入演进主视图。
                    </p>
                  </section>
                </div>
              )}
              {tab === "evolution" && (
                <div>
                  <p className="muted" style={{ margin: "0 0 0.6rem" }}>
                    这里管理角色的 RSI 演进：当前基线、每日收割但尚未固化的工作集、
                    反思候选与回放评估、以及可切换/导出的演进版本。任务执行不在这里，
                    打开 RSI 前此页只读。
                  </p>
                  <label className="muted" style={{
                    display: "inline-flex", alignItems: "center", gap: 6,
                    marginBottom: "0.6rem",
                  }}>
                    <input
                      type="checkbox"
                      checked={rsiRoleEnabled}
                      onChange={async (e) => {
                        const enabled = e.target.checked;
                        try {
                          await api.setRoleRsiEnabled(current.slug, enabled);
                          if (enabled) {
                            try { await api.ensureRoleBaseline(current.slug); }
                            catch { /* baseline may already exist */ }
                          }
                          await refresh();
                          await refreshVersions();
                        } catch (err) {
                          setError(String(err));
                        }
                      }}
                    />
                    本角色参与 RSI 试点（仍需全局 role_rsi 开关）
                  </label>
                  <section className="role-detail-section">
                    <div className="role-section-head">
                      <h3>当前基线</h3>
                      <span className="muted">
                        {currentEvolution ? `v${currentEvolution.version}` : "尚未建立版本"}
                      </span>
                    </div>
                    <div className="role-metrics">
                      <div className="role-metric">
                        <span>工作集</span>
                        <strong>{workingSet?.count ?? 0}<small> 张未版本化知识卡</small></strong>
                      </div>
                      <div className="role-metric">
                        <span>工作集 hash</span>
                        <strong className="mono" style={{ fontSize: "0.8rem" }}>
                          {workingSet?.hash ? workingSet.hash.slice(0, 18) : "—"}
                        </strong>
                      </div>
                      <div className="role-metric">
                        <span>演进版本</span>
                        <strong>
                          {versions.length}
                          <small>
                            {" "}个（配置修订 {configCount} 个
                            {deletedCount > 0 ? `，已清理 ${deletedCount} 条重复` : ""}）
                          </small>
                        </strong>
                      </div>
                    </div>
                    {!currentEvolution && (
                      <div style={{ marginTop: "0.6rem" }}>
                        <p className="muted">
                          当前还没有演进基线。历史手动保存的
                          {configCount > 0 ? ` ${configCount} 个配置修订` : "配置修订"}
                          不会混入演进视图；建立基线后，RSI 才能从这一版开始演进。
                        </p>
                        <button
                          className="primary small"
                          onClick={async () => {
                            try {
                              await api.ensureRoleBaseline(selected!);
                              await refreshVersions();
                              setMessage("已建立当前角色空间的演进基线。");
                            } catch (e) {
                              setError(String(e));
                            }
                          }}
                        >建立当前基线</button>
                      </div>
                    )}
                    {workingSet && workingSet.count > 0 && (
                      <p className="muted">
                        每日收割的知识卡属于工作集，不会自动改变基线；晋级或切换时按规则固化/归档。
                      </p>
                    )}
                  </section>

                  <section className="role-detail-section">
                    <div className="role-section-head">
                      <h3>演化候选</h3>
                      <span className="muted">
                        {candidates.filter((c) => ["draft", "queued", "evaluating", "pending"].includes(c.status)).length} 个待处理
                      </span>
                    </div>
                    <div className="toolbar" style={{ flexWrap: "wrap" }}>
                      <input
                        value={evalSuite}
                        onChange={(e) => setEvalSuite(e.target.value)}
                        placeholder="隐藏回放集 id（如 default）"
                        style={{ minWidth: 220 }}
                      />
                      <button className="small" onClick={refreshCandidates}>刷新候选</button>
                    </div>
                    {candidates.length === 0 ? (
                      <p className="muted">暂无演化候选。触发反思后，候选会出现在这里与 Inbox。</p>
                    ) : (
                      <ul className="role-version-list">
                        {candidates.map((c) => (
                          <li key={c.id} style={{ alignItems: "center", gap: "0.4rem", flexWrap: "wrap" }}>
                            <span className="badge" style={{ background: "#e3f2fd", padding: "0 0.4rem", borderRadius: 4 }}>
                              {c.status}
                            </span>
                            <span className="muted">{c.tier}</span>
                            <span className="mono" style={{ fontSize: "0.78rem" }}>{c.id}</span>
                            <span className="muted">编辑 {(c.edits || []).length}</span>
                            {c.base_version != null && <span className="muted">base v{c.base_version}</span>}
                            {c.eval_id && <span className="muted mono">eval {c.eval_id.slice(0, 10)}</span>}
                            <span className="muted mono">{formatDateTime(c.created_at)}</span>
                            {["draft", "queued"].includes(c.status) && (
                              <button
                                className="small"
                                disabled={evalBusy === c.id || !evalSuite.trim()}
                                onClick={async () => {
                                  try {
                                    setEvalBusy(c.id);
                                    const r = await api.evaluateRsiCandidate(selected!, {
                                      suite_id: evalSuite.trim(), candidate_id: c.id,
                                    });
                                    setMessage(`评估已入队：${r.run_id}（候选 v${r.candidate_version}）`);
                                    await refreshCandidates();
                                  } catch (e) {
                                    setError(String(e));
                                  } finally {
                                    setEvalBusy(null);
                                  }
                                }}
                              >{evalBusy === c.id ? "入队中…" : "评估"}</button>
                            )}
                            {["draft", "pending"].includes(c.status) && (
                              <button
                                className="primary small"
                                onClick={async () => {
                                  const ok = await confirmDialog({
                                    title: "批准并应用候选？",
                                    message: `将应用 ${c.id} 的编辑并生成新的 active 演进版本；`
                                      + "开启评估时必须有 promote 结论。",
                                    confirmLabel: "批准应用",
                                  });
                                  if (!ok) return;
                                  try {
                                    await api.approveRsiCandidate(c.id);
                                    await refreshCandidates();
                                    await refreshVersions();
                                  } catch (e) {
                                    setError(String(e));
                                  }
                                }}
                              >批准应用</button>
                            )}
                            {!["rejected", "applied", "rolled_back"].includes(c.status) && (
                              <button
                                className="small"
                                onClick={async () => {
                                  const note = await promptDialog({
                                    title: "拒绝候选",
                                    message: "填写拒绝理由（写入审计与负结果归档）。",
                                    input: { placeholder: "例如：收益在噪声内 / 成本回归" },
                                    confirmLabel: "拒绝",
                                  });
                                  if (note === null) return;
                                  try {
                                    await api.rejectRsiCandidate(c.id, note);
                                    await refreshCandidates();
                                  } catch (e) {
                                    setError(String(e));
                                  }
                                }}
                              >拒绝</button>
                            )}
                          </li>
                        ))}
                      </ul>
                    )}
                  </section>

                  <section className="role-detail-section">
                    <div className="role-section-head">
                      <h3>版本管理</h3>
                      <span className="muted">{versionTotal} 个 · 第 {versionPage + 1} 页</span>
                    </div>
                    <div className="toolbar" style={{ flexWrap: "wrap" }}>
                      <select value={versionKind} onChange={(e) => { setVersionKind(e.target.value as "evolution" | "config"); setVersionPage(0); }}>
                        <option value="evolution">演进版本</option>
                        <option value="config">配置修订</option>
                      </select>
                      <select value={versionStatus} onChange={(e) => { setVersionStatus(e.target.value); setVersionPage(0); }}>
                        <option value="">全部状态</option>
                        <option value="active">当前</option>
                        <option value="candidate">候选</option>
                        <option value="archived">已归档</option>
                        <option value="rejected">已拒绝</option>
                        <option value="deleted">已删除</option>
                      </select>
                      <button
                        className="small"
                        disabled={versionSelected.length === 0}
                        onClick={purgeSelected}
                      >批量删除（{versionSelected.length}）</button>
                      <button className="small" onClick={dedupeConfigVersions}>
                        清理重复配置修订
                      </button>
                      <button className="small" onClick={() => setImportOpen((v) => !v)}>
                        导入角色版本
                      </button>
                      <button className="small" disabled={versionPage === 0}
                              onClick={() => setVersionPage((p) => Math.max(0, p - 1))}>上一页</button>
                      <button className="small" disabled={(versionPage + 1) * 50 >= versionTotal}
                              onClick={() => setVersionPage((p) => p + 1)}>下一页</button>
                    </div>
                    <div className="role-version-legend">
                      {versionKind === "evolution" ? (
                        <>
                          <span id="version-help-apply"><strong>应用</strong>：把该演进快照设为当前版本（Strategy/Experience；Charter 默认不动）。</span>
                          <span id="version-help-reapply"><strong>重新应用</strong>：对当前 active 版本执行同一操作，修复漂移；不会重跑评估。</span>
                          <span id="version-help-export"><strong>导出 zip</strong>：下载 role@version 的完整演进快照。</span>
                        </>
                      ) : (
                        <span id="version-help-restore"><strong>恢复配置</strong>：恢复手动配置修订（画像/策略文件/工具快照），不改变当前演进版本。</span>
                      )}
                    </div>

                    {importOpen && (
                      <div style={{
                        border: "1px solid var(--border, #ddd)", borderRadius: 8,
                        padding: "0.8rem", margin: "0.6rem 0", display: "grid", gap: "0.5rem",
                      }}>
                        <strong>导入 role@version</strong>
                        <input
                          type="file"
                          accept=".zip,.anvil.zip"
                          onChange={async (e) => {
                            const file = e.target.files?.[0] || null;
                            setImportFile(file);
                            setImportPreview(null);
                            if (!file) return;
                            try {
                              setImportPreview(await api.previewRoleImport(file));
                            } catch (err) {
                              setError(String(err));
                            }
                          }}
                        />
                        {importPreview && (
                          <div style={{ fontSize: "0.85rem" }}>
                            <div>
                              来源：<code>{String(importPreview.export?.slug)}@v{String(importPreview.export?.version)}</code>
                              {" · "}文件 {Object.keys(importPreview.files || {}).length}
                              {" · "}知识卡 {importPreview.manifest?.knowledge_cards?.length ?? 0}
                            </div>
                            {importPreview.warnings?.length > 0 && (
                              <div className="muted">依赖：{importPreview.warnings.join("；")}</div>
                            )}
                          </div>
                        )}
                        <div className="toolbar" style={{ flexWrap: "wrap" }}>
                          <select value={importMode} onChange={(e) => setImportMode(e.target.value)}>
                            <option value="new_role">导入为新角色</option>
                            <option value="add_version">作为现有角色候选版本</option>
                          </select>
                          <input
                            value={importSlug}
                            onChange={(e) => setImportSlug(e.target.value)}
                            placeholder={importMode === "add_version" ? "目标角色 slug" : "新角色 slug（可空）"}
                            style={{ minWidth: 180 }}
                          />
                          <label className="muted">
                            <input
                              type="checkbox"
                              checked={importAllow}
                              onChange={(e) => setImportAllow(e.target.checked)}
                            /> 允许 Charter 差异
                          </label>
                          <button
                            className="primary"
                            disabled={!importFile || importBusy}
                            onClick={async () => {
                              if (!importFile) return;
                              try {
                                setImportBusy(true);
                                await api.importRole(
                                  importFile, importMode,
                                  importSlug.trim() || undefined, importAllow);
                                setImportOpen(false);
                                setImportFile(null);
                                setImportPreview(null);
                                await refresh();
                                await refreshVersions();
                              } catch (err) {
                                setError(String(err));
                              } finally {
                                setImportBusy(false);
                              }
                            }}
                          >{importBusy ? "导入中…" : "确认导入"}</button>
                        </div>
                        <p className="muted" style={{ margin: 0 }}>
                          导入不会自动启用或切换；add_version 只生成候选版本。
                        </p>
                      </div>
                    )}

                    {visibleVersions.length === 0 ? (
                      <p className="muted">
                        {versionKind === "config"
                          ? "暂无配置修订。"
                          : configCount > 0
                            ? `暂无演进版本。检测到 ${configCount} 个历史手动配置修订，`
                              + "可在上方切到「配置修订」查看或批量清理。"
                            : "暂无演进版本。启用 RSI 并建立基线后，候选与晋级版本会出现在这里。"}
                      </p>
                    ) : (
                      <ul className="role-version-list">
                        {visibleVersions.map((v) => {
                          const isCurrent = v.status === "active";
                          const isRsi = String(v.source || "").startsWith("rsi");
                          const isBaseline = v.source === "baseline";
                          const deletable = !isCurrent && !isBaseline && v.status !== "deleted";
                          const evalVerdict = v.eval?.verdict;
                          return (
                            <li key={v.version} style={{ alignItems: "center", gap: "0.4rem", flexWrap: "wrap" }}>
                              <input
                                type="checkbox"
                                disabled={!deletable}
                                checked={versionSelected.includes(v.version)}
                                onChange={() => toggleVersion(v.version)}
                              />
                              <span className="role-version-no">v{v.version}</span>
                              {isCurrent && <span className="badge" style={{ background: "#e8f5e9", padding: "0 0.4rem", borderRadius: 4 }}>当前</span>}
                              {isRsi && <span className="badge" style={{ background: "#e3f2fd", padding: "0 0.4rem", borderRadius: 4 }}>RSI</span>}
                              {isBaseline && <span className="badge" style={{ background: "#f3e5f5", padding: "0 0.4rem", borderRadius: 4 }}>基线</span>}
                              {v.kind === "config" && <span className="badge" style={{ background: "#f5f5f5", padding: "0 0.4rem", borderRadius: 4 }}>配置修订</span>}
                              {v.status && v.status !== "active" && (
                                <span className="muted">{v.status}</span>
                              )}
                              {evalVerdict && (
                                <span className="mono" style={{ fontSize: "0.78rem" }}>{evalVerdict}</span>
                              )}
                              <span className="role-version-source">{v.source}</span>
                              <span className="muted mono">{formatDateTime(v.created_at)}</span>
                              {v.content_hash && (
                                <span className="muted mono" title={v.content_hash}>
                                  {v.content_hash.replace("sha256:", "").slice(0, 10)}
                                </span>
                              )}
                              {v.integrity_status === "corrupt" && (
                                <span style={{ color: "#c62828" }}>损坏</span>
                              )}
                              <button
                                className="small role-version-btn"
                                title={(v.kind || "evolution") === "evolution"
                                  ? (isCurrent
                                    ? "把当前 active 快照重新写回角色空间；会归档未版本化 RSI 卡，不会重新跑评估"
                                    : "把该演进快照设为当前版本；下一次 run 生效")
                                  : "恢复该手动配置修订；不改变当前演进版本"}
                                aria-label={(v.kind || "evolution") === "evolution"
                                  ? (isCurrent ? `重新应用 v${v.version}` : `应用 v${v.version}`)
                                  : `恢复配置 v${v.version}`}
                                aria-describedby={(v.kind || "evolution") === "evolution"
                                  ? (isCurrent ? "version-help-reapply" : "version-help-apply")
                                  : "version-help-restore"}
                                onClick={async () => {
                                  if (!selected) return;
                                  try {
                                    if ((v.kind || "evolution") === "evolution") {
                                      const diff = await api.getRoleVersionDiff(
                                        selected, v.version);
                                      const ok = await confirmDialog({
                                        title: isCurrent
                                          ? "重新应用当前版本？"
                                          : `应用 v${v.version}？`,
                                        message: <VersionDiffSummary diff={diff} />,
                                        confirmLabel: isCurrent
                                          ? "重新应用"
                                          : `应用 v${v.version}`,
                                        danger: true,
                                      });
                                      if (!ok) return;
                                      await api.switchRoleVersion(selected, v.version);
                                    } else {
                                      const dry = await api.restoreRoleConfig(
                                        selected, v.version, true);
                                      const warnings = dry.warnings.length
                                        ? `\n注意：${dry.warnings.join("；")}`
                                        : "";
                                      const ok = await confirmDialog({
                                        title: "恢复配置修订？",
                                        message: `将恢复 v${v.version} 的画像定义`
                                          + ` 与 ${dry.restored_files.length} 个策略文件；`
                                          + `工具配置${dry.tools_restored ? "会" : "不会"}恢复。`
                                          + `\n不会改变当前演进版本。`
                                          + warnings,
                                        confirmLabel: "恢复配置",
                                        danger: true,
                                      });
                                      if (!ok) return;
                                      await api.restoreRoleConfig(
                                        selected, v.version, false);
                                    }
                                    await refreshVersions();
                                    await refresh();
                                    const c = await api.getEffectiveConfig(selected);
                                    setConfig(c);
                                  } catch (err) {
                                    setError(String(err));
                                  }
                                }}
                              >{(v.kind || "evolution") === "evolution"
                                ? (isCurrent ? "重新应用" : "应用")
                                : "恢复配置"}</button>
                              {(v.kind || "evolution") === "evolution" && (
                                <button
                                  className="small role-version-btn"
                                  title="下载该演进版本的 role@version zip（Strategy/Experience + manifest）"
                                  aria-label={`导出 v${v.version} zip`}
                                  aria-describedby="version-help-export"
                                  onClick={() => downloadVersion(v.version)}
                                >导出 zip</button>
                              )}
                            </li>
                          );
                        })}
                      </ul>
                    )}
                  </section>
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
                      {projection.runtime_capabilities?.tool_level_grant === "exact" && "精确裁剪"
                        || projection.runtime_capabilities?.tool_level_grant === "server" && "服务器级"
                        || projection.runtime_capabilities?.tool_level_grant === "inherit" && "继承全局（Anvil 无法约束）"
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
                <section className="role-info-card">
                  <div className="role-info-grid">
                    <div className="role-info-item role-info-wide">
                      <span>角色工作目录（home）</span>
                      <code>{permissionHome}</code>
                    </div>
                    <div className="role-info-item role-info-wide">
                      <span>角色级可写仓库白名单</span>
                      {permissionRepoText ? (
                        <code>{permissionRepoText}</code>
                      ) : (
                        <span className="muted">
                          未设置白名单（不限制仓库；仍受任务 scope 和 .anvilignore 约束）
                        </span>
                      )}
                    </div>
                  </div>
                  <p className="muted" style={{ margin: "0.7rem 0 0" }}>
                    这是只读摘要。实际写入范围 = 任务 repo + 这里列出的白名单
                    （空表示不限制）+ 任务 scope_paths + .anvilignore；
                    它不控制 API/HITL、工具、skill/MCP 或运行时绑定。
                  </p>
                </section>
              )}
              {tab === "catalog" && (
                <div>
                  <div className="catalog-summary">
                    <span>技能 <strong>{selSkills.length}</strong> / {catalog?.skills.length ?? 0}</span>
                    <span>MCP <strong>{selMcp.length}</strong> / {catalog?.mcp_servers.length ?? 0}</span>
                    {roleRuntimeId && <span className="muted">来源：{roleRuntimeId}</span>}
                  </div>
                  <p className="muted">
                    默认全部不勾选；选择按角色独立保存，互不影响。
                    {!roleRuntimeId && " 请先在「运行时」tab 绑定运行时。"}
                  </p>
                  {catalogLoading ? (
                    <p className="muted">加载中…</p>
                  ) : !catalog ? (
                    <p className="muted">请先在「运行时」tab 绑定运行时，或该运行时没有可用的 skill/MCP 目录。</p>
                  ) : (
                    <>
                      <section className="catalog-section">
                        <div className="catalog-section-head">
                          <div>
                            <h3>技能</h3>
                            <span className="muted">Skill · {catalog.skills.length} 项</span>
                          </div>
                          <span className="catalog-selected">{selSkills.length} 已选</span>
                        </div>
                        {catalog.skills.length === 0 ? (
                          <p className="muted">该运行时无可用技能。</p>
                        ) : (
                          <div className="catalog-grid">
                            {catalog.skills.map((s) => {
                              const desc = (s.description || "")
                                .replace(/^[\s|>·—–_-]+|[\s|>·—–_-]+$/g, "");
                              return (
                                <label
                                  key={s.name}
                                  className={`catalog-card ${selSkills.includes(s.name) ? "selected" : ""}`}
                                >
                                  <input
                                    type="checkbox"
                                    checked={selSkills.includes(s.name)}
                                    onChange={() => toggleSel(selSkills, s.name, setSelSkills)}
                                  />
                                  <span className="catalog-card-body">
                                    <span className="catalog-card-name">{s.name}</span>
                                    {desc && (
                                      <span className="catalog-card-desc" title={s.description ?? undefined}>
                                        {desc}
                                      </span>
                                    )}
                                  </span>
                                </label>
                              );
                            })}
                          </div>
                        )}
                      </section>

                      <section className="catalog-section">
                        <div className="catalog-section-head">
                          <div>
                            <h3>MCP 服务器</h3>
                            <span className="muted">Model Context Protocol · {catalog.mcp_servers.length} 项</span>
                          </div>
                          <span className="catalog-selected">{selMcp.length} 已选</span>
                        </div>
                        {catalog.mcp_servers.length === 0 ? (
                          <p className="muted">该运行时无 MCP 服务器。</p>
                        ) : (
                          <div className="catalog-grid catalog-grid-mcp">
                            {catalog.mcp_servers.map((m) => (
                              <label
                                key={m.name}
                                className={`catalog-card catalog-card-mcp ${selMcp.includes(m.name) ? "selected" : ""}`}
                              >
                                <input
                                  type="checkbox"
                                  checked={selMcp.includes(m.name)}
                                  onChange={() => toggleSel(selMcp, m.name, setSelMcp)}
                                />
                                <span className="catalog-card-body">
                                  <span className="catalog-card-head">
                                    <span className="catalog-card-name">{m.name}</span>
                                    <span className="catalog-card-tag">MCP</span>
                                  </span>
                                  <span className="catalog-card-desc" title={m.description || undefined}>
                                    {m.description || m.hint || ""}
                                  </span>
                                </span>
                              </label>
                            ))}
                          </div>
                        )}
                      </section>

                      <div className="toolbar catalog-actions">
                        <span className="muted">
                          已选 {selSkills.length} 个技能 · {selMcp.length} 个 MCP
                        </span>
                        <button onClick={selectAllCatalog}>全选</button>
                        <button onClick={clearCatalog}>全部不选</button>
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
                    <select value={roleEffort} onChange={(e) => setRoleEffort(e.target.value)}>
                      <option value="low">思考强度：低</option>
                      <option value="medium">思考强度：中</option>
                      <option value="high">思考强度：高</option>
                      <option value="max">思考强度：最高</option>
                    </select>
                    <button className="primary" onClick={saveRuntime} disabled={!roleRuntimeId}>保存运行时设置</button>
                  </div>
                  <p className="muted">
                    思考强度对应 Claude Code 的 <code>--effort</code> 参数，控制角色每次运行时的
                    reasoning effort。Cindy 默认“最高”，其他角色默认“高”；不支持 effort 的运行时忽略该值。
                  </p>
                  <section className="role-info-card">
                    <div className="role-info-grid">
                      <div className="role-info-item">
                        <span>当前运行时</span>
                        <code>{currentRuntime}</code>
                      </div>
                      <div className="role-info-item">
                        <span>当前思考强度</span>
                        <code>{EFFORT_LABELS[String(projection.runtime?.effort || "high")] || "高"}</code>
                      </div>
                    </div>
                  </section>

                  <h3 style={{ marginTop: 16 }}>会话上下文</h3>
                  <p className="muted">
                    执行失败/挂起后，如果怀疑上下文已污染，先重置会话再重新启动；
                    MEMORY.md 与 notes 会保留。
                  </p>
                  <div className="toolbar">
                    <button
                      className="danger"
                      onClick={async () => {
                        const ok = await confirmDialog({
                          title: "重置 Agent 会话？",
                          message: "会终止该角色的常驻进程并丢弃当前对话上下文，"
                            + "保留 MEMORY.md 与 notes。",
                          confirmLabel: "重置会话",
                          danger: true,
                        });
                        if (!ok) return;
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
              </div>
            </>
          )}
        </div>
      </div>

      {avatarOpen && current && (
        <AvatarPicker
          mode="modal"
          slug={current.slug}
          name={current.display_name}
          value={current.avatar ?? null}
          onChange={async (value) => {
            try {
              await api.updateRoleAvatar(current.slug, value);
              await refresh();
              setError(null);
            } catch (e) {
              setError(String(e));
              throw e; // 让 AvatarPicker 保持弹窗打开并显示本地错误
            }
          }}
          onClose={() => setAvatarOpen(false)}
        />
      )}
    </div>
  );
}
