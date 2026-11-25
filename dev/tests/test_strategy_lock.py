"""role_rsi_lock_strategy routes agent strategy writes to human review."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from anvil import config, db
from anvil.api import memory as memory_api
from anvil.deps import Auth
from anvil.schemas import InstructionIn
from anvil.services import role_space, roles


@pytest.fixture(autouse=True)
def _lock(monkeypatch):
    monkeypatch.setattr(config, "ROLE_RSI_ENABLED", True)
    monkeypatch.setattr(config, "ROLE_RSI_LOCK_STRATEGY", True)


def _seed(conn):
    roles.create(conn, slug="qa", display_name="QA",
                 definition={"runtime": {"id": "claude-code"},
                             "rsi": {"enabled": True}})
    home = config.AGENTS_DIR / "qa"
    (home / "instruction.md").write_text(
        "<!-- ANVIL:CHARTER:BEGIN -->\n身份\n<!-- ANVIL:CHARTER:END -->\n"
        "<!-- ANVIL:STRATEGY:BEGIN -->\n策略 v1\n<!-- ANVIL:STRATEGY:END -->\n",
        encoding="utf-8")
    (home / "PRINCIPLES.md").write_text("旧准则\n", encoding="utf-8")
    role_space.ensure_baseline(conn, "qa")
    return home


def test_agent_instruction_write_is_rejected(tmp_path):
    conn = db.connect(str(tmp_path / "lock.db"))
    try:
        home = _seed(conn)
        auth = Auth(session="agent", role="qa")
        with pytest.raises(HTTPException) as exc:
            memory_api.write_instruction(
                InstructionIn(content=(
                    "<!-- ANVIL:CHARTER:BEGIN -->\n身份\n<!-- ANVIL:CHARTER:END -->\n"
                    "<!-- ANVIL:STRATEGY:BEGIN -->\n改写\n"
                    "<!-- ANVIL:STRATEGY:END -->\n")),
                conn=conn, auth=auth)
        assert exc.value.status_code == 422
        assert "策略 v1" in (home / "instruction.md").read_text(encoding="utf-8")
    finally:
        conn.close()


def test_agent_charter_write_is_rejected(tmp_path):
    conn = db.connect(str(tmp_path / "charter.db"))
    try:
        home = _seed(conn)
        with pytest.raises(HTTPException) as exc:
            memory_api.write_instruction(
                InstructionIn(content=(
                    "<!-- ANVIL:CHARTER:BEGIN -->\n新身份\n<!-- ANVIL:CHARTER:END -->\n"
                    "<!-- ANVIL:STRATEGY:BEGIN -->\n策略 v1\n"
                    "<!-- ANVIL:STRATEGY:END -->\n")),
                conn=conn, auth=Auth(session="agent", role="qa"))
        assert exc.value.status_code == 422
        assert "旧" not in (home / "instruction.md").read_text(encoding="utf-8")
    finally:
        conn.close()


def test_agent_principle_append_becomes_pending_candidate(tmp_path):
    conn = db.connect(str(tmp_path / "principle.db"))
    try:
        home = _seed(conn)
        result = memory_api.put_principles(
            {"content": "提交前必须跑验收", "mode": "append"},
            conn=conn, auth=Auth(session="agent", role="qa"))
        assert result["accepted"] is False
        assert result["status"] == "pending"
        row = conn.execute(
            "SELECT status, kind, tier FROM proposals WHERE id=?",
            (result["candidate_id"],)).fetchone()
        assert row["status"] == "pending"
        assert row["kind"] == "strategy"
        assert "提交前必须跑验收" not in (
            home / "PRINCIPLES.md").read_text(encoding="utf-8")
    finally:
        conn.close()


def test_agent_principle_replace_is_rejected(tmp_path):
    conn = db.connect(str(tmp_path / "replace.db"))
    try:
        _seed(conn)
        with pytest.raises(HTTPException) as exc:
            memory_api.put_principles(
                {"content": "整段替换", "mode": "replace"},
                conn=conn, auth=Auth(session="agent", role="qa"))
        assert exc.value.status_code == 422
    finally:
        conn.close()


def test_human_direct_writes_still_work(tmp_path):
    conn = db.connect(str(tmp_path / "human.db"))
    try:
        home = _seed(conn)
        # test_bypass models the human operator acting on a role; is_agent()
        # is false, so direct writes stay direct.
        human = Auth(session="human", role="qa", test_bypass=True)
        memory_api.put_principles(
            {"content": "人工准则", "mode": "replace"}, conn=conn, auth=human)
        assert "人工准则" in (home / "PRINCIPLES.md").read_text(encoding="utf-8")
        memory_api.write_instruction(
            InstructionIn(content=(
                "<!-- ANVIL:CHARTER:BEGIN -->\n身份\n<!-- ANVIL:CHARTER:END -->\n"
                "<!-- ANVIL:STRATEGY:BEGIN -->\n人工策略\n"
                "<!-- ANVIL:STRATEGY:END -->\n")),
            conn=conn, auth=human)
        assert "人工策略" in (home / "instruction.md").read_text(encoding="utf-8")
    finally:
        conn.close()
