r"""发布前隐私扫描：证明要上架的树里没有密钥、真实身份、本机痕迹与运行数据。

设计约束（很重要）：**这个脚本自己会上架**，所以它内部绝不硬编码任何真实值 ——
把 QQ 号/域名写进扫描器，等于在反面教材里复述真值。真实值放在
`tools/privacy_denylist.txt`（每行一条，已被 .gitignore 排除），脚本在时就用、不在就只跑通用规则。

用法：
    python tools/privacy_scan.py <目录> [--strict]
退出码 0 = 干净；1 = 发现问题（--strict 时连"待人工确认"也算失败）。
形状规则可以被行内 marker 豁免（`# privacy-scan:allow 理由`），豁免项一律降级成警告并把理由打进报告；命中 denylist 里的真实值不给豁免通道。
"""

import ipaddress
import re
import sys
from pathlib import Path

# 绝不允许出现在发布树里的目录名（运行数据、上游代码、本机环境、凭据）
FORBIDDEN_DIRS = {
    ".venv", "venv", "__pycache__", ".git", "node_modules",
    "secrets", "astrmai_state", "data", "e2e", "probe", "cache", "dist",
    "vendor", "MaiBot-main", "MaiBot-Napcat-Adapter-main", "pythons", "uv-cache",
    "logs", "uploads", "backups",
}
# 绝不允许出现的文件（凭据/数据库/日志/截图/包）
FORBIDDEN_NAMES = {
    "credentials.json", "settings.json", "model_credentials.json", ".env",
    "MaiBot.db", "db.sqlite3", "market_live_report.json", "astrmai_status.json",
}
FORBIDDEN_SUFFIXES = {
    ".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3", ".zip", ".tar", ".gz",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".whl", ".exe", ".dll", ".key", ".pem", ".p12",
}

# 规则名 → 编译好的正则（作用于文本文件内容）
RULES = {
    "疑似 API 密钥": re.compile(rb"sk-[A-Za-z0-9]{8,}"),
    "疑似 Bearer/私钥块": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "疑似 Windows 用户目录绝对路径": re.compile(rb"[A-Za-z]:[\\/]{1,2}[Uu]sers[\\/]{1,2}[A-Za-z0-9_.-]{2,}"),
    "疑似盘符下的私有工作目录": re.compile(rb"[A-Za-z]:[\\/]{1,2}(Qoder|edge|Desktop|Downloads)[\\/]"),
    "疑似邮箱地址": re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "疑似 IPv4 公网地址": re.compile(rb"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "疑似 QQ 号（10-11 位连续数字）": re.compile(rb"(?<!\d)\d{10,11}(?!\d)"),
    "疑似自建域名": re.compile(rb"\b[a-z0-9][a-z0-9.-]{2,}\.(?:vip|top|xyz|cn|club|icu|site|online|store)\b"),
    "疑似密码赋值": re.compile(rb"(?i)(password|passwd|secret|token)\s*[=:]\s*['\"][^'\"]{6,}['\"]"),
}

# 这些命中是"预期存在"的，不算泄露：本机文档里的示例值、测试桩值
ALLOW_PATTERNS = {
    # 链路本地、TEST-NET 文档段与 RFC2544 基准段：这些地址不可路由，写出来只可能是示例或安全用例，
    # 不可能是"泄露了一台真机器的地址"（169.254 出现在媒体护栏里，198.51.100/203.0.113 出现在反代取证里）
    "疑似 IPv4 公网地址": (re.compile(rb"127\.0\.0\.1"), re.compile(rb"0\.0\.0\.0"), re.compile(rb"10\.0\.0\.\d"), re.compile(rb"192\.168\.\d+\.\d+"), re.compile(rb"203\.0\.113\.\d"), re.compile(rb"198\.51\.100\.\d+"), re.compile(rb"192\.0\.2\.\d+"), re.compile(rb"198\.18\.\d+\.\d+"), re.compile(rb"169\.254\.\d+\.\d+"), re.compile(rb"1\.2\.3\.4")),
    "疑似邮箱地址": (re.compile(rb"@example\.(com|org|net)"), re.compile(rb"@astrmai\.(local|invalid)"), re.compile(rb"noreply@github")),
}

# 只警告不失败（需要人看一眼，但常常是合法的：版本号、端口、示例）
WARN_ONLY = {"疑似自建域名", "疑似 QQ 号（10-11 位连续数字）"}

# 行内豁免标记：`# privacy-scan:allow 理由`。只免形状规则、必须写理由，扫描报告会原样列出理由。
ALLOW_MARKER = b"privacy-scan:allow"


def denylist() -> list[bytes]:
    path = Path(__file__).resolve().parent / "privacy_denylist.txt"
    if not path.exists():
        return []
    values = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            values.append(value.encode("utf-8"))
    return values


def scan(root: Path) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    warnings: list[str] = []
    banned = denylist()
    root = Path(root).resolve()
    if not root.exists():
        return [f"目录不存在: {root}"], warnings

    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        parts = [p.lower() for p in rel.parts]

        if path.is_dir():
            if path.name.lower() in FORBIDDEN_DIRS:
                problems.append(f"目录不该出现在发布树里：{rel}")
            continue

        if any(part in FORBIDDEN_DIRS for part in parts):
            continue  # 已经在目录级报过

        if path.name in FORBIDDEN_NAMES:
            problems.append(f"凭据/运行数据文件不该出现：{rel}")
            continue
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            problems.append(f"二进制/字节码/数据文件不该出现在源码仓库：{rel}")
            continue
        if path.name.lower() == "privacy_denylist.txt":
            problems.append(f"真实值清单本身绝不能上架：{rel}")
            continue

        try:
            data = path.read_bytes()
        except OSError as exc:
            problems.append(f"读不了 {rel}: {exc}")
            continue

        for needle in banned:
            if needle in data:
                # 只报长度与字符类别，绝不复述真值的前几个字符（QQ 号只要 4 位就够被认出来）
                kind = "纯数字" if needle.isdigit() else "含空格/符号" if not needle.isalnum() else "字母数字串"
                problems.append(f"{rel}: 命中 denylist 里的真实值（长度 {len(needle)}，{kind}）")

        if b"\x00" in data[:4096]:
            warnings.append(f"{rel}: 看起来是二进制文件，请确认是否该发布")
            continue

        for label, pattern in RULES.items():
            reported = 0
            for match in pattern.finditer(data):
                start = data[: match.start()].count(b"\n")
                line_no = start + 1
                line = data.splitlines()[start] if start < len(data.splitlines()) else b""
                snippet = match.group(0)[:48].decode("utf-8", "replace")
                allowed = any(p.search(match.group(0)) for p in ALLOW_PATTERNS.get(label, ()))
                if allowed:
                    continue
                # 行内豁免只能免"形状猜出来的"规则，且必须写理由；
                # denylist 里的真实值命中走不到这里，加 marker 也照样 FAIL。
                waived = ALLOW_MARKER in line
                message = f"{rel}:{line_no}: {label} → {snippet}" + (
                    f"（人工豁免：{line.split(ALLOW_MARKER)[-1].decode('utf-8', 'replace').strip()[:60]}）" if waived else ""
                )
                if waived or label in WARN_ONLY:
                    warnings.append(message)
                else:
                    problems.append(message)
                reported += 1
                if reported >= 3:  # 每条规则每文件最多报 3 处：够看清，又不会刷屏
                    break

    return problems, warnings


def main(argv: list[str]) -> int:
    strict = "--strict" in argv
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print("用法: python tools/privacy_scan.py <目录> [--strict]")
        return 2
    problems, warnings = scan(Path(args[0]))

    print(f"扫描 {args[0]}：问题 {len(problems)} 项，待人工确认 {len(warnings)} 项", flush=True)
    for line in problems[:80]:
        print("  [FAIL]", line, flush=True)
    for line in warnings[:80]:
        print("  [WARN]", line, flush=True)
    if len(problems) > 80 or len(warnings) > 80:
        print("  …（只列前 80 条）", flush=True)
    if not problems and not warnings:
        print("  干净：没有密钥形状、真实值、本机路径或运行数据。", flush=True)
    if problems:
        return 1
    if strict and warnings:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
