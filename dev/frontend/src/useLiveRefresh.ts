import { useCallback, useEffect, useRef } from "react";
import { subscribeEvents, type AnvilEvent } from "./api/client";

/**
 * 统一的实时刷新：SSE 事件 + 低频轮询 + 页面可见性恢复。
 *
 * - 事件按 topic/match 过滤；tick 单独节流，避免每 5s 全量重拉；
 * - 同一窗口内的多次触发合并；刷新进行中只补跑一次；
 * - 轮询作为半开连接/漏事件的兜底（仅页面可见时执行）；
 * - 重连成功后服务端会发一条合成的 `connected` 事件，强制补拉一次。
 */
export interface LiveRefreshOptions {
  /** 只响应这些 topic；省略表示全部。 */
  topics?: readonly string[];
  /** 自定义匹配（优先于 topics）。 */
  match?: (ev: AnvilEvent) => boolean;
  /** 每个事件都会回调（不受节流影响），用于增量更新。 */
  onEvent?: (ev: AnvilEvent) => void;
  /** 轮询间隔；默认 30000，传 null 关闭。 */
  pollMs?: number | null;
  /** tick 事件的最小刷新间隔；默认 10000。 */
  tickMinGapMs?: number;
  /** 事件触发的全局最小刷新间隔；默认 1000。 */
  minGapMs?: number;
  /** 是否在挂载时立即执行一次；默认 true。 */
  initial?: boolean;
  /** 是否启用；默认 true。 */
  enabled?: boolean;
}

const DEFAULT_POLL_MS = 30_000;
const DEFAULT_TICK_GAP_MS = 10_000;
const DEFAULT_MIN_GAP_MS = 1_000;

export function useLiveRefresh(
  refresh: () => void | Promise<unknown>,
  options: LiveRefreshOptions = {},
): void {
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  const optsRef = useRef(options);
  optsRef.current = options;

  const alive = useRef(true);
  const inFlight = useRef(false);
  const pending = useRef(false);
  const lastRun = useRef(0);
  const gapTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const run = useCallback(() => {
    if (!alive.current) return;
    if (optsRef.current.enabled === false) return;
    if (inFlight.current) {
      pending.current = true;
      return;
    }
    inFlight.current = true;
    lastRun.current = Date.now();
    const finish = () => {
      inFlight.current = false;
      if (!alive.current) return;
      if (pending.current) {
        pending.current = false;
        run();
      }
    };
    try {
      Promise.resolve(refreshRef.current()).then(finish, finish);
    } catch {
      finish();
    }
  }, []);

  const schedule = useCallback((minGap: number) => {
    if (!alive.current) return;
    const elapsed = Date.now() - lastRun.current;
    if (elapsed >= minGap) {
      run();
      return;
    }
    if (gapTimer.current !== null) return;
    gapTimer.current = setTimeout(() => {
      gapTimer.current = null;
      run();
    }, minGap - elapsed);
  }, [run]);

  const { topics, pollMs, tickMinGapMs, minGapMs } = options;
  const enabled = options.enabled !== false;
  const initial = options.initial !== false;
  const topicsKey = topics ? topics.join("|") : "*";
  const poll = pollMs === undefined ? DEFAULT_POLL_MS : pollMs;
  const tickGap = tickMinGapMs ?? DEFAULT_TICK_GAP_MS;
  const minGap = minGapMs ?? DEFAULT_MIN_GAP_MS;

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (!enabled) return;
    if (initial) run();

    const unsub = subscribeEvents((ev) => {
      const opts = optsRef.current;
      if (opts.enabled === false) return;
      if (opts.onEvent) {
        try {
          opts.onEvent(ev);
        } catch {
          /* 增量更新失败不影响补拉 */
        }
      }
      if (ev.topic === "connected") {
        run();
        return;
      }
      const matched = opts.match
        ? opts.match(ev)
        : !opts.topics || opts.topics.includes(ev.topic);
      if (!matched) return;
      schedule(ev.topic === "tick" ? Math.max(minGap, tickGap) : minGap);
    });

    let timer: ReturnType<typeof setInterval> | null = null;
    if (poll && poll > 0) {
      timer = setInterval(() => {
        if (document.visibilityState === "visible") run();
      }, poll);
    }
    const onVisible = () => {
      if (document.visibilityState === "visible") schedule(minGap);
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);

    return () => {
      unsub();
      if (timer) clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
      if (gapTimer.current !== null) {
        clearTimeout(gapTimer.current);
        gapTimer.current = null;
      }
    };
  }, [enabled, initial, poll, tickGap, minGap, topicsKey, run, schedule]);
}
