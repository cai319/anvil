"""macOS 标准档后端：Seatbelt（sandbox-exec）。

- 写：默认拒绝，白名单为工作区、角色 home、实例目录、当前 repo 的 git 公共目录。
- 读：默认允许，但对 Anvil 自身秘密、其它任务工作区/角色 home 做定向拒绝；
  当前工作区/角色 home 在拒绝之后重新放行（Seatbelt 后匹配规则生效）。
- 资源：Seatbelt 无 cgroup；内存/PIDs 由 supervisor 采样并杀树。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from ... import config
from ..ports import SandboxCapabilities, SandboxProcess, SpawnRequest
from .host import HostBackend

logger = logging.getLogger("anvil.sandbox.seatbelt")
SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def available() -> bool:
    return sys.platform == "darwin" and Path(SANDBOX_EXEC).exists()


def _q(path: str | Path) -> str:
    """Seatbelt 字符串转义；拒绝引号/换行/NUL，避免 profile 注入。"""
    text = str(Path(path).resolve(strict=False))
    if any(ch in text for ch in ('"', "\n", "\r", "\x00")):
        raise ValueError(f"unsafe sandbox path: {text!r}")
    return text.replace("\\", "\\\\")


def _sub(kind: str, path: str | Path) -> str:
    return f'({kind} (subpath "{_q(path)}"))'


def _literal(kind: str, path: str | Path) -> str:
    return f'({kind} (literal "{_q(path)}"))'


def _git_common_dir(workspace: str) -> Path | None:
    """只允许 Anvil 托管 repo（data/repos）的 git 元数据写入。"""
    try:
        r = subprocess.run(
            ["git", "-C", workspace, "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None
    p = Path(r.stdout.strip())
    if not p.is_absolute():
        p = Path(workspace) / p
    p = p.resolve(strict=False)
    try:
        p.relative_to(config.REPOS_DIR.resolve(strict=False))
    except ValueError:
        return None
    return p


def build_profile(*, workspace: str, role_home: str, instance_dir: str,
                  read_allow: list[str] | None = None) -> str:
    workspace = str(Path(workspace).resolve(strict=False))
    role_home = str(Path(role_home).resolve(strict=False))
    instance_dir = str(Path(instance_dir).resolve(strict=False))
    git_common = _git_common_dir(workspace)

    write_rules = [
        _sub("allow file-write*", workspace),
        _sub("allow file-write*", role_home),
        _sub("allow file-write*", instance_dir),
    ]
    if git_common is not None:
        write_rules.append(_sub("allow file-write*", git_common))
    write_rules += [
        _literal("allow file-write*", "/dev/null"),
        _literal("allow file-write*", "/dev/zero"),
        _literal("allow file-write*", "/dev/random"),
        _literal("allow file-write*", "/dev/urandom"),
        _literal("allow file-write*", "/dev/stdout"),
        _literal("allow file-write*", "/dev/stderr"),
        '(allow file-write* (regex #"^/dev/fd/[0-9]+$"))',
    ]

    read_denies = [
        _literal("deny file-read*", config.AUTH_TOKEN_FILE),
        _literal("deny file-read*", config.SQLITE_PATH),
        _literal("deny file-read*", config.GRAPH_DB_PATH),
        _literal("deny file-read*", config.DATA_DIR / ".credentials.json"),
        _sub("deny file-read*", config.DATA_DIR / "backups"),
        _sub("deny file-read*", config.ROLE_EVAL_DIR),
        _sub("deny file-read*", config.TASK_WORKSPACES_DIR),
        _sub("deny file-read*", config.DATA_DIR / "worktrees"),
        _sub("deny file-read*", config.AGENTS_DIR),
        _sub("deny file-read*", config.SANDBOX_INSTANCES_DIR),
    ]
    # 后匹配规则生效：把当前工作区/角色 home/实例目录重新放行。
    read_allows = [
        _sub("allow file-read*", workspace),
        _sub("allow file-read*", role_home),
        _sub("allow file-read*", instance_dir),
    ]
    for path in read_allow or []:
        try:
            read_allows.append(_sub("allow file-read*", path))
        except ValueError:
            continue

    lines = ["(version 1)", "(allow default)", "(deny file-write*)"]
    lines += write_rules
    lines += ["; ---- targeted read denies (last match wins) ----"]
    lines += read_denies
    lines += read_allows
    return "\n".join(lines) + "\n"


def _preflight(profile_text: str) -> bool:
    fd, path = tempfile.mkstemp(suffix=".sb")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(profile_text)
        r = subprocess.run(
            [SANDBOX_EXEC, "-f", path, "/usr/bin/true"],
            capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


class SeatbeltBackend(HostBackend):
    name = "seatbelt"

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            backend=self.name,
            platform="darwin",
            write_scope=True,
            read_deny=True,
            resource_caps=False,
            process_tree=True,
            detail="sandbox-exec write scope + targeted read denies; resource caps via supervisor",
        )

    def spawn_persistent(self, req: SpawnRequest) -> SandboxProcess:
        workspace = req.workspace_path or req.cwd
        role_home = str(config.AGENTS_DIR / str(req.role_slug or ""))
        instance_dir = req.instance_dir or tempfile.mkdtemp(prefix="anvil-sbx-")
        read_allow = []
        policy = req.policy
        if policy is not None:
            raw = (policy.resources or {}).get("read_allow")
            if isinstance(raw, list):
                read_allow = [str(p) for p in raw]
        try:
            profile = build_profile(
                workspace=workspace, role_home=role_home,
                instance_dir=instance_dir, read_allow=read_allow)
        except ValueError as exc:
            logger.warning("seatbelt profile build failed; falling back to host: %s", exc)
            return HostBackend(detail="seatbelt profile error").spawn_persistent(req)
        if not _preflight(profile):
            logger.warning("seatbelt preflight failed; falling back to host")
            return HostBackend(detail="seatbelt preflight failed").spawn_persistent(req)

        profile_path = Path(instance_dir) / "profile.sb"
        try:
            profile_path.write_text(profile, encoding="utf-8")
            profile_path.chmod(0o600)
        except OSError:
            profile_path = Path(tempfile.mkstemp(suffix=".sb")[1])
            profile_path.write_text(profile, encoding="utf-8")

        kwargs: dict[str, Any] = dict(
            cwd=req.cwd, env=req.env, stdin=req.stdin,
            stdout=req.stdout, stderr=req.stderr,
        )
        proc = subprocess.Popen(
            [SANDBOX_EXEC, "-f", str(profile_path), req.executable, *req.args],
            start_new_session=True, **kwargs)
        return SandboxProcess(
            popen=proc, backend=self.name,
            instance_id=req.instance_id, native_handle=f"pg:{proc.pid}")
