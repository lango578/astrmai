"""astrmai AstrBot 插件：把麦麦（MaiBot）的群聊人格引擎挂在 AstrBot 的平台层上。

分工由 gate.decide 决定：命令/插件已接管的消息留在 AstrBot，其余群聊与私聊交给麦麦的脑子，
麦麦的出站再通过 context.send_message 走 AstrBot 自己的连接，所以全程只有一个 QQ 连接。
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.message.components import At, AtAll, Image, Plain, Reply
from astrbot.core.star.filter.command import GreedyStr

PLUGIN_DIR = Path(__file__).resolve().parent


def _bridge_layout() -> tuple[bool, Path]:
    """桥接包在哪，取决于两种布局，判一次就好，别在每个调用点各猜。

    市场安装：AstrBot 用 `data.plugins.<名>.main` 这个点号名导入插件，而且只把 AstrBot
    项目根放进 sys.path（star_manager 不加插件目录），所以 `astrmai/` 必须在插件目录内、
    走相对导入；导进来后挂成顶层名 `astrmai`，下面十几处 `from astrmai.x import y` 照旧。
    仓库/靶场：`astrmai/` 在工作区根，走 sys.path。
    """

    if (PLUGIN_DIR / "astrmai").is_dir():
        if __package__:
            from . import astrmai as _pkg  # type: ignore

            sys.modules["astrmai"] = _pkg
        elif str(PLUGIN_DIR) not in sys.path:
            sys.path.insert(0, str(PLUGIN_DIR))
        return True, PLUGIN_DIR
    workspace = Path(os.environ.get("ASTRMAI_WORKSPACE", PLUGIN_DIR.parent))
    if str(workspace) not in sys.path:
        sys.path.insert(0, str(workspace))
    return False, workspace


MARKET_LAYOUT, WORKSPACE = _bridge_layout()

from astrmai.config import AstrmaiConfig  # noqa: E402
from astrmai.credentials import state_dir  # noqa: E402
from astrmai.gate import decide  # noqa: E402
from astrmai.host import MaiBotHost  # noqa: E402

STATE_DIR = state_dir()


def _astrbot_root() -> Path:
    try:
        import astrbot

        return Path(astrbot.__file__).resolve().parents[1]
    except Exception:  # noqa: BLE001 - 拿不到就退回工作区，面板会如实报"未知"
        return WORKSPACE


# 市场布局下麦麦不随包走（16MB 上限），所以默认路径指向可写的 plugin_data，而不是插件目录本身
MAI_ROOT = Path(os.environ.get("ASTRMAI_MAI_ROOT") or (STATE_DIR / "MaiBot" if MARKET_LAYOUT else WORKSPACE / "MaiBot-main"))
NATIVE_ROOT = Path(os.environ.get("ASTRMAI_NATIVE_PLUGINS") or (STATE_DIR / "astrmai_plugins" if MARKET_LAYOUT else WORKSPACE / "astrmai_plugins"))
ASTRBOT_ROOT = Path(os.environ.get("ASTRBOT_ROOT") or (_astrbot_root() if MARKET_LAYOUT else WORKSPACE / "vendor" / "AstrBot-master"))


def _segments_to_chain(segments: list[dict]) -> MessageChain:
    """OneBot v11 段 -> AstrBot MessageChain。"""

    chain = []
    for seg in segments:
        typ = seg.get("type")
        data = seg.get("data")
        if typ == "text":
            text = str((data or {}).get("text", ""))
            if text:
                chain.append(Plain(text=text))
        elif typ == "at":
            qq = str((data or {}).get("qq", ""))
            chain.append(AtAll() if qq == "all" else At(qq=qq, name=str((data or {}).get("name", ""))))
        elif typ == "reply":
            chain.append(Reply(id=str((data or {}).get("id", ""))))
        elif typ == "image":
            file = str((data or {}).get("file", ""))
            if file.startswith("base64://"):
                chain.append(Image.fromBase64(file[len("base64://") :]))
            elif file.startswith("http"):
                chain.append(Image.fromURL(file))
        elif typ == "emoji":
            payload = data if isinstance(data, str) else ""
            if payload:
                chain.append(Image.fromBase64(payload))
    return MessageChain(chain=chain)


class Main(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        # AstrBot 的 Star.__init__ 收下 config 却并不存成 self.config（core/star/base.py），
        # 不自己留一份的话，_conf_schema.json 上每个框都是装饰 —— 填了没人读。
        self.config = config or {}
        self.host: MaiBotHost | None = None
        self.booted = False
        self.boot_error: str = ""
        self.forwarded = 0
        self.sent_total = 0
        # (chat_type, target_id) -> unified_msg_origin，出站要靠它找回会话与平台连接
        self._umo_by_target: dict[tuple[str, str], str] = {}
        #: 运行中加载被拒的记录（典型：要装 pip 依赖被拒）；面板的「加载拒绝」卡读这里
        self.load_denials: list[dict] = []
        # 每个会话最近一次的 AstrBot 事件，供麦麦反向调用插件工具时复用
        self._event_by_target: dict[tuple[str, str], Any] = {}
        self.federation: dict[str, Any] = {}
        self.native: Any = None
        self._market_obj: Any = None
        self._last_tool_sync = 0.0
        self._task: asyncio.Task | None = None
        # 面板用：裁决流水 + 日志环 + 可选控制台服务
        from astrmai.panel_data import Ledger, LogRing

        self.ledger = Ledger()
        self.logring = LogRing()
        self.panel: Any = None
        self.panel_error: str = ""
        self.settings_applied: list[str] = []

    async def initialize(self) -> None:
        if os.environ.get("ASTRMAI_ENABLE", "1") == "0":
            self.boot_error = "ASTRMAI_ENABLE=0，引擎未启动"
            self._write_status()
            return
        self._task = asyncio.create_task(self._boot(), name="astrmai-boot")

    def _pref(self, key: str, env: str, default: Any = None) -> Any:
        """取值优先级：环境变量 > AstrBot 面板里的插件配置 > 默认值。

        市场安装的用户只会去面板填 `_conf_schema.json` 那几个框，所以这条链必须真的被运行时读到；
        测试会改 config 再走一遍启动，验证"填了不等于没填"。
        """

        raw = (os.environ.get(env) or "").strip()
        if raw:
            return raw
        conf = getattr(self, "config", None)
        if conf:
            try:
                value = conf.get(key)
            except Exception:  # noqa: BLE001 - 配置对象不是 dict 就当作没配
                value = None
            if isinstance(value, str):
                value = value.strip()
            if value not in (None, "", []):
                return value
        return default

    async def _boot(self) -> None:
        mai_root = Path(str(self._pref("mai_root", "ASTRMAI_MAI_ROOT", str(MAI_ROOT))))
        allowed = {s.strip() for s in str(self._pref("allowed_users", "ASTRMAI_ALLOWED_USERS", "")).split(",") if s.strip()}
        groups = {s.strip() for s in str(self._pref("groups", "ASTRMAI_GROUPS", "")).split(",") if s.strip()}
        cfg = AstrmaiConfig(
            mai_root=mai_root,
            platform=str(self._pref("platform", "ASTRMAI_PLATFORM", "qq")),
            account_id=str(self._pref("account_id", "ASTRMAI_ACCOUNT_ID", "")),
            allowed_users=allowed,
            groups=groups,
            group_enabled=str(self._pref("allow_groups", "ASTRMAI_ALLOW_GROUPS", "0")).lower() in ("1", "true", "yes", "on"),
            private_enabled=str(self._pref("allow_private", "ASTRMAI_ALLOW_PRIVATE", "1")).lower() in ("1", "true", "yes", "on"),
        )
        # 面板改过的闸门要能在重启后还在（环境变量显式钉住的字段优先）
        from astrmai.panel_data import apply_stored_to_cfg

        self.settings_applied = apply_stored_to_cfg(cfg)
        try:
            self.host = MaiBotHost(cfg, self._deliver_outbound)
            await self.host.boot()
            self.booted = True
            try:
                self._install_federation()
            except Exception as fed_exc:  # noqa: BLE001 - 联邦失败不该让引擎整体起不来
                self.federation = {"error": f"{type(fed_exc).__name__}: {fed_exc}"}
        except Exception as exc:  # noqa: BLE001 - 引擎起不来不能拖垮 AstrBot
            self.boot_error = f"{type(exc).__name__}: {exc}"
            self.host = None
            self.booted = False
        try:
            await self._start_panel()
        except Exception as exc:  # noqa: BLE001 - 面板出任何问题都不该让引擎的 boot 状态写不出来
            self.panel_error = f"{type(exc).__name__}: {exc}"
            print(f"[astrmai] 控制台接线失败: {self.panel_error}", flush=True)
        self._write_status()

    async def _start_panel(self) -> None:
        """按需起 astrmai 自己的控制台；失败只记错，绝不影响两个脑子。"""

        enable = str(self._pref("panel_enable", "ASTRMAI_PANEL", "off")).strip().lower()
        if enable not in ("1", "on", "true", "yes"):
            return
        from astrmai.panel import AstrmaiPanel

        # 值直接传进 start()：写回 os.environ 会让下一次启动被自己写的那条挡住
        host = str(self._pref("panel_host", "ASTRMAI_PANEL_HOST", "127.0.0.1"))
        port = str(self._pref("panel_port", "ASTRMAI_PANEL_PORT", "18124"))
        panel = AstrmaiPanel(lambda: self, ledger=self.ledger, logs=self.logring)
        try:
            await panel.start(host=host, port=int(port))
        except Exception as exc:  # noqa: BLE001
            self.panel_error = f"{type(exc).__name__}: {exc}"
            print(f"[astrmai] 面板启动失败: {self.panel_error}", flush=True)
            return
        self.panel = panel
        info = panel.status()
        tls_note = "TLS 已启用" if info["tls"] else "未启用 TLS（只该在回环或已加密链路里用）"
        print(
            f"[astrmai] 控制台: {info['scheme']}://{info['host']}:{info['port']}/astrmai/ "
            f"（需要凭据登录；初始口令 astrmai-init，登录后必须改，且只有本机认它。{tls_note}）",
            flush=True,
        )

    def _install_federation(self) -> None:
        """双向工具联邦：麦麦能用 AstrBot 插件，AstrBot 智能体也能用麦麦的工具。"""

        from astrbot.core.agent.tool import FunctionTool  # noqa: F401  仅确认可导入

        from astrmai import federation as fed

        manager = self.context.get_llm_tool_manager()

        mirror = fed.AstrBotToolMirror(
            list_tools=lambda: fed.collect_astrbot_tools(manager),
            run_tool=lambda name, args, mb_ctx: self._run_astrbot_tool(name, args, mb_ctx),
            # 默认延迟发现：麦麦的 planner 只把 metadata.visibility == "visible" 的非内置工具直接摊给模型
            # （src/maisaka/focus/tools.py: is_default_visible），其余进 deferred 池由她自己的
            # tool_search 按需捞出来。全 visible 会把 planner 的 prompt 挤爆。
            visible=os.environ.get("ASTRMAI_AB_TOOLS_VISIBLE", "0") == "1",
        )
        self.federation = fed.install_mirror(mirror)

        try:
            self.federation["mai_tools"] = self._sync_maibot_tools()
        except Exception as exc:  # noqa: BLE001
            self.federation["mai_tools_error"] = f"{type(exc).__name__}: {exc}"

        # astrmai 原生插件：一次开发，两边同时可用
        try:
            from astrmai.native_plugin import NativeRegistry

            native_root = NATIVE_ROOT
            self.native = NativeRegistry(self)
            summary = self.native.bootstrap_all(native_root)
            self.federation["native"] = summary
            if summary["tools"]:
                self.context.add_llm_tools(*self.native.astrbot_function_tools())
                fed.install_mirror(
                    # 同上：原生插件默认也走延迟发现，别替麦麦决定她的 planner 要看多少工具
                    self.native.make_maibot_mirror(visible=os.environ.get("ASTRMAI_NATIVE_VISIBLE", "0") == "1")
                )
                self.federation["native_mirror"] = True
        except Exception as exc:  # noqa: BLE001
            self.federation["native_error"] = f"{type(exc).__name__}: {exc}"

        # 只注册命令、没注册 LLM 工具的 AstrBot 插件：也做成工具
        try:
            from astrmai import command_tools as ct

            mode = os.environ.get("ASTRMAI_COMMAND_TOOLS", "deferred").strip().lower()
            if mode != "off":
                handlers = ct.list_command_handlers()

                def list_cmds() -> list[dict]:
                    return [
                        {
                            "name": h["name"],
                            "description": h["description"],
                            "parameters": h["parameters"],
                        }
                        for h in ct.list_command_handlers()
                    ]

                cmd_mirror = fed.AstrBotToolMirror(
                    list_tools=list_cmds,
                    run_tool=ct.make_command_tool_runner(
                        lambda: self.context.get_config(),
                        self._event_for_mb_context,
                    ),
                    visible=mode == "visible",
                    prefix="abcmd_",
                    provider_name="astrbot_commands",
                )
                self.federation["command_tools"] = {
                    "count": len(handlers),
                    "mode": mode,
                    **fed.install_mirror(cmd_mirror),
                }
            else:
                self.federation["command_tools"] = {"count": 0, "mode": "off"}
        except Exception as exc:  # noqa: BLE001
            self.federation["command_tools_error"] = f"{type(exc).__name__}: {exc}"

    def _sync_maibot_tools(self) -> dict:
        """把麦麦的工具（内置 + 自家插件）重扫一遍同步进 AstrBot 的工具表。

        麦麦插件可以在运行中被安装/启用，所以这里按名字集合做增删，不是一次性快照。
        """

        from astrmai import federation as fed

        manager = self.context.get_llm_tool_manager()
        specs = fed.list_maibot_tools()
        desired = {fed.MB_PREFIX + s["name"] for s in specs}
        current = {t.name for t in getattr(manager, "func_list", []) if t.name.startswith(fed.MB_PREFIX)}

        for stale in current - desired:
            try:
                manager.remove_func(stale)
            except Exception:  # noqa: BLE001
                manager.func_list = [t for t in manager.func_list if t.name != stale]

        fresh = [s for s in specs if fed.MB_PREFIX + s["name"] not in current]
        if fresh:
            self.context.add_llm_tools(*fed.make_maibot_function_tools(fresh))

        # 反方向的镜像（AstrBot→麦麦）真正暴露了多少个工具：以前没人算过，面板上永远是 0
        try:
            self.federation["ab_tools"] = len(fed.collect_astrbot_tools(manager))
        except Exception as exc:  # noqa: BLE001
            self.federation["ab_tools_error"] = f"{type(exc).__name__}: {exc}"

        summary = {
            "total": len(desired),
            "added": len(fresh),
            "removed": len(current - desired),
            "plugin_sourced": sum(1 for s in specs if s.get("provider") not in (None, "", "maisaka_builtin")),
        }
        self.federation["ab_tools"] = len(fed.collect_astrbot_tools(manager))
        return summary

    def _event_for_mb_context(self, mb_context: Any) -> Any:
        """按麦麦的执行上下文找回该会话最近一次的 AstrBot 事件。"""

        is_group = bool(getattr(mb_context, "is_group_chat", False))
        target = str(getattr(mb_context, "group_id", "") if is_group else getattr(mb_context, "user_id", "") or "")
        return self._event_by_target.get(("group" if is_group else "private", target))

    async def _run_astrbot_tool(self, name: str, args: dict, mb_context: Any) -> str:
        from astrbot.core.platform.astr_message_event import event_context

        from astrmai import federation as fed

        event = self._event_for_mb_context(mb_context)
        if event is None:
            return "这个会话还没有可用作上下文的消息，AstrBot 工具暂时调不动。"
        with event_context(event):
            return await fed.call_astrbot_tool(
                self.context.get_llm_tool_manager(),
                name,
                args,
                event,
            )

    def _write_status(self) -> None:
        """状态落盘，给外部健康探针和靶场用；写失败不影响主流程。"""

        path = os.environ.get("ASTRMAI_STATUS_FILE")
        if not path:
            return
        payload = {
            "booted": self.booted,
            "boot_error": self.boot_error,
            "boot_ms": round(self.host.boot_ms) if self.host else None,
            "forwarded": self.forwarded,
            "sent": self.sent_total,
            "sessions": len(self._umo_by_target),
            "driver": self.host.driver.driver_id if self.host and self.host.driver else None,
            "live_runners": len(self.host.live_runners()) if self.host else 0,
            "job_guard": self.host.job.status() if self.host and self.host.job else {"mode": "none"},
            "panel": self.panel.status() if self.panel else None,
            "panel_error": self.panel_error,
            "federation": self.federation,
            "updated_at": time.time(),
        }
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    async def terminate(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self.panel is not None:
            try:
                await self.panel.stop()
            except Exception as exc:  # noqa: BLE001 - 面板关不掉也不该拦住引擎关停
                print(f"[astrmai] 面板关停失败: {type(exc).__name__}: {exc}", flush=True)
            self.panel = None
        if self.host is not None:
            await self.host.shutdown()
            self.host = None
        self.booted = False

    async def _deliver_outbound(self, chat_type: str, target_id: str, segments: list[dict]) -> str | None:
        umo = self._umo_by_target.get((chat_type, str(target_id)))
        if not umo:
            raise RuntimeError(f"未知的出站会话 {chat_type}:{target_id}，还没收到它的入站消息")
        chain = _segments_to_chain(segments)
        if not chain.chain:
            return None
        await self.context.send_message(umo, chain)
        self.sent_total += 1
        return f"umo:{umo}"

    def _remember_session(self, event: AstrMessageEvent) -> None:
        if event.is_private_chat():
            self._umo_by_target.setdefault(("private", str(event.get_sender_id())), event.unified_msg_origin)
        else:
            self._umo_by_target.setdefault(("group", str(event.get_group_id())), event.unified_msg_origin)
        if not self.host or not self.host.cfg.account_id:
            if self.host:
                self.host.cfg.account_id = str(event.get_self_id() or "")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-100)
    async def to_maibot(self, event: AstrMessageEvent):
        """在所有插件命令之后接手：把自然对话喂给麦麦的脑子，并阻止 AstrBot 自己再回一遍。"""

        if not self.booted or self.host is None:
            return
        if event.is_stopped():
            return

        raw = event.message_obj.raw_message
        text = event.get_message_str() or ""
        is_group = not event.is_private_chat()
        chat_type = "group" if is_group else "private"
        target_id = str(event.get_group_id() if is_group else event.get_sender_id() or "")
        # 硬白名单：不在名单里的会话连转发都不会发生，陌生人问也不答。
        if not self.host.cfg.chat_allowed(chat_type, target_id):
            self.ledger.record(
                chat_type=chat_type,
                target_id=target_id,
                owner="none",
                reason="闸门沉默（该类会话被关闭或不在白名单）",
                preview=text,
            )
            return

        # 麦麦插件可能在运行中被装好/启用，这里限频重扫，让 AstrBot 侧跟上
        if time.time() - self._last_tool_sync > 30:
            self._last_tool_sync = time.time()
            try:
                self._sync_maibot_tools()
            except Exception:  # noqa: BLE001 - 重扫失败就用现有工具表，不影响本条消息
                pass
        self_id = str(event.get_self_id() or "")
        mentioned = False
        replied_to_bot = False
        for comp in event.message_obj.message or []:
            if isinstance(comp, At) and str(getattr(comp, "qq", "")) == self_id:
                mentioned = True
            if isinstance(comp, Reply) and str(getattr(comp, "sender_id", "")) == self_id:
                replied_to_bot = True

        decision = decide(
            text,
            is_group=is_group,
            prefixes=tuple(self.host.cfg.command_prefixes or ()),
            mentioned_bot=mentioned,
            replied_to_bot=replied_to_bot,
            plugin_claimed=bool(getattr(event, "_has_send_oper", False)),
            private_enabled=self.host.cfg.private_enabled,
        )
        if not decision.to_maibot:
            self.ledger.record(
                chat_type=chat_type,
                target_id=target_id,
                owner=decision.owner,
                reason=decision.reason,
                preview=text,
            )
            return

        self._remember_session(event)
        self._event_by_target[(chat_type, target_id)] = event
        if len(self._event_by_target) > 512:
            # 每个值都拖着整份原始事件，不能按会话数无界涨：丢掉最先见的一半，留热的
            for stale in list(self._event_by_target)[: len(self._event_by_target) - 256]:
                self._event_by_target.pop(stale, None)
        try:
            accepted = await self.host.deliver(dict(raw))
        except Exception as exc:  # noqa: BLE001
            yield f"astrmai 入站失败: {type(exc).__name__}: {exc}"
            return
        if accepted:
            self.forwarded += 1
            event.stop_event()
            self._write_status()
        self.ledger.record(
            chat_type=chat_type,
            target_id=target_id,
            owner="maibot",
            reason=decision.reason,
            preview=text,
            forwarded=bool(accepted),
            detail="" if accepted else "麦麦转换器没接受这条入站",
        )

    def _market(self):
        from astrmai.market import Market

        if self._market_obj is None:
            # 市场布局下缓存写进 plugin_data：插件目录本身会在更新时被删掉重建
            self._market_obj = Market(
                STATE_DIR if MARKET_LAYOUT else WORKSPACE,
                astrbot_root=ASTRBOT_ROOT,
                maibot_root=MAI_ROOT,
                native_root=NATIVE_ROOT,
            )
        return self._market_obj

    @filter.command("mai", alias={"麦麦"})
    async def mai(self, event: AstrMessageEvent, rest: GreedyStr = ""):
        """市场与融合的运维入口：/mai status | market | install | load。

        用 GreedyStr 接整行剩余参数——AstrBot 的命令参数是按声明逐个位置解析的，
        写 *args 拿不到任何东西。
        """

        admins = {s.strip() for s in os.environ.get("ASTRMAI_ADMINS", "").split(",") if s.strip()}
        if not admins:
            admins = set(self.host.cfg.allowed_users) if self.host else set()
        if not admins:
            # 名单空着不等于"谁都是管理员"。装插件和改运行状态是写操作，宁可拒绝并告诉他去哪儿配。
            yield (
                "还没配管理员：环境变量 ASTRMAI_ADMINS 或私聊白名单（--allow）至少给一个 QQ 号，"
                "否则 /mai 一律不放行。"
            )
            return
        if str(event.get_sender_id()) not in admins:
            yield "这个命令只对管理员开放。"
            return

        parts = [str(a).strip() for a in str(rest).split() if str(a).strip()]
        verb = parts[0].lower() if parts else "status"
        from astrmai.loader import LoadPolicy

        if verb in ("status", "状态", ""):
            yield self._status_text(event)
            return

        if verb in ("market", "市场", "search", "搜"):
            source = parts[1] if len(parts) > 1 else "astrbot"
            query = " ".join(parts[2:]) if len(parts) > 2 else ""
            try:
                result = await self._market().search(source, query, 10)
            except Exception as exc:  # noqa: BLE001
                yield f"搜索失败: {type(exc).__name__}: {exc}"
                return
            lines = [f"来源 {result['source']}｜目录 {result['catalog']}｜{result['total']} 条"]
            if result.get("notice"):
                lines.append(f"提示: {result['notice']}")
            for entry in result["plugins"][:10]:
                lines.append(f"- {entry['id']}｜{str(entry.get('name'))[:20]}｜{str(entry.get('description'))[:36]}")
            yield "\n".join(lines)
            return

        if verb in ("install", "装", "安装"):
            if len(parts) < 3:
                yield "用法: /mai install <astrbot|maibot|astrmai> <插件id或路径> [--deps]"
                return
            source, ident = parts[1], parts[2]
            allow_deps = "--deps" in parts[3:]
            if allow_deps:
                yield "已按你的要求允许 pip 安装依赖，执行中不会另发进度消息，可能要等一会儿。"
            try:
                out = await self._market().install(source, ident)
            except Exception as exc:  # noqa: BLE001
                yield f"安装失败: {type(exc).__name__}: {exc}"
                return
            if not out.get("ok"):
                yield f"未安装: {out.get('reason')}"
                return
            yield f"已落地: {out['path']}（{out.get('where', '')}）" + (
                f"\ncommit: {out.get('commit') or '未钉'}" if out.get("commit") is not None else ""
            )
            loaded = await self._load_installed(source, out, LoadPolicy(allow_deps=allow_deps))
            yield loaded
            return

        if verb in ("load", "加载"):
            if len(parts) < 3:
                yield "用法: /mai load <astrbot|maibot> <目录名或插件id> [--deps]"
                return
            allow_deps = "--deps" in parts[2:]
            yield await self._load_installed(parts[0], {"path": parts[1]}, LoadPolicy(allow_deps=allow_deps))
            return

        if verb in ("panel", "面板"):
            yield self._panel_text(reset=len(parts) >= 2 and parts[1] in ("reset", "重置"))
            return

        if verb in ("license", "licenses", "许可证"):
            if len(parts) >= 3 and parts[1] in ("astrbot", "maibot"):
                try:
                    probe = await self._market().probe_license(parts[1], parts[2])
                except Exception as exc:  # noqa: BLE001
                    yield f"许可证查询失败: {type(exc).__name__}: {str(exc)[:160]}"
                    return
                if probe.get("ok"):
                    yield (
                        f"{parts[1]}:{parts[2]} → {probe.get('license')}"
                        f"（依据：{probe.get('basis')}；置信：{probe.get('confidence')}）\n仓库 {probe.get('package')}"
                    )
                else:
                    yield f"查不到：{probe.get('reason')}"
                return
            yield self._license_text()
            return

        yield "用法: /mai status | /mai panel | /mai 许可证 [来源 <id>] | /mai market <来源> <关键词> | /mai install <来源> <id> | /mai load <来源> <名字>"

    def _license_text(self) -> str:
        """已装组件的许可证清单。认不出的显式标 unknown —— 要分发时这张表就是依据。"""

        try:
            rows = self._market().licenses()
        except Exception as exc:  # noqa: BLE001
            return f"许可证清单读不出来: {type(exc).__name__}: {str(exc)[:160]}"

        lines = ["已装组件许可证清单：", "组件 | 来源 | 许可证 | 依据 | 置信"]
        unknown = 0
        for r in rows:
            if r.get("undetermined"):
                unknown += 1
            lines.append(f"{r['component']} | {r['source']} | {r['license']} | {r['basis']} | {r['confidence']}")
        lines.append("")
        lines.append(f"合计 {len(rows)} 项，其中 {unknown} 项许可证无法确定（标 unknown，分发前必须人工确认或剔除）")
        lines.append("分发口径：AstrBot=AGPL-3.0-or-later、麦麦=GPL-3.0-or-later，合并作品只能整体按 AGPL-3.0-or-later 发放并附完整对应源码。")
        return "\n".join(lines)

    def _panel_text(self, reset: bool = False) -> str:
        if self.panel is None:
            if self.panel_error:
                return f"面板没起来：{self.panel_error}"
            return (
                "面板未启用。启动前设 ASTRMAI_PANEL=on（默认只监听 127.0.0.1:18124）；\n"
                "初始口令是公开写死的 astrmai-init，登录后必须先改掉，而且只有本机认这个口令。"
            )
        if reset:
            password = self.panel.reset_password()
            print(f"[astrmai] 面板已重置口令为 {password}（只显示这一次），所有会话已作废", flush=True)
            return "已重置：回到初始口令，只打在 astrmai 控制台日志里（不走 QQ），所有会话已作废，登录后必须先改密。"
        info = self.panel.status()
        pw = info["password"]
        lines = [
            f"控制台: {info['scheme']}://{info['host']}:{info['port']}/astrmai/",
            "TLS: " + ("已启用" if info["tls"] else "未启用（只该在回环或已加密链路里用）"),
            "口令存储: " + (f"{pw['algorithm']} 哈希，明文不落盘" if pw["configured"] else "还没建立"),
            "初始口令: "
            + ("仍是公开的 astrmai-init —— 登录后必须改掉，且只允许本机用它登录" if pw.get("is_default") else f"已改掉（{pw['updated_at']}）"),
            "机器 token: " + ("已配置（同样只存哈希）" if info["token"]["configured"] else "未配置"),
            f"活动会话: {info['sessions']} 个",
            "忘了密码: /mai panel reset（新密码只打在控制台，不走 QQ）",
        ]
        return "\n".join(lines)

    async def _load_installed(self, source: str, out: dict, policy) -> str:
        """把刚落地的插件装进正在跑的脑子，并报告工具数变化。"""

        from astrmai.loader import load_astrbot, load_maibot

        path = Path(str(out.get("path", "")))
        if source == "astrbot":
            manager = getattr(self.context, "_star_manager", None)
            if manager is None:
                return "拿不到 AstrBot 的 PluginManager，只能重启后由它自己扫描加载。"
            res = await load_astrbot(manager, path.name, policy)
            note = "已加载" if res.get("ok") else f"未加载: {res.get('reason') or res.get('error') or res.get('failure_record')}"
            if not res.get("ok") and "pip 安装依赖" in note:
                self.load_denials.append(
                    {"source": "astrbot", "name": path.name, "time": time.time(), "detail": note[:300]}
                )
                self.load_denials[:] = self.load_denials[-32:]
            if res.get("ok"):
                self._sync_maibot_tools()
            return f"[astrbot] {path.name}: {note}"
        if source == "maibot":
            plugin_id = str(out.get("entry", {}).get("id") or path.name)
            try:
                res = await load_maibot(plugin_id, policy)
            except Exception as exc:  # noqa: BLE001
                return f"[maibot] {plugin_id}: 加载异常 {type(exc).__name__}: {exc}"
            note = "已加载" if res.get("ok") else f"未加载: {res.get('load_statuses')}"
            if res.get("ok"):
                summary = self._sync_maibot_tools()
                return f"[maibot] {plugin_id}: {note}（走 {res.get('path')}）；AstrBot 侧 mai_* 工具 {summary.get('total')} 个，本次新增 {summary.get('added')}"
            return f"[maibot] {plugin_id}: {note}"
        if source == "astrmai":
            summary = {}
            if self.native is not None:
                summary = self.native.bootstrap_all(path.parent)
                if summary["tools"]:
                    self.context.add_llm_tools(*self.native.astrbot_function_tools())
            return f"[astrmai] {path.name}: 已重载，工具 {summary.get('tools', 0)} 个，失败 {summary.get('failed') or '无'}"
        return f"未知来源: {source}"

    def _status_text(self, event: AstrMessageEvent) -> str:
        host = self.host
        fed = self.federation or {}
        lines = [
            "astrmai 状态",
            f"引擎: {'运行中' if host else '未启动'}",
            f"启动错误: {self.boot_error or '无'}",
            f"转发入站: {self.forwarded}",
            f"出栈消息: {self.sent_total}",
            f"已记住会话: {len(self._umo_by_target)}",
        ]
        if host:
            lines.append(f"麦麦启动耗时: {host.boot_ms:.0f} ms")
            lines.append(f"驱动: {host.driver.driver_id if host.driver else '无'}")
            lines.append(f"存活子进程: {len(host.live_runners())}")
            lines.append(f"子进程兜底: {host.job.describe() if host.job else '未装'}")
        if self.panel is not None:
            lines.append(f"控制台: http://{self.panel.host}:{self.panel.port}/astrmai/（裁决流水 {len(self.ledger.rows(500))} 条）")
        mai = fed.get("mai_tools") or {}
        lines.append(f"mai_* 工具: {mai.get('total', 0)}（其中插件来源 {mai.get('plugin_sourced', 0)}）")
        lines.append(f"ab_* 工具: {fed.get('ab_tools', 0)}")
        lines.append(f"abcmd_* 命令: {(fed.get('command_tools') or {}).get('count', 0)}")
        native = fed.get("native") or {}
        lines.append(f"nm_* 原生工具: {native.get('tools', 0)}")
        return "\n".join(lines)
