"""面板凭据取证：哈希存储、强制改密、爆破锁定、TLS 门槛。

他会把面板挂公网，所以这里验的不是"能不能登录"，而是**泄露面**：
明文口令在任何文件、任何响应、任何日志里都不许出现一次。
"""

import asyncio
import ipaddress
import json
import os
import shutil
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))

import httpx  # noqa: E402

STATE = WS / "e2e" / "auth_state"
os.environ["ASTRMAI_STATE_DIR"] = str(STATE)
os.environ["ASTRMAI_PANEL_HOST"] = "127.0.0.1"

from astrmai.config import AstrmaiConfig  # noqa: E402
from astrmai.credentials import COOKIE_NAME, DEFAULT_INITIAL_PASSWORD  # noqa: E402
from astrmai.panel import AstrmaiPanel  # noqa: E402
from astrmai.panel_data import Ledger, LogRing  # noqa: E402

FAILS: list[str] = []
SEEN_PLAINTEXT: list[str] = []


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
        host=SimpleNamespace(cfg=cfg, boot_ms=1234.0, job=None, live_runners=lambda: []),
        booted=True,
        boot_error="",
        forwarded=1,
        sent_total=1,
        _umo_by_target={},
        _event_by_target={},
        federation={},
        context=None,
        native=None,
    )


def self_signed(dirpath: Path) -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "astrmai test")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = dirpath / "panel-cert.pem"
    key_path = dirpath / "panel-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
    )
    return str(cert_path), str(key_path)


def scan_for_plaintext(label: str) -> None:
    """把整个状态目录 + 日志翻一遍：任何一次明文口令出现都算泄露。"""

    for secret in SEEN_PLAINTEXT:
        for path in STATE.rglob("*"):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if secret and secret in text:
                check(f"{label}：{path.name} 里没有明文口令", False, "找到了！")
                return
        # 凭据文件本身也必须扫（上面已覆盖），这里只报一次成功
    check(f"{label}：{len(SEEN_PLAINTEXT)} 次明文都没落盘", True, str(STATE))


async def first_run_and_forced_change() -> None:
    for key in ("ASTRMAI_PANEL_PASSWORD", "ASTRMAI_PANEL_TOKEN", "ASTRMAI_PANEL_CERT", "ASTRMAI_PANEL_KEY", "ASTRMAI_PANEL_INSECURE"):
        os.environ.pop(key, None)
    port = free_port()
    os.environ["ASTRMAI_PANEL_PORT"] = str(port)

    panel = AstrmaiPanel(make_star, ledger=Ledger(), logs=LogRing())
    info = await panel.start()
    initial = panel.credentials.password._pending_plain
    SEEN_PLAINTEXT.append(initial)
    check(
        "首次启动用固定初始口令并要求立刻改",
        info["must_change"] is True and initial == DEFAULT_INITIAL_PASSWORD and panel.credentials.password.is_default is True,
        f"口令={initial} is_default={panel.credentials.password.is_default}",
    )

    check("没显式配置就不该凭空生成一份没人知道的机器 token", panel.credentials.token.configured is False, "")

    # 固定初始口令 = 公开知识，所以只许本机登录；这条必须在改密后自动失效
    def fake(peer: str):
        return SimpleNamespace(
            headers={},
            transport=SimpleNamespace(get_extra_info=lambda k, p=peer: (p, 443) if k == "peername" else None),
        )

    check("初始口令允许本机登录", panel._default_password_allowed(fake("127.0.0.1")) is True, "")
    check("初始口令拒绝远端登录（谁先摸到面板都改不掉它、也进不去）", panel._default_password_allowed(fake("203.0.113.9")) is False, "")
    check("初始口令拒绝局域网登录", panel._default_password_allowed(fake("192.168.1.7")) is False, "")

    creds_raw = (STATE / "credentials.json").read_text(encoding="utf-8")
    check("凭据文件里搜不到明文口令", initial not in creds_raw, "")
    record = json.loads(creds_raw)["password"]
    check("口令用 scrypt 存且参数一起落盘", record["algorithm"] == "scrypt" and record["n"] >= 2**14 and len(record["hash"]) == 64, str({k: record[k] for k in ("algorithm", "n", "r", "p", "dklen")}))
    check("存的是定长摘要不是可逆加密", len(record["salt"]) == 32, f"salt hex {len(record['salt'])}")

    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(base_url=base, timeout=15) as c:
        status = (await c.get("/astrmai/api/auth/status")).json()
        check("auth/status 公开可读，前端据此弹登录框", status["authenticated"] is False and status["must_change"] is True, str(status)[:120])
        r = await c.get("/astrmai/api/overview")
        check("强制改密期间功能一律 403", r.status_code == 403 and r.json().get("must_change") is True, f"status={r.status_code}")

        bad = await c.post("/astrmai/api/auth/login", json={"password": "wrong-password-here"})
        check("错密码 401", bad.status_code == 401, str(bad.json())[:80])
        ok = await c.post("/astrmai/api/auth/login", json={"password": initial})
        cookie_header = ok.headers.get("Set-Cookie", "")
        check("初始密码能登录", ok.status_code == 200 and ok.json().get("must_change") is True, f"status={ok.status_code}")
        check(
            "会话 cookie 三件套齐（HttpOnly/SameSite=Strict/限定 path）",
            "httponly" in cookie_header.lower() and "samesite=strict" in cookie_header.lower() and "path=/astrmai" in cookie_header.lower(),
            cookie_header[:150],
        )
        r = await c.get("/astrmai/api/overview")
        check("登录后仍然 403，直到改完初始密码", r.status_code == 403, f"status={r.status_code}")

        short = await c.post("/astrmai/api/auth/change_password", json={"current": initial, "new": "short"})
        check("太短的新密码被拒", short.status_code == 400, str(short.json())[:80])
        same = await c.post("/astrmai/api/auth/change_password", json={"current": initial, "new": initial})
        check("原样沿用初始密码被拒", same.status_code == 400 and "初始" in same.json().get("reason", ""), str(same.json())[:90])
        wrong_cur = await c.post("/astrmai/api/auth/change_password", json={"current": "not-the-password", "new": "Zk9-mTq2vpLd7xQ"})
        check("改密也要验当前密码", wrong_cur.status_code == 400, str(wrong_cur.json())[:80])

        changed = await c.post("/astrmai/api/auth/change_password", json={"current": initial, "new": "Zk9-mTq2vpLd7xQ"})
        check("改密成功并作废全部会话", changed.status_code == 200 and changed.json().get("revoked_sessions", 0) >= 1, str(changed.json())[:120])
        SEEN_PLAINTEXT.append("Zk9-mTq2vpLd7xQ")
        r = await c.get("/astrmai/api/overview")
        check("旧会话立刻失效（401 而不是继续放行）", r.status_code == 401, f"status={r.status_code}")

        again = await c.post("/astrmai/api/auth/login", json={"password": "Zk9-mTq2vpLd7xQ"})
        check("新密码可登录", again.status_code == 200 and again.json().get("must_change") is False, str(again.json())[:100])
        r = await c.get("/astrmai/api/overview")
        check("改完密后功能放行", r.status_code == 200, f"status={r.status_code}")
        check(
            "改掉初始口令后，本机限制自动解除",
            panel._default_password_allowed(fake("203.0.113.9")) is True and panel.credentials.password.is_default is False,
            "",
        )
        check("改掉初始口令后，公开默认口令不再被接受", panel.credentials.password.verify("astrmai-init") is False, "")
        scan_for_plaintext("改密之后")

        # 锁定不能牵连同源已认证的会话：先验退出，再制造锁定
        out = await c.post("/astrmai/api/auth/logout", json={})
        after = await c.get("/astrmai/api/overview")
        check(
            "退出后 cookie 作废",
            out.status_code == 200 and after.status_code == 401,
            f"logout={out.status_code} 再访问={after.status_code} 会话数={panel.sessions.count()} cookie={dict(c.cookies)}",
        )

        # 爆破锁定：另一条连接连错到阈值（同一来源，但已登录的会话不该被牵连）
        relogin = await c.post("/astrmai/api/auth/login", json={"password": "Zk9-mTq2vpLd7xQ"})
        check("重新登录拿到会话", relogin.status_code == 200, f"status={relogin.status_code}")
        async with httpx.AsyncClient(base_url=base, timeout=15) as attacker:
            codes = []
            for _ in range(7):
                codes.append((await attacker.post("/astrmai/api/auth/login", json={"password": "guess-guess-guess"})).status_code)
            check("连错若干次之后开始回 429（按来源锁登录）", 429 in codes, str(codes))
            locked = await attacker.get("/astrmai/api/auth/status")
            check("锁定期间 auth/status 告知剩余秒数", locked.json().get("lockout_left", 0) > 0, str(locked.json())[:110])
            still = await c.get("/astrmai/api/overview")
            check("同一来源被锁定时，已登录会话照常可用", still.status_code == 200, f"status={still.status_code}")
    await panel.stop()


async def token_hashed_and_tls_gate() -> None:
    token = "astrmai-machine-token-Zq7-2x"  # privacy-scan:allow 靶场合成口令：只在 127.0.0.1 的测试实例里用，不是任何真实凭据
    os.environ["ASTRMAI_PANEL_TOKEN"] = token
    os.environ["ASTRMAI_PANEL_PASSWORD"] = "Zk9-mTq2vpLd7xQ"  # 已存在，不会重置
    port = free_port()
    os.environ["ASTRMAI_PANEL_PORT"] = str(port)
    SEEN_PLAINTEXT.append(token)

    panel = AstrmaiPanel(make_star, ledger=Ledger(), logs=LogRing())
    await panel.start()
    raw = (STATE / "credentials.json").read_text(encoding="utf-8")
    check("机器 token 只存哈希（高熵串用快算法，不让人机请求各付 300ms）", token not in raw and json.loads(raw)["token"]["algorithm"] == "sha256-salt", str(json.loads(raw)["token"].get("algorithm")))
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=15, headers={"X-Astrmai-Token": token}) as c:
        r = await c.get("/astrmai/api/overview")
        check("token 通道可用（给脚本/CLI 用）", r.status_code == 200, f"status={r.status_code} body={r.text[:120]} port={port} must_change={panel.credentials.password.must_change} tokcfg={panel.credentials.token.configured}")
        st = panel.status()
        check("面板状态里不含任何明文凭据", token not in json.dumps(st) and "Zk9" not in json.dumps(st), str(st)[:120])
    await panel.stop()

    # 非回环的 TLS 门槛：直接验判定，不去真绑 0.0.0.0
    probe = AstrmaiPanel(make_star, ledger=Ledger(), logs=LogRing())
    os.environ.pop("ASTRMAI_PANEL_CERT", None)
    os.environ.pop("ASTRMAI_PANEL_KEY", None)
    os.environ.pop("ASTRMAI_PANEL_INSECURE", None)
    try:
        probe._build_tls("0.0.0.0")
        check("非回环无证书必须拒绝启动", False, "居然放行了")
    except RuntimeError as exc:
        check("非回环无证书拒绝启动并给出路", "ASTRMAI_PANEL_CERT" in str(exc) and "INSECURE" in str(exc), str(exc)[:120])
    os.environ["ASTRMAI_PANEL_INSECURE"] = "1"
    check("显式承认明文风险才放行（并大声警告）", probe._build_tls("0.0.0.0") is None, "")
    os.environ.pop("ASTRMAI_PANEL_INSECURE")

    cert, key = self_signed(STATE)
    os.environ["ASTRMAI_PANEL_CERT"] = cert
    os.environ["ASTRMAI_PANEL_KEY"] = key
    ctx = probe._build_tls("0.0.0.0")
    check("给了证书就能建 TLS 上下文", ctx is not None and probe.tls is True, str(type(ctx)))

    # 真的走一次 https 握手（只绑回环，不外露）
    tls_panel = AstrmaiPanel(make_star, ledger=Ledger(), logs=LogRing())
    tls_port = free_port()
    os.environ["ASTRMAI_PANEL_PORT"] = str(tls_port)
    info = await tls_panel.start()
    check("带证书时面板以 https 起", info["tls"] is True and info["scheme"] == "https", str(info)[:120])
    # 不关证书校验：把这张自签证书显式当 CA 信任，SAN 里有 127.0.0.1，主机名校验照走
    async with httpx.AsyncClient(base_url=f"https://127.0.0.1:{tls_port}", timeout=15, verify=cert) as c:
        r = await c.get("/astrmai/api/auth/status")
        check("https 真握手成功", r.status_code == 200 and r.json().get("tls") is True, f"status={r.status_code}")
        login = await c.post("/astrmai/api/auth/login", json={"password": "Zk9-mTq2vpLd7xQ"})
        sc = login.headers.get("Set-Cookie", "")
        check("TLS 下 cookie 才加 Secure 标记", "secure" in sc.lower(), sc[:140])
    await tls_panel.stop()
    scan_for_plaintext("token/TLS 之后")


async def main() -> int:
    if STATE.exists():
        shutil.rmtree(STATE)
    STATE.mkdir(parents=True)
    try:
        await first_run_and_forced_change()
        await token_hashed_and_tls_gate()
    finally:
        for key in ("ASTRMAI_PANEL_CERT", "ASTRMAI_PANEL_KEY", "ASTRMAI_PANEL_TOKEN", "ASTRMAI_PANEL_PASSWORD"):
            os.environ.pop(key, None)
        shutil.rmtree(STATE, ignore_errors=True)

    print("\n结果:", "全部通过" if not FAILS else f"{len(FAILS)} 项失败 {FAILS}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
