"""Runtime settings for the bridge, kept out of both frameworks' own config files."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class AstrmaiConfig:
    mai_root: Path
    platform: str = "qq"
    account_id: str = ""
    # MaiBot's transport and web surfaces must not compete with the host for ports.
    disable_maim_message: bool = True
    disable_webui: bool = True
    # Upstream beacons telemetry to a hardcoded third-party host; never in a merged build.
    neuter_telemetry: bool = True
    command_prefixes: tuple[str, ...] = ("/", "!")
    fetch_media: bool = True
    media_timeout: float = 15.0
    # Chats MaiBot is allowed to speak in. Empty = every chat the host forwards.
    groups: set[str] = field(default_factory=set)
    private_enabled: bool = True
    group_enabled: bool = True
    # Hard allowlist for private chats. Non-empty means strangers get silence, not a reply.
    allowed_users: set[str] = field(default_factory=set)

    def chat_allowed(self, chat_type: str, target_id: str) -> bool:
        """Whether this conversation may be answered at all."""

        if chat_type == "private":
            if not self.private_enabled:
                return False
            return not self.allowed_users or str(target_id) in self.allowed_users
        if not self.group_enabled:
            return False
        return not self.groups or str(target_id) in self.groups
