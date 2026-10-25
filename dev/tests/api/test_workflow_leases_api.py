"""Path reservation API (leases)."""
from __future__ import annotations

import pytest

pytest.importorskip("anvil.main")
from tests.conftest import API, auth_headers, human_headers, task_payload


@pytest.mark.asyncio
async def test_path_reservations_api(client):
    await client.post(f"{API}/tasks", json=task_payload(
        id="T-LEASE", assignees=["developer", "tester"]), headers=auth_headers())
    first = await client.post(f"{API}/tasks/T-LEASE/reservations",
                              headers=auth_headers("developer"),
                              json={"paths": ["src/app.ts"]})
    assert first.status_code == 200, first.text
    assert any(p["resource_key"].endswith(":src/app.ts") for p in first.json()["paths"])

    conflict = await client.post(f"{API}/tasks/T-LEASE/reservations",
                                 headers=auth_headers("tester"),
                                 json={"paths": ["src/app.ts"]})
    assert conflict.status_code == 409
    assert "path_conflict" in conflict.json()["detail"]

    listed = await client.get(f"{API}/tasks/T-LEASE/leases", headers=human_headers())
    assert listed.status_code == 200, listed.text
    assert listed.json()["paths"]

    released = await client.request(
        "DELETE", f"{API}/tasks/T-LEASE/reservations", headers=auth_headers("developer"),
        json={"paths": ["src/app.ts"]})
    assert released.status_code == 200, released.text
    assert released.json()["released"] == 1
    after = await client.get(f"{API}/tasks/T-LEASE/leases", headers=human_headers())
    assert not [p for p in after.json()["paths"] if p["status"] == "active"]
