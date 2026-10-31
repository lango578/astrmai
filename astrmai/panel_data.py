"""Panel 的数据层：把"两个脑子在一个进程里"的真实状态抽成可序列化的快照。

与 HTTP 分开写，是为了能不起服务就单测；也逼自己承认哪些字段其实是拿不到的
（拿不到就标 unavailable，绝不猜一个数糊上去）。
"""

import json
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

MAX_LEDGER = 200
MAX_LOGS = 400
ASTRMAI_VERSION = "0.1.0"
_FILE_CACHE: dict[str, tuple[int, Any]] = {}

#: 任何响应里都不得出现的凭据字段名。按整词匹配，绝不按子串 ——
#: 子串匹配曾经把自家布尔字段 credentialed 也 scrub 成了 "<redacted>"，
#: 于是面板上每个"故意没绑定"的模型任务都显示成「密钥已配置」，那是当着人的面说谎。
CREDENTIAL_WORDS = {
    "api_key",
    "apikey",
    "key",
    "secret",
    "token",
    "password",
    "passwd",
    "authorization",
    "bearer",
    "credential",
    "credentials",
}


def is_secret(name: str) -> bool:
    tokens = [t for t in re.split(r"[^0-9a-zA-Z]+", str(name).lower()) if t]
    return any(t in CREDENTIAL_WORDS for t in tokens)


def scrub(value: Any) -> Any:
    """递归剔除凭据字段；只处理字符串/二进制值，布尔与数字原样保留（它们不是凭据）。"""

    if isinstance(value, dict):
        return {
            k: ("<redacted>" if is_secret(k) and isinstance(v, (str, bytes, bytearray, dict, list)) else scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return f"<redacted {len(value)} bytes>"
    if isinstance(value, str):
        if value.startswith("sk-") and len(value) > 8:
            return f"<redacted {len(value)} chars>"
        return value
    return value


def host_of(url: str) -> str:
    try:
        parsed = urlparse(str(url))
        return parsed.netloc or parsed.path or ""
    except ValueError:
        return ""


class Ledger:
    """裁决流水：这条消息归谁、为什么。上游两家面板都没有这一层，而它正是融合产品最该看的。"""

    def __init__(self, maxlen: int = MAX_LEDGER) -> None:
        self._rows: deque[dict] = deque(maxlen=maxlen)

    def record(
        self,
        *,
        chat_type: str,
        target_id: str,
        owner: str,
        reason: str,
        preview: str = "",
        forwarded: bool = False,
        detail: str = "",
    ) -> None:
        self._rows.append(
            {
                "ts": time.time(),
                "chat_type": chat_type,
                "target_id": str(target_id),
                "owner": owner,
                "reason": reason,
                "preview": str(preview)[:60],
                "forwarded": forwarded,
                "detail": detail[:160],
            }
        )

    def rows(self, limit: int = 50) -> list[dict]:
        items = list(self._rows)[-limit:]
        items.reverse()
        return items

    def tally(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self._rows:
            out[row["owner"]] = out.get(row["owner"], 0) + 1
        return out


class LogRing:
    def __init__(self, maxlen: int = MAX_LOGS) -> None:
        self._rows: deque[dict] = deque(maxlen=maxlen)

    def add(self, level: str, name: str, message: str) -> None:
        self._rows.append({"ts": time.time(), "level": level, "name": name, "message": str(message)[:400]})

    def rows(self, limit: int = 120, since: float = 0.0) -> list[dict]:
        items = [r for r in self._rows if r["ts"] >= since][-limit:]
        items.reverse()
        return items

    def size(self) -> int:
        return len(self._rows)


def _maibot_ok() -> bool:
    try:
        import src.config.config  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def _mai_plugin_tools() -> tuple[list[dict], str]:
    """麦麦对插件工具的权威视图；失败时诚实说明为什么没有。"""

    if not _maibot_ok():
        return [], "麦麦未启动（src 不可导入）"
    try:
        from src.plugin_runtime.component_query import component_query_service

        specs = component_query_service.get_llm_available_tool_specs(None) or {}
    except Exception as exc:  # noqa: BLE001
        return [], f"component_query_service 不可用: {type(exc).__name__}"
    return [
        {
            "name": name,
            "provider": getattr(spec, "provider_name", "") or "",
            "description": (getattr(spec, "description", "") or "")[:160],
        }
        for name, spec in specs.items()
    ], ""


def _mai_disk_plugins(mai_root: Path) -> list[dict]:
    out = []
    base = Path(mai_root) / "plugins"
    for manifest in sorted(base.glob("*/_manifest.json")) if base.exists() else []:
        row = {"dir": manifest.parent.name, "id": "", "name": "", "enabled": None, "license": "", "release": ""}
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            row["id"] = str(data.get("id") or "")
            row["name"] = str(data.get("name") or "")
        except (OSError, ValueError) as exc:
            row["error"] = f"manifest 解析失败: {type(exc).__name__}"
        cfg = manifest.parent / "config.toml"
        if cfg.exists():
            try:
                import tomlkit

                doc = tomlkit.parse(cfg.read_text(encoding="utf-8"))
                row["enabled"] = bool((doc.get("plugin") or {}).get("enabled", False))
            except Exception as exc:  # noqa: BLE001
                row["error"] = f"config.toml 解析失败: {type(exc).__name__}"
        release = manifest.parent / ".maibot-release.json"
        if release.exists():
            try:
                rel = json.loads(release.read_text(encoding="utf-8"))
                row["license"] = str(rel.get("license") or "")
                row["release"] = str(rel.get("commit") or "")[:12]
            except (OSError, ValueError):
                pass
        out.append(row)
    return out


def _astrbot_stars(context: Any) -> tuple[list[dict], str]:
    if context is None:
        return [], "AstrBot 上下文不可用"
    try:
        stars = context.get_all_stars() or []
    except Exception as exc:  # noqa: BLE001
        return [], f"get_all_stars 失败: {type(exc).__name__}"
    out = []
    for meta in stars:
        out.append(
            {
                "name": getattr(meta, "name", None) or "",
                "author": getattr(meta, "author", None) or "",
                "version": getattr(meta, "version", None) or "",
                "desc": (getattr(meta, "short_desc", None) or getattr(meta, "desc", None) or "")[:120],
                "repo": getattr(meta, "repo", None) or "",
                "dir": getattr(meta, "root_dir_name", None) or "",
                "reserved": bool(getattr(meta, "reserved", False)),
                "activated": getattr(meta, "activated", None),
            }
        )
    return out, ""


def _astrbot_tools(context: Any) -> tuple[list[dict], str]:
    if context is None:
        return [], "AstrBot 上下文不可用"
    try:
        manager = context.get_llm_tool_manager()
        funcs = list(getattr(manager, "func_list", []) or [])
    except Exception as exc:  # noqa: BLE001
        return [], f"get_llm_tool_manager 失败: {type(exc).__name__}"
    return [
        {
            "name": getattr(f, "name", "") or "",
            "description": (getattr(f, "description", "") or "")[:160],
            "active": bool(getattr(f, "active", True)),
            "module": str(getattr(f, "handler_module_path", "") or ""),
        }
        for f in funcs
    ], ""


def _native_plugins(native_root: Path, registry: Any = None) -> list[dict]:
    """磁盘上的原生插件清单。工具数只能从"已经加载进脑子的那份登记表"拿：
    manifest 里不声明工具，硬按磁盘算会永远报 0，那是假数。"""

    loaded: dict[str, list[str]] = {}
    for full_name, pair in (getattr(registry, "tools", {}) or {}).items():
        plugin = pair[0] if isinstance(pair, (tuple, list)) and pair else None
        loaded.setdefault(str(getattr(plugin, "name", "") or ""), []).append(str(full_name))

    out = []
    base = Path(native_root)
    for toml_path in sorted(base.glob("*/astrmai.toml")) if base.exists() else []:
        row = {"dir": toml_path.parent.name, "name": "", "version": "", "tools": None, "tool_names": [], "license": "", "loaded": False}
        try:
            import tomlkit

            doc = tomlkit.parse(toml_path.read_text(encoding="utf-8"))
            meta = doc.get("plugin") or doc
            row["name"] = str(meta.get("name") or toml_path.parent.name)
            row["version"] = str(meta.get("version") or "")
            row["license"] = str(meta.get("license") or "")
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"astrmai.toml 解析失败: {type(exc).__name__}"
        names = loaded.get(row["name"])
        if names is not None:
            row["tools"] = len(names)
            row["tool_names"] = sorted(names)
            row["loaded"] = True
        out.append(row)
    return out


def _cached_parse(path: Path, kind: str) -> Any:
    """按 mtime 缓存配置文件解析结果。

    面板每 2-3 秒就轮询一次概览，不缓存等于每次轮询都重读并重新解析两份 TOML/JSON；
    而且解析异常也要连着缓存住，否则一个坏文件会被反复重读、把耗时全花在异常上。
    """

    key = f"{path}|{kind}"
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        _FILE_CACHE.pop(key, None)
        return None
    hit = _FILE_CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        if kind == "toml":
            import tomlkit

            value = tomlkit.parse(path.read_text(encoding="utf-8"))
        else:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        value = _ParseError(f"{type(exc).__name__}: {exc}")
    _FILE_CACHE[key] = (mtime, value)
    return value


class _ParseError(Exception):
    pass


def _model_task_bindings(mai_root: Path) -> dict:
    """只报形状：任务→模型→提供商→主机。密钥一律不外传。

    上游结构是 `[[api_providers]]` / `[[models]]` 数组表 + `model_task_config.<任务>.model_list`，
    不是以名字为键的表，按键取会拿到 AoT 再 .get() 就炸。
    """

    path = Path(mai_root) / "config" / "model_config.toml"
    if not path.exists():
        return {"ok": False, "reason": "model_config.toml 不存在"}
    try:
        doc = _cached_parse(path, "toml")
        if isinstance(doc, _ParseError):
            return {"ok": False, "reason": f"model_config 解析失败 {doc}"}
        providers = {str(p.get("name") or ""): p for p in (doc.get("api_providers") or []) if isinstance(p, dict)}
        models = {str(m.get("name") or ""): m for m in (doc.get("models") or []) if isinstance(m, dict)}
        tasks = doc.get("model_task_config") or {}
        rows = {}
        for task, binding in tasks.items():
            names = [str(n) for n in ((binding.get("model_list") or []) if isinstance(binding, dict) else [])]
            if not names:
                rows[str(task)] = {"provider": "", "model": "", "host": "", "credentialed": False, "extra": 0, "unbound": True}
                continue
            model = models.get(names[0]) or {}
            provider = providers.get(str(model.get("api_provider") or "")) or {}
            rows[str(task)] = {
                "provider": str(model.get("api_provider") or ""),
                "model": str(model.get("model_identifier") or names[0]),
                "host": host_of(provider.get("base_url") or ""),
                "credentialed": bool(str(provider.get("api_key") or "")),
                "extra": len(names) - 1,
                "unbound": False,
            }
        return {"ok": True, "tasks": rows, "providers": sorted(providers)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}


def _star_counts(star: Any) -> dict:
    """正在跑的星：总数 / 第三方数 / 名字，首页要能一眼看到。"""

    stars, err = _astrbot_stars(getattr(star, "context", None))
    third = [s for s in stars if not s.get("reserved")]
    return {"total": len(stars), "third_party": len(third), "names": sorted(str(s.get("name") or "") for s in stars)[:20], "error": err}


def _ab_tools_live(star: Any) -> dict:
    """此刻能被麦麦用上的 AstrBot 工具数。boot 时的快照会过时（插件是运行时加载的），面板必须报当下。"""

    try:
        manager = star.context.get_llm_tool_manager()
    except Exception as exc:  # noqa: BLE001
        return {"count": None, "names": [], "error": f"拿不到 AstrBot 工具表: {type(exc).__name__}"}
    try:
        from .federation import collect_astrbot_tools

        names = [t["name"] for t in collect_astrbot_tools(manager)]
    except Exception as exc:  # noqa: BLE001
        return {"count": None, "names": [], "error": f"{type(exc).__name__}: {exc}"}
    return {"count": len(names), "names": sorted(names)[:60], "error": ""}


def _ws_alive(ws: Any) -> bool:
    try:
        return bool(getattr(ws, "_closed", False) is False)
    except Exception:  # noqa: BLE001
        return False


def _platform_connections(star: Any) -> dict:
    """反向 WS 现在到底连没连、连了几个：实机最要紧的一格。

    aiocqhttp 的 CQHttp 在 `_wsr_api_clients`（dict: X-Self-ID → ws）与
    `_wsr_event_clients`（set of ws）里挂着活的反向连接；没有它们，
    NapCat 掉线只表现为"消息不再进来"，面板看不见。其它适配器如实报
    "未知此适配器"，不猜。"""

    result = {"ok": True, "error": "", "platforms": [], "total_connections": 0, "connected": False}
    context = getattr(star, "context", None)
    try:
        insts = context.platform_manager.get_insts()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"拿不到平台实例: {type(exc).__name__}", "platforms": [], "total_connections": 0, "connected": False}
    for inst in insts:
        meta = getattr(inst, "meta", lambda: None)()
        row = {"id": str(getattr(meta, "id", "") or getattr(inst, "inst_name", "") or ""), "type": str(getattr(meta, "name", "") or type(inst).__name__)}
        if row["type"] != "aiocqhttp":
            row["state"] = "未知此适配器"
            result["platforms"].append(row)
            continue
        bot = getattr(inst, "bot", None)
        api_clients = getattr(bot, "_wsr_api_clients", None)
        event_clients = getattr(bot, "_wsr_event_clients", None)
        api_rows = [{"self_id": str(k), "alive": _ws_alive(v)} for k, v in (api_clients or {}).items()] if isinstance(api_clients, dict) else []
        event_rows = [{"alive": _ws_alive(v)} for v in event_clients] if isinstance(event_clients, (set, list)) else []
        n = sum(1 for r in api_rows + event_rows if r["alive"])
        row.update({
            "api_clients": api_rows,
            "event_clients": event_rows,
            "connections": n,
            "state": "已连接" if n else "未连接",
        })
        result["total_connections"] += n
        result["platforms"].append(row)
    result["connected"] = result["total_connections"] > 0
    return result


def overview(star: Any, ledger: Ledger) -> dict:
    host = getattr(star, "host", None)
    cfg = getattr(host, "cfg", None) if host else None
    env = os.environ
    fed = getattr(star, "federation", {}) or {}

    data = {
        "engine": {
            "booted": bool(getattr(star, "booted", False)),
            "boot_error": getattr(star, "boot_error", "") or "",
            "boot_ms": round(getattr(host, "boot_ms", 0.0)) if host else None,
            "forwarded": getattr(star, "forwarded", 0),
            "sent": getattr(star, "sent_total", 0),
            "sessions": len(getattr(star, "_umo_by_target", {}) or {}),
        },
        "arbitration": {
            "decisions": ledger.tally(),
            "recent": ledger.rows(30),
        },
        "process": {
            "runners_live": host.live_runners() if host else [],
            "runner_history": list(getattr(host, "runner_history", []) or [])[-16:],
            "job_guard": host.job.status() if (host and getattr(host, "job", None)) else {"mode": "none"},
            "python": f"{os.name} py{os.sys.version_info.major}.{os.sys.version_info.minor}",
        },
        "federation": {
            "mai_tools": fed.get("mai_tools") or {},
            "ab_tools": fed.get("ab_tools", 0),
            "ab_tools_live": _ab_tools_live(star),
            "command_tools": fed.get("command_tools") or {},
            "native": fed.get("native") or {},
            # 麦麦 planner 默认只摊开 visible 的非内置工具，其余靠她自己的 tool_search 发现
            "mirror_visibility": {
                "ab_": "visible" if env.get("ASTRMAI_AB_TOOLS_VISIBLE", "0") == "1" else "deferred",
                "nm_": "visible" if env.get("ASTRMAI_NATIVE_VISIBLE", "0") == "1" else "deferred",
                "abcmd_": env.get("ASTRMAI_COMMAND_TOOLS", "deferred").strip().lower(),
            },
        },
        "privacy": {
            "telemetry": "放行" if env.get("ASTRMAI_TELEMETRY", "off").lower() in ("on", "1", "true") else "已掐",
            "market_network": (
                "关闭（两条官方目录都不出网）"
                if env.get("ASTRMAI_MARKET_NETWORK", "1").strip().lower() in ("0", "off", "false")
                else "允许（AstrBot 官方目录，以及麦麦那条与遥测同域的 hyybuth.xyz）"
            ),
            "group_enabled": bool(getattr(cfg, "group_enabled", False)) if cfg is not None else None,
            "private_enabled": bool(getattr(cfg, "private_enabled", True)) if cfg is not None else None,
            "allow_private": sorted(str(x) for x in (getattr(cfg, "allowed_users", set()) or set())) if cfg else [],
            "allow_groups": sorted(str(x) for x in (getattr(cfg, "groups", set()) or set())) if cfg else [],
        },
        "connections": _platform_connections(star),
        "versions": _versions(),
        "mai_autonomy": _mai_autonomy_state(),
        "tls": _tls_state(),
        "memory": _memory_counts(),
        "stars": _star_counts(star),
        "upstream_panels": _upstream_panel_state(star),
        "model_bindings": _model_task_bindings(Path(getattr(cfg, "mai_root", "") or mai_default())),
    }
    return scrub(data)


def _versions() -> dict:
    out = {"astrbot": "unknown", "maibot": "unknown", "astrmai": ASTRMAI_VERSION}
    try:
        from astrbot.core.config.default import VERSION

        out["astrbot"] = str(VERSION)
    except Exception:  # noqa: BLE001
        pass
    try:
        from src.config.config import MMC_VERSION

        out["maibot"] = str(MMC_VERSION)
    except Exception:  # noqa: BLE001
        pass
    return out


def mai_default() -> str:
    return str(Path(__file__).resolve().parents[1] / "MaiBot-main")


def _tls_state() -> dict:
    """出站 TLS 上限钉没钉住：模型中转在这台机的 TLS1.3 握手会挂，host.boot() 统一钉 ≤1.2。"""

    try:
        import ssl

        capped = bool(getattr(ssl, "_astrmai_tls_capped", False))
        maximum = getattr(ssl.create_default_context(), "maximum_version", None)
        held = str(getattr(maximum, "name", maximum))
        return {"applied": capped, "maximum": held, "ok": not capped or held == "TLSv1_2"}
    except Exception as exc:  # noqa: BLE001
        return {"applied": False, "maximum": None, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _abcmd_listing(star: Any) -> dict:
    """命令镜像逐条清单：叫什么、来自哪个插件、现在是否 deferred。"""

    try:
        manager = star.context.get_llm_tool_manager()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"拿不到 AstrBot 工具表: {type(exc).__name__}", "items": []}
    items = []
    try:
        for tool in manager.func_list:
            name = str(getattr(tool, "name", "") or "")
            if not name.startswith("abcmd_"):
                continue
            items.append(
                {
                    "tool": name,
                    "module": str(getattr(tool, "handler_module_path", "") or ""),
                    "desc": str(getattr(tool, "description", "") or ""),
                    "active": bool(getattr(tool, "active", False)),
                }
            )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "items": []}
    return {"ok": True, "reason": "", "visibility": os.environ.get("ASTRMAI_COMMAND_TOOLS", "deferred"), "items": sorted(items, key=lambda i: i["tool"])}


def _memory_counts() -> dict:
    """麦麦库的规模（只读、只数行数）。读不到就写读不到，不猜 0。"""

    out: dict[str, Any] = {"ok": False, "reason": "", "db": None, "counts": {}}
    db = None
    try:
        from src.config.config import global_config

        root = Path(getattr(global_config, "root_dir", "") or "")
        if not str(root):
            root = Path.cwd()
        candidates = [root / "data" / "MaiBot.db", Path(mai_default()) / "data" / "MaiBot.db"]
    except Exception:  # noqa: BLE001 - 麦麦没起时退到工作区路径
        candidates = [Path(mai_default()) / "data" / "MaiBot.db"]
    db = next((c for c in candidates if c.exists()), None)
    if db is None:
        out["reason"] = "找不到麦麦的 MaiBot.db，规模未知"
        return out
    out["db"] = str(db)
    # 只挑"记忆/人格/会话"这几张有意义的表数行数，别把整库 40 张表都报上去
    wanted = ("personas", "persona_folders", "person_info", "chat_sessions", "mai_messages", "expressions", "tool_records")
    try:
        import sqlite3

        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for name in wanted:
                if name in tables:
                    out["counts"][name] = int(conn.execute(f"SELECT COUNT(*) FROM \"{name}\"").fetchone()[0])
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"{type(exc).__name__}: {exc}"
    return out


def tools_page(star: Any) -> dict:
    context = None
    try:
        context = star.context
    except Exception:  # noqa: BLE001
        context = None

    ab_tools, ab_err = _astrbot_tools(context)
    mai_specs, mai_err = _mai_plugin_tools()
    builtin: list[dict] = []
    try:
        from astrmai.federation import list_maibot_tools

        builtin = list_maibot_tools() or []
    except Exception as exc:  # noqa: BLE001
        mai_err = mai_err or f"list_maibot_tools 失败: {type(exc).__name__}"

    prefixed = {"mai_": 0, "ab_": 0, "abcmd_": 0, "nm_": 0, "其它": 0}
    for tool in ab_tools:
        name = tool["name"]
        for key in ("abcmd_", "mai_", "ab_", "nm_"):
            if name.startswith(key):
                prefixed[key] += 1
                break
        else:
            prefixed["其它"] += 1

    return scrub(
        {
            "astrbot": {"total": len(ab_tools), "items": ab_tools, "error": ab_err, "by_prefix": prefixed},
            "maibot": {"builtin_count": len(builtin), "plugin_specs": mai_specs, "error": mai_err},
            "command_mirror": _abcmd_listing(star),
        }
    )


async def tool_call(star: Any, body: dict) -> dict:
    """执行一个 AstrBot 侧的工具，给外部调用方（麦麦那侧的桥插件）用。

    必须带 target：AstrBot 的处理器是靠**会话事件**找回平台与上下文的。没有这个会话的近期事件
    就直接拒绝，而不是随便抓一个别人的事件凑数 —— 那是跨会话串权限。
    """

    from .federation import call_astrbot_tool

    name = str(body.get("name") or "").strip()
    if not name:
        return {"ok": False, "reason": "缺 name"}
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    target = str(body.get("target") or "").strip()
    if not target:
        return {"ok": False, "reason": "缺 target：这次执行挂在哪个会话上（工具处理器要靠它找回平台连接）"}
    events = getattr(star, "_event_by_target", {}) or {}
    event = next((ev for (_kind, tid), ev in events.items() if str(tid) == target), None)
    if event is None:
        return {"ok": False, "reason": f"会话 {target} 最近没有消息进来，astrmai 手里没有可复用的事件上下文，不能凭空执行"}
    ctx = _ctx(star)
    manager = None
    if ctx is not None:
        try:
            manager = ctx.get_llm_tool_manager()
        except Exception:  # noqa: BLE001 - 取不到管理器就是没在跑
            manager = None
    if manager is None:
        return {"ok": False, "reason": "AstrBot 工具管理器没在跑"}
    try:
        text = await call_astrbot_tool(manager, name, args, event)
    except Exception as exc:  # noqa: BLE001 - 远端调用方要的是结论，不是堆栈
        return {"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return scrub({"ok": True, "name": name, "target": target, "result": str(text)[:4000]})


def _license_of(target: Path) -> dict:
    """插件目录的许可证结论（安装时记录，没记录就现读）。"""

    try:
        from . import licensing

        info = licensing.read_record(Path(target))
        return {"license": str(info.get("license") or "unknown"), "basis": str(info.get("basis") or ""), "confidence": str(info.get("confidence") or "unknown")}
    except Exception as exc:  # noqa: BLE001
        return {"license": "unknown", "basis": f"读不出来: {type(exc).__name__}", "confidence": "unknown"}


def plugins_page(star: Any) -> dict:
    host = getattr(star, "host", None)
    mai_root = Path(getattr(getattr(host, "cfg", None), "mai_root", "") or mai_default())
    native_root = Path(os.environ.get("ASTRMAI_NATIVE_PLUGINS", str(mai_root.parent / "astrmai_plugins")))
    context = getattr(star, "context", None)

    stars, ab_err = _astrbot_stars(context)
    pm = plugin_manager(star)
    failed = dict(getattr(pm, "failed_plugin_dict", {}) or {}) if pm is not None else {}

    store = None
    for attr in ("plugin_store_path", "plugin_dir", "plugins_path"):
        value = getattr(pm, attr, None) if pm is not None else None
        if value:
            store = Path(value)
            break
    for row in stars:
        if row.get("reserved"):
            row.update({"license": "AGPL-3.0-or-later", "license_basis": "内置星，随 AstrBot 本体分发", "license_confidence": "high"})
        elif store is not None and row.get("dir"):
            info = _license_of(store / str(row["dir"]))
            row.update({"license": info["license"], "license_basis": info["basis"], "license_confidence": info["confidence"]})
        else:
            row.update({"license": "unknown", "license_basis": "拿不到插件目录，无法判定", "license_confidence": "unknown"})

    native_rows = _native_plugins(native_root, getattr(star, "native", None))
    for row in native_rows:
        if not str(row.get("license") or "").strip():
            info = _license_of(Path(native_root) / str(row.get("dir") or ""))
            row["license"] = info["license"]
            row["license_basis"] = info["basis"]

    return scrub(
        {
            "astrbot": {"items": stars, "error": ab_err, "failed": {k: str(v)[:160] for k, v in failed.items()}},
            "maibot": {"items": _mai_disk_plugins(mai_root), "runner_pids": host.live_runners() if host else []},
            "astrmai": {"items": native_rows},
            "manager_attached": pm is not None,
            # 运行中加载被拒的记录：默认不碰 pip，所以"要装包的插件"会在这里留档
            "load_denials": list(getattr(star, "load_denials", []) or [])[-32:],
        }
    )


def plugin_manager(star: Any) -> Any:
    """AstrBot 的 PluginManager 只在自己的 Context 上留了一个后门（star_manager.py:200 赋值）。"""

    context = getattr(star, "context", None)
    return getattr(context, "_star_manager", None) if context is not None else None


def sessions_page(star: Any) -> dict:
    rows = []
    for (chat_type, target_id), umo in (getattr(star, "_umo_by_target", {}) or {}).items():
        event = (getattr(star, "_event_by_target", {}) or {}).get((chat_type, target_id))
        preview = ""
        if event is not None:
            try:
                preview = (event.get_message_str() or "")[:60]
            except Exception:  # noqa: BLE001
                preview = ""
        rows.append({"chat_type": chat_type, "target_id": str(target_id), "umo": umo, "last_preview": preview})
    rows.sort(key=lambda r: (r["chat_type"], r["target_id"]))
    return scrub({"total": len(rows), "items": rows})


# ---------------- 设置 ----------------

#: 改了就立刻影响裁决的字段；其余（遥测等）只在下次启动生效，必须说清楚，不能假装生效了
LIVE_FIELDS = ("group_enabled", "private_enabled", "allowed_users", "groups", "command_prefixes")
#: 麦麦自主性：她每轮调度都现读 global_config（scheduler.py/_get_effective_reply_frequency），当场改当场生效
MAI_LIVE_FIELDS = ("mai_talk_value", "mai_private_reply_paused")
RESTART_FIELDS = ("telemetry",)


def _mai_reply_timing() -> Any:
    try:
        from src.config.config import global_config

        return global_config.chat.reply_timing
    except Exception:  # noqa: BLE001 - 麦麦没起来时没有这个配置对象
        return None


def _mai_autonomy_state() -> dict:
    rt = _mai_reply_timing()
    if rt is None:
        return {"ok": False, "reason": "麦麦还没启动，读不到她的发言配置", "talk_value": None, "private_paused": None}
    try:
        return {
            "ok": True,
            "reason": "",
            "talk_value": float(rt.talk_value),
            "private_paused": bool(rt.private_reply_paused),
            # 她还有自己的自主闲聊（proactive 内部循环），这两项管的是"单条消息谁答 + 频率"
            "note": "群聊 talk_value=0 时麦麦只记不发；私聊 paused 时只收不答。她自己的主动发言循环另算，这里管不住",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "talk_value": None, "private_paused": None}


def workspace_root() -> Path:
    return Path(__file__).resolve().parents[1]


def settings_path() -> Path:
    from .credentials import state_dir  # 同一套判据，别在两处各猜一次插件目录布局

    return state_dir() / "settings.json"


def load_settings_file() -> dict:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings_file(values: dict) -> Path:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {**load_settings_file(), **values}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def apply_stored_to_cfg(cfg: Any) -> list[str]:
    """启动时把面板上次改过的闸门读回来；被环境变量显式钉住的字段让给环境变量。"""

    stored = load_settings_file()
    applied = []
    for key, raw in stored.items():
        if key not in LIVE_FIELDS or _env_pins(key):
            continue
        value = _coerce(key, raw)
        if key in ("allowed_users", "groups"):
            setattr(cfg, key, set(value))
        elif key == "command_prefixes":
            setattr(cfg, key, tuple(value))
        else:
            setattr(cfg, key, value)
        applied.append(key)
    return applied


def apply_stored_mai_settings() -> list[str]:
    """麦麦起来后把面板存过的自主性设置读回 global_config（她每轮现读，不用等重启）。

    返回真正落地的字段名，让宿主日志说实话。"""

    stored = load_settings_file()
    applied = []
    rt = _mai_reply_timing()
    if rt is None:
        return applied
    if "mai_talk_value" in stored:
        rt.talk_value = max(0.0, min(1.0, _coerce("mai_talk_value", stored["mai_talk_value"])))
        applied.append("mai_talk_value")
    if "mai_private_reply_paused" in stored:
        rt.private_reply_paused = _coerce("mai_private_reply_paused", stored["mai_private_reply_paused"])
        applied.append("mai_private_reply_paused")
    return applied


def settings_page(star: Any) -> dict:
    host = getattr(star, "host", None)
    cfg = getattr(host, "cfg", None)
    env = os.environ
    stored = load_settings_file()
    rows = []
    if cfg is not None:
        rows.append({"field": "group_enabled", "label": "允许群聊", "kind": "bool",
                     "value": bool(cfg.group_enabled),
                     "note": "关闭时群消息既不转发也不抑制（陌生人问也不答）"})
        rows.append({"field": "private_enabled", "label": "允许私聊", "kind": "bool", "value": bool(cfg.private_enabled)})
        rows.append({"field": "allowed_users", "label": "私聊硬白名单", "kind": "list",
                     "value": sorted(str(x) for x in (cfg.allowed_users or set())),
                     "note": "空=不限制（谨慎）；非空=名单外一律沉默"})
        rows.append({"field": "groups", "label": "群号白名单", "kind": "list",
                     "value": sorted(str(x) for x in (cfg.groups or set()))})
        rows.append({"field": "command_prefixes", "label": "命令前缀（归 AstrBot）", "kind": "list",
                     "value": list(cfg.command_prefixes)})
    mai = _mai_autonomy_state()
    if mai["ok"]:
        rows.append({"field": "mai_talk_value", "label": "麦麦群聊发言频率 talk_value", "kind": "num",
                     "value": mai["talk_value"], "min": 0, "max": 1, "step": 0.05,
                     "note": "0=群聊只记不说（最安静）；越大越爱插话。当场生效"})
        rows.append({"field": "mai_private_reply_paused", "label": "麦麦私聊暂停思考", "kind": "bool",
                     "value": mai["private_paused"],
                     "note": "开启后私聊只收消息维护记忆，不进思考不回答。当场生效"})
    return scrub(
        {
            # 这一行的标识字段必须叫 field：叫 key 会被下面的 scrub() 按凭据词表抹掉，
            # 前端就会把五个输入框塌成一个 name、保存时发出 {"<redacted>": ...}（我就是这样把设置页测坏的）
            "fields": rows,
            "env_overridden": sorted(k for k in stored if _env_pins(k)),
            "runtime_env": {
                "telemetry": env.get("ASTRMAI_TELEMETRY", "off"),
                "market_network": env.get("ASTRMAI_MARKET_NETWORK", "1"),
                "panel_enable": env.get("ASTRMAI_PANEL", "off"),
                "panel_host": env.get("ASTRMAI_PANEL_HOST", "127.0.0.1"),
                "panel_port": env.get("ASTRMAI_PANEL_PORT", "18124"),
            },
            "stored_keys": sorted(stored),
            "upstream_panels": _upstream_panel_state(star),
        }
    )


def _env_pins(key: str) -> bool:
    return {
        "group_enabled": "ASTRMAI_ALLOW_GROUPS",
        "private_enabled": "ASTRMAI_ALLOW_PRIVATE",
        "allowed_users": "ASTRMAI_ALLOWED_USERS",
        "groups": "ASTRMAI_GROUPS",
        "telemetry": "ASTRMAI_TELEMETRY",
    }.get(key, "") in os.environ


def _coerce(key: str, value: Any) -> Any:
    if key in ("group_enabled", "private_enabled"):
        return bool(value) if not isinstance(value, str) else value.strip().lower() in ("1", "true", "on", "yes")
    if key in ("allowed_users", "groups"):
        if isinstance(value, str):
            value = [s for s in value.replace("，", ",").split(",")]
        return [str(s).strip() for s in value if str(s).strip()]
    if key == "command_prefixes":
        if isinstance(value, str):
            value = value.replace("，", ",").split(",")
        return tuple(str(s).strip() for s in value if str(s).strip())
    if key == "mai_talk_value":
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    if key == "mai_private_reply_paused":
        return bool(value) if not isinstance(value, str) else value.strip().lower() in ("1", "true", "on", "yes")
    if key == "telemetry":
        return "on" if str(value).strip().lower() in ("1", "true", "on", "yes") else "off"
    return value


def apply_settings(star: Any, patch: dict) -> dict:
    """能当场生效的当场生效，只能下次生效的明说 —— 闸门不能骗人。"""

    host = getattr(star, "host", None)
    cfg = getattr(host, "cfg", None)
    applied: list[str] = []
    deferred: list[str] = []
    refused: dict[str, str] = {}

    for key, raw in (patch or {}).items():
        if key not in LIVE_FIELDS and key not in RESTART_FIELDS and key not in MAI_LIVE_FIELDS:
            refused[key] = "面板不开放这个字段（两家引擎自己的配置请去各自配置文件改）"
            continue
        value = _coerce(key, raw)
        if key in MAI_LIVE_FIELDS:
            rt = _mai_reply_timing()
            if rt is None:
                refused[key] = "麦麦还没启动，改不了她的发言配置"
                continue
            if key == "mai_talk_value":
                rt.talk_value = max(0.0, min(1.0, float(value)))
            else:
                rt.private_reply_paused = bool(value)
            applied.append(key)
            continue
        if key in RESTART_FIELDS:
            os.environ["ASTRMAI_TELEMETRY"] = str(value)
            deferred.append("telemetry：本次运行的遥测地址已定，改值只在下次启动生效")
            continue
        if cfg is None:
            refused[key] = "麦麦宿主还没起来，改不了运行中的闸门"
            continue
        if key in ("allowed_users", "groups"):
            setattr(cfg, key, set(value))
        elif key == "command_prefixes":
            setattr(cfg, key, tuple(value))
        else:
            setattr(cfg, key, value)
        applied.append(key)

    stored: dict[str, Any] = {}
    for key in (patch or {}):
        if key in LIVE_FIELDS or key in RESTART_FIELDS or key in MAI_LIVE_FIELDS:
            value = _coerce(key, patch[key])
            stored[key] = list(value) if isinstance(value, (list, tuple, set)) else value
    path = save_settings_file(stored) if stored else None
    return scrub(
        {"ok": not refused, "applied_live": applied, "needs_restart": deferred, "refused": refused, "stored_at": str(path or "")}
    )


def _upstream_panel_state(star: Any) -> dict:
    """两家自带面板都该是关着的；这里把事实报出来，而不是让他去猜端口。"""

    out = {"astrbot_dashboard": "未知", "maibot_webui": "未知"}
    env = os.environ
    try:
        from pathlib import Path as _P

        root = _P(env.get("ASTRBOT_ROOT", workspace_root() / "vendor" / "AstrBot-master"))
        cfg_path = root / "data" / "cmd_config.json"
        if cfg_path.exists():
            data = _cached_parse(cfg_path, "json")  # AstrBot 回写这个文件时带 UTF-8 BOM
            if isinstance(data, _ParseError):
                out["astrbot_dashboard"] = f"解析失败: {data}"
            elif isinstance(data, dict):
                out["astrbot_dashboard"] = "关闭" if not (data.get("dashboard") or {}).get("enable", True) else "开启（不该）"
    except Exception as exc:  # noqa: BLE001
        out["astrbot_dashboard"] = f"读不到: {type(exc).__name__}"
    try:
        from src.config.config import global_config

        out["maibot_webui"] = "关闭" if not global_config.webui.enabled else "开启（不该）"
    except Exception:  # noqa: BLE001
        pass
    return out


# ---------------- AstrBot 侧功能面（provider / 人格 / 对话 / cron） ----------------
#
# 这些是 AstrBot 官方面板的主体功能，astrmai 面板必须能做大部分（覆盖矩阵见
# docs/COVERAGE.md）。全部走 context 上挂着的真管理器（provider_manager /
# persona_manager / conversation_manager / message_history_manager / cron_manager），
# 面板自己不碰它们的存储 —— 官方面板怎么改，这里就怎么改，读写同一个库、同一份配置。


def _ctx(star: Any) -> Any:
    return getattr(star, "context", None)


def _providers_manager(star: Any) -> Any:
    """Context 上就挂着 provider_manager（star/context.py:164），官方面板用的同一条路。"""

    ctx = _ctx(star)
    return getattr(ctx, "provider_manager", None) if ctx is not None else None


def providers_page(star: Any) -> dict:
    """Provider 清单：配置 + 运行状态合并。凭据只报有没有（key_configured），值绝不外传。

    cmd_config.json 的 provider 条目里键是 "key"（str 或 list）——scrub() 会把值
    整个换成 "<redacted>"，但列表长度（多 key 轮换）对运维有用，所以这里先摘出来
    折成布尔+数量，再交 scrub()。"""

    pm = _providers_manager(star)
    if pm is None:
        return {"ok": False, "reason": "AstrBot 核心没在跑，拿不到 ProviderManager", "items": [], "manager_attached": False}

    rows = []
    try:
        configs = list(getattr(pm, "providers_config", []) or [])
        inst_map = getattr(pm, "inst_map", {}) or {}
        agent_runner = (getattr(pm, "acm", None) and getattr(pm.acm, "default_conf", {}).get("agent_runner", {})) or {}
        default_id = (agent_runner.get("config", {}) or {}).get("model", {}).get("provider_id", "")
        for cfg in configs:
            if not isinstance(cfg, dict):
                continue
            if cfg.get("provider_type") == "agent_runner":
                continue
            keys = cfg.get("key")
            n_keys = len(keys) if isinstance(keys, (list, tuple)) else (1 if keys else 0)
            pid = str(cfg.get("id") or "")
            inst = inst_map.get(pid)
            meta = None
            if inst is not None:
                try:
                    meta = inst.meta()
                except Exception:  # noqa: BLE001 - 坏 provider 不能拖垮整页
                    meta = None
            rows.append({
                "id": pid,
                "type": str(cfg.get("type") or ""),
                "provider_type": str(cfg.get("provider_type") or "chat_completion"),
                "enable": bool(cfg.get("enable", False)),
                "model": str(cfg.get("model") or (getattr(meta, "model", "") or "")),
                "api_base": host_of(str(cfg.get("api_base") or "")),
                "key_configured": n_keys > 0,
                "key_count": n_keys,
                "loaded": inst is not None,
                "is_default": pid == default_id,
            })
        rows.sort(key=lambda r: (not r["is_default"], not r["enable"], r["id"]))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "items": [], "manager_attached": True}

    # 模板下拉：官方面板有几十个预置模板，这里给最小的一组（覆盖矩阵口径：建常用的，不搬全部）
    templates = []
    try:
        from astrbot.core.provider.register import provider_registry

        for meta_data in provider_registry:
            tmpl = getattr(meta_data, "default_config_tmpl", None)
            if isinstance(tmpl, dict) and tmpl.get("provider_type") in (None, "chat_completion"):
                templates.append({"type": str(tmpl.get("type") or ""), "name": str(getattr(meta_data, "desc", "") or "")})
    except Exception:  # noqa: BLE001 - vendor 结构变了就只给空模板，不挡页面
        templates = []
    return scrub({"ok": True, "reason": "", "items": rows, "manager_attached": True, "default_provider_id": default_id, "templates": templates})


async def provider_apply(star: Any, action: str, body: dict) -> dict:
    """写操作全部转给真 ProviderManager —— 官方面板同一条路，改完存 cmd_config.json 并热加载。

    api_key 只进不出：POST/PUT 收下写进配置，任何响应里都只回形状。"""

    pm = _providers_manager(star)
    if pm is None:
        return {"ok": False, "reason": "AstrBot 核心没在跑，拿不到 ProviderManager"}

    async def _save_default(pid: str) -> None:
        conf = getattr(pm, "acm", None)
        if conf is None:
            return
        default_conf = getattr(conf, "default_conf", None)
        if default_conf is None:
            return
        runner = default_conf.get("agent_runner", {})
        runner.setdefault("config", {}).setdefault("model", {})["provider_id"] = pid
        if "config" in default_conf:
            default_conf.save_config()

    try:
        if action == "create":
            config = dict(body.get("config") or {})
            pid = str(config.get("id") or "").strip()
            if not pid:
                return {"ok": False, "reason": "config.id 不能为空"}
            config.setdefault("type", "openai_chat_completion")
            config.setdefault("provider_type", "chat_completion")
            config["enable"] = bool(body.get("enable", config.get("enable", True)))
            await pm.create_provider(config)
            if body.get("set_default"):
                await _save_default(pid)
            return scrub({"ok": True, "reason": f"已创建并加载 {pid}", "id": pid})
        if action == "update":
            origin = str(body.get("id") or "").strip()
            config = dict(body.get("config") or {})
            if not origin:
                return {"ok": False, "reason": "id 不能为空"}
            config["id"] = str(config.get("id") or origin)
            # 凭据留空 = 不改密钥：官方面板编辑表单不回显密钥，空值会把 key 抹掉，这是最坑的一步
            keys = config.get("key")
            if keys is None or keys == [] or keys == "":
                old = pm.get_provider_config_by_id(origin) or {}
                config["key"] = old.get("key", [])
            await pm.update_provider(origin, config)
            return scrub({"ok": True, "reason": f"已更新并热加载 {config['id']}", "id": str(config["id"])})
        if action == "delete":
            pid = str(body.get("id") or "").strip()
            if not pid:
                return {"ok": False, "reason": "id 不能为空"}
            await pm.delete_provider(provider_id=pid)
            return scrub({"ok": True, "reason": f"已删除 {pid}", "id": pid})
        if action == "enable":
            pid = str(body.get("id") or "").strip()
            enabled = bool(body.get("enabled", True))
            config = pm.get_provider_config_by_id(pid)
            if config is None:
                return {"ok": False, "reason": f"找不到 provider {pid}"}
            config["enable"] = enabled
            await pm.update_provider(pid, config)
            return scrub({"ok": True, "reason": f"{pid} 已{'启用' if enabled else '停用'}", "id": pid})
        if action == "default":
            pid = str(body.get("id") or "").strip()
            if not pid or pid not in (getattr(pm, "inst_map", {}) or {}):
                return {"ok": False, "reason": f"{pid} 没加载起来，不能设为默认（先启用并测试通过）"}
            await _save_default(pid)
            return scrub({"ok": True, "reason": f"默认 provider 已设为 {pid}", "id": pid})
        if action == "test":
            pid = str(body.get("id") or "").strip()
            target = (getattr(pm, "inst_map", {}) or {}).get(pid)
            if target is None:
                return {"ok": False, "reason": f"{pid} 没有运行中的实例（先启用）"}
            meta = target.meta()
            out = {"ok": True, "id": pid, "model": getattr(meta, "model", None), "type": str(getattr(getattr(meta, "provider_type", None), "value", "") or ""), "status": "unavailable", "error": ""}
            try:
                await target.test()
                out["status"] = "available"
            except Exception as exc:  # noqa: BLE001 - 测试失败本身就是返回值
                out["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            return scrub(out)
        return {"ok": False, "reason": f"未知动作 {action}"}
    except Exception as exc:  # noqa: BLE001
        return scrub({"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"})


def _personas_manager(star: Any) -> Any:
    ctx = _ctx(star)
    return getattr(ctx, "persona_manager", None) if ctx is not None else None


async def personas_page(star: Any) -> dict:
    """人格清单（AstrBot 官方 PersonaManager，读写 data_v4.db 的 personas 表）。"""

    mgr = _personas_manager(star)
    if mgr is None:
        return {"ok": False, "reason": "AstrBot 核心没在跑，拿不到 PersonaManager", "items": [], "default_persona": ""}
    try:
        personas = await mgr.get_all_personas()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "items": [], "default_persona": ""}
    rows = []
    for p in personas or []:
        dialogs = getattr(p, "begin_dialogs", None) or []
        rows.append({
            "persona_id": str(getattr(p, "persona_id", "") or ""),
            "system_prompt": str(getattr(p, "system_prompt", "") or ""),
            "begin_dialogs": [str(d) for d in dialogs][:8],
            "dialog_count": len(dialogs),
            "custom_error_message": str(getattr(p, "custom_error_message", "") or ""),
            "is_default": str(getattr(p, "persona_id", "")) == "default",
        })
    rows.sort(key=lambda r: (not r["is_default"], r["persona_id"]))
    return scrub({"ok": True, "reason": "", "items": rows, "default_persona": str(getattr(mgr, "default_persona", "") or "")})


async def persona_apply(star: Any, action: str, body: dict) -> dict:
    mgr = _personas_manager(star)
    if mgr is None:
        return {"ok": False, "reason": "AstrBot 核心没在跑，拿不到 PersonaManager"}
    pid = str(body.get("persona_id") or body.get("id") or "").strip()
    try:
        if action == "create":
            if not pid:
                return {"ok": False, "reason": "persona_id 不能为空"}
            prompt = str(body.get("system_prompt") or "").strip()
            if not prompt:
                return {"ok": False, "reason": "system_prompt 不能为空"}
            dialogs = [str(x) for x in (body.get("begin_dialogs") or []) if str(x).strip()]
            await mgr.create_persona(pid, prompt, begin_dialogs=dialogs or None)
            return scrub({"ok": True, "reason": f"已创建人格 {pid}"})
        if action == "update":
            if not pid:
                return {"ok": False, "reason": "persona_id 不能为空"}
            kwargs: dict[str, Any] = {}
            if "system_prompt" in body:
                prompt = str(body.get("system_prompt") or "").strip()
                if not prompt:
                    return {"ok": False, "reason": "system_prompt 不能改成空的"}
                kwargs["system_prompt"] = prompt
            if "begin_dialogs" in body:
                kwargs["begin_dialogs"] = [str(x) for x in (body.get("begin_dialogs") or []) if str(x).strip()] or None
            await mgr.update_persona(pid, **kwargs)
            return scrub({"ok": True, "reason": f"已更新人格 {pid}"})
        if action == "delete":
            if not pid:
                return {"ok": False, "reason": "persona_id 不能为空"}
            await mgr.delete_persona(pid)
            return scrub({"ok": True, "reason": f"已删除人格 {pid}"})
        return {"ok": False, "reason": f"未知动作 {action}"}
    except Exception as exc:  # noqa: BLE001
        return scrub({"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"})


async def conversations_page(star: Any, page: int = 1, limit: int = 20) -> dict:
    """对话历史（conversations + platform_message_history）：只读浏览 + 每条 token 数。

    官方面板的 ConversationPage 同款数据，但这里不做新建/切换会话 —— 那是消息流程
    自己的事，面板只看发生过什么。"""

    ctx = _ctx(star)
    mgr = getattr(ctx, "conversation_manager", None) if ctx is not None else None
    if mgr is None:
        return {"ok": False, "reason": "AstrBot 核心没在跑，拿不到 ConversationManager", "items": [], "total": 0, "page": page}
    try:
        rows = []
        for conv in await mgr.get_conversations():
            history = []
            try:
                history = json.loads(getattr(conv, "history", "[]") or "[]")
            except (ValueError, TypeError):
                history = []
            messages = history if isinstance(history, list) else []
            rows.append({
                "cid": str(getattr(conv, "cid", "") or ""),
                "platform_id": str(getattr(conv, "platform_id", "") or ""),
                "user_id": str(getattr(conv, "user_id", "") or ""),
                "title": str(getattr(conv, "title", "") or ""),
                "persona_id": str(getattr(conv, "persona_id", "") or ""),
                "token_usage": int(getattr(conv, "token_usage", 0) or 0),
                "message_count": len(messages),
                "updated_at": int(getattr(conv, "updated_at", 0) or 0),
            })
        rows.sort(key=lambda r: -(r["updated_at"] or 0))
        total = len(rows)
        start = max(0, (page - 1) * limit)
        return scrub({"ok": True, "reason": "", "items": rows[start : start + limit], "total": total, "page": page, "limit": limit})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "items": [], "total": 0, "page": page}


async def conversation_history(star: Any, cid: str, limit: int = 40) -> dict:
    """单个对话的消息正文（OpenAI 格式链），只读。"""

    ctx = _ctx(star)
    mgr = getattr(ctx, "conversation_manager", None) if ctx is not None else None
    if mgr is None:
        return {"ok": False, "reason": "AstrBot 核心没在跑", "messages": []}
    try:
        conv = await mgr.get_conversation(cid)
    except Exception as exc:  # noqa: BLE001
        return scrub({"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:160]}", "messages": []})
    if conv is None:
        return {"ok": False, "reason": f"对话 {cid} 不存在", "messages": []}
    try:
        history = json.loads(getattr(conv, "history", "[]") or "[]")
    except (ValueError, TypeError):
        history = []
    messages = []
    for m in (history if isinstance(history, list) else [])[-limit:]:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)[:400] if content is not None else ""
        messages.append({"role": str(m.get("role") or ""), "content": str(text)[:600]})
    return scrub({"ok": True, "reason": "", "cid": cid, "persona_id": str(getattr(conv, "persona_id", "") or ""), "messages": messages})


def _cron_manager(star: Any) -> Any:
    ctx = _ctx(star)
    return getattr(ctx, "cron_manager", None) if ctx is not None else None


async def cron_page(star: Any) -> dict:
    mgr = _cron_manager(star)
    if mgr is None:
        return {"ok": False, "reason": "AstrBot 核心没在跑，拿不到 CronJobManager", "items": []}
    try:
        jobs = await mgr.list_jobs()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "items": []}
    rows = []
    for job in jobs or []:
        next_run = ""
        try:
            nxt = mgr.get_next_run_time(str(getattr(job, "job_id", "")))
            next_run = nxt.isoformat(sep=" ", timespec="seconds") if nxt else ""
        except Exception:  # noqa: BLE001
            next_run = ""
        last = getattr(job, "last_run_at", None)
        rows.append({
            "job_id": str(getattr(job, "job_id", "") or "")[:12],
            "name": str(getattr(job, "name", "") or ""),
            "type": str(getattr(job, "job_type", "") or ""),
            "cron": str(getattr(job, "cron_expression", "") or ""),
            "enabled": bool(getattr(job, "enabled", False)),
            "next_run": next_run,
            "last_run": last.isoformat(sep=" ", timespec="seconds") if last else "",
            "last_error": str(getattr(job, "last_error", "") or "")[:200],
        })
    return scrub({"ok": True, "reason": "", "items": rows})


async def cron_apply(star: Any, action: str, body: dict) -> dict:
    mgr = _cron_manager(star)
    if mgr is None:
        return {"ok": False, "reason": "AstrBot 核心没在跑，拿不到 CronJobManager"}
    job_id = str(body.get("job_id") or "").strip()
    try:
        if action == "toggle":
            if not job_id:
                return {"ok": False, "reason": "job_id 不能为空"}
            enabled = bool(body.get("enabled", True))
            await mgr.update_job(job_id, enabled=enabled)
            return scrub({"ok": True, "reason": f"任务已{'启用' if enabled else '停用'}"})
        if action == "delete":
            if not job_id:
                return {"ok": False, "reason": "job_id 不能为空"}
            await mgr.delete_job(job_id)
            return scrub({"ok": True, "reason": "已删除任务"})
        if action == "run_now":
            if not job_id:
                return {"ok": False, "reason": "job_id 不能为空"}
            await mgr.run_job_now(job_id)
            return scrub({"ok": True, "reason": "已触发一次立即执行（结果看 last_error 与日志）"})
        return {"ok": False, "reason": f"未知动作 {action}"}
    except Exception as exc:  # noqa: BLE001
        return scrub({"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"})


def about_page() -> dict:
    return scrub(
        {
            "product": "astrmai",
            "version": ASTRMAI_VERSION,
            "license": "AGPL-3.0-or-later",
            "upstream": [
                {"name": "AstrBot", "license": "AGPL-3.0-or-later"},
                {"name": "MaiBot (麦麦/MaiCore)", "license": "GPL-3.0-or-later"},
            ],
            "conveyance": (
                "合并作品按 AGPL-3.0-or-later 整体分发：分发时必须提供完整对应源码（含胶水代码与随包插件源码），"
                "保留 LICENSE 与声明，不得追加额外限制。AGPL §13：只要程序通过网络与用户交互，就要给那些用户提供源码；"
                "只自己私聊用不触发发放义务。"
            ),
            "provenance": (
                "本界面为 astrmai 原创：零框架、手写 HTML/CSS/JS，不复用两个上游引擎自带面板的任何"
                "代码、组件、字体、图标或品牌素材，也不模仿它们的版式与配色。"
            ),
            "privacy": [
                "遥测默认掐断（上游硬编码往第三方站点发心跳）",
                "市场查询与遥测同域 hyybuth.xyz，可用 ASTRMAI_MARKET_NETWORK=0 彻底关闭",
                "面板任何响应都不输出密钥值，只输出是否已配置",
                "面板默认只监听 127.0.0.1；改绑非回环必须带 token",
            ],
            "secrets_policy": "密钥只以形状呈现（是否已配置 / 长度），连前缀都不给",
        }
    )
