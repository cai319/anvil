"""切片 spec 校验（v1.62 #4）。

scope 角色提交的结构化执行契约：验收项（kind/criterion/验证方式）、seam、
out-of-scope、contract、交付物路径与 stub 签名。校验只做结构与路径安全；
锁定集合 / 环境哈希 / 依赖完整性由后续票实现。
"""
from __future__ import annotations

import hashlib
import json
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
    "manual": frozenset({"judge", "method", "instructions"}),
}
PYTEST_FIELDS = frozenset({"paths", "args", "plugins"})
MANUAL_DEFAULT_JUDGE = "decide"
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


def _command_verification(raw, field_: str) -> dict:
    cmd = raw.get("cmd")
    argv = raw.get("argv")
    if bool(cmd) == bool(argv):
        raise SliceSpecError(f"{field_} requires exactly one of cmd/argv")
    out: dict = {}
    if cmd is not None:
        if not isinstance(cmd, str) or not cmd.strip():
            raise SliceSpecError(f"{field_}.cmd must be a non-empty string")
        out["cmd"] = cmd.strip()
    else:
        if (not isinstance(argv, list) or not argv
                or any(not isinstance(a, str) or not a.strip() for a in argv)):
            raise SliceSpecError(f"{field_}.argv must be a non-empty string list")
        out["argv"] = list(argv)
    exit_code = raw.get("exit_code")
    if exit_code is not None:
        if (isinstance(exit_code, bool) or not isinstance(exit_code, int)
                or not 1 <= exit_code <= 255):
            raise SliceSpecError(
                f"{field_}.exit_code must be an integer between 1 and 255")
        out["exit_code"] = exit_code
    return out


def _file_assert_verification(raw, field_: str) -> dict:
    path = _relative_path(raw.get("path"), f"{field_}.path")
    contains = raw.get("contains")
    if contains is not None and (not isinstance(contains, str)
                                 or not contains.strip()):
        raise SliceSpecError(f"{field_}.contains must be a non-empty string")
    return {"path": path,
            "contains": contains.strip() if isinstance(contains, str) else None}


def _manual_verification(raw, field_: str) -> dict:
    out: dict = {}
    for name in ("judge", "method", "instructions"):
        value = raw.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise SliceSpecError(f"{field_}.{name} must be a non-empty string")
    if raw.get("judge") is not None:
        out["judge"] = raw["judge"].strip()
    method = raw.get("method") or raw.get("instructions")
    if method is not None:
        out["method"] = method.strip()
    return out


def _verification(raw, kind: str, index: int) -> str | dict:
    field_ = f"acceptance[{index}].verification"
    if isinstance(raw, str):
        if not raw.strip():
            raise SliceSpecError(f"{field_} must not be empty")
        # 散文验收只有 manual 合法；test/command/file_assert 必须结构化，
        # 否则既不会进入引擎检查也不会进入 manual 判定，却被计入 N_total。
        if kind != "manual":
            raise SliceSpecError(
                f"{field_} must be an object for kind {kind}; "
                "prose is only valid for manual items")
        return raw.strip()
    if not isinstance(raw, dict):
        raise SliceSpecError(f"{field_} must be a string or an object")
    unknown = set(raw) - VERIFICATION_FIELDS[kind]
    if unknown:
        raise SliceSpecError(f"{field_} has unknown field(s) for kind {kind}: "
                             f"{sorted(unknown)}")
    if kind == "test":
        if not isinstance(raw.get("pytest"), dict):
            raise SliceSpecError(
                f"{field_}.pytest must be an object with paths/args/plugins")
        return {"pytest": _pytest_spec(raw["pytest"], field_)}
    if kind == "command":
        return _command_verification(raw, field_)
    if kind == "file_assert":
        return _file_assert_verification(raw, field_)
    return _manual_verification(raw, field_)


def _manual_record(item: dict, criterion: str, evidence) -> tuple[str, str]:
    """manual 项的判定者与判定方法；未显式声明时回落到 decide / evidence / 判据。"""
    verification = item.get("verification")
    judge = None
    method = None
    if isinstance(verification, dict):
        judge = verification.get("judge")
        method = verification.get("method")
    elif isinstance(verification, str):
        method = verification
    judge = judge or MANUAL_DEFAULT_JUDGE
    if not method:
        method = (evidence if isinstance(evidence, str) and evidence.strip()
                  else criterion)
    return judge, method


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
    if kind == "manual":
        # manual 不是引擎执行的检查：锁定判定者与判定方法，留给 decide 阶段。
        item["judge"], item["method"] = _manual_record(
            item, item["criterion"], item.get("evidence"))
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


def engine_checks(spec: dict) -> list[dict]:
    """返回可由引擎执行的检查（test / command / file_assert）。

    manual 项不在此列：它由 decide 阶段判定，不触发引擎执行。只有结构化且可
    机械化的验收项才会出现在这里。
    """
    checks: list[dict] = []
    for item in spec.get("acceptance") or []:
        kind = item.get("kind")
        verification = item.get("verification")
        if not isinstance(verification, dict):
            continue
        base = {"id": item["id"], "kind": kind,
                "criterion": item["criterion"]}
        if kind == "test":
            pytest_spec = verification.get("pytest")
            if not isinstance(pytest_spec, dict) or not pytest_spec.get("paths"):
                continue
            base["pytest"] = pytest_spec
        elif kind == "command":
            if verification.get("exit_code") is None:
                continue
            if verification.get("cmd") is not None:
                base["cmd"] = verification["cmd"]
            elif verification.get("argv"):
                base["argv"] = verification["argv"]
            else:
                continue
            base["exit_code"] = verification["exit_code"]
        elif kind == "file_assert":
            if not verification.get("path"):
                continue
            base["path"] = verification["path"]
            base["contains"] = verification.get("contains")
        else:
            continue
        checks.append(base)
    return checks


def test_checks(spec: dict) -> list[dict]:
    """返回可由引擎红跑的 test 类检查（含结构化 pytest 验证方式）。"""
    return [c for c in engine_checks(spec) if c["kind"] == "test"]


def manual_items(spec: dict) -> list[dict]:
    """返回 manual 验收项，附判定者与判定方法；这些项不由引擎执行。"""
    return [
        {"id": item["id"], "kind": "manual",
         "criterion": item["criterion"],
         "judge": item.get("judge") or MANUAL_DEFAULT_JUDGE,
         "method": item.get("method") or item["criterion"]}
        for item in spec.get("acceptance") or []
        if item.get("kind") == "manual"
    ]


def lock_violations(spec: dict) -> list[str]:
    """锁定前的可执行性校验：command 必须声明断言失败退出码。"""
    violations: list[str] = []
    for index, item in enumerate(spec.get("acceptance") or []):
        if item.get("kind") != "command":
            continue
        verification = item.get("verification")
        if not isinstance(verification, dict):
            violations.append(
                f"acceptance[{index}] command requires structured verification "
                "with cmd/argv and exit_code")
            continue
        if verification.get("exit_code") is None:
            violations.append(
                f"acceptance[{index}] command requires exit_code "
                "(the assertion-failure exit code)")
        elif verification.get("cmd") is None and not verification.get("argv"):
            violations.append(
                f"acceptance[{index}] command requires cmd or argv")
    return violations


def assert_lockable(spec: dict) -> None:
    """不可锁定的 spec 一律拒绝；消息指向第一处违规。"""
    violations = lock_violations(spec)
    if violations:
        raise SliceSpecError(violations[0])


# ---- replan 弱化检测（v1.62 #10）------------------------------------------

ENGINE_CHECK_KINDS = frozenset({"test", "command", "file_assert"})


def spec_hash(spec: dict) -> str:
    """切片 spec 的规范化哈希；replan 审计记录新旧哈希。"""
    canonical = json.dumps(spec or {}, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _acceptance_map(spec: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in (spec or {}).get("acceptance") or []:
        if isinstance(item, dict) and item.get("id"):
            out[str(item["id"])] = item
    return out


def _assertion(item: dict) -> str:
    """检查断言的稳定签名：kind + 判据 + 验证方式。"""
    return json.dumps({
        "kind": item.get("kind"),
        "criterion": item.get("criterion"),
        "verification": item.get("verification"),
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _red_covered(item_id: str, red_evidence) -> bool:
    """该检查是否已附新的引擎红证据。

    ``red_evidence`` 可以是检查 id 列表，或以检查 id 为键的映射。
    """
    if not red_evidence:
        return False
    if isinstance(red_evidence, dict):
        return item_id in red_evidence or item_id in {
            str(k) for k in red_evidence.keys()}
    if isinstance(red_evidence, str):
        return item_id == red_evidence
    return item_id in {str(x) for x in red_evidence}


def weakening_diff(old_spec: dict, new_spec: dict, *,
                   red_evidence=None) -> list[dict]:
    """返回 replan 的弱化项；空列表表示未弱化。

    弱化形态：
    - ``check_removed``：删除验收检查；
    - ``downgraded_to_manual``：引擎检查降级为 manual；
    - ``assertion_changed_without_red_evidence``：修改断言但没有新的红证据。

    新增检查、收紧断言、修改 seam / out-of-scope / contract 等不算弱化。
    """
    old_items = _acceptance_map(old_spec)
    new_items = _acceptance_map(new_spec)
    diff: list[dict] = []
    for item_id, old in old_items.items():
        new = new_items.get(item_id)
        if new is None:
            diff.append({"id": item_id, "type": "check_removed"})
            continue
        old_kind = str(old.get("kind") or "")
        new_kind = str(new.get("kind") or "")
        if old_kind in ENGINE_CHECK_KINDS and new_kind == "manual":
            diff.append({"id": item_id, "type": "downgraded_to_manual"})
            continue
        if (old_kind in ENGINE_CHECK_KINDS
                and new_kind in ENGINE_CHECK_KINDS
                and _assertion(old) != _assertion(new)
                and not _red_covered(item_id, red_evidence)):
            diff.append({"id": item_id,
                         "type": "assertion_changed_without_red_evidence"})
    return diff
