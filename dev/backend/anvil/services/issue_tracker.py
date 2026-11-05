"""问题板 findings → 外部问题跟踪服务器（draft/bulkCreate）。

原则：
- 仅人工触发；外部提交不改变 finding/board 状态，不参与 review 闸门；
- 每次触发都重新提交，本地不做跳过/去重；source 带任务号和时间戳；
- 提交历史与最新 external_id 落库；网络调用不持有 SQLite 写事务；
- 测试连接只发空 list，不创建草稿。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import httpx

from .. import config
from ..util import ConflictError, jdumps, jloads, new_id, now_iso
from . import findings, governance

SETTING_URL = "issue_tracker.url"
SOURCE_PREFIX = "anvil"
ELIGIBLE_STATUSES = ("submitted", "confirmed", "changes_requested")
MAX_BATCH = 200
DEFAULT_TIMEOUT = config.ISSUE_TRACKER_TIMEOUT_SECONDS
TEST_TIMEOUT = config.ISSUE_TRACKER_TEST_TIMEOUT_SECONDS
CONNECT_TIMEOUT = config.ISSUE_TRACKER_CONNECT_TIMEOUT_SECONDS

logger = logging.getLogger("anvil.issue_tracker")


class IssueTrackerError(RuntimeError):
    """外部问题跟踪服务器不可用或返回错误。"""


class IssueTrackerTimeout(IssueTrackerError):
    """连接/读取超时。"""


def _audit(conn: sqlite3.Connection, actor: str, task_id: str | None,
           action: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO audit (role_slug, task_id, action, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (actor, task_id, action, jdumps(payload), now_iso()))


def get_settings(conn: sqlite3.Connection) -> dict:
    url = str(governance.get_setting(conn, SETTING_URL) or "").strip()
    return {
        "server_url": url,
        "configured": bool(url),
        "source_prefix": SOURCE_PREFIX,
    }


def normalize_base_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        raise ValueError("问题跟踪服务器地址不能为空")
    parsed = urlsplit(text)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("问题跟踪服务器地址必须使用 http 或 https")
    if not parsed.hostname:
        raise ValueError("问题跟踪服务器地址缺少主机名")
    if parsed.username or parsed.password:
        raise ValueError("问题跟踪服务器地址不能包含用户名/密码")
    if parsed.query or parsed.fragment:
        raise ValueError("问题跟踪服务器地址不能包含查询参数或 fragment")
    path = (parsed.path or "").rstrip("/")
    suffix = "/draft/bulkCreate"
    if path.endswith(suffix):
        path = path[:-len(suffix)].rstrip("/")
    base = urlunsplit((scheme, parsed.netloc, path, "", "")).rstrip("/")
    if not base:
        raise ValueError("问题跟踪服务器地址无效")
    return base


def endpoint_url(base: str) -> str:
    return f"{normalize_base_url(base)}/draft/bulkCreate"


def save_settings(conn: sqlite3.Connection, url: str, *, actor: str = "human") -> dict:
    text = str(url or "").strip()
    if text:
        text = normalize_base_url(text)
        governance.set_setting(conn, SETTING_URL, text)
    else:
        conn.execute("DELETE FROM settings WHERE key=?", (SETTING_URL,))
        conn.commit()
    _audit(conn, actor, None, "issue_tracker.config",
           {"server_url": text, "cleared": not bool(text)})
    conn.commit()
    return get_settings(conn)


def _post_request(endpoint: str, payload: dict,
                  timeout: float = DEFAULT_TIMEOUT) -> httpx.Response:
    # connect 单独限时，避免 DNS/TCP 建连慢时占用全部读超时；其余阶段共享 timeout。
    limits = httpx.Timeout(timeout, connect=min(timeout, CONNECT_TIMEOUT))
    # 与 curl 一致：trust_env=True 读取系统代理环境变量；未配置代理时由本机
    # 路由表选择出口网卡。ANVIL_ISSUE_TRACKER_PROXY 可显式覆盖环境变量。
    with httpx.Client(
        timeout=limits,
        follow_redirects=False,
        trust_env=True,
        proxy=config.ISSUE_TRACKER_PROXY or None,
    ) as client:
        return client.post(endpoint, json=payload)


def _timeout_message(exc: httpx.TimeoutException, timeout: float,
                     endpoint: str) -> str:
    """把 httpx 超时拆成可操作的阶段，方便区分网络与外部服务问题。"""
    if isinstance(exc, httpx.ConnectTimeout):
        return (f"连接超时：{CONNECT_TIMEOUT:g}s 内无法连接 {endpoint}"
                "（检查 Anvil 后端的 DNS、防火墙、出站代理或地址/端口）")
    if isinstance(exc, httpx.ReadTimeout):
        return (f"读取超时：已连接 {endpoint}，但 {timeout:g}s 内未收到响应"
                "（外部服务处理过慢，或反向代理未及时返回）")
    if isinstance(exc, httpx.WriteTimeout):
        return f"发送超时：{timeout:g}s 内未把请求体发送到 {endpoint}"
    if isinstance(exc, httpx.PoolTimeout):
        return f"连接池超时：{timeout:g}s 内没有可用连接"
    return f"请求超时（{timeout:g}s）：{endpoint}"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _source(task_id: str, stamp: str | None = None) -> str:
    return f"{SOURCE_PREFIX}:{task_id}:{stamp or _stamp()}"


def _line_no(finding: dict) -> str:
    path = str(finding.get("file_path") or "").strip()
    lines = str(finding.get("line_numbers") or "").strip()
    if path and lines:
        return f"{path}:{lines}"
    return path or lines


def _severity(value: str) -> str:
    return {
        "critical": "严重", "high": "高", "medium": "中",
        "low": "低", "info": "低",
    }.get(str(value or "").strip(), str(value or "").strip())


def _risk_type(value: str) -> str:
    raw = str(value or "").strip()
    return findings.RISK_TYPES.get(raw, raw)


def build_description_md(task: dict, board: dict | None, finding: dict,
                         *, source: str, submitted_at: str) -> str:
    def text(key: str, default: str = "—") -> str:
        value = finding.get(key)
        return str(value).strip() if value not in (None, "") else default

    risk = text("risk_description")
    lines = [
        f"# {text('title', '（未命名 finding）')}",
        "",
        f"> {risk}",
        "",
        "## 来源",
        f"- 任务：{task.get('id')} · {task.get('title') or '—'}",
        f"- 问题板：{(board or {}).get('status') or '—'}",
        f"- Finding：{finding.get('id')} · 状态 {finding.get('status')} · 版本 {finding.get('version')}",
        f"- 作者：{text('author_role')}",
        f"- 提交：{submitted_at} · source={source}",
        "",
        "## 风险分类",
        f"- 风险类型：{_risk_type(str(finding.get('risk_type') or ''))}"
        f"（{text('risk_type')}）",
        f"- 严重等级：{findings.SEVERITIES.get(str(finding.get('severity') or ''), text('severity'))}",
        f"- 模块 / 函数：{text('module')} / {text('function_name')}",
        f"- 文件行号：{_line_no(finding) or '—'}",
        f"- 置信度：{finding.get('confidence', 0)}%",
        f"- 修复优先级：{findings.FIX_PRIORITIES.get(str(finding.get('fix_priority') or ''), text('fix_priority'))}",
        "",
        "## 前置条件",
        text("preconditions"),
        "",
        "## 推理链",
        text("reasoning_chain"),
        "",
        "## 攻击路径",
        text("attack_path"),
        "",
        "## 触发点",
        text("trigger_point"),
        "",
        "## PoC",
        f"- 状态：{findings.POC_STATUSES.get(str(finding.get('poc_status') or ''), text('poc_status'))}",
        text("poc"),
        "",
        "## 修复建议",
        text("fix_suggestion"),
        "",
        "## 人工决策记录",
        f"- {finding.get('decided_by') or '—'} · {finding.get('decided_at') or '—'}"
        f" · {finding.get('decision_note') or '—'}",
        "",
    ]
    return "\n".join(lines)


def build_item(task: dict, board: dict | None, finding: dict,
               *, source: str, submitted_at: str) -> dict:
    risk = str(finding.get("risk_description") or finding.get("title") or "").strip()
    item: dict = {
        "risk_description": risk,
        "description_md": build_description_md(
            task, board, finding, source=source, submitted_at=submitted_at),
    }
    for key, external in (
        ("risk_type", "risk_type"),
        ("severity", "severity"),
        ("module", "module"),
        ("function_name", "handler_name"),
        ("preconditions", "pre_condition"),
        ("attack_path", "attack_path"),
        ("trigger_point", "trigger_point"),
        ("fix_priority", "fix_priority"),
        ("fix_suggestion", "fix_suggestion"),
    ):
        value = finding.get(key)
        if key == "risk_type":
            value = _risk_type(str(value or ""))
        elif key == "severity":
            value = _severity(str(value or ""))
        text = str(value or "").strip()
        if text:
            item[external] = text
    line_no = _line_no(finding)
    if line_no:
        item["file_line_no"] = line_no
    return item


def _task(conn: sqlite3.Connection, task_id: str) -> dict:
    row = conn.execute(
        "SELECT id, title, status, issue_board_enabled FROM tasks WHERE id=?",
        (task_id,)).fetchone()
    if row is None:
        raise LookupError(task_id)
    if not row["issue_board_enabled"]:
        raise ConflictError("issue board is not enabled for this task")
    return dict(row)


def _board(conn: sqlite3.Connection, task_id: str) -> dict | None:
    row = conn.execute(
        "SELECT task_id, status, summary FROM task_finding_boards WHERE task_id=?",
        (task_id,)).fetchone()
    return dict(row) if row is not None else None


def _candidates(conn: sqlite3.Connection, task_id: str,
                finding_ids: list[str] | None) -> list[dict]:
    if finding_ids:
        ids = list(dict.fromkeys(str(x) for x in finding_ids if str(x).strip()))
        if not ids:
            raise ConflictError("no eligible findings")
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT * FROM task_findings WHERE task_id=? AND id IN ({placeholders})",
            (task_id, *ids)).fetchall()
        found = {str(row["id"]): dict(row) for row in rows}
        missing = [x for x in ids if x not in found]
        if missing:
            raise LookupError(f"finding not found: {missing[0]}")
        items = [found[x] for x in ids]
    else:
        items = findings.list_findings(conn, task_id, limit=1000)
    eligible = [row for row in items
                if str(row.get("status") or "") in ELIGIBLE_STATUSES]
    if not eligible:
        raise ConflictError("no eligible findings")
    return eligible


def _record_submission(conn: sqlite3.Connection, *, task_id: str,
                       finding_id: str, source: str, external_id: str | None,
                       status: str, error: str | None, request: dict,
                       response: dict, actor: str) -> None:
    conn.execute(
        "INSERT INTO task_finding_submissions "
        "(id, task_id, finding_id, source, external_id, status, error, "
        " request_json, response_json, submitted_by, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (new_id(), task_id, finding_id, source, external_id, status, error,
         jdumps(request), jdumps(response), actor, now_iso()))
    if status == "submitted" and external_id:
        conn.execute(
            "UPDATE task_findings SET external_id=?, external_source=?, "
            "external_submitted_at=? WHERE id=?",
            (str(external_id), source, now_iso(), finding_id))
    _audit(conn, actor, task_id, "finding.external_submit", {
        "finding_id": finding_id, "source": source,
        "external_id": external_id, "status": status, "error": error,
    })


def _fail_all(conn: sqlite3.Connection, *, task_id: str, items: list[dict],
              source: str, error: str, response: dict, actor: str) -> None:
    for item in items:
        _record_submission(
            conn, task_id=task_id, finding_id=str(item["id"]), source=source,
            external_id=None, status="failed", error=error, request=item,
            response=response, actor=actor)
    conn.commit()


def export_findings(conn: sqlite3.Connection, task_id: str,
                    finding_ids: list[str] | None = None,
                    *, actor: str = "human") -> dict:
    task = _task(conn, task_id)
    board = _board(conn, task_id)
    candidates = _candidates(conn, task_id, finding_ids)
    if len(candidates) > MAX_BATCH:
        raise ConflictError(f"too many findings in one submission (max {MAX_BATCH})")
    settings = get_settings(conn)
    if not settings["configured"]:
        raise ConflictError("issue tracker server is not configured")
    submitted_at = now_iso()
    source = _source(task_id)
    items = [build_item(task, board, finding, source=source,
                        submitted_at=submitted_at) for finding in candidates]
    payload = {"source": source, "list": items}
    # 网络调用前释放本地事务，避免长时间持有 SQLite 锁。
    conn.commit()
    endpoint = endpoint_url(settings["server_url"])

    try:
        response = _post_request(endpoint, payload)
    except httpx.TimeoutException as exc:
        message = _timeout_message(exc, DEFAULT_TIMEOUT, endpoint)
        logger.warning("issue tracker submit timeout task=%s endpoint=%s type=%s",
                       task_id, endpoint, type(exc).__name__)
        _fail_all(conn, task_id=task_id, items=candidates, source=source,
                  error=message, response={}, actor=actor)
        raise IssueTrackerTimeout(message) from exc
    except httpx.HTTPError as exc:
        message = f"请求失败（{type(exc).__name__}）：{endpoint}"
        logger.warning("issue tracker submit failed task=%s endpoint=%s error=%s",
                       task_id, endpoint, exc)
        _fail_all(conn, task_id=task_id, items=candidates, source=source,
                  error=message, response={}, actor=actor)
        raise IssueTrackerError(message) from exc

    try:
        body = response.json()
    except ValueError:
        body = {}
    response_snapshot = {"status": response.status_code, "body": body}
    message = str(body.get("msg") or "") if isinstance(body, dict) else ""
    if response.status_code >= 400 or (isinstance(body, dict)
                                       and body.get("code") not in (0, "0", None)):
        error = f"HTTP {response.status_code}: {message or response.text[:500]}"
        _fail_all(conn, task_id=task_id, items=candidates, source=source,
                  error=error, response=response_snapshot, actor=actor)
        raise IssueTrackerError(error)

    external_ids = body.get("data") if isinstance(body, dict) else None
    if not isinstance(external_ids, list):
        error = "external server returned no data list"
        _fail_all(conn, task_id=task_id, items=candidates, source=source,
                  error=error, response=response_snapshot, actor=actor)
        raise IssueTrackerError(error)

    submitted: list[dict] = []
    failed: list[dict] = []
    for index, finding in enumerate(candidates):
        fid = str(finding["id"])
        request_item = items[index]
        if index >= len(external_ids):
            error = "external server returned fewer IDs than submitted items"
            failed.append({"finding_id": fid, "error": error})
            _record_submission(
                conn, task_id=task_id, finding_id=fid, source=source,
                external_id=None, status="failed", error=error,
                request=request_item, response=response_snapshot, actor=actor)
            continue
        external_id = str(external_ids[index])
        submitted.append({"finding_id": fid, "external_id": external_id})
        _record_submission(
            conn, task_id=task_id, finding_id=fid, source=source,
            external_id=external_id, status="submitted", error=None,
            request=request_item, response=response_snapshot, actor=actor)
    conn.commit()
    logger.info(
        "issue tracker submit task=%s source=%s submitted=%s failed=%s",
        task_id, source, len(submitted), len(failed))
    return {
        "source": source,
        "submitted_at": submitted_at,
        "submitted": submitted,
        "failed": failed,
    }


def test_connection(conn: sqlite3.Connection | None, url: str | None = None,
                    *, timeout: float = TEST_TIMEOUT,
                    actor: str = "human") -> dict:
    try:
        base = normalize_base_url(url or (get_settings(conn)["server_url"] if conn else ""))
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    endpoint = endpoint_url(base)
    payload = {"source": f"{SOURCE_PREFIX}-test:{_stamp()}", "list": []}
    started = time.monotonic()
    try:
        response = _post_request(endpoint, payload, timeout=timeout)
    except httpx.TimeoutException as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        message = _timeout_message(exc, timeout, endpoint)
        logger.warning("issue tracker test timeout endpoint=%s type=%s elapsed_ms=%s",
                       endpoint, type(exc).__name__, elapsed_ms)
        return {"ok": False, "endpoint": endpoint, "elapsed_ms": elapsed_ms,
                "error_type": type(exc).__name__, "message": message}
    except httpx.HTTPError as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        message = f"无法连接（{type(exc).__name__}）：{endpoint}：{exc}"
        logger.warning("issue tracker test failed endpoint=%s error=%s",
                       endpoint, exc)
        return {"ok": False, "endpoint": endpoint, "elapsed_ms": elapsed_ms,
                "error_type": type(exc).__name__, "message": message}
    latency_ms = int((time.monotonic() - started) * 1000)
    try:
        body = response.json()
    except ValueError:
        body = {}
    message = str(body.get("msg") or "") if isinstance(body, dict) else ""
    status = response.status_code
    expected_validation = status in (400, 422) and (
        "list" in message.lower() or "不能为空" in message or "source" in message.lower())
    ok = 200 <= status < 300 or expected_validation
    if not ok:
        logger.warning("issue tracker test rejected endpoint=%s status=%s message=%s",
                       endpoint, status, message[:200])
    result = {
        "ok": ok,
        "endpoint": endpoint,
        "status": status,
        "latency_ms": latency_ms,
        "message": message or ("接口可达" if ok else f"HTTP {status}"),
        "body": body if isinstance(body, dict) else {},
    }
    if conn is not None:
        _audit(conn, actor, None, "issue_tracker.test", {
            "server_url": base, "ok": ok, "status": status,
        })
        conn.commit()
    return result
