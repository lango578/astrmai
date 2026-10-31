"""许可证识别取证：全部离线，用真的许可证正文与合成包，不碰网络。

要守住的不是"能认出 MIT"，而是**认不出时必须显式 unknown**：
许可证标识在分发时是依据，猜错比标"不知道"更糟。
"""

import io
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))
sys.path.insert(0, str(WS / "vendor" / "AstrBot-master"))
os.environ["ASTRMAI_WORKSPACE"] = str(WS)

from astrmai import licensing  # noqa: E402
from astrmai.market import Market  # noqa: E402

ROOT = WS / "probe" / "licensing"

MIT = """MIT License

Copyright (c) 2026 Someone

Permission is hereby granted, free of charge, to any person obtaining a copy of this software
and associated documentation files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software.
"""

GPL3 = """                    GNU GENERAL PUBLIC LICENSE
                       Version 3, 29 June 2007

 Copyright (C) 2007 Free Software Foundation, Inc.

 Everyone is permitted to copy and distribute verbatim copies.
 You may convey a covered work under this License, or (at your option) any later version.
"""

GPL2_ONLY = """                    GNU GENERAL PUBLIC LICENSE
                       Version 2, June 1991

 Preamble. The licenses for most software are designed to take away your freedom.
"""

AGPL3 = """                    GNU AFFERO GENERAL PUBLIC LICENSE
                       Version 3, 19 November 2007

 Copyright (C) 2007 Free Software Foundation, Inc.
 You may convey a covered work under this License, or (at your option) any later version.
"""

LGPL21 = """                  GNU LESSER GENERAL PUBLIC LICENSE
                       Version 2.1, February 1999

 ... either version 2.1 of the License, or (at your option) any later version.
"""

APACHE2 = """                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/
   Licensed under the Apache License, Version 2.0 (the "License");
"""

BSD3 = """Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
1. Redistributions of source code must retain the above copyright notice.
3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products.
"""

BSD2 = """Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
1. Redistributions of source code must retain the above copyright notice.
2. Redistributions in binary form must reproduce the above copyright notice.
"""

MPL2 = """Mozilla Public License Version 2.0
==================================

1. Definitions.
"""

GIBBERISH = """版权所有 (C) 2026 某某公司。保留一切权利。
本软件仅供内部使用，不得逆向、不得再分发。
"""

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail else ""), flush=True)
    if not cond:
        FAILS.append(name)


def classify_cases() -> None:
    cases = [
        (MIT, "MIT"),
        (GPL3, "GPL-3.0-or-later"),
        (GPL2_ONLY, "GPL-2.0-only"),
        (AGPL3, "AGPL-3.0-or-later"),
        (LGPL21, "LGPL-2.1-or-later"),
        (APACHE2, "Apache-2.0"),
        (BSD3, "BSD-3-Clause"),
        (BSD2, "BSD-2-Clause"),
        (MPL2, "MPL-2.0"),
        ("This is free and unencumbered software released into the public domain.\nAnyone is free to copy...", "Unlicense"),
        ("CC0 1.0 Universal\nCREATIVE COMMONS CORPORATION IS NOT A LAW FIRM...", "CC0-1.0"),
        ("ISC License\n\nPermission to use, copy, or modify this software is hereby granted.", "ISC"),
    ]
    for text, want in cases:
        got, basis, conf = licensing.classify(text)
        check(f"识别 {want}", got == want, f"实际 {got}｜{basis}｜{conf}")

    got, basis, conf = licensing.classify(GIBBERISH)
    check("专有/EULA 文本绝不猜成开源许可", got == "unknown" and conf == "unknown", f"{got}｜{basis}")
    check(
        "BSD 真文本通篇不写 BSD 这个词也能判出条款数",
        "bsd" not in BSD3.lower() and licensing.classify(BSD3)[0] == "BSD-3-Clause" and licensing.classify(BSD2)[0] == "BSD-2-Clause",
        f"{licensing.classify(BSD3)[0]} / {licensing.classify(BSD2)[0]}"
    )
    got, basis, _c = licensing.classify("")
    check("空文本 → unknown 并说明原因", got == "unknown" and "没有可分析" in basis, basis)
    got, basis, _c = licensing.classify("Some random notes about a chatbot plugin\nnothing license-like here")
    check("无抬头正文 → unknown 且附首行证据", got == "unknown" and "首行" in basis, basis[:90])
    check(
        "GPL 的「any later version」判 -or-later、没有就判 -only",
        licensing.classify(GPL3)[0].endswith("-or-later") and licensing.classify(GPL2_ONLY)[0].endswith("-only"),
        ""
    )


def priority_cases(tmp: Path) -> None:
    files = {"license": MIT, "readme.md": "# demo\n\nLicense: WTFPL\n"}
    verdict = licensing.from_files(files)
    check("LICENSE 正文优先于 README 声明", verdict["license"] == "MIT" and "LICENSE" in verdict["basis"], str(verdict))

    verdict = licensing.from_files({"metadata.yaml": "name: demo\nlicense: MIT  # 中文注释\n", "readme.md": "x"})
    check("没有 LICENSE 时读 metadata.yaml 的 license 字段（并剥掉行内注释）", verdict["license"] == "MIT", str(verdict))

    verdict = licensing.from_files({"readme.md": "## 许可证\n\nLicense: MIT\n"})
    check("只有 README 声明时按 medium 置信记录", verdict["license"] == "MIT" and verdict["confidence"] == "medium", str(verdict))

    verdict = licensing.from_files({"license": GIBBERISH})
    check("有 LICENSE 但认不出 → unknown，不硬凑", verdict["license"] == "unknown" and verdict["confidence"] == "unknown", str(verdict)[:140])

    verdict = licensing.from_files({})
    check("什么证据都没有 → unknown 并说清缺什么", verdict["license"] == "unknown" and "LICENSE" in verdict["basis"], str(verdict))


def make_zip(files: dict[str, str], root: str = "demo-plugin-main/") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(root + name, text)
        zf.writestr(root + "subdir/LICENSE", "嵌套目录里的 LICENSE 不该被当证据")
    return buf.getvalue()


def market_install_records(tmp: Path) -> None:
    m = Market(
        WS,
        astrbot_root=tmp / "ab",
        maibot_root=tmp / "mai",
        native_root=tmp / "native",
    )
    raw = make_zip({"LICENSE": MIT, "metadata.yaml": "name: demo_ab\ndesc: d\nversion: 1.0.0\nauthor: a\n"})
    out = m._place(raw, "demo-plugin-main/", tmp / "ab" / "data" / "plugins" / "demo_ab", source="astrbot", where="synthetic", entry={"repo": "https://github.com/x/y"})
    check("合成包安装成功", out.get("ok") is True, str(out)[:120])
    check("安装即记录许可证", out.get("license") == "MIT" and out.get("license_confidence") == "high", str(out)[:160])
    record = json.loads((tmp / "ab" / "data" / "plugins" / "demo_ab" / licensing.RECORD_FILE).read_text(encoding="utf-8"))
    check("记录带依据与包来源", record["license"] == "MIT" and "LICENSE" in record["basis"] and record["package"].endswith("x/y"), str(record)[:160])
    check("嵌套目录里的同名 LICENSE 不污染判定", "MIT" == record["license"], str(record)[:80])

    raw_none = make_zip({"metadata.yaml": "name: mystery\ndesc: d\nversion: 1.0.0\nauthor: a\n", "main.py": "print(1)"})
    out2 = m._place(raw_none, "demo-plugin-main/", tmp / "ab" / "data" / "plugins" / "mystery", source="astrbot", where="synthetic", entry={"repo": "https://github.com/x/mystery"})
    check("装一个没有任何许可证据的插件 → 记 unknown", out2.get("license") == "unknown", str(out2)[:140])

    rows = m.licenses()
    by_name = {r["component"]: r for r in rows}
    check("清单里两项都在", "demo_ab" in by_name and "mystery" in by_name, str(sorted(by_name))[:160])
    check("unknown 项被显式标出来", by_name["mystery"]["undetermined"] is True and by_name["demo_ab"]["undetermined"] is False, str(by_name["mystery"]))
    engines = {r["component"]: r["license"] for r in rows if r["source"] in ("engine", "astrmai")}
    check(
        "引擎与本融合层的许可也在清单里",
        engines.get("AstrBot", "").startswith("AGPL") and any(v.startswith("GPL") for v in engines.values()),
        str(engines)
    )


def command_listing(tmp: Path) -> None:
    from types import SimpleNamespace

    class Ctx(SimpleNamespace):
        def get_llm_tools(self):
            return []

    import importlib

    plugin_mod = importlib.import_module("astrmai_plugin.main")
    star = plugin_mod.Main(Ctx(), None)
    # 让 _market() 指到刚才合成的安装目录，不碰真插件目录
    star._market_obj = Market(WS, astrbot_root=tmp / "ab", maibot_root=tmp / "mai", native_root=tmp / "native")
    text = star._license_text()
    check("命令输出含表头与每一项", "已装组件许可证清单" in text and "demo_ab" in text and "mystery" in text, text[:120].replace("\n", " / "))
    check("命令明确统计并点名 unknown", "无法确定" in text and "unknown" in text, [l for l in text.splitlines() if "合计" in l][0] if "合计" in text else "")
    check("命令给出分发口径", "AGPL-3.0-or-later" in text, "")


def panel_endpoint(tmp: Path) -> None:
    import asyncio

    from astrmai.panel import AstrmaiPanel
    from astrmai.panel_data import Ledger, LogRing

    star = SimpleNamespace(
        _market=lambda: Market(WS, astrbot_root=tmp / "ab", maibot_root=tmp / "mai", native_root=tmp / "native"),
        host=None,
        booted=False,
        boot_error="",
        forwarded=0,
        sent_total=0,
        _umo_by_target={},
        _event_by_target={},
        federation={},
        context=None,
        native=None,
    )

    async def run() -> None:
        import socket

        import httpx

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        import os

        os.environ["ASTRMAI_PANEL_PORT"] = str(port)
        os.environ["ASTRMAI_PANEL_HOST"] = "127.0.0.1"
        # 面板现在一律要凭据：给一份只用于取证的 token
        token = "astrmai-licensing-token-4Kd9"  # privacy-scan:allow 靶场合成口令：只在 127.0.0.1 的测试实例里用，不是任何真实凭据
        os.environ["ASTRMAI_PANEL_TOKEN"] = token
        os.environ["ASTRMAI_PANEL_PASSWORD"] = "astrmai-licensing-Zx9-4mqd"
        os.environ["ASTRMAI_STATE_DIR"] = str(WS / "e2e" / "licensing_state")
        panel = AstrmaiPanel(lambda: star, ledger=Ledger(), logs=LogRing())
        await panel.start()
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=15, headers={"X-Astrmai-Token": token}) as c:
                data = (await c.get("/astrmai/api/licenses")).json()
                check("面板许可证端点返回清单", data.get("ok") is True and data.get("total", 0) >= 5, str({k: data[k] for k in ('ok', 'total', 'unknown_count') if k in data}))
                check("面板也显式报 unknown 数量", data.get("unknown_count", 0) >= 1, str(data.get("unknown")))
                check("面板给分发口径", "AGPL" in str(data.get("conveyance")), str(data.get("conveyance"))[:80])
        finally:
            await panel.stop()

    asyncio.run(run())


def main() -> int:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True)
    try:
        classify_cases()
        priority_cases(ROOT / "prio")
        market_install_records(ROOT / "mkt")
        command_listing(ROOT / "mkt")
        panel_endpoint(ROOT / "mkt")
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)

    print("\n结果:", "全部通过" if not FAILS else f"{len(FAILS)} 项失败 {FAILS}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
