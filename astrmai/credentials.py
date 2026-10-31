"""面板凭据：内存困难哈希存储 + 首次密码强制改 + 公网暴露下的会话加固。

他会把面板挂到公网上，所以这里的默认假设是"有人在网络上试"：

- 密码用 scrypt（内存困难）存，参数一起落盘，验证走常数时间比较；
- 机器用的 token 同样只存哈希 —— 以前它明文躺在 settings.json 里，那是个漏口；
- 明文只在生成的那一次返回给调用方打印，不进文件、不进日志、不进任何 API 响应、不进状态文件；
- 会话 cookie：HttpOnly + SameSite=Strict + TLS 下加 Secure，空闲 30 分钟 / 绝对 8 小时过期，
  改密后旧会话全部作废；
- 初始密码没改之前，**所有功能一律 403**，token 也拦；
- 非回环绑定但没有证书 → 直接拒绝启动，除非显式 ASTRMAI_PANEL_INSECURE=1（大声警告）。
"""

import hashlib
import hmac
import json
import os
import secrets
import time
from collections import deque
from pathlib import Path
from typing import Any

# ---- KDF 参数（写进记录，验证时按记录里的参数复现，将来调参不会读不出老凭据） ----
KDF = "scrypt"
FAST_KDF = "sha256-salt"
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
DKLEN = 32
MAXMEM = 64 * 1024 * 1024

MIN_LENGTH = 12
# 机器 token 用快哈希存，代价是必须够长：短 token 被拖库就能爆
MIN_TOKEN_LENGTH = 24

# 首次启动的固定初始口令（懒人版）：公开写在代码与文档里，
# 代价是必须①登录后立刻改、②只接受本机登录，否则谁先摸到面板谁就先改掉它把你锁外面
DEFAULT_INITIAL_PASSWORD = "astrmai-init"  # privacy-scan:allow 这是写在文档里的公开初始口令，不是秘密（强制改密 + 只允许本机登录才是防线）
SESSION_IDLE = 30 * 60.0
SESSION_TTL = 8 * 3600.0
MAX_FAILURES = 5
LOCKOUT_BASE = 60.0
LOCKOUT_MAX = 15 * 60.0

COOKIE_NAME = "astrmai_session"
WEAK = {
    "astrmai",
    "password",
    "password1",
    "administrator",
    "qwerty",
    "123456",
    "changeme",
    "admin123",
    "iloveyou",
}


def state_dir() -> Path:
    """凭据与面板设置的家目录。

    市场安装的插件目录会在更新时被整个删掉重建（AstrBot 的 updater 先 rmtree 再 move），
    所以那种布局下状态必须落在 AstrBot 的 `data/plugin_data/astrmai/`，不能跟着插件包一起被抹掉。
    仓库/开发布局下仍然是工作区里的 `astrmai_state/`。
    """

    override = os.environ.get("ASTRMAI_STATE_DIR", "").strip()
    if override:
        return Path(override)
    pkg_root = Path(__file__).resolve().parents[1]
    if pkg_root.parents[0].name == "plugins":
        # .../data/plugins/astrmai/astrmai/credentials.py -> .../data/plugin_data/astrmai
        return pkg_root.parents[1] / "plugin_data" / "astrmai"
    return pkg_root / "astrmai_state"


def credentials_path() -> Path:
    return state_dir() / "credentials.json"


def generate_secret(length: int = 20) -> str:
    """随机口令：去掉容易看错的字符，长度够。"""

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _derive(secret: str, salt: bytes, params: dict) -> bytes:
    """按记录里的算法派生。

    人用的口令走 scrypt（内存困难，抗离线爆破）；机器 token 是高熵随机串，
    用 scrypt 会让每个带 token 的请求多花 ~300ms —— 那是把安全做成了不可用。
    """

    if str(params.get("algorithm") or KDF) == FAST_KDF:
        return hashlib.sha256(salt + secret.encode("utf-8")).digest()
    return hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=int(params.get("n", SCRYPT_N)),
        r=int(params.get("r", SCRYPT_R)),
        p=int(params.get("p", SCRYPT_P)),
        dklen=int(params.get("dklen", DKLEN)),
        maxmem=MAXMEM,
    )


def _params_for(algorithm: str) -> dict:
    if algorithm == FAST_KDF:
        return {"algorithm": FAST_KDF, "dklen": DKLEN}
    return {"algorithm": KDF, "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "dklen": DKLEN}


def _policy(candidate: str, *, forbid: str | None = None) -> str:
    value = candidate or ""
    if len(value.strip()) < MIN_LENGTH:
        return f"新密码至少 {MIN_LENGTH} 位"
    if value.strip().lower() in WEAK:
        return "新密码太容易猜到，换一条"
    if len(set(value.strip())) < 5:
        return "新密码字符太单一（重复太多）"
    if forbid and hmac.compare_digest(value, forbid):
        return "新密码不能还是初始密码"
    return ""


class SecretRecord:
    """一份口令的哈希记录（密码与 token 共用同一套存法）。"""

    def __init__(
        self,
        field: str,
        store: "CredentialStore",
        *,
        algorithm: str = KDF,
        min_length: int = MIN_LENGTH,
        allow_generate: bool = True,
    ) -> None:
        self.field = field
        self.store = store
        self.algorithm = algorithm
        self.min_length = min_length
        # 随机生成一份没人知道的凭据等于凭空造个用不上的东西，所以这个不变量归记录自己
        self.allow_generate = allow_generate
        self._pending_plain = ""  # 只在本次进程内留着，用于"不许原样再用"

    @property
    def _rec(self) -> dict:
        return self.store.data.setdefault(self.field, {})

    @property
    def configured(self) -> bool:
        return bool(self._rec.get("hash")) and bool(self._rec.get("salt"))

    @property
    def must_change(self) -> bool:
        return bool(self._rec.get("must_change", False))

    @property
    def is_default(self) -> bool:
        """当前生效的是不是那个公开写着的初始口令。"""

        return bool(self._rec.get("is_default", False))

    def set(self, secret: str, *, must_change: bool, is_default: bool = False) -> None:
        salt = secrets.token_bytes(16)
        params = _params_for(self.algorithm)
        digest = _derive(secret, salt, params)
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.store.data[self.field] = {
            **params,
            "salt": salt.hex(),
            "hash": digest.hex(),
            "must_change": must_change,
            "is_default": is_default,
            "created_at": self._rec.get("created_at") or now,
            "updated_at": now,
        }
        self.store.save()

    def verify(self, secret: str) -> bool:
        rec = self._rec
        if not rec.get("hash") or not isinstance(secret, str) or not secret:
            return False
        try:
            salt = bytes.fromhex(str(rec["salt"]))
            want = bytes.fromhex(str(rec["hash"]))
        except (KeyError, TypeError, ValueError):
            return False
        got = _derive(secret, salt, rec)
        return hmac.compare_digest(got, want)

    def bootstrap(self, env_value: str, *, default_secret: str = "") -> tuple[bool, str]:
        """建立或对齐凭据。返回 (是否新建, 明文)。

        首次用**固定初始口令**（`default_secret`），不再生成随机串 —— 他要的是懒人版，
        但固定口令必须配两条硬约束：must_change 强制改，且只接受本机登录（见 panel._auth_login）。
        allow_generate=False 的记录（机器 token）不配任何默认值：随机生成一份没人知道的
        凭据等于凭空造个用不上的东西。
        已经存在记录、而环境变量又给了一个新值时，**按环境变量重建**并说清楚：
        否则"我在启动命令里改了 token，面板却不认"会变成一个查不出来的坑。
        """

        provided = (env_value or "").strip()
        if provided and len(provided) < self.min_length:
            note = f"忽略环境变量给的 {self.field}：只有 {len(provided)} 位，少于要求的 {self.min_length} 位"
            self.store.notes.append(note)
            print(f"[astrmai] {note}。", flush=True)
            provided = ""
        if self.configured:
            if provided and not self.verify(provided):
                self._pending_plain = provided
                self.set(provided, must_change=False)
                print(
                    f"[astrmai] 检测到环境变量给了新的 {self.field}，与已存凭据不一致：已按环境变量重建，"
                    "旧凭据立即失效。",
                    flush=True,
                )
                return True, provided
            return False, ""
        secret = provided or (default_secret if self.allow_generate else "")
        if not secret:
            return False, ""
        self._pending_plain = secret
        # 只有回落到公开默认口令时才强制改；操作者自己给的口令不逼他再改一遍
        self.set(secret, must_change=not provided, is_default=not bool(provided))
        return True, secret

    def reset(self, *, default_secret: str = "") -> str:
        """恢复口：回到固定初始口令，并重新要求改密。"""

        secret = default_secret or generate_secret()
        self._pending_plain = secret
        self.set(secret, must_change=True, is_default=bool(default_secret))
        return secret

    def status(self) -> dict:
        rec = self._rec
        return {
            "configured": self.configured,
            "must_change": self.must_change,
            "is_default": self.is_default,
            "algorithm": str(rec.get("algorithm") or ""),
            "n": rec.get("n"),
            "r": rec.get("r"),
            "p": rec.get("p"),
            "dklen": rec.get("dklen"),
            "created_at": str(rec.get("created_at") or ""),
            "updated_at": str(rec.get("updated_at") or ""),
            "min_length": MIN_LENGTH,
        }


class CredentialStore:
    """astrmai_state/credentials.json：只有哈希与参数，永远没有明文。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else credentials_path()
        self.data: dict[str, Any] = {}
        self.error = ""
        self.notes: list[str] = []  # 例如"环境变量给的 token 太短被忽略"——不能只留在控制台
        self.load()
        self.password = SecretRecord("password", self, algorithm=KDF, min_length=MIN_LENGTH)
        # 机器 token：高熵随机串用快哈希存（避免每个请求多花 300ms），代价是必须够长，且不自动生成
        self.token = SecretRecord(
            "token", self, algorithm=FAST_KDF, min_length=MIN_TOKEN_LENGTH, allow_generate=False
        )

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.error = f"凭据文件读不出来: {type(exc).__name__}"
            return
        self.data = data if isinstance(data, dict) else {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)  # Windows 上近乎装饰性，真正的收口在 tools/harden_secrets.ps1
        except OSError:
            pass

    def change_password(self, current: str, new: str) -> tuple[bool, str]:
        if not self.password.configured:
            return False, "还没有凭据，谈不上改密"
        if not self.password.verify(current):
            return False, "当前密码不对"
        reason = _policy(new, forbid=self.password._pending_plain or None)
        if reason:
            return False, reason
        self.password.set(new, must_change=False)
        self.password._pending_plain = ""
        return True, "已改密；旧会话已全部作废，请用新密码重新登录"

    def status(self) -> dict:
        return {
            "password": self.password.status(),
            "token": self.token.status(),
            "error": self.error,
            "notes": list(self.notes)[-5:],
        }


class SessionStore:
    """内存会话表。空闲/绝对双超时，改密或重置口令时整体作废。

    有上限：拿到口令的人刷登录也不该把服务端内存撑爆。
    """

    MAX_SESSIONS = 64

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, float]] = {}
        self.events: deque = deque(maxlen=100)

    def _purge(self, now: float) -> None:
        for token in [t for t, r in self._sessions.items() if now - r["issued"] > SESSION_TTL or now - r["seen"] > SESSION_IDLE]:
            self._sessions.pop(token, None)

    def issue(self, *, who: str) -> str:
        now = time.time()
        self._purge(now)
        while len(self._sessions) >= self.MAX_SESSIONS:
            oldest = next(iter(self._sessions))  # dict 保持插入序，丢最早的那个
            self._sessions.pop(oldest, None)
            self._log("evict", "-", "会话数到上限，最早的会话被顶掉")
        token = secrets.token_urlsafe(32)
        self._sessions[token] = {"issued": now, "seen": now}
        self._log("login", who, "ok")
        return token

    def valid(self, token: str) -> bool:
        if not token:
            return False
        rec = self._sessions.get(token)
        if rec is None:
            return False
        now = time.time()
        if now - rec["issued"] > SESSION_TTL or now - rec["seen"] > SESSION_IDLE:
            self._sessions.pop(token, None)
            return False
        rec["seen"] = now
        return True

    def drop(self, token: str) -> None:
        self._sessions.pop(str(token or ""), None)

    def revoke_all(self, *, reason: str) -> int:
        n = len(self._sessions)
        self._sessions.clear()
        self._log("revoke_all", "-", reason)
        return n

    def count(self) -> int:
        return len(self._sessions)

    def _log(self, kind: str, who: str, note: str) -> None:
        self.events.append({"ts": time.time(), "kind": kind, "who": str(who)[:40], "note": str(note)[:120]})


class LoginGuard:
    """按来源计失败次数，锁定时长递增；只记来源与次数，不记口令。

    来源条目有上限：开了信任代理后 XFF 是可以随便伪造的，不设上限就等于让人用假 IP 撑内存。
    """

    MAX_SOURCES = 256

    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._strike: dict[str, int] = {}
        self.events: deque = deque(maxlen=100)

    def _trim(self) -> None:
        for table in (self._failures, self._locked_until, self._strike):
            while len(table) > self.MAX_SOURCES:
                table.pop(next(iter(table)), None)

    def blocked_for(self, who: str) -> float:
        return max(0.0, self._locked_until.get(str(who), 0.0) - time.time())

    def note_failure(self, who: str) -> float:
        key = str(who)
        now = time.time()
        recent = [t for t in self._failures.get(key, []) if now - t < 600.0]
        recent.append(now)
        self._failures[key] = recent
        self._trim()
        if len(recent) < MAX_FAILURES:
            self.events.append({"ts": now, "kind": "fail", "who": key[:40], "note": f"{len(recent)}/{MAX_FAILURES}"})
            return 0.0
        strikes = self._strike.get(key, 0) + 1
        self._strike[key] = strikes
        wait = min(LOCKOUT_BASE * (2 ** (strikes - 1)), LOCKOUT_MAX)
        self._locked_until[key] = now + wait
        self._failures[key] = []
        self.events.append({"ts": now, "kind": "lockout", "who": key[:40], "note": f"{round(wait)}s"})
        return wait

    def note_success(self, who: str) -> None:
        key = str(who)
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)
        self._strike.pop(key, None)
