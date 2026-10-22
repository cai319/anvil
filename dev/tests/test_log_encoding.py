"""子进程输出编码：UTF-8 / GBK / 混合行 / BOM。"""
from __future__ import annotations

from anvil.util import decode_child_output


def test_decode_utf8_and_bom():
    assert decode_child_output("中文日志\n".encode("utf-8")) == "中文日志\n"
    assert decode_child_output("﻿中文".encode("utf-8")) == "中文"


def test_decode_gbk():
    data = "中文错误：权限不足\n".encode("gbk")
    assert decode_child_output(data) == "中文错误：权限不足\n"


def test_decode_mixed_utf8_and_gbk_lines():
    data = "UTF-8 行：正常\n".encode("utf-8") + "GBK 行：权限不足\n".encode("gbk")
    out = decode_child_output(data)
    assert "UTF-8 行：正常" in out
    assert "GBK 行：权限不足" in out


def test_get_run_log_decodes_gbk(tmp_path, monkeypatch):
    from anvil import config
    from anvil.db import connect
    from anvil.services import runs as runs_service

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    conn = connect(str(tmp_path / "l.db"))
    try:
        run = runs_service.create_run(conn, "qa-role", task_id=None)
        row = runs_service.get_run(conn, run["id"])
        with open(row["log_path"], "ab") as f:
            f.write("GBK 输出：无法连接到服务\n".encode("gbk"))
        out = runs_service.get_run_log(conn, run["id"])
        assert out["exists"] is True
        assert "GBK 输出：无法连接到服务" in out["content"]
    finally:
        conn.close()
