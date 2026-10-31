"""结构级改写 MaiBot 的 model_config.toml（不再用正则碰 TOML）。

之前两个脚本用正则增删 provider/model 块，来回切换会累积重名条目，被 MaiBot 校验直接拒掉
（"API提供商名称存在重复"）。这里用 tomlkit 按结构改写，天然幂等。
"""

import tomllib
from collections import Counter
from pathlib import Path

import tomlkit

LLM_TASKS = ("replyer", "planner", "utils", "fast_model", "memory", "learner", "emoji")
# 这些任务需要别的模型类型或联网服务，塞 LLM 会让配置校验失败
SPECIAL_TASKS = ("web_search", "vlm", "voice", "embedding", "image_embedding")


def apply_config(
    cfg_path: Path,
    *,
    provider_name: str,
    base_url: str,
    api_key: str,
    auth_type: str,
    model_name: str,
    model_identifier: str,
) -> dict:
    """写成单 provider + 单模型，LLM 任务绑定它，特殊任务清空。返回可核对的摘要。"""

    text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    doc = tomlkit.parse(text) if text.strip() else tomlkit.document()

    provider = tomlkit.table(is_super_table=True)
    provider.update(
        {
            "name": provider_name,
            "base_url": base_url,
            "api_key": api_key,
            "client_type": "openai",
            "auth_type": auth_type,
            "model_list_endpoint": "/models",
            "max_retry": 2,
            "timeout": 300,
            "retry_interval": 2,
        }
    )
    model = tomlkit.table(is_super_table=True)
    model.update(
        {
            "model_type": "llm",
            "model_identifier": model_identifier,
            "name": model_name,
            "api_provider": provider_name,
            "send_temperature": True,
            "force_stream_mode": False,
            "visual": False,
        }
    )

    doc["api_providers"] = tomlkit.items.AoT([provider])
    doc["models"] = tomlkit.items.AoT([model])

    mtc = doc.get("model_task_config")
    if mtc is None:
        raise RuntimeError("配置里没有 model_task_config 段落，请先让 MaiBot 生成一次默认配置")

    for task in LLM_TASKS:
        table = mtc.get(task)
        if table is not None:
            table["model_list"] = [model_name]
    for task in SPECIAL_TASKS:
        table = mtc.get(task)
        if table is not None:
            table["model_list"] = []

    cfg_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    # 自证：重新解析，确认没有重名、绑定符合预期
    with cfg_path.open("rb") as fh:
        parsed = tomllib.load(fh)
    providers = [p.get("name") for p in parsed.get("api_providers", [])]
    models = [m.get("name") for m in parsed.get("models", [])]
    dup = [n for n, c in Counter(providers).items() if c > 1] + [n for n, c in Counter(models).items() if c > 1]
    bound = {
        task: (parsed.get("model_task_config", {}).get(task, {}) or {}).get("model_list")
        for task in (*LLM_TASKS, *SPECIAL_TASKS)
        if task in parsed.get("model_task_config", {})
    }
    return {
        "providers": providers,
        "models": models,
        "duplicates": dup,
        "bound": bound,
        "ok": not dup and providers == [provider_name] and models == [model_name],
    }
