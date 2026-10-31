"""运行时加载取证：不重启，把刚放进磁盘的插件装进正在跑的脑子。

流程严格是"先启动 → 再放插件 → 再加载 → 看它出现在两边的工具表里"。
"""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))
sys.path.insert(0, str(WS / "vendor" / "AstrBot-master"))
os.environ["ASTRMAI_WORKSPACE"] = str(WS)
os.environ["ASTRMAI_ACCOUNT_ID"] = "10001"
os.environ["ASTRMAI_ALLOWED_USERS"] = "777"
os.environ["ASTRMAI_ALLOW_GROUPS"] = "0"

PLUGINS_ROOT = WS / "MaiBot-main" / "plugins"
SAMPLE_DIR = PLUGINS_ROOT / "astrmai_runtime_sample"
PLUGIN_ID = "astrmai.runtime-sample"
TOOL = "compare_numbers"

from astrbot.core.provider.register import llm_tools  # noqa: E402

import importlib  # noqa: E402

plugin_mod = importlib.import_module("astrmai_plugin.main")
from astrmai.loader import LoadPolicy, load_maibot  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail else ""), flush=True)
    if not cond:
        FAILS.append(name)


def make_sample() -> None:
    source = PLUGINS_ROOT / "hello_world_plugin"
    if SAMPLE_DIR.exists():
        shutil.rmtree(SAMPLE_DIR)
    shutil.copytree(source, SAMPLE_DIR, ignore=shutil.ignore_patterns("__pycache__", "webui.json"))
    manifest = json.loads((SAMPLE_DIR / "_manifest.json").read_text(encoding="utf-8"))
    manifest["id"] = PLUGIN_ID
    manifest["name"] = "astrmai 运行时加载样本"
    manifest.pop("webui", None)
    (SAMPLE_DIR / "_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (SAMPLE_DIR / "config.toml").write_text('[plugin]\nenabled = false\nconfig_version = "2.0.0"\n', encoding="utf-8")


async def mb_plugin_tool_names() -> set[str]:
    """麦麦对插件工具的权威视图（PluginToolProvider 每轮就是查它）。

    不去数 heartflow 运行时：没有会话时那里本来就是空的，量出来只会得到假阴性。
    """
    from src.plugin_runtime.component_query import component_query_service

    return set(component_query_service.get_llm_available_tool_specs(None).keys())


async def main() -> int:
    if not (PLUGINS_ROOT / "hello_world_plugin").exists():
        print("SKIP: 没有样本来源")
        return 2
    star = None
    try:
        from types import SimpleNamespace

        class FakeContext(SimpleNamespace):
            def get_llm_tool_manager(self):
                return llm_tools

            def add_llm_tools(self, *tools):
                llm_tools.func_list.extend(tools)

            def get_config(self, umo=None):
                return {}

        star = plugin_mod.Main(FakeContext(), None)
        await star.initialize()
        for _ in range(90):
            if star.booted or star.boot_error:
                break
            await asyncio.sleep(1)
        if star.boot_error:
            print("BOOT FAILED:", star.boot_error)
            return 1

        # 启动时样本插件还不存在
        make_sample()  # 只是放文件，运行时还没加载它
        before_mb = await mb_plugin_tool_names()
        check(
            "启动后新插件默认不在工具表里",
            TOOL not in before_mb,
            f"麦麦插件工具 {len(before_mb)} 个，含 {TOOL}? {TOOL in before_mb}",
        )

        res = await load_maibot(PLUGIN_ID, LoadPolicy(allow_deps=False))
        check(
            "运行时加载成功（按组件登记表判定，不信布尔值）",
            bool(res.get("ok")),
            f"path={res.get('path')} attempts={res.get('attempts')} components={res.get('components')}",
        )

        after_mb: set[str] = set()
        for _ in range(20):
            after_mb = await mb_plugin_tool_names()
            if TOOL in after_mb:
                break
            await asyncio.sleep(2)
        check("麦麦插件工具视图运行时出现新工具", TOOL in after_mb, f"含 compare? {TOOL in after_mb}｜共 {len(after_mb)} 个")

        summary = star._sync_maibot_tools()
        ab_after = {t.name for t in llm_tools.func_list}
        check(
            "AstrBot 侧随后也拿到它",
            f"mai_{TOOL}" in ab_after and summary.get("added", 0) >= 1,
            f"新增 {summary.get('added')} 个，mai_compare_numbers 在表里={f'mai_{TOOL}' in ab_after}",
        )
        check("没有把未加载的东西算进插件来源", summary.get("plugin_sourced", 0) >= 1, str(summary))
    finally:
        if star is not None:
            try:
                await star.terminate()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(SAMPLE_DIR, ignore_errors=True)
        print("已移除运行时样本插件目录")

    print("\n结果:", "全部通过" if not FAILS else f"{len(FAILS)} 项失败 {FAILS}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
