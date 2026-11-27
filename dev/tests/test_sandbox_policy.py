"""沙箱策略与工作区保护（M1）。"""
from __future__ import annotations

from anvil import config
from anvil.sandbox import policy
from anvil.services.tasks import _is_anvil_managed


def test_default_standard_policy_has_limits_and_digest():
    p = policy.resolve_standard_policy()
    assert p.tier == "standard"
    assert p.profile == "standard"
    assert p.network == "host"
    assert p.resources["timeout_seconds"] == int(config.SANDBOX_STANDARD_TIMEOUT)
    assert p.resources["rss_mib"] == int(config.SANDBOX_STANDARD_RSS_MIB)
    assert p.resources["pids"] == int(config.SANDBOX_STANDARD_PIDS)
    assert p.resources["disk_mib"] == int(config.SANDBOX_STANDARD_DISK_MIB)
    assert len(p.policy_digest) == 64


def test_policy_digest_is_stable_and_overrides_are_layered():
    a = policy.resolve_standard_policy()
    b = policy.resolve_standard_policy()
    assert a.policy_digest == b.policy_digest

    role = policy.resolve_standard_policy(
        role_sandbox={"resources": {"rss_mib": 2048}})
    assert role.resources["rss_mib"] == 2048
    assert role.source == "role"

    task = policy.resolve_standard_policy(
        role_sandbox={"resources": {"rss_mib": 2048}},
        task_sandbox={"resources": {"rss_mib": 1024, "pids": 64},
                      "read_allow": ["/tmp/external"]})
    assert task.resources["rss_mib"] == 1024
    assert task.resources["pids"] == 64
    assert task.resources["read_allow"] == ["/tmp/external"]
    assert task.source == "task"
    assert task.policy_digest != role.policy_digest


def test_resolve_for_run_uses_frozen_snapshot():
    frozen = {
        "tier": "standard", "profile": "standard", "network": "host",
        "resources": {"rss_mib": 1234}, "policy_digest": "f" * 64,
        "source": "task",
    }
    p = policy.resolve_for_run({"sandbox_policy": frozen}, task=None)
    assert p.policy_digest == "f" * 64
    assert p.resources["rss_mib"] == 1234
    assert p.source == "task"


def test_resolve_for_run_reads_role_projection_and_task_json():
    effective = {"projection": {"sandbox": {"resources": {"rss_mib": 2048}}}}
    task = {"sandbox_json": '{"resources": {"pids": 99}}'}
    p = policy.resolve_for_run(effective, task)
    assert p.resources["rss_mib"] == 2048
    assert p.resources["pids"] == 99
    assert p.source == "task"


def test_sandbox_home_is_excluded_from_changed_paths():
    assert _is_anvil_managed(".anvil/home/.claude/settings.json")
    assert _is_anvil_managed("./.anvil/home/x")
    assert not _is_anvil_managed(".anvil/workspace.json")
    assert _is_anvil_managed(".claude/settings.json")
