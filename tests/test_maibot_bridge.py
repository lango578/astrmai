"""麦麦侧桥插件的端到端本地测试：真起一个 astrmai 控制台，让插件去打它。

覆盖两件事：
- 新端点 `POST /astrmai/api/tools/call`（能执行 AstrBot 工具 = 高权限口）守得住：无凭据、错凭据、缺 target 都要被拒；
- 麦麦插件在对面各种失败形状下回的是**能照着做的人话**，不是一个裸异常。
全程 127.0.0.1，不出网。
"""

import asyncio
import importlib.util
import json
import os
import socket
import sys
from types import SimpleNamespace
from pathlib import Path

import httpx

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))
sys.path.insert(0, str(WS / "vendor" / "AstrBot-master"))
sys.path.insert(0, str(WS / "MaiBot-main"))
STATE_DIR = WS / "e2e" / "bridge_state"
TOKEN = "astrmai-bridge-token-7Kq2"  # privacy-scan:allow 靶场合成口令：只在 127.0.0.1 的测试实例里用，不是任何真实凭据
PASSWORD = "astrmai-bridge-Zx9-4mqd-pass"  # privacy-scan:allow 靶场合成口令：只在 127.0.0.1 的测试实例里用，不是任何真实凭据

from astrmai.panel import AstrmaiPanel  # noqa: E402
from astrmai.panel_data import Ledger, LogRing  # noqa: E402
from test_panel_api import FakeStar  # noqa: E402  （复用假星，别再造一份会漂移的夹具）

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail else ""), flush=True)
    if not cond:
        FAILS.append(name)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def load_bridge():
    spec = importlib.util.spec_from_file_location("astrbot_bridge_plugin", WS / "maibot_plugins" / "astrbot_bridge" / "plugin.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CallableManager:
    """带真 handler 的工具管理器：call_astrbot_tool 要的是能真跑的东西。"""

    def __init__(self) -> None:
        async def peek(event=None, x: str = "", **kwargs):
            return f"PEEK({x})"

        self.func_list = [SimpleNamespace(name="ab_demo_peek", description="演示", handler=peek)]


async def main() -> int:
    if STATE_DIR.exists():
        for p in STATE_DIR.glob("*"):
            p.unlink()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["ASTRMAI_STATE_DIR"] = str(STATE_DIR)
    os.environ["ASTRMAI_PANEL_HOST"] = "127.0.0.1"
    port = free_port()
    os.environ["ASTRMAI_PANEL_PORT"] = str(port)
    os.environ["ASTRMAI_PANEL_PASSWORD"] = PASSWORD
    os.environ["ASTRMAI_PANEL_TOKEN"] = TOKEN

    star = FakeStar()
    star.context.get_llm_tool_manager = lambda: CallableManager()
    panel = AstrmaiPanel(lambda: star, ledger=Ledger(), logs=LogRing())
    info = await panel.start()
    base = f"http://127.0.0.1:{port}/astrmai/api"
    try:
        # ---- 端点自身的守门（这一步比桥更重要：它能执行插件代码）----
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{base}/tools/call", json={"name": "ab_demo_peek", "args": {}, "target": "777"})
            check("无凭据调工具被拒（401）", r.status_code == 401, f"status={r.status_code}")
            r = await c.post(
                f"{base}/tools/call",
                json={"name": "ab_demo_peek", "args": {"x": "7"}, "target": "777"},
                headers={"X-Astrmai-Token": "wrong-token"},
            )
            check("错 token 调工具被拒", r.status_code in (401, 403), f"status={r.status_code}")

        # ---- 麦麦插件：正常路径 ----
        mod = load_bridge()
        bridge = mod.AstrBotBridge.__new__(mod.AstrBotBridge)
        # 走官方注入路径（config 是只读 property，硬塞属性会炸 —— 测试就是这么发现的）
        bridge.set_plugin_config(
            {"plugin": {"config_version": "1.0.0"}, "bridge": {"base_url": base, "token": TOKEN, "target": "777", "timeout_seconds": 15}}
        )
        await bridge.on_load()
        try:
            listed = await bridge.list_tools()
            check("插件能列出对面工具", "ab_demo_peek" in listed["content"], listed["content"][:90])

            called = await bridge.call_tool(tool="ab_demo_peek", args_json='{"x": "7"}')
            check("插件能执行对面工具并拿到结果", called["content"] == "PEEK(7)", str(called)[:120])

            named = await bridge.call_tool(tool="ab_demo_peek", args_json='{"x": "9"}', target="777")
            check("显式 target 也能过", named["content"] == "PEEK(9)", str(named)[:90])

            stat = await bridge.status()
            check("status 回的是可解析的 JSON", isinstance(json.loads(stat["content"]), dict), stat["content"][:80])

            # ---- 失败形状：都要回人话 ----
            unknown = await bridge.call_tool(tool="no_such_tool", args_json="{}")
            check("不存在的工具名回明确说法", "未找到" in unknown["content"], unknown["content"][:90])

            bad_args = await bridge.call_tool(tool="ab_demo_peek", args_json="[1,2]")
            check("参数不是 JSON 对象时不硬猜", "JSON" in bad_args["content"], bad_args["content"][:90])

            # 默认 target 也清空才是"没处可挂"；配置里有默认值时回落到它是设计行为，不是漏洞
            bridge.set_plugin_config({"plugin": {"config_version": "1.0.0"}, "bridge": {"base_url": base, "token": TOKEN}})
            no_target = await bridge.call_tool(tool="ab_demo_peek", args_json="{}", target="")
            check("缺 target 时拒绝而不是乱抓会话上下文", "target" in no_target["content"], no_target["content"][:90])
            bridge.set_plugin_config(
                {"plugin": {"config_version": "1.0.0"}, "bridge": {"base_url": base, "token": TOKEN, "target": "777", "timeout_seconds": 15}}
            )
            fell_back = await bridge.call_tool(tool="ab_demo_peek", args_json='{"x": "5"}')
            check("配置里有默认 target 时按设计回落", fell_back["content"] == "PEEK(5)", str(fell_back)[:90])

            ghost = await bridge.call_tool(tool="ab_demo_peek", args_json="{}", target="424242")
            check("陌生会话没有事件上下文时拒绝", "没有消息进来" in ghost["content"], ghost["content"][:110])

            bridge.set_plugin_config({"plugin": {"config_version": "1.0.0"}, "bridge": {"base_url": base, "token": "bad", "target": "777"}})
            denied = await bridge.call_tool(tool="ab_demo_peek", args_json="{}")
            check("token 不对时给出可照做的提示（401 语义）", "token" in denied["content"], denied["content"][:120])

            bridge.set_plugin_config(
                {"plugin": {"config_version": "1.0.0"}, "bridge": {"base_url": f"http://127.0.0.1:{free_port()}/astrmai/api", "token": TOKEN, "target": "777"}}
            )
            offline = await bridge.list_tools()
            check("对面没在跑时说清是连不上", "连不上" in offline["content"], offline["content"][:120])
        finally:
            await bridge.on_unload()

        # ---- 声明与 manifest：静态但致命（没声明成组件=麦麦根本看不见）----
        from maibot_sdk import components as sdk_components

        attr = sdk_components._COMPONENT_INFO_ATTR
        declared = {}
        for name in ("list_tools", "call_tool", "status"):
            info = getattr(getattr(mod.AstrBotBridge, name), attr, None)
            if info is not None:
                declared[info.name] = (info.metadata or {}).get("visibility")
        check(
            "三个工具都真声明成组件且默认可见",
            set(declared) == {"astrbot_tools", "astrbot_call", "astrbot_status"} and all(v == "visible" for v in declared.values()),
            str(declared),
        )

        from src.plugin_runtime.runner.manifest_validator import PluginManifest

        raw = json.loads((WS / "maibot_plugins" / "astrbot_bridge" / "_manifest.json").read_text(encoding="utf-8"))
        try:
            manifest = PluginManifest.model_validate(raw)
            check("manifest 过麦麦官方 v2 校验器", manifest.id == "astrmai.astrbot-bridge", f"{manifest.id} {manifest.version}")
        except Exception as exc:  # noqa: BLE001
            check("manifest 过麦麦官方 v2 校验器", False, str(exc)[:160])
    finally:
        await panel.stop()
        os.environ.pop("ASTRMAI_PANEL_TOKEN", None)
        os.environ.pop("ASTRMAI_PANEL_PASSWORD", None)

    print("\n%d failure(s)" % len(FAILS), FAILS)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
