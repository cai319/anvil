"""P2 shadow-home isolation tests (spec RSI-EVAL-003/009)."""
from __future__ import annotations

import os

from anvil import config
from anvil.services import eval_runner


def test_shadow_home_is_isolated_and_real_home_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    home = config.AGENTS_DIR / "qa-role"
    home.mkdir(parents=True)
    (home / "instruction.md").write_text("真实内容", encoding="utf-8")
    before = eval_runner._tree_hash(home)
    with eval_runner.shadow_role_home("qa-role") as (shadow, env):
        assert (shadow / "instruction.md").read_text(encoding="utf-8") == "真实内容"
        (shadow / "instruction.md").write_text("影子内容", encoding="utf-8")
        (shadow / "new.md").write_text("x", encoding="utf-8")
        assert env.get("ANVIL_EVAL_MODE") == "1"
        assert "ANVIL_ENDPOINT" not in env
        assert "ANVIL_TOKEN" not in env
        assert "ANVIL_EVAL_KEY" not in env
    assert (home / "instruction.md").read_text(encoding="utf-8") == "真实内容"
    assert not (home / "new.md").exists()
    assert eval_runner._tree_hash(home) == before
    assert not os.path.exists(shadow)
