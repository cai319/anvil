import { useEffect, useState } from "react";

/** 后端时间统一是 UTC（ISO8601，通常带 +00:00）；展示时按浏览器/系统时区转换。 */
export function parseTime(iso?: string | null): Date | null {
  if (!iso) return null;
  let s = String(iso).trim();
  if (!s) return null;
  // 老数据可能没有时区后缀：按 UTC 解释，避免被 JS 当成本地时间。
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$/.test(s)) s += "Z";
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d;
}

const pad = (n: number) => String(n).padStart(2, "0");

/** 本地时间：HH:mm:ss。 */
export function formatClock(iso?: string | null): string {
  const d = parseTime(iso);
  return d ? `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` : "";
}

/** 本地时间：YYYY-MM-DD HH:mm:ss。 */
export function formatDateTime(iso?: string | null): string {
  const d = parseTime(iso);
  return d
    ? `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
      + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    : "";
}

/** 本地时间：MM-DD HH:mm。 */
export function formatShort(iso?: string | null): string {
  const d = parseTime(iso);
  return d
    ? `${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
      + `${pad(d.getHours())}:${pad(d.getMinutes())}`
    : "";
}

/** 相对当前系统时间；now 可传入定时器值以刷新“几分钟前”。 */
export function formatRelative(iso?: string | null, now = Date.now()): string {
  const d = parseTime(iso);
  if (!d) return "";
  const diff = Math.max(0, now - d.getTime());
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  if (diff < 30 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`;
  return formatDateTime(iso);
}

/** 定时触发重渲染，让“刚刚 / N 分钟前 / 用时”等相对时间保持新鲜。 */
export function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}
