"""配置切换的幂等回归：反复切靶机/云端不得产生重名 provider 或错绑任务。

这条专门盯以前那个"API提供商名称存在重复"的坑。结束时留在靶机状态（离线安全）。
"""

import io
import sys
import tomllib
from collections import Counter
from pathlib import Path

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS))

from model_config_io import LLM_TASKS, SPECIAL_TASKS, apply_config  # noqa: E402

CFG = WS / "MaiBot-main" / "config" / "model_config.toml"
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail else ""), flush=True)
    if not cond:
        FAILS.append(name)


def state() -> dict:
    with CFG.open("rb") as fh:
        d = tomllib.load(fh)
    providers = [p.get("name") for p in d.get("api_providers", [])]
    models = [m.get("name") for m in d.get("models", [])]
    mtc = d.get("model_task_config", {})
    return {
        "providers": providers,
        "models": models,
        "dup": [n for n, c in Counter(providers).items() if c > 1] + [n for n, c in Counter(models).items() if c > 1],
        "bound": {k: (mtc.get(k, {}) or {}).get("model_list") for k in mtc},
        "raw": CFG.read_text(encoding="utf-8"),
    }


stub = dict(
    provider_name="LocalQwen",
    base_url="http://127.0.0.1:18099/v1",
    api_key="none",
    auth_type="none",
    model_name="local-qwen3-8b",
    model_identifier="qwen3-8b",
)
cloud = dict(
    provider_name="AstrmaiCloud",
    base_url="https://example.invalid/v1",
    api_key="sk-CANARY-not-real",
    auth_type="bearer",
    model_name="astrmai-cloud",
    model_identifier="some-model",
)

for round_no in range(1, 4):
    for label, kwargs in (("stub", stub), ("cloud", cloud)):
        res = apply_config(CFG, **kwargs)
        s = state()
        check(f"round{round_no} {label} 无重名", not s["dup"], str(s["dup"]) or str(s["providers"]))
        check(f"round{round_no} {label} 自证通过", res["ok"], str(res["providers"]) + str(res["models"]))
        wrong = [t for t in LLM_TASKS if t in s["bound"] and s["bound"][t] != [kwargs["model_name"]]]
        check(f"round{round_no} {label} LLM 任务绑定正确", not wrong, str(wrong))
        leaked = [t for t in SPECIAL_TASKS if s["bound"].get(t)]
        check(f"round{round_no} {label} 特殊任务已清空", not leaked, str(leaked))

apply_config(CFG, **stub)
check("回到靶机后配置不含密钥痕迹", "sk-CANARY" not in state()["raw"])

print("\n结果:", "全部通过" if not FAILS else f"{len(FAILS)} 项失败 {FAILS}")
sys.exit(0 if not FAILS else 1)
