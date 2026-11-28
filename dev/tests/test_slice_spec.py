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
        {"kind": "command", "criterion": "y",
         "verification": {"cmd": "true", "exit_code": 1}},
    ]})
    assert slice_spec.test_checks(spec) == []


# --- command / file_assert / manual（v1.62 #5）------------------------------


def _command_item(**verification):
    raw = {"cmd": "python -m app --check", "exit_code": 1}
    raw.update(verification)
    return {"id": "c1", "kind": "command", "criterion": "cli reports failure",
            "verification": raw}


def _file_assert_item(**verification):
    raw = {"path": "reports/out.md", "contains": "PASS"}
    raw.update(verification)
    return {"id": "f1", "kind": "file_assert", "criterion": "report produced",
            "verification": raw}


def test_parse_command_and_file_assert_verification():
    spec = slice_spec.parse_slice_spec(
        {"acceptance": [_command_item(), _file_assert_item()]})
    command = spec["acceptance"][0]["verification"]
    assert command["cmd"] == "python -m app --check"
    assert command["exit_code"] == 1
    file_assert = spec["acceptance"][1]["verification"]
    assert file_assert["path"] == "reports/out.md"
    assert file_assert["contains"] == "PASS"


def test_parse_command_rejects_malformed_exit_code():
    for bad in (0, 256, -1, "1", True):
        with pytest.raises(slice_spec.SliceSpecError):
            slice_spec.parse_slice_spec({"acceptance": [
                _command_item(exit_code=bad)]})


def test_parse_command_rejects_cmd_and_argv_together():
    with pytest.raises(slice_spec.SliceSpecError):
        slice_spec.parse_slice_spec({"acceptance": [
            _command_item(cmd="run", argv=["run"], exit_code=1)]})


def test_parse_file_assert_rejects_absolute_or_traversing_path():
    for bad in ("/abs/report.md", "../escape.md"):
        with pytest.raises(slice_spec.SliceSpecError):
            slice_spec.parse_slice_spec({"acceptance": [
                _file_assert_item(path=bad)]})


def test_parse_manual_verification_records_judge_and_method():
    spec = slice_spec.parse_slice_spec({"acceptance": [{
        "id": "m1", "kind": "manual", "criterion": "API shape is sensible",
        "verification": {"judge": "human", "method": "read the diff"},
    }]})
    item = spec["acceptance"][0]
    assert item["judge"] == "human"
    assert item["method"] == "read the diff"


def test_parse_manual_rejects_unknown_verification_field():
    with pytest.raises(slice_spec.SliceSpecError):
        slice_spec.parse_slice_spec({"acceptance": [{
            "kind": "manual", "criterion": "x",
            "verification": {"judge": "human", "mystery": 1},
        }]})


def test_manual_items_record_judge_and_method_and_never_execute():
    spec = slice_spec.parse_slice_spec({"acceptance": [
        _command_item(),
        _file_assert_item(),
        {"id": "m1", "kind": "manual", "criterion": "API shape is sensible",
         "verification": {"judge": "human", "method": "read the diff"}},
        {"id": "m2", "kind": "manual", "criterion": "copy reads well",
         "evidence": "review notes in the task thread"},
    ]})
    manual = slice_spec.manual_items(spec)
    assert [m["id"] for m in manual] == ["m1", "m2"]
    assert manual[0]["judge"] == "human"
    assert manual[0]["method"] == "read the diff"
    # 未显式给判定者的 manual 项，默认由 decide 阶段判定；方法回落到 evidence。
    assert manual[1]["judge"] == "decide"
    assert manual[1]["method"] == "review notes in the task thread"
    kinds = [c["kind"] for c in slice_spec.engine_checks(spec)]
    assert kinds == ["command", "file_assert"]


def test_engine_checks_exposes_runnable_command_and_file_assert():
    spec = slice_spec.parse_slice_spec(
        {"acceptance": [_command_item(), _file_assert_item()]})
    checks = slice_spec.engine_checks(spec)
    command, file_assert = checks
    assert command["kind"] == "command"
    assert command["cmd"] == "python -m app --check"
    assert command["exit_code"] == 1
    assert file_assert["kind"] == "file_assert"
    assert file_assert["path"] == "reports/out.md"
    assert file_assert["contains"] == "PASS"


def test_engine_checks_skips_command_without_assertion_failure_exit_code():
    spec = slice_spec.parse_slice_spec({"acceptance": [
        {"kind": "command", "criterion": "cli reports failure",
         "verification": {"cmd": "python -m app --check"}},
    ]})
    assert slice_spec.engine_checks(spec) == []


def test_lock_violations_flag_command_without_exit_code():
    spec = slice_spec.parse_slice_spec({"acceptance": [
        {"kind": "command", "criterion": "cli reports failure",
         "verification": {"cmd": "python -m app --check"}},
        _command_item(),
        _file_assert_item(),
    ]})
    violations = slice_spec.lock_violations(spec)
    assert len(violations) == 1
    assert "exit_code" in violations[0]
    with pytest.raises(slice_spec.SliceSpecError):
        slice_spec.assert_lockable(spec)


def test_lock_violations_accepts_fully_declared_checks():
    spec = slice_spec.parse_slice_spec(
        {"acceptance": [_command_item(), _file_assert_item()]})
    assert slice_spec.lock_violations(spec) == []
    slice_spec.assert_lockable(spec)  # does not raise


# --- replan 弱化检测（v1.62 #10）-------------------------------------------


def _check_spec(*items) -> dict:
    return slice_spec.parse_slice_spec({"acceptance": list(items)})


def test_spec_hash_is_stable_and_canonical():
    first = _valid()
    second = _valid()
    second["out_of_scope"] = ["web UI"]  # 同值、不同对象
    assert slice_spec.spec_hash(first) == slice_spec.spec_hash(second)
    assert slice_spec.spec_hash(first).startswith("sha256:")
    changed = _valid()
    changed["seam"] = "other seam"
    assert slice_spec.spec_hash(first) != slice_spec.spec_hash(changed)


def test_weakening_detects_deleted_check():
    old = _check_spec(_command_item(), _file_assert_item())
    new = _check_spec(_file_assert_item())
    diff = slice_spec.weakening_diff(old, new)
    assert diff == [{"id": "c1", "type": "check_removed"}]


def test_weakening_detects_manual_downgrade():
    old = _check_spec(_command_item())
    downgraded = {"id": "c1", "kind": "manual", "criterion": "cli reports failure",
                  "verification": {"judge": "decide", "method": "read"}}
    diff = slice_spec.weakening_diff(old, _check_spec(downgraded))
    assert diff == [{"id": "c1", "type": "downgraded_to_manual"}]


def test_weakening_detects_assertion_change_without_red_evidence():
    old = _check_spec(_command_item())
    changed = _command_item(cmd="python -m app --check --lenient")
    diff = slice_spec.weakening_diff(old, _check_spec(changed))
    assert diff == [{"id": "c1",
                     "type": "assertion_changed_without_red_evidence"}]


def test_weakening_accepts_assertion_change_with_new_red_evidence():
    old = _check_spec(_command_item())
    changed = _command_item(cmd="python -m app --check --lenient")
    diff = slice_spec.weakening_diff(
        old, _check_spec(changed), red_evidence=["c1"])
    assert diff == []
    # 证据可以按检查 id 映射到具体检查，未覆盖的变更仍算弱化。
    diff = slice_spec.weakening_diff(
        old, _check_spec(changed), red_evidence={"other": ["job"]})
    assert diff and diff[0]["type"] == "assertion_changed_without_red_evidence"


def test_weakening_ignores_added_checks_and_non_assertion_fields():
    old = _check_spec(_command_item())
    new = _check_spec(_command_item(), _file_assert_item())
    new = {**new, "seam": "new seam", "out_of_scope": ["more"]}
    assert slice_spec.weakening_diff(old, new) == []
