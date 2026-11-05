"""运行时功能开关 API（人类在「工具」页控制）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..deps import Auth, get_auth, get_db, require_human
from ..services import features

router = APIRouter()


class FlagUpdate(BaseModel):
    value: bool


@router.get("")
def list_feature_flags(conn=Depends(get_db), auth: Auth = Depends(get_auth)):
    return {"flags": features.list_flags(conn)}


@router.put("/{key}")
def update_feature_flag(key: str, body: FlagUpdate,
                        conn=Depends(get_db),
                        auth: Auth = Depends(require_human)):
    try:
        return features.set_flag(
            conn, key, body.value, actor=auth.role or "human")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="feature flag not found") from exc


@router.delete("/{key}")
def reset_feature_flag(key: str, conn=Depends(get_db),
                       auth: Auth = Depends(require_human)):
    try:
        return features.reset_flag(conn, key, actor=auth.role or "human")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="feature flag not found") from exc
