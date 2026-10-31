# 第三方与许可

## 上游两个引擎

| 项目 | 许可证 | 在本仓库里的位置 |
|---|---|---|
| [AstrBot](https://github.com/AstrBotDevs/AstrBot) | **AGPL-3.0-or-later** | 不含，自备（实测 4.29.3） |
| [MaiBot / 麦麦](https://github.com/MaiM-with-u/MaiBot) | **GPL-3.0-or-later** | 不含，自备（实测 1.3.18） |

两份许可证原文放在 [docs/upstream-astrbot-AGPL-3.0.txt](docs/upstream-astrbot-AGPL-3.0.txt) 与 [docs/upstream-maibot-GPL-3.0.txt](docs/upstream-maibot-GPL-3.0.txt)，便于分发时随包。

麦麦另有一份 EULA（V1.3）和一份隐私条款（V1.2）。EULA 第 2.1 条自己写明主程序按 GPLv3 发布、"可以自由使用、修改、分发，但必须遵守对应开源许可证的要求"，第 2.2 条把第三方部件的许可交回各自声明 —— 也就是说**这两份文件没有给 GPLv3 追加限制**。它们不随本仓库分发（属于上游仓库），但分发合并版时请连同上游那份一起看。

## 为什么不带上游源码

1. **许可**：把别人的 GPL/AGPL 仓库原样重新打包成"我的发布"，除了让源码体积翻几十倍，还平白多出 attribution 与"完整对应源码"的义务纠纷。
2. **卫生**：本机那份上游目录里带着自己的 `config/`、`data/`、跑出来的日志和可能的密钥。发布它 = 发布我的机器。
3. 上游本来就能取，按 [DEPLOY.md](docs/DEPLOY.md) 第 2 节两条命令的事。

## 合并作品的许可口径

**astrmai 自身 = AGPL-3.0-or-later**（见 [LICENSE](LICENSE)）。这不是随便挑的，是被上游决定的：

- MaiBot 是 GPLv3-or-later。GPLv3 §13 明确允许把 GPLv3 代码并入 AGPLv3 工程（反向不允许）。
- 所以融合后的整体只能按 **AGPL-3.0-or-later** 分发。
- 分发时的义务：**提供完整对应源码**，包含胶水代码和随包分发的插件源码；保留 LICENSE 与版权声明；不得追加额外限制。
- **AGPL §13 那条要单独看**：程序通过网络与用户交互，就要给那些用户提供对应源码。把融合版挂在群里让别人也能发消息用，严格解读下适用这一条 —— 请连带 astrmai 与两个上游的源码位置一起给出。只是自己私聊用，不触发发放义务。

## 插件生态

三个生态的插件许可证**不统一**，也不该由我替作者声明：

- **AstrBot 官方目录**根本不带 `license` 字段 → 搜索结果标 `unknown`，安装时从包内 `LICENSE` / `metadata.yaml` / README 徽章判定。
- **麦麦官方 release 索引**带 `manifest.license` → 直接采用；缺失时同样安装时判定。
- 判定逻辑在 [astrmai/licensing.py](astrmai/licensing.py)：认 AGPL/LGPL/GPL/MPL/Apache/MIT/BSD（BSD 按条款数区分 2/3  clause）/Unlicense/CC0/ISC；**遇到 EULA/专有声明或证据不足就标 `unknown`，不猜**。参数与依据会一起落盘（插件目录里的 `.astrmai-license.json`）。
- 看清单：面板「许可证清单」页，或 QQ 里 `/mai 许可证`。

**你要分发的话**：`unknown` 的那几项就是不能动的部分 —— 要么去问作者，要么把插件从分发包里摘出去，不要"看起来像 MIT 就当前提"。

`astrmai_plugins/astrmai_demo/` 是我自己写的示例插件，声明 AGPL-3.0-or-later，随包分发时和主体同许可。

## 界面

控制台界面是**原创实现**：手写 HTML/CSS/JS，零框架、零构建、零外链资源（页面里没有任何第三方 URL、字体或图标引用，测试会断言这一条）。刻意与两家自带面板不同：不复用它们的组件与代码，路径另起 `/astrmai/` 前缀（AstrBot 用 `/api/v1`，麦麦用 `/api/chat` 等），版式与配色不模仿。

## 会被请求的外部服务

 astrmai 主动出网只有三处，全部可关或不需要：

| 目标 | 干什么 | 怎么关 |
|---|---|---|
| 你配置的模型端点 | 推理 | 这是你自己的依赖，关不掉就没得聊 |
| AstrBot 插件目录 / 官方合集 + `api.github.com` + `codeload.github.com` | 只有你点「搜索/安装」时才请求 | 不点就不请求 |
| 麦麦官方市场 `hyybuth.xyz:10059` | 同上；**与麦麦遥测同主机**（遥测在 `:10058`） | `ASTRMAI_MARKET_NETWORK=0` 整块关掉 |
| 麦麦上游遥测 | 上游硬编码的心跳 | 默认已被 astrmai 掐断；`ASTRMAI_TELEMETRY=on` 才放行 |
