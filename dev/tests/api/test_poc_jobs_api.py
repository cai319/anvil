"""PoC 作业 API（M2a：human-only）。"""
from __future__ import annotations

import pytest

from anvil.services import sandbox
from tests.conftest import auth_headers, human_headers

SPEC = {
    "network": "none",
    "phases": {"run": [{"name": "poc", "cmd": "./poc"}]},
    "artifacts": [{"path": "out/**"}],
}


@pytest.mark.asyncio
async def test_human_create_list_cancel(client, monkeypatch):
    monkeypatch.setattr(sandbox, "strict_available", lambda: (True, "ready"))
    r = await client.post("/api/v1/poc-jobs", headers=human_headers(), json=SPEC)
    assert r.status_code == 201
    job = r.json()
    assert job["status"] == "queued"

    listing = await client.get(
        "/api/v1/poc-jobs", headers=human_headers())
    assert listing.status_code == 200
    assert listing.json()[0]["id"] == job["id"]

    detail = await client.get(
        f"/api/v1/poc-jobs/{job['id']}", headers=human_headers())
    assert detail.status_code == 200
    assert detail.json()["phases"] == []

    canceled = await client.post(
        f"/api/v1/poc-jobs/{job['id']}/cancel", headers=human_headers())
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "canceled"


@pytest.mark.asyncio
async def test_agent_cannot_create_or_read(client, monkeypatch):
    monkeypatch.setattr(sandbox, "strict_available", lambda: (True, "ready"))
    denied = await client.post(
        "/api/v1/poc-jobs", headers=auth_headers("pm"), json=SPEC)
    assert denied.status_code == 403
    listing = await client.get(
        "/api/v1/poc-jobs", headers=auth_headers("pm"))
    assert listing.status_code == 403


@pytest.mark.asyncio
async def test_strict_unavailable_returns_409(client, monkeypatch):
    monkeypatch.setattr(
        sandbox, "strict_available", lambda: (False, "docker/podman not found"))
    r = await client.post("/api/v1/poc-jobs", headers=human_headers(), json=SPEC)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "strict_sandbox_unavailable"


@pytest.mark.asyncio
async def test_invalid_spec_returns_400(client, monkeypatch):
    monkeypatch.setattr(sandbox, "strict_available", lambda: (True, "ready"))
    r = await client.post(
        "/api/v1/poc-jobs", headers=human_headers(),
        json={"network": "host", "phases": {"run": [{"cmd": "true"}]}})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_artifact_download_rejects_unknown_path(client, monkeypatch):
    monkeypatch.setattr(sandbox, "strict_available", lambda: (True, "ready"))
    job = (await client.post(
        "/api/v1/poc-jobs", headers=human_headers(), json=SPEC)).json()
    r = await client.get(
        f"/api/v1/poc-jobs/{job['id']}/artifacts/../../etc/passwd",
        headers=human_headers())
    assert r.status_code == 404
