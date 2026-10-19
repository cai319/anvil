"""Inbox 端点（信息架构重构）：人的「待我处理」统一入口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import services
from ..deps import get_db

router = APIRouter()


@router.get("/inbox")
def get_inbox(conn=Depends(get_db)):
    """聚合四类人机决策点：评审请求 / @human 提及 / 待审草案 / 待审提案（lessons+proposals）。

    只读；操作复用现有端点（POST /tasks/{id}/review、/board/drafts/approve、
    /lessons/{id}/approve、/proposals/{id}/approve 等）。
    """
    return services.inbox.collect(conn)
