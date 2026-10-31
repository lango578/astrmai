"""双向工具联邦。

A 方向：AstrBot 插件注册的 LLM 工具 -> 麦麦的 planner 工具表（前缀 ab_）
B 方向：麦麦的内置/插件工具 -> AstrBot 的 function-calling 工具表（前缀 mai_）

两侧都过滤对方的前缀，避免把镜像再镜像回去形成回环。
"""

import inspect
import re
import uuid
from typing import Any, Awaitable, Callable

AB_PREFIX = "ab_"
MB_PREFIX = "mai_"
# 已经属于联邦镜像的前缀，再次导出时必须跳过，否则会镜像出套娃
MIRROR_PREFIXES = (AB_PREFIX, MB_PREFIX, "nm_")

# A 方向需要的宿主回调
ListAbTools = Callable[[], list[dict]]
RunAbTool = Callable[[str, dict, Any], Awaitable[str]]


class AstrBotToolMirror:
    """MaiBot 的 ToolProvider：把外部工具镜像进麦麦的工具表。"""

    def __init__(
        self,
        list_tools: ListAbTools,
        run_tool: RunAbTool,
        *,
        visible: bool = True,
        prefix: str = AB_PREFIX,
        provider_name: str = "astrbot",
    ) -> None:
        from src.core.tooling import ToolExecutionResult, ToolSpec  # noqa: F401  (延迟绑定 src)

        self._list = list_tools
        self._run = run_tool
        self._visible = visible
        self._prefix = prefix
        self._ToolSpec = ToolSpec
        self._Result = ToolExecutionResult
        self.provider_name = provider_name
        self.provider_type = "plugin"

    async def list_tools(self, context: Any = None) -> list:
        specs = []
        for tool in self._list() or []:
            name = str(tool.get("name", ""))
            if not name or name.startswith(MIRROR_PREFIXES):
                continue
            schema = tool.get("parameters") or {"type": "object", "properties": {}, "required": []}
            specs.append(
                self._ToolSpec(
                    name=self._prefix + name,
                    description=str(tool.get("description") or name),
                    title=str(tool.get("title") or name),
                    parameters_schema=schema,
                    provider_name=self.provider_name,
                    provider_type=self.provider_type,
                    metadata={
                        "visibility": "visible" if self._visible else "deferred",
                        "registry_original_name": name,
                    },
                )
            )
        return specs

    async def invoke(self, invocation: Any, context: Any) -> Any:
        original = getattr(invocation, "tool_name", "")
        if original.startswith(self._prefix):
            original = original[len(self._prefix) :]
        try:
            text = await self._run(original, dict(invocation.arguments or {}), context)
        except Exception as exc:  # noqa: BLE001 - 工具失败要变成给模型看的结果，不能炸脑子
            return self._Result(
                tool_name=invocation.tool_name,
                success=False,
                error_message=f"{self.provider_name} 工具 {original} 调用失败: {type(exc).__name__}: {exc}",
            )
        return self._Result(tool_name=invocation.tool_name, success=True, content=text or "（无输出）")

    async def close(self) -> None:
        return None


_MIRRORS: list[Any] = []


def install_mirror(mirror: Any) -> dict:
    """把镜像 Provider 挂到当前及今后每一个会话运行时上。

    可以有多个镜像（AstrBot 工具一个、astrmai 原生插件一个），所以包装后的方法
    每次都会登记列表里的全部镜像，而不是只认第一个。
    """

    report: dict[str, Any] = {}
    from src.chat.heart_flow.heartflow_manager import heartflow_manager
    from src.maisaka.runtime import MaisakaHeartFlowChatting

    if not any(m is mirror for m in _MIRRORS):
        _MIRRORS.append(mirror)

    method = getattr(MaisakaHeartFlowChatting, "_register_tool_providers", None)
    if method is None:
        report["hook"] = "FAIL 上游没有 _register_tool_providers，需要重新适配"
        return report
    if not getattr(method, "_astrmai_wrapped", False):

        def patched(self: Any) -> None:
            method(self)
            for item in list(_MIRRORS):
                try:
                    self._tool_registry.register_provider(item)
                except Exception as exc:  # noqa: BLE001
                    from src.common.logger import get_logger

                    get_logger("astrmai").warning(f"注册工具镜像失败: {exc}")

        patched._astrmai_wrapped = True  # type: ignore[attr-defined]
        MaisakaHeartFlowChatting._register_tool_providers = patched  # type: ignore[method-assign]
        report["hook"] = "wrapped"
    else:
        report["hook"] = "already wrapped"

    attached = 0
    runtimes = getattr(heartflow_manager, "heartflow_chat_list", {}) or {}
    for runtime in list(runtimes.values()):
        try:
            runtime._tool_registry.register_provider(mirror)
            attached += 1
        except Exception:  # noqa: BLE001
            pass
    report["existing_runtimes_attached"] = attached
    report["mirrors"] = [getattr(m, "provider_name", "?") for m in _MIRRORS]
    return report


def collect_astrbot_tools(manager: Any) -> list[dict]:
    """枚举 AstrBot 侧可暴露的工具（跳过我们自己注入的 mai_* 与未启用的）。"""

    out = []
    for tool in getattr(manager, "func_list", []) or []:
        name = getattr(tool, "name", "")
        if not name or name.startswith(MIRROR_PREFIXES):
            continue
        if not getattr(tool, "active", True):
            continue
        out.append(
            {
                "name": name,
                "description": getattr(tool, "description", "") or name,
                "parameters": getattr(tool, "parameters", None) or {"type": "object", "properties": {}, "required": []},
            }
        )
    return out


async def call_astrbot_tool(manager: Any, name: str, args: dict, event: Any) -> str:
    """执行一个 AstrBot 工具并把结果收敛成文本给麦麦。

    处理器既可能是协程，也可能是异步生成器（yield 出 MessageEventResult/字符串）。
    """

    tool = next((t for t in getattr(manager, "func_list", []) or [] if getattr(t, "name", "") == name), None)
    if tool is None or tool.handler is None:
        return f"未找到 AstrBot 工具: {name}"

    result = tool.handler(event, **args)
    chunks: list[str] = []
    if inspect.isasyncgen(result):
        async for item in result:
            chunks.append(_textify(item))
    elif inspect.isawaitable(result):
        chunks.append(_textify(await result))
    else:
        chunks.append(_textify(result))

    captured = ""
    try:
        captured = event.get_result().get_plain_text(with_other_comps_mark=False) or ""
    except Exception:  # noqa: BLE001 - 某些事件没有结果可取
        pass
    text = "\n".join(c for c in (*chunks, captured) if c and c.strip())
    return text.strip() or "（工具执行完成，无文本输出）"


def _textify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    chain = getattr(value, "chain", None)
    if chain is not None:
        try:
            from astrbot.core.message.message_event_result import MessageChain

            return MessageChain(chain=list(chain)).get_plain_text()
        except Exception:  # noqa: BLE001
            return str(value)
    return str(value)


def find_maibot_runtime(*, is_group: bool, group_id: str, user_id: str) -> Any:
    """按会话找到麦麦已有的会话运行时（不新建，避免造出没有上下文的脑子）。"""

    from src.chat.heart_flow.heartflow_manager import heartflow_manager

    wanted = str(group_id if is_group else user_id)
    for runtime in list(getattr(heartflow_manager, "heartflow_chat_list", {}).values()):
        stream = getattr(runtime, "chat_stream", None)
        if stream is None:
            continue
        if is_group:
            if str(getattr(stream, "group_id", "") or "") == wanted:
                return runtime
        else:
            if str(getattr(stream, "user_id", "") or "") == wanted:
                return runtime
    return None


async def run_maibot_tool(name: str, args: dict, event: Any) -> str:
    """AstrBot 侧调用麦麦工具的执行体。"""

    from src.core.tooling import ToolExecutionContext, ToolInvocation

    is_group = not event.is_private_chat()
    runtime = find_maibot_runtime(
        is_group=is_group,
        group_id=str(event.get_group_id() or ""),
        user_id=str(event.get_sender_id() or ""),
    )
    if runtime is None:
        return "麦麦这边还没有这个会话的运行时，先用普通聊天让她说过话再试。"

    session_id = getattr(runtime, "session_id", "") or ""
    invocation = ToolInvocation(
        tool_name=name,
        arguments=dict(args or {}),
        call_id=f"mai-{uuid.uuid4().hex[:12]}",
        session_id=session_id,
        stream_id=session_id,
    )
    context = ToolExecutionContext(
        session_id=session_id,
        stream_id=session_id,
        is_group_chat=is_group,
        group_id=str(event.get_group_id() or ""),
        user_id=str(event.get_sender_id() or ""),
        platform=str(getattr(runtime.chat_stream, "platform", "") or ""),
    )
    result = await runtime._tool_registry.invoke(invocation, context)
    return result.get_history_content() or "（麦麦工具无输出）"


def list_maibot_tools() -> list[dict]:
    """枚举麦麦的全部工具：内置 + 自家插件（out-of-process）+ 其它 provider。

    插件工具走 component_query_service（同步、不依赖会话运行时），所以麦麦刚装的插件
    也能被重新扫描到。外部名冲突时用 provider 短名后缀区分，并保留 target=原始名，
    真正调用时按 target 派发。
    """

    out: list[dict] = []
    seen: dict[str, str] = {}

    def add(spec_name: str, description: str, schema: Any, provider: str) -> None:
        if not spec_name or spec_name.startswith(MIRROR_PREFIXES):
            return
        external = spec_name
        if external in seen:
            slug = re.sub(r"[^0-9a-zA-Z]+", "_", provider).strip("_")[-24:] or "other"
            external = f"{spec_name}_{slug}"
            if external in seen:
                return
        seen[external] = provider
        out.append(
            {
                "name": external,
                "target": spec_name,
                "description": description or spec_name,
                "parameters_schema": schema or {"type": "object", "properties": {}, "required": []},
                "provider": provider,
            }
        )

    try:
        from src.maisaka.builtin_tool import get_all_builtin_tool_specs

        for spec in get_all_builtin_tool_specs():
            add(spec.name, spec.description, spec.parameters_schema, "maisaka_builtin")
    except Exception:  # noqa: BLE001
        pass

    try:
        from src.plugin_runtime.component_query import component_query_service

        for spec in component_query_service.get_llm_available_tool_specs(None).values():
            add(spec.name, spec.description, spec.parameters_schema, spec.provider_name or "plugin")
    except Exception:  # noqa: BLE001
        pass

    return out


def make_maibot_function_tools(specs: list[dict]) -> list:
    """把麦麦工具表包成 AstrBot 的 FunctionTool 列表（过滤掉镜像前缀，防回环）。"""

    from astrbot.core.agent.tool import FunctionTool

    tools = []
    for spec in specs:
        name = str(spec.get("name", ""))
        if not name or name.startswith(MIRROR_PREFIXES):
            continue
        target = str(spec.get("target") or name)

        async def handler(event, _target=target, **params):
            return await run_maibot_tool(_target, params, event)

        tools.append(
            FunctionTool(
                name=MB_PREFIX + name,
                description=str(spec.get("description") or target),
                parameters=spec.get("parameters_schema") or {"type": "object", "properties": {}, "required": []},
                handler=handler,
            )
        )
    return tools
