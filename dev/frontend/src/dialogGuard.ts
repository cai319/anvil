/**
 * 遮罩关闭守卫。
 *
 * 打开弹层的点击如果被用户连点/双击，第二下的 mousedown 会落在刚挂载的
 * 遮罩上，把弹层立刻关掉，表现为「点按钮没反应」。处理规则：
 * - 双击产生的第二下（detail > 1）一律不触发遮罩关闭；
 * - 打开后的防护窗口内（含触摸双 tap 可能产生的 detail=1）也不关闭。
 * 防护窗口结束后，遮罩点击恢复正常。
 */
export const BACKDROP_GUARD_MS = 900;

export function canCloseOnBackdrop(
  openedAt: number,
  detail = 1,
  now = Date.now(),
): boolean {
  if (detail > 1) return false;
  return now - openedAt >= BACKDROP_GUARD_MS;
}
