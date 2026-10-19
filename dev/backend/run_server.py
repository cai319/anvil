"""PyInstaller 入口（绝对导入），运行 `anvil.server.main`。

`anvil/server.py` 用相对导入（属 `anvil` 包），不能直接当顶层脚本；本文件做绝对导入。
"""
from anvil.server import main

if __name__ == "__main__":
    main()
