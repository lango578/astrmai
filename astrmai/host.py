"""Embed MaiBot in another process: no Runner/Worker, no sockets of its own, sends via the host."""

import asyncio
import atexit
import os
import sys
from pathlib import Path
from typing import Any, Optional

from .config import AstrmaiConfig
from .driver import Sink, make_astrbot_driver
from .inbound import onebot_to_maim_dict_async


def prepare_paths(cfg: AstrmaiConfig) -> None:
    """Make MaiBot importable as `src` and forbid its process supervisor."""

    root_path = Path(str(cfg.mai_root))
    if not (root_path / "src").is_dir():
        # 市场安装的 astrmai 不打包麦麦（官方市场 zip 上限 16MB），所以这条是最常见的失败，
        # 必须说清去哪儿拿、放到哪、怎么用环境变量指过去，而不是抛一句 chdir 的 FileNotFoundError
        raise FileNotFoundError(
            f"没找到麦麦本体：{root_path} 下没有 src/。"
            "请先获取 MaiBot v1.3.18（https://github.com/MaiM-with-u/MaiBot）放到该目录，"
            "或在插件配置里填 mai_root（仓库模式下可用环境变量 ASTRMAI_MAI_ROOT）。"
        )
    root = str(cfg.mai_root)
    os.chdir(root)
    if root not in sys.path:
        sys.path.insert(0, root)
    # runtime.py installs signal handlers, spawns a Worker, and ends in os._exit(); a host process
    # that imports it would take over the interpreter.
    sys.modules["src.runtime"] = None  # type: ignore[assignment]


class MaiBotHost:
    def __init__(self, cfg: AstrmaiConfig, sink: Sink) -> None:
        self.cfg = cfg
        self.sink = sink
        self.driver: Optional[Any] = None
        self.system: Any = None
        self._manager: Any = None
        self.boot_ms = 0.0
        self.runner_pids: list[int] = []
        #: runner PID 变化史（重启换 PID 会留痕），面板只读
        self.runner_history: list[dict] = []
        self._reap_registered = False
        self.job: Any = None

    async def boot(self) -> None:
        prepare_paths(self.cfg)
        cfg = self.cfg

        from . import tls
        from .jobguard import arm as arm_job_guard

        # Arm before MaiBot starts spawning plugin runners: children inherit job membership, so a
        # crash or taskkill /F takes them down with us instead of leaking them.
        self.job = await asyncio.to_thread(arm_job_guard)

        tls.apply()

        import src.common.remote as remote

        # 上游遥测硬编码往第三方站点发心跳；默认关掉，只有显式 ASTRMAI_TELEMETRY=on 才放行
        telemetry_on = os.environ.get("ASTRMAI_TELEMETRY", "off").strip().lower() in ("on", "1", "true")
        if cfg.neuter_telemetry and not telemetry_on:
            remote.TELEMETRY_SERVER_URL = "http://127.0.0.1:9"

        from src.config.config import global_config

        if cfg.disable_maim_message:
            global_config.maim_message.enabled = False
        if cfg.disable_webui:
            global_config.webui.enabled = False
        global_config.debug.enable_console_input = False

        from src.main import MainSystem

        started = asyncio.get_running_loop().time()
        self.system = MainSystem()
        await self.system.initialize()
        self.boot_ms = (asyncio.get_running_loop().time() - started) * 1000

        from .panel_data import apply_stored_mai_settings

        reapplied = apply_stored_mai_settings()
        if reapplied:
            print(f"[astrmai] 面板存过的麦麦自主性设置已恢复: {', '.join(reapplied)}", flush=True)

        from src.platform_io import RouteBinding
        from src.platform_io.manager import get_platform_io_manager

        self._manager = get_platform_io_manager()
        self.driver = make_astrbot_driver(
            self.sink,
            platform=cfg.platform,
            account_id=cfg.account_id,
            driver_id=self.driver_id,
        )
        if self._manager.is_started:
            await self._manager.add_driver(self.driver)
        else:
            self._manager.register_driver(self.driver)
        self._manager.bind_send_route(
            RouteBinding(
                route_key=self.driver.descriptor.route_key,
                driver_id=self.driver.driver_id,
                driver_kind=self.driver.descriptor.kind,
            )
        )

        # MaiBot's plugin runners are out-of-process children. If this host dies without
        # shutdown() -- a crash, a cancelled task -- they keep running, so track and reap them.
        self.runner_pids = await asyncio.to_thread(self._collect_runner_pids)
        if self.runner_pids and not self._reap_registered:
            atexit.register(self.reap_runners)
            self._reap_registered = True

        # When the host could not join the job (another job already owns it) adopt the runners by PID.
        if self.runner_pids and self.job is not None and self.job.mode == "pids":
            adopted = await asyncio.to_thread(lambda: [pid for pid in self.runner_pids if self.job.absorb(pid)])
            if not adopted:
                print("[astrmai] 作业对象未能认领 runner 子进程，仅依赖 atexit 回收", flush=True)

    @staticmethod
    def _collect_runner_pids() -> list[int]:
        pids: list[int] = []
        try:
            from src.plugin_runtime.integration import get_plugin_runtime_manager

            for supervisor in getattr(get_plugin_runtime_manager(), "supervisors", []) or []:
                process = getattr(supervisor, "_runner_process", None)
                pid = getattr(process, "pid", None) if process is not None else None
                if pid:
                    pids.append(int(pid))
        except Exception:  # noqa: BLE001 - bookkeeping must never break the boot
            pass
        return pids

    def refresh_runners(self) -> list[int]:
        """Re-scan the supervisors: a restart replaces the PID, so a boot-time list would miss it."""

        import time as _time

        found = self._collect_runner_pids()
        new = [pid for pid in found if pid not in self.runner_pids]
        if new:
            self.runner_history.append({"time": _time.time(), "added": new, "known": sorted(self.runner_pids)})
            self.runner_history[:] = self.runner_history[-16:]
        if found:
            self.runner_pids = sorted(set(self.runner_pids) | set(found))
            if self.job is not None and self.job.mode == "pids":
                for pid in found:
                    self.job.absorb(pid)
        return found

    def live_runners(self) -> list[int]:
        from .jobguard import process_alive

        self.refresh_runners()
        return [pid for pid in self.runner_pids if process_alive(pid)]

    def reap_runners(self) -> str:
        """Terminate any runner still alive. Safe to call twice, and at interpreter exit."""

        from .jobguard import terminate_process

        alive = self.live_runners()
        if not alive:
            return "none-alive"
        gone = [pid for pid in alive if terminate_process(pid)]
        left = [pid for pid in alive if pid not in gone]
        detail = self.job.describe() if self.job is not None else "作业对象未装"
        return f"reaped {len(gone)}/{len(alive)}: {alive}; 残留 {left}; {detail}"

    @property
    def driver_id(self) -> str:
        return f"local.astrmai.{self.cfg.account_id or 'default'}"

    async def deliver(self, onebot_event: dict) -> bool:
        """Feed one OneBot v11 message event into the brain. True if it was accepted."""

        maim = await onebot_to_maim_dict_async(
            onebot_event,
            platform=self.cfg.platform,
            account_id=self.cfg.account_id,
            fetch_media=self.cfg.fetch_media,
            media_timeout=self.cfg.media_timeout,
        )
        if maim is None:
            return False
        from src.chat.message_receive.bot import chat_bot

        await chat_bot.message_process(maim)
        return True

    async def shutdown(self) -> dict[str, str]:
        """Tear down everything MaiBot started; MaiBot's plugin runners do not die with the host."""

        report: dict[str, str] = {}

        if self._manager is not None and self.driver is not None:
            try:
                self._manager.unbind_send_route(self.driver.descriptor.route_key, self.driver.driver_id)
                report["send_route"] = "unbound"
            except Exception as exc:  # noqa: BLE001
                report["send_route"] = f"FAIL {exc}"
        if self._manager is not None:
            try:
                await self._manager.stop()
                report["platform_io"] = "stopped"
            except Exception as exc:  # noqa: BLE001
                report["platform_io"] = f"FAIL {exc}"

        if self.system is not None and getattr(self.system, "http_server", None) is not None:
            try:
                await self.system.http_server.shutdown()
                report["http"] = "stopped"
            except Exception as exc:  # noqa: BLE001
                report["http"] = f"FAIL {exc}"

        try:
            from src.plugin_runtime.integration import get_plugin_runtime_manager

            await get_plugin_runtime_manager().stop()
            report["plugin_runtime"] = "stopped"
        except Exception as exc:  # noqa: BLE001
            report["plugin_runtime"] = f"FAIL {exc}"

        report["plugin_runners"] = self.reap_runners()
        report["job_guard"] = self.job.describe() if self.job is not None else "作业对象未装"

        try:
            from src.manager.async_task_manager import async_task_manager

            await async_task_manager.stop_and_wait_all_tasks()
            report["async_tasks"] = "stopped"
        except Exception as exc:  # noqa: BLE001
            report["async_tasks"] = f"FAIL {exc}"

        try:
            from src.services.memory_flow_service import memory_automation_service

            await memory_automation_service.shutdown()
            report["memory_flow"] = "stopped"
        except Exception as exc:  # noqa: BLE001
            report["memory_flow"] = f"FAIL {exc}"

        return report
