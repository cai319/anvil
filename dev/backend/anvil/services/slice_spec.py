"""切片 spec 校验（v1.62 #4）。

scope 角色提交的结构化执行契约：验收项（kind/criterion/验证方式）、seam、
out-of-scope、contract、交付物路径与 stub 签名。校验只做结构与路径安全；
锁定集合 / 环境哈希 / 依赖完整性由后续票实现。
"""
from __future__ import annotations

import re

ACCEPTANCE_KINDS = ("test", "command", "file_assert", "manual")
SLICE_SPEC_FIELDS = frozenset({
    "acceptance", "seam", "out_of_scope", "contract", "deliverables", "stubs",
})
ACCEPTANCE_FIELDS = frozenset({
    "id", "kind", "criterion", "verification", "evidence",
})
STUB_FIELDS = frozenset({"path", "signature", "signatures"})
VERIFICATION_FIELDS = {
    "test": frozenset({"pytest"}),
    "command": frozenset({"cmd", "argv", "exit_code"}),
    "file_assert": frozenset({"path", "contains"}),
    "manual": frozenset({"instructions"}),
}
PYTEST_FIELDS = frozenset({"paths", "args", "plugins"})
_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class SliceSpecError(ValueError):
    """非法切片 spec；服务端以 409/400 拒绝提交。"""


def _relative_path(value, field_: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SliceSpecError(f"{field_} must be a non-empty string")
    path = value.strip().replace("\\", "/")
    if path.startswith("/") or ".." in path.split("/") or re.match(r"^[A-Za-z]:", path):
        raise SliceSpecError(f"{field_} must be relative without ..")
    return path


def _string_list(value, field_: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SliceSpecError(f"{field_} must be a list of strings")
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise SliceSpecError(f"{field_}[{i}] must be a non-empty string")
        out.append(item.strip())
    return out


def _pytest_spec(raw, field_: str) -> dict:
    if not isinstance(raw, dict):
        raise SliceSpecError(f"{field_}.pytest must be an object")
    unknown = set(raw) - PYTEST_FIELDS
    if unknown:
        raise SliceSpecError(f"{field_}.pytest has unknown field(s): {sorted(unknown)}")
    paths = _string_list(raw.get("paths"), f"{field_}.pytest.paths")
    if not paths:
        raise SliceSpecError(f"{field_}.pytest.paths must not be empty")
    paths = [_relative_path(p, f"{field_}.pytest.paths") for p in paths]
    args = _string_list(raw.get("args"), f"{field_}.pytest.args")
    plugins = _string_list(raw.get("plugins"), f"{field_}.pytest.plugins")
    return {"paths": paths, "args": args, "plugins": plugins}


def _verification(raw, kind: str, index: int) -> str | dict:
    field_ = f"acceptance[{index}].verification"
    if isinstance(raw, str):
        if not raw.strip():
            raise SliceSpecError(f"{field_} must not be empty")
        return raw.strip()
    if not isinstance(raw, dict):
        raise SliceSpecError(f"{field_} must be a string or an object")
    unknown = set(raw) - VERIFICATION_FIELDS[kind]
    if unknown:
        raise SliceSpecError(f"{field_} has unknown field(s) for kind {kind}: "
                             f"{sorted(unknown)}")
    if kind == "test" and isinstance(raw.get("pytest"), dict):
        return {"pytest": _pytest_spec(raw["pytest"], field_)}
    return dict(raw)


def _acceptance_item(raw, index: int) -> dict:
    if not isinstance(raw, dict):
        raise SliceSpecError(f"acceptance[{index}] must be an object")
    unknown = set(raw) - ACCEPTANCE_FIELDS
    if unknown:
        raise SliceSpecError(
            f"acceptance[{index}] has unknown field(s): {sorted(unknown)}")
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in ACCEPTANCE_KINDS:
        raise SliceSpecError(
            f"acceptance[{index}].kind must be one of {ACCEPTANCE_KINDS}")
    criterion = raw.get("criterion")
    if not isinstance(criterion, str) or not criterion.strip():
        raise SliceSpecError(f"acceptance[{index}].criterion must be a non-empty string")
    verification = raw.get("verification")
    evidence = raw.get("evidence")
    if verification is None and not (isinstance(evidence, str) and evidence.strip()):
        raise SliceSpecError(
            f"acceptance[{index}] requires verification or evidence")
    item_id = raw.get("id")
    if item_id is None:
        item_id = f"a{index + 1}"
    elif not isinstance(item_id, str) or not _ITEM_ID_RE.match(item_id.strip()):
        raise SliceSpecError(
            f"acceptance[{index}].id must match {_ITEM_ID_RE.pattern}")
    item = {"id": item_id.strip(), "kind": kind, "criterion": criterion.strip()}
    if verification is not None:
        item["verification"] = _verification(verification, kind, index)
    if isinstance(evidence, str) and evidence.strip():
        item["evidence"] = evidence.strip()
    return item


def _stub(raw, index: int) -> dict:
    field_ = f"stubs[{index}]"
    if not isinstance(raw, dict):
        raise SliceSpecError(f"{field_} must be an object")
    unknown = set(raw) - STUB_FIELDS
    if unknown:
        raise SliceSpecError(f"{field_} has unknown field(s): {sorted(unknown)}")
    path = _relative_path(raw.get("path"), f"{field_}.path")
    signatures = _string_list(raw.get("signatures"), f"{field_}.signatures")
    signature = raw.get("signature")
    if signature is not None:
        if not isinstance(signature, str) or not signature.strip():
            raise SliceSpecError(f"{field_}.signature must be a non-empty string")
        signatures.append(signature.strip())
    return {"path": path, "signatures": signatures}


def parse_slice_spec(raw) -> dict:
    """校验并归一化切片 spec；未知字段与非法 kind 一律拒绝。"""
    if not isinstance(raw, dict):
        raise SliceSpecError("slice spec must be an object")
    raw = dict(raw)
    for alias, canonical in (("deliverable_paths", "deliverables"),
                             ("stub_signatures", "stubs")):
        if alias in raw:
            if canonical in raw:
                raise SliceSpecError(
                    f"provide {canonical} or {alias}, not both")
            raw[canonical] = raw.pop(alias)
    unknown = set(raw) - SLICE_SPEC_FIELDS
    if unknown:
        raise SliceSpecError(f"unknown slice spec field(s): {sorted(unknown)}")
    acceptance_raw = raw.get("acceptance") or []
    if not isinstance(acceptance_raw, list):
        raise SliceSpecError("acceptance must be a list")
    acceptance = [_acceptance_item(item, i)
                  for i, item in enumerate(acceptance_raw)]
    seam = raw.get("seam")
    if seam is not None and (not isinstance(seam, str) or not seam.strip()):
        raise SliceSpecError("seam must be a non-empty string")
    contract = raw.get("contract")
    if contract is not None and (not isinstance(contract, str) or not contract.strip()):
        raise SliceSpecError("contract must be a non-empty string")
    deliverables_raw = raw.get("deliverables") or []
    if not isinstance(deliverables_raw, list):
        raise SliceSpecError("deliverables must be a list of paths")
    deliverables = [_relative_path(p, f"deliverables[{i}]")
                    for i, p in enumerate(deliverables_raw)]
    stubs_raw = raw.get("stubs") or []
    if not isinstance(stubs_raw, list):
        raise SliceSpecError("stubs must be a list")
    stubs = [_stub(item, i) for i, item in enumerate(stubs_raw)]
    return {
        "acceptance": acceptance,
        "seam": seam.strip() if isinstance(seam, str) else None,
        "out_of_scope": _string_list(raw.get("out_of_scope"), "out_of_scope"),
        "contract": contract.strip() if isinstance(contract, str) else None,
        "deliverables": deliverables,
        "stubs": stubs,
    }


def test_checks(spec: dict) -> list[dict]:
    """返回可由引擎红跑的 test 类检查（含结构化 pytest 验证方式）。"""
    checks: list[dict] = []
    for item in spec.get("acceptance") or []:
        if item.get("kind") != "test":
            continue
        verification = item.get("verification")
        if not isinstance(verification, dict):
            continue
        pytest_spec = verification.get("pytest")
        if not isinstance(pytest_spec, dict) or not pytest_spec.get("paths"):
            continue
        checks.append({"id": item["id"], "kind": "test",
                       "criterion": item["criterion"], "pytest": pytest_spec})
    return checks
