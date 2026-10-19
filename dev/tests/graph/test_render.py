"""模板渲染 + managed-block 投影测试。"""
from __future__ import annotations

from anvil.graph.render import project_instruction, render_template


def test_render_template_substitutes_and_preserves_unknown():
    out = render_template("echo {{ task_id }} {{ missing }}", {"task_id": "T-1"})
    assert out == "echo T-1 {{ missing }}"


def test_project_instruction_creates_managed_block(tmp_path):
    target = tmp_path / "CLAUDE.md"
    project_instruction(target, "# 我是后端工程师", {"title": "加个接口", "description": "实现 /api/x"})
    text = target.read_text(encoding="utf-8")
    assert "ANVIL:BEGIN" in text
    assert "我是后端工程师" in text
    assert "加个接口" in text
    assert "anvil task submit" in text  # 协议注入


def test_project_instruction_preserves_user_content(tmp_path):
    target = tmp_path / "CLAUDE.md"
    target.write_text("用户手写内容\n<!-- ANVIL:BEGIN -->\n旧块\n<!-- ANVIL:END -->\n尾部\n", encoding="utf-8")
    project_instruction(target, "新指令", {"title": "t", "description": ""})
    text = target.read_text(encoding="utf-8")
    assert text.startswith("用户手写内容")
    assert text.rstrip().endswith("尾部")
    assert "新指令" in text
    assert "旧块" not in text
