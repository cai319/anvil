"""Runtime endpoints (§10 runtimes 段 + SE §3)."""
from __future__ import annotations

import yaml
from fastapi import APIRouter, Depends, HTTPException

from .. import config, services
from ..deps import Auth, get_db, require_human
from ..schemas import RuntimeIn, RuntimeOut

router = APIRouter()


@router.post("", status_code=201, response_model=RuntimeOut)
def register_runtime(body: RuntimeIn, conn=Depends(get_db)):
    executable = body.executable_path or body.executable
    is_manual = bool(
        body.invocation or body.discovery or body.projection
        or body.version_range or body.version_probe or body.health
        or (body.executable and not body.executable_path)
    )
    if is_manual:
        definition = {
            "id": body.id,
            "display_name": body.display_name,
            "executable": executable,
            "invocation": body.invocation,
            "capabilities": body.capabilities,
            "projection": body.projection or {},
            "discovery": body.discovery,
            "version_range": body.version_range,
            "version_probe": body.version_probe,
            "health": body.health,
        }
        return services.runtime.register_definition(conn, definition)
    return services.runtime.register(
        conn, id=body.id, display_name=body.display_name,
        executable_path=executable or "", detected_version=body.detected_version,
        status=body.status.value, capabilities=body.capabilities,
    )


@router.get("", response_model=list[RuntimeOut])
def list_runtimes(conn=Depends(get_db)):
    return services.runtime.list_all(conn)


@router.post("/discover", response_model=list[RuntimeOut])
def discover_runtimes(conn=Depends(get_db)):
    return services.runtime.discover(conn)


@router.get("/default")
def get_default_runtime(conn=Depends(get_db)):
    """当前默认运行时（AI 生成画像/角色使用）。"""
    rid = services.utility.get_default_runtime_id(conn)
    return {"id": rid, "runtime": services.runtime.get(conn, rid) if rid else None}


@router.put("/default")
def set_default_runtime(body: dict, conn=Depends(get_db),
                        auth: Auth = Depends(require_human)):
    rid = str(body.get("id") or "").strip()
    if not rid:
        raise HTTPException(status_code=422, detail="id is required")
    rt = services.runtime.get(conn, rid)
    if rt is None:
        raise HTTPException(status_code=404, detail="runtime not found")
    services.utility.set_default_runtime_id(conn, rid)
    return {"id": rid, "runtime": rt}


@router.get("/{rid}", response_model=RuntimeOut)
def get_runtime(rid: str, conn=Depends(get_db)):
    rt = services.runtime.get(conn, rid)
    if rt is None:
        raise HTTPException(status_code=404, detail="runtime not found")
    return rt


@router.get("/{rid}/definition")
def get_runtime_definition(rid: str, conn=Depends(get_db)):
    """返回运行时的 YAML 原文与来源，供 UI 编辑。"""
    if services.runtime.get(conn, rid) is None:
        raise HTTPException(status_code=404, detail="runtime not found")
    definition = services.runtime.load_definitions().get(rid)
    if not definition:
        raise HTTPException(status_code=404, detail="runtime definition not found")
    source = str(definition.get("_source") or "")
    return {
        "id": rid,
        "yaml": str(definition.get("_raw") or ""),
        "source": source,
        "builtin": source.startswith(str(config.PACKAGE_RUNTIMES_DIR)),
    }


@router.put("/{rid}/definition", response_model=RuntimeOut)
def put_runtime_definition(rid: str, body: dict, conn=Depends(get_db),
                           auth: Auth = Depends(require_human)):
    """新增/覆盖用户运行时定义：写 YAML → 探测 → 落库。"""
    raw = str(body.get("yaml") or "")
    if not raw.strip():
        raise HTTPException(status_code=422, detail="yaml is required")
    try:
        definition = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=422, detail=f"YAML 解析失败: {exc}")
    if not isinstance(definition, dict):
        raise HTTPException(status_code=422, detail="YAML 根节点必须是对象")
    definition.setdefault("id", rid)
    if str(definition["id"]) != rid:
        raise HTTPException(
            status_code=422,
            detail=f"YAML 中的 id ({definition['id']}) 与路径 ({rid}) 不一致")
    return services.runtime.register_definition(conn, definition, raw_yaml=raw)


@router.post("/{rid}/probe", response_model=RuntimeOut)
def probe_runtime(rid: str, conn=Depends(get_db)):
    rt = services.runtime.probe(conn, rid)
    if rt is None:
        raise HTTPException(status_code=404, detail="runtime not found")
    return rt


@router.post("/{rid}/health")
def health_runtime(rid: str, conn=Depends(get_db)):
    """健康检查：在临时目录跑一次最小任务（design §2.4），会消耗少量额度。"""
    result = services.runtime.health_probe(conn, rid)
    if result is None:
        raise HTTPException(status_code=404, detail="runtime not found")
    return result


@router.get("/{rid}/capabilities")
def runtime_capabilities(rid: str, conn=Depends(get_db)):
    rt = services.runtime.get(conn, rid)
    if rt is None:
        raise HTTPException(status_code=404, detail="runtime not found")
    return {"id": rid, "capabilities": rt["capabilities"]}


@router.get("/{rid}/catalog")
def runtime_catalog(rid: str, conn=Depends(get_db)):
    """该运行时自身的默认 skill / MCP 列表（供角色勾选加载）。"""
    if services.runtime.get(conn, rid) is None:
        raise HTTPException(status_code=404, detail="runtime not found")
    return services.runtime.catalog(conn, rid)


@router.put("/{rid}", response_model=RuntimeOut)
def update_runtime(rid: str, body: RuntimeIn, conn=Depends(get_db),
                   auth: Auth = Depends(require_human)):
    rt = services.runtime.get(conn, rid)
    if rt is None:
        raise HTTPException(status_code=404, detail="runtime not found")
    fields = {"display_name": body.display_name}
    if body.executable_path or body.executable:
        fields["executable_path"] = body.executable_path or body.executable
    return services.runtime.update(conn, rid, **fields)


@router.delete("/{rid}", status_code=204)
def delete_runtime(rid: str, conn=Depends(get_db),
                   auth: Auth = Depends(require_human)):
    if not services.runtime.delete(conn, rid):
        raise HTTPException(status_code=404, detail="runtime not found")
    return None
