"""严格档作业 worker（M2a，单并发）。

从 sandbox_jobs 领取 queued 作业，执行 build → run → 取证，落 phases/artifacts/
evidence。同一时刻只跑一个作业；重启时由 reconcile 把未完成作业标 failed。
"""
from __future__ import annotations

import logging
import threading

from .. import config
from ..db import open_conn
from ..sandbox.backends import strict as strict_backend
from ..sandbox.backends.docker import kill_container
from ..sandbox.jobs import execute_job
from . import sandbox as sandbox_service

logger = logging.getLogger("anvil.sandbox.worker")


class SandboxJobWorker:
    def __init__(self, db_path: str, runner=None) -> None:
        self.db_path = db_path
        self.runner = runner or strict_backend.select_runner()[0]
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.current_job_id: str | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="anvil-sandbox-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.current_job_id:
            conn = None
            try:
                conn = open_conn(self.db_path)
                job = sandbox_service.get_job(conn, self.current_job_id) or {}
                kill_container(job.get("native_job_id"))
            except Exception:  # noqa: BLE001
                pass
            finally:
                if conn is not None:
                    conn.close()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)

    def run_once(self, conn=None) -> bool:
        """执行一个排队作业；测试可传入连接。返回是否执行了作业。"""
        own = conn is None
        conn = conn or open_conn(self.db_path)
        try:
            job = sandbox_service.claim_next_job(conn)
            if job is None:
                return False
            self.current_job_id = job["id"]
            try:
                execution = execute_job(job.get("spec") or {}, job["id"], self.runner)
                sandbox_service.record_execution(conn, job["id"], execution)
            except Exception as exc:  # noqa: BLE001 —— 失败必须落库，不能卡 running
                logger.exception("sandbox job %s failed", job["id"])
                sandbox_service.fail_job(
                    conn, job["id"], f"sandbox_error: {exc}")
            finally:
                self.current_job_id = None
            return True
        finally:
            if own:
                conn.close()

    def _loop(self) -> None:
        while not self._stop.wait(config.SANDBOX_JOB_POLL_SECONDS):
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 —— worker 线程不能崩
                logger.exception("sandbox worker tick failed")
