"""生成指令中的协作路由：每个 static/dynamic slice step 恰好一条。"""
from __future__ import annotations

import pytest

from anvil.db import connect
from anvil.services import workflow as wf_service
from anvil.util import now_iso

pytest.importorskip("anvil.graph")


def _conn(tmp_path):
    conn = connect(str(tmp_path / "collab.db"))
    now = now_iso()
    conn.execute("INSERT INTO tasks (id,title,status,created_at,updated_at) "
                 "VALUES ('T1','切片','todo',?,?)", (now, now))
    conn.commit()
    return conn


def test_slice_collab_rule_constant_is_complete():
    assert "协作路由" in wf_service.SLICE_COLLAB_RULE
    for needle in ("workflow", "mention", "subagent", "dispatch", "委派", "不新建工作"):
        assert needle in wf_service.SLICE_COLLAB_RULE, needle


def test_dynamic_slice_steps_append_rule_once(tmp_path):
    conn = _conn(tmp_path=tmp_path)
    try:
        loop = {
            "from": "S1",
            "execute": {"role": "dev", "instructions": "执行"},
            "verify": {"role": "qa", "instructions": "验证"},
            "decide": {"role": "pm"},
        }
        generated = wf_service._dynamic_slice_steps(
            conn, "S1", loop, [{"id": "A", "title": "A"}],
            existing_ids=set(), weight="standard")
        for step in generated:
            assert step["instructions"].count(wf_service.SLICE_COLLAB_RULE) == 1
        # 幂等：已有则不重复。
        loop2 = {"from": "S1",
                 "execute": {"role": "dev",
                             "instructions": "执行\n\n" + wf_service.SLICE_COLLAB_RULE},
                 "verify": {"role": "qa"}, "decide": {"role": "pm"}}
        generated2 = wf_service._dynamic_slice_steps(
            conn, "S1", loop2, [{"id": "B", "title": "B"}],
            existing_ids=set(), weight="standard")
        assert generated2[0]["instructions"].count(wf_service.SLICE_COLLAB_RULE) == 1
    finally:
        conn.close()


def test_static_slices_append_rule_once():
    definition = wf_service._expand_slices({
        "slices": [{
            "id": "A", "title": "A",
            "execute_role": "dev", "verify_role": "qa", "decision_role": "pm",
        }],
    })
    steps = definition.get("steps") or []
    assert steps
    for step in steps:
        assert step["instructions"].count(wf_service.SLICE_COLLAB_RULE) == 1
