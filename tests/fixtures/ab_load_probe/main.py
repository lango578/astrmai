"""Fixture star used by the runtime-load evidence test: one command, one LLM tool.

Nothing here touches the network or QQ; it only proves that a directory dropped into
data/plugins can be loaded into a *running* AstrBot and immediately answer.
"""

from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register


@register("ab_load_probe", "astrmai 加载取证插件", "1.0.0", "astrmai")
class AbLoadProbe(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("abloadprobe")
    async def abloadprobe(self, event):
        yield event.plain_result("ABLOAD-OK")

    @filter.llm_tool(name="probe_echo")
    async def probe_echo(self, event, text: str):
        """回显一段文本，用于确认插件工具进入了 AstrBot 的工具表。

        Args:
            text(string): 要回显的文本
        """
        return f"probe:{text}"
