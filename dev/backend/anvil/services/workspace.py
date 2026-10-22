"""Shared repo / workspace tree + git status/diff（M1）。

`ws status` = git status；`ws diff` 基础 git diff（scope 越界警告在 M2 三交集）。

B 多频道：工作区即频道，`workspace_members` 记录角色↔频道多对多绑定（频道侧权威）。
"""
from __future__ import annotations

import re
import sqlite3
import subprocess

from .. import config
from ..util import new_id, now_iso


def _repo(ws_id: str):
    # 工作区 id 来自 URL，限制为单段安全字符，避免路径穿越。
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", ws_id or "")
    return config.REPOS_DIR / safe


def _git(root, args: list[str]) -> list[str]:
    try:
        r = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15
        )
        return (r.stdout or r.stderr).strip().splitlines()
    except Exception:  # noqa: BLE001
        return []


def _list_files(root) -> list[str]:
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def tree(ws_id: str) -> dict:
    root = _repo(ws_id)
    status = _git(root, ["status", "--porcelain"]) if (root / ".git").exists() else []
    return {"id": ws_id, "root": str(root), "files": _list_files(root), "status": status}


def diff(ws_id: str) -> dict:
    root = _repo(ws_id)
    changes = _git(root, ["diff", "--stat"]) if (root / ".git").exists() else []
    return {"id": ws_id, "changes": changes}


# ---- 频道 CRUD + 成员管理（B 多频道） -------------------------------------


def create_channel(conn: sqlite3.Connection, name: str, repo: str | None = None,
                   description: str | None = None, id: str | None = None) -> dict:
    wid = id or new_id()
    now = now_iso()
    conn.execute(
        "INSERT INTO workspaces (id, name, repo, description, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (wid, name, repo, description, now, now),
    )
    conn.commit()
    return get_channel(conn, wid)  # type: ignore[return-value]


def get_channel(conn: sqlite3.Connection, wid: str) -> dict | None:
    r = conn.execute("SELECT * FROM workspaces WHERE id=?", (wid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["member_count"] = conn.execute(
        "SELECT COUNT(*) AS n FROM workspace_members WHERE workspace_id=?", (wid,)
    ).fetchone()["n"]
    d["task_count"] = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE workspace_id=?", (wid,)
    ).fetchone()["n"]
    return d


def list_channels(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute("SELECT id FROM workspaces ORDER BY name"):
        c = get_channel(conn, r["id"])
        if c:
            out.append(c)
    return out


def add_member(conn: sqlite3.Connection, workspace_id: str, role_slug: str, access: str) -> list[dict]:
    conn.execute(
        "INSERT INTO workspace_members (workspace_id, role_slug, access, joined_at) VALUES (?,?,?,?) "
        "ON CONFLICT(workspace_id, role_slug) DO UPDATE SET access=excluded.access",
        (workspace_id, role_slug, access, now_iso()),
    )
    conn.commit()
    return list_members(conn, workspace_id)


def list_members(conn: sqlite3.Connection, workspace_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM workspace_members WHERE workspace_id=? ORDER BY role_slug", (workspace_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def update_member(conn: sqlite3.Connection, workspace_id: str, role_slug: str, access: str) -> list[dict]:
    conn.execute(
        "UPDATE workspace_members SET access=? WHERE workspace_id=? AND role_slug=?",
        (access, workspace_id, role_slug),
    )
    conn.commit()
    return list_members(conn, workspace_id)


def remove_member(conn: sqlite3.Connection, workspace_id: str, role_slug: str) -> bool:
    cur = conn.execute(
        "DELETE FROM workspace_members WHERE workspace_id=? AND role_slug=?", (workspace_id, role_slug)
    )
    conn.commit()
    return cur.rowcount > 0


def member_access(conn: sqlite3.Connection, workspace_id: str, role_slug: str) -> str | None:
    r = conn.execute(
        "SELECT access FROM workspace_members WHERE workspace_id=? AND role_slug=?",
        (workspace_id, role_slug),
    ).fetchone()
    return r["access"] if r else None
