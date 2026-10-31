r"""组装可提交 AstrBot 官方市场的插件包：dist/astrbot-plugin-astrmai/。

和 pack_release.py 的区别：那个是"完整工程发布树"（要自备两个上游），这个只装**瘦宿主**——
桥接包 + 控制台 + 元数据。麦麦本体不进包（官方市场 zip 上限 16MB，且它是独立 GPL 项目），
依赖也不写 requirements.txt（AstrBot 会不问就 pip 进用户环境，违反我们"永不代你装包"的政策）。

    python tools/build_plugin.py            # 构建 + 闸门 + 打包
    python tools/build_plugin.py --no-zip
"""

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import zipfile

WS = pathlib.Path(__file__).resolve().parents[1]
DIST = WS / "dist"
NAME = "astrbot-plugin-astrmai"
OUT = DIST / NAME
ZIP = DIST / f"{NAME}.zip"
MAI_NAME = "maibot-plugin-astrbot-bridge"
MAI_OUT = DIST / MAI_NAME
MAI_ZIP = DIST / f"{MAI_NAME}.zip"
LIMIT = 16 * 1024 * 1024  # 官方市场 zip 上限：docs/zh/dev/star/plugin-publish.md

METADATA = """name: astrmai
display_name: astrmai 融合控制台
desc: |-
  把麦麦（MaiBot）的群聊人格引擎嵌进 AstrBot 的进程：一个 asyncio 循环、一条 NapCat 反向 WS、
  逐条消息裁决归属（命令与插件接管的消息留在 AstrBot，其余交给麦麦），同一条消息不会被答两遍。
  自带 Web 控制台（负载首屏 + 双脑状态 + 裁决流水 + AstrBot 功能面：provider/人格/对话历史/定时任务）。
  注意：本插件不打包麦麦本体（市场 zip 上限 16MB），需自备 MaiBot v1.3.18 并在配置里给出路径；
  插件不会替你 pip 安装任何依赖，缺什么会明确告诉你缺什么。整体按 AGPL-3.0-or-later 分发。
version: 0.1.0
author: astrmai
repo: https://github.com/lango578/astrbot-plugin-astrmai
astrbot_version: ">=4.29.0"
license: AGPL-3.0-or-later
tags:
  - maibot
  - fusion
  - console
  - qq
"""

# 只放"运行时真的会被读到"的字段（_pref 那条链），填了没人读的框一律不放
CONF_SCHEMA = {
    "mai_root": {
        "description": "麦麦（MaiBot）本体目录（其下必须有 src/）",
        "type": "string",
        "value": "",
        "hint": "留空则用 data/plugin_data/astrmai/MaiBot。本插件不打包麦麦：请 git clone -b v1.3.18 "
        "https://github.com/MaiM-with-u/MaiBot 到该目录，或填你已有副本的绝对路径。",
    },
    "account_id": {
        "description": "机器人自己的 QQ 号",
        "type": "string",
        "value": "",
        "hint": "用于 @ 与引用判定；留空就只能靠群聊自然对话那条路。",
    },
    "allowed_users": {
        "description": "私聊硬白名单（逗号分隔的 QQ 号）",
        "type": "string",
        "value": "",
        "hint": "非空时名单外的人发消息一律沉默；同时它也是 /mai 的管理员名单（两者都空 = /mai 不放行）。",
    },
    "groups": {
        "description": "群号白名单（逗号分隔）",
        "type": "string",
        "value": "",
        "hint": "留空且总开关开了 = 所有群都放行。",
    },
    "allow_groups": {
        "description": "群聊总开关",
        "type": "bool",
        "value": False,
        "hint": "默认关：群消息既不转发给麦麦也不抑制 AstrBot，避免你还没配置就在群里说话。",
    },
    "allow_private": {
        "description": "私聊总开关",
        "type": "bool",
        "value": True,
        "hint": "关掉后私聊一律沉默。",
    },
    "platform": {
        "description": "平台标识",
        "type": "string",
        "value": "qq",
        "hint": "目前只做了 QQ（aiocqhttp）这条路。",
    },
    "panel_enable": {
        "description": "开启 astrmai 控制台",
        "type": "bool",
        "value": False,
        "hint": "控制台是独立 Web 服务（默认只听 127.0.0.1:18124），初始口令 astrmai-init、登录即强制改密。",
    },
    "panel_host": {
        "description": "控制台监听地址",
        "type": "string",
        "value": "127.0.0.1",
        "hint": "非回环地址必须同时给 TLS 证书，否则面板拒绝启动。",
    },
    "panel_port": {
        "description": "控制台端口",
        "type": "int",
        "value": 18124,
        "hint": "被占用时面板会报错，但不会拖垮两个脑子。",
    },
}

README = """# astrmai（AstrBot 插件版）

把 **[MaiBot 麦麦](https://github.com/MaiM-with-u/MaiBot)** 的群聊人格引擎嵌进 **[AstrBot](https://github.com/AstrBotDevs/AstrBot)** 的同一个进程：
一个 asyncio 事件循环、一条 NapCat 反向 WebSocket、逐条消息裁决归属。同一条消息不会被答两遍。

![astrmai](assets/astrmai-icon.svg)

## 装之前要知道的三件事

1. **本插件不打包麦麦本体**。官方市场 zip 上限 16MB，而麦麦是独立的 GPL 项目 —— 请自己 `git clone -b v1.3.18 https://github.com/MaiM-with-u/MaiBot`，
   放到 `data/plugin_data/astrmai/MaiBot`，或在插件配置 `mai_root` 里填你已有副本的路径。
2. **本插件不带 `requirements.txt`，也不会替你 pip**。AstrBot 加载插件时会把 requirements 静默装进你的 Python 环境，
   而麦麦要的是 faiss / sentence-transformers 这一类重包 —— 装坏你环境这件事我们不代做。
   缺依赖时 astrmai 会明确告诉你缺什么、装哪几个，你自己决定。
3. **合并作品按 AGPL-3.0-or-later 分发**（GPL-3.0 的麦麦可以与 AGPL 的 AstrBot 合并，反向不行）。
   完整对应源码在本仓库；如果你改了再分发，请连改动一起给源码。

## 装完之后

```
# 1) 放麦麦
git clone -b v1.3.18 https://github.com/MaiM-with-u/MaiBot <AstrBot>/data/plugin_data/astrmai/MaiBot
# 2) 在麦麦目录里装它自己的依赖（建议先备份你的 venv，或用 AstrBot 的 venv 并接受风险）
cd <上面那个 MaiBot 目录> && pip install -r requirements.txt
# 3) 在 AstrBot 面板里把本插件的 mai_root 指过去（留空就是第 1 步那个路径），打开 panel_enable，重启 AstrBot
```

控制台：`http://127.0.0.1:18124/astrmai/`，初始口令 `astrmai-init`，**登录即强制改密**，该口令只接受服务器本机登录。
QQ 里：`/mai status`、`/mai panel`、`/mai 许可证`、`/mai market astrbot <关键词>`、`/mai install astrbot <id>`。
`/mai` 只对管理员开放（管理员 = `allowed_users`，两者都空时一律拒绝）。

## 它会往你机器上写什么

| 东西 | 位置 |
|---|---|
| 控制台凭据（只有 scrypt 哈希，无明文） | `data/plugin_data/astrmai/credentials.json` |
| 面板改过的闸门/自主性设置 | `data/plugin_data/astrmai/settings.json` |
| 市场目录缓存（30 分钟 TTL） | `data/plugin_data/astrmai/cache/` |
| 你放的麦麦本体与其运行数据 | `data/plugin_data/astrmai/MaiBot/` |

状态一律放在 `plugin_data` 而不是插件目录里：**AstrBot 更新插件会把插件目录整个删掉重建**。

## 出网清单

| 目标 | 什么时候 | 能不能关 |
|---|---|---|
| 你的模型端点 | 每次回复 | 你配的 |
| NapCat / QQ 服务器 | 收发消息 | 你配的 |
| AstrBot 官方插件目录（api.soulter.top → cloud.astrbot.app、codeload.github.com） | 你主动搜/装市场时 | `ASTRMAI_MARKET_NETWORK=0`（两条链路一起关，缓存也不能当后门） |
| 麦麦官方市场 `hyybuth.xyz:10059` | 同上 | 同上（该主机与麦麦遥测同域） |
| 上游遥测 | **从不**（默认掐断） | 需要显式 `ASTRMAI_TELEMETRY=on` 才放行 |

## 已知边界

- 单管理员控制台，没有多用户/权限分级；登录进去能看见完整对话历史（只读，本插件没做删除入口）+ 200 条裁决摘要 + 400 条日志环。
- 管不住麦麦的自主发言循环（她 own 的主动聊天），裁决层只管"单条消息谁答"。
- 端到端取证用本地桩模型；接真模型请自己先看几轮。
"""

GITIGNORE = """__pycache__/
*.pyc
astrmai_state/
data/
e2e/
secrets/
*.log
"""


def copy_bridge(dst: pathlib.Path) -> int:
    src = WS / "astrmai"
    n = 0
    for path in sorted(src.rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        target = dst / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        n += 1
    return n


def build() -> pathlib.Path:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    shutil.copy2(WS / "astrmai_plugin" / "main.py", OUT / "main.py")
    files = copy_bridge(OUT / "astrmai")
    (OUT / "metadata.yaml").write_text(METADATA, encoding="utf-8")
    (OUT / "_conf_schema.json").write_text(json.dumps(CONF_SCHEMA, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "README.md").write_text(README, encoding="utf-8")
    (OUT / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    shutil.copy2(WS / "LICENSE", OUT / "LICENSE")
    (OUT / "assets").mkdir()
    shutil.copy2(WS / "assets" / "astrmai-icon.svg", OUT / "assets" / "astrmai-icon.svg")
    print(f"构建 {OUT}：main.py + astrmai/（{files} 文件）+ 元数据/配置 schema/README/LICENSE/logo")
    return OUT


def build_maibot_plugin() -> pathlib.Path:
    """把麦麦侧的 astrbot_bridge 独立成一个仓库树（它不依赖 astrmai 代码，只依赖控制台端点）。"""
    src = WS / "maibot_plugins" / "astrbot_bridge"
    if not (src / "plugin.py").exists():
        sys.exit(f"没有 {src}")
    if MAI_OUT.exists():
        shutil.rmtree(MAI_OUT)
    shutil.copytree(src, MAI_OUT, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(WS / "LICENSE", MAI_OUT / "LICENSE")
    (MAI_OUT / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    print(f"构建 {MAI_OUT}：{sum(1 for p in MAI_OUT.rglob('*') if p.is_file())} 个文件")
    return MAI_OUT


def gates(target: pathlib.Path) -> list[str]:
    bad: list[str] = []
    paths = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
    # 不替用户装包：这条是我们的硬政策，不是偏好
    if any(p == "requirements.txt" or p.endswith("/requirements.txt") for p in paths):
        bad.append("包里出现了 requirements.txt —— AstrBot 会不问就 pip 进用户环境")
    for forbidden in ("MaiBot-main", "vendor", "astrmai_state", "secrets", "e2e", "data"):
        if any(p == forbidden or p.startswith(f"{forbidden}/") for p in paths):
            bad.append(f"包里混进了不该随包走的东西: {forbidden}/")
    for required in ("main.py", "metadata.yaml", "_conf_schema.json", "astrmai/config.py", "astrmai/panel_static/app.js", "LICENSE"):
        if required not in paths:
            bad.append(f"缺 {required}")
    meta = (target / "metadata.yaml").read_text(encoding="utf-8")
    for key in ("name:", "desc:", "version:", "author:", "repo:"):
        if key not in meta:
            bad.append(f"metadata.yaml 缺 {key}")
    try:
        schema = json.loads((target / "_conf_schema.json").read_text(encoding="utf-8"))
    except ValueError as exc:
        bad.append(f"_conf_schema.json 不是合法 JSON: {exc}")
        schema = {}
    for key, spec in schema.items():
        if not isinstance(spec, dict) or "description" not in spec or "value" not in spec:
            bad.append(f"配置项 {key} 缺 description/value")
    size = sum((target / p).stat().st_size for p in paths)
    print(f"包内 {len(paths)} 个文件，未压缩 {size/1024:.0f} KB")
    if size > LIMIT:
        bad.append(f"超过官方市场 16MB 上限：{size/1024/1024:.1f} MB")
    return bad


def maibot_gates(target: pathlib.Path) -> list[str]:
    bad: list[str] = []
    names = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
    for required in ("plugin.py", "_manifest.json", "config.toml", "README.md", "LICENSE", "assets/icon.svg"):
        if required not in names:
            bad.append(f"缺 {required}")
    if any("astrmai_state" in n or n.startswith(("secrets", "e2e", "data")) for n in names):
        bad.append("包里混进了本机状态目录")
    size = sum((target / n).stat().st_size for n in names)
    print(f"麦麦插件包 {len(names)} 个文件，未压缩 {size/1024:.0f} KB")
    if size > LIMIT:
        bad.append(f"体积异常（{size/1024/1024:.1f} MB）")
    return bad


def zip_up(target: pathlib.Path, archive: pathlib.Path, arc: str) -> pathlib.Path:
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(target.rglob("*")):
            if path.is_file():
                zf.write(path, arcname=pathlib.Path(arc) / path.relative_to(target))
    print(f"打包 {archive.name}（{archive.stat().st_size/1024:.0f} KB，上限 16 MB）")
    return archive


def main(argv: list[str]) -> int:
    make_zip = "--no-zip" not in argv
    packages = [
        ("AstrBot 市场插件（瘦宿主）", build(), gates, ZIP, "astrmai"),
        ("麦麦侧桥插件（astrbot_bridge）", build_maibot_plugin(), maibot_gates, MAI_ZIP, "astrbot_bridge"),
    ]
    failed = False
    for label, target, gate, archive, arc in packages:
        print(f"\n{label} → {target.name}")
        problems = gate(target)
        for line in problems:
            print("  FAIL", line)
            failed = True
        if not problems:
            print("  闸门通过：必备文件齐、不替用户 pip、不含上游与本机状态、体积在上限内")
        code = subprocess.run([sys.executable, str(WS / "tools" / "privacy_scan.py"), str(target)]).returncode
        if code != 0:
            print("  隐私扫描未通过，拒绝产出。")
            failed = True
            continue
        if make_zip and not problems:
            zip_up(target, archive, arc)
    if failed:
        return 1
    print("\n下一步（两个都开新仓库）：")
    print("  .venv\\Scripts\\python.exe tools\\publish_github.py --tree astrbot-plugin-astrmai --name astrbot-plugin-astrmai")
    print("  .venv\\Scripts\\python.exe tools\\publish_github.py --tree maibot-plugin-astrbot-bridge --name maibot-plugin-astrbot-bridge")
    print("  AstrBot 那个再提交 https://cloud.astrbot.app/publish（要 AstrBot Cloud 账号）；")
    print("  麦麦那个的上架入口还没查（要的话我去查一遍）。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
