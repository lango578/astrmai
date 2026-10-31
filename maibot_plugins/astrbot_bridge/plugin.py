"""astrbot_bridge：让麦麦（MaiBot）这一侧也能用 AstrBot 的插件工具。

方向与 astrmai 相反：astrmai 是把麦麦塞进 AstrBot 进程；这个插件是给**纯麦麦用户**用的，
它不嵌 AstrBot，而是连一个跑着 astrmai 控制台的 AstrBot 实例，把对面的工具列出来、按需调用。

为什么走 HTTP 而不是就地嵌 AstrBot：麦麦的插件跑在独立的 runner 子进程里，
在那里再拉一套 AstrBot 核心意味着第二个数据库、第二份配置、还得给它一个平台连接 —— 得不偿失。
runner 是常驻的，所以一个 httpx 客户端可以跨调用活着。
"""

import json
from typing import Any

import httpx
from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType


class BridgeSection(PluginConfigBase):
    """连接参数。"""

    base_url: str = Field(
        default="http://127.0.0.1:18124/astrmai/api",
        description="astrmai 控制台的 API 前缀（默认只听本机 18124）",
    )
    token: str = Field(
        default="",
        description="控制台机器 token：在 AstrBot 那边设 ASTRMAI_PANEL_TOKEN 后把同一个值填进来",
    )
    target: str = Field(
        default="",
        description="默认执行会话（群号或 QQ 号）；AstrBot 的工具处理器要靠它找回平台连接",
    )
    timeout_seconds: int = Field(default=30, description="单次请求超时（秒）")


class PluginSection(PluginConfigBase):
    """SDK 强制要求的一节：没有 plugin.config_version 就拒绝加载。"""

    enabled: bool = Field(default=True, description="是否启用本插件")
    config_version: str = Field(default="1.0.0", description="配置版本（升级时用于迁移）")


class AstrBotBridgeConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    bridge: BridgeSection = Field(default_factory=BridgeSection)


class AstrBotBridge(MaiBotPlugin):
    """把 AstrBot 侧工具映射成麦麦的 LLM 工具。"""

    # 不声明这行的话 self.config 会在第一次调用时抛"未声明 config_model"
    config_model = AstrBotBridgeConfig

    async def on_load(self) -> None:
        self._client = httpx.AsyncClient(timeout=self.config.bridge.timeout_seconds)

    async def on_unload(self) -> None:
        client = getattr(self, "_client", None)
        if client is not None:
            await client.aclose()

    # ---------------- 内部 ----------------

    def _headers(self) -> dict[str, str]:
        token = str(self.config.bridge.token or "").strip()
        return {"X-Astrmai-Token": token} if token else {}

    async def _get(self, path: str) -> dict[str, Any]:
        url = f"{str(self.config.bridge.base_url).rstrip('/')}/{path}"
        response = await self._client.get(url, headers=self._headers())
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:160]}")
        return response.json()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{str(self.config.bridge.base_url).rstrip('/')}/{path}"
        response = await self._client.post(url, json=payload, headers=self._headers())
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:160]}")
        return response.json()

    @staticmethod
    def _fail(tool: str, exc: Exception) -> dict[str, str]:
        """出错要说清为什么，别回一句"失败了"让模型自己猜。"""

        text = str(exc)
        if "401" in text:
            hint = "对面 astrmai 控制台没认这个 token：检查 ASTRMAI_PANEL_TOKEN 与插件配置里的 token 是否一致"
        elif "403" in text:
            hint = "被拒：多半是对面还没改初始口令（改密前所有端点都不放行），或 Origin/token 校验没过"
        elif "ConnectError" in type(exc).__name__ or "timed out" in text.lower():
            hint = "连不上对面：确认 AstrBot 那边 astrmai 在跑、控制台端口对、base_url 填对"
        else:
            hint = "对面返回了错误，原样贴在下面"
        return {"name": tool, "content": f"AstrBot 侧调用失败（{hint}）：{text[:220]}"}

    # ---------------- 工具 ----------------

    @Tool(
        "astrbot_tools",
        description="列出对面 AstrBot 实例里可用的插件工具与命令（跨生态映射）。要调用之前先查这张表。",
        parameters=[],
        visibility="visible",
    )
    async def list_tools(self, **kwargs: Any) -> dict[str, str]:
        del kwargs
        try:
            data = await self._get("tools")
        except Exception as exc:  # noqa: BLE001
            return self._fail("astrbot_tools", exc)
        lines: list[str] = []
        for group in ("astrbot", "maibot", "astrbot_commands", "native"):
            items = data.get(group) or []
            if isinstance(items, list) and items:
                lines.append(f"[{group}] {len(items)} 个")
                for item in items[:40]:
                    if isinstance(item, dict):
                        name = str(item.get("name") or item.get("tool") or "")
                        desc = str(item.get("description") or item.get("desc") or "")[:70]
                        if name:
                            lines.append(f"  - {name}: {desc}")
        if not lines:
            lines = [json.dumps(data, ensure_ascii=False)[:1200]]
        return {"name": "astrbot_tools", "content": "\n".join(lines)[:4000]}

    @Tool(
        "astrbot_call",
        description="执行对面 AstrBot 实例里的一个工具（先用 astrbot_tools 查名字），把结果当文本返回。",
        parameters=[
            ToolParameterInfo(name="tool", param_type=ToolParamType.STRING, description="要执行的工具名", required=True),
            ToolParameterInfo(
                name="args_json",
                param_type=ToolParamType.STRING,
                description='参数，JSON 对象字符串，例如 {"city":"北京"}；无参数就填 {}',
                required=False,
            ),
            ToolParameterInfo(
                name="target",
                param_type=ToolParamType.STRING,
                description="执行会话（群号/QQ号）；留空则用插件配置里的默认 target",
                required=False,
            ),
        ],
        visibility="visible",
    )
    async def call_tool(
        self,
        tool: str = "",
        args_json: str = "{}",
        target: str = "",
        **kwargs: Any,
    ) -> dict[str, str]:
        del kwargs
        name = str(tool or "").strip()
        if not name:
            return {"name": "astrbot_call", "content": "没给工具名。先用 astrbot_tools 查一张表。"}
        try:
            args = json.loads(args_json or "{}")
            if not isinstance(args, dict):
                raise ValueError("args_json 必须是 JSON 对象")
        except (ValueError, TypeError) as exc:
            return {"name": "astrbot_call", "content": f"参数不是合法 JSON 对象：{exc}"}
        who = str(target or self.config.bridge.target or "").strip()
        if not who:
            return {
                "name": "astrbot_call",
                "content": "缺 target：AstrBot 的工具处理器需要一个会话上下文才能执行。传本次的群号/QQ号，或在插件配置里填默认 target。",
            }
        try:
            data = await self._post("tools/call", {"name": name, "args": args, "target": who})
        except Exception as exc:  # noqa: BLE001
            return self._fail("astrbot_call", exc)
        if not data.get("ok"):
            return {"name": "astrbot_call", "content": f"对面拒绝执行：{str(data.get('reason'))[:300]}"}
        return {"name": str(data.get("name") or name), "content": str(data.get("result") or "（无文本输出）")[:4000]}

    @Tool(
        "astrbot_status",
        description="看对面 AstrBot + 麦麦融合实例的存活状态（连接、闸门、转发计数）。排查用。",
        parameters=[],
        visibility="visible",
    )
    async def status(self, **kwargs: Any) -> dict[str, str]:
        del kwargs
        try:
            data = await self._get("overview")
        except Exception as exc:  # noqa: BLE001
            return self._fail("astrbot_status", exc)
        engine = (data or {}).get("engine") or {}
        return {
            "name": "astrbot_status",
            "content": json.dumps(
                {
                    "booted": engine.get("booted"),
                    "forwarded": engine.get("forwarded"),
                    "sent": engine.get("sent"),
                    "sessions": engine.get("sessions"),
                },
                ensure_ascii=False,
            ),
        }
