"""astrmai 自己的网页面板：一个进程里两个脑子的控制台。

刻意与两家上游的面板不同：不引它们的组件、不抄它们的版式与配色，路径也另起前缀
（AstrBot 用 /api/v1、麦麦用 /api/chat 等，这里全部挂在 /astrmai/api/ 下）。

信任边界（他会把面板挂到公网，所以默认假设"有人在网络上试"）：
- **回环也要登录**：口令只存 scrypt 哈希；没配环境变量时用公开写死的初始口令 `astrmai-init`（懒人版：不用去控制台找随机串），代价是它必须强制改、且只接受本机登录；
- **初始密码没改之前，除登录/改密外一律 403**，机器 token 也拦；
- 会话 cookie：HttpOnly + SameSite=Strict + TLS 下加 Secure，空闲 30 分钟 / 绝对 8 小时，改密即全部作废；
- 机器通道用 X-Astrmai-Token，token 同样只存哈希（以前明文躺在 settings.json 里，那是漏口）；
- 写操作（POST）额外要求 Origin 与 Host（或 ASTRMAI_PANEL_PUBLIC_HOST）一致，挡 CSRF；
- 10 分钟内连错 5 次按来源锁定，时长从 60 秒指数递增到上限 15 分钟；
- **绑非回环必须有证书**（ASTRMAI_PANEL_CERT/KEY），否则拒绝启动并给两条出路；
- 所有响应过 scrub()：凭据字段只报"有没有"，明文口令/token 不进文件、不进日志、不进任何响应。
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .credentials import (
    COOKIE_NAME,
    DEFAULT_INITIAL_PASSWORD,
    KDF,
    MIN_LENGTH,
    SESSION_TTL,
    CredentialStore,
    LoginGuard,
    SessionStore,
)
from .load import LoadProbe
from .panel_data import (
    ASTRMAI_VERSION,
    LogRing,
    about_page,
    apply_settings,
    conversation_history,
    conversations_page,
    cron_apply,
    cron_page,
    overview,
    _platform_connections,
    persona_apply,
    personas_page,
    plugin_manager,
    plugins_page,
    provider_apply,
    providers_page,
    scrub,
    sessions_page,
    settings_page,
    tool_call,
    tools_page,
)

STATIC_DIR = Path(__file__).resolve().parent / "panel_static"
WORKSPACE = Path(__file__).resolve().parents[1]
API_ROOT = "/astrmai/api"

# 未登录也能拿的：外壳、样式、脚本，以及"我还差什么"的状态查询
PUBLIC_PATHS = {
    "/astrmai",
    "/astrmai/",
    "/astrmai/app.js",
    "/astrmai/style.css",
    f"{API_ROOT}/auth/status",
}
# 强制改密期间仍然允许的：只有登录与改密本身
AUTH_PATHS = {
    f"{API_ROOT}/auth/status",
    f"{API_ROOT}/auth/login",
    f"{API_ROOT}/auth/change_password",
    f"{API_ROOT}/auth/logout",
}
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class LogCapture(logging.Handler):
    """把两家引擎的日志收进内存环，不落盘、不外传。"""

    def __init__(self, ring: LogRing) -> None:
        super().__init__(level=logging.INFO)
        self.ring = ring

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            self.ring.add(record.levelname, record.name[:24], record.getMessage()[:400])
        except Exception:  # noqa: BLE001 - 日志处理器绝不能把宿主搞崩
            pass


def _peer_ip(request: Any) -> str:
    try:
        return str(request.transport.get_extra_info("peername")[0])
    except (AttributeError, IndexError, TypeError):
        return ""


def _trust_proxy() -> bool:
    """操作者显式声明"面板躲在反向代理后面"。这是"要不要认 XFF"的唯一依据。"""

    return os.environ.get("ASTRMAI_PANEL_TRUST_PROXY", "").strip().lower() in ("1", "true", "yes")


def _origin_host(request: Any) -> str:
    for header in ("Origin", "Referer"):
        value = request.headers.get(header)
        if value:
            parsed = urlparse(str(value))
            return parsed.netloc
    return ""


class AstrmaiPanel:
    def __init__(self, get_star: Callable[[], Any], *, ledger: Any, logs: LogRing) -> None:
        self.get_star = get_star
        self.ledger = ledger
        self.logs = logs
        self.host = ""
        self.port = 0
        self.tls = False
        self.public_host = os.environ.get("ASTRMAI_PANEL_PUBLIC_HOST", "").strip()
        self.credentials = CredentialStore()
        self.sessions = SessionStore()
        self.guard = LoginGuard()
        self._runner: Any = None
        self._site: Any = None
        self._handler: LogCapture | None = None
        self.last_error = ""
        self.probe = LoadProbe(workspace=WORKSPACE, watch_pids=self._runner_pids)

    def _runner_pids(self) -> list[int]:
        star = self.get_star()
        host = getattr(star, "host", None) if star is not None else None
        try:
            return list(host.live_runners()) if host is not None else []
        except Exception:  # noqa: BLE001
            return []

    def _counters(self) -> tuple[int, int]:
        star = self.get_star()
        if star is None:
            return (0, 0)
        return (int(getattr(star, "forwarded", 0) or 0), int(getattr(star, "sent_total", 0) or 0))

    # ---------------- 生命周期 ----------------

    async def start(self, host: str | None = None, port: int | None = None) -> dict:
        """启动控制台。host/port 由调用方显式传入时优先于环境变量。

        不要拿 os.environ 当参数管道：插件把自己算出的值写回环境变量后，
        下一次启动它就会被自己写的那条挡住（我们就是这样把"关不掉面板"做出来的）。
        """

        from aiohttp import web

        bind = str(host if host else os.environ.get("ASTRMAI_PANEL_HOST", "127.0.0.1")).strip() or "127.0.0.1"
        port = int(port if port else os.environ.get("ASTRMAI_PANEL_PORT", "18124"))
        ssl_context = self._build_tls(bind)

        created_password, plain_password = self.credentials.password.bootstrap(
            os.environ.get("ASTRMAI_PANEL_PASSWORD", ""), default_secret=DEFAULT_INITIAL_PASSWORD
        )
        if created_password and self.credentials.password.is_default:
            print(
                f"[astrmai] 面板初始口令：{DEFAULT_INITIAL_PASSWORD}"
                "  ← 登录后会被要求立刻改掉；该口令只接受来自本机的登录。",
                flush=True,
            )
        elif created_password:
            print("[astrmai] 面板口令按环境变量建立，未要求改密。", flush=True)
        # 机器 token 只在操作者显式给了值时才建立（不自动生成的规则写在凭据记录里）
        self.credentials.token.bootstrap(os.environ.get("ASTRMAI_PANEL_TOKEN", ""))

        @web.middleware
        async def guard(request: Any, handler: Any) -> Any:
            if request.path in PUBLIC_PATHS or not request.path.startswith(API_ROOT):
                return await handler(request)
            if request.path in AUTH_PATHS:
                # 登录/改密/状态自己在处理函数里判锁定与初始口令；
                # 在这里插一条"登录被锁就拒"的分支是到不了的死路（以前就是这么写的）
                return await handler(request)
            if self.credentials.password.must_change:
                return web.json_response(
                    {"ok": False, "must_change": True, "reason": "初始密码还没改，改完才能用其他功能"}, status=403
                )
            if not self._authenticated(request):
                return web.json_response({"ok": False, "login_required": True, "reason": "未登录"}, status=401)
            if request.method not in ("GET", "HEAD"):
                allowed, why = self._write_allowed(request)
                if not allowed:
                    return web.json_response({"ok": False, "reason": why}, status=403)
            return await handler(request)

        app = web.Application(middlewares=[guard])
        app.router.add_get("/astrmai", self._index)
        app.router.add_get("/astrmai/", self._index)
        app.router.add_get("/astrmai/app.js", self._static_js)
        app.router.add_get("/astrmai/style.css", self._static_css)
        app.router.add_get(f"{API_ROOT}/auth/status", self._auth_status)
        app.router.add_post(f"{API_ROOT}/auth/login", self._auth_login)
        app.router.add_post(f"{API_ROOT}/auth/change_password", self._auth_change)
        app.router.add_post(f"{API_ROOT}/auth/logout", self._auth_logout)
        app.router.add_get(f"{API_ROOT}/overview", self._overview)
        app.router.add_get(f"{API_ROOT}/load", self._load)
        app.router.add_get(f"{API_ROOT}/tools", self._tools)
        app.router.add_post(f"{API_ROOT}/tools/call", self._tools_call)
        app.router.add_get(f"{API_ROOT}/plugins", self._plugins)
        app.router.add_get(f"{API_ROOT}/sessions", self._sessions)
        app.router.add_get(f"{API_ROOT}/logs", self._logs)
        app.router.add_get(f"{API_ROOT}/settings", self._settings)
        app.router.add_get(f"{API_ROOT}/about", self._about)
        app.router.add_get(f"{API_ROOT}/licenses", self._licenses)
        app.router.add_post(f"{API_ROOT}/market/search", self._market_search)
        app.router.add_post(f"{API_ROOT}/market/install", self._market_install)
        app.router.add_post(f"{API_ROOT}/plugins/load", self._plugin_load)
        app.router.add_post(f"{API_ROOT}/plugins/uninstall", self._plugin_uninstall)
        app.router.add_post(f"{API_ROOT}/tools/resync", self._tools_resync)
        app.router.add_post(f"{API_ROOT}/settings", self._settings_apply)
        app.router.add_get(f"{API_ROOT}/providers", self._providers)
        app.router.add_post(f"{API_ROOT}/providers/apply", self._providers_apply)
        app.router.add_get(f"{API_ROOT}/personas", self._personas)
        app.router.add_post(f"{API_ROOT}/personas/apply", self._personas_apply)
        app.router.add_get(f"{API_ROOT}/conversations", self._conversations)
        app.router.add_get(f"{API_ROOT}/conversations/history", self._conversation_history)
        app.router.add_get(f"{API_ROOT}/cron", self._cron)
        app.router.add_post(f"{API_ROOT}/cron/apply", self._cron_apply)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        try:
            self._site = web.TCPSite(self._runner, bind, port, ssl_context=ssl_context)
            await self._site.start()
        except OSError as exc:
            self.last_error = f"绑定失败: {exc}"
            await self._runner.cleanup()
            self._runner = None
            raise RuntimeError(self.last_error) from exc
        self.host, self.port = bind, port
        self._attach_log_handler()
        self.probe.attach_counters(self._counters)
        await self.probe.start()
        return {
            "host": bind,
            "port": port,
            "tls": self.tls,
            "must_change": self.credentials.password.must_change,
            "scheme": "https" if self.tls else "http",
            "version": ASTRMAI_VERSION,
        }

    async def stop(self) -> None:
        await self.probe.stop()
        self._detach_log_handler()
        if self._site is not None:
            try:
                await self._site.stop()
            finally:
                self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def _attach_log_handler(self) -> None:
        if self._handler is not None:
            return
        self._handler = LogCapture(self.logs)
        logging.getLogger().addHandler(self._handler)

    def _detach_log_handler(self) -> None:
        if self._handler is None:
            return
        try:
            logging.getLogger().removeHandler(self._handler)
        finally:
            self._handler = None

    # ---------------- 门 ----------------

    def _build_tls(self, bind: str) -> Any:
        """非回环必须有 TLS：公网上传明文口令等于把凭据送给路上每一个人。

        拒绝启动而不是"警告后继续"，但把两条出路都写清楚，不让人卡在原地。
        """

        cert = os.environ.get("ASTRMAI_PANEL_CERT", "").strip()
        key = os.environ.get("ASTRMAI_PANEL_KEY", "").strip()
        if bind in LOOPBACK:
            if cert and key:
                return self._load_cert(cert, key)
            return None
        if not (cert and key):
            if os.environ.get("ASTRMAI_PANEL_INSECURE", "").strip().lower() in ("1", "true", "yes"):
                print(
                    f"[astrmai] 警告：面板绑在 {bind} 且没有证书，密码与会话 cookie 将以明文过网。"
                    "只在你自己确信的链路（如已加密的隧道）里这么用。",
                    flush=True,
                )
                self.last_error = "非回环但未启用 TLS（已显式承认）"
                return None
            raise RuntimeError(
                f"面板要绑到非回环地址 {bind}，必须先配 TLS：设 ASTRMAI_PANEL_CERT 与 ASTRMAI_PANEL_KEY"
                "（PEM 证书与私钥），或者放在反向代理后面并显式设 ASTRMAI_PANEL_INSECURE=1 承担明文风险。"
            )
        return self._load_cert(cert, key)

    def _load_cert(self, cert: str, key: str) -> Any:
        import ssl

        for path in (cert, key):
            if not Path(path).exists():
                raise RuntimeError(f"证书/私钥文件不存在: {path}")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=cert, keyfile=key)
        try:
            context.minimum_version = ssl.TLSVersion.TLSv1_2
        except (AttributeError, ValueError):
            pass
        self.tls = True
        return context

    def _source(self, request: Any) -> str:
        """给爆破计数与来源标记用的地址。只有显式信任代理时才认 X-Forwarded-For，而且只认**最右**一段。

        最右一段是我们自己的 nginx 追加的（`$proxy_add_x_forwarded_for`），客户端写不进那一段；
        最左一段是他自带的 —— 拿它当来源，攻击者每次尝试换一个假 IP 就能领一个新计数器，锁定等于没有。
        """

        if _trust_proxy():
            hops = [h.strip() for h in request.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
            if hops:
                return hops[-1]
        return _peer_ip(request) or "unknown"

    def _default_password_allowed(self, request: Any) -> bool:
        """公开写死的初始口令只接受**本机**。改过之后（is_default=False）这条自动失效。

        判据是 `_source()`，不是裸对端：挂了反向代理时 TCP 对端永远是回环，认对端等于把公开口令
        对全网开放。这条防线成立的前提是代理按文档用 `$proxy_add_x_forwarded_for` **追加**真实来源，
        于是最右一段由不得客户端编；`X-Forwarded-For: 127.0.0.1` 这种伪造会被追加成
        `127.0.0.1, <真客户端>`，取最右就还是远程地址。（改成复制头情的部署不在信任模型里。）
        """

        if not self.credentials.password.is_default:
            return True
        return self._source(request) in LOOPBACK

    def _authenticated(self, request: Any) -> bool:
        cookie = request.cookies.get(COOKIE_NAME, "")
        if cookie and self.sessions.valid(cookie):
            return True
        given = request.headers.get("X-Astrmai-Token") or ""
        if given and self.credentials.token.configured and self.credentials.token.verify(str(given)):
            return True
        return False

    def _write_allowed(self, request: Any) -> tuple[bool, str]:
        allowed_hosts = {str(request.host)}
        if self.public_host:
            allowed_hosts.add(self.public_host)
        raw = ""
        for header in ("Origin", "Referer"):
            if request.headers.get(header):
                raw = str(request.headers.get(header))
                break
        if raw:
            origin = _origin_host(request)
            # 带了 Origin/Referer 却解析不出主机（"null"、data:、被裁剪过的头）一律按跨站处理：
            # 沙箱 iframe 与 data: 页面的 Origin 就是 null，放过就等于开 CSRF
            if not origin:
                return False, f"Origin 无法解析（{raw[:40]}），按跨站拒绝"
            if origin not in allowed_hosts:
                return False, f"Origin 与 Host 不一致（{origin}），按 CSRF 拒绝"
        elif self._source(request) not in LOOPBACK and not request.headers.get("X-Astrmai-Token"):
            return False, "非回环写操作必须带 token 或来自已登录会话"
        return True, ""

    # ---------------- 认证端点 ----------------

    async def _auth_status(self, request: Any) -> Any:
        from aiohttp import web

        authenticated = self._authenticated(request)
        # 初始口令的"值"是给本机运维看的便利，不是给未登录的远端看的情报；
        # 审计流水（谁登过、锁了谁）同理 —— 只有登进来才看得到。
        local = self._default_password_allowed(request)
        out = {
            "ok": True,
            "authenticated": authenticated,
            "must_change": self.credentials.password.must_change,
            "first_run": not self.credentials.password.configured,
            "configured": self.credentials.password.configured,
            "tls": self.tls,
            "lockout_left": round(self.guard.blocked_for(self._source(request))),
            "kdf": KDF,
            "min_length": MIN_LENGTH,
            "default_password_in_use": self.credentials.password.is_default,
            "default_password_hint": DEFAULT_INITIAL_PASSWORD if (local and self.credentials.password.is_default) else "",
            "token_configured": self.credentials.token.configured,
        }
        if authenticated:
            out.update(
                {
                    "notes": list(self.credentials.notes)[-3:],
                    "sessions": self.sessions.count(),
                    "recent_auth": list(self.sessions.events)[-8:],
                    "recent_locks": list(self.guard.events)[-8:],
                }
            )
        return web.json_response(out)

    async def _auth_login(self, request: Any) -> Any:
        from aiohttp import web

        body = await _json_body(request)
        who = self._source(request)
        blocked = self.guard.blocked_for(who)
        if blocked > 0:
            return web.json_response({"ok": False, "reason": f"失败次数过多，{round(blocked)}s 后再试"}, status=429)
        password = str(body.get("password") or "")
        if not self._default_password_allowed(request):
            # 固定初始口令是公开的，只允许在本机用它登录；远端先摸到面板的人既改不掉它也进不去
            self.guard.note_failure(who)
            return web.json_response(
                {"ok": False, "reason": "初始口令只接受服务器本机登录（走代理来的按真实来源判，一律不认）；请先在服务器上登录并把它改掉"},
                status=403,
            )
        if not self.credentials.password.verify(password):
            wait = self.guard.note_failure(who)
            return web.json_response(
                {"ok": False, "reason": f"密码不对（已锁 {round(wait)}s）" if wait else "密码不对"},
                status=401,
            )
        self.guard.note_success(who)
        session = self.sessions.issue(who=who)
        response = web.json_response(
            {"ok": True, "must_change": self.credentials.password.must_change, "tls": self.tls},
            headers={"Cache-Control": "no-store"},
        )
        response.set_cookie(
            COOKIE_NAME,
            session,
            max_age=int(int(SESSION_TTL)),
            httponly=True,
            samesite="strict",
            secure=self.tls,
            path="/astrmai",
        )
        return response

    async def _auth_change(self, request: Any) -> Any:
        from aiohttp import web

        body = await _json_body(request)
        ok, msg = self.credentials.change_password(str(body.get("current") or ""), str(body.get("new") or ""))
        if not ok:
            return web.json_response({"ok": False, "reason": msg}, status=400)
        revoked = self.sessions.revoke_all(reason="password changed")
        response = web.json_response({"ok": True, "reason": msg, "revoked_sessions": revoked}, headers={"Cache-Control": "no-store"})
        response.del_cookie(COOKIE_NAME, path="/astrmai")
        return response

    async def _auth_logout(self, request: Any) -> Any:
        from aiohttp import web

        self.sessions.drop(request.cookies.get(COOKIE_NAME, ""))
        response = web.json_response({"ok": True}, headers={"Cache-Control": "no-store"})
        response.del_cookie(COOKIE_NAME, path="/astrmai")
        return response

    # ---------------- 只读端点 ----------------

    async def _index(self, request: Any) -> Any:
        from aiohttp import web

        return self._asset("index.html", "text/html; charset=utf-8")

    async def _static_js(self, request: Any) -> Any:
        return self._asset("app.js", "application/javascript; charset=utf-8")

    async def _static_css(self, request: Any) -> Any:
        return self._asset("style.css", "text/css; charset=utf-8")

    def _asset(self, name: str, content_type: str) -> Any:
        from aiohttp import web

        path = STATIC_DIR / name
        if not path.exists():
            return web.Response(text=f"缺少前端文件 {name}", status=500)
        # aiohttp 不许 content_type 里带 charset，得拆成两个参数
        base = content_type.split(";")[0].strip()
        return web.Response(body=path.read_bytes(), content_type=base, charset="utf-8")

    async def _overview(self, request: Any) -> Any:
        return _json(overview(self.get_star(), self.ledger))

    async def _load(self, request: Any) -> Any:
        """首页数据：主机负载 + 本进程 + 融合运行时（含 runner 与事件循环延迟）。"""

        data = self.probe.snapshot()
        star = self.get_star()
        conn = _platform_connections(star) if star is not None else {"ok": False, "error": "无引擎", "connected": False, "total_connections": 0}
        data["engine"] = {
            "booted": bool(getattr(star, "booted", False)) if star is not None else False,
            "boot_ms": round(getattr(getattr(star, "host", None), "boot_ms", 0.0)) if star is not None else 0,
            "sessions": len(getattr(star, "_umo_by_target", {}) or {}) if star is not None else 0,
            "connected": conn["connected"],
            "total_connections": conn["total_connections"],
            "job_guard": (getattr(getattr(star, "host", None), "job", None).status() if (star is not None and getattr(getattr(star, "host", None), "job", None)) else {"mode": "none"}),
        }
        return _json(scrub(data))

    async def _tools(self, request: Any) -> Any:
        return _json(tools_page(self.get_star()))

    async def _tools_call(self, request: Any) -> Any:
        """给外部调用方（麦麦侧的桥插件）执行一个 AstrBot 工具。

        走的是和其它写端点同一道闸：必须已登录/带 token、过 CSRF、改密前不放行。
        """

        body, denied = await self._body(request)
        if denied is not None:
            return denied
        result = await tool_call(self.get_star(), body)
        return _json(result, status=200 if result.get("ok") else 400)

    async def _plugins(self, request: Any) -> Any:
        return _json(plugins_page(self.get_star()))

    async def _sessions(self, request: Any) -> Any:
        return _json(sessions_page(self.get_star()))

    async def _settings(self, request: Any) -> Any:
        return _json(settings_page(self.get_star()))

    async def _about(self, request: Any) -> Any:
        return _json(about_page())

    async def _licenses(self, request: Any) -> Any:
        """已装组件的许可证清单；认不出的显式 unknown，不留空。"""

        star = self.get_star()
        try:
            rows = star._market().licenses()
        except Exception as exc:  # noqa: BLE001
            return _json({"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:200]}", "items": []}, status=502)
        unknown = [r["component"] for r in rows if r.get("undetermined")]
        return _json(
            scrub(
                {
                    "ok": True,
                    "items": rows,
                    "total": len(rows),
                    "unknown_count": len(unknown),
                    "unknown": unknown,
                    "conveyance": "合并作品只能整体按 AGPL-3.0-or-later 分发，并附完整对应源码",
                }
            )
        )

    async def _logs(self, request: Any) -> Any:
        from aiohttp import web

        try:
            since = float(request.query.get("since", "0"))
        except ValueError:
            since = 0.0
        limit = min(400, max(10, int(request.query.get("limit", "120") or 120)))
        level = (request.query.get("level") or "").upper()
        rows = self.logs.rows(limit=limit, since=since)
        if level:
            rows = [r for r in rows if r["level"].startswith(level)]
        return _json({"items": scrub(rows), "buffered": self.logs.size(), "panel_version": ASTRMAI_VERSION})

    # ---------------- 写端点 ----------------

    async def _body(self, request: Any) -> tuple[dict, Any]:
        """取 JSON 体。认证与 CSRF 已经由中间件统一拦过，这里不重复一套判断。"""

        return (await _json_body(request)), None

    async def _market_search(self, request: Any) -> Any:
        body, denied = await self._body(request)
        if denied is not None:
            return denied
        star = self.get_star()
        source = str(body.get("source") or "astrbot")
        try:
            found = await star._market().search(
                source,
                str(body.get("query") or ""),
                int(body.get("limit") or 20),
                int(body.get("offset") or 0),
                force=bool(body.get("force")),
            )
            return _json(scrub(found))
        except Exception as exc:  # noqa: BLE001
            return _json({"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}, status=502)

    async def _market_install(self, request: Any) -> Any:
        body, denied = await self._body(request)
        if denied is not None:
            return denied
        star = self.get_star()
        source, ident = str(body.get("source") or ""), str(body.get("id") or "")
        if not source or not ident:
            return _json({"ok": False, "reason": "需要 source 与 id"}, status=400)
        try:
            from astrmai.loader import LoadPolicy

            out = await star._market().install(source, ident)
            if out.get("ok"):
                out["load"] = await star._load_installed(source, out, LoadPolicy(allow_deps=bool(body.get("allow_deps"))))
            return _json(scrub(out))
        except Exception as exc:  # noqa: BLE001
            return _json({"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}, status=502)

    async def _plugin_load(self, request: Any) -> Any:
        body, denied = await self._body(request)
        if denied is not None:
            return denied
        star = self.get_star()
        source = str(body.get("source") or "")
        name = str(body.get("name") or "")
        if plugin_manager(star) is None and source == "astrbot":
            return _json({"ok": False, "reason": "AstrBot 核心没在跑，拿不到 PluginManager"}, status=503)
        try:
            from astrmai.loader import LoadPolicy

            result = await star._load_installed(source, {"path": name}, LoadPolicy(allow_deps=bool(body.get("allow_deps"))))
            return _json(scrub({"ok": True, "report": str(result)}))
        except Exception as exc:  # noqa: BLE001
            return _json({"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}, status=502)

    async def _plugin_uninstall(self, request: Any) -> Any:
        body, denied = await self._body(request)
        if denied is not None:
            return denied
        star = self.get_star()
        try:
            out = star._market().uninstall(str(body.get("source") or ""), str(body.get("name") or ""))
            return _json(scrub(out))
        except Exception as exc:  # noqa: BLE001
            return _json({"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}, status=502)

    async def _tools_resync(self, request: Any) -> Any:
        body, denied = await self._body(request)
        if denied is not None:
            return denied
        star = self.get_star()
        try:
            summary = star._sync_maibot_tools()
            return _json(scrub({"ok": True, "summary": summary}))
        except Exception as exc:  # noqa: BLE001
            return _json({"ok": False, "reason": f"{type(exc).__name__}: {exc}"}, status=502)

    async def _settings_apply(self, request: Any) -> Any:
        body, denied = await self._body(request)
        if denied is not None:
            return denied
        patch = body.get("patch") if isinstance(body.get("patch"), dict) else body
        return _json(scrub(apply_settings(self.get_star(), patch or {})))

    # ---------------- AstrBot 功能面：provider / 人格 / 对话 / cron ----------------

    async def _providers(self, request: Any) -> Any:
        return _json(providers_page(self.get_star()))

    async def _providers_apply(self, request: Any) -> Any:
        body, denied = await self._body(request)
        if denied is not None:
            return denied
        action = str(body.get("action") or "")
        result = await provider_apply(self.get_star(), action, body)
        return _json(result, status=200 if result.get("ok") else 400)

    async def _personas(self, request: Any) -> Any:
        return _json(await personas_page(self.get_star()))

    async def _personas_apply(self, request: Any) -> Any:
        body, denied = await self._body(request)
        if denied is not None:
            return denied
        action = str(body.get("action") or "")
        result = await persona_apply(self.get_star(), action, body)
        return _json(result, status=200 if result.get("ok") else 400)

    async def _conversations(self, request: Any) -> Any:
        try:
            page = max(1, int(request.query.get("page", "1")))
        except ValueError:
            page = 1
        try:
            limit = min(50, max(5, int(request.query.get("limit", "20"))))
        except ValueError:
            limit = 20
        return _json(await conversations_page(self.get_star(), page=page, limit=limit))

    async def _conversation_history(self, request: Any) -> Any:
        cid = str(request.query.get("cid", "") or "")
        if not cid:
            return _json({"ok": False, "reason": "需要 cid"}, status=400)
        try:
            limit = min(200, max(5, int(request.query.get("limit", "40"))))
        except ValueError:
            limit = 40
        return _json(await conversation_history(self.get_star(), cid, limit=limit))

    async def _cron(self, request: Any) -> Any:
        return _json(await cron_page(self.get_star()))

    async def _cron_apply(self, request: Any) -> Any:
        body, denied = await self._body(request)
        if denied is not None:
            return denied
        action = str(body.get("action") or "")
        result = await cron_apply(self.get_star(), action, body)
        return _json(result, status=200 if result.get("ok") else 400)

    # ---------------- 状态 ----------------

    def status(self) -> dict:
        """面板与凭据的姿态。任何字段都不带明文口令或 token。"""

        creds = self.credentials.status()
        return {
            "enabled": self._site is not None,
            "host": self.host,
            "port": self.port,
            "scheme": "https" if self.tls else "http",
            "tls": self.tls,
            "password": {
                "configured": creds["password"]["configured"],
                "must_change": creds["password"]["must_change"],
                "is_default": creds["password"]["is_default"],
                "algorithm": creds["password"]["algorithm"],
                "updated_at": creds["password"]["updated_at"],
            },
            "notes": list(creds.get("notes") or [])[-3:],
            "token": {
                "configured": creds["token"]["configured"],
                "algorithm": creds["token"]["algorithm"],
                "stored_as": "hash",  # 明文只在生成那一次打到控制台，文件里永远没有
            },
            "sessions": self.sessions.count(),
            "error": self.last_error,
            "version": ASTRMAI_VERSION,
        }

    def reset_password(self) -> str:
        """恢复口：只能从本机/QQ 管理路径调用，绝不通过 HTTP —— 否则未登录的人就能重置。"""

        password = self.credentials.password.reset(default_secret=DEFAULT_INITIAL_PASSWORD)
        self.sessions.revoke_all(reason="password reset")
        return password


async def _json_body(request: Any) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - 空体或坏 JSON 都按空处理
        return {}
    return body if isinstance(body, dict) else {}


def _json(payload: Any, status: int = 200) -> Any:
    from aiohttp import web

    return web.json_response(payload, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))
