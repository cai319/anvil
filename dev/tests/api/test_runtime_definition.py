"""自定义运行时 YAML 定义：新增/覆盖、探测、权限。"""
from __future__ import annotations

import sys

import pytest
import yaml

pytest.importorskip("anvil.main")
from anvil import config  # noqa: E402
from tests.conftest import API, auth_headers, human_headers  # noqa: E402


def _yaml_for(rid: str = "codeagent", executable: str = sys.executable) -> str:
    return yaml.safe_dump({
        "id": rid,
        "display_name": "CodeAgent",
        "executable": executable,
        "version_range": ">=3.0.0",
        "version_probe": {
            "args": ["--version"],
            "parse": r"regex:(\d+\.\d+\.\d+)",
        },
        "invocation": {
            "args": ["-c", "print('{{ prompt }}')"],
            "cwd": "{{ workspace_path }}",
            "timeout_seconds": 30,
        },
        "capabilities": {"managed_mcp": False, "managed_skills": False},
        "projection": {
            "instruction": {"target": "{{ workspace_path }}/AGENTS.md"},
        },
        "health": {
            "probe_prompt": "Reply with exactly: ANVIL_OK",
            "expect_contains": "ANVIL_OK",
            "args": ["-c", "print('ANVIL_OK')"],
        },
    }, allow_unicode=True, sort_keys=False)


def _isolate_user_runtimes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USER_RUNTIMES_DIR", tmp_path / "runtimes")


async def test_put_and_get_runtime_definition(client, tmp_path, monkeypatch):
    _isolate_user_runtimes(tmp_path, monkeypatch)
    raw = _yaml_for()

    r = await client.put(f"{API}/runtimes/codeagent/definition",
                         headers=human_headers(), json={"yaml": raw})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "codeagent"
    assert body["status"] == "ready", body.get("last_probe_log")
    assert body["detected_version"]

    listed = (await client.get(f"{API}/runtimes")).json()
    assert any(x["id"] == "codeagent" for x in listed)

    got = await client.get(f"{API}/runtimes/codeagent/definition")
    assert got.status_code == 200, got.text
    assert got.json()["yaml"] == raw
    assert got.json()["builtin"] is False

    # 新运行时可以设为默认
    r = await client.put(f"{API}/runtimes/default", headers=human_headers(),
                         json={"id": "codeagent"})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "codeagent"


async def test_definition_id_must_match_path(client, tmp_path, monkeypatch):
    _isolate_user_runtimes(tmp_path, monkeypatch)
    r = await client.put(f"{API}/runtimes/other/definition",
                         headers=human_headers(),
                         json={"yaml": _yaml_for("codeagent")})
    assert r.status_code == 422
    assert "id" in r.json()["detail"]


async def test_definition_put_requires_human(client, tmp_path, monkeypatch):
    _isolate_user_runtimes(tmp_path, monkeypatch)
    r = await client.put(f"{API}/runtimes/codeagent/definition",
                         headers=auth_headers(), json={"yaml": _yaml_for()})
    assert r.status_code == 403


async def test_definition_rejects_invalid_yaml(client, tmp_path, monkeypatch):
    _isolate_user_runtimes(tmp_path, monkeypatch)
    r = await client.put(f"{API}/runtimes/codeagent/definition",
                         headers=human_headers(), json={"yaml": "id: [unclosed"})
    assert r.status_code == 422
