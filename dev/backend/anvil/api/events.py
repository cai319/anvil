"""SSE 事件流（§10 /api/v1/events）+ 极简进程内事件总线。

同步代码（端点/调度器线程）通过 `publish()` 广播；SSE 生成器用线程安全的 `queue.Queue`
订阅。为不阻塞事件循环，用非阻塞 `get_nowait` 轮询 + 定期 keep-alive（不再每连接开线程）。
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..util import now_iso

router = APIRouter()

_subscribers: set[queue.Queue] = set()
_guard = threading.Lock()
_KEEPALIVE_SECONDS = 15
_POLL_SECONDS = 1.0


def publish(topic: str, payload: dict | None = None) -> None:
    """线程安全地广播一条事件。topic 与设计 §11.2 一致（task/run/activity/cost/proposal）。"""
    msg = json.dumps({"topic": topic, "payload": payload or {}, "at": now_iso()}, ensure_ascii=False)
    with _guard:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(msg)
        except Exception:  # noqa: BLE001
            pass


@router.get("/events")
async def events():
    q: queue.Queue = queue.Queue()
    with _guard:
        _subscribers.add(q)

    async def stream():
        yield f"event: connected\ndata: {json.dumps({'topic': 'connected', 'payload': {}, 'at': now_iso()})}\n\n"
        last_keepalive = time.monotonic()
        try:
            while True:
                try:
                    msg = q.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(_POLL_SECONDS)
                    if time.monotonic() - last_keepalive >= _KEEPALIVE_SECONDS:
                        yield ": keep-alive\n\n"
                        last_keepalive = time.monotonic()
                    continue
                yield f"data: {msg}\n\n"
        finally:
            with _guard:
                _subscribers.discard(q)

    return StreamingResponse(stream(), media_type="text/event-stream")
