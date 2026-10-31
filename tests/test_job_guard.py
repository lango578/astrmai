"""Prove the Windows job guard takes plugin runners down with the host, even under taskkill /F.

Two layers:
  1. in-process: arm() puts us in the job, a freshly spawned child inherits membership, and
     process_alive() reports truthfully without killing anything (the os.kill(pid, 0) trap).
  2. cross-process: a mid process arms the guard and spawns a leaf; we taskkill /F the mid and
     require the leaf to be gone. atexit cannot have run -- this is the case the guard exists for.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

WS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WS))

from astrmai.jobguard import JobGuard, arm, process_alive  # noqa: E402

MID = WS / "tests" / "jobguard_mid.py"
MB_FIXTURE = WS / "tests" / "jobguard_maibot.py"
PIDS = WS / "e2e" / "jobguard_pids.json"
TIMEOUT = 25
MB_TIMEOUT = 420  # 麦麦冷启动 + 插件 runner 起来，慢但要给足


def _spawn_leaf_probe() -> int:
    """A child that only sleeps; used to test inheritance from the already-armed host."""

    p = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return p.pid


def test_arm_and_inherit() -> None:
    if sys.platform != "win32":
        print(f"SKIP arm_and_inherit: 非 Windows（{sys.platform}）")
        return

    guard = arm()
    assert guard.mode == "self", f"未能进入作业对象: {guard.detail}"
    assert guard.is_member() is True, "宿主进程不在自己建的作业里"

    pid = _spawn_leaf_probe()
    try:
        time.sleep(0.6)
        assert process_alive(pid), "刚起的子进程被判定为已死"
        assert guard.is_member(pid) is True, "子进程没有继承作业成员身份，宿主死亡时它会留下"
        assert guard.describe().startswith("作业对象=host"), guard.describe()
    finally:
        subprocess.Popen(["taskkill", "/F", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).wait()
        time.sleep(0.6)
        assert not process_alive(pid), "taskkill 后仍判定存活，探针说谎"

    # 退化路径：作业对象不可用时也必须给出诚实状态，而不是假装成功
    idle = JobGuard()
    assert idle.mode == "none" and not idle.armed, "未 arm 的守卫不该自称已装"
    print(f"arm_and_inherit OK  ({guard.describe()})")


def _read_pids(timeout: float = TIMEOUT, path: Path = PIDS) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                time.sleep(0.3)
                continue
            if data.get("mid") and (data.get("leaf") or data.get("runners") or "error" in data):
                return data
        time.sleep(0.3)
    raise AssertionError(f"{timeout}s 内没等到 {path}")


def _run_mid(arm_job: bool) -> tuple[subprocess.Popen, dict]:
    """启动夹具并等它写下 PID 文件，返回 (壳进程, 夹具自报的 PID / 作业状态)。"""

    PIDS.unlink(missing_ok=True)
    env = dict(os.environ, ASTRMAI_JOB_ARM="1" if arm_job else "0")
    mid = subprocess.Popen(
        [sys.executable, str(MID), str(PIDS)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    try:
        return mid, _read_pids()
    except Exception:
        if mid.poll() is None:
            mid.kill()
        raise


def test_kill_host_takes_children() -> None:
    """对照组证明硬杀本身带不走子进程，实验组证明装了作业对象就带得走。"""

    if sys.platform != "win32":
        print("SKIP kill_host_takes_children: 非 Windows")
        return

    PIDS.unlink(missing_ok=True)
    control_mid, control_leaf, control_host = None, None, None
    mid, leaf_pid, host_pid = None, None, None
    try:
        # ---- 对照组：不装作业对象，硬杀宿主后叶子必须活下来，否则这个测试证明不了任何东西 ----
        control_mid, control = _run_mid(arm_job=False)
        control_leaf = int(control["leaf"])
        # venv 的 python.exe 可能是转发壳，真正跑了守卫逻辑的是 PID 文件里那个
        control_host = int(control["mid"])
        time.sleep(1.0)
        assert process_alive(control_leaf), "对照组叶子没起来，测不了"

        subprocess.run(["taskkill", "/F", "/PID", str(control_host)], capture_output=True, timeout=20)
        time.sleep(2.0)
        assert process_alive(control_leaf), (
            "对照组硬杀宿主后叶子竟然也死了 —— 另有机制在兜底，本测试失去意义，需重新设计"
        )
        print(f"control OK  (无作业对象: host={control_host} 被硬杀, leaf={control_leaf} 确实存活)")

        # ---- 实验组：装了作业对象，同一套硬杀必须连坐 ----
        mid, data = _run_mid(arm_job=True)
        leaf_pid = int(data["leaf"])
        host_pid = int(data["mid"])
        assert data["armed"] == "self", f"中间进程没能装作业对象: {data}"
        assert data["inherited"] is True, f"叶子没被认成作业成员: {data}"
        assert process_alive(leaf_pid), "叶子还没被杀就不该继续测"

        # 真·硬杀：atexit、signal handler 都不会跑
        subprocess.run(["taskkill", "/F", "/PID", str(host_pid)], capture_output=True, timeout=20)
        mid.wait(timeout=TIMEOUT)

        deadline = time.time() + TIMEOUT
        while time.time() < deadline and process_alive(leaf_pid):
            time.sleep(0.5)
        assert not process_alive(leaf_pid), (
            f"叶子 {leaf_pid} 在宿主 {host_pid} 被 taskkill /F 后仍存活 —— 作业对象没起到连坐作用"
        )
        print(
            f"kill_host_takes_children OK  (host={host_pid} 硬杀, leaf={leaf_pid} 随之消失；"
            "对照组同条件却存活)"
        )
    finally:
        for pid in (control_leaf, leaf_pid):
            if pid and process_alive(pid):
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        for proc in (control_mid, mid):
            if proc is not None and proc.poll() is None:
                proc.kill()
        PIDS.unlink(missing_ok=True)


def test_real_maibot_runner_dies_with_host() -> None:
    """合成用例的最后一段：被连坐的必须是麦麦真实的 plugin runner，不是我的 sleep 桩。

    需要模型配置已经指向本地靶机（run_tests.py 会切），否则跳过——绝不为了测试去花真 key。
    """

    if sys.platform != "win32":
        print("SKIP real_maibot_runner: 非 Windows")
        return
    cfg_text = ""
    try:
        cfg_text = (WS / "MaiBot-main" / "config" / "model_config.toml").read_text(encoding="utf-8")
    except OSError:
        pass
    if "127.0.0.1:18099" not in cfg_text:
        print("SKIP real_maibot_runner: 模型配置没指向本地靶机（请通过 run_tests.py 跑）")
        return

    mb_pids = WS / "e2e" / "jobguard_maibot_pids.json"
    mb_pids.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [sys.executable, str(MB_FIXTURE), str(mb_pids)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    runners: list[int] = []
    host_pid = None
    try:
        try:
            data = _read_pids(timeout=MB_TIMEOUT, path=mb_pids)
        except AssertionError:
            out = proc.communicate(timeout=30)[0] or ""
            raise AssertionError(f"麦麦夹具没交出 PID 文件；夹具输出尾部:\n{out[-1500:]}") from None
        host_pid = int(data["mid"])
        assert data["armed"] == "self", f"麦麦宿主没装作业对象: {data}"
        runners = [int(p) for p in data.get("runners") or []]
        assert runners, f"麦麦插件 runner 没起来（load_ok={data.get('load_ok')}）：{data}"
        alive = [p for p in runners if process_alive(p)]
        assert alive, f"PID 全是死的，测不了: {runners}"
        print(f"麦麦宿主={host_pid} boot={data.get('boot_ms')}ms runner={alive}")

        subprocess.run(["taskkill", "/F", "/PID", str(host_pid)], capture_output=True, timeout=20)
        proc.wait(timeout=MB_TIMEOUT)

        deadline = time.time() + 30
        while time.time() < deadline and any(process_alive(p) for p in alive):
            time.sleep(0.5)
        left = [p for p in alive if process_alive(p)]
        assert not left, f"宿主被硬杀后麦麦 runner 仍存活: {left} —— 又回到孤儿泄漏"
        print(f"real_maibot_runner OK  (runner {alive} 随宿主硬杀一起消失)")
    finally:
        for pid in runners:
            if process_alive(pid):
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        if proc.poll() is None:
            proc.kill()
        mb_pids.unlink(missing_ok=True)
        sample = WS / "MaiBot-main" / "plugins" / "astrmai_jobguard_sample"
        if sample.exists():
            shutil.rmtree(sample, ignore_errors=True)


if __name__ == "__main__":
    if os.environ.get("ASTRMAI_ALLOW_JOB_TEST", "1") != "1":
        print("SKIP test_job_guard: ASTRMAI_ALLOW_JOB_TEST=0")
        sys.exit(0)
    test_arm_and_inherit()
    test_kill_host_takes_children()
    test_real_maibot_runner_dies_with_host()
    print("\n=== JOB GUARD PASS ===")
