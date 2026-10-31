"""实机启动器：真 AstrBot 核心 + 进程内麦麦，等你的 NapCat 连进来。

不发任何请求、不主动发消息；能不能答由硬白名单决定（--allow）。Ctrl+C 干净退出并回收子进程。
"""

import argparse
import asyncio
import json
import os
import shutil
import signal
import sys
from pathlib import Path

WS = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="astrmai 实机运行")
    p.add_argument("--port", type=int, default=8123, help="NapCat 要连过来的反向 WS 端口")
    p.add_argument("--token", default="", help="NapCat 的访问密钥（留空=不校验）")
    p.add_argument("--allow", required=True, help="允许私聊的 QQ 号，逗号分隔（不在名单里一律沉默）")
    p.add_argument("--groups", default="", help="额外允许的群号，逗号分隔；不填就是群全沉默")
    p.add_argument("--account", default="", help="机器人 QQ 号（用于 @ 判定，可留空）")
    p.add_argument("--allow-groups", action="store_true", help="允许群聊（默认关）")
    return p.parse_args()


def build_root(args: argparse.Namespace) -> Path:
    root = WS / "e2e" / "real"
    data = root / "data"
    plugin = data / "plugins" / "astrmai"
    if plugin.exists():
        shutil.rmtree(plugin)
    plugin.mkdir(parents=True)
    shutil.copy(WS / "astrmai_plugin" / "main.py", plugin / "main.py")
    shutil.copy(WS / "astrmai_plugin" / "metadata.yaml", plugin / "metadata.yaml")
    config = {
        "config_version": 5,
        "platform": [
            {
                "id": "default",
                "name": "astrmai-real",
                "type": "aiocqhttp",
                "enable": True,
                "ws_reverse_host": "127.0.0.1",
                "ws_reverse_port": args.port,
                "ws_reverse_token": args.token,
            }
        ],
        "wake_prefix": ["/"],
        "admins_id": [s.strip() for s in args.allow.split(",") if s.strip()],
        "log_level": "INFO",
        "platform_settings": {"enable_id_white_list": True, "id_whitelist": []},
        "dashboard": {"enable": False},
        "provider_sources": [],
        "provider": [],
    }
    (data / "cmd_config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    return root


async def main(args: argparse.Namespace) -> int:
    root = build_root(args)
    os.environ["ASTRBOT_ROOT"] = str(root)
    os.environ["ASTRMAI_WORKSPACE"] = str(WS)
    os.environ["ASTRMAI_MAI_ROOT"] = str(WS / "MaiBot-main")
    os.environ["ASTRMAI_PLATFORM"] = "qq"
    os.environ["ASTRMAI_ACCOUNT_ID"] = args.account
    os.environ["ASTRMAI_ALLOWED_USERS"] = args.allow
    os.environ["ASTRMAI_GROUPS"] = args.groups
    os.environ["ASTRMAI_ALLOW_GROUPS"] = "1" if args.allow_groups else "0"
    os.environ["ASTRMAI_STATUS_FILE"] = str(root / "astrmai_status.json")

    sys.path.insert(0, str(WS))
    sys.path.insert(0, str(WS / "vendor" / "AstrBot-master"))
    os.chdir(root)

    import main as astrbot_main
    from astrbot.core import LogBroker, LogManager, db_helper, logger
    from astrbot.core.core_lifecycle import AstrBotCoreLifecycle

    from astrmai.tls import apply as cap_tls

    capped = cap_tls()
    print(f"TLS 上限钉到 1.2: {capped}", flush=True)

    astrbot_main.check_env()
    broker = LogBroker()
    LogManager.set_queue_handler(logger, broker)

    print("正在启动 astrmai（真 AstrBot + 进程内麦麦）...", flush=True)
    lifecycle = AstrBotCoreLifecycle(broker, db_helper)
    await lifecycle.initialize()
    core = asyncio.create_task(lifecycle.start(), name="astrbot")
    core.add_done_callback(lambda t: print(f"[astrbot 任务结束] {t.exception()!r}" if not t.cancelled() else "[astrbot 已取消]", flush=True))

    for _ in range(120):
        try:
            _r, w = await asyncio.open_connection("127.0.0.1", args.port)
            w.close()
            await w.wait_closed()
            break
        except OSError:
            await asyncio.sleep(1)
    else:
        print(f"FAIL: 端口 {args.port} 没起来", flush=True)
        await lifecycle.stop()
        return 1

    allow = args.allow.replace(",", "、")
    print(
        "\n================ astrmai 就绪 ================\n"
        f"  监听        : ws://127.0.0.1:{args.port}/ws\n"
        f"  访问密钥    : {'（NapCat 里也留空）' if not args.token else args.token}\n"
        f"  私聊白名单  : {allow}\n"
        f"  群聊        : {'开启，名单 ' + args.groups if args.allow_groups else '全部沉默（不转发、不回答）'}\n"
        "  NapCat 要做的：网络构建器 → 新建 Websocket 客户端\n"
        f"                     URL 填 ws://127.0.0.1:{args.port}/ws ，密钥同上，然后点亮\n"
        "  注意先把 NapCat 上连向原 AstrBot 的那条停掉，免得两个脑子抢同一条连接\n"
        "  Ctrl+C 退出\n"
        "==============================================\n",
        flush=True,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: 信号不能进 loop，用默认中断
            pass

    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    print("\n正在关停...", flush=True)
    try:
        await lifecycle.stop()
    except Exception as exc:  # noqa: BLE001
        print(f"lifecycle.stop 失败: {type(exc).__name__}: {exc}", flush=True)
    core.cancel()
    await asyncio.gather(core, return_exceptions=True)
    await asyncio.sleep(2)
    try:
        print("状态:", (root / "astrmai_status.json").read_text(encoding="utf-8"), flush=True)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main(parse_args())))
    except KeyboardInterrupt:
        print("\n已中断", flush=True)
