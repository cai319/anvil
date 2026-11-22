"""P1.11 prompt 单点化：协议字节快照 + run 类型分类唯一入口。

prompt 字节变化会直接改变 Agent 行为；这些测试锁定当前基线，重构后必须
逐字节一致，任何有意修改都要显式更新快照。
"""
from __future__ import annotations

import hashlib

from anvil.db import connect
from anvil.graph import nodes, render
from anvil.services.workflow import step_prompt

PROTOCOL_HASHES = {
    "ATTACHMENT_BUDGET": "4c70c54379099cb7c055f41ef354a8b46922a6e1ce98d852d7185f59d8db453c",
    "DELIVERABLE_MANIFEST": "78cda9fd3b4193b780d26e30cc06ed53406e7ae44ad3d9b92a73f14c1db3cc37",
    "COLLABORATION_PROTOCOL": "0fe5da109d5b5bddca2902bdb567d9d73bd9aa277a168008b9c785eacdb9ba73",
    "DELEGATION_PROTOCOL": "13e210def59bb3acca093e3857ff1eacb659494afa5c63f5fb2a78b291e16bca",
    "SUBAGENT_PROTOCOL": "76741ff87b06f922078386b1f716cd6d2f73ba891af64a8210e6a11e059d75f4",
    "SUBAGENT_AUDIT_PROTOCOL": "5315c430bd2e187b0cfe7392b1aebc149cc25f3e3c0014e80297b077be7db066",
    "DISPATCH_PROTOCOL": "22091bf23e72d9a64ca25f2317d85615dba8914cae7879804dab2406cb20b741",
    "REPLY_DELEGATION_PROTOCOL": "8dc2a3d261ebe5d8c31c905f3c1028fb729354497dc4d1cb6e3eaf539ce83c1e",
    "PROTOCOL": "0b756afb7d68ce876644bf9c76875ef2e11f6b48cf695223c2ed8ce93e10fa58",
    "REPLY_PROTOCOL": "dbffdf70f0039f544fa5c1a0f1ea5ca7483e844a7f5eb578481ee4c2b9b4c0e7",
    "STEP_PROTOCOL": "f7e446864262f75429a084b37aece9da86ae12228ea7f59e77711c29e43129cc",
    "FINDING_WRITE_PROTOCOL": "3351d2138dbf79535ecb270f7fbf883d33b4e0463f5cd9b831d444ba3c3cba88",
    "FINDING_PROTOCOL": "a3ae9d27c4881e73a92673554bb21e5410b6ec2abfd97531c5a5cd959dea1482",
    "STEP_FINDING_PROTOCOL": "068aba37f396c6c234dd39c3fb65e7384f055e6aa37470c3bcd5e35c49b901ae",
    "PLANNING_PROTOCOL": "43c750004fba924010948113a5059975df9eb0517e20e545106811bdbcc34055",
    "REVISION_PROTOCOL": "2b528956dc6c6b021fff68ecb7d9494aeba5407e6780afcfd56fb605ee9ec934",
}


def test_protocol_blocks_are_byte_stable():
    actual = {
        name: hashlib.sha256(getattr(render, name).encode()).hexdigest()
        for name in PROTOCOL_HASHES
    }
    assert actual == PROTOCOL_HASHES


STEP_GOLDEN = (
    "## 工作流步骤 S2：验证\n所属任务：任务\n工作流 ID：W1\n你的角色：tester\n"
    "依赖步骤（已完成）：S1\n验收标准：\n- 测试通过\n- 覆盖边界\n\n"
    "任务背景：\n描述\n\n步骤说明：\n运行测试\n\n"
    "契约（必须遵守，违反即视为失败）：\n本步骤契约：\n不得修改源码\n\n\n"
    "## 上游结果（必须纳入本次处理）\n- S1 执行：已完成\n\n"
    "## 上次决策/评审反馈（必须处理）\n补一个边界用例\n\n"
    '完成后必须执行：`anvil step submit W1 S2 --summary "完成摘要" '
    '--changed-paths "src/a.py,src/b.py"`。\n不要调用 `anvil task submit` / '
    "`anvil task claim`。\n"
)


def test_step_prompt_golden():
    workflow = {
        "id": "W1",
        "definition": {},
        "steps": [
            {"step_id": "S1", "title": "执行", "role_slug": "dev",
             "depends_on": [], "acceptance": [], "instructions": "",
             "summary": "已完成", "evidence": [], "contract": "", "kind": "execute"},
            {"step_id": "S2", "title": "验证", "role_slug": "tester",
             "depends_on": ["S1"], "acceptance": ["测试通过", "覆盖边界"],
             "instructions": "运行测试", "summary": None, "evidence": [],
             "contract": "不得修改源码", "feedback": "补一个边界用例",
             "recovery_count": 0, "kind": "execute", "evidence_required": False,
             "slice_key": "", "source_step_id": None},
        ],
    }
    assert step_prompt(
        workflow, workflow["steps"][1],
        {"id": "T1", "title": "任务", "description": "描述"},
    ) == STEP_GOLDEN


def test_run_prompt_kind_is_single_source(tmp_path):
    conn = connect(str(tmp_path / "p.db"))
    try:
        assert nodes._run_prompt_kind(
            conn, {"prompt": "[anvil:findings]\n复查"}) == "findings"
        assert nodes._run_prompt_kind(
            conn, {"prompt": "x", "step_id": "S1"}) == "step"
        assert nodes._run_prompt_kind(
            conn, {"prompt": "[anvil:plan]\n规划"}) == "planning"
        assert nodes._run_prompt_kind(
            conn, {"prompt": "[anvil:reply]\n回复"}) == "reply"
        assert nodes._run_prompt_kind(conn, {"prompt": "普通任务"}) == "task"

        step = {"prompt": "x", "step_id": "S1", "role_slug": "dev"}
        assert nodes._protocol_for_run(conn, step) == render.STEP_PROTOCOL
        finding_step = {**step, "task_id": "T1"}
        assert nodes._protocol_for_run(conn, finding_step) == render.STEP_PROTOCOL
        assert nodes._protocol_for_run(
            conn, {"prompt": "[anvil:findings]\n复查"}) == render.FINDING_PROTOCOL
        assert nodes._protocol_for_run(
            conn, {"prompt": "[anvil:plan]\n规划"}
        ) == render.PLANNING_PROTOCOL + "\n\n" + render.REVISION_PROTOCOL
        assert nodes._protocol_for_run(
            conn, {"prompt": "[anvil:reply]\n回复"}) == render.REPLY_PROTOCOL
        assert nodes._protocol_for_run(conn, {"prompt": "普通任务"}) == render.PROTOCOL
    finally:
        conn.close()
