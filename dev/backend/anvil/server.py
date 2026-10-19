"""后端服务入口（dev 与 PyInstaller 打包共用）。

`anvil-server` 启动 FastAPI（uvicorn）+ 自动打开浏览器；首启引导由 `anvil init` 完成。
"""
from __future__ import annotations

import threading
import webbrowser

import uvicorn

from . import config
from .main import create_app


def main() -> None:
    config.ensure_dirs()
    app = create_app({"scheduler_enabled": True})
    url = f"http://{config.HOST}:{config.PORT}"
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")


if __name__ == "__main__":
    main()
