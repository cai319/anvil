"""工具治理端点（M3 / design §10）：skills、MCP servers、工具预设。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import services
from ..deps import Auth, get_auth, get_db, require_human
from ..util import ConflictError

router = APIRouter()


# ---- skills ---------------------------------------------------------------


@router.get("/skills")
def list_skills(runtime: str | None = None, conn=Depends(get_db)):
    return services.tooling.list_skills(conn, runtime)


@router.post("/skills", status_code=201)
def create_skill(body: dict, conn=Depends(get_db),
                 auth: Auth = Depends(require_human)):
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    try:
        return services.tooling.create_skill(
            conn, name=name, description=str(body.get("description") or ""),
            content=str(body.get("content") or ""), runtime=body.get("runtime"),
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/skills/{sid}")
def get_skill(sid: str, conn=Depends(get_db)):
    skill = services.tooling.get_skill(conn, sid)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return skill


@router.put("/skills/{sid}")
def update_skill(sid: str, body: dict, conn=Depends(get_db),
                 auth: Auth = Depends(require_human)):
    try:
        skill = services.tooling.update_skill(conn, sid, **body)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return skill


@router.delete("/skills/{sid}", status_code=204)
def delete_skill(sid: str, conn=Depends(get_db),
                 auth: Auth = Depends(require_human)):
    if not services.tooling.delete_skill(conn, sid):
        raise HTTPException(status_code=404, detail="skill not found")
    return None


@router.get("/skills/{sid}/versions")
def skill_versions(sid: str, conn=Depends(get_db)):
    if services.tooling.get_skill(conn, sid) is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return services.tooling.skill_versions(conn, sid)


@router.get("/skills/{sid}/compat")
def skill_compat(sid: str, conn=Depends(get_db)):
    compat = services.tooling.skill_compat(conn, sid)
    if not compat:
        raise HTTPException(status_code=404, detail="skill not found")
    return compat


# ---- MCP servers ----------------------------------------------------------


@router.get("/mcp/servers")
def list_mcp_servers(conn=Depends(get_db)):
    return services.tooling.list_mcp_servers(conn)


@router.post("/mcp/servers", status_code=201)
def create_mcp_server(body: dict, conn=Depends(get_db),
                      auth: Auth = Depends(require_human)):
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    transport = str(body.get("transport") or "stdio")
    if transport not in ("stdio", "http", "sse"):
        raise HTTPException(status_code=422, detail="transport ∈ stdio|http|sse")
    try:
        return services.tooling.create_mcp_server(
            conn, name=name, transport=transport, command=body.get("command"),
            args=body.get("args") or [], env=body.get("env") or {}, url=body.get("url"),
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/mcp/servers/{mid}")
def get_mcp_server(mid: str, conn=Depends(get_db)):
    server = services.tooling.get_mcp_server(conn, mid)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    return server


@router.put("/mcp/servers/{mid}")
def update_mcp_server(mid: str, body: dict, conn=Depends(get_db),
                      auth: Auth = Depends(require_human)):
    server = services.tooling.update_mcp_server(conn, mid, **body)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    return server


@router.delete("/mcp/servers/{mid}", status_code=204)
def delete_mcp_server(mid: str, conn=Depends(get_db),
                      auth: Auth = Depends(require_human)):
    if not services.tooling.delete_mcp_server(conn, mid):
        raise HTTPException(status_code=404, detail="mcp server not found")
    return None


@router.post("/mcp/servers/{mid}/test")
def test_mcp_server(mid: str, conn=Depends(get_db),
                    auth: Auth = Depends(require_human)):
    """MCP 握手探测（stdio：initialize + tools/list），刷新工具缓存。"""
    result = services.tooling.probe_mcp_server(conn, mid)
    if result is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    return result


@router.get("/mcp/servers/{mid}/tools")
def mcp_server_tools(mid: str, conn=Depends(get_db)):
    server = services.tooling.get_mcp_server(conn, mid)
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    tools = server["tools"]
    return {
        "server": server["name"],
        "tools": [
            {**t, "estimated_tokens": max(80, int(len(str(t)) / 3.5))} for t in tools
        ],
    }


# ---- tool presets ---------------------------------------------------------


@router.get("/tools/presets")
def list_tool_presets():
    return services.tooling.load_presets()
