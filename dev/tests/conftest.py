"""共享 fixture：L2 API 集成测试的 app/testclient/临时 SQLite/鉴权/种子数据。

契约（dev 落地时对齐）：
- 后端包 import 名为 `anvil`，暴露 `create_app(config: dict) -> FastAPI`。
- config 至少支持 `{"db_url": "sqlite+aiosqlite:///:memory:"}` 或临时文件路径。
- 鉴权：`X-Anvil-Session` 头，测试用固定 session token，绕过真实会话校验的
  轻量模式由 `ANVIL_TEST_AUTH=1` 开启（dev 需在中间件里支持该测试旁路）。
"""
from __future__ import annotations

import os

import httpx
import pytest

os.environ.setdefault("ANVIL_TEST_AUTH", "1")
# 默认关闭知识 repo 绑定，避免测试扫描真实 data/repos；特性测试显式开启并改 REPOS_DIR。
os.environ.setdefault("ANVIL_KNOWLEDGE_BINDING_MODE", "off")

# 后端 M1 落地前跳过；落地后移除这行 importorskip 即可。
app_factory = pytest.importorskip(
    "anvil.main", reason="后端 M1 未落地：backend/anvil/main.py 需暴露 create_app"
)

API = "/api/v1"
TEST_SESSION = "test-session"
TEST_ROLE = "qa-role"
TEST_RUNTIME = "claude-code"


@pytest.fixture
def app(tmp_path):
    """构建一个隔离的 FastAPI app（临时 SQLite + 测试鉴权旁路）。"""
    cfg = {"db_url": f"sqlite+aiosqlite:///{tmp_path}/test.db", "test_auth": True}
    return app_factory.create_app(cfg)


@pytest.fixture
async def client(app):
    """httpx AsyncClient（ASGITransport 直连 app，不占端口）。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as c:
        yield c


def auth_headers(role: str = TEST_ROLE, session: str = TEST_SESSION) -> dict:
    return {
        "X-Anvil-Session": session,
        "X-Anvil-Role": role,
    }


def human_headers(session: str = TEST_SESSION) -> dict:
    """自然人身份：人工确认点（评审/审批/删除/重置）只接受该身份。"""
    return {"X-Anvil-Session": session, "X-Anvil-Role": "human"}


# ---- 种子数据 factory（各测试文件复用） ---------------------------------


def runtime_payload(id_: str = TEST_RUNTIME, **kw) -> dict:
    p = {
        "id": id_,
        "display_name": "Claude Code",
        "executable_path": "/usr/local/bin/claude",
        "detected_version": "1.0.0",
        "status": "ready",
        "capabilities": {"managed_mcp": True, "tool_level_grant": "exact"},
    }
    p.update(kw)
    return p


def role_payload(slug: str = TEST_ROLE, **kw) -> dict:
    p = {
        "slug": slug,
        "display_name": "QA 角色",
        "runtime": {"id": TEST_RUNTIME},
        "workspace": {"home": f"agents/{slug}/", "repos": []},
        "tools": {"preset": "backend-default", "max_tool_tokens": 3500},
        "concurrency": {"max_parallel": 2},
    }
    p.update(kw)
    return p


def task_payload(id_: str = "T-0001", **kw) -> dict:
    p = {
        "id": id_,
        "title": "M1 冒烟任务",
        "type": "feature",
        "priority": 2,
        "tags": ["backend"],
        "repo": "main-project",
        "scope_paths": ["src/**"],
        "description": "端到端手动指派冒烟",
        "acceptance": ["跑通 submit"],
        "created_by": "human",
    }
    p.update(kw)
    return p
