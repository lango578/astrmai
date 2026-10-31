"""astrmai 原生插件 SDK —— 写一次，两个脑子都能用。

一个原生插件 = 一个目录：astrmai.toml（清单） + main.py（暴露 PLUGIN）。
它的工具会同时注册进 AstrBot 的 function-calling 工具表和麦麦的 planner 工具表，
handler 拿到的 ToolContext 会说明这次调用是从哪一边进来的。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

HOSTS = ("astrbot", "maibot")


@dataclass(slots=True)
class ToolContext:
    """一次工具调用的会话背景。"""

    source: str  # "astrbot" | "maibot"
    chat_type: str  # "group" | "private"
    target_id: str
    sender_id: str = ""
    text: str = ""
    event: Any = None  # AstrBot 事件（source=astrbot 时有）
    host: Any = None  # astrmai 插件实例，可用于发送/查询

    @property
    def is_group(self) -> bool:
        return self.chat_type == "group"


@dataclass(slots=True)
class NativeTool:
    """一个原生工具。handler 是 `async (args: dict, ctx: ToolContext) -> str`。"""

    name: str
    description: str
    handler: Callable[[dict, ToolContext], Awaitable[str]]
    parameters: dict = field(
        default_factory=lambda: {"type": "object", "properties": {}, "required": []}
    )

    def qualify(self, plugin_name: str) -> str:
        return f"{plugin_name}.{self.name}"


class NativePlugin:
    """插件基类。子类给 name/version，并实现 tools()。"""

    name: str = "unnamed"
    version: str = "0.0.0"
    author: str = ""
    description: str = ""

    def tools(self) -> list[NativeTool]:  # pragma: no cover - 由子类实现
        return []

    async def on_load(self, host: Any) -> None:
        """插件被加载时调用，可选实现。"""

    async def on_unload(self) -> None:
        """插件被卸载时调用，可选实现。"""
