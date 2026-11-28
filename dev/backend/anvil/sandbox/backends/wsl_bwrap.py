"""Windows 严格档：默认 WSL2 发行版 + 专用用户 + bubblewrap（M3）。

- 不新建发行版；复用默认/配置的 WSL2 发行版。
- 每次作业在独立 mount/network namespace 中运行，只暴露工作区快照、job 脚本、
  ext4 scratch/out；`/mnt`、真实 home、`/root` 不进入新 root。
- 一次性 root provisioning（human consent）：安装 bubblewrap/工具链、创建
  `anvil-sbx` 用户、写 marker；Ubuntu 24.04 userns 受限时安装 AppArmor profile。
"""
from __future__ import annotations

import io
import json
import platform
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

from ... import config
from ...util import now_iso
from ...version import VERSION
from ..jobs import JobPaths, RunnerResult, container_name

SBX_USER = re.sub(r"[^a-z0-9_-]", "", (config.SANDBOX_WSL_USER or "anvil-sbx").lower()) or "anvil-sbx"
WSL_ROOT = "/var/lib/anvil"
MIB = 1024 * 1024

_state = {"at": 0.0, "ok": False, "detail": "not checked", "distro": None}


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _wsl_exe() -> str:
    return shutil.which("wsl.exe") or "wsl.exe"


def _decode(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data.replace("\x00", "")
    if not data:
        return ""
    if b"\x00" in data[:200]:
        try:
            return data.decode("utf-16-le").replace("\x00", "")
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace").replace("\x00", "")


def _wsl_raw(args: list[str], *, timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_wsl_exe(), *args], capture_output=True, timeout=timeout)


def _wsl_cmd(args: list[str], *, user: str | None = None,
             distro: str | None = None) -> list[str]:
    cmd = [_wsl_exe()]
    if distro:
        cmd += ["-d", distro]
    if user:
        cmd += ["-u", user]
    cmd += ["--", *args]
    return cmd


def _run_wsl(args: list[str], *, user: str | None = None,
             distro: str | None = None,
             timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        _wsl_cmd(args, user=user, distro=distro),
        capture_output=True, timeout=timeout)


def list_distros() -> list[str]:
    if not _is_windows():
        return []
    try:
        r = _wsl_raw(["-l", "-q"])
    except Exception:  # noqa: BLE001
        return []
    if r.returncode != 0:
        return []
    return [ln.strip() for ln in _decode(r.stdout).splitlines() if ln.strip()]


def distro() -> str | None:
    if config.SANDBOX_WSL_DISTRO:
        return config.SANDBOX_WSL_DISTRO
    distros = list_distros()
    return distros[0] if distros else None


def available(refresh: bool = False) -> tuple[bool, str]:
    now = time.time()
    if not refresh and now - _state["at"] < 60:
        return bool(_state["ok"]), str(_state["detail"])
    if not _is_windows():
        _state.update(at=now, ok=False, detail="WSL is only available on Windows")
        return False, _state["detail"]
    d = distro()
    if not d:
        _state.update(at=now, ok=False, detail="no WSL distro found")
        return False, _state["detail"]
    try:
        kernel = _decode(_run_wsl(["uname", "-r"], distro=d).stdout).strip()
        if "wsl2" not in kernel.lower() and "microsoft-standard" not in kernel.lower():
            _state.update(at=now, ok=False,
                          detail=f"WSL2 required (kernel={kernel[:80]})")
            return False, _state["detail"]
        uid = _run_wsl(["id", "-u", SBX_USER], distro=d)
        if uid.returncode != 0:
            _state.update(at=now, ok=False,
                          detail=f"sandbox user {SBX_USER} missing; run provisioning")
            return False, _state["detail"]
        probe = _run_wsl(
            ["bwrap", "--unshare-net", "--ro-bind", "/usr", "/usr",
             "--proc", "/proc", "--dev", "/dev", "/usr/bin/true"],
            user=SBX_USER, distro=d, timeout=20)
        if probe.returncode != 0:
            detail = _decode(probe.stderr).strip()[:300]
            _state.update(at=now, ok=False,
                          detail=f"bubblewrap probe failed: {detail or 'run provisioning'}")
            return False, _state["detail"]
    except Exception as exc:  # noqa: BLE001
        _state.update(at=now, ok=False, detail=f"WSL probe failed: {exc}")
        return False, _state["detail"]
    _state.update(at=now, ok=True, detail=f"{d} ready", distro=d)
    return True, _state["detail"]


def reset_for_tests() -> None:
    _state.update(at=0.0, ok=False, detail="not checked", distro=None)


def provision_script(d: str) -> str:
    marker = json.dumps(
        {"version": VERSION, "distro": d, "user": SBX_USER,
         "provisioned_at": now_iso()}, ensure_ascii=False)
    return f"""set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  bubblewrap build-essential clang lld cmake make ninja-build \
  python3 python3-dev gdb ca-certificates pkg-config git curl rsync tar
id -u {SBX_USER} >/dev/null 2>&1 || useradd -m -s /bin/bash {SBX_USER}
mkdir -p {WSL_ROOT}/jobs /etc/anvil-sandbox
chown -R {SBX_USER}:{SBX_USER} {WSL_ROOT}
if [ -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns ] && \
   [ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns)" = "1" ]; then
  cat > /etc/apparmor.d/bwrap <<'PROFILE'
abi <abi/4.0>,
include <tunables/global>
profile bwrap /usr/bin/bwrap flags=(unconfined) {{
  userns,
}}
PROFILE
  apparmor_parser -r /etc/apparmor.d/bwrap || true
fi
cat > /etc/anvil-sandbox/provisioned.json <<'JSON'
{marker}
JSON
"""


def provision() -> dict:
    if not _is_windows():
        raise RuntimeError("WSL provisioning is only available on Windows")
    d = distro()
    if not d:
        raise RuntimeError("no WSL distro found")
    r = _run_wsl(["bash", "-lc", provision_script(d)],
                 user="root", distro=d, timeout=1800)
    log = (_decode(r.stdout) + _decode(r.stderr))[-8000:]
    reset_for_tests()
    return {"ok": r.returncode == 0, "distro": d,
            "returncode": r.returncode, "log": log}


def path_to_wsl(host_path: str | Path) -> str:
    d = distro()
    if not d:
        raise RuntimeError("no WSL distro found")
    r = _run_wsl(["wslpath", "-a", "-u", str(host_path)], distro=d)
    out = _decode(r.stdout).strip().splitlines()
    if r.returncode != 0 or not out or not out[0].startswith("/"):
        raise RuntimeError(
            f"path translation failed for {host_path}: {_decode(r.stderr)[:200]}")
    return out[0]


def _signal_name(code: int | None) -> str | None:
    if code is None or code <= 128:
        return None
    try:
        import signal as signal_mod
        return signal_mod.Signals(code - 128).name
    except Exception:  # noqa: BLE001
        return f"SIG{code - 128}"


def build_bwrap_argv(*, spec: dict, src: str, job: str, scratch: str,
                     out: str) -> list[str]:
    resources = spec["resources"]
    argv = [
        "prlimit",
        f"--as={int(resources['rss_mib']) * MIB}",
        f"--nproc={int(resources['pids'])}",
        "--nofile=1024",
        f"--cpu={int(resources['timeout_seconds']) + 60}",
        "bwrap", "--die-with-parent", "--new-session",
        "--unshare-user", "--unshare-pid", "--unshare-ipc",
        "--unshare-uts", "--unshare-cgroup",
    ]
    if spec.get("network") == "none":
        argv.append("--unshare-net")
    argv += [
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--ro-bind", "/etc", "/etc",
        "--proc", "/proc", "--dev", "/dev",
        "--tmpfs", "/tmp", "--tmpfs", "/run",
        "--dir", f"/home/{SBX_USER}",
        "--setenv", "HOME", f"/home/{SBX_USER}",
        "--setenv", "TMPDIR", "/tmp",
        "--ro-bind", src, "/src",
        "--ro-bind", job, "/job",
        "--bind", scratch, "/scratch",
        "--bind", out, "/out",
        "--chdir", "/scratch",
        "/bin/bash", "/job/job.sh",
    ]
    return argv


def safe_extract_tar(data: bytes, dest: Path, *, max_bytes: int) -> None:
    """只解普通文件/目录；拒绝穿越、绝对路径、链接、设备与超限归档。"""
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            name = member.name.replace("\\", "/")
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if (not parts or name.startswith("/") or re.match(r"^[A-Za-z]:", name)
                    or any(p == ".." for p in parts)
                    or member.issym() or member.islnk()
                    or member.isdev() or member.isfifo()):
                raise ValueError(f"unsafe tar member: {member.name!r}")
            target = dest.joinpath(*parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            total += int(member.size)
            if total > max_bytes:
                raise ValueError("artifact archive exceeds limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                raise ValueError(f"cannot read tar member {member.name!r}")
            with open(target, "wb") as fh:
                shutil.copyfileobj(src, fh)


class WSLBwrapRunner:
    name = "wsl_bwrap"

    def available(self) -> tuple[bool, str]:
        return available()

    def provision(self) -> dict:
        return provision()

    def kill(self, job_id: str | None) -> None:
        d = distro()
        if not d or not job_id:
            return
        try:
            _run_wsl(["pkill", "-9", "-u", SBX_USER],
                     user="root", distro=d, timeout=20)
        except Exception:  # noqa: BLE001
            pass
        try:
            _run_wsl(["rm", "-rf", f"{WSL_ROOT}/jobs/{container_name(job_id)}"],
                     user=SBX_USER, distro=d, timeout=30)
        except Exception:  # noqa: BLE001
            pass

    def run(self, *, spec: dict, paths: JobPaths, job_id: str,
            log_path: Path) -> RunnerResult:
        ok, detail = available()
        if not ok:
            raise RuntimeError(detail)
        d = distro()
        assert d
        job_root = f"{WSL_ROOT}/jobs/{container_name(job_id)}"
        try:
            _run_wsl(["mkdir", "-p", f"{job_root}/scratch", f"{job_root}/out"],
                     user=SBX_USER, distro=d)
            src = path_to_wsl(paths.source)
            job = path_to_wsl(paths.script.parent)
            argv = build_bwrap_argv(
                spec=spec, src=src, job=job,
                scratch=f"{job_root}/scratch", out=f"{job_root}/out")
            cmd = _wsl_cmd(argv, user=SBX_USER, distro=d)
            with open(log_path, "ab") as log:
                log.write(("[anvil] " + " ".join(cmd) + "\n").encode())
                log.flush()
                proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
                timed_out = False
                try:
                    code = proc.wait(
                        timeout=int(spec["resources"]["timeout_seconds"]) + 60)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    proc.kill()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                    _run_wsl(["pkill", "-9", "-u", SBX_USER],
                             user="root", distro=d, timeout=20)
                    code = proc.poll()
                log.write(f"\n[anvil] wsl exit={code} timed_out={timed_out}\n".encode())

            # ext4 /out → 宿主 out（安全解包），供 evidence/artifact 收集。
            paths.artifacts.mkdir(parents=True, exist_ok=True)
            for child in paths.artifacts.iterdir():
                if child.is_file() or child.is_symlink():
                    child.unlink(missing_ok=True)
                elif child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
            arch = _run_wsl(
                ["tar", "-C", f"{job_root}/out", "-czf", "-", "."],
                user=SBX_USER, distro=d, timeout=120)
            if arch.returncode == 0 and arch.stdout:
                safe_extract_tar(
                    arch.stdout, paths.artifacts,
                    max_bytes=int(spec["resources"]["artifact_mib"]) * MIB)
            else:
                (paths.logs / "export-error.txt").write_text(
                    _decode(arch.stderr)[:2000], encoding="utf-8")
            return RunnerResult(
                exit_code=code, timed_out=timed_out,
                signal=_signal_name(code), backend=self.name)
        finally:
            try:
                _run_wsl(["rm", "-rf", job_root], user=SBX_USER, distro=d, timeout=30)
            except Exception:  # noqa: BLE001
                pass
