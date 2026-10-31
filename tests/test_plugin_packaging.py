"""市场插件包的加载语义回归：必须按 AstrBot 自己的方式导入才算数。

AstrBot 的 star_manager 是 `__import__("data.plugins.<名>.main")`，而且**只把项目根放进 sys.path**
（插件目录不进）。所以"在仓库里能跑"和"装进 data/plugins 能跑"是两件事，这个测试盯的就是后者。
"""

import asyncio
import importlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))
sys.path.insert(0, str(WS / "vendor" / "AstrBot-master"))

PKG_DIR = WS / "dist" / "astrbot-plugin-astrmai"
PKG_ZIP = WS / "dist" / "astrbot-plugin-astrmai.zip"
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail else ""), flush=True)
    if not cond:
        FAILS.append(name)


class FakeContext:
    def __init__(self) -> None:
        self._tools: dict = {}

    def get_llm_tool_manager(self):
        return None

    def add_llm_tools(self, *tools):
        self._tools.setdefault("added", []).extend(tools)


def ensure_package() -> None:
    # 每次都重建：测的必须是当前源码的产物，不然改完代码还在测旧包（假绿）
    import subprocess

    subprocess.run([sys.executable, str(WS / "tools" / "build_plugin.py"), "--no-zip"], cwd=WS, check=True)


def main() -> int:
    ensure_package()
    saved_modules = {k: v for k, v in sys.modules.items() if k == "astrmai" or k.startswith("astrmai.")}
    saved_env = {k: os.environ.get(k) for k in ("ASTRMAI_MAI_ROOT", "ASTRMAI_PANEL", "ASTRMAI_PANEL_HOST", "ASTRMAI_PANEL_PORT")}

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        plugin_dir = root / "data" / "plugins" / "astrmai"
        shutil.copytree(PKG_DIR, plugin_dir, ignore=shutil.ignore_patterns("__pycache__"))

        # 1) 包内容硬约束：不替用户 pip、不带上游与本机状态
        names = {p.relative_to(plugin_dir).as_posix() for p in plugin_dir.rglob("*") if p.is_file()}
        check("包里没有 requirements.txt（不静默替用户 pip）", "requirements.txt" not in names, str(sorted(n for n in names if "require" in n)))
        check("包里没有上游源码/本机状态", not any(n.startswith(("MaiBot-main", "vendor", "astrmai_state", "secrets")) for n in names))
        check("桥接包在插件目录内（市场布局）", "astrmai/config.py" in names and "astrmai/panel_static/app.js" in names)
        check("metadata 与配置 schema 齐", {"metadata.yaml", "_conf_schema.json", "main.py", "LICENSE"} <= names)

        # 2) 按 star_manager 的点号名导入：这是与"仓库里能跑"不同的另一条路
        sys.path.insert(0, str(root))
        try:
            mod = importlib.import_module("data.plugins.astrmai.main")
            check("以 data.plugins.astrmai.main 导入成功", hasattr(mod, "Main"))
        except Exception as exc:  # noqa: BLE001
            check("以 data.plugins.astrmai.main 导入成功", False, f"{type(exc).__name__}: {exc}")
            return 1
        finally:
            sys.path.remove(str(root))

        check("MARKET_LAYOUT 被识别为 True", getattr(mod, "MARKET_LAYOUT", False) is True)
        aliased = sys.modules.get("astrmai")
        check(
            "astrmai 顶层名指向插件目录内那份（不是仓库里那份）",
            aliased is not None and str(Path(aliased.__file__).resolve()).startswith(str(plugin_dir.resolve())),
            str(getattr(aliased, "__file__", ""))[:90],
        )

        # 3) 状态目录必须落在 plugin_data：插件目录会被更新流程整个删掉重建
        state = Path(str(mod.STATE_DIR))
        check("状态目录落在 data/plugin_data/astrmai", state == (root / "data" / "plugin_data" / "astrmai").resolve() or state.as_posix().endswith("plugin_data/astrmai"), str(state))
        check("状态目录不在插件目录里（否则更新会被清空）", plugin_dir not in state.parents, str(state))

        # 4) 配置优先级链：环境变量 > 面板配置 > 默认值（填了没人读=假开关）
        probe = mod.Main(FakeContext(), {"mai_root": "/from/panel", "account_id": "111"})
        check("面板里的 mai_root 真被读到", str(probe._pref("mai_root", "ASTRMAI_MAI_ROOT", "/default")) == "/from/panel")
        os.environ["ASTRMAI_MAI_ROOT"] = "/from/env"
        check("环境变量赢过面板配置", str(probe._pref("mai_root", "ASTRMAI_MAI_ROOT", "/default")) == "/from/env")
        del os.environ["ASTRMAI_MAI_ROOT"]
        check(
            "两者都没有时用默认值",
            str(mod.Main(FakeContext(), {})._pref("mai_root", "ASTRMAI_MAI_ROOT", "/default")) == "/default",
        )

        # 5) schema 里每个字段都必须有运行时读取点，否则不该出现在面板上
        src = (plugin_dir / "main.py").read_text(encoding="utf-8")
        schema = json.loads((plugin_dir / "_conf_schema.json").read_text(encoding="utf-8"))
        orphans = [k for k in schema if f'_pref("{k}"' not in src]
        check("配置面板上没有装饰性字段（每个字段都有读取点）", not orphans, f"没人读: {orphans}")

        # 6) 面板开关/端口真的会传到面板实现
        import astrmai.panel as panel_mod

        seen: dict = {}
        real_start = panel_mod.AstrmaiPanel.start

        async def fake_start(self, host=None, port=None):  # noqa: ANN001
            seen.update(enable=os.environ.get("ASTRMAI_PANEL"), host=host, port=port)

        panel_mod.AstrmaiPanel.start = fake_start
        try:
            for key in ("ASTRMAI_PANEL", "ASTRMAI_PANEL_HOST", "ASTRMAI_PANEL_PORT"):
                os.environ.pop(key, None)
            host = mod.Main(FakeContext(), {"panel_enable": True, "panel_host": "127.0.0.1", "panel_port": 18999})
            asyncio.run(host._start_panel())
            check("panel_host/port 作为参数真传进了面板启动", seen.get("port") == 18999 and seen.get("host") == "127.0.0.1", str(seen))
            check("面板不把自己的选择写回环境变量", os.environ.get("ASTRMAI_PANEL") is None, str(os.environ.get("ASTRMAI_PANEL")))
            seen.clear()
            off = mod.Main(FakeContext(), {"panel_enable": False})
            asyncio.run(off._start_panel())
            check("panel_enable=False 时面板不起（第二次也关得掉）", not seen, str(seen))
        finally:
            panel_mod.AstrmaiPanel.start = real_start

        # 7) 没放麦麦本体时必须说人话（市场安装的第一次失败一定是这个）
        from astrmai.config import AstrmaiConfig
        from astrmai.host import prepare_paths

        try:
            prepare_paths(AstrmaiConfig(mai_root=root / "nowhere"))
            check("缺麦麦时报错可照做", False, "居然没报错")
        except FileNotFoundError as exc:
            text = str(exc)
            check(
                "缺麦麦时报错可照做（说清去哪儿拿、放哪、怎么用配置指）",
                "v1.3.18" in text and "mai_root" in text and "src/" in text,
                text[:90],
            )

        # 8) zip 体积守官方市场 16MB 上限
        if PKG_ZIP.exists():
            size = PKG_ZIP.stat().st_size
            check(f"zip 体积在上限内（{size/1024:.0f} KB / 16 MB）", size < 16 * 1024 * 1024)
            with zipfile.ZipFile(PKG_ZIP) as zf:
                tops = {n.split("/")[0] for n in zf.namelist()}
            check("zip 解出来是一个插件目录（不是散文件）", tops == {"astrmai"}, str(sorted(tops))[:60])

    for name in list(sys.modules):
        if name.startswith("data.plugins.astrmai") or name == "astrmai" or name.startswith("astrmai."):
            del sys.modules[name]
    sys.modules.update(saved_modules)
    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    print("\n%d failure(s)" % len(FAILS), FAILS)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
