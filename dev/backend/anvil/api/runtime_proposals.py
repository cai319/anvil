"""Runtime improvement proposal API (v1.50 P4)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import Auth, get_auth, get_db, require_human
from ..services import runtime_proposals

router = APIRouter()


class ProposalIn(BaseModel):
    component_type: str
    component_key: str
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    proposed_change_md: str = Field(min_length=1)
    severity: str = "medium"
    evidence: dict = Field(default_factory=dict)
    root_cause_hypothesis: str | None = None
    verification_md: str | None = None
    risk_md: str | None = None
    runtime_id: str | None = None
    phase: str | None = None


class EvidenceIn(BaseModel):
    evidence: dict = Field(default_factory=dict)


class TransitionIn(BaseModel):
    status: str
    note: str | None = None


@router.get("/runtime-proposals")
def list_proposals(status: str | None = None, conn=Depends(get_db),
                   auth: Auth = Depends(get_auth)):
    return {"items": runtime_proposals.list_proposals(conn, status)}


@router.post("/runtime-proposals", status_code=201)
def create_proposal(body: ProposalIn, conn=Depends(get_db),
                    auth: Auth = Depends(get_auth)):
    try:
        result = runtime_proposals.create_or_update(
            conn, component_type=body.component_type,
            component_key=body.component_key, title=body.title,
            summary=body.summary, proposed_change_md=body.proposed_change_md,
            severity=body.severity, origin="agent",
            origin_role=auth.role, runtime_id=body.runtime_id,
            evidence=body.evidence,
            root_cause_hypothesis=body.root_cause_hypothesis,
            verification_md=body.verification_md, risk_md=body.risk_md,
            phase=body.phase)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


@router.get("/runtime-proposals/{proposal_id}")
def get_proposal(proposal_id: str, conn=Depends(get_db),
                 auth: Auth = Depends(get_auth)):
    proposal = runtime_proposals.get_proposal(conn, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    return proposal


@router.post("/runtime-proposals/{proposal_id}/evidence")
def add_evidence(proposal_id: str, body: EvidenceIn, conn=Depends(get_db),
                 auth: Auth = Depends(get_auth)):
    try:
        return runtime_proposals.add_evidence(conn, proposal_id, body.evidence)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runtime-proposals/{proposal_id}/transition")
def transition(proposal_id: str, body: TransitionIn, conn=Depends(get_db),
               auth: Auth = Depends(require_human)):
    try:
        return runtime_proposals.transition(
            conn, proposal_id, body.status, actor=auth.role or "human",
            note=body.note)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # ConflictError
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/runtime-proposals/{proposal_id}/markdown")
def get_markdown(proposal_id: str, conn=Depends(get_db),
                 auth: Auth = Depends(require_human)):
    proposal = runtime_proposals.get_proposal(conn, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    path = runtime_proposals.markdown_path(proposal_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="markdown not generated")
    return {"id": proposal_id, "content": path.read_text(encoding="utf-8")}


@router.post("/runtime-proposals/{proposal_id}/export")
def export(proposal_id: str, conn=Depends(get_db),
           auth: Auth = Depends(require_human)):
    try:
        path = runtime_proposals.export(conn, proposal_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": proposal_id, "path": str(path)}
