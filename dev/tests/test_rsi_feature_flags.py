"""P0 RSI feature-flag tests (spec RSI-SEC-004).

RSI must ship dark: master/reflection/evaluation flags default off; runtime
proposal intake may be on, but its detector defaults off.
"""
from __future__ import annotations

from anvil import config, db
from anvil.services import features

RSI_KEYS = {
    "role_rsi",
    "role_rsi_autonomous_knowledge",
    "role_rsi_reflection",
    "role_rsi_evaluation",
    "role_rsi_auto_rollback",
    "role_rsi_rubric_judge",
    "role_rsi_lock_strategy",
    "role_rsi_replay_l2",
    "runtime_proposals",
    "runtime_proposal_detector",
}


def test_rsi_flags_are_registered(tmp_path):
    conn = db.connect(str(tmp_path / "flags.db"))
    try:
        flags = {f["key"]: f for f in features.list_flags(conn)}
        assert RSI_KEYS <= set(flags)
        off = {
            "role_rsi", "role_rsi_evaluation", "role_rsi_rubric_judge",
            "role_rsi_lock_strategy", "role_rsi_replay_l2",
            "runtime_proposal_detector",
        }
        on = {
            "role_rsi_autonomous_knowledge", "role_rsi_reflection",
            "role_rsi_auto_rollback", "runtime_proposals",
        }
        for key in off:
            assert flags[key]["value"] is False, key
        for key in on:
            assert flags[key]["value"] is True, key
    finally:
        conn.close()


def test_rsi_master_flag_can_be_toggled_and_reset(tmp_path):
    conn = db.connect(str(tmp_path / "toggle.db"))
    try:
        features.set_flag(conn, "role_rsi", True, actor="human")
        assert config.ROLE_RSI_ENABLED is True
        flags = {f["key"]: f for f in features.list_flags(conn)}
        assert flags["role_rsi"]["value"] is True
        assert flags["role_rsi"]["overridden"] is True
        features.reset_flag(conn, "role_rsi", actor="human")
        assert config.ROLE_RSI_ENABLED is False
    finally:
        conn.close()
