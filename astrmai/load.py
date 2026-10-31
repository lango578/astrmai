"""负载采样：主机 + astrmai 进程 + 两个脑子自己的运行时，全 psutil 真值。

首页就是这张表，所以宁可少放也不能放假的：
- 需要基线的（网络速率、消息速率）没攒够样本就报 null，不报 0；
- 事件循环延迟是融合产品最该看的数：两个脑子共用一个 loop，谁卡了一目了然。
"""

import asyncio
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

SAMPLE_INTERVAL = 2.0
HISTORY = 180  # 2s × 180 = 6 分钟窗口


def _bytes_per_sec(prev: int, curr: int, dt: float) -> float:
    if dt <= 0:
        return 0.0
    return max(0.0, (curr - prev) / dt)


class LoadProbe:
    """后台采样器。由面板起停，进程停了数据也就没了（不落盘，免得变成第二个日志）。"""

    def __init__(self, *, workspace: Path, watch_pids: Any = None) -> None:
        self.workspace = Path(workspace)
        self._watch_pids = watch_pids or (lambda: [])
        self.interval = SAMPLE_INTERVAL
        self.series = {
            "t": deque(maxlen=HISTORY),
            "cpu": deque(maxlen=HISTORY),
            "mem": deque(maxlen=HISTORY),
            "proc_rss": deque(maxlen=HISTORY),
            "net_up": deque(maxlen=HISTORY),
            "net_down": deque(maxlen=HISTORY),
            "loop_lag": deque(maxlen=HISTORY),
            "msgs_in": deque(maxlen=HISTORY),
            "msgs_out": deque(maxlen=HISTORY),
        }
        self.latest: dict[str, Any] = {}
        self.samples = 0
        self._task: asyncio.Task | None = None
        self._proc = None
        self._last_net: tuple[int, int, float] | None = None
        self._last_counts: tuple[int, int, float] | None = None
        self._counters = lambda: (0, 0)
        self._extra: dict[str, Any] = {}
        self.error = ""

    # ---------------- 生命周期 ----------------

    def attach_counters(self, fn: Any) -> None:
        """星插件把 (forwarded, sent) 计数器接进来，速率才是真的。"""

        self._counters = fn

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            import psutil

            self._proc = psutil.Process(os.getpid())
            self._proc.cpu_percent(None)  # 第一次调用只是建基线，值本身没意义
        except Exception as exc:  # noqa: BLE001
            self.error = f"psutil 不可用: {type(exc).__name__}: {exc}"
            return
        self._task = asyncio.create_task(self._loop(), name="astrmai-load")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            due = loop.time() + self.interval
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                raise
            lag = max(0.0, loop.time() - due)
            try:
                self._sample(loop_lag=lag)
            except Exception as exc:  # noqa: BLE001 - 采样绝不能拖垮宿主
                self.error = f"{type(exc).__name__}: {exc}"

    # ---------------- 采样 ----------------

    def _sample(self, *, loop_lag: float) -> None:
        import psutil

        now = time.time()
        net = psutil.net_io_counters()
        vm = psutil.virtual_memory()
        cpu = psutil.cpu_percent(None)
        pi = self._proc.memory_info()
        sent, recv = int(net.bytes_sent), int(net.bytes_recv)

        rates: dict[str, float] = {}
        if self._last_net:
            p_sent, p_recv, p_t = self._last_net
            dt = now - p_t
            rates = {
                "net_up": _bytes_per_sec(p_sent, sent, dt),
                "net_down": _bytes_per_sec(p_recv, recv, dt),
            }
        self._last_net = (sent, recv, now)

        fwd, out = self._counters()
        msg_rates: dict[str, Any] = {"in": None, "out": None}
        if self._last_counts:
            pf, po, pt = self._last_counts
            dt = max(now - pt, 1e-6)
            msg_rates = {"in": max(0, fwd - pf) / dt, "out": max(0, out - po) / dt}
        self._last_counts = (fwd, out, now)

        runners = self._runner_load()
        disk = psutil.disk_usage(str(self.workspace))
        try:
            data_free = psutil.disk_usage(str(self.workspace / "MaiBot-main" / "data")).free
        except OSError:
            data_free = disk.free

        self.latest = {
            "ts": now,
            "host": {
                "cpu_percent": cpu,
                "cores": psutil.cpu_count(logical=True),
                "cpu_mhz": int(psutil.cpu_freq().current) if psutil.cpu_freq() else None,
                "mem_percent": vm.percent,
                "mem_used_mb": round(vm.used / 2**20),
                "mem_total_mb": round(vm.total / 2**20),
                "swap_percent": psutil.swap_memory().percent,
                "disk_percent": disk.percent,
                "disk_used_gb": round(disk.used / 2**30, 1),
                "disk_total_gb": round(disk.total / 2**30, 1),
                "data_free_mb": round(data_free / 2**20),
                "uptime_sec": round(now - psutil.boot_time()),
                "net_up_bps": rates.get("net_up"),
                "net_down_bps": rates.get("net_down"),
                "net_sent_total": sent,
                "net_recv_total": recv,
            },
            "process": {
                "pid": os.getpid(),
                "rss_mb": round(pi.rss / 2**20, 1),
                "vms_mb": round(pi.vms / 2**20, 1),
                "cpu_percent": self._proc.cpu_percent(None),
                "threads": self._proc.num_threads(),
                "handles": getattr(self._proc, "num_handles", lambda: None)(),
                "create_time": self._proc.create_time(),
            },
            "fusion": {
                "loop_lag_ms": round(loop_lag * 1000, 1),
                "asyncio_tasks": len([t for t in asyncio.all_tasks() if not t.done()]),
                "msg_rate": msg_rates,
                "counters": {"forwarded": fwd, "sent": out},
                "runners": runners,
            },
            "probe": {"samples": self.samples, "interval": self.interval, "error": self.error},
        }
        self.samples += 1

        for key, value in (
            ("t", now),
            ("cpu", cpu),
            ("mem", vm.percent),
            ("proc_rss", pi.rss / 2**20),
            ("net_up", rates.get("net_up", 0.0)),
            ("net_down", rates.get("net_down", 0.0)),
            ("loop_lag", loop_lag * 1000),
            ("msgs_in", msg_rates["in"] or 0.0),
            ("msgs_out", msg_rates["out"] or 0.0),
        ):
            self.series[key].append(round(float(value), 2))

    def _runner_load(self) -> list[dict]:
        """麦麦的 out-of-process runner：它们的 CPU/内存要单独看，混在宿主里看不出来。"""

        import psutil

        out = []
        for pid in self._watch_pids() or []:
            try:
                proc = psutil.Process(int(pid))
                out.append(
                    {
                        "pid": int(pid),
                        "rss_mb": round(proc.memory_info().rss / 2**20, 1),
                        "cpu_percent": proc.cpu_percent(None),
                        "threads": proc.num_threads(),
                        "status": proc.status(),
                    }
                )
            except Exception:  # noqa: BLE001 - runner 随时可能被 supervisor 换掉
                out.append({"pid": int(pid), "gone": True})
        return out

    # ---------------- 出口 ----------------

    def snapshot(self) -> dict:
        series = {k: list(v) for k, v in self.series.items()} if self.samples else {}
        return {
            "ok": bool(self.samples),
            "latest": self.latest,
            "series": series,
            "window_sec": round(self.interval * HISTORY),
            "reason": "" if self.samples else (self.error or "采样器还没攒到数据（刚启动）"),
        }
