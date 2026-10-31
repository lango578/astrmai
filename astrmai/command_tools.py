"""把"只注册命令"的 AstrBot 插件也变成可调用的工具。

AstrBot 没有公开的"程序化调用命令"API，所以这里做的是它自己流水线内部动作的最小复刻：
拿一份会话事件的浅拷贝 → 改写 message_str 并置唤醒标志 → 权限过滤器自己跑一遍 →
CommandFilter 解析参数 → call_handler 迭代 → 截获结果链，全程不碰平台发送。
"""

import copy
import re
from typing import Any

_NAME_RE = re.compile(r"[^0-9a-zA-Z_-]+")

# 工具名 -> 原始命令全文。必须留着，因为工具名里的下划线可能本来就是命令的一部分，
# 从名字反推命令会把 "plugin market" 和 "plugin_market" 混掉。
COMMAND_BY_TOOL: dict[str, str] = {}


def _tool_name(full_command: str) -> str:
    return _NAME_RE.sub("_", full_command.strip()).strip("_") or "command"


def _command_filters(handler: Any) -> list[Any]:
    from astrbot.core.star.filter.command import CommandFilter

    return [f for f in getattr(handler, "event_filters", []) if isinstance(f, CommandFilter)]


def _full_command_name(filter_obj: Any) -> str:
    # 非命令组的 CommandFilter 会带一个空的 parent 占位，直接 join 会多出前导空格
    parents = [str(p) for p in (getattr(filter_obj, "parent_command_names", []) or []) if str(p).strip()]
    return " ".join([*parents, str(filter_obj.command_name).strip()]).strip()


def list_command_handlers() -> list[dict]:
    """枚举全部命令处理器（含子命令叶子），返回可直接建工具的描述。"""

    from astrbot.core.star.star_handler import star_handlers_registry

    out = []
    seen: set[str] = set()
    COMMAND_BY_TOOL.clear()
    for handler in list(getattr(star_handlers_registry, "_handlers", []) or []):
        if not getattr(handler, "enabled", True):
            continue
        for cf in _command_filters(handler):
            full = _full_command_name(cf)
            tool = _tool_name(full)
            if tool in seen:
                continue
            seen.add(tool)
            COMMAND_BY_TOOL[tool] = full
            try:
                from astrbot.core.star.star import star_map

                plugin = getattr(star_map.get(handler.handler_module_path), "name", "")
            except Exception:  # noqa: BLE001
                plugin = ""
            alias = sorted(getattr(cf, "alias", set()) or [])
            params = getattr(cf, "handler_params", {}) or {}
            desc = (handler.desc or "").strip() or f"AstrBot 命令 {full}"
            if alias:
                desc += f"（别名: {', '.join(alias)}）"
            if params:
                desc += "；参数: " + ", ".join(params.keys())
            out.append(
                {
                    "name": tool,
                    "command": full,
                    "description": desc,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "args": {
                                "type": "string",
                                "description": f"`{full}` 后面的参数，用空格分隔；不带参数就留空",
                            }
                        },
                        "required": [],
                    },
                    "plugin": plugin,
                }
            )
    return out


def _chain_text(chain: Any) -> str:
    if not chain:
        return ""
    try:
        from astrbot.core.message.message_event_result import MessageChain

        return MessageChain(chain=list(chain)).get_plain_text()
    except Exception:  # noqa: BLE001
        return " ".join(str(getattr(c, "text", "") or "") for c in chain).strip()


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    chain = getattr(value, "chain", None)
    if chain is not None:
        return _chain_text(chain)
    return _chain_text(getattr(value, "message", None)) or str(value)


async def run_command(command_text: str, event: Any, cfg: Any) -> str:
    """在指定会话里跑一条命令，返回它本想发到 QQ 的文本。"""

    from astrbot.core.pipeline.context_utils import call_handler
    from astrbot.core.platform.astr_message_event import event_context
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.filter.permission import PermissionTypeFilter
    from astrbot.core.star.star_handler import star_handlers_registry

    line = re.sub(r"\s+", " ", str(command_text or "").strip())
    if not line:
        return "请给出命令名，例如 `sid` 或 `help`。"

    matched = None
    for handler in list(getattr(star_handlers_registry, "_handlers", []) or []):
        if not getattr(handler, "enabled", True):
            continue
        for cf in _command_filters(handler):
            full = _full_command_name(cf)
            if line == full or line.startswith(full + " "):
                if matched is None or len(full) > len(matched[1]):
                    matched = (handler, full, cf)
    if matched is None:
        return f"AstrBot 里没有这条命令: {line}"
    handler, full, cf = matched

    ev = copy.copy(event)
    ev._extras = dict(getattr(event, "_extras", {}) or {})
    # CommandFilter 自己会剥掉命令词并切分参数，这里必须给完整命令行；
    # 唤醒前缀（"/"）由 WakingCheck 负责去掉，我们传的本来就没有前缀。
    ev.message_str = line
    ev.is_at_or_wake_command = True
    ev._force_stopped = False

    captured: list[str] = []

    async def capture(data: Any = None, *args: Any, **kwargs: Any) -> bool:
        captured.append(_as_text(data))
        return True

    ev.send = capture  # type: ignore[method-assign]
    ev._has_send_oper = True

    for f in getattr(handler, "event_filters", []) or []:
        if isinstance(f, PermissionTypeFilter):
            try:
                allowed = f.filter(ev, cfg)
            except Exception as exc:  # noqa: BLE001
                return f"权限检查异常: {type(exc).__name__}: {exc}"
            if not allowed:
                return f"权限不足：{full} 需要 {f.permission_type}"

    try:
        if not cf.filter(ev, cfg):
            return f"命令未被匹配: {full}"
    except ValueError as exc:
        # CommandFilter 对缺参/类型错直接抛 ValueError，这正是给模型看的最好说明
        return f"参数不对：{exc}"

    params = ev.get_extra("parsed_params") or {}
    call = handler.handler
    # 正常情况下 star_manager 加载插件时会把处理器绑成 partial(raw, star_cls)；
    # 但没走那条加载路径（测试、或我们自己 import 的插件）时拿到的是未绑定函数，
    # 直接调用会 TypeError 并被 call_handler 静默吞掉。这里显式补绑。
    try:
        import functools
        import inspect

        if inspect.isfunction(call):
            sig = inspect.signature(call)
            first = next(iter(sig.parameters.values()), None)
            if first is not None and first.name == "self":
                from astrbot.core.star.star import star_map

                meta = star_map.get(handler.handler_module_path)
                instance = getattr(meta, "star_cls", None) if meta is not None else None
                if instance is not None:
                    call = functools.partial(call, instance)
                else:
                    # call_handler 遇到参数不匹配只会记日志然后静默返回，
                    # 不拦下来的话结果会是"执行完毕但没有任何输出"这种误导性说法。
                    return f"找不到插件实例，无法代跑命令 {full}（该插件没走 AstrBot 的 star_manager 加载）"
    except Exception:  # noqa: BLE001 - 绑不上就按原样调用，失败会有明确返回
        pass

    try:
        with event_context(ev):
            # call_handler 对 yield 字符串并不 set_result，而是把值向上冒泡；
            # 所以冒泡值和 ev.get_result() 两条都要截，否则最常见的插件写法会被漏掉。
            async for ret in call_handler(ev, call, **params):
                if ret is not None:
                    captured.append(_as_text(ret))
                captured.append(_as_text(ev.get_result()))
    except Exception as exc:  # noqa: BLE001 - 插件炸了要变成工具结果，不能炸宿主
        return f"命令执行出错: {type(exc).__name__}: {exc}"

    text = "\n".join(c for c in captured if c and c.strip())
    return text.strip() or f"（{full} 执行完毕，没有文本输出）"


def make_command_tool_runner(cfg_getter: Any, event_getter: Any) -> Any:
    """给联邦用的 run_tool 闭包：工具名换回原始命令，args.args 拼在后面执行。"""

    async def run(name: str, args: dict, mb_context: Any) -> str:
        event = event_getter(mb_context)
        if event is None:
            return "该会话还没有可用的 AstrBot 事件，无法代跑命令。"
        command = COMMAND_BY_TOOL.get(str(name), str(name).replace("_", " "))
        rest = str(args.get("args", "") or "").strip()
        return await run_command(f"{command} {rest}".strip(), event, cfg_getter())

    return run
