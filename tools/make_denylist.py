r"""生成 tools/privacy_denylist.txt：把"这台机上真实存在的身份值"集合起来，交给隐私扫描器逐字节比对。

规则文件里只写值、不写解释；脚本本身**不含任何真值字面量**（全部从本机已有文件与环境变量读出来），
所以它可以安全地留在仓库里。产物文件被 .gitignore 与打包白名单双重排除。

    python tools/make_denylist.py            # 生成/更新，只报条数，不打印值
    python tools/make_denylist.py --show     # 打印每条的形状（长度 + 前 3 字符），仍然不给全文
"""

import argparse
import getpass
import json
import os
import re
import sys
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
OUT = WS / "tools" / "privacy_denylist.txt"
NUM_BYTES = re.compile(rb"(?<!\d)\d{10,11}(?!\d)")
SECRET_KEY = re.compile(r"(?i)key|token|secret|base_url|url|model")
IDENTITY_KEY = re.compile(r"(?i)account|user|self|qq|admin|target|group")
TIME_KEY = re.compile(r"(?i)time|updated_at|created|_at$|ts$|stamp")

SOURCES = (
    WS / "secrets" / "model_credentials.json",
    WS / "secrets" / "github_credentials.json",
    WS / "e2e" / "real" / "astrmai_status.json",
    WS / "astrmai_state" / "settings.json",
)


def harvest() -> set[str]:
    values: set[str] = set()

    for path in SOURCES:
        if not path.exists():
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            stack: list = [(data, "")]
            while stack:
                node, key_name = stack.pop()
                if isinstance(node, dict):
                    for key, value in node.items():
                        if isinstance(value, (dict, list)):
                            stack.append((value, str(key)))
                        elif isinstance(value, int) and IDENTITY_KEY.search(str(key)):
                            # QQ 号在本机文件里常是裸整数（OneBot 的 user_id 就是 int），不转字符串就漏收
                            digits = str(value)
                            if NUM_BYTES.search(digits.encode()):
                                values.add(digits)
                        elif isinstance(value, str) and len(value.strip()) >= 8:
                            kk = str(key)
                            if SECRET_KEY.search(kk):
                                values.add(value.strip())
                            elif IDENTITY_KEY.search(kk) and NUM_BYTES.search(value.encode("utf-8", "replace")):
                                # 只有挂在身份字段下的 10–11 位数字才算 QQ 号，
                                # 整文件刮数字会把状态文件里的时间戳也当身份收进来（误报源）
                                values.add(value.strip())
                            if not TIME_KEY.search(kk):
                                # QQ 号也常藏在复合串里（driver 字段就是 "local.astrmai.<QQ>"），
                                # 所以从任意非时间字段里抠出 10–11 位数字；17xx/18xx 开头的是 epoch，跳过
                                for hit in NUM_BYTES.findall(value.encode("utf-8", "replace")):
                                    digits = hit.decode()
                                    if digits[:2] in ("17", "18"):
                                        continue
                                    values.add(digits)
                elif isinstance(node, list):
                    for item in node:
                        if isinstance(item, (dict, list)):
                            stack.append((item, key_name))
                        elif isinstance(item, int) and IDENTITY_KEY.search(key_name) and NUM_BYTES.search(str(item).encode()):
                            values.add(str(item))

    # 本机路径与主机/账号名：这些是最容易跟着日志混进文档的东西
    home = os.path.expanduser("~")
    values.add(home)
    values.add(f"C:\\Users\\{getpass.getuser()}")
    values.add(getpass.getuser())
    values.add(os.environ.get("COMPUTERNAME", ""))
    # 启动参数里的白名单号常常只活在环境变量里（run_real.py --allow 落到 ASTRMAI_ALLOWED_USERS）
    for name, value in os.environ.items():
        if name.startswith("ASTRMAI_") and re.search(r"(?i)USER|ADMIN|GROUP|ACCOUNT", name):
            for piece in str(value).split(","):
                piece = piece.strip()
                if NUM_BYTES.fullmatch(piece.encode()) and piece[:2] not in ("17", "18"):
                    values.add(piece)
    # 门槛 8：短值（"admin" 这种通用词）当"真值"比对只会误报，反而让人开始忽略 FAIL
    return {v for v in values if v and len(v) >= 8}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="只打印形状（长度 + 前 3 字符）")
    args = ap.parse_args()

    values = sorted(harvest(), key=len, reverse=True)
    if not values:
        print("没收集到任何东西：本机文件都读不到？")
        return 1
    if args.show:
        print(f"共 {len(values)} 条，按形状列出（不给全文）：")
        for v in values:
            print(f"  长度 {len(v):>3}｜前缀 {v[:3]}…")
        return 0

    body = "\n".join(values)
    OUT.write_text(
        "# astrmai 隐私扫描器的真实值清单（本文件被 .gitignore 与打包白名单双重排除，绝不进发布树、绝不进 git）\n"
        "# 生成：python tools/make_denylist.py\n"
        f"{body}\n",
        encoding="utf-8",
    )
    print(f"已写 {OUT.name}：{len(values)} 条真值（本终端全程未打印它们）")
    print("现在 python tools/pack_release.py 会对发布树逐字节比对这份清单，命中即 FAIL 且没有豁免通道")
    return 0


if __name__ == "__main__":
    sys.exit(main())
