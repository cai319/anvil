"""Anvil 文件日志与 HTTP 访问日志。

默认日志文件：<项目根>/log/anvil.log（源码模式）；
打包模式：<anvil-server.exe 所在目录>/log/anvil.log。
可用 ANVIL_LOG_DIR / ANVIL_LOG_LEVEL / ANVIL_LOG_MAX_BYTES / ANVIL_LOG_BACKUPS 覆盖。

记录范围：
- 所有 /api/v1 用户操作：方法、路径、状态、耗时、角色、任务、客户端 IP、请求 ID；
- 后端出站通信的详细诊断由 services/issue_tracker.py 写入同一个 root logger；
- 不记录 token、Cookie、请求正文或响应正文。
"""
from __future__ import annotations

import logging
import os
import platform
import re
import socket
import sys
import threading
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import getproxies

from starlette.middleware.base import BaseHTTPMiddleware

from . import config

logger = logging.getLogger("anvil.logging")
access_logger = logging.getLogger("anvil.access")

_handler: RotatingFileHandler | None = None
_log_path: Path | None = None
_lock = threading.Lock()
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error")

_PROXY_KEYS = {
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
}


def sanitize_proxy(value: str) -> str:
    """隐藏代理 URL 中的用户名/密码，避免日志泄露凭据。"""
    text = str(value or "")
    try:
        parts = urlsplit(text)
        if not parts.scheme:
            return text
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        netloc = f"***@{host}" if (parts.username or parts.password) else host
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return "<invalid-proxy-url>"


def proxy_snapshot() -> str:
    """记录代理相关环境变量名和脱敏后的值。"""
    parts = []
    for key, value in os.environ.items():
        if key.lower() in _PROXY_KEYS:
            parts.append(f"{key}={sanitize_proxy(value)}")
    return ",".join(parts) if parts else "-"


def system_proxy_snapshot() -> str:
    """记录操作系统代理（Windows 注册表 / macOS 系统设置），仅用于诊断。

    tracker 请求固定直连，不会使用这份配置；记录它是为了识别 localhost 系统代理
    导致的“curl 能通、Anvil 抓不到包”问题。
    """
    try:
        proxies = getproxies()
    except Exception as exc:  # noqa: BLE001
        return f"error:{type(exc).__name__}:{exc}"
    parts = [f"{key}={sanitize_proxy(value)}"
             for key, value in sorted(proxies.items()) if value]
    return ",".join(parts) if parts else "-"


def _local_addresses() -> str:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None)
        addresses = sorted({info[4][0] for info in infos})
        return ",".join(addresses) or "-"
    except OSError as exc:
        return f"error:{type(exc).__name__}:{exc}"


def setup_logging(log_dir: Path | str | None = None) -> Path:
    """初始化文件日志；重复调用幂等，返回 anvil.log 路径。"""
    global _handler, _log_path
    with _lock:
        target_dir = Path(log_dir) if log_dir else config.LOG_DIR
        target = target_dir / "anvil.log"
        if _handler is not None and _log_path == target:
            return target

        # 同一个进程重复初始化时先移除旧 handler。
        old_handler = _handler

        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # 项目根不可写时退回数据目录，避免日志初始化阻断服务启动。
            target_dir = config.DATA_DIR / "log"
            target = target_dir / "anvil.log"
            target_dir.mkdir(parents=True, exist_ok=True)

        level_name = str(getattr(config, "LOG_LEVEL", "INFO")).upper()
        level = getattr(logging, level_name, logging.INFO)
        handler = RotatingFileHandler(
            target,
            maxBytes=int(getattr(config, "LOG_MAX_BYTES", 20 * 1024 * 1024)),
            backupCount=int(getattr(config, "LOG_BACKUPS", 10)),
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)s [%(name)s] "
            "[%(threadName)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        handler.setLevel(level)

        root = logging.getLogger()
        if old_handler is not None:
            root.removeHandler(old_handler)
            for name in _UVICORN_LOGGERS:
                logging.getLogger(name).removeHandler(old_handler)
            old_handler.close()
        root.addHandler(handler)
        if root.level == logging.NOTSET or root.level > level:
            root.setLevel(level)
        for name in _UVICORN_LOGGERS:
            uv_logger = logging.getLogger(name)
            uv_logger.addHandler(handler)
            if uv_logger.level == logging.NOTSET or uv_logger.level > level:
                uv_logger.setLevel(level)
        # 默认 INFO 时不让 httpx/httpcore 的逐请求 INFO 行重复刷日志；
        # 主动排查可设 ANVIL_LOG_LEVEL=DEBUG 恢复。
        if level > logging.DEBUG:
            for name in ("httpx", "httpcore"):
                dependency_logger = logging.getLogger(name)
                if dependency_logger.level == logging.NOTSET \
                        or dependency_logger.level < logging.WARNING:
                    dependency_logger.setLevel(logging.WARNING)

        _handler = handler
        _log_path = target
        logger.info(
            "file logging enabled path=%s level=%s pid=%s python=%s platform=%s "
            "cwd=%s hostname=%s local_addresses=%s env_proxies=%s system_proxies=%s",
            target, logging.getLevelName(level), os.getpid(),
            sys.version.split()[0], platform.platform(), os.getcwd(),
            socket.gethostname(), _local_addresses(), proxy_snapshot(),
            system_proxy_snapshot(),
        )
        return target


class AccessLogMiddleware(BaseHTTPMiddleware):
    """记录所有 /api/v1 用户操作；不记录请求/响应正文和 token。"""

    async def dispatch(self, request, call_next):
        path = request.url.path
        if not path.startswith("/api/v1") or path == "/api/v1/events":
            return await call_next(request)

        request_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        method = request.method
        role = request.headers.get("X-Anvil-Role") or "human"
        client = request.client.host if request.client else "-"
        forwarded = request.headers.get("x-forwarded-for", "-")
        task_match = re.match(r"^/api/v1/tasks/([^/]+)", path)
        task = task_match.group(1) if task_match else "-"
        query_keys = ",".join(sorted(request.query_params.keys())) or "-"
        content_length = request.headers.get("content-length", "-")
        user_agent = (request.headers.get("user-agent") or "-")[:160]

        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            access_logger.exception(
                "http error id=%s method=%s path=%s query_keys=%s role=%s "
                "task=%s client=%s xff=%s duration_ms=%s content_length=%s ua=%s",
                request_id, method, path, query_keys, role, task, client,
                forwarded, elapsed_ms, content_length, user_agent,
            )
            raise

        elapsed_ms = int((time.monotonic() - started) * 1000)
        # 默认 INFO：成功的 GET/轮询（页面刷新、状态轮询）记 DEBUG，不落盘；
        # 变更操作（POST/PUT/PATCH/DELETE）与实际错误记 INFO/WARNING。
        if response.status_code >= 500:
            log_level = logging.ERROR
        elif response.status_code >= 400:
            log_level = logging.WARNING
        elif method in ("GET", "HEAD", "OPTIONS"):
            log_level = logging.DEBUG
        else:
            log_level = logging.INFO
        access_logger.log(
            log_level,
            "http id=%s method=%s path=%s query_keys=%s status=%s "
            "duration_ms=%s role=%s task=%s client=%s xff=%s "
            "content_length=%s ua=%s",
            request_id, method, path, query_keys, response.status_code,
            elapsed_ms, role, task, client, forwarded,
            content_length, user_agent,
        )
        return response
