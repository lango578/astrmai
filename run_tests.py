"""Run the offline suite without ever spending the real API key.

Switches MaiBot to the local stub, runs the tests, and restores the cloud config afterwards --
even if a test fails -- so the workspace never stays in a half-configured state.
"""

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

# 本机控制台默认码是 GBK：子进程往管道里写中文会按 GBK 编码，父进程再按 UTF-8 解就全是 \ufffd，
# 连打印结果这行本身都能把 run_tests 自己崩掉。两边都钉死 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

WS = Path(__file__).resolve().parent
PY = WS / ".venv" / "Scripts" / "python.exe"
STUB_PORT = 18099
TESTS = (
    "tests/test_model_config_switch.py",
    "tests/test_job_guard.py",
    "tests/test_segments.py",
    "tests/test_market_offline.py",
    "tests/test_licensing.py",
    "tests/test_plugin_bridge.py",
    "tests/test_federation.py",
    "tests/test_plugin_tools_mirror.py",
    "tests/test_command_tools.py",
    "tests/test_runtime_load.py",
    "tests/test_native_plugin.py",
    "tests/test_panel_api.py",
    "tests/test_panel_auth.py",
    "tests/test_panel_security.py",
    "tests/test_e2e_astrbot.py",
)


def stub_alive() -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{STUB_PORT}/v1/models", timeout=3).status_code == 200
    except Exception:
        return False


MAI_CFG = WS / "MaiBot-main" / "config" / "model_config.toml"


def restore(backup: Optional[bytes]) -> None:
    """还原成"跑之前那一份字节"，而不是硬调云端脚本。

    以前这里 `check=True` 跑 use_real_model.py：拿到发布包、没有 secrets/ 的人跑一次自检就整段抛异常，
    连带 finally 里"收掉自己起的桩"那几行永远轮不到 —— 僵尸 mock_llm.py 就是这么攒下来的（第三方复检 L9 的成因）。
    """

    try:
        if backup is not None:
            MAI_CFG.write_bytes(backup)
            print("模型配置已按跑之前的字节还原（不假设你用的是云端还是本机模型）")
        else:
            subprocess.run([str(PY), "use_real_model.py"], cwd=WS, capture_output=True, env=CHILD_ENV)
            print("模型配置尝试还原为云端（跑之前读不到原配置）")
    except Exception as exc:  # noqa: BLE001 - 还原失败要报告，但不能把桩留下当僵尸
        print(f"警告：模型配置还原失败（{type(exc).__name__}: {exc}），请自己核对 {MAI_CFG}")


def main() -> int:
    backup = MAI_CFG.read_bytes() if MAI_CFG.exists() else None
    subprocess.run([str(PY), "use_local_model.py"], cwd=WS, check=True, capture_output=True, env=CHILD_ENV)
    print("模型配置已切到本地靶机")

    # 守卫：靶机配置必须指向本地桩、且不得携带任何真实密钥
    cfg_text = (WS / "MaiBot-main" / "config" / "model_config.toml").read_text(encoding="utf-8")
    if f"127.0.0.1:{STUB_PORT}" not in cfg_text:
        print(f"ABORT: 靶机配置的 base_url 不指向 127.0.0.1:{STUB_PORT}")
        restore(backup)
        return 3
    if "sk-" in cfg_text:
        print("ABORT: 靶机配置里残留了密钥，拒绝继续")
        restore(backup)
        return 4
    print("守卫通过：靶机地址正确、配置内无密钥")

    mock = None
    if not stub_alive():
        mock = subprocess.Popen(
            [str(PY), "mock_llm.py"], cwd=WS, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=CHILD_ENV
        )
        for _ in range(20):
            if stub_alive():
                break
            time.sleep(1)
    print(f"靶机存活: {stub_alive()}")

    codes = {}
    # 每项的完整输出都留档（e2e/logs/ 被 .gitignore 与打包白名单双重排除）。
    # 只在失败时留证据是不够的：绿灯也得能回溯"到底验了哪几条"，不然就是在信退出码。
    evidence = WS / "e2e" / "logs"
    evidence.mkdir(parents=True, exist_ok=True)
    try:
        for t in TESTS:
            started = time.time()
            r = subprocess.run(
                [str(PY), t],
                cwd=WS,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1500,
                env=CHILD_ENV,
            )
            codes[t] = r.returncode
            (evidence / f"{Path(t).stem}.{'fail' if r.returncode else 'ok'}.log").write_text(
                (r.stdout or "") + "\n=== STDERR ===\n" + (r.stderr or ""), encoding="utf-8"
            )
            tail = [l for l in (r.stdout or "").splitlines() if l.strip()][-1:]
            print(f"{'PASS' if r.returncode == 0 else 'FAIL'} {t} ({time.time() - started:.0f}s) {tail[0][:160] if tail else ''}")
            if r.returncode != 0:
                name = Path(t).stem + ".failure.log"
                (WS / name).write_text((r.stdout or "") + "\n=== STDERR ===\n" + (r.stderr or ""), encoding="utf-8")
                print(f"    取证写入 {name}")
    finally:
        try:
            restore(backup)
        finally:
            # 收桩这件事不能被上面的异常挡掉：起它的是我，收它的也得是我
            if mock is not None:
                mock.terminate()
                try:
                    mock.wait(timeout=10)
                except Exception:
                    mock.kill()

    bad = [k for k, v in codes.items() if v != 0]
    print(f"\n合计 {len(codes)} 项，失败 {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
