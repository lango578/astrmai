"""Round trip: OneBot event in -> brain -> OneBot send out, all inside one asyncio loop."""

import asyncio
import json
import sys
import time
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))

from astrmai.config import AstrmaiConfig  # noqa: E402
from astrmai.gate import decide  # noqa: E402
from astrmai.host import MaiBotHost  # noqa: E402

SENT: list[dict] = []


async def sink(chat_type: str, target_id: str, segments: list[dict]) -> str:
    SENT.append({"chat_type": chat_type, "target_id": target_id, "segments": segments, "at": time.time()})
    return f"sent-{len(SENT)}"


def group_event(text: str, *, msg_id: str = "900001") -> dict:
    return {
        "time": int(time.time()),
        "self_id": "10001",
        "post_type": "message",
        "message_type": "group",
        "message_id": msg_id,
        "group_id": 12345,
        "user_id": 777,
        "message": [
            {"type": "text", "data": {"text": text}},
        ],
        "sender": {"card": "测试群友", "nickname": "测试群友", "group_name": "astrmai靶场"},
    }


async def main() -> int:
    cfg = AstrmaiConfig(
        mai_root=WS / "MaiBot-main",
        platform="qq",
        account_id="10001",
    )
    host = MaiBotHost(cfg, sink)
    await host.boot()
    print(f"boot ok in {host.boot_ms:.0f} ms", flush=True)

    # gate sanity first: commands stay with AstrBot, chatter goes to the brain
    assert decide("/help", is_group=True).owner == "astrbot"
    assert decide("今天好吵", is_group=True).owner == "maibot"
    assert decide("", is_group=True).owner == "none"
    assert decide("在吗", is_group=False).owner == "maibot"
    print("gate ok", flush=True)

    accepted = await host.deliver(group_event(" astrmai 整合测试，随便说一句 "))
    print(f"deliver accepted={accepted}", flush=True)
    assert accepted

    deadline = time.time() + 120
    while time.time() < deadline and not SENT:
        await asyncio.sleep(1)

    report = await host.shutdown()
    print("shutdown report:", json.dumps(report, ensure_ascii=False), flush=True)

    if not SENT:
        print("RESULT: no outbound send within 120s")
        return 1
    print("RESULT: outbound =", json.dumps(SENT[0], ensure_ascii=False)[:400])
    bad = [k for k, v in report.items() if v.startswith("FAIL")]
    return 0 if not bad else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
