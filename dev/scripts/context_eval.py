#!/usr/bin/env python3
"""Phase 0 eval harness：从 context_manifests 汇总上下文/隔离基线。

只读 SQLite + 文件系统，不修改任何数据。示例：

    cd dev && python3 scripts/context_eval.py --db ../data/anvil.db --json

指标：
- manifest 覆盖率（有 manifest 的 run / 总 run）；
- 跨任务污染：task-scoped 块里出现非当前 task_id 的来源数（目标 0）；
- L0–L5 各层字符数与块数；
- 被丢弃/截断块的原因分布；
- task workspace 目录数/体积；
- 角色 PRINCIPLES.md 数量/体积。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from anvil import config  # noqa: E402
from anvil.db import open_conn  # noqa: E402


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def workspace_stats(root: Path) -> dict:
    dirs = 0
    files = 0
    chars = 0
    if root.exists():
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            dirs += 1
            for p in entry.rglob("*"):
                if p.is_file():
                    files += 1
                    try:
                        chars += p.stat().st_size
                    except OSError:
                        pass
    return {"root": str(root), "dirs": dirs, "files": files, "bytes": chars}


def principles_stats(agents: Path) -> dict:
    roles = 0
    chars = 0
    with_file = 0
    if agents.exists():
        for home in agents.iterdir():
            if not home.is_dir():
                continue
            roles += 1
            p = home / "PRINCIPLES.md"
            if p.is_file():
                with_file += 1
                try:
                    chars += len(p.read_text(encoding="utf-8"))
                except OSError:
                    pass
    return {"roles": roles, "with_principles": with_file,
            "principles_chars": chars}


def evaluate(db_path: str) -> dict:
    conn = open_conn(db_path)
    try:
        if not _table_exists(conn, "context_manifests"):
            return {"error": "context_manifests table missing; start the server once"}
        runs_total = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        runs_with = conn.execute(
            "SELECT COUNT(DISTINCT run_id) AS n FROM context_manifests "
            "WHERE run_id IS NOT NULL").fetchone()["n"]
        rows = conn.execute("SELECT * FROM context_manifests").fetchall()

        layers: dict[str, dict] = defaultdict(lambda: {"blocks": 0, "chars": 0})
        reasons = Counter()
        kinds = Counter()
        cross_task = 0
        cross_task_examples: list[dict] = []
        injected = 0
        omitted = 0

        for row in rows:
            task_id = row["task_id"]
            blocks = json.loads(row["blocks_json"] or "[]")
            omitted_blocks = json.loads(row["omitted_json"] or "[]")
            for block in blocks:
                layer = str(block.get("layer") or "unknown")
                layers[layer]["blocks"] += 1
                layers[layer]["chars"] += int(block.get("chars") or 0)
                kinds[str(block.get("kind") or "unknown")] += 1
                if block.get("injected"):
                    injected += 1
                else:
                    omitted += 1
                    reasons[str(block.get("reason") or "unknown")] += 1
                if (task_id and block.get("scope") == "task"
                        and block.get("injected")):
                    ids = [str(x) for x in (block.get("source_ids") or [])]
                    if ids and task_id not in ids:
                        cross_task += 1
                        if len(cross_task_examples) < 10:
                            cross_task_examples.append({
                                "run_id": row["run_id"], "task_id": task_id,
                                "block": block.get("name"),
                                "source_ids": ids,
                            })
            for block in omitted_blocks:
                omitted += 1
                reasons[str(block.get("reason") or "unknown")] += 1

        return {
            "db": db_path,
            "manifest": {
                "rows": len(rows),
                "runs_total": runs_total,
                "runs_with_manifest": runs_with,
                "coverage": round(runs_with / runs_total, 4) if runs_total else None,
                "blocks_injected": injected,
                "blocks_omitted": omitted,
                "omitted_reasons": dict(reasons),
                "layers": dict(sorted(layers.items())),
                "top_kinds": dict(kinds.most_common(15)),
                "cross_task_blocks": cross_task,
                "cross_task_examples": cross_task_examples,
            },
            "workspace": workspace_stats(Path(config.TASK_WORKSPACES_DIR)),
            "role_principles": principles_stats(Path(config.AGENTS_DIR)),
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Anvil context/isolation eval harness")
    parser.add_argument("--db", default=str(config.SQLITE_PATH))
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    result = evaluate(args.db)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    m = result.get("manifest") or {}
    print(f"manifest coverage: {m.get('runs_with_manifest')}/{m.get('runs_total')} "
          f"({m.get('coverage')})")
    print(f"blocks: injected={m.get('blocks_injected')} omitted={m.get('blocks_omitted')}")
    print(f"cross-task blocks: {m.get('cross_task_blocks')}")
    print("layers:")
    for layer, stat in (m.get("layers") or {}).items():
        print(f"  {layer}: {stat['blocks']} blocks / {stat['chars']} chars")
    print(f"workspace: {result['workspace']}")
    print(f"role principles: {result['role_principles']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
