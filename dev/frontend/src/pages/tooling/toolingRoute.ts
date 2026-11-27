export type ToolingTab = "config" | "import" | "flags" | "proposals";

export const TOOLING_TABS: { key: ToolingTab; label: string }[] = [
  { key: "config", label: "工具配置" },
  { key: "import", label: "工具导入" },
  { key: "flags", label: "功能开关" },
  { key: "proposals", label: "运行时提案" },
];

const TAB_KEYS = new Set<ToolingTab>(TOOLING_TABS.map((tab) => tab.key));

export function readToolingRoute(search = window.location.search): {
  tab: ToolingTab;
  ripId: string | null;
} {
  const params = new URLSearchParams(search);
  const rawTab = params.get("tab");
  const ripId = params.get("rip");
  // 兼容旧 Inbox 深链 /tooling?rip=<id>。
  if (!rawTab && ripId) return { tab: "proposals", ripId };
  const tab = rawTab && TAB_KEYS.has(rawTab as ToolingTab)
    ? rawTab as ToolingTab
    : "config";
  return {
    tab,
    ripId: tab === "proposals" ? ripId : null,
  };
}

export function toolingHref(tab: ToolingTab, ripId?: string | null): string {
  const params = new URLSearchParams();
  if (tab !== "config") params.set("tab", tab);
  if (tab === "proposals" && ripId) params.set("rip", ripId);
  const qs = params.toString();
  return qs ? `/tooling?${qs}` : "/tooling";
}

export function writeToolingRoute(tab: ToolingTab, ripId?: string | null): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (tab === "config") url.searchParams.delete("tab");
  else url.searchParams.set("tab", tab);
  if (tab === "proposals" && ripId) url.searchParams.set("rip", ripId);
  else url.searchParams.delete("rip");
  window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
}
