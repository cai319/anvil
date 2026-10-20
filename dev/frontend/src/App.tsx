import { useEffect, useState } from "react";
import { api, subscribeEvents } from "./api/client";
import { installLinkInterceptor, setRouteListener } from "./router";
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

function useRoute(): { route: Route; navKey: number } {
  const [loc, setLoc] = useState({ path: window.location.pathname, key: 0 });

  useEffect(() => {
    // 客户端路由：站内链接局部切换；同路径点击 bump key → 页面 remount（等效刷新）
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
      /* ignore */
    }
  };

  return (
    <>
      <button onClick={toggle} className={frozen ? "danger" : "small"} title={frozen ? "解除全场冻结" : "全场冻结"}>
        {frozen ? "已冻结 · 点击解除" : "全场冻结"}
      </button>
      <span className="chip" title="累计 Token 消耗（输入含 cache；来自 CLI result.usage）">
        Token {tokens == null ? "—" : fmtTokens(tokens.total)}
        {tokens && <span className="muted">（入 {fmtTokens(tokens.input)} / 出 {fmtTokens(tokens.output)}）</span>}
      </span>
    </>
  );
}

const NAV = [
  { href: "/inbox", label: "收件箱" },
  { href: "/", label: "任务大厅" },
  { href: "/roles", label: "角色工坊" },
  { href: "/runtimes", label: "运行时" },
  { href: "/decompose", label: "目标拆解" },
  { href: "/channels", label: "频道" },
  { href: "/knowledge", label: "知识库" },
  { href: "/reminders", label: "提醒" },
  { href: "/tooling", label: "工具" },
  { href: "/governance", label: "治理" },
];

/** 导航「收件箱」待办数徽章（SSE 驱动刷新；无待办不显示）。 */
function InboxBadge() {
  const [count, setCount] = useState(0);
  useEffect(() => {
    const refresh = () => api.getInbox().then((b) => setCount(b.count)).catch(() => {});
    refresh();
    return subscribeEvents(() => refresh());
  }, []);
  if (!count) return null;
  return <span className="badge badge-in_progress" style={{ marginLeft: 6 }}>{count}</span>;
}

export default function App() {
  const { route, navKey } = useRoute();
  const pageKey = `${navKey}`;
  return (
    <>
      <header className="topbar">
        <a href="/" className="brand"><span className="dot" />Anvil</a>
        <nav className="nav">
          {NAV.map((n) => (
            <a key={n.href} href={n.href} style={route.page === (n.href === "/" ? "lobby" : n.href.slice(1)) ? { color: "var(--text)", background: "var(--surface)" } : undefined}>
              {n.href === "/inbox" ? <>{n.label}<InboxBadge /></> : n.label}
            </a>
          ))}
        </nav>
        <span className="spacer" />
        <TopStatus />
      </header>

      <main className="page">
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
    </>
  );
}
