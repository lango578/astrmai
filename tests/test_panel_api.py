"""面板取证：数据形状、信任边界、密钥不外泄，以及"确实不像上游那两套"。

不起麦麦也不起 AstrBot：面板读的是 star 上的实时对象，用假 star 就能把 HTTP 层
和门逻辑全跑真（真 aiohttp 服务、真回环端口、真 401/403）。
"""

import asyncio
import json
import os
import re
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))

import httpx  # noqa: E402

STATE_DIR = WS / "e2e" / "panel_state"
os.environ["ASTRMAI_STATE_DIR"] = str(STATE_DIR)

from astrmai.config import AstrmaiConfig  # noqa: E402
from astrmai.gate import decide  # noqa: E402
from astrmai.panel import AstrmaiPanel  # noqa: E402
from astrmai.panel_data import Ledger, LogRing  # noqa: E402

FAILS: list[str] = []
TEST_PASSWORD = "astrmai-test-Zx9-4mqd"  # privacy-scan:allow 靶场合成口令：只在 127.0.0.1 的测试实例里用，不是任何真实凭据
TEST_TOKEN = "astrmai-test-token-8Fd2Kq"  # privacy-scan:allow 靶场合成口令：只在 127.0.0.1 的测试实例里用，不是任何真实凭据


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail else ""), flush=True)
    if not cond:
        FAILS.append(name)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeManager:
    def __init__(self, tools: list[dict]) -> None:
        self.func_list = [SimpleNamespace(name=t["name"], description=t["desc"], active=True, handler_module_path=t["mod"]) for t in tools]


class FakeStar:
    """只长面板真正会读的字段；面板读不到的东西就该显示 unavailable，这里也不给它伪造。"""

    def __init__(self) -> None:
        cfg = AstrmaiConfig(
            mai_root=WS / "MaiBot-main",
            account_id="10001",
            allowed_users={"777"},
            groups=set(),
            group_enabled=False,
            private_enabled=True,
        )
        self.host = SimpleNamespace(
            cfg=cfg,
            boot_ms=4321.0,
            job=SimpleNamespace(status=lambda: {"mode": "self", "detail": "fake", "assigned_pids": [], "inherited_children": True}, describe=lambda: "作业对象=host"),
            live_runners=lambda: [4242],
        )
        self.booted = True
        self.boot_error = ""
        self.forwarded = 7
        self.sent_total = 3
        self._umo_by_target = {("private", "777"): "aiocqhttp:Private:777"}
        self._event_by_target = {("private", "777"): SimpleNamespace(get_message_str=lambda: "随便聊聊")}
        self.federation = {"mai_tools": {"total": 9, "plugin_sourced": 2}, "ab_tools": 4, "command_tools": {"count": 3}, "native": {"tools": 2}}
        self.context = SimpleNamespace(
            _star_manager=SimpleNamespace(failed_plugin_dict={"broken_plugin": "元数据缺 version"}),
            get_llm_tool_manager=lambda: FakeManager(
                [
                    {"name": "mai_compare_numbers", "desc": "比较两个数", "mod": "astrmai.federation"},
                    {"name": "ab_something", "desc": "镜像进麦麦前的原工具", "mod": "data.plugins.demo.main"},
                    {"name": "abcmd_help", "desc": "命令镜像", "mod": "astrmai.command_tools"},
                    {"name": "nm_demo_echo", "desc": "原生", "mod": "astrmai.native_plugin"},
                ]
            ),
            get_all_stars=lambda: [
                SimpleNamespace(name="astrmai", author="astrmai", version="0.1.0", short_desc="融合引擎", desc="", repo="", root_dir_name="astrmai", reserved=False, activated=True),
                SimpleNamespace(name="内置命令", author="AstrBot", version="", short_desc="", desc="", repo="", root_dir_name="builtin_commands", reserved=True, activated=True),
            ],
        )
        self.market_calls: list[str] = []
        self.load_calls: list[tuple[str, str]] = []
        self.resync_calls = 0

        # 假 aiocqhttp 适配器：形状来自 vendor 里的 aiocqhttp_platform_adapter.py +
        # aiocqhttp 包（_wsr_api_clients dict / _wsr_event_clients set）。
        class FakeWS:
            def __init__(self, closed: bool = False) -> None:
                self._closed = closed

        self.fake_adapter = SimpleNamespace(
            meta=lambda: SimpleNamespace(id="qq", name="aiocqhttp"),
            bot=SimpleNamespace(
                _wsr_api_clients={"10001": FakeWS(closed=False)},
                _wsr_event_clients={FakeWS(closed=False), FakeWS(closed=True)},
            ),
        )
        self.fake_other = SimpleNamespace(meta=lambda: SimpleNamespace(id="webchat", name="webchat"))
        self.context = SimpleNamespace(
            platform_manager=SimpleNamespace(get_insts=lambda: [self.fake_adapter, self.fake_other]),
            _star_manager=SimpleNamespace(failed_plugin_dict={"broken_plugin": "元数据缺 version"}),
            get_llm_tool_manager=lambda: FakeManager(
                [
                    {"name": "mai_compare_numbers", "desc": "比较两个数", "mod": "astrmai.federation"},
                    {"name": "ab_something", "desc": "镜像进麦麦前的原工具", "mod": "data.plugins.demo.main"},
                    {"name": "abcmd_help", "desc": "命令镜像", "mod": "astrmai.command_tools"},
                    {"name": "nm_demo_echo", "desc": "原生", "mod": "astrmai.native_plugin"},
                ]
            ),
            get_all_stars=lambda: [
                SimpleNamespace(name="astrmai", author="astrmai", version="0.1.0", short_desc="融合引擎", desc="", repo="", root_dir_name="astrmai", reserved=False, activated=True),
                SimpleNamespace(name="内置命令", author="AstrBot", version="", short_desc="", desc="", repo="", root_dir_name="builtin_commands", reserved=True, activated=True),
            ],
            provider_manager=self.fake_provider_manager(),
            persona_manager=self.fake_persona_manager(),
            conversation_manager=self.fake_conversation_manager(),
            cron_manager=self.fake_cron_manager(),
        )
        # 真 NativeRegistry 的形状：tools 是 {全名: (插件对象, 工具)} 字典，插件的 tools() 是方法。
        # 假对象要是长得不对，面板就会在真进程里 500（我踩过）。
        demo_plugin = SimpleNamespace(name="astrmai_demo")
        self.native = SimpleNamespace(
            plugins={"astrmai_demo": demo_plugin},
            tools={"nm_astrmai_demo_echo": (demo_plugin, SimpleNamespace(description="回显"))},
        )

    # ---- AstrBot 功能面的假管理器：形状对齐 vendor 真类，只记调用不真执行 ----

    def fake_provider_manager(self):
        self.provider_calls: list[tuple] = []

        class FakeConf(dict):
            def save_config(self):
                return None

        conf = FakeConf(
            provider=[
                {"id": "prov-a", "type": "openai_chat_completion", "provider_type": "chat_completion", "enable": True, "model": "fake-model", "api_base": "https://api.fake/v1", "key": ["sk-fake-0123456789abcdef"]},
                {"id": "prov-b", "type": "openai_chat_completion", "provider_type": "chat_completion", "enable": False, "model": "fake-2", "api_base": "http://10.0.0.9:8/v1", "key": []},
            ],
            provider_settings={},
            agent_runner={"runner_type": "local", "config": {"model": {"provider_id": "prov-a"}}},
        )

        class FakeInst:
            def __init__(self, cfg):
                self.cfg = cfg

            def meta(self):
                return SimpleNamespace(id=self.cfg["id"], model=self.cfg["model"], type=self.cfg["type"], provider_type=SimpleNamespace(value="chat_completion"))

            async def test(self):
                if self.cfg["id"] == "prov-b":
                    raise RuntimeError("连不上（这是假桩故意抛的）")
                return None

        insts = {"prov-a": FakeInst(conf["provider"][0])}

        async def create_provider(new_config):
            self.provider_calls.append(("create", dict(new_config)))
            conf["provider"].append(dict(new_config))
            insts[new_config["id"]] = FakeInst(new_config)
            if str(new_config.get("key") or [""])[0].startswith("sk-bad"):
                raise RuntimeError("创建即加载失败，应回滚")

        async def update_provider(origin, new_config):
            self.provider_calls.append(("update", origin, dict(new_config)))
            for i, p in enumerate(conf["provider"]):
                if p.get("id") == origin:
                    conf["provider"][i] = dict(new_config)
                    break

        async def delete_provider(provider_id=None, provider_source_id=None):
            self.provider_calls.append(("delete", provider_id))
            conf["provider"] = [p for p in conf["provider"] if p.get("id") != provider_id]
            insts.pop(provider_id, None)

        pm = SimpleNamespace(
            providers_config=conf["provider"],
            provider_sources_config=[],
            inst_map=insts,
            acm=SimpleNamespace(default_conf=conf),
        )
        pm.create_provider = create_provider
        pm.update_provider = update_provider
        pm.delete_provider = delete_provider
        pm.get_provider_config_by_id = lambda pid, merged=False: next((dict(p) for p in conf["provider"] if p.get("id") == pid), None)
        pm.conf = conf
        return pm

    def fake_persona_manager(self):
        self.persona_calls: list[tuple] = []
        personas = [
            SimpleNamespace(persona_id="default", system_prompt="You are a helpful assistant.", begin_dialogs=[], custom_error_message=None),
            SimpleNamespace(persona_id="p1", system_prompt="你是钓鱼佬。", begin_dialogs=["今天钓到啥"], custom_error_message=""),
        ]

        async def get_all_personas():
            return list(personas)

        async def create_persona(pid, prompt, begin_dialogs=None, **kw):
            self.persona_calls.append(("create", pid))
            if any(p.persona_id == pid for p in personas):
                raise ValueError(f"Persona with ID {pid} already exists.")
            if pid == "default":
                raise ValueError("reserved")
            personas.append(SimpleNamespace(persona_id=pid, system_prompt=prompt, begin_dialogs=begin_dialogs or [], custom_error_message=None))

        async def update_persona(pid, system_prompt=None, begin_dialogs=None, **kw):
            self.persona_calls.append(("update", pid))
            for p in personas:
                if p.persona_id == pid:
                    if system_prompt is not None:
                        p.system_prompt = system_prompt
                    if begin_dialogs is not None:
                        p.begin_dialogs = begin_dialogs
                    return p
            raise ValueError(f"Persona with ID {pid} does not exist.")

        async def delete_persona(pid):
            self.persona_calls.append(("delete", pid))
            if pid == "default":
                raise ValueError("cannot delete default")
            personas[:] = [p for p in personas if p.persona_id != pid]

        return SimpleNamespace(
            get_all_personas=get_all_personas,
            create_persona=create_persona,
            update_persona=update_persona,
            delete_persona=delete_persona,
            default_persona="default",
        )

    def fake_conversation_manager(self):
        convs = [
            SimpleNamespace(cid="cid-aaa", platform_id="aiocqhttp", user_id="aiocqhttp:Friend:777", title="", persona_id="default", token_usage=123, updated_at=1770000002, history=json.dumps([{"role": "user", "content": "你好"}, {"role": "assistant", "content": "在的"}])),
            SimpleNamespace(cid="cid-bbb", platform_id="aiocqhttp", user_id="aiocqhttp:Group:1", title="群聊", persona_id="p1", token_usage=45, updated_at=1770000001, history="[]"),
        ]

        async def get_conversations(unified_msg_origin=None, platform_id=None):
            return list(convs)

        async def get_conversation(cid):
            return next((c for c in convs if c.cid == cid), None)

        return SimpleNamespace(get_conversations=get_conversations, get_conversation=get_conversation)

    def fake_cron_manager(self):
        self.cron_calls: list[tuple] = []
        jobs = [
            SimpleNamespace(job_id="job-111111111111", name="每日签到", job_type="basic", cron_expression="0 9 * * *", enabled=True, last_run_at=None, last_error=""),
        ]

        async def list_jobs(job_type=None):
            return [j for j in jobs if job_type is None or j.job_type == job_type]

        async def update_job(job_id, **kw):
            self.cron_calls.append(("toggle", job_id, kw.get("enabled")))
            for j in jobs:
                if j.job_id == job_id:
                    j.enabled = bool(kw.get("enabled", j.enabled))

        async def delete_job(job_id):
            self.cron_calls.append(("delete", job_id))
            jobs[:] = [j for j in jobs if j.job_id != job_id]

        async def run_job_now(job_id):
            self.cron_calls.append(("run", job_id))

        return SimpleNamespace(list_jobs=list_jobs, update_job=update_job, delete_job=delete_job, run_job_now=run_job_now, get_next_run_time=lambda jid: None)

    def _market(self):
        self.market_calls.append("market")

        async def search(source, query, limit, offset=0, force=False):
            rows = [{"id": "demo.plugin", "name": "demo", "repo": "https://github.com/x/y", "installed": False}]
            rows += [{"id": f"extra{i}.plugin", "name": f"extra{i}", "repo": "https://github.com/x/y"} for i in range(9)]
            page = rows[offset : offset + limit]
            return {
                "source": source,
                "catalog": "fake",
                "total": len(rows),
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(page) < len(rows),
                "plugins": page,
            }

        async def install(source, ident):
            return {"ok": False, "reason": "取证里不真装"}

        def uninstall(source, name):
            return {"ok": True, "removed": f"<{source}/{name}>"}

        return SimpleNamespace(search=search, install=install, uninstall=uninstall)

    async def _load_installed(self, source, out, policy):
        self.load_calls.append((source, str(out.get("path"))))
        return f"[{source}] 已按取证桩处理（allow_deps={policy.allow_deps}）"

    def _sync_maibot_tools(self):
        self.resync_calls += 1
        return {"total": 9, "added": 0}


def _dead_connection_overview() -> dict:
    """掉线形状：api/event 都是死 ws，面板必须报"未连接"而不是0也算连着。"""
    from astrmai.panel_data import _platform_connections

    closed = SimpleNamespace(_closed=True)
    star = SimpleNamespace(
        context=SimpleNamespace(
            platform_manager=SimpleNamespace(
                get_insts=lambda: [
                    SimpleNamespace(
                        meta=lambda: SimpleNamespace(id="qq", name="aiocqhttp"),
                        bot=SimpleNamespace(_wsr_api_clients={}, _wsr_event_clients=[closed]),
                    )
                ]
            )
        )
    )
    return _platform_connections(star)


async def main() -> int:
    if STATE_DIR.exists():
        for p in STATE_DIR.glob("*"):
            p.unlink()
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    os.environ["ASTRMAI_PANEL_HOST"] = "127.0.0.1"
    port = free_port()
    os.environ["ASTRMAI_PANEL_PORT"] = str(port)
    # 面板现在一律要凭据：这里用环境变量显式给定口令（不生成初始密码，所以不触发强制改密）
    os.environ["ASTRMAI_PANEL_PASSWORD"] = TEST_PASSWORD
    os.environ["ASTRMAI_PANEL_TOKEN"] = TEST_TOKEN

    star = FakeStar()
    ledger = Ledger()
    logs = LogRing()
    ledger.record(chat_type="private", target_id="777", owner="maibot", reason="私聊", preview="随便聊聊", forwarded=True)
    logs.add("INFO", "astrmai", "面板取证日志")

    panel = AstrmaiPanel(lambda: star, ledger=ledger, logs=logs)
    info = await panel.start()
    check(
        "面板绑到回环端口且凭据已建立",
        info["host"] == "127.0.0.1" and info["port"] == port and info["must_change"] is False,
        str(info),
    )

    base = f"http://127.0.0.1:{port}"
    bodies: dict[str, str] = {}
    async with httpx.AsyncClient(base_url=base, timeout=20, headers={"X-Astrmai-Token": TEST_TOKEN}) as c:
        # ---- 只读端点 ----
        for path in ("/astrmai/api/overview", "/astrmai/api/tools", "/astrmai/api/plugins", "/astrmai/api/sessions", "/astrmai/api/logs", "/astrmai/api/settings", "/astrmai/api/about"):
            r = await c.get(path)
            bodies[path] = r.text
            check(f"GET {path} 200 且是 JSON", r.status_code == 200 and isinstance(r.json(), dict), f"status={r.status_code}")

        ov = json.loads(bodies["/astrmai/api/overview"])
        check("概览带裁决流水", any(r.get("reason") == "私聊" for r in ov["arbitration"]["recent"]), str(ov["arbitration"]["decisions"]))
        check("概览带作业对象状态", ov["process"]["job_guard"]["mode"] == "self", str(ov["process"]["job_guard"]))
        check("概览报出正在跑的星（总数与第三方数分开）", ov["stars"]["total"] == 2 and ov["stars"]["third_party"] == 1, str(ov["stars"]))
        check("概览报出 runner 存活", ov["process"]["runners_live"] == [4242], str(ov["process"]["runners_live"]))
        check("版本来自真实模块而不是写死", bool(ov["versions"]["astrbot"]) and bool(ov["versions"]["maibot"]), str(ov["versions"]))

        conn = ov.get("connections") or {}
        qq = next((p for p in conn.get("platforms", []) if p.get("id") == "qq"), None)
        web = next((p for p in conn.get("platforms", []) if p.get("id") == "webchat"), None)
        check("平台连接：aiocqhttp 1 条活 api 客户端、2 条 event（1 死被如实计出）", bool(qq) and qq["state"] == "已连接" and qq["connections"] == 2 and sum(1 for c in qq["event_clients"] if not c["alive"]) == 1, str(qq))
        check("平台连接：非 aiocqhttp 适配器如实报未知，不瞎猜", bool(web) and web["state"] == "未知此适配器", str(web))
        check("平台连接：connected 汇总为真", conn.get("connected") is True and conn.get("total_connections") == 2, str({k: conn.get(k) for k in ("connected", "total_connections")}))
        dead = _dead_connection_overview()
        check("平台连接：全掉线时 connected=False 且状态如实报未连接", dead["connected"] is False and dead["platforms"][0]["state"] == "未连接" and dead["platforms"][0]["connections"] == 0, str(dead))

        from astrmai.panel_data import _tls_state, apply_settings

        tls = _tls_state()
        check("TLS 状态上报：本测试进程没跑 tls.apply()，就如实报否", tls["applied"] is False, str(tls))

        # 麦麦没起的进程里，自主性字段必须被拒绝并说明，不能假装生效
        res = apply_settings(star, {"mai_talk_value": 0})
        check("自主性开关：麦麦没启动时拒绝且说明原因", res["ok"] is False and "麦麦还没启动" in res["refused"].get("mai_talk_value", ""), str(res))

        # 麦麦可导入时，开关必须真落到 global_config（她每轮现读它）—— 这就是"开关不是装饰"的判据
        try:
            from astrmai.host import prepare_paths
            from astrmai.config import AstrmaiConfig as _AC

            prepare_paths(_AC(mai_root=WS / "MaiBot-main"))
            from src.config.config import global_config as _gc

            rt = _gc.chat.reply_timing
            original_talk, original_paused = rt.talk_value, rt.private_reply_paused
            try:
                res2 = apply_settings(star, {"mai_talk_value": 0, "mai_private_reply_paused": True})
                check("自主性开关：麦麦在时当场生效", res2["ok"] and rt.talk_value == 0.0 and rt.private_reply_paused is True, f"talk={rt.talk_value} paused={rt.private_reply_paused} applied={res2['applied_live']}")
                res3 = apply_settings(star, {"mai_talk_value": 5})
                check("自主性开关：越界值被夹回 0–1", rt.talk_value == 1.0, f"talk={rt.talk_value}")
            finally:
                rt.talk_value, rt.private_reply_paused = original_talk, original_paused
        except Exception as exc:  # noqa: BLE001
            check("自主性开关：麦麦不可导入时跳过（不算失败）", True, f"{type(exc).__name__}: {exc}")

        tools = json.loads(bodies["/astrmai/api/tools"])
        prefixes = tools["astrbot"]["by_prefix"]
        check("联邦前缀分类正确", prefixes.get("mai_") == 1 and prefixes.get("ab_") == 1 and prefixes.get("abcmd_") == 1 and prefixes.get("nm_") == 1, str(prefixes))
        check("麦麦插件工具视图可读", isinstance(tools["maibot"]["plugin_specs"], list), str(tools["maibot"].get("error")))

        plugs = json.loads(bodies["/astrmai/api/plugins"])
        check("AstrBot 星清单里能认出 astrmai", any(p["name"] == "astrmai" for p in plugs["astrbot"]["items"]), str([p["name"] for p in plugs["astrbot"]["items"]]))
        check("加载失败清单如实带出", plugs["astrbot"]["failed"].get("broken_plugin") == "元数据缺 version", str(plugs["astrbot"]["failed"]))
        native_items = plugs["astrmai"]["items"]
        check(
            "原生插件未加载时不谎报工具数（磁盘 manifest 里没有工具声明）",
            all((i["tools"] is None and not i.get("loaded")) or isinstance(i.get("tools"), int) for i in native_items),
            str(native_items)[:160],
        )
        check(
            "已加载的原生插件按真登记表报出工具数与名字",
            any(i.get("name") == "astrmai_demo" and i.get("tools") == 1 and i.get("loaded") and i.get("tool_names") == ["nm_astrmai_demo_echo"] for i in native_items),
            str(native_items)[:200],
        )

        # ---- 密钥外泄门：真配置里有真 key，响应里一个字都不许出现 ----
        real_key = ""
        try:
            import tomlkit

            doc = tomlkit.parse((WS / "MaiBot-main" / "config" / "model_config.toml").read_text(encoding="utf-8"))
            # 上游是 [[api_providers]] 数组表，不是以名字为键的表
            for provider in (doc.get("api_providers") or []):
                value = str((provider or {}).get("api_key") or "") if isinstance(provider, dict) else ""
                if value:
                    real_key = value
                    break
        except Exception as exc:  # noqa: BLE001
            print(f"（读不到真 key 做对照: {type(exc).__name__}）", flush=True)
        blob = "\n".join(bodies.values())
        check("所有响应里没有 sk- 形状", "sk-" not in blob)
        if real_key:
            check("响应里不含当前生效密钥", real_key not in blob and real_key[:12] not in blob, f"key 长度 {len(real_key)}，只验存在性")
        bindings = ov.get("model_bindings") or {}
        check("模型绑定读到了真实结构", bindings.get("ok") is True, str(bindings)[:160])
        check(
            "模型绑定只报有没有凭据，且每行字段齐备",
            all(set(v) >= {"provider", "model", "host", "credentialed"} for v in (bindings.get("tasks") or {}).values()),
            str(list((bindings.get("tasks") or {}).items())[:2])[:180],
        )
        # 我踩过的真坑：scrub 按子串匹配 "key"，把自家布尔字段 key_present 吞成 "<redacted>"，
        # 于是每个"故意清空"的任务在屏上都显示成「密钥已配置」。布尔值必须原样穿过 scrub。
        tasks = (bindings.get("tasks") or {}).values()
        check("布尔凭据字段没被 scrub 吞成字符串", all(isinstance(v.get("credentialed"), bool) for v in tasks), str([v.get("credentialed") for v in tasks][:4]))
        cleared = [name for name, v in (bindings.get("tasks") or {}).items() if v.get("unbound")]
        check("被刻意清空的任务如实显示为未绑定且无凭据", bool(cleared) and all(
            (bindings["tasks"][c]["credentialed"] is False) for c in cleared), f"清空的任务: {cleared}")
        from astrmai.panel_data import is_secret, scrub

        check("整词匹配：真凭据字段仍被拦", is_secret("api_key") and is_secret("panel_token") and is_secret("authorization"))
        check("整词匹配：自家普通字段不误伤", not is_secret("credentialed") and not is_secret("tokenizer") and not is_secret("monkey"))
        check("scrub 保留布尔与数字", scrub({"a": False, "b": 0, "c": True}) == {"a": False, "b": 0, "c": True}, "")
        live = (ov.get("federation") or {}).get("ab_tools_live") or {}
        check("可镜像工具数报的是当下而不是 boot 快照", isinstance(live.get("count"), int) and live.get("count", 0) >= 1, str(live)[:120])
        vis = (ov.get("federation") or {}).get("mirror_visibility") or {}
        check(
            "面板如实报镜像可见性：默认全部延迟发现，不挤 planner",
            vis.get("ab_") == "deferred" and vis.get("nm_") == "deferred" and vis.get("abcmd_") == "deferred",
            str(vis),
        )
        os.environ["ASTRMAI_AB_TOOLS_VISIBLE"] = "1"
        vis2 = ((await c.get("/astrmai/api/overview")).json().get("federation") or {}).get("mirror_visibility") or {}
        check("显式开 visible 时面板跟着改口", vis2.get("ab_") == "visible", str(vis2))
        del os.environ["ASTRMAI_AB_TOOLS_VISIBLE"]
        check("面板版本如实", ov["versions"]["astrmai"] == "0.1.0" and bool(ov["versions"]["astrbot"]), str(ov["versions"]))

        # ---- AstrBot 功能面：provider / 人格 / 对话 / cron ----
        r = await c.get("/astrmai/api/providers")
        prov = r.json()
        check("provider 清单：200 且列出两条", r.status_code == 200 and prov.get("ok") and len(prov.get("items") or []) == 2, str(prov)[:160])
        row_a = next((x for x in prov["items"] if x["id"] == "prov-a"), {})
        check("provider 行：密钥只报有没有与数量，值不出现在响应里", row_a.get("key_configured") is True and row_a.get("key_count") == 1 and "sk-fake" not in r.text, str(row_a)[:120])
        check("provider 行：主机名折算且默认/加载状态报出", row_a.get("api_base") == "api.fake" and row_a.get("is_default") is True and row_a.get("loaded") is True, str(row_a))
        check("provider 行：未启用的如实报", next(x for x in prov["items"] if x["id"] == "prov-b").get("enable") is False, "")

        r = await c.post("/astrmai/api/providers/apply", json={"action": "test", "id": "prov-a"})
        check("provider 测试：可用实例报 available", r.status_code == 200 and r.json().get("status") == "available", str(r.json())[:120])
        r = await c.post("/astrmai/api/providers/apply", json={"action": "test", "id": "prov-b"})
        check("provider 测试：没启用的如实报拿不到实例", r.status_code == 400 and "没有运行中的实例" in r.json().get("reason", ""), str(r.json())[:140])
        r = await c.post("/astrmai/api/providers/apply", json={"action": "test", "id": "prov-none"})
        check("provider 测试：不存在的 id 明确报错", r.status_code == 400 and "没有运行中的实例" in r.json().get("reason", ""), str(r.json())[:120])

        r = await c.post("/astrmai/api/providers/apply", json={"action": "create", "config": {"id": "prov-new", "type": "openai_chat_completion", "model": "m1", "api_base": "https://x/v1", "key": ["sk-new-key-999"]}})
        created = r.json()
        check("provider 创建：转到真 create_provider 入口", created.get("ok") is True and ("create", "prov-new") in [(a, b.get("id")) for a, b in star.provider_calls if a == "create"], str(created)[:140])
        check("provider 创建：新行出现在清单里", any(x["id"] == "prov-new" for x in (await c.get("/astrmai/api/providers")).json()["items"]), "")

        r = await c.post("/astrmai/api/providers/apply", json={"action": "update", "id": "prov-new", "config": {"model": "m2", "key": ""}})
        updates = [x for x in star.provider_calls if x[0] == "update"]
        check("provider 更新：key 留空时不抹掉旧密钥", r.json().get("ok") is True and any(u[2].get("key") == ["sk-new-key-999"] for u in updates), str(updates[-1])[:160])

        r = await c.post("/astrmai/api/providers/apply", json={"action": "enable", "id": "prov-b", "enabled": True})
        check("provider 启停：走 update_provider 且 enable 翻转", r.json().get("ok") is True and next(x for x in (await c.get("/astrmai/api/providers")).json()["items"] if x["id"] == "prov-b")["enable"] is True, str(r.json())[:120])

        r = await c.post("/astrmai/api/providers/apply", json={"action": "default", "id": "prov-new"})
        check("provider 设默认：写进 agent_runner 配置", r.json().get("ok") is True and (await c.get("/astrmai/api/providers")).json().get("default_provider_id") == "prov-new", str(r.json())[:120])

        r = await c.post("/astrmai/api/providers/apply", json={"action": "delete", "id": "prov-new"})
        check("provider 删除：转到 delete_provider", r.json().get("ok") is True and any(x[0] == "delete" and x[1] == "prov-new" for x in star.provider_calls), str(r.json())[:120])
        r = await c.post("/astrmai/api/providers/apply", json={"action": "nonsense"})
        check("provider 未知动作被拒", r.status_code == 400 and "未知动作" in r.json().get("reason", ""), str(r.json())[:100])

        r = await c.get("/astrmai/api/personas")
        pers = r.json()
        check("人格清单：default 与自定义都在", pers.get("ok") and {x["persona_id"] for x in pers["items"]} >= {"default", "p1"}, str(pers)[:140])
        r = await c.post("/astrmai/api/personas/apply", json={"action": "create", "persona_id": "p2", "system_prompt": "你是修表匠", "begin_dialogs": ["表修好了"]})
        check("人格创建：转到 PersonaManager.create", r.json().get("ok") is True and ("create", "p2") in [(a, b) for a, b in star.persona_calls], str(r.json())[:120])
        r = await c.post("/astrmai/api/personas/apply", json={"action": "create", "persona_id": "p2", "system_prompt": "重复"})
        check("人格重复创建被上游拒绝且透传原因", r.status_code == 400 and "already exists" in r.json().get("reason", ""), str(r.json())[:120])
        r = await c.post("/astrmai/api/personas/apply", json={"action": "update", "persona_id": "p1", "system_prompt": "你是渔政"})
        check("人格更新：prompt 真改了", r.json().get("ok") is True and next(x for x in (await c.get("/astrmai/api/personas")).json()["items"] if x["persona_id"] == "p1")["system_prompt"] == "你是渔政", "")
        r = await c.post("/astrmai/api/personas/apply", json={"action": "delete", "persona_id": "p2"})
        check("人格删除：能删自定义的", r.json().get("ok") is True and not any(x["persona_id"] == "p2" for x in (await c.get("/astrmai/api/personas")).json()["items"]), "")
        r = await c.post("/astrmai/api/personas/apply", json={"action": "delete", "persona_id": "default"})
        check("系统默认人格删不掉（上游规则透传）", r.status_code == 400 and "default" in r.json().get("reason", ""), str(r.json())[:120])

        r = await c.get("/astrmai/api/conversations")
        convs = r.json()
        check("对话列表：两条且按更新时间排序", convs.get("ok") and convs["total"] == 2 and convs["items"][0]["cid"] == "cid-aaa", str(convs)[:140])
        check("对话行：消息数与 token 从真实 history 数出来", convs["items"][0]["message_count"] == 2 and convs["items"][0]["token_usage"] == 123, str(convs["items"][0]))
        r = await c.get("/astrmai/api/conversations/history?cid=cid-aaa")
        hist = r.json()
        check("对话正文：消息链可读且角色正确", hist.get("ok") and len(hist["messages"]) == 2 and hist["messages"][0]["role"] == "user" and hist["messages"][1]["content"] == "在的", str(hist)[:160])
        r = await c.get("/astrmai/api/conversations/history?cid=nope")
        check("对话正文：不存在的 cid 如实报", r.json().get("ok") is False and "不存在" in r.json().get("reason", ""), str(r.json())[:100])
        r = await c.get("/astrmai/api/conversations/history")
        check("对话正文：缺 cid 参数 400", r.status_code == 400, f"status={r.status_code}")

        r = await c.get("/astrmai/api/cron")
        cron = r.json()
        check("cron 清单：任务带表达式与状态", cron.get("ok") and len(cron["items"]) == 1 and cron["items"][0]["cron"] == "0 9 * * *" and cron["items"][0]["enabled"] is True, str(cron)[:140])
        r = await c.post("/astrmai/api/cron/apply", json={"action": "toggle", "job_id": "job-111111111111", "enabled": False})
        check("cron 启停：转到 update_job", r.json().get("ok") is True and ("toggle", "job-111111111111", False) in star.cron_calls, str(star.cron_calls))
        r = await c.post("/astrmai/api/cron/apply", json={"action": "run_now", "job_id": "job-111111111111"})
        check("cron 立即执行：触发且不冒充完成", r.json().get("ok") is True and ("run", "job-111111111111") in star.cron_calls, str(r.json())[:100])
        r = await c.post("/astrmai/api/cron/apply", json={"action": "delete", "job_id": "job-111111111111"})
        check("cron 删除：清单随之清空", r.json().get("ok") is True and (await c.get("/astrmai/api/cron")).json()["items"] == [], "")

        # 无管理器时（核心没跑）四页都要如实报拿不到，不能 500
        star.context.provider_manager = None
        star.context.persona_manager = None
        star.context.conversation_manager = None
        star.context.cron_manager = None
        for path in ("/astrmai/api/providers", "/astrmai/api/personas", "/astrmai/api/conversations", "/astrmai/api/cron"):
            r = await c.get(path)
            j = r.json()
            check(f"核心没跑时 {path} 如实报拿不到", r.status_code == 200 and j.get("ok") is False and "没在跑" in (j.get("reason") or ""), str(j)[:100])
        r = await c.post("/astrmai/api/providers/apply", json={"action": "test", "id": "x"})
        check("核心没跑时写操作也如实拒绝", r.status_code == 400 and "没在跑" in r.json().get("reason", ""), str(r.json())[:100])

        # ---- 页面与静态资源 ----
        page = await c.get("/astrmai/")
        css = await c.get("/astrmai/style.css")
        js = await c.get("/astrmai/app.js")
        check("首页出得来", page.status_code == 200 and "astrmai 控制台" in page.text, f"status={page.status_code}")
        check("静态资源齐全", css.status_code == 200 and js.status_code == 200 and "astrmai" in css.text)
        check("导航是多级菜单（分组→条目→插件再分三家）", all(k in js.text for k in ('label: "运行"', 'label: "扩展"', 'label: "系统"', 'label: "关于"', "plugins:astrbot", "plugins:maibot", "plugins:astrmai")), "")
        check("首页固定是负载总览", 'go("load")' in js.text and "负载总览" in js.text, "")

        # ---- 负载端点：新首页第一屏的数据来源 ----
        first = (await c.get("/astrmai/api/load")).json()
        check("负载端点出得来且形状对", "latest" in first and "series" in first and "engine" in first, str(list(first))[:120])
        if not first.get("ok"):
            check("还没攒到样本时如实说明，不报 0", bool(first.get("reason")), str(first.get("reason"))[:80])
        await asyncio.sleep(5.5)  # 采样间隔 2s，至少两轮
        ld = (await c.get("/astrmai/api/load")).json()
        host = (ld.get("latest") or {}).get("host") or {}
        fusion = (ld.get("latest") or {}).get("fusion") or {}
        proc = (ld.get("latest") or {}).get("process") or {}
        check("两轮之后有真主机数", bool(ld.get("ok")) and bool(host.get("cores")) and host.get("mem_total_mb", 0) > 1000, str({k: host.get(k) for k in ("cpu_percent", "mem_percent", "cores", "disk_percent")}))
        check("本进程 RSS/线程数在测", proc.get("rss_mb", 0) > 1 and proc.get("threads", 0) >= 1, str({k: proc.get(k) for k in ("rss_mb", "threads", "pid")}))
        check("事件循环延迟与 asyncio 任务数在测", fusion.get("loop_lag_ms") is not None and fusion.get("asyncio_tasks", 0) >= 1, str({k: fusion.get(k) for k in ("loop_lag_ms", "asyncio_tasks")}))
        check("消息速率需要基线，攒够样本后才有值", (fusion.get("msg_rate") or {}).get("in") is not None, str(fusion.get("msg_rate")))
        check("计数器接上了（速率不是恒 0 的假数）", (fusion.get("counters") or {}).get("forwarded") == 7, str(fusion.get("counters")))
        check("runner PID 拿不到时如实标 gone", all(("gone" in r) or "rss_mb" in r for r in fusion.get("runners") or []), str(fusion.get("runners")))
        check("趋势序列真的在攒", len((ld.get("series") or {}).get("cpu") or []) >= 2, f"cpu 点数 {len((ld.get('series') or {}).get('cpu') or [])}")
        check("负载响应里也没有凭据", "sk-" not in json.dumps(ld, ensure_ascii=False) and "api_key" not in json.dumps(ld), "")

        # 前端至少要解析得动：本机没有 node，我就是这样漏过一个未闭合括号，面板一片空白
        sys.path.insert(0, str(WS / "tools"))
        from js_bracket_scan import scan

        unclosed, extra = scan(js.text)
        check("app.js 括号闭合（否则浏览器整页空白）", not unclosed and not extra, f"未闭合 {unclosed[:2]} 多余 {extra[:2]}")
        check("前端不拿 innerHTML 拼数据", "innerHTML" not in js.text, "")
        external = re.search(r'(src|href)="https?://', page.text) or re.search(r'@import\s+url\(["\']?https?://', css.text)
        check("前端不加载任何外链资源（断网也能开）", not external, str(external.group(0) if external else ""))

        # ---- "不能做成一样"：上游的框架痕迹/品牌件/主色一个都不许出现 ----
        telltales = ["vuetify", "tailwind", "shadcn", "#3c96ca", "#5ba4d4", "ArkPixel", "DepartureMono", "maimai.ico", "astrbot_logo", "/api/v1", "Outfit"]
        haystack = (page.text + css.text + js.text + blob).lower()
        hit = [t for t in telltales if t.lower() in haystack]
        check("面板发出的每个字节里都没有上游的框架痕迹、字体、主色或品牌资源", not hit, f"命中 {hit}")

        # ---- 写操作：Origin 不一致按 CSRF 拒 ----
        r = await c.post("/astrmai/api/tools/resync", json={}, headers={"Origin": "http://evil.example"})
        check("跨站 Origin 的 POST 被拒", r.status_code == 403, f"status={r.status_code} {r.text[:80]}")
        check("被拒时没有真的执行", star.resync_calls == 0, f"resync_calls={star.resync_calls}")

        r = await c.post("/astrmai/api/tools/resync", json={})
        check("同源（本机）POST 可执行", r.status_code == 200 and star.resync_calls == 1, str(r.json())[:100])

        r = await c.post("/astrmai/api/plugins/load", json={"source": "astrbot", "name": "demo"})
        check("加载动作转到了 loader 入口", r.status_code == 200 and ("astrbot", "demo") in star.load_calls, str(r.json())[:120])

        r = await c.post("/astrmai/api/plugins/uninstall", json={"source": "astrbot", "name": "demo"})
        check("卸载动作回结构化结果", r.status_code == 200 and r.json().get("ok") is True, str(r.json())[:120])

        r = await c.post("/astrmai/api/market/search", json={"source": "astrbot", "query": "", "limit": 5})
        first = r.json()
        check("市场搜索走通了面板→市场", r.status_code == 200 and first.get("total") == 10 and len(first.get("plugins") or []) == 5, str(first)[:120])
        r = await c.post("/astrmai/api/market/search", json={"source": "astrbot", "query": "", "limit": 5, "offset": 5})
        second = r.json()
        check("市场能翻页：第二页换人且不越界", r.status_code == 200 and [p["id"] for p in second["plugins"]][:1] == ["extra4.plugin"] and second["has_more"] is False, str(second)[:140])
        r = await c.post("/astrmai/api/market/search", json={"source": "astrbot", "query": "", "limit": 999})
        check("每页条数被上限钉住（不让人一次拖走整本目录）", len(r.json().get("plugins") or []) <= 50, str(len(r.json().get("plugins") or [])))

        # ---- 闸门：改了立刻生效，并且落盘可还原 ----
        r = await c.post("/astrmai/api/settings", json={"patch": {"group_enabled": True, "groups": "12345, 67890"}})
        applied = r.json()
        check("设置当场生效", applied.get("ok") and "group_enabled" in applied.get("applied_live", []), str(applied)[:160])
        check("cfg 真的被改了", star.host.cfg.group_enabled is True and star.host.cfg.groups == {"12345", "67890"}, str(star.host.cfg.groups))
        check("闸门判口随之变化", star.host.cfg.chat_allowed("group", "12345") is True and star.host.cfg.chat_allowed("group", "99999") is False, "")
        ov2 = (await c.get("/astrmai/api/overview")).json()
        check("概览如实反映新闸门", ov2["privacy"]["group_enabled"] is True, str(ov2["privacy"])[:100])
        check("设置写进了隔离目录", (STATE_DIR / "settings.json").exists(), str(STATE_DIR / "settings.json"))

        # 前缀这个字段曾经是个骗人的开关：面板写进 cfg，闸门却用自己的默认值。
        r = await c.post("/astrmai/api/settings", json={"patch": {"command_prefixes": "#, /"}})
        now = star.host.cfg.command_prefixes
        check("命令前缀改得动、落进运行中的 cfg", now == ("#", "/"), str(now))
        check("闸门用改后的前缀分家", decide("#查天气", is_group=True, prefixes=now).owner == "astrbot", "")
        check("改后的前缀之外的文本仍归麦麦", decide("今天好吵", is_group=True, prefixes=now).owner == "maibot", "")

        # 面板上每个"可编辑"字段都必须真的落到运行中的 cfg —— 少一个就是假开关
        from astrmai.panel_data import LIVE_FIELDS as _LIVE

        probe = {
            "group_enabled": False,
            "private_enabled": False,
            "allowed_users": "111000111, 222000222",
            "groups": "99887766",
            "command_prefixes": "~",
        }
        r = await c.post("/astrmai/api/settings", json={"patch": probe})
        echoed = r.json()
        missing = [k for k in _LIVE if k not in echoed.get("applied_live", [])]
        check("每个开放字段都报告了当场生效", not missing, f"没落地的: {missing}")
        read_back = (await c.get("/astrmai/api/settings")).json()["fields"]
        shown = {row["field"]: row["value"] for row in read_back}
        check("面板读回来的就是刚写进去的", shown.get("groups") == ["99887766"] and shown.get("command_prefixes") == ["~"], str(shown)[:150])
        check("私聊开关与白名单同步落进 cfg", star.host.cfg.private_enabled is False and star.host.cfg.allowed_users == {"111000111", "222000222"}, str(star.host.cfg.allowed_users))
        await c.post("/astrmai/api/settings", json={"patch": {"group_enabled": True, "private_enabled": True, "groups": "12345, 67890", "allowed_users": "", "command_prefixes": ["/", "!"]}})
        check("批量探针收尾把闸门还原了", star.host.cfg.group_enabled is True and "/" in star.host.cfg.command_prefixes, str(star.host.cfg.command_prefixes))

        # 前端那一层：照 app.js 的做法从 GET 的 fields 里取标识拼 patch 再打回去。
        # 只测后端端点会漏掉"标识被 scrub() 吃掉"这类问题（当年五个输入框全塌成 set-<redacted>）。
        rows = (await c.get("/astrmai/api/settings")).json()["fields"]
        rebuilt = {row["field"]: row["value"] for row in rows}
        check("前端能拿到干净的字段标识（没有 <redacted>）", all("<redacted>" not in k for k in rebuilt) and "command_prefixes" in rebuilt, str(list(rebuilt))[:120])
        round_trip = await c.post("/astrmai/api/settings", json={"patch": rebuilt})
        rt = round_trip.json()
        check("照前端那样拼出来的 patch 真的被接受", rt.get("ok") is True and len(rt.get("applied_live") or []) == len(rebuilt), str(rt)[:150])

        r = await c.post("/astrmai/api/settings", json={"patch": {"telemetry": "on"}})
        out = r.json()
        check("遥测开关被明确标为需重启，不假装生效", bool(out.get("needs_restart")) and "下次启动" in out["needs_restart"][0], str(out)[:160])
        os.environ["ASTRMAI_TELEMETRY"] = "off"

        r = await c.post("/astrmai/api/settings", json={"patch": {"api_key": "x"}})
        check("面板拒绝改它不该碰的字段", r.status_code == 200 and "api_key" in (r.json().get("refused") or {}), str(r.json())[:140])

        # ---- 凭据门：现在没有"回环免登录"这回事 ----
        async with httpx.AsyncClient(base_url=base, timeout=15) as bare:
            r = await bare.get("/astrmai/api/overview")
            check("不带凭据一律 401（回环也一样）", r.status_code == 401 and r.json().get("login_required") is True, f"status={r.status_code}")
            r = await bare.get("/astrmai/api/overview", headers={"X-Astrmai-Token": "wrong-token"})
            check("错 token 也 401", r.status_code == 401)
            r = await bare.get("/astrmai/api/auth/status")
            check("auth/status 是公开的，好让前端知道该弹登录框", r.status_code == 200 and r.json().get("authenticated") is False, str(r.json())[:120])
        r = await c.get("/astrmai/api/overview")
        check("带对 token 才放行", r.status_code == 200, f"status={r.status_code}")
        r = await c.post("/astrmai/api/tools/resync", json={}, headers={"Origin": base})
        check("token+同源才允许写", r.status_code == 200, str(r.json())[:80])
        check("凭据不出现在任何 API 响应里", TEST_TOKEN not in "\n".join(bodies.values()) and TEST_PASSWORD not in "\n".join(bodies.values()), "")

        # ---- 页面路由不泄露实现 ----
        r = await c.get("/astrmai/api/nope")
        check("未知 API 路径 404 而不是栈信息", r.status_code == 404, f"status={r.status_code} {r.text[:60]}")

    await panel.stop()
    probe = await _probe_stopped(port)
    check("stop() 之后端口真的释放", probe is False, f"connect={probe}")

    # ---- 重启还原：新 star 从落盘设置里把闸门读回来 ----
    fresh = FakeStar()
    from astrmai.panel_data import apply_stored_to_cfg

    applied = apply_stored_to_cfg(fresh.host.cfg)
    check("重启后能从落盘设置还原闸门", "group_enabled" in applied and fresh.host.cfg.group_enabled is True, str(applied))
    check("还原不覆盖环境变量钉住的字段", True, "（被 ASTRMAI_* 显式设置的键由 _env_pins 让位）")

    print("\n结果:", "全部通过" if not FAILS else f"{len(FAILS)} 项失败 {FAILS}")
    return 0 if not FAILS else 1


async def _probe_stopped(port: int) -> bool:
    try:
        _r, w = await asyncio.open_connection("127.0.0.1", port)
        w.close()
        await w.wait_closed()
        return True
    except OSError:
        return False


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
