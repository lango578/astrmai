# astrmai

![astrmai 图标：琥珀（AstrBot）与紫罗兰（麦麦）两条通道各伸出一个插头，被一道青色总线（融合层）强行对接](assets/astrmai-icon.svg)

把 **[AstrBot](https://github.com/AstrBotDevs/AstrBot)**（平台层 / 插件生态 / 命令）和 **[MaiBot 麦麦](https://github.com/MaiM-with-u/MaiBot)**（群聊人格引擎）**强行合进同一个进程**：一个 asyncio 事件循环、一条 NapCat 反向 WebSocket 连接、两个脑子分工应答。

> 现状：**研究可用的 0.1.0**，不是开箱即用的产品。核心链路有 15 项离线测试兜着（含端到端与攻击面取证），但请自己验一遍再接线上群。

## 它解决什么

两家各跑一套时会撞三样东西：QQ 连接（一个 bot 号只能挂一条反向 WS）、事件循环、以及"同一条消息谁回答"。astrmai 的答法是：

- **一条连接**：AstrBot 的 aiocqhttp 平台照常收消息，麦麦不再有自己 transport（`DriverKind.LOCAL` 驱动 + 直调 `chat_bot.message_process`）。
- **一个循环**：麦麦以库的形式在 AstrBot 的进程内启动；它的进程监督器 `src/runtime.py` 被显式禁用（那东西会抢信号、chdir、`os._exit`）。
- **一个裁决层**：`astrmai/gate.py` 逐条判归属 —— 命令前缀或被插件消费的消息留在 AstrBot，其余自然对话交给麦麦，并由 `event.stop_event()` 阻止 AstrBot 再答一遍。**同一条消息不会被答两次**，这是端到端测试里的一条硬断言。

## 快速开始

```bash
# 1) 取两个上游（版本按实测钉住，见 docs/DEPLOY.md）
git clone --depth 1 https://github.com/AstrBotDevs/AstrBot vendor/AstrBot-master
git clone --depth 1 -b v1.3.18 https://github.com/MaiM-with-u/MaiBot MaiBot-main

# 2) 装依赖（同一个 venv 里两套 requirements 实测零冲突）
python -m venv .venv && .venv/bin/pip install -r vendor/AstrBot-master/requirements.txt -r MaiBot-main/requirements.txt

# 3) 配模型：编辑 MaiBot-main/config/model_config.toml（OpenAI 兼容端点）

# 4) 起
ASTRMAI_PANEL=on .venv/bin/python run_real.py --allow <你的QQ号> --account <机器人QQ号>
```

- NapCat 侧：网络构建器 → 新建 Websocket 客户端 → `ws://127.0.0.1:8123/ws`（先把原来连向 AstrBot 的那条停掉，别两个脑子抢一条连接）。
- Windows 上的命令写法（`.venv\Scripts\python.exe` + `$env:`）见 [docs/DEPLOY.md](docs/DEPLOY.md) 开头。
- 控制台：<http://127.0.0.1:18124/astrmai/> 初始口令 `astrmai-init`，**登录后会强制你改掉**；该默认口令只接受来自本机的登录。
- QQ 里：`/mai status`、`/mai panel`、`/mai 许可证`、`/mai market astrbot <关键词>`、`/mai install astrbot <id>`、`/mai load astrbot <目录名>`。

## 你能拿到什么

| 能力 | 位置 |
|---|---|
| 融合桥接（入站/出站/裁决/驱动/宿主） | `astrmai/` |
| 双向工具联邦 `mai_* / ab_* / abcmd_*` | `astrmai/federation.py`、`astrmai/command_tools.py` |
| astrmai 原生插件 SDK（一次开发，两边可用 `nm_*`） | `astrmai/sdk.py`、`astrmai_plugins/astrmai_demo/` |
| 官方市场：两家目录各一页，打开即列出、可翻页、已装会标记，点行安装并加载 | `astrmai/market.py`、`astrmai/panel_static/app.js` |
| 许可证取证（判不出就标 unknown，不猜） | `astrmai/licensing.py` |
| 运行时加载（不重启把插件装进两个脑子）+ pip 依赖政策 | `astrmai/loader.py` |
| 子进程回收（Windows 作业对象，硬杀也连坐） | `astrmai/jobguard.py` |
| Web 控制台（多级菜单 + 负载首屏 + scrypt 凭据；含平台连接状态、麦麦自主性开关、TLS 上限与记忆库规模、命令镜像清单、加载拒绝留痕、runner 重启留痕） | `astrmai/panel.py`、`astrmai/panel_static/` |
| AstrBot 功能面：服务提供商（建/改/启停/设默认/连通测试）、人格、对话历史浏览、定时任务 | `astrmai/panel_data.py`、docs/COVERAGE.md |
| 离线测试套件（15 项，含攻击面取证） | `tests/`、`run_tests.py` |

## 默认就是保守的

- **群聊默认沉默**，私聊走硬白名单（`ASTRMAI_ALLOWED_USERS`）；不在名单里的会话既不转发也不抑制。
- **上游遥测被掐断**（麦麦硬编码往第三方站点发心跳）。
- **市场查询与遥测同域**（`hyybuth.xyz`），`ASTRMAI_MARKET_NETWORK=0` 是**两条链路一起关**的总闸（AstrBot 目录/下载 + 麦麦目录），关掉后本地缓存也不能当后门；搜索结果里也会明写出网去向。
- **永不代你 pip**：需要装依赖的插件在调用加载器之前就被拒绝，理由会返回。
- **面板口令只存 scrypt 哈希**，明文不落盘、不进日志、不进任何响应；非回环绑定必须有 TLS，否则拒绝启动。
- 完整开关清单见 [docs/CONFIG.md](docs/CONFIG.md)，威胁模型见 [docs/SECURITY.md](docs/SECURITY.md)。

## 测试

```bash
python run_tests.py      # 15 项离线：把模型指向本地桩，跑完自动还原，不花你的额度
```

覆盖：配置切换幂等、子进程回收（**带对照组**）、消息段转换、市场解析与越界删除保护、许可证识别（认不出就标 `unknown`，不猜）、双向联邦与真 RPC 往返、命令镜像、运行时加载、原生插件、面板 API 形状（含 provider/人格/对话/定时任务四块）、凭据与强制改密、攻击面（未认证、CSRF、路径穿越、畸形参数、爆破、明文零落地）、以及真 AstrBot + 假 NapCat 的单连接端到端。

## 许可与署名

- astrmai 自身：**AGPL-3.0-or-later**（见 [LICENSE](LICENSE)）。
- 上游：AstrBot = AGPL-3.0-or-later，MaiBot = GPL-3.0-or-later（两份原文在 `docs/`，说明见 [THIRD_PARTY.md](THIRD_PARTY.md)）。
- **合并作品只能整体按 AGPL-3.0-or-later 分发**（GPLv3 §13 允许 GPL 代码并入 AGPL 工程，反向不行）。分发时要提供完整对应源码（含胶水代码与随包插件源码），保留 LICENSE 与声明，不得追加限制。
- AGPL §13 还有一条：**程序通过网络与用户交互**就要向那些用户提供源码 —— 挂在群里给别人用严格解读下适用；只自己私聊用不触发发放义务。
- 本仓库**不包含**两个上游的源码，请按 `docs/DEPLOY.md` 自行获取；控制台界面也是原创，未使用两家的组件、字体、图标或品牌素材。

## 已知边界

- 面板是单管理员控制台，没有多用户/权限体系。登录进去能看见三类内容：**完整对话历史**（从 `data/data_v4.db` 现读，只给读、没做删除入口）、裁决流水 200 条（每条前 60 字预览，内存里，重启清空）、日志环 400 条。给账号之前按这三条想，明细见 docs/SECURITY.md。
- 面板只做"融合层 + AstrBot 常用面板功能"的事：AstrBot 的服务提供商/人格/对话历史/定时任务已经可以在面板里改（直连其运行时管理器，落 cmd_config.json 与 data_v4.db）；模型绑定仍只读展示（改要动麦麦自己的 `config/model_config.toml`）；知识库、备份、子代理还是两家的地盘 —— 覆盖矩阵见 docs/COVERAGE.md，没做的都写了理由。
- 麦麦的自主发言循环会周期性自己说话，裁决层管"单条消息谁答"，管不住她的主动聊天。
- 端到端取证用的是本地桩模型；接真模型跑实机时请自己先看几轮。
