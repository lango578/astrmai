r"""打一个可以上架的干净发布树：只带 astrmai 自己的代码与文档，然后强制过隐私扫描。

刻意不包含两个上游引擎的源码（AstrBot=AGPL、麦麦=GPL）：
用户按 docs/DEPLOY.md 自己取，避免把别人的仓库重新包装成"我的发布"，
也避免把自己机器上带着配置和数据的上游目录顺手传出去。

用法：
    python tools/pack_release.py            # 构建 + 扫描 + 打包
    python tools/pack_release.py --no-zip   # 只构建并扫描
"""

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
DIST = WS / "dist"

# 明确白名单：不在这里的一律不进发布树
INCLUDE_DIRS = [
    "astrmai",
    "astrmai_plugin",
    "astrmai_plugins",
    "maibot_plugins",
    "tests",
    "tools",
    "docs",
    "assets",
]
INCLUDE_FILES = [
    "run_real.py",
    "run_tests.py",
    "model_config_io.py",
    "use_local_model.py",
    "use_real_model.py",
    "mock_llm.py",
    "README.md",
    "LICENSE",
    "THIRD_PARTY.md",
    ".gitignore",
]
# 目录内的排除项（大小写不敏感，按名字匹配）
EXCLUDE_NAMES = {
    "__pycache__", ".venv", "pythons", "uv-cache", "cache", "e2e", "data", "secrets",
    "astrmai_status.json", "privacy_denylist.txt", "panel_shot.py",  # 截图工具会写出带本机路径的图片
    # 这几个是别的项目（MC 服务器凭据收集/加固）的工具，和 astrmai 无关，一律不随包走
    "get_key.cmd", "set_key.ps1", "selfcheck_key.ps1", "harden_secrets.ps1",
    # 凭据录入脚本一律不随包走：它们写 secrets/、是交互式的，别人拿到也不该照抄本机路径
    "get_github_token.cmd", "set_github_token.ps1", "selfcheck_github.ps1",
    # publish_github.py 与 make_denylist.py 反之要随包走：里面一个真值都没有，
    # 接包的人需要"怎么推上去"和"怎么用自己的真实值建隐私清单"
}
EXCLUDE_SUFFIXES = {".pyc", ".log", ".png", ".jpg", ".zip", ".db", ".sqlite3", ".key", ".pem", ".tmp", ".skill"}


def version() -> str:
    try:
        text = (WS / "astrmai" / "panel_data.py").read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    for line in text.splitlines():
        if line.startswith("ASTRMAI_VERSION"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return "0.0.0"


def copy_tree(src: Path, dst: Path) -> int:
    written = 0
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        parts = [p.lower() for p in rel.parts]
        if any(part in EXCLUDE_NAMES for part in parts):
            continue
        if path.suffix.lower() in EXCLUDE_SUFFIXES:
            continue
        if path.name.lower() in EXCLUDE_NAMES:
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        written += 1
    return written


def build() -> Path:
    ver = version()
    out = DIST / f"astrmai-{ver}"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    total = 0
    for name in INCLUDE_DIRS:
        src = WS / name
        if not src.exists():
            print(f"  跳过（不存在）: {name}")
            continue
        total += copy_tree(src, out / name)
    for name in INCLUDE_FILES:
        src = WS / name
        if not src.exists():
            print(f"  跳过（不存在）: {name}")
            continue
        if src.is_dir():
            total += copy_tree(src, out / name)
        else:
            (out / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out / name)
            total += 1
    print(f"构建 {out}：{total} 个文件")
    return out


def scan(target: Path, strict: bool = False) -> int:
    cmd = [sys.executable, str(WS / "tools" / "privacy_scan.py"), str(target)]
    if strict:
        cmd.append("--strict")
    return subprocess.run(cmd).returncode


# 修复回归闸门：隐私扫描看的是"有没有泄露"，这几条看的是"修过的洞有没有被改回去"。
# 第三方复检里的 H1/H2/H3/H4/L3 每一条都在这里留一道机器检查 —— 靠我记得是不够的。
GATES = [
    (
        "astrmai_plugin/main.py",
        ["if not admins:"],
        ["if admins and str("],
        "H1 /mai 闸门 fail-closed（名单为空必须拒绝）",
    ),
    (
        "astrmai/panel.py",
        ["return hops[-1]"],
        ['forwarded.split(",")[0]'],
        "L3 来源取 XFF 最右一段（伪造首段不能冒充本机）",
    ),
    (
        "astrmai/inbound.py",
        ["ASTRMAI_MEDIA_ROOTS", "def onebot_to_maim_dict_async"],
        ["urllib.request.urlopen"],
        "H4 媒体取回：默认拒本地路径 + 有异步入口",
    ),
    (
        "astrmai/market.py",
        ["out.is_relative_to(staging_root)", "_require_network(\"取 AstrBot 目录\")"],
        [],
        "H2 安装落盘校验 zip slip；H3 出网闸覆盖 AstrBot 那条",
    ),
]


def content_gates(target: Path) -> list[str]:
    bad: list[str] = []
    for rel, must_have, must_not, label in GATES:
        path = target / rel
        if not path.exists():
            bad.append(f"{label}：发布树里找不到 {rel}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        missing = [needle for needle in must_have if needle not in text]
        present = [needle for needle in must_not if needle in text]
        if missing:
            bad.append(f"{label}：{rel} 缺少 {missing}")
        if present:
            bad.append(f"{label}：{rel} 又出现了 {present}（这条防线被改回去了）")
    return bad


def zip_up(target: Path) -> Path:
    archive = DIST / f"{target.name}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(target.rglob("*")):
            if path.is_file():
                zf.write(path, arcname=path.relative_to(DIST))
    print(f"打包 {archive}（{archive.stat().st_size / 1024:.0f} KB）")
    return archive


def main(argv: list[str]) -> int:
    make_zip = "--no-zip" not in argv
    strict = "--strict" in argv
    target = build()
    print("\n发布前隐私扫描：")
    code = scan(target, strict=strict)
    if code != 0:
        print("\n扫描未通过，已拒绝打包。先清掉上面列出的项再重试。")
        return code
    print("\n扫描通过。")
    print("\n修复回归闸门（发布树里必须有这些防线）：")
    problems = content_gates(target)
    for line in problems:
        print("  FAIL", line)
    if problems:
        print("\n闸门未过，已拒绝打包。别把修过的洞发出去。")
        return 1
    print(f"  全部 {len(GATES)} 组在位。")
    if make_zip:
        zip_up(target)
    print(f"\n下一步：cd {target} && git init && git add -A && git commit -m 'astrmai {version()}'")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
