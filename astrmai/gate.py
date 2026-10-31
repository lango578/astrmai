"""Which brain gets a given message.

AstrBot owns explicit interaction (commands, plugin filters, direct @). MaiBot owns ambient
group chatter and private conversation. Anything claimed by AstrBot never reaches the brain, so
the two never answer the same message.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class Decision:
    owner: str  # "astrbot" | "maibot" | "none"
    reason: str

    @property
    def to_maibot(self) -> bool:
        return self.owner == "maibot"


def decide(
    text: str,
    *,
    is_group: bool,
    prefixes: tuple[str, ...] = ("/", "!"),
    mentioned_bot: bool = False,
    replied_to_bot: bool = False,
    plugin_claimed: bool = False,
    private_enabled: bool = True,
) -> Decision:
    stripped = (text or "").strip()

    if plugin_claimed:
        return Decision("astrbot", "已被 AstrBot 插件消费")
    if any(stripped.startswith(p) and len(stripped) > len(p) for p in prefixes):
        return Decision("astrbot", "命令前缀")
    if not stripped:
        return Decision("none", "空消息")
    if not is_group:
        return Decision("maibot", "私聊") if private_enabled else Decision("none", "私聊已关闭")
    if mentioned_bot or replied_to_bot:
        return Decision("maibot", "点名或引用机器人")
    return Decision("maibot", "群聊自然对话")
