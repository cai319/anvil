"""导出当前 FastAPI 应用的 OpenAPI 规格，供前端生成 TypeScript 类型。

用法（dev/ 目录）：
    .venv/bin/python backend/dump_openapi.py frontend/openapi.json

前端随后执行：
    cd frontend && npm run gen:api
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def main(argv: list[str]) -> int:
    out = Path(argv[1] if len(argv) > 1 else "openapi.json")
    data_dir = Path(tempfile.mkdtemp(prefix="anvil-openapi-"))
    os.environ.setdefault("ANVIL_DATA_DIR", str(data_dir))
    from anvil.main import create_app

    app = create_app({
        "db_url": f"sqlite+aiosqlite:///{data_dir}/openapi.db",
        "test_auth": True,
    })
    spec = app.openapi()
    out.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"wrote {out} ({len(spec.get('paths', {}))} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
