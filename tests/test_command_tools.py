"""命令型 AstrBot 插件做成工具后的取证：麦麦能看见、能调、且不会真发到 QQ。

用 astrmai 插件自己的 `/astrmai` 状态命令当样本（它就是 `yield` 一个字符串，
正好验证"冒泡值也要截"这条最常見写法）。全程不联网、不调模型。
"""

import asyncio
import inspect
import os
import sys
import time
from types import SimpleNamespace
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))
sys.path.insert(0, str(WS / "vendor" / "AstrBot-master"))
os.environ["ASTRMAI_WORKSPACE"] = str(WS)
os.environ["ASTRMAI_ACCOUNT_ID"] = "10001"
os.environ["ASTRMAI_ALLOWED_USERS"] = "777"
os.environ["ASTRMAI_ALLOW_GROUPS"] = "1"
os.environ["ASTRMAI_COMMAND_TOOLS"] = "visible"
os.environ["ASTRMAI_NATIVE_PLUGINS"] = str(WS / "astrmai_plugins")

from astrbot.core.provider.register import llm_tools  # noqa: E402

import importlib  # noqa: E402

plugin_mod = importlib.import_module("astrmai_plugin.main")

FAILS: list[str] = []
TOOL = "abcmd_mai"


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
        self.message_str = " ".join(str(getattr(c, "text", "")) for c in comps)
        self.stopped_called = False
        self.is_at_or_wake_command = False
        self._execution_active = False
        self._has_send_oper = False
        self._extras = {}
        self._force_stopped = False
        self.role = "admin"

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
        return self.message_str

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def clear_result(self):
        self._captured_result = None

    def get_result(self):
        return getattr(self, "_captured_result", None)

    def set_result(self, ret):
        self._captured_result = ret

    @property
    def unified_msg_origin(self):
        return "aiocqhttp:Group:12345"


class FakeContext:
    def __init__(self):
        self.sends = []

    def get_llm_tool_manager(self):
        return llm_tools

    def add_llm_tools(self, *tools):
        llm_tools.func_list.extend(tools)

    def get_config(self, umo=None):
        return {}

    async def send_message(self, session, chain):
        self.sends.append({"session": str(session), "chain": chain})
        return True


def plain(text):
    from astrbot.core.message.components import Plain

    return [Plain(text=text)]


def raw_event(text):
    return {
        "time": int(time.time()),
        "self_id": 10001,
        "post_type": "message",
        "message_type": "group",
        "message_id": 9501,
        "group_id": 12345,
        "user_id": 777,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"card": "测试群友", "nickname": "测试群友", "group_name": "命令工具靶场"},
    }


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

    # 生产里 star_manager 加载插件时会把处理器绑成 partial(raw, star_cls)；
    # 本测试没走那条加载路径，这里复刻同样的绑定，测的是同一种条件。
    import functools

    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import star_handlers_registry

    module_path = getattr(star.__class__, "__module__", "astrmai_plugin.main")

    class _Meta:
        name = "astrmai"
        star_cls = star
        activated = True

    for md in star_handlers_registry._handlers:
        if inspect.isfunction(md.handler) and md.handler_module_path == module_path:
            md.handler = functools.partial(md.handler, star)
    star_map[module_path] = _Meta()

    cmds = star.federation.get("command_tools") or {}
    check("命令已做成工具", cmds.get("count", 0) >= 1, str({k: cmds.get(k) for k in ("count", "mode", "hook")}))

    event = FakeEvent(plain("命令工具测试"), raw_event("命令工具测试"))
    async for _ in star.to_maibot(event):
        pass

    from src.chat.heart_flow.heartflow_manager import heartflow_manager
    from src.core.tooling import ToolExecutionContext, ToolInvocation

    runtime = None
    mb_names: set[str] = set()
    for _ in range(10):
        for rt in list(heartflow_manager.heartflow_chat_list.values()):
            mb_names = {s.name for s in await rt._tool_registry.list_tools(None)}
            if TOOL in mb_names:
                runtime = rt
                break
        if runtime:
            break
        await asyncio.sleep(2)
    check("麦麦工具表里有 abcmd_", runtime is not None, f"abcmd_*={[n for n in mb_names if n.startswith('abcmd_')][:6]}")

    if runtime is not None:
        inv = ToolInvocation(tool_name=TOOL, arguments={}, call_id="cmd1", session_id=runtime.session_id, stream_id=runtime.session_id)
        ec = ToolExecutionContext(session_id=runtime.session_id, stream_id=runtime.session_id, is_group_chat=True, group_id="12345", user_id="777")
        res = await runtime._tool_registry.invoke(inv, ec)
        ok = res.success and ("astrmai 状态" in res.content or "引擎:" in res.content)
        check("命令真被执行并截回文本", ok, f"success={res.success} content={res.content[:130]!r} err={res.error_message[:60]!r}")
        check("没有把结果真发到 QQ", not ctx.sends, f"sends={len(ctx.sends)}")

        bad = await runtime._tool_registry.invoke(
            ToolInvocation(tool_name="abcmd_不存在xyz", arguments={}, call_id="cmd2", session_id=runtime.session_id, stream_id=runtime.session_id),
            ec,
        )
        check("不存在的命令给出说明而非崩溃", bad.success or "未找到" in (bad.content + bad.error_message), (bad.content or bad.error_message)[:90])

    providers = [getattr(p, "provider_name", "") for p in runtime._tool_registry._providers] if runtime else []
    check("命令 provider 独立登记", "astrbot_commands" in providers, str(providers))

    await star.terminate()
    print("\n结果:", "全部通过" if not FAILS else f"{len(FAILS)} 项失败 {FAILS}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
