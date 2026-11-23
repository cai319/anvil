/**
 * 遮罩关闭守卫。
 *
 * 打开弹层的点击如果被用户快速连点/双击，第二下的 mousedown 会落在刚挂载的
 * 遮罩上，把弹层立刻关掉，表现为「点按钮没反应」。打开后的极短时间内忽略
 * 遮罩关闭，保证弹层稳定出现；超过阈值后遮罩点击恢复正常。
 */
export const BACKDROP_GUARD_MS = 400;

export function canCloseOnBackdrop(openedAt: number, now = Date.now()): boolean {
  return now - openedAt >= BACKDROP_GUARD_MS;
}
