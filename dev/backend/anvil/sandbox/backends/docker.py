"""Docker/Podman 严格档 runner（M2a）。

作业阶段：只读源码快照 + 可写 scratch/out、默认 `--network none`、容器级资源硬限。
镜像首次使用本地构建并缓存；Docker/Podman 都不可用时严格档不可用。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from ... import config
from ..jobs import JobPaths, RunnerResult, container_name

_state = {"at": 0.0, "ok": False, "detail": "not checked"}
_IMAGE_BUILD_TIMEOUT = 1800


def _cli() -> str | None:
    for name in ("docker", "podman"):
        if shutil.which(name):
            return name
    return None


def available(refresh: bool = False) -> tuple[bool, str]:
    now = time.time()
    if not refresh and now - _state["at"] < 60:
        return bool(_state["ok"]), str(_state["detail"])
    cli = _cli()
    if not cli:
        _state.update(at=now, ok=False, detail="docker/podman not found")
        return False, _state["detail"]
    try:
        r = subprocess.run(
            [cli, "info"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10)
    except Exception as exc:  # noqa: BLE001
        _state.update(at=now, ok=False, detail=f"{cli} info failed: {exc}")
        return False, _state["detail"]
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()[:300]
        _state.update(at=now, ok=False, detail=f"{cli} daemon unavailable: {detail}")
        return False, _state["detail"]
    _state.update(at=now, ok=True, detail=f"{cli} ready")
    return True, _state["detail"]


def reset_for_tests() -> None:
    _state.update(at=0.0, ok=False, detail="not checked")


def _image_present(cli: str, image: str) -> bool:
    try:
        r = subprocess.run(
            [cli, "image", "inspect", image], capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _dockerfile() -> Path:
    return Path(__file__).resolve().parent.parent / "images" / "toolchain.Dockerfile"


def ensure_image(cli: str, image: str, log) -> None:
    if _image_present(cli, image):
        return
    if os.environ.get("ANVIL_SANDBOX_SKIP_IMAGE_BUILD") == "1":
        raise RuntimeError(f"image {image} missing and image build is disabled")
    dockerfile = _dockerfile()
    if not dockerfile.exists():
        raise RuntimeError(f"toolchain Dockerfile missing: {dockerfile}")
    log.write(f"[anvil] building sandbox image {image}\n".encode())
    log.flush()
    r = subprocess.run(
        [cli, "build", "-f", str(dockerfile), "-t", image, str(dockerfile.parent)],
        stdout=log, stderr=subprocess.STDOUT, timeout=_IMAGE_BUILD_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(f"sandbox image build failed (exit {r.returncode})")


def _signal_name(code: int | None) -> str | None:
    if code is None or code <= 128:
        return None
    try:
        import signal as signal_mod
        return signal_mod.Signals(code - 128).name
    except Exception:  # noqa: BLE001
        return f"SIG{code - 128}"


def _run_cmd(cli: str, spec: dict, paths: JobPaths, image: str,
             name: str) -> list[str]:
    resources = spec["resources"]
    cmd = [
        cli, "run", "--rm", "--name", name,
        "--network", "none" if spec.get("network") == "none" else "bridge",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", str(int(resources["pids"])),
        "--memory", f"{int(resources['rss_mib'])}m",
        "--memory-swap", f"{int(resources['rss_mib'])}m",
        "--cpus", "2",
        "--ulimit", "nofile=1024:1024",
        "--user", "1000:1000",
        "--workdir", "/scratch",
        "--mount", f"type=bind,src={paths.source},dst=/src,readonly",
        "--mount", f"type=bind,src={paths.script.parent},dst=/job,readonly",
        "--mount", f"type=bind,src={paths.scratch},dst=/scratch",
        "--mount", f"type=bind,src={paths.artifacts},dst=/out",
        image, "/bin/bash", "/job/job.sh",
    ]
    return cmd


class DockerRunner:
    name = "docker"

    def available(self) -> tuple[bool, str]:
        return available()

    def run(self, *, spec: dict, paths: JobPaths, job_id: str,
            log_path: Path) -> RunnerResult:
        cli = _cli()
        if not cli:
            raise RuntimeError("docker/podman not found")
        ok, detail = available()
        if not ok:
            raise RuntimeError(detail)
        image = str(spec.get("image") or config.SANDBOX_IMAGE)
        name = container_name(job_id)
        with open(log_path, "ab") as log:
            ensure_image(cli, image, log)
            # 清掉同名残留（上次控制器崩溃可能留下）。
            subprocess.run([cli, "rm", "-f", name], capture_output=True, timeout=20)
            cmd = _run_cmd(cli, spec, paths, image, name)
            log.write(("[anvil] " + " ".join(cmd) + "\n").encode())
            log.flush()
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
            timed_out = False
            try:
                code = proc.wait(timeout=int(spec["resources"]["timeout_seconds"]) + 60)
            except subprocess.TimeoutExpired:
                timed_out = True
                subprocess.run([cli, "kill", name], capture_output=True, timeout=20)
                try:
                    code = proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    code = proc.poll()
            finally:
                subprocess.run([cli, "rm", "-f", name], capture_output=True, timeout=20)
        return RunnerResult(
            exit_code=code, timed_out=timed_out, signal=_signal_name(code),
            image=image, backend=self.name)


def kill_container(native_job_id: str | None) -> None:
    if not native_job_id:
        return
    cli = _cli()
    if not cli:
        return
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", str(native_job_id))[:96]
    try:
        subprocess.run([cli, "kill", name], capture_output=True, timeout=20)
    except Exception:  # noqa: BLE001
        pass
    try:
        subprocess.run([cli, "rm", "-f", name], capture_output=True, timeout=20)
    except Exception:  # noqa: BLE001
        pass
