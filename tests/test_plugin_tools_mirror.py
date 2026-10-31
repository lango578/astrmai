"""麦麦自家插件（out-of-process）的工具，是否真的镜像进了 AstrBot 并且调得动。

注意：仓库自带的 hello_world_plugin 在 1.3.18 上自己就加载失败（webui.json 还是协议 1，
宿主已升级到协议 2 → 整个插件"初始化失败"）。所以这里复制一份、删掉过期的 webui 声明，
做一个能真正加载起来的样本插件 —— 测的是镜像链路，不是上游样板的腐坏。
"""

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))
sys.path.insert(0, str(WS / "vendor" / "AstrBot-master"))
os.environ["ASTRMAI_WORKSPACE"] = str(WS)
os.environ["ASTRMAI_ACCOUNT_ID"] = "10001"
os.environ["ASTRMAI_ALLOWED_USERS"] = "777"
os.environ["ASTRMAI_ALLOW_GROUPS"] = "1"

PLUGINS_ROOT = WS / "MaiBot-main" / "plugins"
SAMPLE_DIR = PLUGINS_ROOT / "astrmai_sample_tool"
TOOL_NAME = "mai_compare_numbers"

from astrbot.core.provider.register import llm_tools  # noqa: E402

import importlib  # noqa: E402

plugin_mod = importlib.import_module("astrmai_plugin.main")

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail else ""), flush=True)
    if not cond:
        FAILS.append(name)


def make_sample_plugin() -> bool:
    source = PLUGINS_ROOT / "hello_world_plugin"
    if not source.exists():
        return False
    if SAMPLE_DIR.exists():
        shutil.rmtree(SAMPLE_DIR)
    shutil.copytree(source, SAMPLE_DIR, ignore=shutil.ignore_patterns("__pycache__", "webui.json"))

    manifest = json.loads((SAMPLE_DIR / "_manifest.json").read_text(encoding="utf-8"))
    manifest["id"] = "astrmai.sample-tool"
    manifest["name"] = "astrmai 镜像测试样本插件"
    manifest.pop("webui", None)
    (SAMPLE_DIR / "_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (SAMPLE_DIR / "config.toml").write_text('[plugin]\nenabled = true\nconfig_version = "2.0.0"\n', encoding="utf-8")
    return True


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
    def __init__(self):
        self.sends = []

    def get_llm_tool_manager(self):
        return llm_tools

    def add_llm_tools(self, *tools):
        llm_tools.func_list.extend(tools)

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
        "message_id": 9101,
        "group_id": 12345,
        "user_id": 777,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"card": "测试群友", "nickname": "测试群友", "group_name": "插件工具靶场"},
    }


async def main() -> int:
    if not make_sample_plugin():
        print("SKIP: 找不到 hello_world_plugin 样本")
        return 2
    star = None
    try:
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

        found = False
        for _ in range(20):
            star._sync_maibot_tools()
            if any(t.name == TOOL_NAME for t in llm_tools.func_list):
                found = True
                break
            await asyncio.sleep(2)

        mai = sorted(t.name for t in llm_tools.func_list if t.name.startswith("mai_"))
        check("麦麦插件工具进了 AstrBot", found, f"mai_* 共 {len(mai)} 个: {mai[:8]}")
        check(
            "被识别为插件来源而非内置",
            (star.federation.get("mai_tools") or {}).get("plugin_sourced", 0) >= 1,
            str(star.federation.get("mai_tools")),
        )

        async for _ in star.to_maibot(FakeEvent(plain("插件工具测试"), raw_event("插件工具测试"))):
            pass
        from src.chat.heart_flow.heartflow_manager import heartflow_manager

        runtime = None
        mb_names: set[str] = set()
        for rt in list(heartflow_manager.heartflow_chat_list.values()):
            mb_names = {s.name for s in await rt._tool_registry.list_tools(None)}
            if TOOL_NAME.removeprefix("mai_") in mb_names:
                runtime = rt
                break
        check("麦麦自己也能看到这个插件工具", runtime is not None, f"麦麦侧工具数={len(mb_names)}")

        tool = next((t for t in llm_tools.func_list if t.name == TOOL_NAME), None)
        if tool is not None:
            out = await tool.handler(FakeEvent(plain("x"), raw_event("x")), num1=3, num2=7)
            ok = isinstance(out, str) and ("小于" in out or "大于" in out or "等于" in out)
            check("AstrBot 侧真的调到了麦麦插件（走 RPC 往返）", ok, f"{str(out)[:170]!r}")
        else:
            check("AstrBot 侧真的调到了麦麦插件（走 RPC 往返）", False, "工具不存在")

        # 重扫的增删要收敛：连续两次不该重复注册
        before = len([t for t in llm_tools.func_list if t.name.startswith("mai_")])
        star._sync_maibot_tools()
        star._sync_maibot_tools()
        after = len([t for t in llm_tools.func_list if t.name.startswith("mai_")])
        check("重扫幂等，不重复注册", before == after, f"{before} -> {after}")
    finally:
        if star is not None:
            try:
                await star.terminate()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(SAMPLE_DIR, ignore_errors=True)
        print("已移除样本插件目录")

    print("\n结果:", "全部通过" if not FAILS else f"{len(FAILS)} 项失败 {FAILS}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
