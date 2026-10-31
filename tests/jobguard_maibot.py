"""Fixture for tests/test_job_guard.py: boot the real MaiBot brain, get a real plugin runner up,
then wait to be hard-killed so the test can check whether that runner followed the host down.

Refuses to run unless MaiBot is pointed at the local stub -- this fixture must never spend the
user's cloud quota.
"""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

WS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WS))

STUB = "127.0.0.1:18099"
PLUGIN_ID = "astrmai.jobguard-sample"
SAMPLE_DIR = WS / "MaiBot-main" / "plugins" / "astrmai_jobguard_sample"


def die(msg: str):
    print(json.dumps({"error": msg}, ensure_ascii=False), flush=True)
    sys.exit(2)


def make_sample() -> bool:
    source = WS / "MaiBot-main" / "plugins" / "hello_world_plugin"
    if not source.exists():
        return False
    if SAMPLE_DIR.exists():
        shutil.rmtree(SAMPLE_DIR)
    shutil.copytree(source, SAMPLE_DIR, ignore=shutil.ignore_patterns("__pycache__", "webui.json"))
    manifest_path = SAMPLE_DIR / "_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["id"] = PLUGIN_ID
    manifest.pop("webui", None)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (SAMPLE_DIR / "config.toml").write_text('[plugin]\nenabled = false\nconfig_version = "2.0.0"\n', encoding="utf-8")
    return True


async def main(pids_path: Path) -> int:
    cfg_text = (WS / "MaiBot-main" / "config" / "model_config.toml").read_text(encoding="utf-8")
    if STUB not in cfg_text:
        die(f"模型配置没指向本地靶机（{STUB}），拒绝用真 key 跑夹具")

    os.environ["ASTRMAI_WORKSPACE"] = str(WS)
    os.environ["ASTRMAI_ACCOUNT_ID"] = "10001"
    os.environ["ASTRMAI_ALLOWED_USERS"] = "777"
    os.environ["ASTRMAI_ALLOW_GROUPS"] = "0"

    if not make_sample():
        die("没有 hello_world_plugin 可用作样本来源")

    from astrmai.config import AstrmaiConfig
    from astrmai.host import MaiBotHost
    from astrmai.loader import LoadPolicy, load_maibot

    async def sink(*_a, **_k) -> None:  # 不出站，只为了把脑子跑起来
        return None

    host = MaiBotHost(AstrmaiConfig(mai_root=WS / "MaiBot-main", account_id="10001"), sink)
    await host.boot()

    result = await load_maibot(PLUGIN_ID, LoadPolicy(allow_deps=False))
    runners: list[int] = []
    for _ in range(30):
        runners = host.live_runners()  # 内部会重扫 supervisor，重启换 PID 也认得出
        if runners:
            break
        await asyncio.sleep(1.0)

    payload = {
        "mid": os.getpid(),
        "boot_ms": round(host.boot_ms),
        "armed": host.job.mode if host.job else "none",
        "detail": host.job.detail if host.job else "",
        "load_ok": bool(result.get("ok")),
        "runners": runners,
    }
    tmp = pids_path.with_suffix(".maibot.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(pids_path)

    await asyncio.Event().wait()  # 保持 loop 活着，别把 supervisor 的心跳掐了


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main(Path(sys.argv[1]))))
    except KeyboardInterrupt:
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - 夹具失败要把原因留在 stdout 里给测试看
        print(f"fixture crashed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
