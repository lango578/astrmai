"""OneBot v11 event -> MaiBot unified message dict.

MaiBot's own legacy converter (ChatBot.message_process) is the target contract, so segments are
emitted as maim_message Seg dicts: text/at/reply pass through, while image/emoji/voice must be
base64 bytes because the brain hashes them into its media store.
"""

import asyncio
import base64
import ipaddress
import os
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

SEG_TYPES = {"text", "image", "emoji", "voice", "file", "at", "reply", "dict"}

MEDIA_MAX_BYTES_DEFAULT = 8 * 1024 * 1024
_BLOCKED_HOSTNAMES = {"metadata", "metadata.google.internal"}


def _media_cap() -> int:
    raw = os.environ.get("ASTRMAI_MEDIA_MAX_BYTES", "").strip()
    try:
        return max(1024, int(raw)) if raw else MEDIA_MAX_BYTES_DEFAULT
    except ValueError:
        return MEDIA_MAX_BYTES_DEFAULT


def _media_roots() -> list[Path]:
    """本地媒体目录白名单。默认空 = 一个本地路径都不读。"""

    out: list[Path] = []
    for piece in os.environ.get("ASTRMAI_MEDIA_ROOTS", "").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            out.append(Path(piece).resolve())
        except OSError:
            continue
    return out


def _assert_reachable_host(host: Optional[str]) -> None:
    """挡掉链路本地/元数据服务：QQ 消息里的 URL 不该变成内网探测器。"""

    if not host:
        raise ValueError("URL 没有主机名")
    lowered = host.lower().rstrip(".")
    if lowered in _BLOCKED_HOSTNAMES:
        raise ValueError(f"拒绝访问元数据主机: {host}")
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        return  # 域名：交给 DNS，这里不做解析（解析后的检查由连接层兜）
    if ip.is_link_local or ip.is_reserved or ip.is_multicast:
        raise ValueError(f"拒绝访问链路本地/保留地址: {host}")


def _local_b64(url: str) -> str:
    roots = _media_roots()
    target = Path(url.removeprefix("file://")).resolve()
    if not roots:
        raise PermissionError("默认不读本地路径；确实需要就把目录写进 ASTRMAI_MEDIA_ROOTS")
    if not any(target == root or target.is_relative_to(root) for root in roots):
        raise PermissionError(f"本地媒体路径不在白名单目录里: {target}")
    size = target.stat().st_size
    if size > _media_cap():
        raise ValueError(f"媒体太大（{size} > {_media_cap()} 字节）")
    return base64.b64encode(target.read_bytes()).decode()


def _b64_from_url(url: str, timeout: float) -> Optional[str]:
    """取回一段媒体并 base64。只走 http/https，流式读，带大小上限。"""

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        # 曾经这里直接 open()：file:// 与 X:\\ 让一条 QQ 消息就能读进程可读的任意文件
        return _local_b64(url)
    _assert_reachable_host(parsed.hostname)
    import httpx

    cap = _media_cap()
    chunks: list[bytes] = []
    read = 0
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            _assert_reachable_host(urlparse(str(response.url)).hostname)  # 跳转后也要再过一遍
            for piece in response.iter_bytes():
                read += len(piece)
                if read > cap:
                    raise ValueError(f"媒体超过上限（>{cap} 字节）")
                chunks.append(piece)
    return base64.b64encode(b"".join(chunks)).decode()


def _one_segment(raw: dict, cfg_fetch: Callable[[str], Optional[str]]) -> Optional[dict]:
    seg_type = (raw.get("type") or "").strip().lower()
    data = raw.get("data") or {}
    if seg_type == "text":
        return {"type": "text", "data": str(data.get("text", ""))}
    if seg_type == "at":
        target = data.get("qq") or data.get("user_id") or data.get("target_user_id")
        return {"type": "at", "data": {"target_user_id": str(target or ""), "display": str(data.get("name") or "")}}
    if seg_type == "reply":
        return {"type": "reply", "data": str(data.get("id", ""))}
    if seg_type in {"image", "face", "emoji", "record", "voice", "video", "file"}:
        kind = {"face": "emoji", "record": "voice", "video": "file"}.get(seg_type, seg_type)
        if kind == "file":
            return {"type": "dict", "data": {"type": "file", "data": data}}
        payload = data.get("base64")
        if not payload:
            url = data.get("url") or data.get("file") or data.get("path") or ""
            if not url:
                return None
            try:
                payload = cfg_fetch(str(url))
            except Exception:
                return None
        return {"type": kind, "data": payload} if payload else None
    if seg_type == "unknown":
        return None
    return {"type": "dict", "data": {"type": seg_type, "data": data}} if seg_type in SEG_TYPES else None


def onebot_to_maim_dict(
    event: dict,
    *,
    platform: str = "qq",
    account_id: str = "",
    fetch_media: bool = True,
    media_timeout: float = 15.0,
) -> Optional[dict]:
    """Convert one OneBot v11 message event, or return None if nothing conveyable."""

    def fetch(url: str) -> Optional[str]:
        return _b64_from_url(url, media_timeout) if fetch_media and url else None

    raw_segments = event.get("message") or []
    if isinstance(raw_segments, str):
        raw_segments = [{"type": "text", "data": {"text": raw_segments}}]

    segments = [seg for seg in (_one_segment(item, fetch) for item in raw_segments) if seg]
    if event.get("message_type") == "private" and not any(s["type"] == "text" for s in segments):
        # A bare sticker to the bot still needs text or the brain sees an empty turn.
        segments.append({"type": "text", "data": "[图片]"})
    if not segments:
        return None

    group_id = event.get("group_id")
    sender = event.get("sender") or {}
    message_info: dict[str, Any] = {
        "platform": platform,
        "message_id": str(event.get("message_id")),
        "time": float(event.get("time") or 0) or None,
        "user_info": {
            "platform": platform,
            "user_id": str(event.get("user_id")),
            "user_nickname": str(sender.get("nickname") or sender.get("card") or event.get("user_id")),
            "user_cardname": str(sender.get("card") or ""),
        },
    }
    if group_id:
        message_info["group_info"] = {
            "platform": platform,
            "group_id": str(group_id),
            "group_name": str(sender.get("group_name") or group_id),
        }
    if message_info["time"] is None:
        import time as _t

        message_info["time"] = _t.time()

    return {
        "account_id": str(account_id or event.get("self_id") or ""),
        "message_info": message_info,
        "message_segment": {"type": "seglist", "data": segments},
        "raw_message": event,
    }


async def onebot_to_maim_dict_async(event: dict, **kwargs: Any) -> Optional[dict]:
    """Event-loop-safe conversion: media fetches are real blocking I/O and the two brains share one loop."""

    return await asyncio.to_thread(onebot_to_maim_dict, event, **kwargs)
