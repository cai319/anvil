"""常驻角色运行时：同一进程复用、忙时排队、空闲退出后 --resume 恢复同一会话。"""
from __future__ import annotations

import sys
import time

import pytest

from anvil.db import connect, open_conn

pytest.importorskip("anvil.graph")
from anvil.services import runs as runs_service  # noqa: E402
from anvil.services.daemon import DaemonManager, frame_user_message  # noqa: E402

FAKE_RUNTIME = '''
import json, os, sys, time
mode = "resume" if "--resume" in sys.argv else "new"
sid = sys.argv[sys.argv.index("--resume" if mode == "resume" else "--session-id") + 1]
with open(os.environ["FAKE_ARGV_LOG"], "a") as f:
    f.write(mode + " " + sid + "\\n")
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
for line in sys.stdin:
    time.sleep(0.2)
    print(json.dumps({"type": "result", "subtype": "success", "result": "OK",
                      "total_cost_usd": 0.01}), flush=True)
'''


def test_frame_format():
    assert frame_user_message("hi") == (
        '{"type": "user", "message": {"role": "user", "content": '
        '[{"type": "text", "text": "hi"}]}}\n')


@pytest.fixture
def env(tmp_path):
    fake = tmp_path / "fake_runtime.py"
    fake.write_text(FAKE_RUNTIME, encoding="utf-8")
    log = tmp_path / "argv.log"
    db_path = str(tmp_path / "t.db")
    conn = connect(db_path)
    mgr = DaemonManager(db_path, enabled=True, idle_release_minutes=60)
    yield conn, mgr, fake, log
    try:
        mgr.shutdown()
    except Exception:  # noqa: BLE001
        pass
    conn.close()


def _wait(predicate, timeout=6.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _run_status(db_path, rid):
    c = open_conn(db_path)
    try:
        r = runs_service.get_run(c, rid)
        return r["status"] if r else None
    finally:
        c.close()


def _deliver(mgr, conn, fake, log, run_id, text="hello"):
    runs_service.set_status(conn, run_id, "running")  # 等价于 resume→spawn 的状态流转
    args = {
        "executable": sys.executable,
        "args_new": [str(fake), "--session-id", "{{session_id}}"],
        "args_resume": [str(fake), "--resume", "{{session_id}}"],
        "ctx": {},
        "cwd": str(fake.parent),
        "env": {"FAKE_ARGV_LOG": str(log)},
        "prompt": text,
        "run_id": run_id,
        "task_id": None,
    }
    return mgr.ensure_and_deliver(conn, "qa-role", **args)


def test_same_process_reused_and_resume_after_exit(env):
    conn, mgr, fake, log = env
    db_path = mgr.db_path
    run = runs_service.create_run(conn, "qa-role", task_id=None)
    runs_service.set_status(conn, run["id"], "running")  # 等价于 spawn_node 的状态流转

    # 第一次唤醒：新建会话进程
    _deliver(mgr, conn, fake, log, run["id"], "first")
    assert _wait(lambda: _run_status(db_path, run["id"]) == "suspended"), "一轮结束应转 suspended"
    st1 = mgr.status("qa-role")
    assert st1["alive"] and st1["status"] == "idle"
    pid1 = st1["pid"]

    # 第二次唤醒：复用同一进程（核心要求：不新起进程）
    _deliver(mgr, conn, fake, log, run["id"], "second")
    assert _wait(lambda: _run_status(db_path, run["id"]) == "suspended")
    st2 = mgr.status("qa-role")
    assert st2["alive"] and st2["pid"] == pid1

    # 空闲释放后再次唤醒：--resume 同一 session_id 恢复上下文
    mgr.kill("qa-role")
    assert _wait(lambda: not mgr.status("qa-role")["alive"])
    _deliver(mgr, conn, fake, log, run["id"], "third")
    assert _wait(lambda: _run_status(db_path, run["id"]) == "suspended")
    modes = [line.split()[0] for line in log.read_text().splitlines()]
    assert modes == ["new", "resume"]
    sids = {line.split()[1] for line in log.read_text().splitlines()}
    assert len(sids) == 1  # 同一会话 id
