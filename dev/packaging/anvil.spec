# -*- mode: python ; coding: utf-8 -*-
"""Anvil PyInstaller 单目录分发 spec（跨平台）。

产出 `anvil`（CLI）与 `anvil-server`（后端），前端静态 + 内置运行时定义随包。
Windows/macOS/Linux 共用本 spec，只需在目标机装依赖后 `pyinstaller packaging/anvil.spec`。
"""
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent  # dev/

# uvicorn 动态导入的 loop/protocol 实现，打包时必须显式声明。
UVICORN_HIDDEN = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    # FastAPI multipart 解析用于工具目录/压缩包导入。
    "python_multipart",
    "python_multipart.multipart",
]

# ---- anvil CLI ---------------------------------------------------------
cli = Analysis(
    [str(ROOT / "agentcli" / "anvil_cli" / "main.py")],
    pathex=[str(ROOT / "agentcli")],
    binaries=[],
    datas=[],
    hiddenimports=[],
    excludes=[],
    noarchive=False,
)
cli_pyz = PYZ(cli.pure)
cli_exe = EXE(
    cli_pyz, cli.scripts, [],
    exclude_binaries=True,
    name="anvil",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

# ---- anvil-server ------------------------------------------------------
server = Analysis(
    [str(ROOT / "backend" / "run_server.py")],
    pathex=[str(ROOT / "backend")],
    binaries=[],
    datas=[
        (str(ROOT / "backend" / "anvil" / "runtimes"), "anvil/runtimes"),
        (str(ROOT / "backend" / "anvil" / "tools.yaml"), "anvil"),
        (str(ROOT / "frontend" / "dist"), "frontend"),
    ],
    hiddenimports=UVICORN_HIDDEN,
    excludes=[],
    noarchive=False,
)
server_pyz = PYZ(server.pure)
server_exe = EXE(
    server_pyz, server.scripts, [],
    exclude_binaries=True,
    name="anvil-server",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    cli_exe, cli.binaries, cli.datas,
    server_exe, server.binaries, server.datas,
    strip=False,
    upx=False,
    name="anvil-dist",
)
