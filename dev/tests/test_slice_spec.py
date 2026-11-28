"""切片 spec 校验：验收项、seam、out-of-scope、contract、交付物与 stub（v1.62 #4）。"""
from __future__ import annotations

import pytest

from anvil.services import slice_spec


def _valid() -> dict:
    return {
        "acceptance": [
            {
                "id": "a1",
                "kind": "test",
                "criterion": "stub raises NotImplementedError until implemented",
                "verification": {
                    "pytest": {
                        "paths": ["tests/test_feature.py"],
                        "args": ["-q"],
                        "plugins": ["pytest_timeout"],
                    },
                },
            },
            {
                "kind": "manual",
                "criterion": "human reviews the API shape",
                "evidence": "review notes in the task thread",
            },
        ],
        "seam": "AcceptanceCheck service boundary",
        "out_of_scope": ["web UI"],
        "contract": "public signatures do not change",
        "deliverables": ["src/feature.py", "tests/test_feature.py"],
        "stubs": [
            {"path": "src/feature.py", "signatures": ["def run(value: int) -> int"]},
        ],
    }


def test_parse_accepts_full_spec_and_normalizes_ids():
    spec = slice_spec.parse_slice_spec(_valid())
    assert [a["id"] for a in spec["acceptance"]] == ["a1", "a2"]
    assert spec["acceptance"][0]["kind"] == "test"
    assert spec["acceptance"][0]["verification"]["pytest"]["paths"] == [
        "tests/test_feature.py"]
    assert spec["seam"] == "AcceptanceCheck service boundary"
    assert spec["out_of_scope"] == ["web UI"]
    assert spec["contract"] == "public signatures do not change"
    assert spec["deliverables"] == ["src/feature.py", "tests/test_feature.py"]
    assert spec["stubs"][0]["path"] == "src/feature.py"


def test_parse_accepts_all_four_kinds():
    for kind in ("test", "command", "file_assert", "manual"):
        spec = slice_spec.parse_slice_spec({"acceptance": [{
            "kind": kind, "criterion": "x", "verification": "check it"}]})
        assert spec["acceptance"][0]["kind"] == kind


def test_parse_accepts_deliverable_and_stub_aliases():
    spec = slice_spec.parse_slice_spec({
        "acceptance": [],
        "deliverable_paths": ["src/a.py"],
        "stub_signatures": [{"path": "src/a.py", "signatures": ["def f()"]}],
    })
    assert spec["deliverables"] == ["src/a.py"]
    assert spec["stubs"][0]["path"] == "src/a.py"


def test_parse_rejects_unknown_top_level_field():
    raw = _valid()
    raw["nonsense"] = True
    with pytest.raises(slice_spec.SliceSpecError):
        slice_spec.parse_slice_spec(raw)


def test_parse_rejects_unknown_acceptance_field():
    raw = _valid()
    raw["acceptance"][0]["mystery"] = 1
    with pytest.raises(slice_spec.SliceSpecError):
        slice_spec.parse_slice_spec(raw)


def test_parse_rejects_unknown_stub_field():
    raw = _valid()
    raw["stubs"][0]["signature_hash"] = "abc"
    with pytest.raises(slice_spec.SliceSpecError):
        slice_spec.parse_slice_spec(raw)


def test_parse_rejects_illegal_kind():
    raw = {"acceptance": [{
        "kind": "integration", "criterion": "x", "verification": "y"}]}
    with pytest.raises(slice_spec.SliceSpecError):
        slice_spec.parse_slice_spec(raw)


def test_parse_requires_criterion_and_verification():
    with pytest.raises(slice_spec.SliceSpecError):
        slice_spec.parse_slice_spec({"acceptance": [{"kind": "test"}]})
    with pytest.raises(slice_spec.SliceSpecError):
        slice_spec.parse_slice_spec({"acceptance": [{
            "kind": "test", "criterion": "x"}]})


def test_parse_rejects_absolute_or_traversing_paths():
    with pytest.raises(slice_spec.SliceSpecError):
        slice_spec.parse_slice_spec(
            {"acceptance": [], "deliverables": ["/abs.py"]})
    with pytest.raises(slice_spec.SliceSpecError):
        slice_spec.parse_slice_spec(
            {"acceptance": [], "deliverables": ["../escape.py"]})
    with pytest.raises(slice_spec.SliceSpecError):
        slice_spec.parse_slice_spec(
            {"acceptance": [], "stubs": [{"path": "../escape.py",
                                          "signatures": []}]})


def test_test_checks_exposes_runnable_pytest_items_only():
    spec = slice_spec.parse_slice_spec(_valid())
    checks = slice_spec.test_checks(spec)
    assert len(checks) == 1
    check = checks[0]
    assert check["id"] == "a1"
    assert check["kind"] == "test"
    assert check["pytest"]["paths"] == ["tests/test_feature.py"]


def test_test_checks_skips_items_without_structured_pytest():
    spec = slice_spec.parse_slice_spec({"acceptance": [
        {"kind": "test", "criterion": "x", "verification": "manual prose"},
        {"kind": "command", "criterion": "y", "verification": {"cmd": "true"}},
    ]})
    assert slice_spec.test_checks(spec) == []
