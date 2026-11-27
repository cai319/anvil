const RELOAD_KEY = "anvil.asset-reload-at";
const RELOAD_GUARD_MS = 10_000;

function showReloadBanner() {
  if (document.getElementById("asset-reload-banner")) return;
  const banner = document.createElement("div");
  banner.id = "asset-reload-banner";
  banner.className = "asset-reload-banner";
  banner.setAttribute("role", "alert");

  const text = document.createElement("span");
  text.textContent = "前端资源已更新，需要重新加载页面。";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "small";
  button.textContent = "重新加载";
  button.addEventListener("click", () => {
    sessionStorage.removeItem(RELOAD_KEY);
    window.location.reload();
  });

  banner.append(text, button);
  document.body.appendChild(banner);
}

/** Recover an open tab whose hashed chunks were removed by a new build. */
export function installAssetRecovery() {
  window.addEventListener("vite:preloadError", (event) => {
    event.preventDefault();
    const now = Date.now();
    const last = Number(sessionStorage.getItem(RELOAD_KEY) || 0);
    if (now - last > RELOAD_GUARD_MS) {
      sessionStorage.setItem(RELOAD_KEY, String(now));
      window.location.reload();
      return;
    }
    showReloadBanner();
  });

  // If the replacement build loaded successfully, allow a later rebuild to
  // trigger the same recovery again. A failed replacement shows the banner.
  window.setTimeout(() => sessionStorage.removeItem(RELOAD_KEY), RELOAD_GUARD_MS);
}
