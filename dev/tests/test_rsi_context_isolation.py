"""P1 context isolation foundation (spec RSI-OBS-006)."""
from __future__ import annotations

from anvil.services import daemon_common


def test_rsi_reflect_uses_dedicated_context_slot():
    assert daemon_common.context_key("T-1", "slice-a") == "T-1::slice::slice-a"
    assert daemon_common.context_key(None) == daemon_common.GLOBAL_TASK_KEY
    assert daemon_common.context_key(
        "T-1", "slice-a", origin="rsi_reflect") == daemon_common.RSI_REFLECT_TASK_KEY
    assert daemon_common.RSI_REFLECT_TASK_KEY not in (
        daemon_common.GLOBAL_TASK_KEY, "T-1")
