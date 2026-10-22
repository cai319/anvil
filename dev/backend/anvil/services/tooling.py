"""工具治理（M3）：skill / MCP server 注册与握商、工具预设、token 估算。

能力继承自运行时（design §1.2 立场五）：Anvil 不自造工具，只决定**加载哪些**。
本模块补齐 design §10 的 skills / mcp / tools 接口，并为投影层提供数据。
"""
from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import yaml  # type: ignore

from .. import config
from ..util import ConflictError, jdumps, jloads, new_id, now_iso


# ---- skills ---------------------------------------------------------------


def create_skill(conn: sqlite3.Connection, *, name: str, description: str = "",
                 content: str = "", runtime: str | None = None) -> dict:
    sid = new_id()
    now = now_iso()
    try:
        conn.execute(
            "INSERT INTO skills (id, name, description, content, runtime, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (sid, name, description, content, runtime, now, now),
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError(f"skill {name} already exists") from exc
    conn.execute(
        "INSERT INTO skill_versions (skill_id, version, content, created_at) VALUES (?,?,?,?)",
        (sid, 1, content, now),
    )
    conn.commit()
    return get_skill(conn, sid)  # type: ignore[return-value]


def get_skill(conn: sqlite3.Connection, sid: str) -> dict | None:
    row = conn.execute("SELECT * FROM skills WHERE id=? OR name=?", (sid, sid)).fetchone()
    return dict(row) if row else None


def list_skills(conn: sqlite3.Connection, runtime: str | None = None) -> list[dict]:
    if runtime:
        rows = conn.execute(
            "SELECT id, name, description, runtime, updated_at FROM skills "
            "WHERE runtime IS NULL OR runtime=? ORDER BY name", (runtime,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, name, description, runtime, updated_at FROM skills ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def update_skill(conn: sqlite3.Connection, sid: str, **fields) -> dict | None:
    skill = get_skill(conn, sid)
    if skill is None:
        return None
    allowed = {k: v for k, v in fields.items() if k in ("name", "description", "content", "runtime")}
    if not allowed:
        return skill
    allowed["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in allowed)
    conn.execute(f"UPDATE skills SET {cols} WHERE id=?", (*allowed.values(), skill["id"]))
    if "content" in allowed:
        row = conn.execute(
            "SELECT COALESCE(MAX(version),0) AS v FROM skill_versions WHERE skill_id=?", (skill["id"],)
        ).fetchone()
        conn.execute(
            "INSERT INTO skill_versions (skill_id, version, content, created_at) VALUES (?,?,?,?)",
            (skill["id"], row["v"] + 1, allowed["content"], now_iso()),
        )
    conn.commit()
    return get_skill(conn, skill["id"])


def delete_skill(conn: sqlite3.Connection, sid: str) -> bool:
    skill = get_skill(conn, sid)
    if skill is None:
        return False
    conn.execute("DELETE FROM skill_versions WHERE skill_id=?", (skill["id"],))
    conn.execute("DELETE FROM skills WHERE id=?", (skill["id"],))
    conn.commit()
    return True


def skill_versions(conn: sqlite3.Connection, sid: str) -> list[dict]:
    skill = get_skill(conn, sid)
    if skill is None:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT version, created_at, length(content) AS chars FROM skill_versions "
        "WHERE skill_id=? ORDER BY version DESC", (skill["id"],)
    )]


def skill_compat(conn: sqlite3.Connection, sid: str) -> dict:
    skill = get_skill(conn, sid)
    if skill is None:
        return {}
    return {
        "skill": skill["name"],
        "runtime": skill["runtime"],
        "compatible": "all" if not skill["runtime"] else [skill["runtime"]],
    }


# ---- MCP servers ----------------------------------------------------------


def create_mcp_server(conn: sqlite3.Connection, *, name: str, transport: str = "stdio",
                      command: str | None = None, args: list[str] | None = None,
                      env: dict[str, str] | None = None, url: str | None = None) -> dict:
    mid = new_id()
    now = now_iso()
    try:
        conn.execute(
            "INSERT INTO mcp_servers (id, name, transport, command, args_json, env_json, url, "
            "tools_json, enabled, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (mid, name, transport, command, jdumps(args or []), jdumps(env or {}),
             url, "[]", 1, now, now),
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError(f"mcp server {name} already exists") from exc
    conn.commit()
    return get_mcp_server(conn, mid)  # type: ignore[return-value]


def get_mcp_server(conn: sqlite3.Connection, mid: str) -> dict | None:
    row = conn.execute("SELECT * FROM mcp_servers WHERE id=? OR name=?", (mid, mid)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["args"] = jloads(d.pop("args_json"), [])
    d["env"] = jloads(d.pop("env_json"), {})
    d["tools"] = jloads(d.pop("tools_json"), [])
    d["enabled"] = bool(d["enabled"])
    return d


def list_mcp_servers(conn: sqlite3.Connection) -> list[dict]:
    return [get_mcp_server(conn, r["id"]) for r in conn.execute(  # type: ignore[misc]
        "SELECT id FROM mcp_servers ORDER BY name"
    )]


def update_mcp_server(conn: sqlite3.Connection, mid: str, **fields) -> dict | None:
    server = get_mcp_server(conn, mid)
    if server is None:
        return None
    allowed: dict = {}
    for k in ("name", "transport", "command", "url"):
        if k in fields and fields[k] is not None:
            allowed[k] = fields[k]
    if "args" in fields and fields["args"] is not None:
        allowed["args_json"] = jdumps(fields["args"])
    if "env" in fields and fields["env"] is not None:
        allowed["env_json"] = jdumps(fields["env"])
    if "enabled" in fields and fields["enabled"] is not None:
        allowed["enabled"] = 1 if fields["enabled"] else 0
    if not allowed:
        return server
    allowed["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in allowed)
    conn.execute(f"UPDATE mcp_servers SET {cols} WHERE id=?", (*allowed.values(), server["id"]))
    conn.commit()
    return get_mcp_server(conn, server["id"])


def delete_mcp_server(conn: sqlite3.Connection, mid: str) -> bool:
    server = get_mcp_server(conn, mid)
    if server is None:
        return False
    conn.execute("DELETE FROM mcp_servers WHERE id=?", (server["id"],))
    conn.commit()
    return True


def _read_line(q: queue.Queue, deadline: float) -> str | None:
    remaining = max(0.0, deadline - time.monotonic())
    try:
        return q.get(timeout=remaining)
    except queue.Empty:
        return None


def probe_mcp_server(conn: sqlite3.Connection, mid: str, timeout: float = 8.0) -> dict | None:
    """最小 MCP stdio 握商：initialize → initialized → tools/list。

    HTTP/SSE transport 暂只做可达性检查（不做完整 streamable HTTP 会话）。
    握商结果（工具名/描述）缓存到 tools_json。
    """
    server = get_mcp_server(conn, mid)
    if server is None:
        return None

    if server["transport"] != "stdio":
        return {"id": server["id"], "ok": None, "detail": f"暂不支持 {server['transport']} 握商", "tools": []}
    if not server["command"]:
        return {"id": server["id"], "ok": False, "detail": "缺少 command", "tools": []}

    merged_env = {**os.environ, **{str(k): str(v) for k, v in (server["env"] or {}).items()}}
    try:
        proc = subprocess.Popen(
            [server["command"], *[str(a) for a in server["args"]]],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=merged_env,
        )
    except Exception as exc:  # noqa: BLE001
        return {"id": server["id"], "ok": False, "detail": str(exc), "tools": []}
    try:
        if proc.stdin:
            proc.stdin.reconfigure(newline="\n")
    except Exception:  # noqa: BLE001
        pass

    q: queue.Queue = queue.Queue()

    def reader() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                q.put(line)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=reader, daemon=True).start()

    def send(msg: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def await_id(msg_id: int, deadline: float) -> dict | None:
        while True:
            line = _read_line(q, deadline)
            if line is None:
                return None
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == msg_id:
                return msg

    deadline = time.monotonic() + timeout
    try:
        send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "anvil", "version": "0.1.0"},
            },
        })
        init = await_id(1, deadline)
        if init is None or "error" in init:
            detail = (init or {}).get("error", "initialize timeout")
            return {"id": server["id"], "ok": False, "detail": str(detail), "tools": []}

        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listing = await_id(2, deadline)
        tools = []
        if listing and "result" in listing:
            tools = [
                {"name": t.get("name"), "description": t.get("description", "")}
                for t in (listing["result"].get("tools") or [])
            ]
        conn.execute(
            "UPDATE mcp_servers SET tools_json=?, updated_at=? WHERE id=?",
            (jdumps(tools), now_iso(), server["id"]),
        )
        conn.commit()
        return {
            "id": server["id"], "ok": listing is not None, "tools": tools,
            "server_info": (init.get("result") or {}).get("serverInfo", {}),
        }
    except Exception as exc:  # noqa: BLE001 —— 握商失败返回结构化错误，不让端点 500
        return {"id": server["id"], "ok": False, "detail": str(exc), "tools": []}
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


def mcp_config_for(conn: sqlite3.Connection, names: list[str]) -> dict[str, dict]:
    """把 DB 中登记的 MCP server 转成运行时配置文件里的 mcpServers 结构。"""
    out: dict[str, dict] = {}
    for name in names:
        server = get_mcp_server(conn, name)
        if not server or not server["enabled"]:
            continue
        if server["transport"] == "stdio":
            cfg = {"command": server["command"], "args": server["args"]}
            if server["env"]:
                cfg["env"] = server["env"]
        else:
            cfg = {"url": server["url"]}
        out[name] = cfg
    return out


# ---- tool presets ---------------------------------------------------------


def load_presets() -> dict:
    """读取 tools.yaml（用户目录优先，内置兜底）。

    结构：`presets: {name: {max_tool_tokens, allowlist: [{server, tools}]}}`
    """
    merged: dict = {}
    paths = [config.DATA_DIR / "tools.yaml", Path(__file__).resolve().parent.parent / "tools.yaml"]
    for p in paths:
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        merged.update(data.get("presets") or {})
    return merged
