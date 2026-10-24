import { useEffect, useRef, useState } from "react";
import { api, subscribeEvents } from "./api/client";
import { installLinkInterceptor, setRouteListener } from "./router";
import { APP_VERSION } from "./version";
import { Icon, type IconName } from "./components/Icon";
import { ActivityHistory } from "./pages/ActivityHistory";
import { AgentSessions } from "./pages/AgentSessions";
import { Channels } from "./pages/Channels";
import { Decompose } from "./pages/Decompose";
import { Governance } from "./pages/Governance";
import { Inbox } from "./pages/Inbox";
import { Knowledge } from "./pages/Knowledge";
import { Lobby } from "./pages/Lobby";
import { Reminders } from "./pages/Reminders";
import { Roles } from "./pages/Roles";
import { Runtimes } from "./pages/Runtimes";
import { RunSession } from "./pages/RunSession";
import { TaskDetail } from "./pages/TaskDetail";
import { Tooling } from "./pages/Tooling";

type Route =
  | { page: "lobby" }
  | { page: "inbox" }
  | { page: "activity" }
  | { page: "decompose" }
  | { page: "channels" }
  | { page: "roles" }
  | { page: "runtimes" }
  | { page: "governance" }
  | { page: "reminders" }
  | { page: "knowledge" }
  | { page: "tooling" }
  | { page: "task"; id: string }
  | { page: "agent"; slug: string }
  | { page: "run"; id: string };

const NAV_GROUPS = [
  {
    label: "工作台",
    items: [
      { href: "/inbox", label: "收件箱", icon: "inbox" },
      { href: "/", label: "任务大厅", icon: "dashboard" },
      { href: "/decompose", label: "目标拆解", icon: "git-branch" },
      { href: "/channels", label: "频道", icon: "hash" },
    ],
  },
  {
    label: "角色与知识",
    items: [
      { href: "/roles", label: "角色工坊", icon: "users" },
      { href: "/runtimes", label: "运行时", icon: "cpu" },
      { href: "/knowledge", label: "知识库", icon: "book" },
      { href: "/reminders", label: "提醒", icon: "bell" },
    ],
  },
  {
    label: "系统",
    items: [
      { href: "/tooling", label: "工具", icon: "wrench" },
      { href: "/governance", label: "治理", icon: "shield" },
    ],
  },
] as const satisfies ReadonlyArray<{
  label: string;
  items: ReadonlyArray<{ href: string; label: string; icon: IconName }>;
}>;

const PAGE_TITLE: Record<Route["page"], string> = {
  lobby: "任务大厅",
  inbox: "收件箱",
  activity: "活动历史",
  decompose: "目标拆解",
  channels: "频道",
  roles: "角色工坊",
  runtimes: "运行时",
  governance: "治理",
  reminders: "提醒",
  knowledge: "知识库",
  tooling: "工具",
  task: "任务详情",
  agent: "角色会话",
  run: "运行详情",
};

const SIDEBAR_KEY = "anvil.sidebar.collapsed";

function initialSidebarCollapsed(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_KEY) === "1";
  } catch {
    return false;
  }
}

function parseRoute(path: string): Route {
  if (path.startsWith("/inbox")) return { page: "inbox" };
  if (path.startsWith("/activity")) return { page: "activity" };
  if (path.startsWith("/decompose")) return { page: "decompose" };
  if (path.startsWith("/channels")) return { page: "channels" };
  if (path.startsWith("/roles")) return { page: "roles" };
  if (path.startsWith("/runtimes")) return { page: "runtimes" };
  if (path.startsWith("/governance")) return { page: "governance" };
  if (path.startsWith("/reminders")) return { page: "reminders" };
  if (path.startsWith("/knowledge")) return { page: "knowledge" };
  if (path.startsWith("/tooling")) return { page: "tooling" };
  const m = path.match(/^\/tasks\/([^/]+)$/);
  if (m) return { page: "task", id: decodeURIComponent(m[1]) };
  const a = path.match(/^\/agents\/([^/]+)$/);
  if (a) return { page: "agent", slug: decodeURIComponent(a[1]) };
  const r = path.match(/^\/runs\/([^/]+)$/);
  if (r) return { page: "run", id: decodeURIComponent(r[1]) };
  return { page: "lobby" };
}

function activeHref(route: Route): string {
  if (route.page === "task" || route.page === "run") return "/";
  if (route.page === "agent") return "/roles";
  return route.page === "lobby" ? "/" : `/${route.page}`;
}

function useRoute(): { route: Route; navKey: number } {
  const [loc, setLoc] = useState({ path: window.location.pathname, key: 0 });

  useEffect(() => {
    setRouteListener((path: string) => setLoc((l) => ({ path, key: l.key + 1 })));
    const onPop = () => setLoc((l) => ({ path: window.location.pathname, key: l.key + 1 }));
    window.addEventListener("popstate", onPop);
    const uninstall = installLinkInterceptor();
    return () => {
      setRouteListener(null);
      window.removeEventListener("popstate", onPop);
      uninstall();
    };
  }, []);

  return { route: parseRoute(loc.path), navKey: loc.key };
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function TopStatus() {
  const [frozen, setFrozen] = useState(false);
  const [tokens, setTokens] = useState<{ total: number; input: number; output: number } | null>(null);

  const refresh = () => {
    api.getFreeze().then((f) => setFrozen(f.frozen)).catch(() => {});
    api.getTokens().then((t) => setTokens({
      total: t.total_tokens, input: t.input_tokens, output: t.output_tokens,
    })).catch(() => {});
  };

  useEffect(() => {
    refresh();
    return subscribeEvents(() => refresh());
  }, []);

  const toggle = async () => {
    try {
      const r = frozen ? await api.resumeAll() : await api.freezeAll();
      setFrozen(r.frozen);
    } catch {
      // ignore
    }
  };

  return (
    <>
      <button
        onClick={toggle}
        className={frozen ? "danger small" : "small"}
        title={frozen ? "解除全场冻结" : "全场冻结"}
      >
        {frozen ? "已冻结 · 点击解除" : "全场冻结"}
      </button>
      <span className="chip" title="累计 Token 消耗（输入含 cache；来自 CLI result.usage）">
        Token {tokens == null ? "—" : fmtTokens(tokens.total)}
        {tokens && (
          <span className="muted">
            （入 {fmtTokens(tokens.input)} / 出 {fmtTokens(tokens.output)}）
          </span>
        )}
      </span>
    </>
  );
}

function InboxBadge() {
  const [count, setCount] = useState(0);
  useEffect(() => {
    const refresh = () => api.getInbox().then((b) => setCount(b.count)).catch(() => {});
    refresh();
    return subscribeEvents(() => refresh());
  }, []);
  if (!count) return null;
  return <span className="nav-badge">{count}</span>;
}

export default function App() {
  const { route, navKey } = useRoute();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(initialSidebarCollapsed);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const menuRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    try {
      localStorage.setItem(SIDEBAR_KEY, sidebarCollapsed ? "1" : "0");
    } catch {
      // ignore
    }
  }, [sidebarCollapsed]);

  useEffect(() => {
    setDrawerOpen(false);
  }, [navKey]);

  useEffect(() => {
    // 浏览器标签页标题始终带版本号（静态 index.html 只是首屏兜底）。
    document.title = `Anvil ${APP_VERSION} — ${PAGE_TITLE[route.page]}`;
  }, [route.page]);

  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setDrawerOpen(false);
      menuRef.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawerOpen]);

  const active = activeHref(route);
  const pageKey = `${navKey}`;

  return (
    <div
      className={`app-shell ${sidebarCollapsed ? "sidebar-collapsed" : ""} ${drawerOpen ? "drawer-open" : ""}`}
    >
      <aside id="app-sidebar" className="sidebar" aria-label="主导航">
        <a href="/" className="brand" onClick={() => setDrawerOpen(false)}>
          <span className="dot" />
          <span className="brand-name">Anvil</span>
          <span className="brand-version" title={`Anvil 当前版本 ${APP_VERSION}`}>
            {APP_VERSION}
          </span>
        </a>

        <nav className="sidebar-nav">
          {NAV_GROUPS.map((group) => (
            <div className="nav-group" key={group.label}>
              <div className="nav-label">{group.label}</div>
              {group.items.map((item) => (
                <a
                  key={item.href}
                  href={item.href}
                  className={`nav-item ${active === item.href ? "active" : ""}`}
                  title={item.label}
                  aria-current={active === item.href ? "page" : undefined}
                  onClick={() => setDrawerOpen(false)}
                >
                  <span className="nav-icon" aria-hidden="true"><Icon name={item.icon} /></span>
                  <span className="nav-text">{item.label}</span>
                  {item.href === "/inbox" && <InboxBadge />}
                </a>
              ))}
            </div>
          ))}
        </nav>

        <div className="sidebar-foot">
          <button
            type="button"
            className="rail-btn"
            onClick={() => setSidebarCollapsed((v) => !v)}
            aria-expanded={!sidebarCollapsed}
            aria-label={sidebarCollapsed ? "展开侧栏" : "收起侧栏"}
          >
            <Icon name={sidebarCollapsed ? "chevron-right" : "chevron-left"} />
            <span className="nav-text">{sidebarCollapsed ? "展开侧栏" : "收起侧栏"}</span>
          </button>
        </div>
      </aside>

      <div className="app-main">
        <header className="app-header">
          <button
            ref={menuRef}
            type="button"
            className="hamburger"
            onClick={() => setDrawerOpen(true)}
            aria-label="打开导航"
            aria-expanded={drawerOpen}
            aria-controls="app-sidebar"
          >
            <Icon name="menu" size={18} />
          </button>
          <div className="page-crumb">
            <span>{PAGE_TITLE[route.page]}</span>
            <span className="muted">/ Anvil</span>
          </div>
          <span className="header-spacer" />
          <TopStatus />
        </header>

        <main className={`content ${route.page === "task" ? "content-task" : route.page === "roles" ? "content-roles" : route.page === "lobby" ? "content-lobby" : route.page === "tooling" ? "content-tooling" : route.page === "governance" ? "content-governance" : route.page === "reminders" ? "content-reminders" : route.page === "knowledge" ? "content-knowledge" : ""}`}>
          {route.page === "inbox" ? (
            <Inbox key={`inbox-${pageKey}`} />
          ) : route.page === "activity" ? (
            <ActivityHistory key={`activity-${pageKey}`} />
          ) : route.page === "task" ? (
            <TaskDetail key={`task-${route.id}-${pageKey}`} id={route.id} />
          ) : route.page === "agent" ? (
            <AgentSessions key={`agent-${route.slug}-${pageKey}`} slug={route.slug} />
          ) : route.page === "run" ? (
            <RunSession key={`run-${route.id}-${pageKey}`} id={route.id} />
          ) : route.page === "decompose" ? (
            <Decompose key={`decompose-${pageKey}`} />
          ) : route.page === "channels" ? (
            <Channels key={`channels-${pageKey}`} />
          ) : route.page === "roles" ? (
            <Roles key={`roles-${pageKey}`} />
          ) : route.page === "runtimes" ? (
            <Runtimes key={`runtimes-${pageKey}`} />
          ) : route.page === "knowledge" ? (
            <Knowledge key={`knowledge-${pageKey}`} />
          ) : route.page === "reminders" ? (
            <Reminders key={`reminders-${pageKey}`} />
          ) : route.page === "tooling" ? (
            <Tooling key={`tooling-${pageKey}`} />
          ) : route.page === "governance" ? (
            <Governance key={`governance-${pageKey}`} />
          ) : (
            <Lobby key={`lobby-${pageKey}`} />
          )}
        </main>
      </div>

      <button
        type="button"
        className="drawer-mask"
        aria-label="关闭导航"
        onClick={() => setDrawerOpen(false)}
      />
    </div>
  );
}
