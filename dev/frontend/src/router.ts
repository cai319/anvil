// 极简客户端路由：拦截站内 <a href="/...">，pushState 局部切换，避免整页刷新。
// 同一路径重复点击 → 触发页面 remount（等效刷新），给用户可见反馈。
type Listener = (path: string) => void;

let listener: Listener | null = null;

/** App 挂载时注册唯一的路由监听器。 */
export function setRouteListener(fn: Listener | null): void {
  listener = fn;
}

/** 站内跳转：同路径也通知（页面以 key remount 实现刷新）。 */
export function navigate(path: string): void {
  if (listener) {
    if (path !== window.location.pathname) history.pushState({}, "", path);
    listener(path);
  } else {
    window.location.href = path;
  }
}

/** 全局点击拦截：站内链接走客户端路由；修饰键点击（新标签页等）不拦。 */
export function installLinkInterceptor(): () => void {
  const onClick = (e: MouseEvent) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = (e.target as HTMLElement | null)?.closest?.("a");
    if (!a) return;
    const href = a.getAttribute("href");
    if (!href || !href.startsWith("/")) return; // 只处理站内绝对路径链接
    e.preventDefault();
    navigate(href);
  };
  document.addEventListener("click", onClick);
  return () => document.removeEventListener("click", onClick);
}
