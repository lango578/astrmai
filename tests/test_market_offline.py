"""市场里不需要联网的那部分回归：解析、过滤、越界保护。"""

import os
import sys
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))
# 加载政策要复用 AstrBot 自己的依赖判定，所以这里也得能 import astrbot
sys.path.insert(0, str(WS / "vendor" / "AstrBot-master"))

from astrmai.market import Market, _split_repo  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail else ""), flush=True)
    if not cond:
        FAILS.append(name)


META = """name: astrbot_plugin_meihua # 插件唯一识别名，以 astrbot_plugin_ 前缀开头
desc: 每天固定推送梅花易数卦象结果 # 简要介绍插件功能，建议 300 字以内
ver: v1.0.0
version: 1.0.0 # 版本号
author: g985892345 # 作者名
"""

m = Market(WS, astrbot_root=WS / "probe" / "x1", maibot_root=WS / "probe" / "x2", native_root=WS / "probe" / "x3")

check("metadata 的 name 剥掉行内注释", m._meta_field(META, "name") == "astrbot_plugin_meihua", repr(m._meta_field(META, "name")))
check("metadata 的 author 干净", m._meta_field(META, "author") == "g985892345", repr(m._meta_field(META, "author")))
check("metadata 的 version 存在", m._meta_field(META, "version") == "1.0.0", repr(m._meta_field(META, "version")))
check("缺失字段返回空串", m._meta_field(META, "nonexistent") == "")

catalog = {
    "$meta": {"schema_version": 1, "name": "AstrBot Official Plugin Market"},
    "a/b": {"name": "b", "desc": "x", "author": "a", "version": "1", "repo": "https://github.com/a/b"},
    "c/d": {"name": "d", "desc": "y", "author": "c", "version": "1", "repo": "https://gitee.com/c/d"},
    "e/f": {"name": "f", "author": "e"},
}
entries = Market._parse_ab_json(catalog)
ids = [e["id"] for e in entries]
check("目录解析跳过保留键 $meta", "$meta" not in ids, str(ids))
check("目录解析只保留可下载仓库", ids == ["a/b"], str(ids))

check("repo 拆分支持 .git 结尾", _split_repo("https://github.com/a/b.git") == ("a", "b"), str(_split_repo("https://github.com/a/b.git")))
check("repo 拆分拒绝非 GitHub", _split_repo("https://example.com/a/b") is None)

# 越界保护：不允许删市场目录之外的东西
bad = m.uninstall("astrbot", "..\\..\\MaiBot-main")
check("卸载拒绝越界路径", not bad.get("ok"), str(bad)[:110])
bad2 = m.uninstall("astrbot", "not_exists_dir")
check("卸载不存在目录不误删", not bad2.get("ok"), str(bad2)[:110])

# ---------------- 复检 H2：包内路径越界（zip slip）必须整包拒绝 ----------------
import io as _io
import tempfile as _tempfile
import zipfile as _zip  # noqa: E402

with _tempfile.TemporaryDirectory() as td:
    root = Path(td) / "plugins"
    root.mkdir()
    buf = _io.BytesIO()
    with _zip.ZipFile(buf, "w") as zf:
        zf.writestr("evil_plugin/main.py", "print('ok')")
        zf.writestr("evil_plugin/../../../escape.txt", "escaped")
    res = m._place(buf.getvalue(), "evil_plugin/", root / "evil_plugin", source="astrbot", where="probe", entry={"id": "evil"})
    check("包内 ../ 越界被整包拒绝", not res.get("ok") and "越界" in str(res.get("reason", "")), str(res)[:130])
    escaped = [p for p in Path(td).parent.glob("escape.txt")] + [p for p in Path(td).glob("escape.txt")]
    check("越界条目真的没落盘", not escaped and not (root / "evil_plugin").exists(), str(escaped))
    check("被拒的包不在暂存区外面留下碎片", not list(root.glob(".astrmai-install-*")), str(list(root.iterdir())))

    clean = _io.BytesIO()
    with _zip.ZipFile(clean, "w") as zf:
        zf.writestr("good_plugin/main.py", "print('ok')")
        zf.writestr("good_plugin/metadata.yaml", "name: good_plugin\n")
    res2 = m._place(clean.getvalue(), "good_plugin/", root / "good_plugin", source="astrbot", where="probe", entry={"id": "good"})
    check("正常包照样落地（别把修复做成拒绝一切）", res2.get("ok") is True and (root / "good_plugin" / "main.py").exists(), str(res2)[:120])

    backslash = _io.BytesIO()
    with _zip.ZipFile(backslash, "w") as zf:
        zf.writestr("bs_plugin/..\\..\\escape2.txt", "x")
    res3 = m._place(backslash.getvalue(), "bs_plugin/", root / "bs_plugin", source="astrbot", where="probe", entry={"id": "bs"})
    check("反斜杠写法的越界条目同样拒绝", not res3.get("ok"), str(res3)[:120])

# ---------------- 市场页要能标"已安装"，翻页不能越界 ----------------
import asyncio  # noqa: E402
import json as _json  # noqa: E402
import shutil as _shutil  # noqa: E402
import tempfile as _tempfile  # noqa: E402
import time as _time  # noqa: E402

_probe = _tempfile.mkdtemp(prefix="astrmai-market-probe-")
try:
    pm = Market(
        Path(_probe),  # 工作区也指到临时目录：缓存是 <工作区>/cache，写进真缓存会把别的测试喂成假目录
        astrbot_root=Path(_probe) / "ab",
        maibot_root=Path(_probe) / "mb",
        native_root=Path(_probe) / "nm",
    )
    (Path(_probe) / "ab" / "data" / "plugins" / "已装的星").mkdir(parents=True)
    (Path(_probe) / "ab" / "data" / "plugins" / "已装的星" / "metadata.yaml").write_text(
        "name: 已装的星\ndesc: x\nauthor: y\nversion: 1.0.0\n", encoding="utf-8"
    )
    listed = [{"id": "已装的星", "name": "已装的星", "repo": ""}, {"id": "other", "name": "别的插件", "repo": "https://github.com/a/b"}]
    pm._mark_installed("astrbot", listed)
    check("目录里的已装项被标出来（点两遍才发现没装上是给运维挖坑）", listed[0]["installed"] is True, str(listed[0])[:90])
    check("没装的项不会被误标（误标比不标更坏）", listed[1]["installed"] is False, str(listed[1])[:90])

    async def _paging() -> None:
        # 目录整本缓存到本地，避免这一步出网：cache/ab_catalog.json 就是搜索读的东西
        import json as _json
        import time as _time

        cache = pm.cache_dir / "ab_catalog.json"
        cache.write_text(
            _json.dumps(
                {
                    "at": _time.time(),
                    "value": [{"source": "astrbot", "id": f"p{i}", "name": f"p{i}", "repo": "https://github.com/a/b", "license": "MIT"} for i in range(40)],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        page1 = await pm.search("astrbot", "", 15, 0)
        page3 = await pm.search("astrbot", "", 15, 30)
        beyond = await pm.search("astrbot", "", 15, 90)
        check("翻页按 offset 取，页与页不重叠", [p["id"] for p in page1["plugins"]][:1] == ["p0"] and [p["id"] for p in page3["plugins"]][:1] == ["p30"], "")
        check("总数与 has_more 说的是真话", page1["total"] == 40 and page1["has_more"] is True and page3["has_more"] is False, str(page3)[:110])
        check("offset 超出目录就是空页，不循环回首", beyond["plugins"] == [] and beyond["has_more"] is False, str(beyond)[:90])
        check("每一页都带已装标记的能力（这里一个都没装）", all(p["installed"] is False for p in page1["plugins"]), "")

        # 麦麦那个接口不理 limit（实测整本 399 条一次给完），所以整本缓存、本地翻页。
        # 但 ASTRMAI_MARKET_NETWORK=0 必须赢过缓存 —— 说"彻底关闭"就不能拿旧缓存当后门。
        (pm.cache_dir / "mb_catalog.json").write_text(
            _json.dumps({"at": _time.time(), "value": [{"source": "maibot", "id": "cached.plugin", "name": "cached", "license": "MIT"}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.environ.pop("ASTRMAI_MARKET_NETWORK", None)
        cached_page = await pm.search("maibot", "", 10, 0)
        check("麦麦目录命中缓存时如实标 cache", cached_page["catalog"] == "cache" and bool(cached_page.get("stale")), str(cached_page)[:110])
        check("缓存命中时不再谎称本次出网", "notice" not in cached_page, str(cached_page)[:90])
        os.environ["ASTRMAI_MARKET_NETWORK"] = "0"
        try:
            await pm.search("maibot", "", 10, 0)
            check("网络开关赢过缓存（有缓存也照样拒绝）", False, "居然返回了数据")
        except RuntimeError as exc:
            check("网络开关赢过缓存（有缓存也照样拒绝）", "ASTRMAI_MARKET_NETWORK=0" in str(exc), str(exc)[:90])
        # 复检 H3：以前这个开关只挡麦麦那条，AstrBot 目录与下载照旧出网，
        # 而页面与 SECURITY.md 都写"彻底关闭/整块关"。现在两条链路共用一道闸。
        try:
            await pm.search("astrbot", "", 10, 0)
            check("同一个开关也关掉 AstrBot 那条（说了整块关就得整块关）", False, "居然返回了数据")
        except RuntimeError as exc:
            check("同一个开关也关掉 AstrBot 那条（说了整块关就得整块关）", "ASTRMAI_MARKET_NETWORK=0" in str(exc), str(exc)[:90])
        try:
            await pm._download_repo_zip("https://github.com/a/b")
            check("关闭时连下载插件包也拒绝", False, "居然去了")
        except RuntimeError as exc:
            check("关闭时连下载插件包也拒绝", "ASTRMAI_MARKET_NETWORK=0" in str(exc), str(exc)[:90])
        # force=True 必须真的重新出网。这里把接口地址指到一个没人监听的本地端口：
        # 缓存被绕开就会连接被拒；要是还吐缓存，就是"重新载入"骗人。
        import astrmai.market as mk

        os.environ["ASTRMAI_MARKET_NETWORK"] = "1"
        saved_base = mk.MB_BASE
        mk.MB_BASE = "http://127.0.0.1:1/api/v2/market"
        try:
            await pm.search("maibot", "", 10, 0, True)
            escaped = "force=True 居然没出网（还在吃缓存）"
        except Exception as exc:  # noqa: BLE001 - 期望的就是"它尝试出网然后失败"
            escaped = "" if type(exc).__name__ in ("ConnectError", "RuntimeError") else f"意外异常 {type(exc).__name__}"
        finally:
            mk.MB_BASE = saved_base
        check("force=True 会绕开缓存真的重新取", not escaped, escaped)

    asyncio.run(_paging())
finally:
    _shutil.rmtree(_probe, ignore_errors=True)

# ---------------- 加载器的依赖政策 ----------------
import asyncio  # noqa: E402
import tempfile  # noqa: E402

from astrmai.loader import LoadPolicy, astrbot_needs_pip, load_astrbot  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / "no_reqs").mkdir()
    (root / "missing").mkdir()
    (root / "missing" / "requirements.txt").write_text("definitely-not-a-real-pkg-xyz==1.0\n", encoding="utf-8")
    (root / "satisfied").mkdir()
    (root / "satisfied" / "requirements.txt").write_text("httpx\n", encoding="utf-8")

    check("无 requirements.txt 判定为不需要 pip", astrbot_needs_pip(root / "no_reqs")[0] is False)
    needs, why = astrbot_needs_pip(root / "missing")
    check("缺依赖判定为需要 pip", needs and "definitely-not-a-real-pkg" in why, why[:80])
    check("依赖已满足判定为不需要 pip", astrbot_needs_pip(root / "satisfied")[0] is False)

    class FakePM:
        def __init__(self, store):
            self.plugin_store_path = str(store)
            self.failed_plugin_dict = {}
            self.calls = []

        async def load(self, specified_module_path=None, specified_dir_name=None, ignore_version_check=False):
            self.calls.append(specified_dir_name)
            return True, None

    pm = FakePM(root)
    res = asyncio.run(load_astrbot(pm, "missing", LoadPolicy(allow_deps=False)))
    check("需要装包时默认拒绝加载", not res["ok"] and "不代你" in res["reason"], res["reason"][:90])
    check("拒绝时确实没调 load()", pm.calls == [], str(pm.calls))

    res2 = asyncio.run(load_astrbot(pm, "no_reqs", LoadPolicy(allow_deps=False)))
    check("不需要装包时直接加载", res2["ok"] and pm.calls == ["no_reqs"], f"{res2['ok']} {pm.calls}")

    res3 = asyncio.run(load_astrbot(pm, "missing", LoadPolicy(allow_deps=True)))
    check("显式允许后才放行", res3["ok"] and pm.calls == ["no_reqs", "missing"], str(pm.calls))

print("\n结果:", "全部通过" if not FAILS else f"{len(FAILS)} 项失败 {FAILS}")
sys.exit(0 if not FAILS else 1)
