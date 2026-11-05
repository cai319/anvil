"""工具页功能开关：默认值、人工修改、恢复默认、持久化。"""
from __future__ import annotations

from anvil import config
from anvil.db import connect
from anvil.services import features
from tests.conftest import API, auth_headers, human_headers


async def test_feature_flags_default_toggle_and_reset(client):
    data = (await client.get(f"{API}/features", headers=auth_headers())).json()
    flags = {item["key"]: item for item in data["flags"]}
    capture = flags["experience_capture"]
    assert capture["value"] is True
    assert capture["default"] is True
    assert capture["overridden"] is False

    try:
        changed = await client.put(
            f"{API}/features/experience_capture",
            json={"value": False}, headers=human_headers())
        assert changed.status_code == 200
        body = changed.json()
        assert body["value"] is False
        assert body["overridden"] is True

        listed = (await client.get(
            f"{API}/features", headers=human_headers())).json()["flags"]
        assert next(item for item in listed
                    if item["key"] == "experience_capture")["value"] is False
    finally:
        reset = await client.delete(
            f"{API}/features/experience_capture", headers=human_headers())
        assert reset.status_code == 200
        assert reset.json()["value"] is True
        assert reset.json()["overridden"] is False

    missing = await client.put(
        f"{API}/features/not-a-flag", json={"value": True},
        headers=human_headers())
    assert missing.status_code == 404


def test_flag_override_persists_across_apply(tmp_path, monkeypatch):
    conn = connect(str(tmp_path / "features.db"))
    try:
        monkeypatch.setattr(config, "EXPERIENCE_CAPTURE_ENABLED", True)
        features.set_flag(conn, "experience_capture", False, actor="human")
        # 模拟重启：config 先回到 env 默认，再从 settings 应用用户覆盖。
        monkeypatch.setattr(config, "EXPERIENCE_CAPTURE_ENABLED", True)
        features.apply_overrides(conn)
        assert config.EXPERIENCE_CAPTURE_ENABLED is False

        features.reset_flag(conn, "experience_capture", actor="human")
        assert config.EXPERIENCE_CAPTURE_ENABLED is True
        assert next(
            item for item in features.list_flags(conn)
            if item["key"] == "experience_capture"
        )["overridden"] is False
    finally:
        conn.close()
