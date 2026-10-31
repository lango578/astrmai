"""市场联网取证：三家目录能不能搜到、能不能真的装下一个。

不进离线套件（run_tests.py 默认不跑它），因为它要出网。用 --live 显式执行。
"""

import asyncio
import json
import shutil
import sys
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))

from astrmai.market import Market  # noqa: E402

ROOT = WS / "probe" / "market"


async def main() -> int:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    (ROOT / "ab" / "data" / "plugins").mkdir(parents=True)
    (ROOT / "mai" / "plugins").mkdir(parents=True)
    (ROOT / "native").mkdir(parents=True)

    market = Market(
        WS,
        astrbot_root=ROOT / "ab",
        maibot_root=ROOT / "mai",
        native_root=ROOT / "native",
    )
    results = {}
    failures = []

    for source in ("astrbot", "maibot"):
        try:
            r = await market.search(source, "", 5)
            results[f"search:{source}"] = {"catalog": r["catalog"], "total": r["total"], "sample": r["plugins"][:2]}
            print(f"PASS search {source}: {r['total']} 条，源={r['catalog']}")
            for e in r["plugins"][:2]:
                print(f"     - {e['id']} | {e['name'][:24]} | {e['repo'][:56]}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"search:{source}")
            print(f"FAIL search {source}: {type(exc).__name__}: {str(exc)[:180]}")

    # 装一个 AstrBot 插件
    ab_ident = ""
    try:
        r = await market.search("astrbot", "", 40)
        cand = next((e for e in r["plugins"] if e["repo"].startswith("https://github.com/")), None)
        if cand is None:
            raise RuntimeError("目录里没有可下载的 GitHub 仓库条目")
        ab_ident = str(cand["id"])
        out = await market.install("astrbot", cand["id"])
        ok = bool(out.get("ok"))
        print(f"{'PASS' if ok else 'FAIL'} install astrbot {cand['name']}: {out.get('path') or out.get('reason')}")
        if ok:
            files = sorted(p.name for p in Path(out["path"]).iterdir())
            print(f"     落地文件: {files[:8]}")
            results["install:astrbot"] = {"path": out["path"], "where": out["where"], "files": files[:10]}
            # 装完能不能直接进脑子：只做静态评估，绝不在测试里执行第三方插件代码
            from astrmai.loader import astrbot_needs_pip

            installed = Path(out["path"])
            needs, why = astrbot_needs_pip(installed)
            entry_ok = (installed / "metadata.yaml").exists() and any(
                (installed / n).exists() for n in ("main.py", "astrbot_plugin.py")
            )
            print(f"{'PASS' if entry_ok else 'FAIL'} 装载前置检查: 入口齐备={entry_ok}｜依赖判定={why}")
            lic = str(out.get("license") or "")
            record_ok = (installed / ".astrmai-license.json").exists()
            print(f"{'PASS' if lic and record_ok else 'FAIL'} 许可证已抓下并记录: {lic}（{out.get('license_confidence')}｜{str(out.get('license_basis'))[:60]}）记录文件={record_ok}")
            results["license:astrbot"] = {"license": lic, "basis": out.get("license_basis"), "record": record_ok}
            if not (lic and record_ok):
                failures.append("license:astrbot")
            # 目录条目本身就该带 license 字段（AstrBot 目录不给 → 显式 unknown，不留空）
            probe_missing = [p for p in (r.get("plugins") or []) if not p.get("license")]
            print(f"{'PASS' if not probe_missing else 'FAIL'} 搜索结果的 license 字段从不留空（unknown 也算填了）｜缺 {len(probe_missing)} 条")
            if probe_missing:
                failures.append("license-field:astrbot")
            results["loadable:astrbot"] = {"entry": entry_ok, "needs_pip": needs, "why": why}
            if not entry_ok:
                failures.append("loadable:astrbot")
        else:
            failures.append("install:astrbot")
            results["install:astrbot"] = out
    except Exception as exc:  # noqa: BLE001
        failures.append("install:astrbot")
        print(f"FAIL install astrbot: {type(exc).__name__}: {str(exc)[:180]}")

    # 装一个麦麦插件（按官方 release 索引钉 tag+commit）
    try:
        r = await market.search("maibot", "", 40)
        cand = next((e for e in r["plugins"] if e.get("tag") and e.get("repo", "").startswith("https://github.com/")), None)
        if cand is None:
            raise RuntimeError("索引里没给带 tag 的条目")
        out = await market.install("maibot", cand["id"])
        ok = bool(out.get("ok"))
        commit = str(out.get("commit") or "")
        print(f"{'PASS' if ok else 'FAIL'} install maibot {cand['id']}: {out.get('path') or out.get('reason')}")
        print(f"{'PASS' if len(commit) >= 7 else 'FAIL'} tag/commit 已钉住: {cand.get('tag')} -> {commit[:12] or '(空)'}")
        print(f"{'PASS' if out.get('license') else 'FAIL'} 许可证已记录: {out.get('license') or '(空)'}")
        # 装载前置检查：manifest 的 id 要和索引一致，release 取证文件要在
        from astrmai.loader import astrbot_needs_pip  # noqa: F401  仅为确认政策模块可用

        mb_dir = Path(out["path"]) if out.get("path") else None
        manifest_ok = release_ok = False
        if mb_dir and mb_dir.exists():
            try:
                manifest_ok = json.loads((mb_dir / "_manifest.json").read_text(encoding="utf-8")).get("id") == cand["id"]
            except (OSError, ValueError):
                manifest_ok = False
            release_ok = (mb_dir / ".maibot-release.json").exists()
        print(f"{'PASS' if (manifest_ok and release_ok) else 'FAIL'} 麦麦插件装载前置检查: id一致={manifest_ok}｜release取证={release_ok}")
        results["loadable:maibot"] = {"id_match": manifest_ok, "release_record": release_ok}
        if not (manifest_ok and release_ok):
            failures.append("loadable:maibot")
        results["install:maibot"] = out
        if not ok or len(commit) < 7 or not out.get("license"):
            failures.append("install:maibot")
    except Exception as exc:  # noqa: BLE001
        failures.append("install:maibot")
        print(f"FAIL install maibot: {type(exc).__name__}: {str(exc)[:180]}")

    # 装一个原生插件（本地复制）
    out = await market.install("astrmai", str(WS / "astrmai_plugins" / "astrmai_demo"), dest=ROOT / "native")
    print(f"{'PASS' if out.get('ok') else 'FAIL'} install astrmai native: {out.get('path') or out.get('reason')}")
    results["install:astrmai"] = out
    if not out.get("ok"):
        failures.append("install:astrmai")

    for source in ("astrbot", "maibot", "astrmai"):
        results[f"list:{source}"] = market.list_installed(source)
        print(f"  已安装 {source}: {[i['name'] for i in results[f'list:{source}']][:5]}")

    # 装前探测 + 已装清单（许可证这条链的最后一环：能查、能列、认不出就 unknown）
    try:
        probe = await market.probe_license("astrbot", ab_ident)
        ok = bool(probe.get("ok")) and bool(str(probe.get("license") or ""))
        print(f"{'PASS' if ok else 'FAIL'} probe_license 装前探测: {probe.get('license')}（{probe.get('confidence')}｜{str(probe.get('basis'))[:60]}）")
        results["probe:astrbot"] = probe
        if not ok:
            failures.append("probe:astrbot")
    except Exception as exc:  # noqa: BLE001
        failures.append("probe:astrbot")
        print(f"FAIL probe_license: {type(exc).__name__}: {str(exc)[:180]}")

    rows = market.licenses()
    unknown = [r["component"] for r in rows if r.get("undetermined")]
    print(f"{'PASS' if rows else 'FAIL'} 许可证清单 {len(rows)} 项，显式 unknown {len(unknown)} 项：{unknown or '无'}")
    for r in rows:
        print(f"     - {r['component']} | {r['source']} | {r['license']} | {r['confidence']} | {str(r['basis'])[:60]}")
    results["licenses"] = rows
    if not rows:
        failures.append("licenses")

    (WS / "market_live_report.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n未成功项:", failures or "无")
    print("报告:", WS / "market_live_report.json")
    return 1 if failures else 0


if __name__ == "__main__":
    if "--live" not in sys.argv:
        print("这个测试要出网。确认请加 --live")
        sys.exit(2)
    sys.exit(asyncio.run(main()))
