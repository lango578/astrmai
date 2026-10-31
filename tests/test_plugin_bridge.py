"""Drive the astrmai AstrBot plugin with a duck-typed event and a fake Context.

AstrBot's MessageChain/Plain/At/Reply components are the real classes; only the event/platform
plumbing is faked, so segment conversion and the arbitration path are genuinely exercised.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))
sys.path.insert(0, str(WS / "vendor" / "AstrBot-master"))
os.environ["ASTRMAI_WORKSPACE"] = str(WS)
os.environ["ASTRMAI_ACCOUNT_ID"] = "10001"
os.environ["ASTRMAI_ALLOWED_USERS"] = "777"
os.environ["ASTRMAI_ALLOW_GROUPS"] = "1"

from astrbot.core.message.components import At, Plain, Reply  # noqa: E402

import importlib  # noqa: E402

plugin_mod = importlib.import_module("astrmai_plugin.main")


class FakeMessageObj:
    def __init__(self, comps, raw):
        self.message = comps
        self.raw_message = raw


class FakeEvent:
    def __init__(self, comps, raw, *, group=True, stopped=False, sender_id="777"):
        self.message_obj = FakeMessageObj(comps, raw)
        self._group = group
        self._stopped = stopped
        self._sender_id = sender_id
        self.stopped_called = False

    def is_stopped(self):
        return self._stopped

    def stop_event(self):
        self.stopped_called = True

    def is_private_chat(self):
        return not self._group

    def get_self_id(self):
        return "10001"

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return "12345"

    def get_message_str(self):
        return " ".join(str(getattr(c, "text", "")) for c in self.message_obj.message if isinstance(c, Plain)).strip()

    @property
    def unified_msg_origin(self):
        return "aiocqhttp:Group:12345" if self._group else "aiocqhttp:Friend:777"


class FakeContext:
    def __init__(self):
        self.sends = []

    def get_llm_tool_manager(self):
        from astrbot.core.provider.register import llm_tools

        return llm_tools

    def add_llm_tools(self, *tools):
        from astrbot.core.provider.register import llm_tools

        llm_tools.func_list.extend(tools)

    async def send_message(self, session, chain):
        self.sends.append({"session": str(session), "chain": chain})
        return True


def onebot_raw(text, msg_id="9001"):
    return {
        "time": int(time.time()),
        "self_id": 10001,
        "post_type": "message",
        "message_type": "group",
        "message_id": msg_id,
        "group_id": 12345,
        "user_id": 777,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"card": "测试群友", "nickname": "测试群友", "group_name": "astrmai靶场"},
    }


async def drain(handler, event):
    gen = handler(event)
    async for _ in gen:
        pass


async def main() -> int:
    ctx = FakeContext()
    star = plugin_mod.Main(ctx, None)
    await star.initialize()
    for _ in range(90):
        if star.booted or star.boot_error:
            break
        await asyncio.sleep(1)
    if star.boot_error:
        print("BOOT FAILED:", star.boot_error)
        return 1
    if not star.booted:
        print("BOOT TIMED OUT")
        return 1
    print(f"plugin boot ok ({star.host.boot_ms:.0f} ms)", flush=True)

    # 1) a command stays with AstrBot and never reaches the brain
    cmd = FakeEvent([Plain(text="/help")], onebot_raw("/help"))
    await drain(star.to_maibot, cmd)
    assert not cmd.stopped_called and star.forwarded == 0, "命令不该被麦麦抢走"
    print("command passthrough ok", flush=True)

    # 2) already-consumed messages are not forwarded twice
    consumed = FakeEvent([Plain(text="随便聊聊")], onebot_raw("随便聊聊"), stopped=True)
    await drain(star.to_maibot, consumed)
    assert star.forwarded == 0, "已停止的事件不该再转发"
    print("stopped-event guard ok", flush=True)

    # 3) natural chatter goes to the brain and AstrBot is silenced
    chat = FakeEvent([Plain(text="麦麦你好，说句话")], onebot_raw("麦麦你好，说句话"))
    await drain(star.to_maibot, chat)
    assert star.forwarded == 1 and chat.stopped_called, "群聊应转给麦麦并抑制 AstrBot 回复"
    print("chatter forwarded + AstrBot silenced", flush=True)

    # 4) 硬白名单：陌生人的私聊既不转发也不抑制，astrmai 对它彻底沉默
    before = star.forwarded
    stranger = FakeEvent(
        [Plain(text="在吗，帮我个忙")],
        onebot_raw("在吗，帮我个忙", "9002"),
        group=False,
        sender_id="999",
    )
    await drain(star.to_maibot, stranger)
    assert star.forwarded == before and not stranger.stopped_called, "白名单外的私聊不该被麦麦接手"
    print("allowlist silence ok (stranger private ignored)", flush=True)

    # 5) /mai 的管理员闸门：名单空着绝不等于"谁都是管理员"（第三方复检 H1）
    async def mai_text(sender: str, rest: str) -> str:
        raw = onebot_raw(f"/mai {rest}", "9003")
        ev = FakeEvent([Plain(text=f"/mai {rest}")], raw, group=True, sender_id=sender)
        out: list[str] = []
        async for item in star.mai(ev, rest):
            out.append(str(item))
        return " ".join(out)

    os.environ.pop("ASTRMAI_ADMINS", None)
    saved_allow = set(star.host.cfg.allowed_users)
    got = await mai_text("777", "status")
    assert "只对管理员" not in got and "还没配管理员" not in got, f"白名单里的管理员该放行：{got[:100]}"
    print("/mai admin allowed", flush=True)
    got = await mai_text("999", "install astrbot whatever")
    assert "只对管理员" in got, f"非管理员该被拒：{got[:100]}"
    print("/mai non-admin refused", flush=True)
    star.host.cfg.allowed_users = set()
    try:
        got = await mai_text("999", "install astrbot whatever")
        assert "还没配管理员" in got, f"名单为空必须拒绝并说明怎么配：{got[:120]}"
        print("/mai fail-closed when no admin configured", flush=True)
    finally:
        star.host.cfg.allowed_users = saved_allow

    deadline = time.time() + 120
    while time.time() < deadline and not ctx.sends:
        await asyncio.sleep(1)

    runner_pids = list(star.host.runner_pids) if star.host else []

    def alive(pids: list[int]) -> list[int]:
        out = []
        for pid in pids:
            try:
                os.kill(pid, 0)
            except OSError:
                continue
            out.append(pid)
        return out

    print(f"runner children tracked at boot: {runner_pids}", flush=True)
    await star.terminate()
    await asyncio.sleep(2)
    leftover = alive(runner_pids)
    print(f"live runners after terminate: {leftover or 'none'}", flush=True)

    if not ctx.sends:
        print("RESULT: no outbound through AstrBot context")
        return 1
    sent = ctx.sends[0]
    kinds = [type(c).__name__ for c in sent["chain"].chain]
    texts = [getattr(c, "text", "") for c in sent["chain"].chain]
    print(f"RESULT: session={sent['session']} components={kinds} text={texts}", flush=True)
    if leftover:
        print(f"RESULT: {len(leftover)} runner subprocess(es) survived terminate")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
