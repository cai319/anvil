"""角色思考强度（runtime effort）：默认值、有效配置、Claude Code 参数注入。"""
from __future__ import annotations

from anvil.db import connect
from anvil.graph import nodes
from anvil.services import roles, runtime as runtime_service


def _conn(tmp_path):
    return connect(str(tmp_path / "effort.db"))


def test_cindy_defaults_to_max(tmp_path):
    conn = _conn(tmp_path)
    roles.ensure_onboarding(conn)
    role = roles.get(conn, "cindy")
    assert role["definition"]["runtime"]["effort"] == "max"
    effective = roles.effective_config(conn, "cindy")
    assert effective["projection"]["runtime"]["effort"] == "max"


def test_other_role_defaults_to_high(tmp_path):
    conn = _conn(tmp_path)
    roles.create(
        conn, slug="dev", display_name="Dev",
        definition={"runtime": {"id": "claude-code"}})
    role = roles.get(conn, "dev")
    assert role["definition"]["runtime"]["effort"] == "high"
    effective = roles.effective_config(conn, "dev")
    assert effective["projection"]["runtime"]["effort"] == "high"


def test_claude_runtime_passes_effort_flag():
    definitions = runtime_service.load_definitions()
    cc = definitions["claude-code"]
    assert "--effort" in cc["invocation"]["args"]
    assert "{{ effort }}" in cc["invocation"]["args"]
    assert "--effort" in cc["session"]["args_new"]
    assert "--effort" in cc["session"]["args_resume"]


def test_persistent_ctx_carries_effective_effort(tmp_path):
    conn = _conn(tmp_path)
    roles.ensure_onboarding(conn)
    roles.create(
        conn, slug="dev", display_name="Dev",
        definition={"runtime": {"id": "claude-code"}})
    for slug, expected in (("cindy", "max"), ("dev", "high")):
        role = roles.get(conn, slug)
        effective = roles.effective_config(conn, slug)
        result = nodes._render_persistent(
            conn, {"role_slug": slug}, {"id": "T1"},
            effective, role, str(tmp_path / "ws"))
        ctx = result[6]
        assert ctx["effort"] == expected
        assert "--effort" in result[1]
