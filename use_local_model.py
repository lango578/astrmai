"""把 MaiBot 指到本地靶机（离线、零成本、可复现）。"""

import sys
from pathlib import Path

from model_config_io import apply_config

WS = Path(__file__).resolve().parent
CFG = WS / "MaiBot-main" / "config" / "model_config.toml"

summary = apply_config(
    CFG,
    provider_name="LocalQwen",
    base_url="http://127.0.0.1:18099/v1",
    api_key="none",
    auth_type="none",
    model_name="local-qwen3-8b",
    model_identifier="qwen3-8b",
)
print("靶机配置:", summary["providers"], summary["models"], "重复:", summary["duplicates"] or "无")
sys.exit(0 if summary["ok"] else 1)
