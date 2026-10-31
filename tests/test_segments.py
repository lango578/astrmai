"""Pure-function tests for the two segment converters, no engine, no network."""

import base64
import sys
from pathlib import Path
from types import SimpleNamespace

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))
sys.path.insert(0, str(WS / "vendor" / "AstrBot-master"))

from astrbot.core.message.components import At, AtAll, Image, Plain, Reply  # noqa: E402

from astrmai.inbound import onebot_to_maim_dict  # noqa: E402
from astrmai.outbound import components_to_segments, resolve_target  # noqa: E402

import importlib  # noqa: E402

plugin = importlib.import_module("astrmai_plugin.main")

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\ntiny").decode()
FAILS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


event = {
    "time": 1793333000,
    "self_id": 10001,
    "message_type": "group",
    "message_id": 555,
    "group_id": 12345,
    "user_id": 777,
    "message": [
        {"type": "text", "data": {"text": "看看这张"}},
        {"type": "at", "data": {"qq": "10001", "name": "麦麦"}},
        {"type": "reply", "data": {"id": "444"}},
        {"type": "image", "data": {"base64": PNG}},
        {"type": "face", "data": {"base64": PNG}},
        {"type": "unknown", "data": {}},
    ],
    "sender": {"card": "测试群友", "nickname": "测试群友", "group_name": "astrmai靶场"},
}

maim = onebot_to_maim_dict(event, platform="qq", account_id="10001")
segs = maim["message_segment"]["data"]
types = [s["type"] for s in segs]
check("inbound seg types", types == ["text", "at", "reply", "image", "emoji"], str(types))
check("inbound at target", segs[1]["data"]["target_user_id"] == "10001", str(segs[1]["data"]))
check("inbound reply id", segs[2]["data"] == "444")
check("inbound image b64 kept", segs[3]["data"] == PNG)
check("inbound ids are str", isinstance(maim["message_info"]["user_info"]["user_id"], str) and isinstance(maim["message_info"]["group_info"]["group_id"], str))
check("inbound platform/account", maim["message_info"]["platform"] == "qq" and maim["account_id"] == "10001")

# private chat with only a sticker still needs a text turn for the brain
priv = dict(event, message_type="private", group_id=None, message=[{"type": "image", "data": {"base64": PNG}}])
priv.pop("group_id")
maim_priv = onebot_to_maim_dict(priv, platform="qq", account_id="10001")
check(
    "private sticker gets placeholder",
    any(s["type"] == "text" for s in maim_priv["message_segment"]["data"]) and "group_info" not in maim_priv["message_info"],
    str([s["type"] for s in maim_priv["message_segment"]["data"]]),
)

out_segs = [
    {"type": "text", "data": {"text": "好"}},
    {"type": "at", "data": {"qq": "777", "name": "群友"}},
    {"type": "at", "data": {"qq": "all"}},
    {"type": "reply", "data": {"id": "555"}},
    {"type": "image", "data": {"file": f"base64://{PNG}"}},
    {"type": "emoji", "data": PNG},
]
chain = plugin._segments_to_chain(out_segs)
kinds = [type(c).__name__ for c in chain.chain]
check("outbound chain kinds", kinds == ["Plain", "At", "AtAll", "Reply", "Image", "Image"], str(kinds))
check("outbound text round trip", chain.chain[0].text == "好")
check("outbound at qq", str(chain.chain[1].qq) == "777")
check("outbound image url form", chain.chain[4].file == f"base64://{PNG}", str(getattr(chain.chain[4], "file", "no file attr")))


class FakeComp(SimpleNamespace):
    pass


def fake_message(*, group_id=None, target=None, user_id="777", comps=None):
    group_info = SimpleNamespace(group_id=group_id) if group_id else None
    return SimpleNamespace(
        message_info=SimpleNamespace(
            group_info=group_info,
            user_info=SimpleNamespace(user_id=user_id),
            additional_config={"platform_io_target_user_id": target} if target else {},
        ),
        raw_message=SimpleNamespace(components=[FakeComp(**c) for c in (comps or [])]),
    )


check("resolve group target", resolve_target(fake_message(group_id="12345")) == ("group", "12345"))
check(
    "resolve private uses platform_io target",
    resolve_target(fake_message(target="999")) == ("private", "999"),
)
check("resolve private falls back to sender", resolve_target(fake_message()) == ("private", "777"))
try:
    resolve_target(SimpleNamespace(message_info=SimpleNamespace(group_info=None, user_info=None, additional_config={})))
    check("resolve raises without target", False)
except ValueError:
    check("resolve raises without target", True)

comps = [
    {"format_name": "text", "text": "一句话"},
    {"format_name": "image", "binary_data": b"\x89PNGtiny"},
    {"format_name": "at", "at_user_id": "777"},
    {"format_name": "reply", "target_message_id": "555"},
]
rendered = components_to_segments(fake_message(group_id="1", comps=comps))
rtypes = [s["type"] for s in rendered]
check("components to segments", rtypes == ["text", "image", "at", "reply"], str(rtypes))
check("image component becomes b64 file", rendered[1]["data"]["file"].startswith("base64://"))

# ---- 复检 H4：媒体取回的边界（默认不读本地路径 / 只走 http(s) / 带上限 / 挡元数据地址）----
import asyncio  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402

from astrmai.inbound import _b64_from_url, onebot_to_maim_dict_async  # noqa: E402

os.environ.pop("ASTRMAI_MEDIA_ROOTS", None)
os.environ.pop("ASTRMAI_MEDIA_MAX_BYTES", None)
with tempfile.TemporaryDirectory() as td:
    secret = Path(td) / "peeper.txt"
    secret.write_bytes(b"do-not-read")
    try:
        _b64_from_url(str(secret), 3)
        check("默认拒绝本地路径（一条消息读不到任意文件）", False, "居然读了")
    except PermissionError:
        check("默认拒绝本地路径（一条消息读不到任意文件）", True)

    media = Path(td) / "media"
    media.mkdir()
    (media / "a.png").write_bytes(b"\x89PNGtiny")
    big = media / "big.bin"
    big.write_bytes(b"x" * 4096)
    os.environ["ASTRMAI_MEDIA_ROOTS"] = str(media)
    got = _b64_from_url(str(media / "a.png"), 3)
    check("白名单目录里的文件能读（NapCat 给本地路径时的正解）", got == base64.b64encode(b"\x89PNGtiny").decode(), str(got)[:24])
    try:
        _b64_from_url(str(secret), 3)
        check("白名单外的路径仍然拒绝", False)
    except PermissionError:
        check("白名单外的路径仍然拒绝", True)
    os.environ["ASTRMAI_MEDIA_MAX_BYTES"] = "1024"
    try:
        _b64_from_url(str(big), 3)
        check("本地文件也受大小上限", False)
    except ValueError:
        check("本地文件也受大小上限", True)
    os.environ.pop("ASTRMAI_MEDIA_MAX_BYTES", None)
    os.environ.pop("ASTRMAI_MEDIA_ROOTS", None)

for bad in ("http://169.254.169.254/latest/meta-data/", "http://metadata.google.internal/x"):
    try:
        _b64_from_url(bad, 3)
        check(f"拒绝元数据地址 {bad[:28]}", False)
    except ValueError:
        check(f"拒绝元数据地址 {bad[:28]}", True)

try:
    _b64_from_url("file:///C:/Windows/win.ini", 3)
    check("file:// 不再被直接 open", False)
except PermissionError:
    check("file:// 不再被直接 open", True)

check("媒体取回有异步入口（不压共用事件循环）", asyncio.iscoroutinefunction(onebot_to_maim_dict_async))

print("\n%d failure(s)" % len(FAILS), FAILS)
sys.exit(1 if FAILS else 0)
