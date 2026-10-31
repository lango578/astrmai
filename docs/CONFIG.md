# 配置

三层，越上面越优先：

1. **环境变量 `ASTRMAI_*`** —— 进程级，钉死了就不给面板改（面板会明写哪些字段"被环境变量占了"）。
2. **面板的「设置」页** —— 改的是运行中的闸门，同时落 `astrmai_state/settings.json`，重启自动读回。
3. **两个上游自己的配置文件** —— 面板刻意不碰：模型、人格、提示词、AstrBot 的平台/provider 都请到各自文件里改，改错了也不该由一个融合控制台来背。

## 环境变量

### 引擎

| 变量 | 默认 | 作用 |
|---|---|---|
| `ASTRMAI_ENABLE` | `1` | `0` 时星插件注册了但不启动麦麦宿主（用来临时关掉融合，保留 AstrBot 本体） |
| `ASTRMAI_WORKSPACE` | 仓库根 | 工作区，其它默认路径的锚点 |
| `ASTRMAI_MAI_ROOT` | `<工作区>/MaiBot-main` | 麦麦源码位置 |
| `ASTRMAI_NATIVE_PLUGINS` | `<工作区>/astrmai_plugins` | astrmai 原生插件目录 |
| `ASTRMAI_STATUS_FILE` | 未设＝不写 | 把运行状态写成一个 JSON，给外部健康探针和靶场用；`run_real.py` 会指到 `e2e/real/astrmai_status.json`。面板挂了也能靠它看现场 |

### 谁能跟它说话（默认全是保守的）

| 变量 | 默认 | 作用 |
|---|---|---|
| `ASTRMAI_PLATFORM` | `qq` | 目前只做了 QQ（aiocqhttp）这条路 |
| `ASTRMAI_ACCOUNT_ID` | 空 | 机器人自己的 QQ 号，用于 @ 与引用判定；留空就只能靠群聊自然对话那条路 |
| `ASTRMAI_ALLOWED_USERS` | 空 | **私聊硬白名单**，逗号分隔。空＝不限制（不建议）；非空＝名单外一律沉默 |
| `ASTRMAI_ALLOW_PRIVATE` | `1` | 私聊总开关 |
| `ASTRMAI_ALLOW_GROUPS` | `0` | 群聊总开关。**默认关**：群消息既不转发也不抑制 |
| `ASTRMAI_GROUPS` | 空 | 群号白名单，逗号分隔；空＝总开关开了就全放行 |
| `ASTRMAI_ADMINS` | 空 | 能用 `/mai market/install/load/panel` 的号；留空则退回私聊白名单。**两个都空＝`/mai` 一律不放行**（fail-closed，会回一句告诉你去哪儿配）—— 装插件和改运行状态是写操作，默认不开放 |

### 控制台

| 变量 | 默认 | 生效时机 |
|---|---|---|
| `ASTRMAI_PANEL` | `off` | 启动 |
| `ASTRMAI_PANEL_HOST` | `127.0.0.1` | 启动 |
| `ASTRMAI_PANEL_PORT` | `18124` | 启动 |
| `ASTRMAI_PANEL_PASSWORD` | 空 | 启动 |
| `ASTRMAI_PANEL_TOKEN` | 空 | 启动 |
| `ASTRMAI_PANEL_CERT` / `ASTRMAI_PANEL_KEY` | 空 | 启动（PEM 路径） |
| `ASTRMAI_PANEL_INSECURE` | 空 | 启动（只有非回环且无证书时才有意义） |
| `ASTRMAI_PANEL_TRUST_PROXY` | 空 | 每次请求 |
| `ASTRMAI_PANEL_PUBLIC_HOST` | 空 | 每次请求 |

口令这一条要写清，因为它的默认行为是**故意的**：

- 不给 `ASTRMAI_PANEL_PASSWORD` → 用公开写死的初始口令 `astrmai-init`，`must_change` 挂上（进去必须先改），并且**这个口令只接受本机登录**。
- 给了 → 用它，且不强制改密（否则"我在 systemd 里配了口令，结果每次启动都要我去面板改一遍"）。少于 12 位会被**忽略**并大声说明，退回默认口令路径 —— 宁可用一个只在本机有效的已知口令，也不静默接受一个弱得离谱的。
- 环境变量给的值和已存记录不一致 → **按环境变量重建**，旧口令立刻失效。这一步会打印出来，不会静默吞掉你改了 token 却进不去的那种坑。
- token（给脚本/自动化用的 `X-Astrmai-Token`）**不会自动生成**：没人知道的随机 token 等于凭空造个用不上、还会挡掉你之后真正配置的那份的凭据。少于 24 位同样被忽略并说明。

反代必须同时设 `ASTRMAI_PANEL_TRUST_PROXY=1` 和 `ASTRMAI_PANEL_PUBLIC_HOST=<对外域名>`，理由见 [DEPLOY.md 第 7 节](DEPLOY.md)。

### 出网与工具暴露

| 变量 | 默认 | 作用 |
|---|---|---|
| `ASTRMAI_TELEMETRY` | `off` | 上游遥测硬编码往第三方站点发心跳；`on` 才放行。面板上改它属于"下次启动生效" |
| `ASTRMAI_MARKET_NETWORK` | `1` | `0` 关掉**整个市场的出网**：AstrBot 那条（api.soulter.top / 合集 codeload）与麦麦那条（hyybuth.xyz:10059，与遥测 :10058 同主机）一起关，目录、下载、许可证探测全都拒绝。**这个开关赢过本地缓存**：设成 0 后即使 `cache/` 里有整本目录也照样拒绝，不会拿旧缓存当后门 |
| `ASTRMAI_MEDIA_ROOTS` | 空 | 消息里出现**本地路径**（NapCat 有时直接给文件路径）时允许读哪些目录，逗号分隔。默认空＝一个本地路径都不读：否则一条 QQ 消息里的 `file://C:\...` 就能把进程可读的任意文件 base64 进模型 |
| `ASTRMAI_MEDIA_MAX_BYTES` | `8388608` | 单段媒体的大小上限（超限即丢段并记原因），流式读、不一次性进内存。同时挡住"发个 2G 文件把你内存打满" |

媒体取回只走 `http`/`https`，拒 `file://`、`\\` 之类的路径；直连 IP 是链路本地/保留段（`169.254.0.0/16`、`metadata.google.internal`）也拒。跳转之后会再判一次主机名。取回跑在工作线程里，不压在两个脑子共用的事件循环上（`astrmai/inbound.py` 的 `onebot_to_maim_dict_async`）。

两家官方目录都**整本缓存在 `cache/`（TTL 30 分钟）**，翻页与筛选在本地切片，不重复出网（实测麦麦那个接口不理 `limit`，一次就给你整本 399 条，逐页出网纯属浪费）。命中缓存时面板会在页面上写"这一页来自缓存，本次没有出网"，并提供「重新载入目录」强制重取 —— 不许把没出网说成出网。
| `ASTRMAI_AB_TOOLS_VISIBLE` | `0` | `1`＝把 `ab_*`（AstrBot 工具→麦麦）直接塞进麦麦的工具表 |
| `ASTRMAI_NATIVE_VISIBLE` | `0` | 同上，`nm_*`（astrmai 原生插件工具） |
| `ASTRMAI_COMMAND_TOOLS` | `deferred` | `abcmd_*`（AstrBot 命令→麦麦工具）的暴露方式 |

**默认全部走麦麦的延迟发现（`tool_search`）**。工具表一次性摊开会把她的 planner 挤爆 —— 上下文被几十条工具说明占住，回复质量和 token 都难看。要图省事全摊开，把上面三个开关设成 visible，代价自己承担。

### 上游那几个也用得到的变量

`ASTRBOT_ROOT`（AstrBot 的运行目录，`run_real.py` 会生成并指定）、`ASTRBOT_DISABLE_ANALYTICS=1`（AstrBot 自己的分析开关，测试里默认设上）。

## 面板能改什么

| 字段 | 当场生效 | 说明 |
|---|---|---|
| 允许群聊 / 允许私聊 | 是 | 改的就是 `cfg.group_enabled/private_enabled`，下一条消息就用 |
| 私聊硬白名单 / 群号白名单 | 是 | 集合直接换掉 |
| 命令前缀（归 AstrBot） | 是 | 裁决闸门读的就是这个元组；端到端测试里有一条专门验"面板改了前缀，流水真的按新前缀分家" |
| 麦麦群聊发言频率 talk_value（0–1） | 是 | 直接落到 `global_config.chat.reply_timing.talk_value`（她每轮调度现读它）；0=群聊只记不说。重启后由宿主启动日志从 settings.json 读回 |
| 麦麦私聊暂停思考 | 是 | `private_reply_paused`：开了只收消息维护记忆，不进思考。注意管不住她自己的主动发言循环（面板明写了这一点） |
| 遥测开关 | **否** | 本次运行的地址已经定过了，面板会明写"下次启动生效"，不假装生效 |
| 模型 / 人格 / 平台 / provider | 不提供 | 属于两个上游自己的配置，面板拒绝改（返回 `refused`），不是"藏在某个二级菜单里" |

被环境变量钉住的字段，面板改不动，也会告诉你为什么改不动。

## 想要不同的部署形态

- **只想跑 AstrBot，不要麦麦**：`ASTRMAI_ENABLE=0`，或者干脆别装这个星插件。
- **只想本机看面板**：默认形态就是 —— 别设 `PANEL_HOST`，别设 `INSECURE`。
- **脚本自动点面板 API**：给 `ASTRMAI_PANEL_TOKEN`（≥24 位），请求带 `X-Astrmai-Token`。写操作还需要同源 `Origin`，脚本里请显式带上。
- **多管理员/分级权限**：没有。这是单管理员控制台，见 README「已知边界」。
