"""The Platform IO driver that makes AstrBot's connection MaiBot's mouth.

MaiBot types are resolved lazily because they only become importable once the host has put
MaiBot on sys.path, so the class is built by a factory rather than at module import time.
"""

from collections import deque
from typing import Any, Awaitable, Callable, Optional

Sink = Callable[[str, str, list[dict]], Awaitable[Optional[str]]]


def make_astrbot_driver(
    sink: Sink,
    *,
    platform: str,
    account_id: str = "",
    driver_id: str = "local.astrmai",
) -> Any:
    from src.platform_io import DeliveryReceipt, DeliveryStatus, DriverDescriptor, DriverKind
    from src.platform_io.drivers.base import PlatformIODriver

    from .outbound import components_to_segments, resolve_target

    class AstrBotDriver(PlatformIODriver):
        """Sends every MaiBot packet out through the host framework instead of a socket."""

        def __init__(self) -> None:
            super().__init__(
                DriverDescriptor(
                    driver_id=driver_id,
                    kind=DriverKind.LOCAL,
                    platform=platform,
                    account_id=account_id or None,
                )
            )
            self.sent: deque = deque(maxlen=200)  # 只是留证；计数走 sent_total，别让它无界涨（段里可能带 base64）

        async def send_message(
            self,
            message: Any,
            route_key: Any,
            metadata: Optional[dict] = None,
        ) -> DeliveryReceipt:
            chat_type, target_id = resolve_target(message)
            segments = components_to_segments(message)
            status = DeliveryStatus.FAILED
            error = ""
            platform_id = None
            if not segments:
                error = "no segments"
            else:
                try:
                    platform_id = await sink(chat_type, target_id, segments)
                    status = DeliveryStatus.SENT
                    self.sent.append(
                        {"chat_type": chat_type, "target_id": target_id, "segments": segments, "platform_id": platform_id}
                    )
                except Exception as exc:  # noqa: BLE001 - a failed send must not kill the brain
                    error = f"{type(exc).__name__}: {exc}"
            return DeliveryReceipt(
                internal_message_id=getattr(message, "message_id", ""),
                route_key=route_key,
                status=status,
                driver_id=self.driver_id,
                driver_kind=self.descriptor.kind,
                external_message_id=platform_id,
                error=error,
            )

    return AstrBotDriver()
