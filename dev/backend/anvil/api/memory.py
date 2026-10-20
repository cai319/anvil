"""Agent self-service endpoints: memory / notes / proposals / lessons (§3.2/§3.4)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import services
from ..deps import Auth, get_auth, get_db, require_human
from ..schemas import InstructionIn, LessonIn, MemoryAppendIn, NoteWriteIn, ProposalIn
from ..util import ConflictError
from . import events

router = APIRouter()


@router.get("/me")
def get_me(conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """anvil self show — 返回自身身份 + 并发额度 + 已用成本（SE §3.3）。"""
    from ..util import jloads

    role_slug = auth.role or "qa-role"
    cost = conn.execute(
        "SELECT COALESCE(SUM(usd), 0) FROM cost_ledger WHERE role_slug=?", (role_slug,)
    ).fetchone()[0]
    profile: dict = {}
    role = services.roles.get(conn, role_slug)
    if role:
        profile = jloads(role["definition_json"], {})
    concurrency = profile.get("concurrency", {})
    return {
        "role_slug": role_slug,
        "profile": profile,
        "concurrency_quota": concurrency,
        "cost_used_usd": cost,
    }


@router.post("/me/memory")
def append_memory(body: MemoryAppendIn, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    return services.memory.append_memory(conn, auth.role or "qa-role", body.content)


@router.get("/me/memory/search")
def search_memory(q: str, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    return services.memory.search_memory(conn, auth.role or "qa-role", q)


@router.post("/me/notes")
def write_note(body: NoteWriteIn, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    return services.memory.write_note(conn, auth.role or "qa-role", body.path, body.content)


@router.post("/me/instruction")
def write_instruction(body: InstructionIn, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    """`anvil self edit-instruction`（自主档，§9.2）：直接写 instruction.md。"""
    return services.memory.write_instruction(conn, auth.role or "qa-role", body.content)


@router.get("/proposals")
def list_proposals(status: str | None = None, conn=Depends(get_db)):
    return services.memory.list_proposals(conn, status)


@router.post("/proposals", status_code=201)
def create_proposal(body: ProposalIn, conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    proposal = services.memory.create_proposal(
        conn, auth.role or "qa-role", body.field, body.proposed, body.rationale, body.risk,
    )
    events.publish("proposal", {"id": proposal["id"], "status": "pending"})
    return proposal


@router.post("/proposals/{pid}/approve")
def approve_proposal(pid: str, conn=Depends(get_db), auth: Auth = Depends(require_human)):
    """审批通过：把提案字段落到角色配置（§9.2 审批档）。"""
    p = services.memory.approve_proposal(conn, pid)
    if p is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    events.publish("proposal", {"id": pid, "status": "approved"})
    return p


@router.post("/proposals/{pid}/reject")
def reject_proposal(pid: str, conn=Depends(get_db), auth: Auth = Depends(require_human)):
    p = services.memory.reject_proposal(conn, pid)
    if p is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    events.publish("proposal", {"id": pid, "status": "rejected"})
    return p


@router.get("/lessons")
def list_lessons(status: str | None = None, repo: str | None = None,
                 q: str | None = None, conn=Depends(get_db)):
    return services.memory.list_lessons(conn, status, repo, q)


@router.post("/lessons/pending", status_code=201)
def propose_lesson(body: LessonIn, conn=Depends(get_db)):
    return services.memory.create_lesson(
        conn, body.content, body.tags, repo=body.repo or "shared", source_task=body.source_task,
    )


@router.get("/lessons/pending")
def list_pending_lessons(conn=Depends(get_db)):
    return services.memory.list_lessons(conn, status="pending")


@router.put("/lessons/{lid}")
def update_lesson(lid: str, body: dict, conn=Depends(get_db)):
    lesson = services.memory.update_lesson(
        conn, lid, content=body.get("content"), tags=body.get("tags"), repo=body.get("repo"),
    )
    if lesson is None:
        raise HTTPException(status_code=404, detail="lesson not found")
    return lesson


@router.post("/lessons/{lid}/approve")
def approve_lesson(lid: str, conn=Depends(get_db), auth: Auth = Depends(require_human)):
    try:
        lesson = services.memory.approve_lesson(conn, lid)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if lesson is None:
        raise HTTPException(status_code=404, detail="lesson not found")
    events.publish("proposal", {"id": lid, "kind": "lesson", "status": lesson["status"]})
    return lesson


@router.post("/lessons/{lid}/reject")
def reject_lesson(lid: str, conn=Depends(get_db), auth: Auth = Depends(require_human)):
    lesson = services.memory.reject_lesson(conn, lid)
    if lesson is None:
        raise HTTPException(status_code=404, detail="lesson not found")
    return lesson


@router.delete("/lessons/{lid}", status_code=204)
def delete_lesson(lid: str, conn=Depends(get_db)):
    if not services.memory.delete_lesson(conn, lid):
        raise HTTPException(status_code=404, detail="lesson not found")
    return None
