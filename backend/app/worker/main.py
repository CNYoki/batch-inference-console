"""Worker 进程入口：领取任务、并行执行、心跳与租约回收。

启动：  python -m app.worker.main
可以起多个进程/多台机器，通过 Redis 队列自动分摊任务。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import time
import uuid

from ..config import settings
from ..db import engine, session_scope
from ..services.queue import queue
from ..services.retention import sweep
from ..services.storage import backfill_result_sizes
from .runner import JobRunner

log = logging.getLogger("worker")

IDLE_SLEEP = 1.0        # 队列为空时的轮询间隔
HEARTBEAT_SECONDS = 10.0
REAP_SECONDS = 30.0
# 心跳停了这么久就从看板上删掉。要明显长于看板的「心跳超时」判定（45s），
# 让崩溃的 worker 先以超时状态露个面，方便发现问题
DEAD_WORKER_SECONDS = 600.0
RETENTION_SECONDS = 3600.0   # 保留期清理的扫描间隔


class Worker:
    def __init__(self) -> None:
        self.id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.max_jobs = settings.worker_max_concurrent_jobs
        self.running: dict[str, asyncio.Task] = {}
        self._shutdown = asyncio.Event()
        # 收到退出信号后进入收尾状态：不再领新任务，但把手上的跑完
        self._draining = False

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if not await queue.ping():
            raise SystemExit(f"无法连接 Redis: {settings.redis_url}")

        log.info("worker %s 启动，最大并行任务数 %d", self.id, self.max_jobs)

        # 补齐历史任务的结果体积（幂等，补完就不会再扫到）
        try:
            async with session_scope() as db:
                await backfill_result_sizes(db)
        except Exception as exc:  # noqa: BLE001 — 补数据失败不该挡住 worker 启动
            log.warning("补齐历史结果体积失败: %s", exc)
        # 心跳单独拿出来：收尾期间必须继续上报，否则看板会把一个
        # 正在正常跑任务的 worker 显示成「心跳超时」
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        background = [
            asyncio.create_task(self._reaper_loop()),
            asyncio.create_task(self._retention_loop()),
        ]
        try:
            await self._claim_loop()
        finally:
            # 这两个循环会产生新工作，退出时立刻停掉
            for t in background:
                t.cancel()
            self._draining = True
            await self._drain()
            # 手上的任务都跑完了，这时才停心跳并注销自己
            heartbeat.cancel()
            await queue.drop_worker(self.id)
            await queue.close()
            await engine.dispose()
            log.info("worker %s 已退出", self.id)

    def request_shutdown(self) -> None:
        if not self._shutdown.is_set():
            log.info("收到退出信号，停止领取新任务，等待在跑任务收尾……")
            self._shutdown.set()
            self._draining = True

    # ------------------------------------------------------------------ #
    async def _claim_loop(self) -> None:
        while not self._shutdown.is_set():
            if len(self.running) >= self.max_jobs:
                await self._wait_any(timeout=IDLE_SLEEP)
                continue
            try:
                job_id = await queue.claim(self.id)
            except Exception as exc:  # noqa: BLE001 — Redis 抖动时退避重试
                log.warning("领取任务失败: %s", exc)
                await asyncio.sleep(2.0)
                continue

            if job_id is None:
                await asyncio.sleep(IDLE_SLEEP)
                continue

            log.info("领取任务 %s", job_id)
            task = asyncio.create_task(self._run_job(job_id))
            self.running[job_id] = task

    async def _run_job(self, job_id: str) -> None:
        try:
            await JobRunner(job_id, self.id).run()
        except Exception:
            log.exception("任务 %s 执行失败", job_id)
        finally:
            self.running.pop(job_id, None)
            try:
                await queue.release(job_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("释放任务 %s 租约失败: %s", job_id, exc)

    async def _wait_any(self, timeout: float) -> None:
        if not self.running:
            await asyncio.sleep(timeout)
            return
        await asyncio.wait(set(self.running.values()), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)

    async def _drain(self) -> None:
        if not self.running:
            return
        log.info("等待 %d 个在跑任务结束……", len(self.running))
        await asyncio.gather(*self.running.values(), return_exceptions=True)

    # ------------------------------------------------------------------ #
    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await queue.heartbeat(
                    self.id,
                    json.dumps({
                        "ts": time.time(),
                        "jobs": list(self.running.keys()),
                        "capacity": self.max_jobs,
                        "draining": self._draining,
                        # 用于发现「多个部署共用一个队列但各自的数据目录不同」——
                        # 那种情况下任务会被错误的 worker 领走，输入文件自然找不到
                        "data_dir": str(settings.data_dir),
                    }),
                )
                for job_id in list(self.running.keys()):
                    await queue.renew_lease(job_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("心跳失败: %s", exc)
            await asyncio.sleep(HEARTBEAT_SECONDS)

    async def _retention_loop(self) -> None:
        """按保留期清理终态任务的文件。

        用 Redis 锁保证多 worker 时同一轮只有一个在扫；锁的 TTL 略小于扫描间隔，
        这样即使持有者崩溃，下一轮也能正常接手。
        """
        # 启动后先等一会儿，避开进程刚起来时的冷启动高峰
        await asyncio.sleep(60)
        while True:
            try:
                if await queue.acquire_lock("retention", ttl=int(RETENTION_SECONDS) - 60):
                    async with session_scope() as db:
                        result = await sweep(db)
                    if result.get("purged"):
                        log.info(
                            "保留期清理完成：%d 个任务，释放 %.1f MB",
                            result["purged"], result["freed_bytes"] / 1024 / 1024,
                        )
            except Exception as exc:  # noqa: BLE001 — 清理失败不能影响任务执行
                log.warning("保留期清理失败: %s", exc)
            await asyncio.sleep(RETENTION_SECONDS)

    async def _reaper_loop(self) -> None:
        """回收租约过期的任务，并清掉心跳早已停止的 worker 记录（持有者进程崩溃/被杀）。"""
        while True:
            await asyncio.sleep(REAP_SECONDS)
            try:
                revived = await queue.reap_expired()
                if revived:
                    log.warning("回收超时任务并重新入队: %s", revived)
            except Exception as exc:  # noqa: BLE001
                log.warning("租约回收失败: %s", exc)
            try:
                purged = await queue.purge_dead_workers(DEAD_WORKER_SECONDS)
                if purged:
                    log.info("清理已失联的 worker 记录: %s", purged)
            except Exception as exc:  # noqa: BLE001
                log.warning("清理失联 worker 失败: %s", exc)


def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    worker = Worker()

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # Windows 的事件循环不支持信号处理器
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, worker.request_shutdown)
        await worker.start()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
