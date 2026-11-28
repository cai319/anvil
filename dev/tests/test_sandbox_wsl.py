"""Windows 严格档（WSL2 + bwrap）：命令构建、路径翻译、tar 安全解包（M3）。"""
from __future__ import annotations

import io
import tarfile

import pytest

from anvil.sandbox import jobs
from anvil.sandbox.backends import strict, wsl_bwrap


def _tar(entries) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for entry in entries:
            name, data = entry[0], entry[1]
            kind = entry[2] if len(entry) > 2 else "file"
            info = tarfile.TarInfo(name)
            if kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = data.decode() if isinstance(data, bytes) else data
                info.size = 0
                tar.addfile(info)
                continue
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _spec(network="none") -> dict:
    return jobs.parse_spec({
        "network": network,
        "phases": {"run": [{"name": "poc", "cmd": "true"}]},
    })


def test_bwrap_argv_isolates_mounts_and_network():
    argv = wsl_bwrap.build_bwrap_argv(
        spec=_spec("none"), src="/mnt/d/src", job="/mnt/d/job",
        scratch="/var/lib/anvil/jobs/j/scratch", out="/var/lib/anvil/jobs/j/out")
    assert "--unshare-net" in argv
    assert "--unshare-user" in argv and "--unshare-pid" in argv
    assert "/mnt/d/src" in argv and "/src" in argv
    assert "/var/lib/anvil/jobs/j/scratch" in argv
    # 不把 /mnt 或 home 整体挂进新 root
    assert not any(a == "/mnt" and b == "/mnt" for a, b in zip(argv, argv[1:]))
    assert not any(a == "/home" and b == "/home" for a, b in zip(argv, argv[1:]))
    assert argv[-2:] == ["/bin/bash", "/job/job.sh"]


def test_bwrap_argv_relaxed_network_omits_unshare():
    argv = wsl_bwrap.build_bwrap_argv(
        spec=_spec("bridge"), src="/mnt/d/src", job="/mnt/d/job",
        scratch="/s", out="/o")
    assert "--unshare-net" not in argv


def test_safe_extract_tar_accepts_regular_files(tmp_path):
    # 实际导出来自 `tar -C /out .`，成员形如 ./a.txt，不带 /out 前缀。
    data = _tar([("a.txt", b"ok"), ("sub/b.txt", b"two")])
    wsl_bwrap.safe_extract_tar(data, tmp_path / "out", max_bytes=100)
    assert (tmp_path / "out" / "a.txt").read_bytes() == b"ok"
    assert (tmp_path / "out" / "sub" / "b.txt").read_bytes() == b"two"


@pytest.mark.parametrize("entries", [
    [("../escape", b"x")],
    [("/etc/passwd", b"x")],
    [("link", b"/etc/passwd", "symlink")],
])
def test_safe_extract_tar_rejects_unsafe_members(tmp_path, entries):
    with pytest.raises(ValueError):
        wsl_bwrap.safe_extract_tar(
            _tar(entries), tmp_path / "out", max_bytes=100)


def test_safe_extract_tar_rejects_oversize(tmp_path):
    with pytest.raises(ValueError):
        wsl_bwrap.safe_extract_tar(
            _tar([("big", b"12345")]), tmp_path / "out", max_bytes=2)


def test_path_to_wsl(monkeypatch):
    class Result:
        returncode = 0
        stdout = b"/mnt/d/work\n"
        stderr = b""

    monkeypatch.setattr(wsl_bwrap, "_is_windows", lambda: True)
    monkeypatch.setattr(wsl_bwrap, "distro", lambda: "Ubuntu")
    monkeypatch.setattr(wsl_bwrap, "_run_wsl", lambda *a, **k: Result())
    assert wsl_bwrap.path_to_wsl("D:\\work") == "/mnt/d/work"


def test_available_non_windows(monkeypatch):
    wsl_bwrap.reset_for_tests()
    monkeypatch.setattr(wsl_bwrap, "_is_windows", lambda: False)
    ok, detail = wsl_bwrap.available(refresh=True)
    assert ok is False
    assert "Windows" in detail


def test_provision_script_and_windows_only(monkeypatch):
    script = wsl_bwrap.provision_script("Ubuntu")
    assert "bubblewrap" in script
    assert "anvil-sbx" in script
    assert "provisioned.json" in script
    assert "apparmor" in script
    monkeypatch.setattr(wsl_bwrap, "_is_windows", lambda: False)
    with pytest.raises(RuntimeError):
        wsl_bwrap.provision()


def test_select_runner_darwin_uses_docker(monkeypatch):
    monkeypatch.setattr(strict.platform, "system", lambda: "Darwin")
    runner, _ok, _detail = strict.select_runner()
    assert runner.name == "docker"
