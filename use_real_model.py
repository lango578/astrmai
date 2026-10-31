"""把 MaiBot 指到 tools/get_key.cmd 录入的云端模型。

只打印形状（长度/前缀），绝不回显 key。云端配置里必须清空 embedding/vlm/voice/web_search
这类需要别的模型类型的任务。
"""

import json
import sys
from pathlib import Path

from model_config_io import apply_config

WS = Path(__file__).resolve().parent
CFG = WS / "MaiBot-main" / "config" / "model_config.toml"
SECRETS = WS / "secrets" / "model_credentials.json"

if not SECRETS.exists():
    sys.exit(f"缺少 {SECRETS}，请先运行 tools/get_key.cmd 录入凭据")

creds = json.loads(SECRETS.read_text(encoding="utf-8-sig"))
base_url = str(creds.get("base_url", "")).strip()
model_id = str(creds.get("model", "")).strip()
api_key = str(creds.get("api_key", "")).strip()
provider = str(creds.get("provider", "AstrmaiCloud")).strip() or "AstrmaiCloud"
if not (base_url and model_id and api_key):
    sys.exit("凭据不完整（base_url/model/api_key 三项都要有）")

summary = apply_config(
    CFG,
    provider_name=provider,
    base_url=base_url,
    api_key=api_key,
    auth_type="bearer",
    model_name="astrmai-cloud",
    model_identifier=model_id,
)

print("云端配置:", summary["providers"], summary["models"], "重复:", summary["duplicates"] or "无")
print(f"  base_url = {base_url}")
print(f"  model    = {model_id} (作为 astrmai-cloud 供 7 个 LLM 任务使用)")
print(f"  key      = 长度 {len(api_key)}，前缀 {api_key[:3]}***（全文不打印）")
sys.exit(0 if summary["ok"] else 1)
