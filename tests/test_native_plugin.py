"""原生插件的双端可用性取证：一个 handler，两边都调得动。

全程离线：模型不介入（不等待回复），只用靶机构造会话运行时。
"""

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))
sys.path.insert(0, str(WS / "vendor" / "AstrBot-master"))
os.environ["ASTRMAI_WORKSPACE"] = str(WS)
os.environ["ASTRMAI_ACCOUNT_ID"] = "10001"
os.environ["ASTRMAI_ALLOWED_USERS"] = "777"
os.environ["ASTRMAI_ALLOW_GROUPS"] = "1"
os.environ["ASTRMAI_NATIVE_PLUGINS"] = str(WS / "astrmai_plugins")

from astrbot.core.provider.register import llm_tools  # noqa: E402

import importlib  # noqa: E402

plugin_mod = importlib.import_module("astrmai_plugin.main")

FAILS: list[str] = []
NAMES = ("nm_astrmai_demo_who_am_i", "nm_astrmai_demo_echo")


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail else ""), flush=True)
    if not cond:
        FAILS.append(name)


class FakeMsgObj:
    def __init__(self, comps, raw):
        self.message = comps
        self.raw_message = raw


class FakeEvent:
    def __init__(self, comps, raw):
        self.message_obj = FakeMsgObj(comps, raw)
        self.stopped_called = False
        self._execution_active = False
        self._has_send_oper = False

    def is_stopped(self):
        return False

    def stop_event(self):
        self.stopped_called = True

    def cleanup_temporary_local_files(self) -> None:
        pass

    def is_private_chat(self):
        return False

    def get_self_id(self):
        return "10001"

    def get_sender_id(self):
        return "777"

    def get_group_id(self):
        return "12345"

    def get_message_str(self):
        return " ".join(str(getattr(c, "text", "")) for c in self.message_obj.message)

    @property
    def unified_msg_origin(self):
        return "aiocqhttp:Group:12345"


class FakeContext:
    def __init__(self) -> None:
        self.sends: list[dict] = []

    def get_llm_tool_manager(self):
        return llm_tools

    def add_llm_tools(self, *tools):
        llm_tools.func_list.extend(tools)

    async def send_message(self, session, chain):
        self.sends.append({"session": str(session), "chain": chain})
        return True


def group_event_dict(text: str) -> dict:
    return {
        "time": int(time.time()),
        "self_id": 10001,
        "post_type": "message",
        "message_type": "group",
        "message_id": 8001,
        "group_id": 12345,
        "user_id": 777,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"card": "测试群友", "nickname": "测试群友", "group_name": "原生插件靶场"},
    }


def plain(text: str):
    from astrbot.core.message.components import Plain

    return [Plain(text=text)]


async def main() -> int:
    ctx = FakeContext()
    star = plugin_mod.Main(ctx, None)
    await star.initialize()
    for _ in range(90):
        if star.booted or star.boot_error:
            break
        await asyncio.sleep(1)
    if star.boot_error:
        print("BOOT FAILED:", star.boot_error)
        return 1

    native = star.federation.get("native", {})
    check("原生插件已加载", bool(native.get("loaded")) and not native.get("failed"), f"{native}")

    ab_names = {t.name for t in llm_tools.func_list}
    check("AstrBot 工具表含原生工具", all(n in ab_names for n in NAMES), str(sorted(n for n in ab_names if n.startswith("nm_"))))
    check("原生工具没被再镜像成 nm_nm_", not any(n.startswith("nm_nm_") for n in ab_names), "")

    # 建一个麦麦会话运行时
    async for _ in star.to_maibot(FakeEvent(plain("原生插件测试"), group_event_dict("原生插件测试"))):
        pass
    from src.chat.heart_flow.heartflow_manager import heartflow_manager

    runtime = None
    mb_names: list[str] = []
    for rt in list(heartflow_manager.heartflow_chat_list.values()):
        specs = await rt._tool_registry.list_tools(None)
        mb_names = [s.name for s in specs if s.name.startswith("nm_")]
        if all(n in mb_names for n in NAMES):
            runtime = rt
            break
    check("麦麦工具表含同名原生工具", runtime is not None, str(mb_names))
    check("两边名字完全一致", runtime is not None and all(n in ab_names for n in mb_names), f"{mb_names} vs {sorted(ab_names)[:3]}")

    # AstrBot 侧执行
    tool = next(t for t in llm_tools.func_list if t.name == "nm_astrmai_demo_who_am_i")
    out_ab = await tool.handler(FakeEvent(plain("x"), group_event_dict("x")))
    check("AstrBot 侧执行成功", "astrbot" in str(out_ab), str(out_ab)[:110])

    # 麦麦侧执行（走 registry 的真实派发）
    from src.core.tooling import ToolExecutionContext, ToolInvocation

    inv = ToolInvocation(
        tool_name="nm_astrmai_demo_echo",
        arguments={"text": "ping"},
        call_id="n1",
        session_id=runtime.session_id,
        stream_id=runtime.session_id,
    )
    ec = ToolExecutionContext(
        session_id=runtime.session_id,
        stream_id=runtime.session_id,
        is_group_chat=True,
        group_id="12345",
        user_id="777",
    )
    res = await runtime._tool_registry.invoke(inv, ec)
    check(
        "麦麦侧执行成功",
        res.success and "maibot" in res.content and "ping" in res.content,
        f"success={res.success} content={res.content[:120]!r} err={res.error_message[:80]!r}",
    )

    providers = [getattr(p, "provider_name", "") for p in runtime._tool_registry._providers]
    check("两个镜像 provider 并存", "astrbot" in providers and "astrmai_native" in providers, str(providers))

    await star.terminate()
    print("\n结果:", "全部通过" if not FAILS else f"{len(FAILS)} 项失败 {FAILS}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
