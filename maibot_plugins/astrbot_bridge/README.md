# astrbot_bridge —— 让麦麦也能用 AstrBot 的插件

方向与 astrmai 相反：**astrmai 是把麦麦塞进 AstrBot 进程**，这个插件是给**纯麦麦用户**用的 ——
你只跑麦麦，但想用 AstrBot 生态里的插件工具。

它不嵌 AstrBot，而是连一个跑着 **astrmai 控制台**的 AstrBot 实例，把对面的工具列出来、按需调用。

## 为什么走 HTTP 而不是就地嵌一套 AstrBot

麦麦的插件跑在**独立的 runner 子进程**里。在那个子进程里再拉一套 AstrBot 核心，意味着第二个数据库、
第二份配置、还得给 AstrBot 一个平台连接 —— 为了"用几个插件"付出整个第二实例的代价，不划算。
runner 是常驻的（不是每次调用起一个进程），所以一个 `httpx` 客户端可以跨调用活着，HTTP 桥就够了。

## 装

> **目录名必须是 `astrbot_bridge`**：麦麦按插件目录名做 IPC 路由与配置落盘，而这个仓库叫
> `maibot-plugin-astrbot-bridge`，所以 clone 时要显式指定目标目录名，别直接 clone 进 `plugins/`。

```bash
git clone https://github.com/lango578/maibot-plugin-astrbot-bridge <麦麦>/plugins/astrbot_bridge
```

（下载 zip 解压也一样，把最外层目录改名成 `astrbot_bridge`。）

1. 对面（AstrBot 那边）要满足：
   - astrmai 在跑且控制台开着：`ASTRMAI_PANEL=on`；
   - 设了机器 token：`ASTRMAI_PANEL_TOKEN=<一串值>`；
   - **控制台初始口令必须已经改掉** —— 没改之前所有端点都不放行，这个插件也会拿到 403。
2. 在麦麦的插件配置里填：

| 字段 | 说明 |
|---|---|
| `bridge.base_url` | 默认 `http://127.0.0.1:18124/astrmai/api`。跨机就填对面地址，**跨机必须走 TLS**（astrmai 侧配证书，或用隧道） |
| `bridge.token` | 与对面 `ASTRMAI_PANEL_TOKEN` 同一个值 |
| `bridge.target` | 默认执行会话（群号/QQ 号）。AstrBot 的工具处理器要靠事件上下文才能跑，所以必须有一个会话可挂 |
| `bridge.timeout_seconds` | 单次请求超时，默认 30 |

## 麦麦会拿到三个工具

| 工具 | 干什么 |
|---|---|
| `astrbot_tools` | 列出对面可用的工具/命令（含来源分组与说明） |
| `astrbot_call` | 执行其中一个：`tool` + `args_json`（JSON 对象字符串）+ 可选 `target` |
| `astrbot_status` | 对面 booted / 转发数 / 出站数 / 会话数，排查用 |

三个都声明成默认可见（`visibility="visible"`），不然麦麦要先 `tool_search` 才发现得到。

## 安全边界（认真读这段）

- 这个桥的**执行能力等价于在对面跑插件代码**。所以：token 不要写进任何会公开的文件；
  跨机调用必须有 TLS；对面的控制台本身是单管理员模型，拿到 token 等于拿到执行权。
- 每次执行都必须带 `target`（或配置里的默认值）。对面**只接受它自己记得的会话事件**：
  那个会话最近没有消息进来就直接拒绝，不会随便抓一个别人的会话凑数 —— 那是跨会话串权限。
- 对面的响应经过脱敏过滤器，凭据类字段不会回传；但**工具输出本身是什么，取决于对面装了什么插件**。
  你不信任某个 AstrBot 插件，就别让麦麦调它。
- 本插件只声明 `config.get` 一个宿主能力；出网走的是插件自己的 httpx（麦麦对插件网络没有沙箱，
  所以这条要你自己把关）。

## 已知边界

- 对面 AstrBot 装了新插件 → 调 `astrbot_tools` 就能看到（清单是实时取的），不需要重载本插件。
- 工具参数是 JSON 字符串而不是强 schema：麦麦的 planner 看不到每个远端工具各自的参数表，
  要先 `astrbot_tools` 查。这是"一个通用 call 工具"换掉"每次重启才能刷新工具表"的代价，我选了前者。
- 对面没开 astrmai 控制台时，三个工具都会回一句"连不上对面"，不会静默失败。
