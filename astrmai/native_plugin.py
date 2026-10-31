"""原生插件的加载与双端注册。

同一份 NativeTool 会：
  1) 以 `nm_<插件>_<工具>` 的名字进 AstrBot 的 FunctionTool 表；
  2) 以同样的名字经联邦镜像进麦麦的 planner 工具表。
名字两边一致，日志和排查不用换算。
"""

import importlib.util
import re
import tomllib
from pathlib import Path
from typing import Any

from .sdk import NativePlugin, NativeTool, ToolContext

NAME_RE = re.compile(r"[^0-9A-Za-z_.]+")


def _safe(value: str) -> str:
    return NAME_RE.sub("_", str(value).strip())


def qualified(plugin_name: str, tool_name: str) -> str:
    return f"nm_{_safe(plugin_name)}_{_safe(tool_name)}"


class NativeRegistry:
    def __init__(self, host: Any) -> None:
        self.host = host
        self.plugins: dict[str, NativePlugin] = {}
        self.tools: dict[str, tuple[NativePlugin, NativeTool]] = {}

    # ---------- 发现与加载 ----------

    @staticmethod
    def manifest(path: Path) -> dict:
        try:
            return tomllib.loads((path / "astrmai.toml").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def discover(self, root: Path) -> list[Path]:
        if not root.exists():
            return []
        out = []
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and (entry / "astrmai.toml").exists() and (entry / "main.py").exists():
                out.append(entry)
        return out

    def load_dir(self, path: Path) -> NativePlugin:
        manifest = self.manifest(path)
        module_name = f"astrmai_native_{_safe(path.name)}"
        spec = importlib.util.spec_from_file_location(module_name, path / "main.py")
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法导入插件入口: {path / 'main.py'}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        plugin = getattr(module, "PLUGIN", None)
        if plugin is None and callable(getattr(module, "build", None)):
            plugin = module.build()
        if not isinstance(plugin, NativePlugin):
            raise RuntimeError(f"{path.name} 必须暴露 PLUGIN（或 build()）并继承 NativePlugin")
        # 清单优先于类属性，方便作者只改 toml
        plugin.name = str(manifest.get("name") or plugin.name or path.name)
        plugin.version = str(manifest.get("version") or plugin.version)
        plugin.author = str(manifest.get("author") or plugin.author)
        plugin.description = str(manifest.get("description") or plugin.description)
        return plugin

    def bootstrap_all(self, root: Path) -> dict:
        """加载目录下所有原生插件并注册到两边。返回诊断摘要。"""

        summary = {"loaded": [], "failed": {}, "tools": 0}
        for path in self.discover(root):
            try:
                plugin = self.load_dir(path)
                self.plugins[plugin.name] = plugin
                for tool in plugin.tools():
                    self.tools[qualified(plugin.name, tool.name)] = (plugin, tool)
                summary["loaded"].append(f"{plugin.name}@{plugin.version}")
            except Exception as exc:  # noqa: BLE001 - 单个插件坏掉不能拖垮别人
                summary["failed"][path.name] = f"{type(exc).__name__}: {exc}"
        summary["tools"] = len(self.tools)
        return summary

    # ---------- 两侧共用的枚举 ----------

    def list_specs(self) -> list[dict]:
        out = []
        for full_name, (plugin, tool) in self.tools.items():
            out.append(
                {
                    "name": full_name.removeprefix("nm_"),
                    "description": f"[{plugin.name}] {tool.description}",
                    "parameters": tool.parameters,
                }
            )
        return out

    # ---------- 执行 ----------

    async def run(self, full_name: str, args: dict, ctx: ToolContext) -> str:
        entry = self.tools.get(full_name)
        if entry is None:
            # 允许麦麦侧带镜像前缀之外的裸名
            entry = self.tools.get(f"nm_{full_name}")
        if entry is None:
            return f"未找到 astrmai 原生工具: {full_name}"
        _, tool = entry
        return await tool.handler(dict(args or {}), ctx)

    def astrbot_function_tools(self) -> list:
        """生成可直接交给 Context.add_llm_tools 的 FunctionTool 列表。"""

        from astrbot.core.agent.tool import FunctionTool

        tools = []
        for full_name, (plugin, tool) in self.tools.items():
            tools.append(
                FunctionTool(
                    name=full_name,
                    description=f"[{plugin.name}] {tool.description}",
                    parameters=tool.parameters,
                    handler=self._make_ab_handler(full_name),
                )
            )
        return tools

    def _make_ab_handler(self, full_name: str):
        async def handler(event, **params):
            ctx = ToolContext(
                source="astrbot",
                chat_type="private" if event.is_private_chat() else "group",
                target_id=str(event.get_group_id() if not event.is_private_chat() else event.get_sender_id() or ""),
                sender_id=str(event.get_sender_id() or ""),
                text=event.get_message_str() or "",
                event=event,
                host=self.host,
            )
            return await self.run(full_name, dict(params), ctx)

        return handler

    def make_maibot_mirror(self, visible: bool = True):
        """给麦麦用的镜像 Provider（复用联邦机制，前缀 nm_）。"""

        from .federation import AstrBotToolMirror

        async def run_bare(name: str, args: dict, mb_context: Any) -> str:
            is_group = bool(getattr(mb_context, "is_group_chat", False))
            ctx = ToolContext(
                source="maibot",
                chat_type="group" if is_group else "private",
                target_id=str(getattr(mb_context, "group_id", "") or getattr(mb_context, "user_id", "") or ""),
                sender_id=str(getattr(mb_context, "user_id", "") or ""),
                host=self.host,
            )
            return await self.run(f"nm_{name}", args, ctx)

        return AstrBotToolMirror(
            list_tools=self.list_specs,
            run_tool=run_bare,
            visible=visible,
            prefix="nm_",
            provider_name="astrmai_native",
        )
