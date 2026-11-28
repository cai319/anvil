"""Paired replay runner isolation helpers (v1.50 P2/P3).

`ReplayRunner` executes one suite item against a materialized role-space
version in a disposable shadow home/workspace. It never mounts the real role
home, never provides an Anvil endpoint/token, and runs the item's verifier
itself (the role cannot see verifier commands).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import uuid
import shutil
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from .. import config
from ..db import open_conn
from ..sandbox import jobs as sandbox_jobs
from ..sandbox.backends import strict as strict_backend
from ..util import now_iso
from . import role_eval, role_space, roles as roles_service, runtime as runtime_service

logger = logging.getLogger(__name__)

_IGNORE = shutil.ignore_patterns("logs", "scratch", ".anvil", "CLAUDE.md")
_TREE_IGNORE = {".git", ".anvil", "node_modules", "__pycache__"}

REPLAY_PROTOCOL = """## 离线回放协议
- 这是匿名离线回放环境；没有 Anvil 服务、任务租约或人工评审。
- 只在当前 workspace 内完成任务，不要等待人工确认，不要访问网络服务。
- 完成后直接退出；不要编造验收结果。"""


class ReplayError(RuntimeError):
    """Replay setup/invocation failed before a comparable score exists."""


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return "sha256:missing"
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _tree_map(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        if any(part in _TREE_IGNORE for part in rel.split("/")):
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


@contextmanager
def shadow_role_home(role_slug: str, *, overlay=None):
    """Yield (shadow_home, env) with no Anvil endpoint/token; real home is hashed.

    Writes inside the shadow home are discarded when the context exits. Any
    change to the real role home aborts the evaluation.
    """
    source = config.AGENTS_DIR / role_slug
    before = _tree_hash(source)
    tmp = tempfile.TemporaryDirectory(prefix=f"anvil-eval-{role_slug}-")
    shadow = Path(tmp.name) / "home"
    try:
        if source.is_dir():
            shutil.copytree(source, shadow, ignore=_IGNORE)
        else:
            shadow.mkdir(parents=True, exist_ok=True)
        if overlay is not None:
            overlay(shadow)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ANVIL_")}
        env["ANVIL_EVAL_MODE"] = "1"
        yield shadow, env
    finally:
        tmp.cleanup()
        after = _tree_hash(source)
        if after != before:
            raise RuntimeError(
                "real role home changed during evaluation; aborting result")


def _read_text(path: Path, limit: int = 8000) -> str:
    try:
        return path.read_text(encoding="utf-8")[:limit]
    except OSError:
        return ""


def _prompt_from_item(item: dict) -> str:
    prompt = item.get("prompt") or {}
    parts = [
        str(prompt.get("task_title") or "").strip(),
        str(prompt.get("task_description") or "").strip(),
        str(prompt.get("step_instructions") or "").strip(),
    ]
    acceptance = prompt.get("acceptance") or []
    if acceptance:
        parts.append("验收标准：\n" + "\n".join(f"- {x}" for x in acceptance))
    return "\n\n".join(x for x in parts if x)


def _git_repo(value: str) -> Path | None:
    raw = Path(os.path.expanduser(str(value or "")))
    if raw.is_dir():
        return raw
    candidate = config.REPOS_DIR / str(value or "")
    return candidate if candidate.is_dir() else None


def _prepare_workspace(item: dict, workspace: Path) -> None:
    spec = item.get("workspace") or {}
    kind = str(spec.get("kind") or "temp")
    if kind not in ("temp", "none"):
        if kind != "git_snapshot":
            raise ReplayError(f"unsupported workspace kind: {kind}")
        source = spec.get("path") or spec.get("repo")
        repo = _git_repo(str(source)) if source else None
        if repo is None:
            raise ReplayError(f"workspace repo not found: {source}")
        if workspace.exists():
            shutil.rmtree(workspace)
        proc = subprocess.run(
            ["git", "clone", "--no-hardlinks", "--quiet", str(repo), str(workspace)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300)
        if proc.returncode != 0:
            raise ReplayError(f"git clone failed: {proc.stderr[-500:]}")
        commit = str(spec.get("commit") or "").strip()
        if commit:
            proc = subprocess.run(
                ["git", "-C", str(workspace), "checkout", "--quiet", "--detach", commit],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=120)
            if proc.returncode != 0:
                raise ReplayError(f"git checkout failed: {proc.stderr[-500:]}")
    workspace.mkdir(parents=True, exist_ok=True)
    setup = spec.get("setup") or []
    if isinstance(setup, str):
        setup = [setup]
    for cmd in setup:
        proc = subprocess.run(
            str(cmd), shell=True, cwd=workspace, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
            env={k: v for k, v in os.environ.items() if not k.startswith("ANVIL_")})
        if proc.returncode != 0:
            raise ReplayError(f"workspace setup failed: {proc.stderr[-500:]}")


def _sum_usage(usage: dict) -> int:
    total = 0
    for key in ("input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens"):
        try:
            total += int(usage.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _usage_tokens(text: str) -> int:
    total = 0
    structured = False
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("type") == "result" \
                and isinstance(payload.get("usage"), dict):
            structured = True
            total += _sum_usage(payload["usage"])
    if structured:
        return total
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("[result] usage="):
            continue
        try:
            payload = json.loads(line[len("[result] usage="):])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            total += _sum_usage(payload)
    return total


def _project_instruction(home: Path, workspace: Path, item: dict, *,
                         definition: dict, effective: dict) -> None:
    from ..graph import render
    prompt = item.get("prompt") or {}
    target_tpl = (((definition.get("projection") or {}).get("instruction") or {})
                  .get("target") or "{{ workspace_path }}/CLAUDE.md")
    target = Path(render.render_template(str(target_tpl),
                                         {"workspace_path": str(workspace)}))
    cards = []
    for path in sorted((home / "notes/memory/cards").rglob("*.md")):
        cards.append(_read_text(path, 1200))
    projection = effective.get("projection") or {}
    render.project_instruction(
        target, _read_text(home / "instruction.md"),
        {"title": str(prompt.get("task_title") or "replay"),
         "description": str(prompt.get("task_description") or "")},
        system_prompt=str(projection.get("system_prompt") or ""),
        responsibilities=list(projection.get("responsibilities") or []),
        memory=_read_text(home / "PRINCIPLES.md", 3000),
        notes=[x for x in cards if x],
        prompt=_prompt_from_item(item),
        protocol=REPLAY_PROTOCOL,
    )


class ReplayRunner:
    """Callable `(item, version) -> {score, tokens, hard, forbidden}`.

    Each call copies the real role home into a temp shadow, overlays the
    requested version's manifest, renders an offline instruction projection,
    runs the role's runtime once in a temp workspace, and executes the hidden
    deterministic verifier.
    """

    def __init__(self, conn, role_slug: str, *, runtime_id: str | None = None,
                 timeout: float | None = None,
                 env_extra: dict[str, str] | None = None):
        self.conn = conn
        self.role_slug = role_slug
        self.runtime_id = runtime_id
        self.timeout = timeout
        self.env_extra = env_extra or {}

    def __call__(self, item: dict, version: int) -> dict:
        return self.run(item, version)

    def run(self, item: dict, version: int) -> dict:
        with shadow_role_home(
                self.role_slug,
                overlay=lambda home: role_space.materialize_version(
                    self.conn, self.role_slug, version, home)) as (home, env):
            workspace = Path(tempfile.mkdtemp(
                prefix=f"anvil-eval-work-{self.role_slug}-"))
            try:
                return self._run_in_shadow(item, version, home, workspace, env)
            finally:
                shutil.rmtree(workspace, ignore_errors=True)

    def _run_in_shadow(self, item: dict, version: int, home: Path,
                       workspace: Path, env: dict) -> dict:
        verifier = item.get("verifier") or {}
        token_cap = int(verifier.get("token_cap") or 60000)
        try:
            _prepare_workspace(item, workspace)
            before = _tree_map(workspace)
            effective = roles_service.effective_config(
                self.conn, self.role_slug) or {}
            definition = runtime_service.load_definitions().get(
                self._runtime_id(effective)) or {}
            if not definition.get("invocation"):
                raise ReplayError("runtime invocation is not configured")
            _project_instruction(home, workspace, item,
                                 definition=definition, effective=effective)
            text, tokens, exit_code = self._invoke(
                definition, effective, workspace, item, env)
            after = _tree_map(workspace)
            forbidden = self._forbidden_touched(
                before, after, verifier.get("forbidden_changes") or [])
            score, hard, failures = self._verify(item, workspace)
            if tokens > token_cap:
                hard = True
                failures.append(f"token cap exceeded: {tokens}>{token_cap}")
            return {
                "score": round(float(score), 4),
                "tokens": int(tokens),
                "hard": bool(hard),
                "forbidden": bool(forbidden),
                "version": version,
                "exit_code": int(exit_code),
                "failures": failures,
            }
        except (ReplayError, OSError, subprocess.SubprocessError,
                ValueError, KeyError) as exc:
            return {
                "score": 0.0, "tokens": 0, "hard": False,
                "forbidden": False, "version": version,
                "error": str(exc)[:1000],
            }

    def _runtime_id(self, effective: dict) -> str:
        rt = (effective.get("projection") or {}).get("runtime")
        runtime_id = rt.get("id") if isinstance(rt, dict) else rt
        return str(self.runtime_id or runtime_id or config.DEFAULT_RUNTIME)

    def _invoke(self, definition: dict, effective: dict, workspace: Path,
                item: dict, base_env: dict) -> tuple[str, int, int]:
        from ..graph import render
        runtime_id = self._runtime_id(effective)
        row = runtime_service.get(self.conn, runtime_id) or {}
        executable = (
            row.get("executable_path") or definition.get("executable")
            or definition.get("executable_path") or runtime_id)
        executable = runtime_service.effective_executable(executable) or executable
        runtime_cfg = (effective.get("projection") or {}).get("runtime") or {}
        permission_mode = (
            runtime_cfg.get("permission_mode")
            if isinstance(runtime_cfg, dict) else None
        ) or config.DEFAULT_PERMISSION_MODE
        ctx = {
            "permission_mode": permission_mode,
            "prompt": _prompt_from_item(item),
            "workspace_path": str(workspace),
            "task_id": "replay",
            "agent_slug": self.role_slug,
            "session_token": "",
        }
        invocation = definition.get("invocation") or {}
        args = [render.render_template(str(a), ctx)
                for a in invocation.get("args", [])]
        timeout = float(self.timeout or invocation.get("timeout_seconds") or 900)
        timeout = min(timeout, float(
            (item.get("verifier") or {}).get("timeout_seconds") or timeout))
        env = {k: v for k, v in base_env.items()}
        for key, value in (invocation.get("env") or {}).items():
            if str(key).startswith("ANVIL_"):
                continue  # never expose endpoint/token/eval key to the role
            env[str(key)] = render.render_template(str(value), ctx)
        env.update(self.env_extra)
        env["ANVIL_EVAL_MODE"] = "1"
        try:
            proc = subprocess.run(
                [executable, *args], cwd=workspace, env=env,
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            text = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            return text, _usage_tokens(text), -9
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        return text, _usage_tokens(text), int(proc.returncode)

    @staticmethod
    def _forbidden_touched(before: dict[str, str], after: dict[str, str],
                           patterns: list[str]) -> bool:
        if not patterns:
            return False
        touched = {rel for rel in after if before.get(rel) != after[rel]}
        touched |= {rel for rel in before if rel not in after}
        return any(fnmatch.fnmatch(rel, pattern)
                   for rel in touched for pattern in patterns)

    @staticmethod
    def _host_verify(checks: list[dict], workspace: Path,
                     timeout: float) -> tuple[float, bool, list[str]]:
        total = 0.0
        passed = 0.0
        hard_failure = False
        failures: list[str] = []
        for check in checks:
            weight = float(check.get("weight") or 1)
            total += weight
            try:
                proc = subprocess.run(
                    str(check.get("cmd") or "false"), shell=True, cwd=workspace,
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=timeout,
                    env={k: v for k, v in os.environ.items()
                         if not k.startswith("ANVIL_")})
                ok = proc.returncode == int(check.get("expect_exit") or 0)
            except (OSError, subprocess.SubprocessError):
                ok = False
            if ok:
                passed += weight
            else:
                failures.append(str(check.get("id") or "check"))
                if check.get("hard"):
                    hard_failure = True
        return (passed / total if total else 0.0), hard_failure, failures

    @classmethod
    def _verify(cls, item: dict, workspace: Path) -> tuple[float, bool, list[str]]:
        verifier = item.get("verifier") or {}
        checks = verifier.get("checks") or []
        timeout = float(verifier.get("timeout_seconds") or 600)
        if not checks:
            return 0.0, False, []
        if not config.SANDBOX_ENABLED or config.SANDBOX_MODE == "host-unsafe":
            return cls._host_verify(checks, workspace, timeout)
        runner, ok, detail = strict_backend.select_runner()
        if not ok:
            return 0.0, True, [f"strict sandbox unavailable: {detail}"]
        try:
            spec = sandbox_jobs.parse_spec({
                "network": "none",
                "resources": {"timeout_seconds": max(1, int(timeout))},
                "phases": {"run": [
                    {
                        "name": f"check-{i}",
                        "cmd": str(check.get("cmd") or "false"),
                        "timeout_seconds": max(1, int(timeout)),
                    }
                    for i, check in enumerate(checks)
                ]},
            })
            execution = sandbox_jobs.execute_job(
                spec, "rsi_" + uuid.uuid4().hex, runner,
                source_dir=workspace)
        except Exception as exc:  # noqa: BLE001 —— 基础设施失败不误评分
            return 0.0, True, [f"strict verifier failed: {exc}"]
        phases = {phase["name"]: phase for phase in execution.phases}
        total = 0.0
        passed = 0.0
        hard_failure = False
        failures: list[str] = []
        for i, check in enumerate(checks):
            weight = float(check.get("weight") or 1)
            total += weight
            phase = phases.get(f"check-{i}")
            expected = int(check.get("expect_exit") or 0)
            ok = phase is not None and phase.get("exit_code") == expected
            if ok:
                passed += weight
            else:
                failures.append(str(check.get("id") or "check"))
                if check.get("hard"):
                    hard_failure = True
        if execution.failure_reason == "sandbox_timeout":
            hard_failure = True
            failures.append("strict verifier timed out")
        return (passed / total if total else 0.0), hard_failure, failures


class EvalWorker:
    """Single background worker consuming queued `eval_runs` rows.

    Runs only when `ROLE_RSI_EVALUATION` is enabled. Concurrency defaults to 1
    so replay does not compete with role daemons.
    """

    def __init__(self, db_path: str, *, poll_seconds: float = 5.0):
        self.db_path = db_path
        self.poll_seconds = max(1.0, float(poll_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="anvil-eval-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.process_once()
            except Exception:  # noqa: BLE001 —— worker 不因单次失败退出
                logger.exception("eval worker iteration failed")

    def process_once(self) -> dict | None:
        if not bool(getattr(config, "ROLE_RSI_ENABLED", False)) or not bool(
                getattr(config, "ROLE_RSI_EVALUATION", False)):
            return None
        conn = open_conn(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM eval_runs WHERE status='queued' "
                "ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            claimed = conn.execute(
                "UPDATE eval_runs SET status='running', started_at=? "
                "WHERE id=? AND status='queued'", (now_iso(), row["id"]))
            conn.commit()
            if claimed.rowcount == 0:
                return None
            try:
                options = json.loads(row["request_json"] or "{}")
            except json.JSONDecodeError:
                options = {}
            runner = ReplayRunner(
                conn, str(row["role_slug"]),
                runtime_id=options.get("runtime_id"),
                env_extra=options.get("env_extra"))
            try:
                result = role_eval.evaluate(
                    conn, role_slug=str(row["role_slug"]),
                    suite_id=str(row["suite_id"]),
                    candidate_version=int(row["candidate_version"]),
                    baseline_version=int(row["baseline_version"]),
                    candidate_id=row["candidate_id"], replay_fn=runner,
                    noise_floor=float(options.get("noise_floor", 0.10)),
                    minimum_effect=float(options.get("minimum_effect", 0.05)),
                    seed=int(options.get("seed", 7)),
                    runtime_id=options.get("runtime_id"),
                    model_id=options.get("model_id"),
                    baseline_working_set_hash=options.get(
                        "baseline_working_set_hash"),
                    candidate_working_set_hash=options.get(
                        "candidate_working_set_hash"),
                    run_id=str(row["id"]), request_json=options)
                return result
            except Exception as exc:  # noqa: BLE001 —— 失败落到 eval_runs.error
                logger.exception("eval run %s failed", row["id"])
                conn.execute(
                    "UPDATE eval_runs SET status='failed', verdict='inconclusive', "
                    "error=?, finished_at=? WHERE id=?",
                    (str(exc)[:2000], now_iso(), row["id"]))
                conn.commit()
                return {"run_id": str(row["id"]), "verdict": "inconclusive",
                        "error": str(exc)}
        finally:
            conn.close()


def paired_eval(conn, *, role_slug: str, suite_id: str,
                candidate_version: int, baseline_version: int,
                replay_fn, **kw) -> dict:
    """Run paired replay through role_eval; callers provide the item runner."""
    return role_eval.evaluate(
        conn, role_slug=role_slug, suite_id=suite_id,
        candidate_version=candidate_version, baseline_version=baseline_version,
        replay_fn=replay_fn, **kw)
