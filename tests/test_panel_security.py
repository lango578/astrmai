"""面板攻击面与可用性取证：不是验"功能能跑"，是验"坏消息进不来、好东西不泄露、人用得顺"。

每一项都是先怀疑自己写的东西，再让它自证：
- 未认证能不能拿到数据；错误信息会不会泄露绝对路径/堆栈；参数畸形会不会 500；
- 爆破、会话与锁定表会不会无界增长（DoS）；凭据会不会出现在任何响应、日志或文件里；
- 可用性：表单会不会被自动刷新清空、会话过期后前端弹的是登录还是过时的 token 框。
"""

import asyncio
import json
import os
import shutil
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))
sys.path.insert(0, str(WS / "vendor" / "AstrBot-master"))

import httpx  # noqa: E402

STATE = WS / "e2e" / "sec_state"
os.environ["ASTRMAI_STATE_DIR"] = str(STATE)
os.environ["ASTRMAI_PANEL_HOST"] = "127.0.0.1"
os.environ.pop("ASTRMAI_PANEL_TRUST_PROXY", None)

from astrmai.config import AstrmaiConfig  # noqa: E402
from astrmai.panel import AstrmaiPanel  # noqa: E402
from astrmai.panel_data import Ledger, LogRing  # noqa: E402

FAILS: list[str] = []
PASSWORD = "astrmai-sec-Zx9-4mqd-pass"  # privacy-scan:allow 靶场合成口令：只在 127.0.0.1 的测试实例里用，不是任何真实凭据
PROXY_PASSWORD = "astrmai-sec-proxy-Kq72-host"  # privacy-scan:allow 靶场合成口令：只在 127.0.0.1 的测试实例里用，不是任何真实凭据
TOKEN = "astrmai-sec-token-7Qm3xZk"  # privacy-scan:allow 靶场合成口令：只在 127.0.0.1 的测试实例里用，不是任何真实凭据
MAX_SESSIONS_EXPECTED = 64
AUTH_HEADERS = {"X-Astrmai-Token": TOKEN}
READ_PATHS = (
    "/astrmai/api/overview",
    "/astrmai/api/load",
    "/astrmai/api/tools",
    "/astrmai/api/plugins",
    "/astrmai/api/sessions",
    "/astrmai/api/logs",
    "/astrmai/api/settings",
    "/astrmai/api/licenses",
    "/astrmai/api/about",
)
WRITE_PATHS = (
    "/astrmai/api/market/search",
    "/astrmai/api/market/install",
    "/astrmai/api/plugins/load",
    "/astrmai/api/plugins/uninstall",
    "/astrmai/api/tools/resync",
    "/astrmai/api/settings",
)


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


def make_star() -> SimpleNamespace:
    cfg = AstrmaiConfig(mai_root=WS / "MaiBot-main", account_id="10001", allowed_users={"777"})
    return SimpleNamespace(
        host=SimpleNamespace(
            cfg=cfg,
            boot_ms=1000.0,
            job=SimpleNamespace(status=lambda: {"mode": "self"}, describe=lambda: "host"),
            live_runners=lambda: [],
        ),
        booted=True,
        boot_error="",
        forwarded=0,
        sent_total=0,
        _umo_by_target={("private", "777"): "aiocqhttp:Private:777"},
        _event_by_target={},
        federation={},
        context=None,
        native=None,
        _market=lambda: None,
        _load_installed=lambda *a, **k: "stub",
        _sync_maibot_tools=lambda: {"total": 0, "added": 0},
    )


async def start_panel(**env) -> tuple[AstrmaiPanel, str]:
    for key, value in env.items():
        if value is None:
            os.environ.pop(f"ASTRMAI_PANEL_{key}", None)
        else:
            os.environ[f"ASTRMAI_PANEL_{key}"] = str(value)
    port = free_port()
    os.environ["ASTRMAI_PANEL_PORT"] = str(port)
    panel = AstrmaiPanel(make_star, ledger=Ledger(), logs=LogRing())
    await panel.start()
    return panel, f"http://127.0.0.1:{port}"


def leaks(text: str) -> list[str]:
    hits = []
    for needle in (PASSWORD, TOKEN, "sk-"):
        if needle and needle in text:
            hits.append(needle if needle != "sk-" else "sk- 形状")
    return hits


async def unauthenticated_surface(base: str) -> None:
    async with httpx.AsyncClient(base_url=base, timeout=15) as c:
        for path in READ_PATHS:
            r = await c.get(path)
            check(f"未认证读 {path} 被拒", r.status_code in (401, 403), f"status={r.status_code} body={r.text[:60]}")
        for path in WRITE_PATHS:
            r = await c.post(path, json={})
            check(f"未认证写 {path} 被拒", r.status_code in (401, 403), f"status={r.status_code}")
        # 数据一个字都不许漏
        bodies = ""
        for path in READ_PATHS:
            r = await c.get(path)
            if r.status_code < 400:
                bodies += r.text
        check("未认证响应里没有引擎数据", "unified_msg_origin" not in bodies and "model_bindings" not in bodies, bodies[:80])

        # 复检 L2：/auth/status 是公开的（前端靠它弹登录框），但情报要分级 —— 
        # 谁登过、锁了谁、有几个会话，这些只有登进来才该看见。
        r = await c.get("/astrmai/api/auth/status")
        body = r.json()
        check(
            "未认证的 /auth/status 不吐审计流水",
            "recent_auth" not in body and "recent_locks" not in body and "sessions" not in body,
            str(sorted(body))[:140],
        )
        check(
            "未认证的 /auth/status 仍够前端弹登录屏",
            body.get("authenticated") is False and "must_change" in body and "min_length" in body,
            str(body)[:90],
        )

        # 路径穿越与越权资源
        for probe in ("/astrmai/../MaiBot-main/config/model_config.toml", "/astrmai/..%2f..%2fcredentials.json", "/etc/passwd"):
            r = await c.get(probe)
            check(f"路径穿越 {probe[:38]} 不返回内容", r.status_code in (400, 403, 404) and "pbkdf2" not in r.text and "api_key" not in r.text, f"status={r.status_code}")


async def auth_abuse(base: str, panel: AstrmaiPanel) -> None:
    async with httpx.AsyncClient(base_url=base, timeout=20) as c:
        # 爆破：默认按真实对端计，伪造 X-Forwarded-For 不该绕过
        codes = []
        for i in range(8):
            r = await c.post(
                "/astrmai/api/auth/login",
                json={"password": f"guess-{i}-whatever"},
                headers={"X-Forwarded-For": f"10.0.0.{i}"},
            )
            codes.append(r.status_code)
        check("伪造 XFF 绕不过爆破锁（未显式信任代理时）", 429 in codes, str(codes))
        status = (await c.get("/astrmai/api/auth/status")).json()
        check("锁定状态被如实报出", status.get("lockout_left", 0) > 0, str(status)[:110])
        check("锁定不牵连其它端点（否则共享出口就是自我 DoS）", (await c.get("/astrmai/api/about")).status_code in (401, 403), "about 被一起拒了")


async def session_growth(base: str, panel: AstrmaiPanel) -> None:
    """拿到口令的人也不该能靠刷登录把服务端内存撑爆。

    上一项爆破测试已经把 127.0.0.1 锁了；不清掉锁定，这里的登录会全被 429 拒掉，
    于是"会话数=0"是假绿 —— 必须先解锁，再证明上限真的在起作用。
    """

    panel.guard.note_success("127.0.0.1")
    async with httpx.AsyncClient(base_url=base, timeout=30) as c:
        codes = []
        for _ in range(80):
            codes.append((await c.post("/astrmai/api/auth/login", json={"password": PASSWORD})).status_code)
    check("刷登录确实建立了会话（否则下面的上限断言是假绿）", codes.count(200) >= 50, f"200 次数={codes.count(200)}")
    count = panel.sessions.count()
    check("反复登录不会让会话表无界增长（有上限+回收）", 0 < count <= MAX_SESSIONS_EXPECTED, f"sessions={count} 期望 0<{count}≤{MAX_SESSIONS_EXPECTED}")
    guard_sources = len(panel.guard._failures) + len(panel.guard._locked_until) + len(panel.guard._strike)
    check("爆破计数表也有上限（不会按来源无限膨胀）", guard_sources <= 512, f"来源条目={guard_sources}")


async def malformed_inputs(base: str) -> None:
    async with httpx.AsyncClient(base_url=base, timeout=20, headers=AUTH_HEADERS) as c:
        cases = (
            ("/logs?limit=abc", "GET", None),
            ("/logs?since=-1e999", "GET", None),
            ("/logs?level=%00", "GET", None),
            ("/load?x=<<<", "GET", None),
        )
        for path, method, body in cases:
            r = await c.request(method, path)
            check(f"畸形参数 {path} 不 500", r.status_code < 500, f"status={r.status_code} body={r.text[:70]}")

        bad_json = await c.post("/astrmai/api/tools/resync", content=b"{not json", headers={"Content-Type": "application/json"})
        check("坏 JSON 体不 500", bad_json.status_code < 500, f"status={bad_json.status_code}")

        huge = await c.post("/astrmai/api/settings", content=b'{"patch":' + b" " * 200 + b"{" + b'"a"*1' + b"[]", headers={"Content-Type": "application/json"})
        check("超长/畸形体不炸服务端", huge.status_code < 500, f"status={huge.status_code}")

        # 错误信息不得泄露绝对路径或堆栈
        err = await c.post("/astrmai/api/plugins/load", json={"source": "astrbot", "name": "does_not_exist"})
        text = err.text
        check("失败响应不带绝对路径与堆栈", "Traceback" not in text and ("D:\\" not in text and "d:\\" not in text.lower()), text[:140])

        # 提交的东西不得被回显（尤其口令）
        echo = await c.post("/astrmai/api/settings", json={"patch": {"password": "Super-Secret-Value-123"}})
        check("被拒字段只回键名，不回提交的值", "Super-Secret-Value-123" not in echo.text, echo.text[:140])


async def csrf_and_cookie(base: str) -> None:
    async with httpx.AsyncClient(base_url=base, timeout=20, headers=AUTH_HEADERS) as c:
        r = await c.post("/astrmai/api/tools/resync", json={}, headers={"Origin": "http://evil.example"})
        check("跨站 Origin 的写被拒", r.status_code == 403, f"status={r.status_code}")
        r = await c.post("/astrmai/api/tools/resync", json={}, headers={"Origin": "null"})
        check("Origin: null 也算跨站，拒", r.status_code == 403, f"status={r.status_code}")
        r = await c.get("/astrmai/api/overview", headers={"Origin": "http://evil.example"})
        check("读操作不吃 Origin（只拦写）", r.status_code == 200, f"status={r.status_code}")

    async with httpx.AsyncClient(base_url=base, timeout=20) as fresh:
        login = await fresh.post("/astrmai/api/auth/login", json={"password": PASSWORD})
    sc = login.headers.get("Set-Cookie", "").lower()
    check("回环（无 TLS）下 cookie 仍是 HttpOnly+SameSite=strict", "httponly" in sc and "samesite=strict" in sc, sc[:140])
    check("无 TLS 时不加 Secure（否则本地 http 直接不能用）", "secure" not in sc, sc[:140])


async def secret_leak_scan(base: str) -> None:
    real_key = ""
    try:
        import tomlkit

        doc = tomlkit.parse((WS / "MaiBot-main" / "config" / "model_config.toml").read_text(encoding="utf-8"))
        for provider in (doc.get("api_providers") or []):
            value = str((provider or {}).get("api_key") or "") if isinstance(provider, dict) else ""
            if value:
                real_key = value
                break
    except Exception as exc:  # noqa: BLE001
        print(f"（读不到真 key 做对照: {type(exc).__name__}）", flush=True)

    async with httpx.AsyncClient(base_url=base, timeout=20, headers=AUTH_HEADERS) as c:
        blob = ""
        for path in READ_PATHS:
            blob += (await c.get(path)).text
        for path in ("/astrmai/", "/astrmai/app.js", "/astrmai/style.css"):
            blob += (await c.get(path)).text
        check("所有响应都不含 sk- 形状", "sk-" not in blob, "")
        if real_key and len(real_key) >= 12:
            # 短到几位 key 做子串扫描必然误命中（靶机假 key 就是 4 位），那种断言只会自嗨
            check("所有响应都不含当前生效密钥", real_key not in blob and real_key[:10] not in blob, f"key 长度 {len(real_key)}")
        else:
            check("密钥字段本身只回有没有值", '"api_key"' not in blob, f"key 太短（{len(real_key)} 位），子串扫描无意义")
        check("所有响应都不含面板口令/token", not leaks(blob), str(leaks(blob)))

    for path in STATE.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if PASSWORD in text or TOKEN in text:
            check(f"状态目录里搜不到明文（{path.name}）", False, "找到了")
            return
    check("状态目录里搜不到任何一次明文口令", True, str(STATE))


async def availability(base: str) -> None:
    """可用性：轮询会不会把正在填的表单清空、参数缓存有没有做。"""

    async with httpx.AsyncClient(base_url=base, timeout=30, headers=AUTH_HEADERS) as c:
        started = time.perf_counter()
        first = (await c.get("/astrmai/api/overview")).text
        for _ in range(20):
            again = (await c.get("/astrmai/api/overview")).text
        took = time.perf_counter() - started
        check("概览 21 次轮询不慢到影响使用（配置读盘有缓存）", took < 1.2, f"{took:.2f}s")
        check("概览内容稳定可重复", json.loads(first)["engine"]["booted"] == json.loads(again)["engine"]["booted"], "")

        # 前端：会话过期后要弹登录框，而不是过时的 token 框
        js = (await c.get("/astrmai/app.js")).text
        check("前端 401 走登录流程（不再弹过时的 token 框）", "renderTokenPrompt" not in js, "仍残留 token 提示分支" if "renderTokenPrompt" in js else "")
        check("前端自动刷新会保住正在输入的表单", "preservedForm" in js or "data-keep" in js or "captureInputs" in js, "没找到保输入的逻辑")


def token_policy_checks() -> None:
    """人用口令与机器 token 的算法必须分开：都给 scrypt 就等于每个请求付 300ms。"""

    from astrmai.credentials import CredentialStore

    path = STATE / "policy.json"
    path.unlink(missing_ok=True)
    store = CredentialStore(path=path)
    store.token.bootstrap("short-123")
    check("太短的机器 token 被拒（快哈希存必须有长度下限）", store.token.configured is False, "")
    good = "astrmai-a-decently-long-token-9x"
    store.token.bootstrap(good)
    check("够长的 token 能建立并可验证", store.token.configured and store.token.verify(good), "")
    check("token 用快算法存", store.token.status()["algorithm"] == "sha256-salt", store.token.status()["algorithm"])
    store.password.bootstrap("Zk9-mTq2vpLd7xQ-pass")
    check("人用的口令仍然用内存困难的 scrypt", store.password.status()["algorithm"] == "scrypt", store.password.status()["algorithm"])
    raw = path.read_text(encoding="utf-8")
    check("两种凭据都不落明文", good not in raw and "Zk9-mTq2vpLd7xQ-pass" not in raw, "")
    path.unlink(missing_ok=True)


async def proxy_traps() -> None:
    """挂反向代理时 TCP 对端永远是 127.0.0.1，真实来源只在 X-Forwarded-For 里。

    这一步盯的就是"信任代理之后，回环特判有没有把全网放进屋里"：
    公开写着的初始口令、以及没带 Origin 的写操作，都必须按 XFF 的真实来源判。
    """

    alt = WS / "e2e" / "sec_state_proxy"
    if alt.exists():
        shutil.rmtree(alt)
    alt.mkdir(parents=True)
    saved_state = os.environ.get("ASTRMAI_STATE_DIR")
    saved_password = os.environ.get("ASTRMAI_PANEL_PASSWORD")
    os.environ["ASTRMAI_STATE_DIR"] = str(alt)
    remote = {"X-Forwarded-For": "203.0.113.9"}
    panel, base = await start_panel(TRUST_PROXY="1", HOST="127.0.0.1", PASSWORD=None, TOKEN=None)
    try:
        async with httpx.AsyncClient(base_url=base, timeout=15) as c:
            # 复检 L2 的口令那半边：公开状态端点在初始口令还有效时，
            # 远端来源不该拿到口令的值，本机来源要拿得到（否则运维便利就坏了）
            r = await c.get("/astrmai/api/auth/status", headers=remote)
            check("远端来源的 /auth/status 不报初始口令的值", "astrmai-init" not in r.text, r.text[:80])
            r = await c.get("/astrmai/api/auth/status")
            check("本机来源仍看得到初始口令提示", r.json().get("default_password_hint") == "astrmai-init", str(r.json())[:90])

            r = await c.post("/astrmai/api/auth/login", json={"password": "astrmai-init"}, headers=remote)
            check("信任代理后，远程用公开初始口令登录被拒", r.status_code in (401, 403), f"status={r.status_code} {r.text[:70]}")
            r = await c.post("/astrmai/api/auth/login", json={"password": "astrmai-init"})
            check("本机仍能用初始口令登录（不能把运维自己也关在外面）", r.status_code == 200, f"status={r.status_code} {r.text[:70]}")
            check("初始口令登录后挂着强制改密", bool(r.json().get("must_change")), str(r.json())[:80])

            # 复检 L3：客户端写不进 nginx 追加的那一段。伪造 "127.0.0.1, <真客户端>" 时取最右一段，
            # 冒充本机这条路应该是堵的（取首段的老写法就是在这儿漏的）。
            r = await c.post(
                "/astrmai/api/auth/login",
                json={"password": "astrmai-init"},
                headers={"X-Forwarded-For": "127.0.0.1, 203.0.113.9"},
            )
            check(
                "伪造 XFF 首段冒充本机登录被拒（来源取最右一段）",
                r.status_code in (401, 403),
                f"status={r.status_code} {r.text[:70]}",
            )

            r = await c.post("/astrmai/api/auth/change_password", json={"current": "astrmai-init", "new": PROXY_PASSWORD})
            check("改密通道在 must_change 期间可用", r.status_code == 200 and r.json().get("ok") is True, f"status={r.status_code} {r.text[:70]}")
            check("改密后旧会话立刻作废", (await c.get("/astrmai/api/overview", cookies=r.cookies)).status_code == 401, "")

            login = await c.post("/astrmai/api/auth/login", json={"password": PROXY_PASSWORD})
            jar = login.cookies
            r = await c.post("/astrmai/api/settings", json={"patch": {"group_enabled": True}}, headers=remote, cookies=jar)
            check("远程来源的写操作（已登录、无 Origin）被拒", r.status_code in (401, 403), f"status={r.status_code} {r.text[:70]}")
            r = await c.post(
                "/astrmai/api/settings",
                json={"patch": {"group_enabled": True}},
                headers={**remote, "Origin": base, "Referer": f"{base}/astrmai/"},
                cookies=jar,
            )
            check("代理后面同源带 Origin 的正常写入不受影响", r.status_code == 200 and r.json().get("ok") is True, f"status={r.status_code} {r.text[:70]}")

            # 爆破锁定按真实来源算：每次换一个假的首段不该领到新计数器
            codes = []
            for i in range(6):
                rr = await c.post(
                    "/astrmai/api/auth/login",
                    json={"password": "astrmai-wrong-tries-9"},
                    headers={"X-Forwarded-For": f"10.9.{i}.1, 198.51.100.7"},
                )
                codes.append(rr.status_code)
            check("反复伪造首段绕不过爆破锁", 429 in codes, str(codes))
    finally:
        await panel.stop()
        if saved_state:
            os.environ["ASTRMAI_STATE_DIR"] = saved_state
        if saved_password is None:
            os.environ.pop("ASTRMAI_PANEL_PASSWORD", None)
        else:
            os.environ["ASTRMAI_PANEL_PASSWORD"] = saved_password
        os.environ.pop("ASTRMAI_PANEL_TRUST_PROXY", None)
        shutil.rmtree(alt, ignore_errors=True)


async def main() -> int:
    if STATE.exists():
        shutil.rmtree(STATE)
    STATE.mkdir(parents=True)

    token_policy_checks()

    panel, base = await start_panel(PASSWORD=PASSWORD, TOKEN=TOKEN)
    try:
        await unauthenticated_surface(base)
        await auth_abuse(base, panel)
        await session_growth(base, panel)
        await malformed_inputs(base)
        await csrf_and_cookie(base)
        await secret_leak_scan(base)
        await availability(base)
        await proxy_traps()
    finally:
        await panel.stop()
        shutil.rmtree(STATE, ignore_errors=True)

    print("\n结果:", "全部通过" if not FAILS else f"{len(FAILS)} 项失败")
    for f in FAILS:
        print("  -", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
