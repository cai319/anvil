"""可复用工作流模板：角色槽位、版本、导入、校验与套用。

模板是人工维护的 definition 草案；发布后的版本不可变。模板不会直接被运行时
解析：应用时先编译成现有 workflow definition，再走 `validate_definition` /
`create_workflow`，因此不复制 workflow 引擎的 DAG/loop 规则。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from copy import deepcopy

from ..util import ConflictError, jdumps, jloads, new_id, now_iso
from . import roles as roles_service
from . import tasks as tasks_service
from . import workflow as workflow_service

MAX_STEPS = 100
MAX_SLOTS = 30
MAX_DEFINITION_BYTES = 256 * 1024
MAX_LAYOUT_BYTES = 64 * 1024
MAX_TEXT = 8000
SLOT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
TOKEN_RE = re.compile(r"\{\{slot:([A-Za-z0-9_-]+)\}\}")
MENTION_RE = re.compile(r"@([A-Za-z0-9_-]+)")
STEP_KINDS = {"task", "decision", "plan_slices"}
LOOP_PHASES = ("scope", "execute", "verify", "fix", "verify2", "decide")
STEP_FIELDS = {
    "id", "title", "kind", "role_slot", "reviewer_slot", "depends_on",
    "instructions", "acceptance", "gate", "contract", "on_reject", "max_rework",
}


def _issue(severity: str, code: str, message: str, *, node_id: str | None = None,
           slot_key: str | None = None) -> dict:
    return {
        "severity": severity, "code": code, "message": message,
        "node_id": node_id, "slot_key": slot_key,
    }


def _definition_hash(definition: dict) -> str:
    return hashlib.sha256(jdumps(definition).encode("utf-8")).hexdigest()


def _slots(definition: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for raw in definition.get("role_slots") or []:
        if isinstance(raw, dict) and str(raw.get("key") or ""):
            out[str(raw["key"])] = raw
    return out


def _steps(definition: dict) -> list[dict]:
    return [s for s in (definition.get("steps") or []) if isinstance(s, dict)]


def used_slot_keys(definition: dict) -> set[str]:
    used: set[str] = set()
    for step in _steps(definition):
        if step.get("role_slot"):
            used.add(str(step["role_slot"]))
        if step.get("reviewer_slot"):
            used.add(str(step["reviewer_slot"]))
    loop = definition.get("loop")
    if isinstance(loop, dict):
        for phase in LOOP_PHASES:
            spec = loop.get(phase)
            if isinstance(spec, dict) and spec.get("role_slot"):
                used.add(str(spec["role_slot"]))
    for raw in definition.get("verification") or []:
        if isinstance(raw, dict) and raw.get("verifier_slot"):
            used.add(str(raw["verifier_slot"]))
    return used


def _text_slots(value) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        found.update(TOKEN_RE.findall(value))
    elif isinstance(value, list):
        for item in value:
            found.update(_text_slots(item))
    elif isinstance(value, dict):
        for item in value.values():
            found.update(_text_slots(item))
    return found


def _shape_issues(definition: dict, layout: dict | None = None) -> list[dict]:
    issues: list[dict] = []
    if not isinstance(definition, dict):
        return [_issue("error", "invalid_definition", "模板 definition 必须是对象")]
    slots_list = definition.get("role_slots")
    if not isinstance(slots_list, list):
        issues.append(_issue("error", "role_slots", "role_slots 必须是数组"))
        slots_list = []
    if len(slots_list) > MAX_SLOTS:
        issues.append(_issue("error", "too_many_slots", f"角色槽位最多 {MAX_SLOTS} 个"))
    seen_slots: set[str] = set()
    for raw in slots_list:
        if not isinstance(raw, dict):
            issues.append(_issue("error", "invalid_slot", "角色槽位必须是对象"))
            continue
        key = str(raw.get("key") or "")
        if not SLOT_KEY_RE.match(key):
            issues.append(_issue("error", "invalid_slot_key", f"非法角色槽位 key: {key!r}",
                                 slot_key=key or None))
        elif key in seen_slots:
            issues.append(_issue("error", "duplicate_slot", f"重复角色槽位: {key}", slot_key=key))
        seen_slots.add(key)
        if not str(raw.get("label") or "").strip():
            issues.append(_issue("error", "slot_label", f"角色槽位 {key} 缺少名称",
                                 slot_key=key))
        suggested = raw.get("suggested_roles")
        if suggested is not None and not isinstance(suggested, list):
            issues.append(_issue("error", "slot_suggested", f"角色槽位 {key} 的 suggested_roles 必须是数组",
                                 slot_key=key))
    steps = definition.get("steps")
    if not isinstance(steps, list) or not steps:
        issues.append(_issue("error", "no_steps", "模板至少需要一个步骤"))
        steps = []
    if len(steps) > MAX_STEPS:
        issues.append(_issue("error", "too_many_steps", f"模板步骤最多 {MAX_STEPS} 个"))
    seen_ids: set[str] = set()
    for raw in steps:
        if not isinstance(raw, dict):
            issues.append(_issue("error", "invalid_step", "步骤必须是对象"))
            continue
        sid = str(raw.get("id") or "")
        if not sid:
            issues.append(_issue("error", "missing_step_id", "步骤缺少 id"))
            continue
        if sid in seen_ids:
            issues.append(_issue("error", "duplicate_step", f"重复步骤 id: {sid}", node_id=sid))
        seen_ids.add(sid)
        if str(raw.get("kind") or "task") not in STEP_KINDS:
            issues.append(_issue("error", "invalid_kind", f"步骤 {sid} 的 kind 非法", node_id=sid))
        slot = str(raw.get("role_slot") or "")
        if not slot:
            issues.append(_issue("error", "missing_role_slot", f"步骤 {sid} 缺少 role_slot", node_id=sid))
        elif slot not in seen_slots:
            issues.append(_issue("error", "unknown_role_slot",
                                 f"步骤 {sid} 引用未知槽位 {slot}", node_id=sid, slot_key=slot))
        reviewer = str(raw.get("reviewer_slot") or "")
        if reviewer and reviewer not in seen_slots:
            issues.append(_issue("error", "unknown_reviewer_slot",
                                 f"步骤 {sid} 引用未知 reviewer 槽位 {reviewer}",
                                 node_id=sid, slot_key=reviewer))
        if raw.get("gate") and not reviewer:
            issues.append(_issue("warning", "gate_without_reviewer",
                                 f"审核门 {sid} 未设置 reviewer 槽位", node_id=sid))
        if reviewer and reviewer == slot:
            issues.append(_issue("error", "reviewer_role_conflict",
                                 f"步骤 {sid} 的审核人与执行人不能是同一槽位",
                                 node_id=sid, slot_key=reviewer))
        title = str(raw.get("title") or "")
        if len(title) > 56:
            issues.append(_issue("warning", "long_title",
                                 f"步骤 {sid} 标题超过 56 字，图谱会截断", node_id=sid))
    loop = definition.get("loop")
    if loop is not None:
        if not isinstance(loop, dict):
            issues.append(_issue("error", "invalid_loop", "loop 必须是对象"))
        else:
            for phase in LOOP_PHASES:
                spec = loop.get(phase)
                if spec is None:
                    continue
                if not isinstance(spec, dict):
                    issues.append(_issue("error", "invalid_loop_phase", f"loop.{phase} 必须是对象"))
                    continue
                slot = str(spec.get("role_slot") or "")
                if not slot:
                    issues.append(_issue("error", "loop_role_slot",
                                         f"loop.{phase} 缺少 role_slot", slot_key=phase))
                elif slot not in seen_slots:
                    issues.append(_issue("error", "unknown_loop_slot",
                                         f"loop.{phase} 引用未知槽位 {slot}",
                                         slot_key=slot))
    for raw in definition.get("verification") or []:
        if isinstance(raw, dict) and raw.get("verifier_slot"):
            slot = str(raw["verifier_slot"])
            if slot not in seen_slots:
                issues.append(_issue("error", "unknown_verifier_slot",
                                     f"验证矩阵引用未知槽位 {slot}", slot_key=slot))
    # 静态可达性：没有根节点 / 从根不可达的步骤通常是 AI 生成或手工编辑遗漏。
    step_deps = {
        str(raw.get("id")): [str(dep) for dep in (raw.get("depends_on") or [])]
        for raw in steps if isinstance(raw, dict) and raw.get("id")
    }
    roots = [sid for sid, deps in step_deps.items() if not deps]
    if step_deps and not roots:
        issues.append(_issue("error", "no_entry",
                             "模板没有无依赖的入口步骤（可能存在环）"))
    reachable: set[str] = set()
    stack = list(roots)
    while stack:
        current = stack.pop()
        if current in reachable:
            continue
        reachable.add(current)
        for sid, deps in step_deps.items():
            if current in deps and sid not in reachable:
                stack.append(sid)
    for sid in step_deps:
        if roots and sid not in reachable:
            issues.append(_issue("error", "unreachable_step",
                                 f"步骤 {sid} 从入口不可达", node_id=sid))
    used = used_slot_keys(definition)
    for key in seen_slots - used:
        issues.append(_issue("warning", "unused_slot",
                             f"角色槽位 {key} 未被任何步骤或 loop 使用",
                             slot_key=key))
    if len(jdumps(definition).encode("utf-8")) > MAX_DEFINITION_BYTES:
        issues.append(_issue("error", "definition_too_large", "模板 definition 超过大小限制"))
    if layout is not None:
        if not isinstance(layout, dict):
            issues.append(_issue("error", "invalid_layout", "layout 必须是对象"))
        elif len(jdumps(layout).encode("utf-8")) > MAX_LAYOUT_BYTES:
            issues.append(_issue("error", "layout_too_large", "layout 超过大小限制"))
    for key in _text_slots(definition):
        if key not in seen_slots:
            issues.append(_issue("error", "unknown_text_slot",
                                 f"文本占位符引用未知槽位 {key}", slot_key=key))
    return issues


def _synthetic_mapping(definition: dict) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for i, key in enumerate(_slots(definition)):
        mapping[key] = f"__slot_{i}"
    return mapping


def _substitute(value, mapping: dict[str, str]):
    if isinstance(value, str):
        return TOKEN_RE.sub(lambda m: mapping.get(m.group(1), m.group(0)), value)
    if isinstance(value, list):
        return [_substitute(x, mapping) for x in value]
    if isinstance(value, dict):
        return {k: _substitute(v, mapping) for k, v in value.items()}
    return value


def compile_definition(definition: dict, role_mapping: dict[str, str]) -> dict:
    """把 role_slot 模板编译成 workflow 引擎可直接消费的 definition。"""
    slots = _slots(definition)
    used = used_slot_keys(definition)
    missing = sorted(k for k in used if not role_mapping.get(k))
    if missing:
        raise ConflictError("missing role mapping for slots: " + ", ".join(missing))
    unknown = sorted(k for k in used if k not in slots)
    if unknown:
        raise ConflictError("unknown role slots: " + ", ".join(unknown))
    out = deepcopy(definition)
    out.pop("role_slots", None)
    out.pop("schema_version", None)
    out.pop("layout", None)
    for step in out.get("steps") or []:
        if not isinstance(step, dict):
            continue
        slot = str(step.pop("role_slot", "") or "")
        if slot:
            step["role"] = role_mapping[slot]
        reviewer = str(step.pop("reviewer_slot", "") or "")
        if reviewer:
            step["reviewer"] = role_mapping[reviewer]
    loop = out.get("loop")
    if isinstance(loop, dict):
        for phase in LOOP_PHASES:
            spec = loop.get(phase)
            if isinstance(spec, dict):
                slot = str(spec.pop("role_slot", "") or "")
                if slot:
                    spec["role"] = role_mapping[slot]
    for raw in out.get("verification") or []:
        if isinstance(raw, dict):
            slot = str(raw.pop("verifier_slot", "") or "")
            if slot:
                raw["verifier_role"] = role_mapping[slot]
    return _substitute(out, role_mapping)


def validate_template(conn: sqlite3.Connection, definition: dict,
                      layout: dict | None = None) -> list[dict]:
    issues = _shape_issues(definition, layout)
    if any(i["severity"] == "error" for i in issues):
        return issues
    compiled = compile_definition(definition, _synthetic_mapping(definition))
    try:
        workflow_service.validate_definition(conn, compiled)
    except ConflictError as exc:
        message = str(exc)
        match = re.search(r"\bstep\s+([A-Za-z0-9_.-]+)", message)
        issues.append(_issue("error", "workflow_invalid", message,
                             node_id=match.group(1) if match else None))
    # 运行期 _dynamic_slice_steps 比 _validate_loop 多校验 verify2 的差异；
    # 模板发布前必须提前拦住，避免运行到切片阶段才失败。
    loop = definition.get("loop")
    if isinstance(loop, dict):
        def slot_of(phase: str) -> str:
            spec = loop.get(phase)
            return str(spec.get("role_slot") or "") if isinstance(spec, dict) else ""
        verify2 = slot_of("verify2")
        if verify2 and verify2 in {slot_of("execute"), slot_of("fix")}:
            issues.append(_issue(
                "error", "verify2_role_conflict",
                "loop.verify2 不能与 execute/fix 使用同一个角色槽位", slot_key=verify2))
    return issues


def _version_row(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["definition"] = jloads(d.pop("definition_json", "{}"), {})
    d["origin_definition"] = jloads(d.pop("origin_definition_json", None), None)
    d["layout"] = jloads(d.pop("layout_json", "{}"), {})
    d["validation"] = jloads(d.pop("validation_json", "[]"), [])
    d["tags"] = jloads(d.pop("tags_json", "[]"), [])
    d["provenance"] = jloads(d.pop("provenance_json", "{}"), {})
    d["state"] = d.get("state") or "draft"
    return d


def _template_row(row) -> dict:
    d = dict(row)
    d["tags"] = jloads(d.pop("tags_json", "[]"), [])
    d["source_ref"] = jloads(d.pop("source_ref_json", "{}"), {})
    return d


def list_templates(conn: sqlite3.Connection, q: str | None = None,
                   status: str | None = None) -> list[dict]:
    where, params = [], []
    if status:
        where.append("status=?")
        params.append(status)
    if q:
        where.append("(name LIKE ? OR description LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])
    sql = "SELECT * FROM workflow_templates"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC"
    out = []
    for row in conn.execute(sql, params):
        d = _template_row(row)
        dv = d.get("draft_version")
        pv = d.get("published_version")
        d["has_draft"] = bool(dv)
        d["has_published"] = bool(pv)
        d["usage_count"] = conn.execute(
            "SELECT COUNT(*) AS n FROM workflows WHERE template_id=?", (d["id"],)
        ).fetchone()["n"]
        out.append(d)
    return out


def get_template(conn: sqlite3.Connection, tid: str) -> dict | None:
    row = conn.execute("SELECT * FROM workflow_templates WHERE id=?", (tid,)).fetchone()
    if not row:
        return None
    d = _template_row(row)
    draft = None
    if d.get("draft_version"):
        draft = _version_row(conn.execute(
            "SELECT * FROM workflow_template_versions WHERE template_id=? AND version=?",
            (tid, d["draft_version"])).fetchone())
    published = None
    if d.get("published_version"):
        published = _version_row(conn.execute(
            "SELECT * FROM workflow_template_versions WHERE template_id=? AND version=?",
            (tid, d["published_version"])).fetchone())
    d["draft"] = draft
    d["published"] = published
    d["versions"] = [
        {"version": r["version"], "state": r["state"], "revision": r["revision"],
         "created_at": r["created_at"], "published_at": r["published_at"]}
        for r in conn.execute(
            "SELECT version,state,revision,created_at,published_at "
            "FROM workflow_template_versions WHERE template_id=? ORDER BY version DESC",
            (tid,))
    ]
    return d


def _blank_definition() -> dict:
    return {"schema_version": 1, "role_slots": [], "steps": []}


def create_template(conn: sqlite3.Connection, name: str, description: str = "",
                    definition: dict | None = None, layout: dict | None = None,
                    tags: list[str] | None = None, use_mode: str = "both",
                    created_by: str = "human", source_kind: str = "manual",
                    source_ref: dict | None = None,
                    origin_definition: dict | None = None) -> dict:
    name = str(name or "").strip()
    if not name:
        raise ConflictError("template name is required")
    definition = deepcopy(definition or _blank_definition())
    layout = layout or {}
    now = now_iso()
    tid = new_id()
    version = 1
    issues = validate_template(conn, definition, layout)
    conn.execute(
        "INSERT INTO workflow_templates "
        "(id,name,description,tags_json,use_mode,status,source_kind,source_ref_json,"
        " draft_version,published_version,created_by,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, name, description, jdumps(tags or []), use_mode, "active", source_kind,
         jdumps(source_ref or {}), version, None, created_by, now, now),
    )
    conn.execute(
        "INSERT INTO workflow_template_versions "
        "(template_id,version,state,name,description,tags_json,definition_json,"
        " origin_definition_json,layout_json,validation_json,definition_hash,revision,"
        " based_on_version,provenance_json,created_by,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, version, "draft", name, description, jdumps(tags or []), jdumps(definition),
         jdumps(origin_definition) if origin_definition is not None else None,
         jdumps(layout), jdumps(issues), _definition_hash(definition), 1, None,
         jdumps(source_ref or {}), created_by, now),
    )
    conn.commit()
    return get_template(conn, tid)  # type: ignore[return-value]


def _draft_version(conn: sqlite3.Connection, template: dict) -> tuple[int, dict | None]:
    if template.get("draft_version"):
        row = conn.execute(
            "SELECT * FROM workflow_template_versions WHERE template_id=? AND version=?",
            (template["id"], template["draft_version"])).fetchone()
        return int(template["draft_version"]), _version_row(row)
    maxv = conn.execute(
        "SELECT COALESCE(MAX(version),0) AS n FROM workflow_template_versions WHERE template_id=?",
        (template["id"],)).fetchone()["n"]
    version = int(maxv) + 1
    base = template.get("published_version")
    return version, None


def save_draft(conn: sqlite3.Connection, tid: str, definition: dict,
               layout: dict | None = None, name: str | None = None,
               description: str | None = None, tags: list[str] | None = None,
               use_mode: str | None = None, base_revision: int | None = None,
               created_by: str = "human") -> dict:
    template = get_template(conn, tid)
    if template is None:
        raise LookupError(tid)
    if template["status"] == "archived":
        raise ConflictError("template is archived")
    version, existing = _draft_version(conn, template)
    if base_revision is not None and existing and int(existing["revision"]) != int(base_revision):
        raise ConflictError("template revision conflict; reload before saving")
    issues = validate_template(conn, definition, layout or {})
    now = now_iso()
    new_name = str(name if name is not None else template["name"]).strip()
    if not new_name:
        raise ConflictError("template name is required")
    new_desc = description if description is not None else template["description"]
    new_tags = tags if tags is not None else template["tags"]
    new_mode = use_mode or template.get("use_mode") or "both"
    if existing is None:
        origin_definition = None
        base_version = template.get("published_version")
        if base_version:
            base_row = conn.execute(
                "SELECT origin_definition_json FROM workflow_template_versions "
                "WHERE template_id=? AND version=?", (tid, base_version)).fetchone()
            origin_definition = jloads(base_row["origin_definition_json"], None) if base_row else None
        conn.execute(
            "INSERT INTO workflow_template_versions "
            "(template_id,version,state,name,description,tags_json,definition_json,"
            " origin_definition_json,layout_json,validation_json,definition_hash,revision,"
            " based_on_version,provenance_json,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, version, "draft", new_name, new_desc or "", jdumps(new_tags or []),
             jdumps(definition), jdumps(origin_definition) if origin_definition is not None else None,
             jdumps(layout or {}), jdumps(issues),
             _definition_hash(definition), 1, base_version,
             jdumps({"saved_from": "draft"}), created_by, now),
        )
    else:
        revision = int(existing["revision"]) + 1
        conn.execute(
            "UPDATE workflow_template_versions SET name=?,description=?,tags_json=?,"
            "definition_json=?,layout_json=?,validation_json=?,definition_hash=?,"
            "revision=?,published_at=NULL WHERE template_id=? AND version=?",
            (new_name, new_desc or "", jdumps(new_tags or []), jdumps(definition),
             jdumps(layout or {}), jdumps(issues), _definition_hash(definition),
             revision, tid, version),
        )
    conn.execute(
        "UPDATE workflow_templates SET name=?,description=?,tags_json=?,use_mode=?,"
        "draft_version=?,updated_at=? WHERE id=?",
        (new_name, new_desc or "", jdumps(new_tags or []), new_mode, version, now, tid),
    )
    conn.commit()
    return get_template(conn, tid)  # type: ignore[return-value]


def publish(conn: sqlite3.Connection, tid: str, change_note: str = "",
            created_by: str = "human") -> dict:
    template = get_template(conn, tid)
    if template is None:
        raise LookupError(tid)
    draft = template.get("draft")
    if not draft:
        raise ConflictError("template has no draft to publish")
    issues = validate_template(conn, draft["definition"], draft.get("layout") or {})
    errors = [i for i in issues if i["severity"] == "error"]
    if errors:
        raise ConflictError("template validation failed: " + errors[0]["message"])
    now = now_iso()
    conn.execute(
        "UPDATE workflow_template_versions SET state='published', published_at=?,"
        " validation_json=? WHERE template_id=? AND version=?",
        (now, jdumps(issues), tid, draft["version"]))
    conn.execute(
        "UPDATE workflow_templates SET published_version=?, draft_version=NULL, updated_at=? "
        "WHERE id=?", (draft["version"], now, tid))
    conn.commit()
    return get_template(conn, tid)  # type: ignore[return-value]


def archive_template(conn: sqlite3.Connection, tid: str) -> dict:
    template = get_template(conn, tid)
    if template is None:
        raise LookupError(tid)
    conn.execute("UPDATE workflow_templates SET status='archived',updated_at=? WHERE id=?",
                 (now_iso(), tid))
    conn.commit()
    return get_template(conn, tid)  # type: ignore[return-value]


def get_version(conn: sqlite3.Connection, tid: str, version: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM workflow_template_versions WHERE template_id=? AND version=?",
        (tid, version)).fetchone()
    return _version_row(row)


def rollback_template(conn: sqlite3.Connection, tid: str, version: int,
                      created_by: str = "human") -> dict:
    template = get_template(conn, tid)
    if template is None:
        raise LookupError(tid)
    target = get_version(conn, tid, version)
    if target is None:
        raise LookupError(f"template version {version}")
    now = now_iso()
    existing_version, existing = _draft_version(conn, template)
    name = target["name"] or template["name"]
    description = target.get("description") or ""
    tags = target.get("tags") or []
    if existing is None:
        new_version = existing_version
        conn.execute(
            "INSERT INTO workflow_template_versions "
            "(template_id,version,state,name,description,tags_json,definition_json,"
            " origin_definition_json,layout_json,validation_json,definition_hash,revision,"
            " based_on_version,provenance_json,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, new_version, "draft", name, description, jdumps(tags),
             jdumps(target["definition"]),
             jdumps(target.get("origin_definition")) if target.get("origin_definition") is not None else None,
             jdumps(target.get("layout") or {}), jdumps(target.get("validation") or []),
             _definition_hash(target["definition"]), 1, version,
             jdumps({"rollback_from": version}), created_by, now),
        )
    else:
        new_version = existing_version
        conn.execute(
            "UPDATE workflow_template_versions SET name=?,description=?,tags_json=?,"
            "definition_json=?,origin_definition_json=?,layout_json=?,validation_json=?,"
            "definition_hash=?,revision=?,based_on_version=?,provenance_json=?,published_at=NULL "
            "WHERE template_id=? AND version=?",
            (name, description, jdumps(tags), jdumps(target["definition"]),
             jdumps(target.get("origin_definition")) if target.get("origin_definition") is not None else None,
             jdumps(target.get("layout") or {}), jdumps(target.get("validation") or []),
             _definition_hash(target["definition"]), int(existing["revision"]) + 1, version,
             jdumps({"rollback_from": version}), tid, new_version),
        )
    conn.execute(
        "UPDATE workflow_templates SET name=?,description=?,tags_json=?,draft_version=?,"
        "updated_at=? WHERE id=?",
        (name, description, jdumps(tags), new_version, now, tid))
    conn.commit()
    return get_template(conn, tid)  # type: ignore[return-value]


def duplicate_template(conn: sqlite3.Connection, tid: str, name: str | None = None,
                       created_by: str = "human") -> dict:
    template = get_template(conn, tid)
    if template is None:
        raise LookupError(tid)
    source = template.get("draft") or template.get("published")
    if not source:
        raise ConflictError("template has no definition")
    return create_template(
        conn, name or f"{template['name']} (副本)",
        description=template.get("description") or "",
        definition=source["definition"], layout=source.get("layout") or {},
        tags=template.get("tags") or [], use_mode=template.get("use_mode") or "both",
        created_by=created_by, source_kind="manual",
        source_ref={"duplicated_from": tid, "version": source["version"]},
    )


def _published_version(conn: sqlite3.Connection, tid: str, version: int | None = None) -> tuple[dict, dict]:
    template = get_template(conn, tid)
    if template is None:
        raise LookupError(tid)
    v = version or template.get("published_version")
    if not v:
        raise ConflictError("template has no published version")
    row = conn.execute(
        "SELECT * FROM workflow_template_versions WHERE template_id=? AND version=?",
        (tid, v)).fetchone()
    if row is None:
        raise LookupError(f"template version {v}")
    data = _version_row(row)
    if data.get("state") != "published":
        raise ConflictError("template version is not published")
    return template, data


def _role_ok(role: str) -> bool:
    return bool(role) and role != "human" and not role.startswith("__")


def validate_mapping(conn: sqlite3.Connection, definition: dict,
                     role_mapping: dict[str, str],
                     task: dict | None = None) -> list[dict]:
    issues: list[dict] = []
    slots = _slots(definition)
    used = used_slot_keys(definition)
    for key in sorted(used):
        role = role_mapping.get(key)
        if not role:
            issues.append(_issue("error", "missing_mapping",
                                 f"角色槽位 {key} 未映射", slot_key=key))
            continue
        if not _role_ok(role):
            issues.append(_issue("error", "invalid_role", f"槽位 {key} 的角色非法", slot_key=key))
            continue
        row = roles_service.get(conn, role)
        if row is None:
            issues.append(_issue("error", "role_missing", f"角色不存在: {role}", slot_key=key))
        elif not row.get("enabled", True):
            issues.append(_issue("error", "role_disabled", f"角色已停用: {role}", slot_key=key))
        elif roles_service.is_onboarding(conn, role):
            issues.append(_issue("error", "onboarding_role",
                                 f"onboarding 助理不能用于工作流: {role}", slot_key=key))
    try:
        compiled = compile_definition(definition, role_mapping)
        workflow_service.validate_definition(conn, compiled)
    except ConflictError as exc:
        issues.append(_issue("error", "workflow_invalid", str(exc)))
    if task is not None and not any(i["severity"] == "error" for i in issues):
        assignees = list(dict.fromkeys(
            [*(task.get("assignees") or []), *([task.get("assignee")] if task.get("assignee") else [])]))
        used_roles = _workflow_roles(compile_definition(definition, role_mapping))
        uncovered = [r for r in assignees if r not in used_roles]
        if uncovered:
            issues.append(_issue(
                "warning", "uncovered_assignees",
                "任务中以下角色未被模板覆盖: " + "、".join(uncovered)))
    return issues


def _workflow_roles(definition: dict) -> set[str]:
    roles: set[str] = set()
    for step in _steps(definition):
        if step.get("role"):
            roles.add(str(step["role"]))
        if step.get("reviewer"):
            roles.add(str(step["reviewer"]))
    loop = definition.get("loop")
    if isinstance(loop, dict):
        for phase in LOOP_PHASES:
            spec = loop.get(phase)
            if isinstance(spec, dict) and spec.get("role"):
                roles.add(str(spec["role"]))
    return roles


def _pending_draft_count(conn: sqlite3.Connection, task_id: str) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) AS n FROM board_drafts WHERE source_task=? AND status='pending' "
        "AND COALESCE(kind,'tasks')='workflow'", (task_id,)).fetchone()["n"])


def preview_apply(conn: sqlite3.Connection, task_id: str, tid: str,
                  version: int | None, role_mapping: dict[str, str]) -> dict:
    task = tasks_service.get(conn, task_id)
    if task is None:
        raise LookupError(task_id)
    template, ver = _published_version(conn, tid, version)
    issues = validate_mapping(conn, ver["definition"], role_mapping, task)
    try:
        resolved = compile_definition(ver["definition"], role_mapping)
    except ConflictError as exc:
        resolved = None
        issues.append(_issue("error", "compile_failed", str(exc)))
    workflow = workflow_service.get_for_task(conn, task_id)
    pending = _pending_draft_count(conn, task_id)
    return {
        "template": {"id": template["id"], "name": template["name"],
                     "version": ver["version"], "use_mode": template.get("use_mode")},
        "task": {"id": task_id, "status": task.get("status"),
                 "assignees": task.get("assignees") or []},
        "issues": issues,
        "definition": resolved,
        "role_mapping": role_mapping,
        "task_roles": list(task.get("assignees") or []),
        "uncovered_assignees": [
            r for r in (task.get("assignees") or [])
            if resolved and r not in _workflow_roles(resolved)
        ],
        "conflicts": {
            "running_workflow": bool(workflow and workflow.get("status") == "running"),
            "running_workflow_id": workflow.get("id") if workflow and workflow.get("status") == "running" else None,
            "running_execution_no": workflow.get("execution_no") if workflow and workflow.get("status") == "running" else None,
            "pending_drafts": pending,
            "task_terminal": task.get("status") in ("done", "closed"),
        },
    }


def apply_to_task(conn: sqlite3.Connection, task_id: str, tid: str,
                  version: int | None, role_mapping: dict[str, str],
                  *, restart_running: bool = False,
                  restart_reason: str | None = None,
                  restart_note: str | None = None,
                  reject_pending_drafts: bool = False,
                  acknowledge_uncovered: bool = False,
                  mode: str = "direct", actor: str = "human") -> dict:
    task = tasks_service.get(conn, task_id)
    if task is None:
        raise LookupError(task_id)
    if task.get("status") in ("done", "closed"):
        raise ConflictError("task is terminal")
    template, ver = _published_version(conn, tid, version)
    issues = validate_mapping(conn, ver["definition"], role_mapping, task)
    errors = [i for i in issues if i["severity"] == "error"]
    if errors:
        raise ConflictError("; ".join(i["message"] for i in errors))
    uncovered = [r for r in (task.get("assignees") or [])
                 if r not in _workflow_roles(compile_definition(ver["definition"], role_mapping))]
    if uncovered and not acknowledge_uncovered:
        raise ConflictError("uncovered task roles: " + ", ".join(uncovered))
    workflow = workflow_service.get_for_task(conn, task_id)
    if workflow and workflow.get("status") == "running" and not restart_running:
        raise ConflictError(
            "task already has a running workflow; workflow replacement is not allowed. "
            "Confirm restart_running to terminate the current execution and restart from step 1.")
    pending = _pending_draft_count(conn, task_id)
    if pending and mode == "direct" and not reject_pending_drafts:
        raise ConflictError("task has pending workflow drafts; reject_pending_drafts required")
    if pending and reject_pending_drafts:
        now = now_iso()
        conn.execute(
            "UPDATE board_drafts SET status='rejected', decided_at=? "
            "WHERE source_task=? AND status='pending' AND COALESCE(kind,'tasks')='workflow'",
            (now, task_id))
    # 映射到的新角色如果尚未绑定到任务，按人类确认补绑。
    bound = set(task.get("assignees") or [])
    mapped_roles = list(dict.fromkeys(
        r for r in role_mapping.values() if r and r not in bound))
    if mapped_roles:
        tasks_service.add_assignees(conn, task_id, mapped_roles)
        task = tasks_service.get(conn, task_id) or task
    resolved = compile_definition(ver["definition"], role_mapping)
    resolved["template"] = {
        "id": template["id"], "version": ver["version"], "name": template["name"],
        "role_mapping": role_mapping, "applied_by": actor, "applied_at": now_iso(),
    }
    if mode == "draft":
        from . import board as board_service
        draft = board_service.create_draft(
            conn, goal=template["name"], tree=resolved, created_by=f"human:{actor}",
            source_task=task_id, kind="workflow")
        return {"mode": "draft", "draft": draft}
    wf = workflow_service.create_workflow(
        conn, task_id, resolved, created_by=f"human:{actor}",
        enforce_role_coverage=not acknowledge_uncovered,
        restart_running=restart_running, restart_reason=restart_reason,
        restart_note=restart_note)
    conn.execute(
        "INSERT INTO activities (task_id, role_slug, kind, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (task_id, "human", "comment", jdumps({
            "body": f"已套用工作流模板「{template['name']}」v{ver['version']}。"
        }), now_iso()))
    conn.commit()
    return {"mode": "direct", "workflow": wf}


def set_plan_seed(conn: sqlite3.Connection, task_id: str, tid: str,
                  version: int | None, created_by: str = "human") -> dict:
    task = tasks_service.get(conn, task_id)
    if task is None:
        raise LookupError(task_id)
    template, ver = _published_version(conn, tid, version)
    if template.get("use_mode") not in ("seed", "both"):
        raise ConflictError("template is not enabled as a Cindy seed")
    seed = {"template_id": tid, "version": ver["version"], "name": template["name"],
            "set_by": created_by, "set_at": now_iso()}
    conn.execute("UPDATE tasks SET plan_seed_json=?, updated_at=? WHERE id=?",
                 (jdumps(seed), now_iso(), task_id))
    conn.commit()
    return seed


def clear_plan_seed(conn: sqlite3.Connection, task_id: str) -> None:
    if tasks_service.get(conn, task_id) is None:
        raise LookupError(task_id)
    conn.execute("UPDATE tasks SET plan_seed_json=NULL, updated_at=? WHERE id=?",
                 (now_iso(), task_id))
    conn.commit()


def render_seed_for_prompt(conn: sqlite3.Connection, task: dict) -> str:
    seed = task.get("plan_seed") or {}
    if not seed.get("template_id"):
        return ""
    try:
        _template, ver = _published_version(
            conn, seed["template_id"], seed.get("version"))
    except (LookupError, ConflictError):
        return ""
    definition = ver["definition"]
    slots = _slots(definition)
    lines = [
        f"- 模板：{seed.get('name') or ver.get('name')}（v{ver['version']}）",
        "- 模板是建议骨架，必须结合任务的实际 Roster、scope 与知识仓库调整；"
        "不要照抄不适合的步骤，也不要虚构角色。",
        "- 角色槽位：" + "；".join(
            f"{k}={v.get('label') or k}"
            + (f"(建议 {', '.join(v.get('suggested_roles') or [])})"
               if v.get("suggested_roles") else "")
            for k, v in slots.items()),
        "- 模板骨架（role_slot 需要你映射为 Roster 中的实际角色）：",
        json.dumps(definition, ensure_ascii=False, indent=2)[:12000],
        "- 最终仍按正常 workflow 草案提交；不要输出 role_slot 字段，必须写实际角色。",
    ]
    return "\n".join(lines)


def import_definition(conn: sqlite3.Connection, definition: dict, *,
                      source_kind: str, source_ref: dict,
                      name: str, created_by: str = "human") -> dict:
    """把已落/草案 workflow definition 转成 role-slot 模板草稿。"""
    normalized = workflow_service.normalize_definition(conn, deepcopy(definition))
    workflow_service.validate_definition(conn, normalized)
    steps = [deepcopy(s) for s in (normalized.get("steps") or normalized.get("children") or [])
             if isinstance(s, dict)]
    loop = deepcopy(normalized.get("loop")) if isinstance(normalized.get("loop"), dict) else None
    role_to_slot: dict[str, str] = {}
    slot_labels: dict[str, str] = {}
    slot_suggested: dict[str, list[str]] = {}
    key_to_role: dict[str, str] = {}
    warnings: list[str] = []

    def role_of(spec: dict) -> str:
        return str(spec.get("role") or spec.get("role_slug") or "").strip()

    def add_slot(role: str, preferred: str) -> str:
        if role in role_to_slot:
            return role_to_slot[role]
        key = preferred
        n = 2
        while key in key_to_role and key_to_role[key] != role:
            key = f"{preferred}_{n}"
            n += 1
        key_to_role[key] = role
        role_to_slot[role] = key
        role = roles_service.get(conn, role)
        slot_labels[key] = str((role or {}).get("display_name") or key)
        slot_suggested[key] = [role_slug] if (role_slug := next(
            (r for r, s in role_to_slot.items() if s == key), "")) else []
        return key

    loop_slots: dict[str, str] = {}
    if loop:
        phase_specs = {p: loop.get(p) for p in LOOP_PHASES if isinstance(loop.get(p), dict)}
        execute_role = role_of(phase_specs.get("execute") or {})
        verify_role = role_of(phase_specs.get("verify") or {})
        decide_role = role_of(phase_specs.get("decide") or {})
        fix_role = role_of(phase_specs.get("fix") or {}) or execute_role
        verify2_role = role_of(phase_specs.get("verify2") or {}) or verify_role
        scope_role = role_of(phase_specs.get("scope") or {}) or decide_role
        preferred = {
            "execute": "executor",
            "verify": "verifier",
            "fix": "executor" if fix_role == execute_role else "fixer",
            "verify2": "verifier" if verify2_role == verify_role else "verifier2",
            "decide": "decider",
            "scope": "decider" if scope_role == decide_role else "scope_owner",
        }
        for phase, spec in phase_specs.items():
            role = role_of(spec)
            if role:
                loop_slots[phase] = add_slot(role, preferred.get(phase, f"role_{phase}"))
    for step in steps:
        role = role_of(step)
        if role:
            step["__role_slot"] = add_slot(role, f"role_{re.sub(r'[^A-Za-z0-9_-]', '_', role)}")
        reviewer = str(step.get("reviewer") or "").strip()
        if reviewer:
            step["__reviewer_slot"] = add_slot(reviewer, f"reviewer_{re.sub(r'[^A-Za-z0-9_-]', '_', reviewer)}")
        # replace @mentions after all slots are known
    for raw in normalized.get("verification") or []:
        if isinstance(raw, dict) and raw.get("verifier_role"):
            add_slot(str(raw["verifier_role"]), f"verifier_{re.sub(r'[^A-Za-z0-9_-]', '_', str(raw['verifier_role']))}")

    def tokenize(value):
        if isinstance(value, str):
            def repl(m):
                key = role_to_slot.get(m.group(1))
                return f"@{{{{slot:{key}}}}}" if key else m.group(0)
            return MENTION_RE.sub(repl, value)
        if isinstance(value, list):
            return [tokenize(x) for x in value]
        if isinstance(value, dict):
            return {k: tokenize(v) for k, v in value.items()}
        return value

    out_steps = []
    for step in steps:
        clean = {k: tokenize(v) for k, v in step.items()
                 if k in STEP_FIELDS or k in ("__role_slot", "__reviewer_slot")}
        slot = clean.pop("__role_slot", None)
        reviewer_slot = clean.pop("__reviewer_slot", None)
        if slot:
            clean["role_slot"] = slot
        if reviewer_slot:
            clean["reviewer_slot"] = reviewer_slot
        for key in ("instructions", "title", "contract"):
            if isinstance(clean.get(key), str) and len(clean[key]) > MAX_TEXT:
                clean[key] = clean[key][:MAX_TEXT] + "…"
                warnings.append(f"step {clean.get('id')}: {key} 已截断")
        if clean.get("on_reject") and not isinstance(clean["on_reject"], list):
            clean["on_reject"] = []
        out_steps.append(clean)
    out_loop = None
    if loop:
        out_loop = {}
        for key, value in loop.items():
            if key in LOOP_PHASES and isinstance(value, dict):
                spec = {k: tokenize(v) for k, v in value.items()
                        if k not in ("role", "role_slug", "gate", "on_reject")}
                if key in loop_slots:
                    spec["role_slot"] = loop_slots[key]
                out_loop[key] = spec
            elif key == "from":
                out_loop[key] = value
    out_verification = []
    for raw in normalized.get("verification") or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        role = str(item.pop("verifier_role", "") or "")
        if role:
            item["verifier_slot"] = role_to_slot.get(role, add_slot(role, f"verifier_{role}"))
        out_verification.append(item)
    if normalized.get("knowledge_repos"):
        warnings.append("源 workflow 的 knowledge_repos 已忽略（任务特定）")
    template_def = {
        "schema_version": 1,
        "role_slots": [
            {"key": key, "label": slot_labels.get(key, key),
             "suggested_roles": slot_suggested.get(key, [])}
            for key in key_to_role
        ],
        "steps": out_steps,
        "feedback_rules": tokenize(normalized.get("feedback_rules") or []),
        "verification": out_verification,
    }
    if out_loop:
        template_def["loop"] = out_loop
    result = create_template(
        conn, name=name, description="",
        definition=template_def, layout={}, tags=[],
        use_mode="both", created_by=created_by,
        source_kind=source_kind, source_ref=source_ref,
        origin_definition=template_def,
    )
    result["warnings"] = warnings
    return result


def import_workflow(conn: sqlite3.Connection, workflow_id: str,
                    name: str | None = None, created_by: str = "human") -> dict:
    wf = workflow_service.get_workflow(conn, workflow_id)
    if wf is None:
        raise LookupError(workflow_id)
    task = tasks_service.get(conn, wf.get("task_id")) or {}
    return import_definition(
        conn, wf.get("definition") or {}, source_kind="import_workflow",
        source_ref={"workflow_id": workflow_id, "task_id": wf.get("task_id")},
        name=name or f"{task.get('title') or wf.get('task_id')} · 模板",
        created_by=created_by,
    )


def import_draft(conn: sqlite3.Connection, draft_id: str,
                 name: str | None = None, created_by: str = "human") -> dict:
    from . import board as board_service
    draft = board_service.get_draft(conn, draft_id)
    if draft is None:
        raise LookupError(draft_id)
    if (draft.get("kind") or "tasks") != "workflow":
        raise ConflictError("draft is not a workflow draft")
    task = tasks_service.get(conn, draft.get("source_task")) if draft.get("source_task") else {}
    return import_definition(
        conn, draft.get("tree") or {}, source_kind="import_draft",
        source_ref={"draft_id": draft_id, "task_id": draft.get("source_task")},
        name=name or f"{(task or {}).get('title') or draft.get('goal') or '工作流'} · 模板",
        created_by=created_by,
    )


def draft_to_editor(conn: sqlite3.Connection, draft_id: str) -> dict:
    from . import board as board_service
    draft = board_service.get_draft(conn, draft_id)
    if draft is None:
        raise LookupError(draft_id)
    if (draft.get("kind") or "tasks") != "workflow":
        raise ConflictError("draft is not a workflow draft")
    task = tasks_service.get(conn, draft.get("source_task")) if draft.get("source_task") else {}
    # 用临时转换得到槽位映射；不落库。
    # 转换会创建模板，因此这里复用一个轻量转换：构造模板后立即给出草稿视图，
    # 前端保存草案时通过 PATCH 回到原草案。
    converted = import_definition(
        conn, draft.get("tree") or {}, source_kind="import_draft",
        source_ref={"draft_id": draft_id, "task_id": draft.get("source_task")},
        name=f"{(task or {}).get('title') or draft.get('goal') or '工作流'} · 草案",
        created_by="human",
    )
    return {
        "draft": draft,
        "task": task,
        "template": converted,
        "definition": (converted.get("draft") or {}).get("definition") or {},
        "layout": (converted.get("draft") or {}).get("layout") or {},
        "role_mapping": {},
    }
