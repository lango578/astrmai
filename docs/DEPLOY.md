# 部署

astrmai 不是独立程序，它是**装在 AstrBot 里的一个星插件**，把麦麦当库拉进同一个进程。所以部署 = 备好两个上游 + 一个共享 venv + 一条 QQ 连接。

实测组合：**AstrBot 4.29.3** + **MaiBot 1.3.18** + Python 3.12（麦麦要求 ≥3.12）+ NapCat。版本换了别慌，先看第 8 节自检能不能跑通。

**本文命令用 Linux/macOS 写法（`.venv/bin/python`）；Windows（PowerShell）把路径换成 `.venv\Scripts\python.exe`，环境变量用 `$env:NAME="值"` 先设一行**：

```powershell
$env:ASTRMAI_PANEL = "on"
& .\.venv\Scripts\python.exe run_real.py --allow 12345678 --account 87654321
```

Windows 上别直接敲 `python`：系统里可能是微软商店的占位程序（敲了没反应、退出码 49）。一律用 venv 里那个解释器的全路径。

## 1. 目录形状

`run_real.py` 按这个布局找上游，路径不对就用环境变量覆盖：

```
<工作区>/
  astrmai/                 # 桥接包（本仓库）
  astrmai_plugin/          # AstrBot 星插件：融合入口（本仓库）
  astrmai_plugins/         # astrmai 原生插件目录（本仓库）
  vendor/AstrBot-master/   # 自备：AstrBot 源码
  MaiBot-main/             # 自备：麦麦源码
  run_real.py              # 启动器（本仓库）
```

- 上游目录名随便叫，用 `ASTRMAI_MAI_ROOT` 指过去；AstrBot 的路径是 `run_real.py` 里 `sys.path` 加的，改名字就要改这一行，或者保持 `vendor/AstrBot-master`。
- **本仓库不含两个上游的源码**（许可与署名口径见 [THIRD_PARTY.md](../THIRD_PARTY.md)）。

## 2. 取上游

```bash
git clone --depth 1 https://github.com/AstrBotDevs/AstrBot vendor/AstrBot-master
git clone --depth 1 -b v1.3.18 https://github.com/MaiM-with-u/MaiBot MaiBot-main
```

拿不到 git（有些网络只放通 API/下载）就下 zip 解开，目录名对齐即可。

## 3. 依赖

两份 requirements 装进**同一个 venv**，实测零冲突：

```bash
python -m venv .venv
.venv/bin/pip install -r vendor/AstrBot-master/requirements.txt -r MaiBot-main/requirements.txt
```

麦麦的插件是 out-of-process 的 runner，需要它自己那套依赖；装不上时先看 `MaiBot-main/requirements.txt` 里有没有编译型包（numpy、onnx 之类）。

## 4. 模型

麦麦的模型走 `MaiBot-main/config/model_config.toml`（OpenAI 兼容）。最少要写一个 provider 和一个模型，再把任务指过去：

```toml
[[api_providers]]
name = "my-provider"
base_url = "https://你的兼容端点/v1"
api_key = "密钥放这里，只在这个文件里出现"
client_type = "openai"
auth_type = "bearer"

[[models]]
name = "my-chat"
model_type = "text"
model_identifier = "实际的模型 id"
api_provider = "my-provider"

[model_task_config.replyer]   # 回复：直接影响麦麦的表现
model_list = ["my-chat"]

[model_task_config.utils]     # 概括/整理等小任务：建议换个快的小模型
model_list = ["my-chat"]
```

`planner`（决定麦麦的行动）需要一个有 Agent 能力的模型；`vlm`/`embedding`/`voice` 留空就是不启用，不会崩。**面板只报「某个任务绑了没绑、有没有密钥」，永远不输出密钥值。**

## 5. QQ 连接

AstrBot 起一个反向 WebSocket 服务端，NapCat 作为客户端连进来：

```
NapCat → 网络构建器 → 新建 → Websocket 客户端 → ws://127.0.0.1:8123/ws （访问密钥可选）
```

**先把原来连向 AstrBot 或麦麦的那条连接停掉**：一个 bot 号挂两条反向 WS 会出现两个人抢同一份事件，表现是"答一半"或者重复回复。

## 6. 启动

```bash
ASTRMAI_PANEL=on .venv/bin/python run_real.py \
  --allow <你的QQ号> \
  --account <机器人QQ号> \
  --port 8123 \
  --token <NapCat 的访问密钥，没有就留空>
```

- `--allow` 是**私聊硬白名单**：不在名单里的人发消息，astrmai 一个字都不回，日志里留一行"闸门沉默"。
- 群聊默认关。要开：`--allow-groups`（全放）或 `--groups <群号,群号>`（白名单）。
- 启动器会在 `e2e/real/` 下**自己生成一份 AstrBot 运行目录**（`data/cmd_config.json` 里写好反向 WS、wake 前缀 `/`、admins）。不需要手工配 AstrBot 面板。
- 停止：`Ctrl+C`。硬杀（`taskkill /F`、崩溃、断电）也不会漏下麦麦的子进程 —— 宿主一开头就把自己和它们圈进同一个 Windows 作业对象，见 [astrmai/jobguard.py](../astrmai/jobguard.py)。

## 7. 控制台

`http://127.0.0.1:18124/astrmai/`，初始口令 `astrmai-init`，**登录即强制改密**，不改什么都干不了（连 token 也被拦）。

QQ 里也有一套等价命令：`/mai status`、`/mai panel`、`/mai panel reset`、`/mai 许可证`、`/mai market astrbot <关键词>`、`/mai install astrbot <id> [--deps]`、`/mai load astrbot <目录名>`。`/mai` 只对管理员开放（`ASTRMAI_ADMINS`，没配就用私聊白名单 `--allow`）；**两个都空时一律拒绝**——它回一句告诉你去哪儿配，而不是让群里任何人都能装插件。

### 要放到公网

只有两条正确姿势。**推荐第一条。**

**A. 本机绑定 + 反向代理（推荐）**

面板继续只听 `127.0.0.1`，TLS 交给 nginx / Caddy：

```nginx
location /astrmai/ {
    proxy_pass http://127.0.0.1:18124;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

再设两个环境变量，否则面板会被自己骗：

| 变量 | 为什么必须 |
|---|---|
| `ASTRMAI_PANEL_TRUST_PROXY=1` | 认 `X-Forwarded-For` 里的真实来源。不认的话：爆破锁定会把整个代理当成一个来源（一个人锁死所有人），而"初始口令只允许本机登录"这条防线也会因为对端是回环而被绕过 —— 测试里有两个专门盯它的断言。**认的是最右一段**（由 nginx 追加），所以 nginx 那行必须用 `$proxy_add_x_forwarded_for`（追加），不能用 `$http_x_forwarded_for`（复制）—— 后者让客户端自己写 `127.0.0.1` 就冒充得了本机 |
| `ASTRMAI_PANEL_PUBLIC_HOST=<对外域名>` | 写操作的 Origin/Referer 要拿它比对，不然浏览器带 `Origin: 你的域名` 会被当成跨站拒绝 |

**B. 面板自己挂证书**

```bash
ASTRMAI_PANEL_HOST=0.0.0.0 ASTRMAI_PANEL_CERT=/路径/full.pem ASTRMAI_PANEL_KEY=/路径/priv.pem ...
```

证书不全就**拒绝启动**（不是警告后继续）。确实躲在已加密的隧道（WireGuard/ssh -L/Tailscale）后面，可以显式 `ASTRMAI_PANEL_INSECURE=1` 放行，启动时会大声警告一次。

## 8. 装完先自检

```bash
.venv/bin/python run_tests.py      # Windows 用 .venv\Scripts\python.exe run_tests.py
```

17 项离线测试：跑之前把模型配置**按字节备份**，临时指向本地桩，跑完原样还原 —— 不假设你用的是云端还是本机模型，也不需要 `secrets/` 才跑得起来，自己起的桩一定收掉。**不发你的额度、不碰线上**。它覆盖：端到端单连接、同一条消息不重复回答、作业对象回收（带对照组）、市场越界删除保护与 zip slip、许可证判不出就标 `unknown`、双向联邦真 RPC 往返、面板 API 形状（含 provider/人格/对话/定时四块）、凭据与强制改密、攻击面（未认证、CSRF、路径穿越、畸形参数、爆破、伪造 XFF 冒充本机、明文全目录零落地）、市场插件包按 AstrBot 的 `data.plugins.<名>.main` 语义能装起来（含"配置字段必须真被运行时读到"）。

## 9. 状态、升级、回滚

| 东西 | 在哪 | 迁移时要不要带上 |
|---|---|---|
| 凭据哈希（口令 + 机器 token） | `astrmai_state/credentials.json`（`ASTRMAI_STATE_DIR` 可换位置） | 带上就沿用同一套口令；不带就当新装。文件里只有 scrypt/sha256 摘要、盐和参数，**没有明文** |
| 面板改过的闸门 | `astrmai_state/settings.json` | 带。环境变量显式钉住的字段以环境变量为准 |
| 装的插件 | `vendor/AstrBot-master/data/plugins/`、`MaiBot-main/plugins/`、`astrmai_plugins/` | 各自带；许可证记录写在插件目录里的 `.astrmai-license.json` |
| 市场目录缓存 | `cache/` | **不要带**，半小时就过期，带着只会碍事 |

登录会话只在内存里（上限 64 条），面板日志也是内存环形缓冲 —— 都不落盘，重启即清空；这也是"明文口令不可能留在磁盘上"的一部分。

升级顺序：先跑 `run_tests.py` → 再换代码 → 重启。回滚就是把上一版插件目录之外的东西换回去；`settings.json` 与 `credentials.json` 是向前兼容的，旧版本读得懂。

忘了口令：本机跑 `/mai panel reset`（在 QQ 里，只对管理员开放），新口令只打印一次在控制台，不进文件、不进日志、也不走 QQ。

## 10. 发布到公开仓库（给维护者）

```bash
.venv/bin/python tools/make_denylist.py    # 第一步：把"本机真实存在的身份值"收成清单（终端不打印它们）
.venv/bin/python tools/pack_release.py     # 白名单构建 → 强制隐私扫描（含真实值比对）→ 修复回归闸门 → 打包
```

四道关卡，缺一不可：

1. **白名单构建**：只有 `astrmai/`、`astrmai_plugin/`、`astrmai_plugins/`、`tests/`、`tools/`、`docs/` 加几个根文件进树。`vendor/`、`MaiBot-main/`、`.venv/`、`astrmai_state/`、`data/`、`e2e/`、`secrets/`、`cache/`、`__pycache__`、`*.log`、`*.png` 一律不进 —— 默认拒绝，不是默认包含。交互式凭据录入脚本（`tools/get_*`、`tools/set_*`、`tools/selfcheck_*`）也不进。
2. **强制扫描**：构建完立刻对**发布树本身**跑 `tools/privacy_scan.py`，命中 API 密钥形状、私钥块、Windows 用户目录、真实值赋值、QQ 号形状、自建域名就非零退出。**扫描不过就拒绝打包**，不会"警告后继续"。
3. **真实值逐字节比对**：`tools/make_denylist.py` 从本机的 `secrets/*.json`、运行状态文件与环境变量里收集"你这个账号/这台机器独有的值"（模型 key、中转域名、主机名、家目录、挂在身份字段下的 10–11 位数字），写成 `tools/privacy_denylist.txt`。扫描器有它就逐字节比对，**命中没有豁免通道**（加 `# privacy-scan:allow` 也照样 FAIL），报告里只说"长度 10、纯数字"，绝不复述值本身。这份清单被 `.gitignore` 与打包白名单双重排除，永远不随包走。换台机器就重新生成一次。
4. **修复回归闸门**：`tools/pack_release.py` 里的 `GATES` 会对发布树再查一遍安全防线是否在位（`/mai` 空名单必须拒绝、来源取 XFF 最右一段、媒体默认不读本地路径、安装落盘校验 zip slip、市场出网闸覆盖两条链路）。**缺任一条就拒绝打包** —— 第三方审计修过的洞，不该靠我记得别改回去。

### 推上去

```bash
tools\get_github_token.cmd                              # 本地录入 fine-grained token（不进聊天记录、不回显）
.venv/bin/python tools/publish_github.py --check        # 只看账号与权限，不写任何东西
.venv/bin/python tools/publish_github.py                # 建【私有】仓库 astrmai 并推 dist 树
.venv/bin/python tools/publish_github.py --visibility public   # 内容确认无误后再翻公开
```

`publish_github.py` 只走 `api.github.com` 的 Git Data API（建 blob → 建树 → 建提交 → 指 ref），不需要 `git push` 能通：有些网络环境里 GitHub 域名被指到本地代理，克隆/推送都到不了远端，而 REST 是通的。三个已经踩过的坑：

- **别在 `POST /git/trees` 里带 `content` + `encoding: base64`**：实测那个端点不解码，会把 base64 字符串原样当文件内容存 —— 69 个文件全变成 4/3 长的乱码，而**按路径比对完全看不出来**（当时"核对通过"就是这么骗过我的）。正确做法是先 `POST /git/blobs`（它的 `content` 默认就是 base64），拿到 sha 再用 sha 建树。
- **核对必须比 git blob sha，不能比文件名**：`sha1("blob <长度>\0" + 字节)`，两边一致才叫推对了；再随机抽几个文件按 raw 拉回来逐字节比一次。
- **空仓库上建不了树**（409 `Git Repository is empty`），脚本会先放一个占位提交；**删除仓库需要 `delete_repo` 权限**，普通 `repo` scope 会 403。碰到坏历史又没有删除权限时，用 `--orphan` 推一条无父提交并强制移动分支，坏提交就不可达了。

它另外做两件与隐私有关的事：

- **默认建私有仓库**，你核对过内容再一条命令翻公开（公开一旦被 fork/缓存就收不回来）；
- 提交署名用 `<用户ID>+<账号名>@users.noreply.github.com`，**不写私人邮箱**（历史上就这么漏过一次，代价是重写已发布的 tag）；token 全程不打印，异常文本先做掩码再输出。

推之前自己再过一遍这几项（工具会漏，人不会）：

- 初始口令、token、机器人 QQ 号、你的 QQ 号有没有出现在文档或测试输出里；
- 测试截图与 `*_key.txt`/`*_tail.txt` 这类取证残留有没有被顺手 `git add`；
- `astrmai_state/credentials.json`（虽然是哈希，也没必要公开）与 `data/` 目录；
- 上游源码有没有混进来（`git status` 里 `vendor/`、`MaiBot-main/` 应该是被忽略的）。

## 11. 出问题先看这里

- **面板打不开**：先看控制台有没有 `面板启动失败` —— 面板起不来不会拖垮机器人本身（那条我拿 e2e 逼出来过：一个异常能把 `_boot` 打断，导致状态文件里写着 booted 但人进不去）。也检查端口是否被上一个实例占了。
- **插件"装了但没反应"**：`/mai load <source> <目录名>` 才进运行中的两个脑子；市场安装会自动尝试加载，结果在返回里，加载失败不会假装成功。需要 pip 依赖的会被政策先拒绝（默认永不代你装依赖）。
- **麦麦不回话**：`/mai status` 看 `forwarded/sent`，看裁决流水那条消息判给了谁；群聊要先 `--allow-groups` 或群白名单。
- **改完设置不生效**：面板会把"当场生效"和"下次启动生效"分开明写，`needs_restart` 里列的就是要重启的。
- **HTTPS 端点连不上/卡住**：宿主启动时会把出站 TLS 上限压到 1.2（本机实测某个兼容端点在 Python 的 TLS 1.3 握手会静默挂住）。换别的端点出问题时，这是第一个要怀疑的开关点。
- **"我这测不出来"**：不代表没有。测试副本掐掉真实启动、跑完核对残留、按 PID 精确杀，别拿线上试。
