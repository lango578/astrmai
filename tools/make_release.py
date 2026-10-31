r"""把 dist/astrmai-0.1.0.zip 挂成 GitHub Release，并补仓库 topics/description。

和 publish_github.py 共用 secrets/github_credentials.json。网络被本机代理挡住时会给出可读诊断，
不吐 traceback（这台机的 GitHub 域名走 127.0.0.1 的本地代理，代理切拦截模式后 Python 不认它的证书）。

    python tools/make_release.py --check     # 只看通不通与计划
    python tools/make_release.py            # 建 Release + 传 zip + 设 topics
"""

import argparse
import json
import os
import pathlib
import socket
import sys

import httpx

WS = pathlib.Path(__file__).resolve().parents[1]
ZIP = WS / "dist" / "astrmai-0.1.0.zip"
API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
TAG = "v0.1.0"
TOPICS = ["astrbot", "maibot", "onebot-v11", "napcat", "qq-bot", "agpl-3-0", "asyncio"]


def token() -> str:
    path = WS / "secrets" / "github_credentials.json"
    if not path.exists():
        sys.exit("没有 secrets/github_credentials.json：先跑 tools\\get_github_token.cmd")
    value = json.loads(path.read_text(encoding="utf-8")).get("token")
    if not value:
        sys.exit("secrets 里没有 token")
    return str(value)


def diagnose(exc: Exception) -> str:
    """把"代理在中间做拦截"这种情况讲清楚，别让人对着 SSL 报错猜。"""

    text = f"{type(exc).__name__}: {str(exc)[:120]}"
    try:
        ip = socket.gethostbyname("api.github.com")
    except OSError:
        ip = "解析失败"
    if "CERTIFICATE_VERIFY_FAILED" in text:
        return f"{text}｜api.github.com 解析到 {ip}。若这是 127.0.0.1（本机代理在做中间人），把代理切回直连/隧道模式再试"
    if ip == "127.0.0.1":
        return f"{text}｜api.github.com 解析到本机代理，可能是代理没在跑或端口没监听"
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只验通不通与计划，不写")
    ap.add_argument("--tag", default=TAG)
    ap.add_argument("--name", default="astrmai", help="仓库名（归属由 token 的账号决定）")
    args = ap.parse_args()

    tok = token()
    headers = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
    try:
        zen = httpx.get(f"{API}/zen", headers=headers, timeout=20)
        print("api.github.com 可达:", zen.status_code)
        who = httpx.get(f"{API}/user", headers=headers, timeout=20).json()
        slug = f"{who.get('login')}/{args.name}"
        print(f"仓库: {slug}")
        repo = httpx.get(f"{API}/repos/{slug}", headers=headers, timeout=20).json()
        print(f"当前可见性: {'私有' if repo.get('private') else '公开'}")
        if not ZIP.exists():
            print(f"zip 不存在: {ZIP}（先跑 tools/pack_release.py）")
            return 1
        size = ZIP.stat().st_size
        print(f"计划: tag {args.tag}，附 {ZIP.name}（{size/1024:.0f} KB），topics {len(TOPICS)} 个")
        try:
            httpx.get(UPLOADS, timeout=15)
            print("uploads.github.com 可达")
        except Exception as exc:  # noqa: BLE001
            print("uploads.github.com:", diagnose(exc))
        if args.check:
            print("--check：没有写任何东西")
            return 0

        body = {
            "tag_name": args.tag,
            "name": f"astrmai {args.tag}",
            "body": (
                "AstrBot 与麦麦（MaiBot）强行融合：一个进程、一个 asyncio 循环、一条 NapCat 反向 WS。\n\n"
                "**包里是 astrmai 自己的代码与文档，不含两个上游**——按 docs/DEPLOY.md 第 2 节自行获取 AstrBot 4.29.3 与 MaiBot v1.3.18。\n\n"
                "- 融合桥接 + 逐条裁决（同一条消息不被答两遍）\n"
                "- 双向工具联邦 `ab_* / mai_* / abcmd_* / nm_*`\n"
                "- 两家官方市场 + 许可证取证（判不出标 unknown）\n"
                "- 自建控制台：负载首屏、AstrBot 功能面（provider/人格/对话/定时）、scrypt 凭据 + 强制改密\n"
                "- 15 项离线测试（含端到端与攻击面取证）\n\n"
                "AGPL-3.0-or-later。合并作品按 AGPL 分发，源码就在仓库里。"
            ),
            "draft": False,
            "prerelease": False,
        }
        created = httpx.post(f"{API}/repos/{slug}/releases", headers=headers, json=body, timeout=60)
        if created.status_code == 422 and "Tag name already exists" in created.text:
            rel = httpx.get(f"{API}/repos/{slug}/releases/tags/{args.tag}", headers=headers, timeout=30).json()
            print("Release 已存在，复用:", rel["html_url"])
        elif created.status_code in (200, 201):
            rel = created.json()
            print("Release 已建:", rel["html_url"])
        else:
            print("建 Release 失败:", created.status_code, created.text[:200])
            return 1

        with ZIP.open("rb") as fh:
            up = httpx.post(
                f"{UPLOADS}/repos/{slug}/releases/{rel['id']}/assets?name={ZIP.name}",
                headers={**headers, "Content-Type": "application/zip"},
                content=fh.read(),
                timeout=180,
            )
        print("上传 zip:", up.status_code, "|", (up.json().get("browser_download_url") or up.text[:160]))

        top = httpx.put(
            f"{API}/repos/{slug}/topics",
            headers={**headers, "Accept": "application/vnd.github.mercy-preview+json"},
            json={"names": TOPICS},
            timeout=60,
        )
        print("设 topics:", top.status_code, top.json().get("names") if top.status_code == 200 else top.text[:160])
        return 0
    except Exception as exc:  # noqa: BLE001
        print("被网络挡住了:", diagnose(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
