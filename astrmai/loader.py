"""把已经落到磁盘的插件，装进正在运行的两个脑子。

依赖政策（默认禁止 pip）：
- AstrBot 的 load() 内部会走 `_import_plugin_with_dependency_recovery`，而它是否可能装包
  完全取决于插件目录里有没有 requirements.txt 且缺依赖（没有 requirements.txt 时模式就是
  DISABLED，绝不碰 pip）。所以这里先自己算一遍：需要装包的插件一律拒绝加载，除非显式 allow_deps。
- 麦麦侧 `activate_installed_plugin` 一定会跑依赖流水线，所以默认改用
  `load_plugin_globally`；只有 allow_deps 才用 activate。
"""

import asyncio
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEPS_ASK = "该插件需要 pip 安装依赖，astrmai 默认不代你在环境里装包；确认后再带 allow_deps 重试"


@dataclass(slots=True)
class LoadPolicy:
    allow_deps: bool = False


def astrbot_needs_pip(plugin_dir: Path) -> tuple[bool, str]:
    """判断 AstrBot 插件加载时是否可能触发 pip。"""

    req = Path(plugin_dir) / "requirements.txt"
    if not req.exists():
        return False, "无 requirements.txt，AstrBot 的 load() 处于 DISABLED 模式，不会碰 pip"

    plan = None
    try:
        from astrbot.core.utils.requirements_utils import plan_missing_requirements_install

        plan = plan_missing_requirements_install(str(req))
    except Exception:  # noqa: BLE001 - 拿不到官方判定就按"有风险"处理，保守方向
        return True, "无法评估 requirements.txt（astrbot 依赖工具不可导入），按需要装包处理"

    if plan is None:
        return False, "官方判定无需安装（install_plan 为空）"
    # 关键：依赖全满足时它返回的是 missing_names 为空的 plan，不是 None
    missing = sorted(set(getattr(plan, "missing_names", None) or []))
    if not missing:
        return False, "requirements.txt 里的依赖都已满足"
    return True, "缺失依赖: " + ", ".join(missing[:6])


async def load_astrbot(plugin_manager: Any, dir_name: str, policy: LoadPolicy) -> dict:
    """让运行中的 AstrBot 加载 data/plugins 下已存在的插件目录。"""

    root = None
    for attr in ("plugin_store_path", "plugin_dir", "plugins_path"):
        value = getattr(plugin_manager, attr, None)
        if value:
            root = Path(value)
            break
    plugin_dir = (root / dir_name) if root else None

    if plugin_dir is not None and plugin_dir.exists():
        needs, why = astrbot_needs_pip(plugin_dir)
        if needs and not policy.allow_deps:
            return {"ok": False, "side": "astrbot", "reason": f"{DEPS_ASK}（{why}）", "plugin": dir_name}
    elif plugin_dir is not None:
        return {"ok": False, "side": "astrbot", "reason": f"目录不存在: {plugin_dir}", "plugin": dir_name}

    before = set(getattr(plugin_manager, "failed_plugin_dict", {}) or {})
    result = plugin_manager.load(specified_dir_name=dir_name)
    if inspect.isawaitable(result):
        ok, err = await result
    else:  # pragma: no cover - 防御不同版本
        ok, err = result, None

    failed = getattr(plugin_manager, "failed_plugin_dict", {}) or {}
    record = failed.get(dir_name)
    if dir_name in before:
        record = record or "仍在失败清单里"
    return {
        "ok": bool(ok) and record is None,
        "side": "astrbot",
        "plugin": dir_name,
        "load_returned": bool(ok),
        "error": str(err or "")[:200] if err else "",
        "failure_record": str(record)[:200] if record else "",
        "deps_allowed": policy.allow_deps,
    }


async def load_maibot(plugin_id: str, policy: LoadPolicy, *, enable_first: bool = True, timeout: float = 30.0) -> dict:
    """让运行中的麦麦加载已放进 plugins/ 的插件（默认不跑依赖安装）。

    不能只信返回值：麦麦自己的插件文件监视器可能正在因为同一个新目录重启 supervisor，
    这时显式加载会拿到 E_SHUTTING_DOWN 而返回 False，几秒后插件其实照样加载成功。
    所以按"组件登记表里是否真的出现了这个插件"来定成败，并对瞬时失败做短重试。
    """

    import time

    from src.common.runtime_loop import run_on_main_loop
    from src.plugin_runtime.integration import get_plugin_runtime_manager

    if enable_first:
        _enable_on_disk(plugin_id)

    manager = get_plugin_runtime_manager()
    attempts: list[str] = []
    deadline = time.monotonic() + timeout

    while True:
        if policy.allow_deps:
            coro = manager.activate_installed_plugin(plugin_id)
            how = "activate_installed_plugin（含依赖安装）"
        else:
            coro = manager.load_plugin_globally(plugin_id, reason="astrmai_load")
            how = "load_plugin_globally（跳过依赖安装）"
        try:
            outcome = await run_on_main_loop(coro)
        except Exception as exc:  # noqa: BLE001
            outcome, _ = None, None
            attempts.append(f"{type(exc).__name__}: {exc}")
        else:
            attempts.append(f"returned={outcome}")

        loaded = _plugin_present(plugin_id)
        if loaded:
            return {
                "ok": True,
                "side": "maibot",
                "plugin": plugin_id,
                "path": how,
                "attempts": attempts,
                "components": loaded,
            }
        if time.monotonic() >= deadline:
            return {
                "ok": False,
                "side": "maibot",
                "plugin": plugin_id,
                "path": how,
                "attempts": attempts,
                "load_statuses": _status_for(manager, plugin_id),
                "reason": f"等待 {timeout:.0f}s 后仍未在组件登记表出现",
            }
        await asyncio.sleep(2)


def _plugin_present(plugin_id: str) -> list[str]:
    """组件登记表里该插件已出现的组件名。"""

    try:
        from src.plugin_runtime.component_query import component_query_service

        specs = component_query_service.get_llm_available_tool_specs(None)
    except Exception:  # noqa: BLE001 - 还没起插件运行时属于正常情况
        return []
    return sorted(
        name
        for name, spec in specs.items()
        if getattr(spec, "provider_name", "") == plugin_id or plugin_id.split(".")[-1] in str(getattr(spec, "provider_name", ""))
    )


def _enable_on_disk(plugin_id: str) -> None:
    """把插件目录的 config.toml 里 [plugin] enabled 打开，否则加载会被开关拦住。"""

    import tomlkit

    from src.common.paths import get_instance_root

    candidates = [Path(get_instance_root()) / "plugins"]
    for base in candidates:
        for entry in base.glob("*/_manifest.json") if base.exists() else []:
            try:
                import json

                if json.loads(entry.read_text(encoding="utf-8")).get("id") != plugin_id:
                    continue
            except ValueError:
                continue
            cfg = entry.parent / "config.toml"
            doc = tomlkit.parse(cfg.read_text(encoding="utf-8")) if cfg.exists() else tomlkit.document()
            if "plugin" not in doc:
                doc.add("plugin", tomlkit.table())
            doc["plugin"]["enabled"] = True
            cfg.write_text(tomlkit.dumps(doc), encoding="utf-8")
            return


def _status_for(manager: Any, plugin_id: str) -> Any:
    try:
        statuses = manager.get_plugin_load_statuses()
    except Exception:  # noqa: BLE001
        return None
    if isinstance(statuses, dict):
        return {k: v for k, v in statuses.items() if plugin_id in str(k)} or list(statuses)[:5]
    return statuses
