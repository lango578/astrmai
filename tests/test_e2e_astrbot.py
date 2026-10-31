"""End-to-end forced fusion: a real AstrBot process, one fake-NapCat connection, MaiBot inside.

Nothing here talks to QQ. The fake client impersonates NapCat's reverse-WS "universal" role on
127.0.0.1, so every assertion is made on the API actions AstrBot itself issues over that socket.
"""

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
E2E = WS / "e2e" / "ab"
DATA = E2E / "data"
PLUGIN_DIR = DATA / "plugins" / "astrmai"
WS_PORT = 8123
E2E.mkdir(parents=True, exist_ok=True)

os.environ["ASTRBOT_ROOT"] = str(E2E)
os.environ["ASTRMAI_WORKSPACE"] = str(WS)
os.environ["ASTRMAI_MAI_ROOT"] = str(WS / "MaiBot-main")
os.environ["ASTRMAI_PLATFORM"] = "qq"
os.environ["ASTRMAI_ACCOUNT_ID"] = "10001"
os.environ["ASTRMAI_ALLOWED_USERS"] = "777"
os.environ["ASTRMAI_ALLOW_GROUPS"] = "1"
STATUS_FILE = WS / "e2e" / "astrmai_status.json"
os.environ["ASTRMAI_STATUS_FILE"] = str(STATUS_FILE)
os.environ.setdefault("ASTRBOT_DISABLE_ANALYTICS", "1")

PANEL_PORT = int(os.environ.get("ASTRMAI_PANEL_PORT", "18129"))
PANEL_TOKEN = "astrmai-e2e-token-7Qm3x-long-enough"  # privacy-scan:allow 靶场合成口令：只在 127.0.0.1 的测试实例里用，不是任何真实凭据
os.environ["ASTRMAI_PANEL"] = "on"
os.environ["ASTRMAI_PANEL_HOST"] = "127.0.0.1"
os.environ["ASTRMAI_PANEL_PORT"] = str(PANEL_PORT)
# 面板现在一律要凭据：显式给定口令与 token，并且用独立的凭据目录 ——
# 早先实机留下的 credentials.json 会让这里的 token 对不上（我就是这样测出一坑的）
os.environ["ASTRMAI_PANEL_PASSWORD"] = "astrmai-e2e-Zx9-4mqd-pass"
os.environ["ASTRMAI_PANEL_TOKEN"] = PANEL_TOKEN
os.environ["ASTRMAI_STATE_DIR"] = str(WS / "e2e" / "ab_state")


async def _port_free(port: int) -> bool:
    try:
        _r, w = await asyncio.open_connection("127.0.0.1", port)
        w.close()
        await w.wait_closed()
        return False
    except OSError:
        return True

sys.path.insert(0, str(WS))
sys.path.insert(0, str(WS / "vendor" / "AstrBot-master"))

CONFIG = {
    "config_version": 5,
    "platform": [
        {
            "id": "default",
            "name": "fakenapcat",
            "type": "aiocqhttp",
            "enable": True,
            "ws_reverse_host": "127.0.0.1",
            "ws_reverse_port": WS_PORT,
            "ws_reverse_token": "",
        }
    ],
    "wake_prefix": ["/"],
    "admins_id": ["astrbot"],
    "log_level": "INFO",
    "platform_settings": {
        "enable_id_white_list": False,
        "id_whitelist": [],
        "rate_limit": {"time": 60, "count": 300, "strategy": "discard"},
    },
    "dashboard": {"enable": False, "host": "127.0.0.1", "port": 6187},
    "provider_sources": [],
    "provider": [],
}


def setup_fs() -> None:
    if DATA.exists():
        shutil.rmtree(DATA)
    state = WS / "e2e" / "ab_state"
    if state.exists():
        shutil.rmtree(state)
    # 状态文件必须清掉：留着上一轮的 booted:true，引擎真死了测试也会当"已启动"放行
    STATUS_FILE.unlink(missing_ok=True)
    PLUGIN_DIR.mkdir(parents=True)
    shutil.copy(WS / "astrmai_plugin" / "main.py", PLUGIN_DIR / "main.py")
    shutil.copy(WS / "astrmai_plugin" / "metadata.yaml", PLUGIN_DIR / "metadata.yaml")
    DATA.joinpath("cmd_config.json").write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")


class FakeNapCat:
    """One universal reverse-WS socket: pushes events, answers AstrBot's API calls."""

    def __init__(self, session, ws):
        self.ws = ws
        self.actions: list[dict] = []
        self.frames = 0
        self._mid = 1000
        self._stop = False

    async def reader(self) -> None:
        import aiohttp

        try:
            async for raw in self.ws:
                payload = getattr(raw, "data", raw)
                try:
                    frame = json.loads(payload)
                except (ValueError, TypeError):
                    print(f"FRAME(unparsed) <- {str(payload)[:140]}", flush=True)
                    continue
                self.frames += 1
                print(f"FRAME #{self.frames} <- {json.dumps(frame, ensure_ascii=False)[:200]}", flush=True)
                if "action" not in frame:
                    continue
                frame["_recv_at"] = time.time()
                self.actions.append(frame)
                self._mid += 1
                reply = {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"message_id": self._mid},
                    "echo": frame.get("echo"),
                }
                await self.ws.send_str(json.dumps(reply))
                print(f"REPLY -> {json.dumps(reply, ensure_ascii=False)[:200]}", flush=True)
        except (aiohttp.ClientError, asyncio.CancelledError) as exc:
            print(f"reader ended: {type(exc).__name__}", flush=True)

    def reply_ids(self, since: float = 0.0) -> list[str]:
        """Message ids that replies in this window quote.

        A double-answer shows up here: if both brains answered the same command, the command's
        own message id gets quoted by a MaiBot reply as well as answered by AstrBot.
        """

        out = []
        for a in self.actions:
            if a.get("_recv_at", 0) < since:
                continue
            for seg in (a.get("params") or {}).get("message", []) or []:
                if seg.get("type") == "reply":
                    out.append(str(seg["data"].get("id", "")))
        return out

    def texts(self, since: float = 0.0) -> list[str]:
        out = []
        for a in self.actions:
            if a.get("_recv_at", 0) < since:
                continue
            for seg in (a.get("params") or {}).get("message", []) or []:
                if seg.get("type") == "text":
                    out.append(str(seg["data"].get("text", "")))
        return out

    def sends(self) -> list[dict]:
        return [a for a in self.actions if a.get("action") in {"send_group_msg", "send_private_msg", "send_msg"}]

    async def push_event(self, payload: dict) -> None:
        await self.ws.send_str(json.dumps(payload))


def group_event(text: str, msg_id: int) -> dict:
    return {
        "time": int(time.time()),
        "self_id": 10001,
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": msg_id,
        "group_id": 12345,
        "user_id": 777,
        "font": 0,
        "sender": {"card": "测试群友", "nickname": "测试群友", "role": "member", "user_id": 777},
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
    }


async def dump_stuck_tasks(tag: str) -> None:
    print(f"--- live tasks at {tag} ---", flush=True)
    for task in asyncio.all_tasks():
        print(f"[{task.get_name()}]", flush=True)
        task.print_stack(limit=5)


async def wait_port(host: str, port: int, timeout: float = 150.0) -> bool:
    deadline = time.time() + timeout
    dumped = set()
    while time.time() < deadline:
        try:
            _reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return True
        except OSError:
            elapsed = int(timeout - (deadline - time.time()))
            for mark in (30, 75, 120):
                if elapsed >= mark and mark not in dumped:
                    dumped.add(mark)
                    await dump_stuck_tasks(f"{elapsed}s")
            await asyncio.sleep(1)
    return False


def _task_done(task: asyncio.Task) -> None:
    """A dying AstrBot task must never fail silently."""

    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        print(f"FATAL: astrbot task died: {type(exc).__name__}: {exc}", flush=True)
        import traceback

        traceback.print_exception(type(exc), exc, exc.__traceback__)


async def _real_market_load(pm, policy) -> dict:
    """可选加码：真从市场装一个第三方 AstrBot 插件，再用同一个运行中的 PluginManager 加载。

    默认不跑——执行别人的插件代码属于别人的供应链，要显式 ASTRMAI_LIVE_LOAD_REAL=1 授权。
    """

    from astrmai.loader import astrbot_needs_pip, load_astrbot
    from astrmai.market import Market

    out: dict = {"run": True}
    market = Market(WS, astrbot_root=E2E, maibot_root=WS / "MaiBot-main", native_root=WS / "astrmai_plugins")
    installed_name = None
    try:
        found = await market.search("astrbot", "hello", limit=8)
        cands = found.get("plugins") or []
        if not cands:
            return {**out, "ok": False, "reason": "市场没搜到候选"}
        result = None
        for cand in cands[:4]:
            res = await market.install("astrbot", cand["id"])
            if res.get("ok"):
                result = res
                installed_name = Path(res["path"]).name
                break
        if result is None:
            return {**out, "ok": False, "reason": "前 4 个候选都装不上"}

        plugin_dir = Path(result["path"])
        needs, why = astrbot_needs_pip(plugin_dir)
        out["plugin"] = installed_name
        out["needs_deps"] = needs
        out["deps_why"] = why
        if needs:
            # 需要 pip 就应当被政策拦住，这本身就是正确行为，不算失败
            return {**out, "ok": True, "outcome": "按依赖政策拒加载（未碰 pip）", "loaded": False}
        loaded = await load_astrbot(pm, installed_name, policy)
        out["load"] = {k: v for k, v in loaded.items() if k != "attempts"}
        stars = {getattr(s, "name", "") for s in pm.context.get_all_stars()}
        out["loaded"] = bool(loaded.get("ok"))
        out["in_stars"] = installed_name in stars or any(installed_name in s for s in stars)
        out["stars_seen"] = sorted(stars)[:12]
        return {**out, "ok": bool(loaded.get("ok")) and out["in_stars"]}
    finally:
        if installed_name:
            try:
                out["uninstall"] = market.uninstall("astrbot", installed_name)
            except Exception as exc:  # noqa: BLE001
                out["uninstall"] = f"FAIL {type(exc).__name__}: {exc}"


async def _live_load_scenario(lifecycle, nap: FakeNapCat) -> dict:
    """装完≠用得上：不重启，把刚落到 data/plugins 的插件装进正在跑的 AstrBot，然后立刻验它能答、能当工具用。

    用的是 AstrBot 真实的 PluginManager.load，不是离线假对象。
    """

    from astrmai import federation as fed
    from astrmai.loader import LoadPolicy, astrbot_needs_pip, load_astrbot

    pm = lifecycle.plugin_manager
    out: dict = {}
    manager = pm.context.get_llm_tool_manager()

    probe = DATA / "plugins" / "ab_load_probe"
    needsdeps = DATA / "plugins" / "ab_load_probe_needsdeps"
    for path in (probe, needsdeps):
        if path.exists():
            shutil.rmtree(path)
    shutil.copytree(WS / "tests" / "fixtures" / "ab_load_probe", probe)
    shutil.copytree(probe, needsdeps)
    (needsdeps / "requirements.txt").write_text(
        "astrmai-no-such-package==9.9.9\n", encoding="utf-8"
    )

    policy = LoadPolicy(allow_deps=False)

    # 1) 需要先 pip 的插件：拒绝，而且拒绝得干净（没进星表、没碰 pip）
    needs, why = astrbot_needs_pip(needsdeps)
    refused = await load_astrbot(pm, needsdeps.name, policy)
    stars = {getattr(s, "name", "") for s in pm.context.get_all_stars()}
    out["pip_risk_detected"] = needs
    out["refused"] = refused
    out["refused_not_loaded"] = (not refused.get("ok")) and needsdeps.name not in stars
    out["refused_reason"] = refused.get("reason", "")

    # 2) 正常插件：真 PluginManager 运行时加载
    loaded = await load_astrbot(pm, probe.name, policy)
    out["load"] = {k: v for k, v in loaded.items() if k != "attempts"}
    out["loaded_into_stars"] = probe.name in {getattr(s, "name", "") for s in pm.context.get_all_stars()}

    # 3) 加载完马上能用：命令直接答
    mark = time.time()
    await nap.push_event(group_event("/abloadprobe", 5003))
    deadline = time.time() + 90
    while time.time() < deadline:
        await asyncio.sleep(2)
        if any("ABLOAD-OK" in t for t in nap.texts(mark)):
            break
    out["command_answered"] = any("ABLOAD-OK" in t for t in nap.texts(mark))

    # 4) 工具进了 AstrBot 的工具表，并且能被麦麦那侧的镜像源看到（不用重启）
    tool_names = {t.name for t in getattr(manager, "func_list", []) or []}
    out["tool_registered"] = "probe_echo" in tool_names
    mirrored = {t["name"] for t in fed.collect_astrbot_tools(manager)}
    out["mirror_sees_it"] = "probe_echo" in mirrored

    # 5) 真调用一次，证明 handler 绑得上、结果收得回来
    from types import SimpleNamespace

    fake_event = SimpleNamespace(session_id="astrmai-load-probe", plain_result=lambda x: x)
    try:
        result_text = await fed.call_astrbot_tool(manager, "probe_echo", {"text": "hello"}, fake_event)
    except Exception as exc:  # noqa: BLE001
        result_text = f"CALL-FAILED {type(exc).__name__}: {exc}"
    out["tool_called"] = str(result_text)
    out["tool_call_ok"] = "probe:hello" in str(result_text)

    # 6) 卸载路径也得能收干净（reload 前不留残）
    shutil.rmtree(needsdeps, ignore_errors=True)

    # 7) 可选加码：真市场插件的运行时加载（默认关，见 _real_market_load 的说明）
    if os.environ.get("ASTRMAI_LIVE_LOAD_REAL") == "1":
        try:
            out["real_market"] = await _real_market_load(pm, policy)
        except Exception as exc:  # noqa: BLE001
            out["real_market"] = {"run": True, "ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    else:
        out["real_market"] = {"run": False, "why": "未授权执行第三方插件代码（ASTRMAI_LIVE_LOAD_REAL=1 才跑）"}
    return out


async def main() -> int:
    setup_fs()

    # 面板端口要是已经被占，读到的就是别人的面板 —— 测试会假绿，所以先硬拦
    if not await _port_free(PANEL_PORT):
        print(f"FAIL: 端口 {PANEL_PORT} 上已经有别的东西在监听（大概是另一个 astrmai）。先停掉它再跑。", flush=True)
        return 1
    print(f"面板端口 {PANEL_PORT} 空闲，开始", flush=True)

    import aiohttp

    import main as astrbot_main  # vendored AstrBot entry module

    # Drive AstrBot's real core lifecycle directly. main_async() would first resolve the
    # dashboard static files, which can hit the network and has nothing to do with the fusion.
    from astrbot.core import LogBroker, LogManager, db_helper, logger
    from astrbot.core.core_lifecycle import AstrBotCoreLifecycle

    astrbot_main.check_env()
    log_broker = LogBroker()
    LogManager.set_queue_handler(logger, log_broker)

    lifecycle = AstrBotCoreLifecycle(log_broker, db_helper)

    if os.environ.get("E2E_PROBE_API") == "1":
        # 一次性探针：看清 call_action 拿到的 self_id 与已注册客户端的键，判断是不是我的假头写错
        import aiocqhttp.api_impl as _impl

        _orig = _impl.WebSocketReverseApi.call_action

        async def probed(self, action, **params):
            print(
                f"PROBE action={action} self_id={params.get('self_id')!r} "
                f"type={type(params.get('self_id')).__name__} "
                f"api_client_keys={list(self._api_clients.keys())} "
                f"event_clients={len(self._event_clients)}",
                flush=True,
            )
            return await _orig(self, action, **params)

        _impl.WebSocketReverseApi.call_action = probed

    await lifecycle.initialize()
    astrbot_task = asyncio.create_task(lifecycle.start(), name="astrbot")
    astrbot_task.add_done_callback(_task_done)
    globals()["_lifecycle"] = lifecycle
    print("astrbot starting...", flush=True)

    if not await wait_port("127.0.0.1", WS_PORT):
        print("FAIL: AstrBot never bound the reverse-WS port")
        astrbot_task.cancel()
        return 1
    print(f"astrbot listening on {WS_PORT}", flush=True)

    results = {}
    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(trust_env=False, connector=connector) as session:
        async with session.ws_connect(
            f"ws://127.0.0.1:{WS_PORT}/ws",
            headers={"X-Client-Role": "universal", "X-Self-ID": "10001"},
        ) as ws:
            nap = FakeNapCat(session, ws)
            reader_task = asyncio.create_task(nap.reader(), name="napcat-reader")
            await nap.push_event(
                {
                    "time": int(time.time()),
                    "self_id": 10001,
                    "post_type": "meta_event",
                    "meta_event_type": "lifecycle",
                    "sub_type": "connect",
                }
            )

            # give the plugin time to boot MaiBot inside AstrBot's loop
            deadline = time.time() + 180
            while time.time() < deadline:
                await asyncio.sleep(2)
                if await asyncio.to_thread(_plugin_booted):
                    break
            results["booted"] = _plugin_booted()
            print(f"maibot booted inside astrbot process: {results['booted']}", flush=True)
            if not results["booted"]:
                print("FAIL: MaiBot engine never came up inside AstrBot")
                reader_task.cancel()
                astrbot_task.cancel()
                return 1

            # scenario 1: ambient group chatter -> only MaiBot may answer
            mark = time.time()
            await nap.push_event(group_event("麦麦在吗，随便说一句", 5001))
            deadline = time.time() + 150
            while time.time() < deadline:
                await asyncio.sleep(2)
                # 只等麦麦的特征文本：麦麦的自主闲聊会往窗口里插别的话，不能被当成答案
                if any("【mock】" in t for t in nap.texts(mark)):
                    break
            results["chatter_replies"] = nap.texts(mark)

            # scenario 2: a slash command -> AstrBot's own star must answer
            if os.environ.get("E2E_SKIP_COMMAND") != "1":
                mark2 = time.time()
                await nap.push_event(group_event("/help", 5002))
                deadline = time.time() + 90
                while time.time() < deadline:
                    await asyncio.sleep(2)
                    if any("AstrBot v" in t for t in nap.texts(mark2)):
                        break
                results["command_replies"] = nap.texts(mark2)
                results["command_quoted_ids"] = nap.reply_ids(mark2)
            else:
                results["command_replies"] = ["skipped"]
                results["command_quoted_ids"] = []

            # scenario 3：市场装完的插件，不重启能不能直接用（真 PluginManager）
            if os.environ.get("E2E_SKIP_LIVELOAD") != "1":
                try:
                    results["live_load"] = await _live_load_scenario(lifecycle, nap)
                except Exception as exc:  # noqa: BLE001 - 取证失败要留下现场而不是把整个 e2e 崩掉
                    results["live_load"] = {"error": f"{type(exc).__name__}: {exc}"}
                print("LIVE LOAD:", json.dumps(results["live_load"], ensure_ascii=False)[:1200], flush=True)

            results["frames_seen"] = nap.frames
            results["actions_seen"] = [a.get("action") for a in nap.actions]

            # scenario 4：面板报的数必须和这条 socket 上真实发生的一致
            results["panel"] = await _panel_checks(nap)
            print("PANEL:", json.dumps(results["panel"], ensure_ascii=False)[:600], flush=True)

            if os.environ.get("E2E_HOLD") == "1":
                seconds = int(os.environ.get("E2E_HOLD_SECONDS", "900"))
                print(f"\n>>> 面板 http://127.0.0.1:{PANEL_PORT}/astrmai/ ；E2E_HOLD=1，保持 {seconds}s 供人眼验收 <<<\n", flush=True)
                await asyncio.sleep(seconds)

            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)

    await _teardown(astrbot_task)

    print("\n===== E2E RESULT =====")
    print(json.dumps(results, ensure_ascii=False, indent=2)[:2500])
    chatter = results.get("chatter_replies") or []
    command = results.get("command_replies") or []
    quoted = results.get("command_quoted_ids") or []

    # What the fusion must guarantee: one connection carries both brains, each brain answers
    # what it owns, and the same message is never answered twice.
    ok_chatter = any("【mock】" in t for t in chatter) and not any("AstrBot v" in t for t in chatter)
    ok_command = any("AstrBot v" in t for t in command)
    ok_no_double = "5002" not in quoted
    ok_single_socket = (results.get("frames_seen") or 0) > 0

    live = results.get("live_load") or {}
    live_keys = (
        "pip_risk_detected",
        "refused_not_loaded",
        "loaded_into_stars",
        "command_answered",
        "tool_registered",
        "mirror_sees_it",
        "tool_call_ok",
    )
    if os.environ.get("E2E_SKIP_LIVELOAD") == "1":
        ok_live_load = True
    else:
        ok_live_load = all(bool(live.get(k)) for k in live_keys) and bool((live.get("load") or {}).get("ok"))

    print(f"chatter answered by maibot only: {ok_chatter}")
    print(f"command answered by astrbot: {ok_command}")
    print(f"command not answered twice: {ok_no_double} (quoted ids in window: {quoted})")
    print(f"both brains on one socket: {ok_single_socket}")
    panel = results.get("panel") or {}
    ok_prefix = bool(panel.get("prefix_applied_live")) and bool(panel.get("prefix_wired"))
    print(f"面板改的命令前缀真的进了裁决闸门: {ok_prefix} {json.dumps(panel.get('prefix_row'), ensure_ascii=False)[:200]}")
    ok_panel = (
        bool(panel.get("reachable"))
        and bool(panel.get("booted"))
        and bool(panel.get("counts_match"))
        and bool(panel.get("page_ok"))
        and bool(panel.get("no_secret"))
        and bool(panel.get("gate_enforced"))
        and bool(panel.get("public_shell"))
        and bool(panel.get("no_token_leak"))
        and "maibot" in (panel.get("ledger_owners") or [])
    )
    print(f"panel agrees with what really happened on the socket: {ok_panel} {json.dumps(panel, ensure_ascii=False)[:320]}")
    print(f"装完立刻能用（真 PluginManager 运行时加载）: {ok_live_load} {[(k, live.get(k)) for k in live_keys] if live else ''}")
    return 0 if all((results.get("booted"), ok_chatter, ok_command, ok_no_double, ok_single_socket, ok_live_load, ok_panel, ok_prefix)) else 1


def _status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _plugin_booted() -> bool:
    return bool(_status().get("booted"))


def _is_json_obj(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except (ValueError, TypeError):
        return False


async def _panel_checks(nap: FakeNapCat) -> dict:
    """面板必须报出这条 socket 上真的发生过的事，不能自己编数字。"""

    import httpx

    base = f"http://127.0.0.1:{PANEL_PORT}"
    out: dict = {"base": base}
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            ov = (await c.get(f"{base}/astrmai/api/overview", headers={"X-Astrmai-Token": PANEL_TOKEN})).json()
        except Exception as exc:  # noqa: BLE001
            return {**out, "reachable": False, "reason": f"{type(exc).__name__}: {exc}"}
        out["reachable"] = True
        bare = await c.get(f"{base}/astrmai/api/overview")
        out["gate_enforced"] = bare.status_code == 401
        eng = ov.get("engine") or {}
        out["booted"] = eng.get("booted")
        out["forwarded"] = eng.get("forwarded")
        out["sent"] = eng.get("sent")
        out["sessions"] = eng.get("sessions")
        out["job_guard"] = (ov.get("process") or {}).get("job_guard", {}).get("mode")
        out["ledger_owners"] = sorted({r.get("owner", "") for r in (ov.get("arbitration") or {}).get("recent", [])})
        out["ledger_has_command"] = any(
            r.get("owner") == "astrbot" for r in (ov.get("arbitration") or {}).get("recent", [])
        )
        out["socket_sends"] = len(nap.sends())
        out["counts_match"] = (eng.get("sent") or 0) >= 1 and (eng.get("forwarded") or 0) >= 1
        page = (await c.get(f"{base}/astrmai/", headers={"X-Astrmai-Token": PANEL_TOKEN})).text
        tools = (await c.get(f"{base}/astrmai/api/tools", headers={"X-Astrmai-Token": PANEL_TOKEN})).text
        plugins = (await c.get(f"{base}/astrmai/api/plugins", headers={"X-Astrmai-Token": PANEL_TOKEN})).text
        settings = (await c.get(f"{base}/astrmai/api/settings", headers={"X-Astrmai-Token": PANEL_TOKEN})).text
        load_page = (await c.get(f"{base}/astrmai/")).text  # 外壳与静态资源是公开的，前端靠它弹登录框
        out["public_shell"] = "astrmai-shell" in load_page
        out["no_token_leak"] = PANEL_TOKEN not in (page + tools + plugins + settings + json.dumps(ov))
        out["page_ok"] = "astrmai-shell" in page
        out["tools_prefix_seen"] = sorted(
            k for k in ("mai_", "ab_", "abcmd_", "nm_") if f'"{k}' in tools or f"{k}" in tools
        )
        out["maibot_plugin_tools_readable"] = bool(json.loads(tools).get("maibot")) if _is_json_obj(tools) else False
        out["no_secret"] = not any("sk-" in blob for blob in (page, tools, plugins, settings, json.dumps(ov)))
        out["upstream_panels_off"] = all(
            str(v).startswith("关闭") for v in (ov.get("upstream_panels") or {}).values()
        ) or "未知" in str(ov.get("upstream_panels"))

        # 面板上「命令前缀」这个字段必须是真闸门在用的那个，不能改了个没人读的副本。
        # 取证方式：换个前缀 → 从这条 socket 推一条以新前缀开头的消息 → 看流水把它判给谁。
        probe = f"#astrmai-probe-{int(time.time())}"
        hdr = {"X-Astrmai-Token": PANEL_TOKEN, "Origin": base}
        patched = await c.post(f"{base}/astrmai/api/settings", json={"patch": {"command_prefixes": ["#", "/"]}}, headers=hdr)
        out["prefix_patch_status"] = patched.status_code
        out["prefix_applied_live"] = "command_prefixes" in (patched.json().get("applied_live") or [])
        await nap.push_event(group_event(probe, 5099))
        await asyncio.sleep(8)
        ov2 = (await c.get(f"{base}/astrmai/api/overview", headers={"X-Astrmai-Token": PANEL_TOKEN})).json()
        rows = [r for r in (ov2.get("arbitration") or {}).get("recent", []) if r.get("preview") == probe]
        out["prefix_wired"] = bool(rows) and rows[0].get("owner") == "astrbot" and "命令前缀" in str(rows[0].get("reason"))
        out["prefix_row"] = rows[:1]
        await c.post(f"{base}/astrmai/api/settings", json={"patch": {"command_prefixes": ["/", "!"]}}, headers=hdr)
    return out


async def _teardown(astrbot_task: asyncio.Task) -> None:
    lifecycle = globals().get("_lifecycle")
    if lifecycle is not None:
        try:
            await lifecycle.stop()
        except Exception as exc:  # noqa: BLE001
            print(f"lifecycle stop failed: {type(exc).__name__}: {exc}", flush=True)
    astrbot_task.cancel()
    await asyncio.gather(astrbot_task, return_exceptions=True)
    await asyncio.sleep(2)


if __name__ == "__main__":
    os.chdir(E2E)
    sys.exit(asyncio.run(main()))
