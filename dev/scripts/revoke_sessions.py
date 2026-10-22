"""撤销 Anvil 的全部会话/角色令牌（安全事件响应）。

用法：
    python dev/scripts/revoke_sessions.py --db data/anvil.db --yes

说明：
- 删除 sessions 表中的运行级/人类令牌；
- 清空 role_sessions 的 session_id / token，并将角色标为 dead；
- 不删除角色、任务、记忆或数据库。
执行后，运行中的 Agent 需要重新唤醒/重启才能获得新令牌。
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/anvil.db", help="SQLite 数据库路径")
    ap.add_argument("--yes", action="store_true", help="确认执行")
    args = ap.parse_args()
    db = Path(args.db)
    if not db.exists():
        print(f"database not found: {db}")
        return 2
    if not args.yes:
        print("refusing to revoke without --yes")
        return 2
    conn = sqlite3.connect(db)
    try:
        sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        roles = conn.execute(
            "SELECT COUNT(*) FROM role_sessions WHERE token IS NOT NULL OR session_id<>''"
        ).fetchone()[0]
        conn.execute("DELETE FROM sessions")
        conn.execute(
            "UPDATE role_sessions SET session_id='', token=NULL, status='dead', pid=NULL"
        )
        conn.commit()
    finally:
        conn.close()
    print(f"revoked {sessions} session token(s), cleared {roles} role session(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
