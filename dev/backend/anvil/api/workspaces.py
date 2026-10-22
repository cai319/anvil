"""Workspace/Git 端点（M1 最小 + B 多频道：频道 CRUD + 成员管理 + 手动合并）。"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from .. import config, services
from ..deps import get_db

router = APIRouter()


# ---- 频道（workspace）CRUD ---------------------------------------------


@router.post("/workspaces", status_code=201)
def create_workspace(body: dict, conn=Depends(get_db)):
    name = body.get("name")
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    return services.workspace.create_channel(conn, name, body.get("repo"), body.get("description"), body.get("id"))


@router.get("/workspaces")
def list_workspaces(conn=Depends(get_db)):
    return services.workspace.list_channels(conn)


@router.get("/workspaces/{ws_id}")
def get_workspace(ws_id: str, conn=Depends(get_db)):
    channel = services.workspace.get_channel(conn, ws_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return channel


@router.delete("/workspaces/{ws_id}", status_code=204)
def delete_workspace(ws_id: str, conn=Depends(get_db)):
    cur = conn.execute("DELETE FROM workspaces WHERE id=?", (ws_id,))
    conn.execute("DELETE FROM workspace_members WHERE workspace_id=?", (ws_id,))
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="workspace not found")
    return None


# ---- 成员管理 ------------------------------------------------------------


@router.get("/workspaces/{ws_id}/members")
def list_members(ws_id: str, conn=Depends(get_db)):
    if services.workspace.get_channel(conn, ws_id) is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return services.workspace.list_members(conn, ws_id)


@router.post("/workspaces/{ws_id}/members", status_code=201)
def add_member(ws_id: str, body: dict, conn=Depends(get_db)):
    if services.workspace.get_channel(conn, ws_id) is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    role_slug = body.get("role_slug")
    access = body.get("access", "read")
    if not role_slug or access not in ("read", "write", "admin"):
        raise HTTPException(status_code=422, detail="role_slug required; access ∈ read|write|admin")
    return services.workspace.add_member(conn, ws_id, role_slug, access)


@router.patch("/workspaces/{ws_id}/members/{role_slug}")
def update_member(ws_id: str, role_slug: str, body: dict, conn=Depends(get_db)):
    access = body.get("access")
    if access not in ("read", "write", "admin"):
        raise HTTPException(status_code=422, detail="access ∈ read|write|admin")
    return services.workspace.update_member(conn, ws_id, role_slug, access)


@router.delete("/workspaces/{ws_id}/members/{role_slug}", status_code=204)
def remove_member(ws_id: str, role_slug: str, conn=Depends(get_db)):
    services.workspace.remove_member(conn, ws_id, role_slug)
    return None


# ---- 树 / diff -----------------------------------------------------------


@router.get("/workspaces/{ws_id}/tree")
def workspace_tree(ws_id: str, conn=Depends(get_db)):
    return services.workspace.tree(ws_id)


@router.get("/workspaces/{ws_id}/diff")
def workspace_diff(ws_id: str, conn=Depends(get_db)):
    return services.workspace.diff(ws_id)


def _git(root: Path, args: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30
        )
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


@router.post("/workspaces/{ws_id}/merge")
def merge_workspace(ws_id: str, body: dict, conn=Depends(get_db)):
    """手动合并分支（design §10）：先做越界校验，再 `git merge --no-ff`。

    body: {branch, base_branch?}；越界文件直接 409，不做部分合并。
    """
    channel = services.workspace.get_channel(conn, ws_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    branch = str(body.get("branch") or "")
    if not branch:
        raise HTTPException(status_code=422, detail="branch is required")
    repo_name = channel.get("repo") or ws_id
    safe_repo = re.sub(r"[^A-Za-z0-9_.-]", "_", repo_name or "")
    root = config.REPOS_DIR / safe_repo
    if not (root / ".git").exists():
        raise HTTPException(status_code=422, detail=f"repo {repo_name} is not a git checkout")

    base = str(body.get("base_branch") or "main")
    rc, changed_text = _git(root, ["diff", "--name-only", f"{base}...{branch}"])
    if rc != 0:
        raise HTTPException(status_code=422, detail=f"branch diff failed: {changed_text}")
    changed = [p for p in changed_text.splitlines() if p.strip()]

    patterns = [".anvil/", ".git/", ".env", "secrets/"]
    ignore = root / ".anvilignore"
    if ignore.is_file():
        patterns.extend(
            ln.strip() for ln in ignore.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        )
    violations = [
        p for p in changed
        if any(p == pat or p.startswith(pat.rstrip("/") + "/") for pat in patterns)
    ]
    if violations:
        raise HTTPException(
            status_code=409,
            detail={"error": "out of bounds", "violations": violations, "changed": changed},
        )

    rc, out = _git(root, ["merge", "--no-ff", branch, "-m", f"Merge {branch} into {base}"])
    if rc != 0:
        _git(root, ["merge", "--abort"])
        raise HTTPException(status_code=409, detail=f"merge failed: {out}")
    return {"workspace": ws_id, "base_branch": base, "branch": branch,
            "changed": changed, "output": out}
