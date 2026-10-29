"""Workspace/Git 端点（M1 最小 + B 多频道：频道 CRUD + 成员管理 + 手动合并）。"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from .. import config, services
from ..deps import Auth, get_auth, get_db, require_human
from ..util import now_iso

router = APIRouter()


# ---- 频道（workspace）CRUD ---------------------------------------------


@router.post("/workspaces", status_code=201)
def create_workspace(body: dict, conn=Depends(get_db),
                     auth: Auth = Depends(require_human)):
    name = body.get("name")
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    return services.workspace.create_channel(
        conn, name, body.get("repo"), body.get("description"), body.get("id"),
        knowledge_repo=body.get("knowledge_repo"),
    )


@router.get("/workspaces")
def list_workspaces(conn=Depends(get_db)):
    return services.workspace.list_channels(conn)


@router.get("/workspaces/{ws_id}")
def get_workspace(ws_id: str, conn=Depends(get_db)):
    channel = services.workspace.get_channel(conn, ws_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return channel


@router.patch("/workspaces/{ws_id}")
def update_workspace(ws_id: str, body: dict, conn=Depends(get_db),
                     auth: Auth = Depends(require_human)):
    name = body.get("name")
    if name is not None and not str(name).strip():
        raise HTTPException(status_code=422, detail="name cannot be empty")
    channel = services.workspace.update_channel(
        conn, ws_id,
        name=str(name).strip() if name is not None else None,
        repo=body.get("repo"),
        description=body.get("description"),
        knowledge_repo=body.get("knowledge_repo"),
        update_repo="repo" in body,
        update_description="description" in body,
        update_knowledge_repo="knowledge_repo" in body,
    )
    if channel is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return channel


@router.delete("/workspaces/{ws_id}", status_code=204)
def delete_workspace(ws_id: str, conn=Depends(get_db),
                     auth: Auth = Depends(require_human)):
    cur = conn.execute("DELETE FROM workspaces WHERE id=?", (ws_id,))
    conn.execute("DELETE FROM workspace_members WHERE workspace_id=?", (ws_id,))
    conn.execute("DELETE FROM workspace_participants WHERE workspace_id=?", (ws_id,))
    conn.execute(
        "DELETE FROM workspace_knowledge_versions WHERE knowledge_id IN "
        "(SELECT id FROM workspace_knowledge WHERE workspace_id=?)", (ws_id,)
    )
    conn.execute("DELETE FROM workspace_knowledge WHERE workspace_id=?", (ws_id,))
    conn.execute(
        "UPDATE tasks SET workspace_id=NULL, updated_at=? WHERE workspace_id=?",
        (now_iso(), ws_id),
    )
    conn.execute("UPDATE board_drafts SET workspace_id=NULL WHERE workspace_id=?", (ws_id,))
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="workspace not found")
    return None


# ---- 参与角色（无 access；仅作自动派发偏好/展示） --------------------------


@router.get("/workspaces/{ws_id}/participants")
def list_participants(ws_id: str, conn=Depends(get_db)):
    if services.workspace.get_channel(conn, ws_id) is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return services.workspace.list_participants(conn, ws_id)


@router.post("/workspaces/{ws_id}/participants", status_code=201)
def add_participant(ws_id: str, body: dict, conn=Depends(get_db),
                    auth: Auth = Depends(require_human)):
    if services.workspace.get_channel(conn, ws_id) is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    role_slug = str(body.get("role_slug") or "").strip()
    if not role_slug:
        raise HTTPException(status_code=422, detail="role_slug is required")
    role = services.roles.get(conn, role_slug)
    if role is None:
        raise HTTPException(status_code=409, detail=f"role {role_slug} does not exist")
    if services.roles.is_onboarding(conn, role_slug):
        raise HTTPException(status_code=409, detail="onboarding assistant cannot be a participant")
    return services.workspace.add_participant(conn, ws_id, role_slug)


@router.delete("/workspaces/{ws_id}/participants/{role_slug}", status_code=204)
def remove_participant(ws_id: str, role_slug: str, conn=Depends(get_db),
                       auth: Auth = Depends(require_human)):
    if services.workspace.get_channel(conn, ws_id) is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    services.workspace.remove_participant(conn, ws_id, role_slug)
    return None


# ---- 频道知识（知识卡） ---------------------------------------------------


@router.get("/workspaces/{ws_id}/knowledge")
def list_workspace_knowledge(ws_id: str, status: str | None = None,
                             kind: str | None = None, sort: str | None = None,
                             conn=Depends(get_db)):
    if services.workspace.get_channel(conn, ws_id) is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return services.workspace_knowledge.list_for_workspace(
        conn, ws_id, status=status, kind=kind, sort=sort)


@router.get("/workspaces/{ws_id}/knowledge/review")
def review_workspace_knowledge(ws_id: str, conn=Depends(get_db)):
    if services.workspace.get_channel(conn, ws_id) is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return services.workspace_knowledge.review_candidates(conn, ws_id)


@router.post("/workspaces/{ws_id}/knowledge", status_code=201)
def create_workspace_knowledge(ws_id: str, body: dict, conn=Depends(get_db),
                               auth: Auth = Depends(get_auth)):
    is_real_agent = bool(auth.role and auth.role != "human" and not auth.test_bypass)
    actor = auth.role or "human"
    try:
        return services.workspace_knowledge.create(
            conn, ws_id,
            title=str(body.get("title") or "").strip(),
            tldr=str(body.get("tldr") or "").strip(),
            kind=str(body.get("kind") or ""),
            authority=body.get("authority"),
            fields=body.get("fields") or {},
            tags=body.get("tags") or [],
            source_task_id=body.get("source_task_id"),
            source_subtask_id=body.get("source_subtask_id"),
            owner=body.get("owner"),
            review_at=body.get("review_at"),
            expires_at=body.get("expires_at"),
            related=body.get("related") or [],
            supersedes_id=body.get("supersedes_id"),
            actor=actor,
            origin="agent" if is_real_agent else "human",
            status="proposed" if is_real_agent else "approved",
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="workspace not found")
    except services.tasks.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


def _workspace_knowledge_or_404(conn, ws_id: str, kid: str) -> dict:
    card = services.workspace_knowledge.get(conn, kid)
    if card is None or card.get("workspace_id") != ws_id:
        raise HTTPException(status_code=404, detail="knowledge not found")
    return card


@router.patch("/workspaces/{ws_id}/knowledge/{kid}")
def update_workspace_knowledge(ws_id: str, kid: str, body: dict,
                               conn=Depends(get_db),
                               auth: Auth = Depends(require_human)):
    _workspace_knowledge_or_404(conn, ws_id, kid)
    try:
        return services.workspace_knowledge.update(
            conn, kid,
            expected_version=body.get("expected_version"),
            actor=auth.role,
            title=body.get("title"),
            tldr=body.get("tldr"),
            kind=body.get("kind"),
            authority=body.get("authority"),
            fields=body.get("fields"),
            tags=body.get("tags"),
            owner=body.get("owner"),
            review_at=body.get("review_at"),
            expires_at=body.get("expires_at"),
            related=body.get("related"),
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="knowledge not found")
    except services.tasks.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/workspaces/{ws_id}/knowledge/{kid}/approve")
def approve_workspace_knowledge(ws_id: str, kid: str, body: dict | None = None,
                                conn=Depends(get_db),
                                auth: Auth = Depends(require_human)):
    _workspace_knowledge_or_404(conn, ws_id, kid)
    try:
        return services.workspace_knowledge.approve(
            conn, kid, expected_version=(body or {}).get("expected_version"), actor=auth.role)
    except LookupError:
        raise HTTPException(status_code=404, detail="knowledge not found")
    except services.tasks.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/workspaces/{ws_id}/knowledge/{kid}/reject")
def reject_workspace_knowledge(ws_id: str, kid: str, body: dict | None = None,
                               conn=Depends(get_db),
                               auth: Auth = Depends(require_human)):
    _workspace_knowledge_or_404(conn, ws_id, kid)
    try:
        return services.workspace_knowledge.reject(
            conn, kid, expected_version=(body or {}).get("expected_version"), actor=auth.role)
    except LookupError:
        raise HTTPException(status_code=404, detail="knowledge not found")
    except services.tasks.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/workspaces/{ws_id}/knowledge/{kid}/resolve-conflicts")
def resolve_workspace_knowledge_conflicts(ws_id: str, kid: str,
                                          conn=Depends(get_db),
                                          auth: Auth = Depends(require_human)):
    _workspace_knowledge_or_404(conn, ws_id, kid)
    try:
        return services.workspace_knowledge.resolve_conflicts(
            conn, kid, actor=auth.role)
    except LookupError:
        raise HTTPException(status_code=404, detail="knowledge not found")


@router.delete("/workspaces/{ws_id}/knowledge/{kid}", status_code=204)
def delete_workspace_knowledge(ws_id: str, kid: str, conn=Depends(get_db),
                               auth: Auth = Depends(require_human)):
    _workspace_knowledge_or_404(conn, ws_id, kid)
    services.workspace_knowledge.delete(conn, kid)
    return None


@router.post("/workspaces/{ws_id}/knowledge/harvest", status_code=201)
def harvest_workspace_knowledge(ws_id: str, body: dict, conn=Depends(get_db),
                                auth: Auth = Depends(get_auth)):
    task_id = str(body.get("task_id") or "").strip()
    if not task_id:
        raise HTTPException(status_code=422, detail="task_id is required")
    is_real_agent = bool(auth.role and auth.role != "human" and not auth.test_bypass)
    actor = auth.role or "human"
    try:
        return services.workspace_knowledge.harvest_from_task(
            conn, ws_id, task_id, actor=actor if not is_real_agent else "system")
    except LookupError:
        raise HTTPException(status_code=404, detail="task or workspace not found")
    except services.tasks.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ---- 成员管理（旧接口，保留一个版本；忽略 access） ------------------------


@router.get("/workspaces/{ws_id}/members")
def list_members(ws_id: str, conn=Depends(get_db)):
    if services.workspace.get_channel(conn, ws_id) is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return services.workspace.list_members(conn, ws_id)


@router.post("/workspaces/{ws_id}/members", status_code=201)
def add_member(ws_id: str, body: dict, conn=Depends(get_db),
               auth: Auth = Depends(require_human)):
    if services.workspace.get_channel(conn, ws_id) is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    role_slug = body.get("role_slug")
    access = body.get("access", "read")
    if not role_slug or access not in ("read", "write", "admin"):
        raise HTTPException(status_code=422, detail="role_slug required; access ∈ read|write|admin")
    return services.workspace.add_member(conn, ws_id, role_slug, access)


@router.patch("/workspaces/{ws_id}/members/{role_slug}")
def update_member(ws_id: str, role_slug: str, body: dict, conn=Depends(get_db),
                  auth: Auth = Depends(require_human)):
    access = body.get("access")
    if access not in ("read", "write", "admin"):
        raise HTTPException(status_code=422, detail="access ∈ read|write|admin")
    return services.workspace.update_member(conn, ws_id, role_slug, access)


@router.delete("/workspaces/{ws_id}/members/{role_slug}", status_code=204)
def remove_member(ws_id: str, role_slug: str, conn=Depends(get_db),
                  auth: Auth = Depends(require_human)):
    services.workspace.remove_member(conn, ws_id, role_slug)
    return None


# ---- 树 / diff -----------------------------------------------------------


@router.get("/workspaces/{ws_id}/tree")
def workspace_tree(ws_id: str, conn=Depends(get_db),
                   auth: Auth = Depends(get_auth)):
    return services.workspace.tree(ws_id)


@router.get("/workspaces/{ws_id}/diff")
def workspace_diff(ws_id: str, conn=Depends(get_db),
                   auth: Auth = Depends(get_auth)):
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
def merge_workspace(ws_id: str, body: dict, conn=Depends(get_db),
                    auth: Auth = Depends(require_human)):
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
    for ref in (base, branch):
        if (not re.match(r"^[A-Za-z0-9._/-]+$", ref)
                or ref.startswith(("-", "/")) or ".." in ref):
            raise HTTPException(status_code=422, detail=f"invalid git ref: {ref!r}")
        rc, _ = _git(root, ["rev-parse", "--verify", ref])
        if rc != 0:
            raise HTTPException(status_code=422, detail=f"unknown git ref: {ref!r}")
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
