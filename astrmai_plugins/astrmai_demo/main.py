"""astrmai 原生插件示例。"""

from astrmai.sdk import NativePlugin, NativeTool, ToolContext


async def who_am_i(args: dict, ctx: ToolContext) -> str:
    return f"这次调用来自 {ctx.source}，会话 {ctx.chat_type}:{ctx.target_id}，发送者 {ctx.sender_id or '未知'}"


async def echo(args: dict, ctx: ToolContext) -> str:
    return f"回声（{ctx.source}）: {args.get('text', '')}"


class DemoPlugin(NativePlugin):
    name = "astrmai_demo"
    version = "0.1.0"
    description = "证明一次开发、两边可用"

    def tools(self) -> list[NativeTool]:
        return [
            NativeTool(
                name="who_am_i",
                description="告诉模型这次是谁在调它（AstrBot 还是麦麦）",
                handler=who_am_i,
            ),
            NativeTool(
                name="echo",
                description="原样回声，用于验证调用链",
                handler=echo,
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string", "description": "要回声的文字"}},
                    "required": ["text"],
                },
            ),
        ]


PLUGIN = DemoPlugin()
