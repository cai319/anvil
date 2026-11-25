"""L2 replay runner: shadow home, version materialization, verifier isolation."""
from __future__ import annotations

import stat

from anvil import config, db
from anvil.services import (
    eval_runner,
    eval_suite,
    role_eval,
    role_space,
    roles,
    runtime as runtime_service,
)


def _script(tmp_path) -> str:
    path = tmp_path / "fake-agent.sh"
    path.write_text(
        "#!/bin/sh\n"
        "env | grep '^ANVIL_' | sort > agent_env.txt\n"
        "if env | grep -qE '^ANVIL_(ENDPOINT|TOKEN|EVAL_KEY)='; then exit 9; fi\n"
        "if grep -q CANDIDATE CLAUDE.md 2>/dev/null; then echo ok > done.txt; fi\n"
        "echo '[result] usage={\"input_tokens\": 10, \"output_tokens\": 5}'\n"
        "echo '{\"type\":\"result\",\"usage\":{\"input_tokens\":10,\"output_tokens\":5}}'\n",
        encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _item() -> dict:
    return {
        "item_id": "R-REPLAY-1",
        "suite_id": "s1",
        "split": "held_in",
        "family": "repo:demo;mech:1",
        "prompt": {"task_title": "实现验收", "step_instructions": "完成文件"},
        "workspace": {"kind": "temp"},
        "verifier": {
            "checks": [{"id": "done", "cmd": "test -f done.txt",
                        "expect_exit": 0, "weight": 1, "hard": True}],
            "forbidden_changes": ["tests/**"],
            "timeout_seconds": 30,
            "token_cap": 1000,
        },
    }


def test_replay_runner_materializes_version_and_hides_anvil(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROLE_EVAL_DIR", tmp_path / "eval")
    conn = db.connect(str(tmp_path / "replay.db"))
    try:
        runtime_service.register(
            conn, id="fake-rt", display_name="Fake",
            executable_path=_script(tmp_path), status="ready")
        roles.create(conn, slug="qa", display_name="QA",
                     definition={"runtime": {"id": "fake-rt"},
                                 "responsibilities": ["qa"]})
        home = config.AGENTS_DIR / "qa"
        base_instruction = (
            "<!-- ANVIL:CHARTER:BEGIN -->\n身份\n<!-- ANVIL:CHARTER:END -->\n"
            "<!-- ANVIL:STRATEGY:BEGIN -->\nBASE\n<!-- ANVIL:STRATEGY:END -->\n")
        (home / "instruction.md").write_text(base_instruction, encoding="utf-8")
        (home / "PRINCIPLES.md").write_text("准则\n", encoding="utf-8")
        baseline = role_space.snapshot(
            conn, "qa", kind="evolution", source="baseline", status="active")
        candidate = role_space.snapshot(
            conn, "qa", kind="evolution", source="rsi_candidate",
            status="candidate", force=True,
            file_overrides={"instruction.md": (
                "<!-- ANVIL:CHARTER:BEGIN -->\n身份\n<!-- ANVIL:CHARTER:END -->\n"
                "<!-- ANVIL:STRATEGY:BEGIN -->\nCANDIDATE\n"
                "<!-- ANVIL:STRATEGY:END -->\n").encode("utf-8")})
        monkeypatch.setattr(runtime_service, "load_definitions", lambda: {
            "fake-rt": {
                "id": "fake-rt",
                "invocation": {
                    "args": ["--prompt", "{{ prompt }}"],
                    "cwd": "{{ workspace_path }}",
                    "env": {"ANVIL_ENDPOINT": "http://127.0.0.1:7317",
                            "ANVIL_TOKEN": "secret", "PLAIN": "ok"},
                    "timeout_seconds": 30,
                },
                "projection": {"instruction": {
                    "target": "{{ workspace_path }}/CLAUDE.md"}},
            }})
        before = eval_runner._tree_hash(home)
        runner = eval_runner.ReplayRunner(conn, "qa", runtime_id="fake-rt")
        base = runner(_item(), baseline["version"])
        cand = runner(_item(), candidate["version"])
        assert base["score"] == 0.0
        assert cand["score"] == 1.0
        assert cand["hard"] is False
        assert cand["tokens"] == 15
        assert eval_runner._tree_hash(home) == before
        # The fake agent's cwd was a disposable workspace; env had no secrets.
        # It wrote agent_env.txt, which was discarded with the workspace.
        assert not (tmp_path / "done.txt").exists()
    finally:
        conn.close()


def test_replay_runner_reports_forbidden_changes(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "forbid.db"))
    try:
        script = tmp_path / "fake-touch.sh"
        script.write_text(
            "#!/bin/sh\nmkdir -p tests && echo x > tests/new.txt\n",
            encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        runtime_service.register(
            conn, id="fake-rt", display_name="Fake",
            executable_path=str(script), status="ready")
        roles.create(conn, slug="qa", display_name="QA",
                     definition={"runtime": {"id": "fake-rt"}})
        (config.AGENTS_DIR / "qa" / "instruction.md").write_text(
            "base", encoding="utf-8")
        version = role_space.snapshot(
            conn, "qa", kind="evolution", source="baseline", status="active")
        monkeypatch.setattr(runtime_service, "load_definitions", lambda: {
            "fake-rt": {
                "id": "fake-rt",
                "invocation": {"args": ["x"], "cwd": "{{ workspace_path }}"},
                "projection": {"instruction": {
                    "target": "{{ workspace_path }}/CLAUDE.md"}},
            }})
        item = _item()
        item["verifier"]["checks"] = [
            {"id": "always", "cmd": "true", "expect_exit": 0,
             "weight": 1, "hard": False}]
        result = eval_runner.ReplayRunner(
            conn, "qa", runtime_id="fake-rt")(item, version["version"])
        assert result["forbidden"] is True
        assert result["score"] == 1.0
    finally:
        conn.close()


def test_eval_worker_consumes_queued_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROLE_EVAL_DIR", tmp_path / "eval")
    monkeypatch.setattr(config, "ROLE_RSI_ENABLED", True)
    monkeypatch.setattr(config, "ROLE_RSI_EVALUATION", True)
    conn = db.connect(str(tmp_path / "worker.db"))
    try:
        runtime_service.register(
            conn, id="fake-rt", display_name="Fake",
            executable_path=_script(tmp_path), status="ready")
        roles.create(conn, slug="qa", display_name="QA",
                     definition={"runtime": {"id": "fake-rt"}})
        home = config.AGENTS_DIR / "qa"
        (home / "instruction.md").write_text(
            "<!-- ANVIL:CHARTER:BEGIN -->\nC\n<!-- ANVIL:CHARTER:END -->\n"
            "<!-- ANVIL:STRATEGY:BEGIN -->\nBASE\n<!-- ANVIL:STRATEGY:END -->\n",
            encoding="utf-8")
        (home / "PRINCIPLES.md").write_text("p\n", encoding="utf-8")
        baseline = role_space.snapshot(
            conn, "qa", kind="evolution", source="baseline", status="active")
        candidate = role_space.snapshot(
            conn, "qa", kind="evolution", source="rsi_candidate",
            status="candidate", force=True,
            file_overrides={"instruction.md": (
                "<!-- ANVIL:CHARTER:BEGIN -->\nC\n<!-- ANVIL:CHARTER:END -->\n"
                "<!-- ANVIL:STRATEGY:BEGIN -->\nCANDIDATE\n"
                "<!-- ANVIL:STRATEGY:END -->\n").encode("utf-8")})
        eval_suite.create_suite("s1")
        for i in range(8):
            item = _item()
            item.update(item_id=f"R-IN-{i}", split="held_in",
                        family=f"repo:a;mech:{i}")
            eval_suite.add_item("s1", item)
        for i in range(8):
            item = _item()
            item.update(item_id=f"R-OUT-{i}", split="held_out",
                        family=f"repo:b;mech:{i}")
            eval_suite.add_item("s1", item)
        eval_suite.freeze("s1")
        monkeypatch.setattr(runtime_service, "load_definitions", lambda: {
            "fake-rt": {
                "id": "fake-rt",
                "invocation": {"args": ["--prompt", "{{ prompt }}"],
                               "cwd": "{{ workspace_path }}"},
                "projection": {"instruction": {
                    "target": "{{ workspace_path }}/CLAUDE.md"}},
            }})
        queued = role_eval.enqueue(
            conn, role_slug="qa", suite_id="s1",
            candidate_version=candidate["version"],
            baseline_version=baseline["version"],
            request={"noise_floor": 0.02, "minimum_effect": 0.05})
        assert queued["status"] == "queued"
        worker = eval_runner.EvalWorker(str(tmp_path / "worker.db"))
        result = worker.process_once()
        assert result is not None
        assert result["verdict"] == "promote", result
        row = conn.execute("SELECT status, verdict, finished_at FROM eval_runs "
                           "WHERE id=?", (queued["id"],)).fetchone()
        assert row["status"] == "done"
        assert row["verdict"] == "promote"
        assert row["finished_at"]
    finally:
        conn.close()
