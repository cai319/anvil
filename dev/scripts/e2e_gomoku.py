"""端到端验收场景：用「实现一个五子棋小程序」驱动 4 个角色走完整闭环。

流程（design §6/§7「Agent 是驾驶员」）：
  PM 拆任务 → 编排器为每个任务拉起对应角色的 demo agent 子进程 →
  agent 通过 `anvil` CLI 反向调用 claim → 干活（写文件到 worktree）→ submit →
  人（PM）review approve → done。

运行前提：后端已启动（ANVIL_AUTH_TOKEN=dev-secret、调度器开或关均可，本脚本手动派发）。
用法：python3 scripts/e2e_gomoku.py [base_url]
"""
from __future__ import annotations

import sys
import time

import httpx

REAL = "--real" in sys.argv
BASE = next((a for a in sys.argv[1:] if not a.startswith("--")), "http://127.0.0.1:7317")
API = BASE + "/api/v1"
H = {"X-Anvil-Session": "dev-secret"}
DEMO_AGENT = "/Users/ikaruga/cctest/aliceAgent/data/demo-agent.sh"
# 真实运行时：claude CLI 自动执行（需已登录）；demo 模式用假 agent 脚本。
EXECUTABLE = "/opt/homebrew/bin/claude" if REAL else DEMO_AGENT
PERMISSION_MODE = "bypassPermissions" if REAL else "default"
TIMEOUT = 600 if REAL else 60

ROLES = [
    ("pm", "产品经理"),
    ("designer", "设计师"),
    ("developer", "开发工程师"),
    ("tester", "测试工程师"),
]

# 任务 → (角色, 依赖) 的流水线
PIPELINE = [
    ("T-DESIGN", "设计五子棋小程序", "design", ["design"], "designer", []),
    ("T-DEV", "实现五子棋网页版", "feature", ["frontend"], "developer", ["T-DESIGN"]),
    ("T-TEST", "编写五子棋逻辑测试", "chore", ["test"], "tester", ["T-DEV"]),
]


def main() -> None:
    c = httpx.Client(base_url=API, timeout=15)

    # 1. 注册运行时（executable = 真实 claude 或 demo agent）
    c.post("/runtimes", json={
        "id": "claude-code",
        "display_name": "Claude Code" if REAL else "Demo Agent",
        "executable_path": EXECUTABLE, "status": "ready",
        "capabilities": {"managed_mcp": True, "tool_level_grant": "exact"},
    }, headers=H)

    # 2. 4 个角色，绑定 gomoku 仓库写权限
    for slug, name in ROLES:
        c.post("/roles", json={
            "slug": slug, "display_name": name,
            "runtime": {"id": "claude-code", "permission_mode": PERMISSION_MODE},
            "workspace": {"home": f"agents/{slug}/",
                          "repos": [{"name": "gomoku", "access": "write"}]},
            "tools": {"preset": "backend-default", "max_tool_tokens": 3500},
            "concurrency": {"max_parallel": 2},
        }, headers=H)
    print(f"[setup] 4 角色就绪（{'真实 claude' if REAL else 'demo agent'}）：{', '.join(r[0] for r in ROLES)}")

    # 3. PM 拆任务（带依赖的流水线）
    for tid, title, type_, tags, _role, deps in PIPELINE:
        c.post("/tasks", json={
            "id": tid, "title": title, "type": type_, "tags": tags,
            "description": f"{title}（五子棋小程序）",
            "acceptance": ["产出落地", "通过评审"],
            "scope_paths": ["**"], "repo": "gomoku", "depends_on": deps,
            "created_by": "pm",
        }, headers=H)
    print("[setup] 3 个任务（设计→开发→测试 依赖流水线）已创建")

    # 4. 按依赖顺序逐任务派发 + 评审
    for tid, _title, _t, _g, role, _deps in PIPELINE:
        print(f"\n▶ 派发 {tid} → {role}")
        r = c.post("/runs", json={"role_slug": role, "task_id": tid}, headers=H)
        assert r.status_code == 201, f"派发失败: {r.text}"
        run_id = r.json()["id"]

        # 等 agent 认领 + 干活 + 提交（任务进 in_review）
        deadline = time.time() + TIMEOUT
        status = None
        while time.time() < deadline:
            status = c.get(f"/tasks/{tid}", headers=H).json()["status"]
            if status == "in_review":
                break
            time.sleep(1)
        assert status == "in_review", f"{tid} 未提交（当前 {status}）"

        # 人工评审（HITL）：这是需要自然人确认的关卡，脚本不代劳。
        # 提示用户在浏览器点「通过/驳回」，轮询直到任务离开 in_review。
        print(f"  ⏸ {tid} 进入待评审 —— 请人工评审：{BASE}/tasks/{tid}（点「通过」或「驳回」）")
        while True:
            reviewed = c.get(f"/tasks/{tid}", headers=H).json()
            if reviewed["status"] != "in_review":
                break
            time.sleep(3)
        if reviewed["status"] != "done":
            print(f"  ⚠ {tid} 被人工驳回（当前 {reviewed['status']}）—— 需 agent 重做后重新派发，流水线在此暂停")
            break
        print(f"  ✔ {tid} 人工评审通过 · assignee={reviewed['assignee']} · run={run_id[:8]}")

    # 5. 验证产物 + 可追溯
    print("\n=== 验证 ===")
    tasks = c.get("/tasks", headers=H).json()
    assert all(t["status"] == "done" for t in tasks), "存在未完成任务"
    print(f"  任务全部 done：{len(tasks)} 个")

    runs = c.get("/runs", headers=H).json()
    assert len(runs) == 3 and all(r["status"] == "completed" for r in runs), \
        f"run 状态异常: {[(r['role_slug'], r['status']) for r in runs]}"
    print(f"  run 全部 completed：{[r['role_slug'] for r in runs]}")

    for tid in ("T-DESIGN", "T-DEV", "T-TEST"):
        tl = c.get(f"/tasks/{tid}/timeline", headers=H).json()
        kinds = [e["kind"] for e in tl]
        assert "claim" in kinds and "submit" in kinds and "review" in kinds, f"{tid} 时间线缺交互"
        print(f"  {tid} 时间线 {len(tl)} 条：claim/submit/review 齐全")

    # 抽查开发角色的 run 日志
    dev_run = next(r for r in runs if r["role_slug"] == "developer")
    log = c.get(f"/runs/{dev_run['id']}/log", headers=H).json()
    assert log["exists"] and "developer" in log["content"]
    print("  developer run 日志含 agent 输出 ✔")

    print("\n✅ 端到端全流程通过：拆任务 → 派发 → 认领 → 干活 → 提交 → 评审 → done")


if __name__ == "__main__":
    main()
