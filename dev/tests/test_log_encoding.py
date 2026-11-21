"""子进程输出编码：UTF-8 / GBK / 混合行 / BOM / 截断边界。"""
from __future__ import annotations

import json

from anvil.util import decode_child_output, decode_text_file, force_utf8_stdio, read_text_file


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


def test_decode_gbk_on_western_locale(monkeypatch):
    """西文 Windows 的 cp1252 locale 不能抢在 GB18030/GBK 之前成功。"""
    import anvil.util as util

    monkeypatch.setattr(util, "_fallback_encoding", lambda: "cp1252")
    data = "中文错误：权限不足\n".encode("gbk")
    assert decode_child_output(data) == "中文错误：权限不足\n"


def test_decode_western_text_not_misdetected(monkeypatch):
    """cp1252 的双高字节西文不能被 gb18030 误判成中文。"""
    import anvil.util as util

    monkeypatch.setattr(util, "_fallback_encoding", lambda: "cp1252")
    for text in ("caféx\n", "Grüße\n", "Grüße für\n", "Ölöl\n", "Schön\n", "déjà vu\n"):
        assert decode_child_output(text.encode("cp1252")) == text


def test_decode_utf16_and_utf32_bom_is_stripped():
    assert decode_child_output("中文\n".encode("utf-16")) == "中文\n"
    assert decode_child_output("中文\n".encode("utf-32")) == "中文\n"


def test_decode_text_file_utf16_without_bom():
    assert decode_text_file('{"a":"中文"}'.encode("utf-16-le")) == '{"a":"中文"}'
    assert decode_text_file('{"a":"中文"}'.encode("utf-16-be")) == '{"a":"中文"}'


def test_read_text_file_gbk_and_utf16(tmp_path):
    gbk = tmp_path / "a.md"
    gbk.write_bytes("中文内容\n".encode("gbk"))
    assert read_text_file(gbk) == "中文内容\n"
    u16 = tmp_path / "b.json"
    u16.write_bytes('{"a":"中文"}'.encode("utf-16"))
    assert read_text_file(u16) == '{"a":"中文"}'


def test_read_text_file_normalizes_newlines(tmp_path):
    """与 Path.read_text 的 universal-newline 语义一致，避免 Windows \r\r\n。"""
    crlf = tmp_path / "crlf.md"
    crlf.write_bytes("第一行\r\n第二行\r第三行\r\n".encode())
    assert read_text_file(crlf) == "第一行\n第二行\n第三行\n"


def test_cli_read_text_file_gbk_utf16_and_bom(tmp_path):
    from anvil_cli.main import _read_text_file

    gbk = tmp_path / "note.md"
    gbk.write_bytes("中文内容\n".encode("gbk"))
    assert _read_text_file(str(gbk)) == "中文内容\n"

    u16 = tmp_path / "payload.json"
    u16.write_bytes('{"a":"中文"}'.encode("utf-16"))
    assert json.loads(_read_text_file(str(u16))) == {"a": "中文"}

    bom = tmp_path / "bom.json"
    bom.write_bytes('{"a":"中文"}'.encode("utf-8-sig"))
    assert json.loads(_read_text_file(str(bom))) == {"a": "中文"}

    crlf = tmp_path / "crlf.md"
    crlf.write_bytes("第一行\r\n第二行\r\n".encode())
    assert _read_text_file(str(crlf)) == "第一行\n第二行\n"


def test_force_utf8_stdio_overrides_parent(monkeypatch):
    import anvil.util as util

    monkeypatch.setattr(util, "_is_windows", lambda: True)
    env = force_utf8_stdio({
        "PYTHONIOENCODING": "gbk",
        "pythonioencoding": "cp936",
        "PYTHONUTF8": "0",
        "PYTHONLEGACYWINDOWSSTDIO": "1",
    })
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert "pythonioencoding" not in env
    assert env["PYTHONUTF8"] == "1"
    assert "PYTHONLEGACYWINDOWSSTDIO" not in env


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


def test_get_run_log_tail_start_is_whole_line(tmp_path, monkeypatch):
    """截断点落在多字节字符中间时，首行必须是完整行而不是乱码。"""
    from anvil import config
    from anvil.db import connect
    from anvil.services import runs as runs_service

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    conn = connect(str(tmp_path / "l.db"))
    try:
        run = runs_service.create_run(conn, "qa-role", task_id=None)
        row = runs_service.get_run(conn, run["id"])

        head = "前一行\n".encode()
        char = "中".encode()
        tail = "尾部中文\n".encode()
        # 让 cut = len(data) - 200000 正好落在 char 的第二个字节上。
        filler = b"y" * (runs_service.RUN_LOG_TAIL_BYTES - 3 - len(tail))
        data = head + char + filler + b"\n" + tail
        assert (len(data) - runs_service.RUN_LOG_TAIL_BYTES) == len(head) + 1
        with open(row["log_path"], "wb") as f:
            f.write(data)

        out = runs_service.get_run_log(conn, run["id"])
        content = out["content"]
        assert content.startswith("... (truncated")
        assert "尾部中文" in content
        assert "€" not in content
        assert "�" not in content
        assert content.split("\n", 1)[1].startswith("尾部中文")
    finally:
        conn.close()


def test_read_log_tail_keeps_first_line_when_cut_at_line_start(tmp_path):
    from anvil.services.runs import _read_log_tail

    prev = "上一行中文\n".encode()
    goal = "目标行中文\n".encode()
    p = tmp_path / "run.log"
    p.write_bytes(prev + goal)
    data, truncated = _read_log_tail(p, limit=len(goal))
    assert truncated is True
    assert data == goal


def test_read_log_tail_single_long_line_not_empty(tmp_path):
    from anvil.services.runs import _read_log_tail
    from anvil.util import decode_text_file

    p = tmp_path / "run.log"
    p.write_bytes(("长" * 500).encode())
    data, truncated = _read_log_tail(p, limit=101)
    assert truncated is True
    assert data
    text = decode_text_file(data)
    assert text and "�" not in text


def test_get_run_log_drops_partial_trailing_char(tmp_path, monkeypatch):
    """日志正被追加、末尾是半个 UTF-8 字符时不应显示替换字符。"""
    from anvil import config
    from anvil.db import connect
    from anvil.services import runs as runs_service

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    conn = connect(str(tmp_path / "l.db"))
    try:
        run = runs_service.create_run(conn, "qa-role", task_id=None)
        row = runs_service.get_run(conn, run["id"])
        with open(row["log_path"], "ab") as f:
            f.write("尾部中文".encode("utf-8") + "中".encode("utf-8")[:2])
        out = runs_service.get_run_log(conn, run["id"])
        assert "尾部中文" in out["content"]
        assert "�" not in out["content"]
    finally:
        conn.close()
