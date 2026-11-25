"""P2 hidden replay suite tests (spec RSI-EVAL-001/002/009)."""
from __future__ import annotations

import json

import pytest

from anvil import config
from anvil.services import eval_suite


def _item(i: int, split: str, family: str) -> dict:
    return {
        "schema_version": 1,
        "item_id": f"R-{split}-{i}",
        "suite_id": "s1",
        "split": split,
        "family": family,
        "source": {"task_id": f"T-{i}", "role_slug": "qa-role"},
        "prompt": {"task_title": f"任务 {i}", "step_instructions": "做测试"},
        "workspace": {"kind": "git_snapshot", "repo": "demo", "commit": "abc"},
        "verifier": {
            "kind": "deterministic",
            "checks": [{"id": "c1", "cmd": "true", "expect_exit": 0,
                        "weight": 1, "hard": True}],
            "forbidden_changes": ["tests/**"],
            "timeout_seconds": 60,
            "token_cap": 1000,
        },
        "rubric": None,
        "provenance": {"original_outcome": "done"},
    }


def _seed_suite(suite_id: str = "s1", n_in: int = 8, n_out: int = 8):
    eval_suite.create_suite(suite_id, name="默认 RSI 套件",
                            description="真实任务回放")
    for i in range(n_in):
        eval_suite.add_item(suite_id, _item(i, "held_in", f"repo:a;mech:{i}"))
    for i in range(n_out):
        eval_suite.add_item(suite_id, _item(100 + i, "held_out", f"repo:b;mech:{i}"))
    return eval_suite.freeze(suite_id)


def test_suite_roundtrip_and_minimum_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROLE_EVAL_DIR", tmp_path / "eval")
    digest = _seed_suite()
    assert digest.startswith("sha256:")
    loaded = eval_suite.load_suite("s1")
    assert loaded["counts"] == {"held_in": 8, "held_out": 8}
    assert loaded["ready"] is True
    assert loaded["digest"] == digest
    families_in = {x["family"] for x in loaded["items"] if x["split"] == "held_in"}
    families_out = {x["family"] for x in loaded["items"] if x["split"] == "held_out"}
    assert families_in.isdisjoint(families_out)
    status = eval_suite.status("s1")
    assert status["ready"] is True
    assert status["minimum"] == {"held_in": 8, "held_out": 8}


def test_suite_not_ready_below_minimum(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROLE_EVAL_DIR", tmp_path / "eval")
    _seed_suite("small", n_in=7, n_out=7)
    status = eval_suite.status("small")
    assert status["ready"] is False
    assert status["counts"] == {"held_in": 7, "held_out": 7}


def test_family_leak_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROLE_EVAL_DIR", tmp_path / "eval")
    eval_suite.create_suite("leak")
    eval_suite.add_item("leak", _item(1, "held_in", "repo:a;mech:1"))
    with pytest.raises(ValueError):
        eval_suite.add_item("leak", _item(2, "held_out", "repo:a;mech:1"))


def test_item_content_is_file_only_not_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROLE_EVAL_DIR", tmp_path / "eval")
    _seed_suite("files")
    root = config.ROLE_EVAL_DIR / "suites" / "files"
    assert (root / "suite.json").is_file()
    item_files = list((root / "items").glob("*.json"))
    assert len(item_files) == 16
    # The suite index contains hashes/counts, not prompt/verifier bodies.
    index = json.loads((root / "suite.json").read_text(encoding="utf-8"))
    text = json.dumps(index, ensure_ascii=False)
    assert "check" not in text
    assert "做测试" not in text
