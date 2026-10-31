"""MaiBot SessionMessage -> OneBot v11 send request."""

from typing import Any


def components_to_segments(message: Any) -> list[dict]:
    """Render MaiBot's internal components as OneBot v11 segments."""

    segments: list[dict] = []
    for comp in getattr(message.raw_message, "components", []) or []:
        kind = getattr(comp, "format_name", "")
        if kind == "text":
            text = str(getattr(comp, "text", ""))
            if text:
                segments.append({"type": "text", "data": {"text": text}})
        elif kind in {"image", "emoji"}:
            payload = getattr(comp, "binary_data", None)
            if payload:
                import base64

                key = "emoji" if kind == "emoji" else "image"
                if kind == "emoji":
                    segments.append({"type": "emoji", "data": base64.b64encode(payload).decode()})
                else:
                    segments.append({"type": "image", "data": {"file": f"base64://{base64.b64encode(payload).decode()}"}})
        elif kind == "at":
            segments.append({"type": "at", "data": {"qq": str(getattr(comp, "at_user_id", ""))}})
        elif kind == "reply":
            segments.append({"type": "reply", "data": {"id": str(getattr(comp, "target_message_id", ""))}})
        elif kind == "voice":
            payload = getattr(comp, "binary_data", None)
            if payload:
                import base64

                segments.append({"type": "record", "data": {"file": f"base64://{base64.b64encode(payload).decode()}"}})
        else:
            text = getattr(comp, "text", None)
            if text:
                segments.append({"type": "text", "data": {"text": str(text)}})
    return segments


def resolve_target(message: Any) -> tuple[str, str]:
    """Return ("group"|"private", target_id) for one outbound SessionMessage.

    In private chats MaiBot puts the bot itself in user_info, so the real recipient only lives in
    additional_config["platform_io_target_user_id"] (same rule the legacy WebUI sender uses).
    """

    info = message.message_info
    group_info = getattr(info, "group_info", None)
    if group_info is not None and getattr(group_info, "group_id", ""):
        return "group", str(group_info.group_id)
    extra = getattr(info, "additional_config", None) or {}
    target = extra.get("platform_io_target_user_id")
    if target:
        return "private", str(target)
    user_info = getattr(info, "user_info", None)
    if user_info is not None and getattr(user_info, "user_id", ""):
        return "private", str(user_info.user_id)
    raise ValueError("无法确定出站目标：既没有 group_id 也没有 target_user_id")
