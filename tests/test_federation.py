"""工具联邦的取证测试：两边插件在对方脑子里都看得见、叫得动。

不依赖模型输出：只验证 (a) 麦麦的工具表里出现了 AstrBot 工具，(b) 反向执行能真的跑到处理器，
(c) AstrBot 的工具表里出现了麦麦的工具。全程不花钱、不联网。
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
os.environ.setdefault("ASTRMAI_STATUS_FILE", str(WS / "e2e" / "fed_status.json"))

from astrbot.core.agent.tool import FunctionTool  # noqa: E402
from astrbot.core.provider.register import llm_tools  # noqa: E402

import importlib  # noqa: E402

plugin_mod = importlib.import_module("astrmai_plugin.main")
from astrmai import federation as fed  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail else ""), flush=True)
    if not cond:
        FAILS.append(name)


class FakeContext:
    def __init__(self) -> None:
        self.sends: list[dict] = []

    def get_llm_tool_manager(self):
        return llm_tools

    def add_llm_tools(self, *tools) -> None:
        for t in tools:
            llm_tools.func_list.append(t)

    async def send_message(self, session, chain):
        self.sends.append({"session": str(session), "chain": chain})
        return True


PROBE_MARK = "联邦探针"


async def probe_handler(event, text: str = "") -> str:
    """一个假装是第三方插件注册的 LLM 工具。"""

    return f"{PROBE_MARK}:{text}"


def install_probe_tool() -> None:
    llm_tools.func_list.append(
        FunctionTool(
            name="fed_probe",
            description="联邦测试探针工具",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            handler=probe_handler,
        )
    )


class FakeEvent:
    def __init__(self, comps, raw):
        self.message_obj = SimpleNamespace(message=comps, raw_message=raw)
        self.stopped_called = False
        # 真实 AstrMessageEvent 上有这些字段，event_context() 会读写它们
        self._execution_active = False
        self._has_send_oper = False

    def is_stopped(self):
        return False

    def cleanup_temporary_local_files(self) -> None:
        """真实事件在 event_context 退出时会调它，替身没有临时文件要清。"""

    def stop_event(self):
        self.stopped_called = True

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


def group_event_dict(text: str) -> dict:
    return {
        "time": int(time.time()),
        "self_id": 10001,
        "post_type": "message",
        "message_type": "group",
        "message_id": 7001,
        "group_id": 12345,
        "user_id": 777,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"card": "测试群友", "nickname": "测试群友", "group_name": "联邦靶场"},
    }


def plain(text):
    from astrbot.core.message.components import Plain

    return [Plain(text=text)]


async def main() -> int:
    install_probe_tool()

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
    print(f"boot ok ({star.host.boot_ms:.0f} ms) 联邦: {star.federation}", flush=True)

    check("A 方向挂载成功", star.federation.get("hook") in ("wrapped", "already wrapped"), str(star.federation.get("hook")))
    mai = star.federation.get("mai_tools") or {}
    check("B 方向有工具进 AstrBot", isinstance(mai, dict) and mai.get("total", 0) > 0, f"mai_tools={mai}")

    # AstrBot 侧确实能看到 mai_ 前缀的麦麦工具
    names_ab = [t.name for t in llm_tools.func_list]
    mai_names = [n for n in names_ab if n.startswith("mai_")]
    check("AstrBot 工具表含麦麦工具", len(mai_names) >= 3, f"{len(mai_names)} 个: {mai_names[:6]}")
    check("联邦不自我镜像回环", not any(n.startswith("mai_ab_") for n in names_ab), str([n for n in names_ab if "ab_" in n][:5]))

    # 让麦麦建一个真实会话运行时（不等待回复，不花钱）
    event = FakeEvent(plain("联邦测试，先打个招呼"), group_event_dict("联邦测试，先打个招呼"))
    async for _ in star.to_maibot(event):
        pass
    from src.chat.heart_flow.heartflow_manager import heartflow_manager

    runtimes = list(heartflow_manager.heartflow_chat_list.values())
    check("麦麦会话运行时已建立", bool(runtimes), f"{len(runtimes)} 个运行时")

    runtime = None
    mirrored_names: list[str] = []
    for rt in runtimes:
        specs = await rt._tool_registry.list_tools(None)
        mirrored_names = [s.name for s in specs if s.name.startswith("ab_")]
        if "ab_fed_probe" in mirrored_names:
            runtime = rt
            break
    check("麦麦工具表含 AstrBot 探针", runtime is not None, f"镜像到的工具: {mirrored_names}")

    if runtime is not None:
        # 走 registry 的真实派发路径，而不是直接调 provider
        from src.core.tooling import ToolExecutionContext, ToolInvocation

        inv = ToolInvocation(
            tool_name="ab_fed_probe",
            arguments={"text": "ping"},
            call_id="c1",
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
            "反向执行真的调到了处理器",
            res.success and PROBE_MARK in res.content,
            f"success={res.success} content={res.content[:100]!r} err={res.error_message[:220]!r}",
        )
        providers = [getattr(p, "provider_name", "") for p in runtime._tool_registry._providers]
        check("镜像 provider 已登记", "astrbot" in providers, str(providers))

        # ---- 延迟发现：默认不摊给 planner，但她自己的 tool_search 必须能捞回来 ----
        from src.maisaka.focus.tools import FocusToolRouter

        from astrmai import federation as fed_mod

        specs_now = await runtime._tool_registry.list_tools(None)
        probe_spec = next((s for s in specs_now if s.name == "ab_fed_probe"), None)
        check(
            "镜像工具默认 deferred（不挤爆 planner 的 prompt）",
            probe_spec is not None and probe_spec.metadata.get("visibility") == "deferred",
            str(getattr(probe_spec, "metadata", None)),
        )
        check(
            "麦麦自己的路由也判它不是默认可见",
            probe_spec is not None and FocusToolRouter.is_default_visible(probe_spec) is False,
            "" if probe_spec is None else str(FocusToolRouter.is_default_visible(probe_spec)),
        )
        if probe_spec is not None:
            # 按麦麦 router 的同一套分类喂给运行时，再走她真实的 tool_search 发现路径
            runtime.update_deferred_tool_specs([s for s in specs_now if not FocusToolRouter.is_default_visible(s)])
            hits = runtime.search_deferred_tool_specs("ab_", limit=20)
            check("tool_search 按前缀能搜到镜像工具", any(h.name == "ab_fed_probe" for h in hits), str([h.name for h in hits][:6]))
            newly = runtime.discover_deferred_tools(["ab_fed_probe"])
            check(
                "发现后进入已激活集合（下一轮 planner 就能直接调）",
                newly == ["ab_fed_probe"] and "ab_fed_probe" in runtime.discovered_tool_names,
                f"newly={newly} discovered={sorted(runtime.discovered_tool_names)[:5]}",
            )

        # 开关反向：显式 visible 时麦麦应当直接摊给 planner（证明这确实是一个开关的事）
        visible_mirror = fed_mod.AstrBotToolMirror(list_tools=lambda: [{"name": "probe", "description": "d"}], run_tool=None, visible=True)
        deferred_mirror = fed_mod.AstrBotToolMirror(list_tools=lambda: [{"name": "probe", "description": "d"}], run_tool=None, visible=False)
        v_spec = (await visible_mirror.list_tools(None))[0]
        d_spec = (await deferred_mirror.list_tools(None))[0]
        check(
            "visible=1 时麦麦直接摊开",
            v_spec.metadata.get("visibility") == "visible" and FocusToolRouter.is_default_visible(v_spec) is True,
            str(v_spec.metadata),
        )
        check(
            "visible=0 时进 deferred 池",
            d_spec.metadata.get("visibility") == "deferred" and FocusToolRouter.is_default_visible(d_spec) is False,
            str(d_spec.metadata),
        )

    await star.terminate()
    print("\n结果:", "全部通过" if not FAILS else f"{len(FAILS)} 项失败 {FAILS}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
