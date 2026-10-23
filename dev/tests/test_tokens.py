"""Token 统计：input 含 cache、output 单列、按角色/日聚合。"""
from __future__ import annotations

from anvil.db import connect
from anvil.services import tokens
from anvil.util import now_iso


def test_token_summary(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    tokens.record(conn, "pm", "T1", "r1", {
        "input_tokens": 100, "output_tokens": 20,
        "cache_read_input_tokens": 30, "cache_creation_input_tokens": 10})
    tokens.record(conn, "developer", "T1", "r2", {
        "input_tokens": 50, "output_tokens": 5})
    s = tokens.summary(conn)
    assert s["input_tokens"] == 190   # 100+30+10+50
    assert s["output_tokens"] == 25
    assert s["total_tokens"] == 215
    assert any(r["role"] == "pm" and r["total_tokens"] == 160 for r in s["by_role"])
    assert s["today_total_tokens"] == 215
    conn.close()


def test_legacy_token_usage_reset_once(tmp_path):
    """旧 assistant 事件记账（output=0、input 重复）一次性清空，之后不再清。"""
    db = str(tmp_path / "legacy.db")
    conn = connect(db)
    tokens.record(conn, "pm", None, None, {
        "input_tokens": 100, "output_tokens": 0})
    conn.execute("DELETE FROM settings WHERE key='schema.token_usage_reset_v1'")
    conn.commit()
    conn.close()

    conn = connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0] == 0
        tokens.record(conn, "pm", None, None, {
            "input_tokens": 10, "output_tokens": 2})
    finally:
        conn.close()

    conn = connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0] == 1
    finally:
        conn.close()
