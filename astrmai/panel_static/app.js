/* astrmai 控制台前端：手写原生 DOM，零框架、零构建、零外链。
   不复用两个上游引擎自带面板的任何代码、组件或资源（差异红线见 astrmai/panel.py）。 */

const API = "/astrmai/api";

/* 多级菜单：分组 → 条目 →（插件再分三家）。首页固定是负载总览。 */
const NAV = [
  {
    label: "运行",
    children: [
      { key: "load", label: "负载总览" },
      { key: "engines", label: "双脑状态" },
      { key: "ledger", label: "裁决流水" },
      { key: "sessions", label: "会话" },
    ],
  },
  {
    label: "扩展",
    children: [
      {
        key: "plugins",
        label: "插件",
        children: [
          { key: "plugins:astrbot", label: "AstrBot 星" },
          { key: "plugins:maibot", label: "麦麦插件" },
          { key: "plugins:astrmai", label: "astrmai 原生" },
        ],
      },
      { key: "tools", label: "工具联邦" },
      {
        key: "abfeature",
        label: "AstrBot 功能面",
        children: [
          { key: "abfeature:providers", label: "服务提供商" },
          { key: "abfeature:personas", label: "人格" },
          { key: "abfeature:conversations", label: "对话历史" },
          { key: "abfeature:cron", label: "定时任务" },
        ],
      },
      {
        key: "market",
        label: "官方市场",
        children: [
          { key: "market:astrbot", label: "AstrBot 官方市场" },
          { key: "market:maibot", label: "麦麦官方市场" },
        ],
      },
    ],
  },
  {
    label: "系统",
    children: [
      { key: "logs", label: "日志" },
      { key: "settings", label: "设置" },
      { key: "security", label: "凭据与隐私" },
    ],
  },
  {
    label: "关于",
    children: [
      { key: "licenses", label: "许可证清单" },
      { key: "about", label: "分发口径" },
    ],
  },
];

const state = {
  route: "load",
  crumb: ["运行", "负载总览"],
  token: sessionStorage.getItem("astrmai_token") || "",
  data: {},
  hold: {},
  busy: false,
  pendingRefresh: false,
  logLevel: "",
  lastError: "",
};

/* ---------------- DOM 基础 ---------------- */

function el(tag, attrs = {}, kids = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const kid of [].concat(kids)) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.appendChild(typeof kid === "string" || typeof kid === "number" ? document.createTextNode(String(kid)) : kid);
  }
  return node;
}

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}

function card(title, hint, body, lane) {
  const attrs = { class: "card" };
  if (lane) attrs["data-lane"] = lane;
  return el("div", attrs, [el("h3", {}, [title, hint ? el("span", { class: "hint", text: hint }) : null]), ...[].concat(body)]);
}

function facts(pairs) {
  const list = el("dl", { class: "facts" });
  for (const [k, v] of pairs) {
    list.appendChild(el("dt", { text: k }));
    list.appendChild(el("dd", { text: v === null || v === undefined || v === "" ? "—" : String(v) }));
  }
  return list;
}

function table(columns, rows, emptyText = "暂无") {
  if (!rows.length) return el("p", { class: "empty", text: emptyText });
  const head = el("tr", {}, columns.map((c) => el("th", { text: c })));
  const body = rows.map((row) => {
    const cells = row.map((cell) => {
      if (cell instanceof Node) return el("td", {}, [cell]);
      return el("td", { text: cell === null || cell === undefined ? "—" : String(cell) });
    });
    return el("tr", {}, cells);
  });
  return el("table", {}, [el("thead", {}, [head]), el("tbody", {}, body)]);
}

function badge(text, kind) {
  return el("span", { class: "badge" + (kind ? " " + kind : ""), text });
}

function pageHead(title, desc) {
  return el("div", { class: "page-head" }, [
    el("h1", { text: title }),
    el("span", { class: "crumb", text: state.crumb.join(" / ") }),
    desc ? el("span", { class: "desc", text: desc }) : null,
  ]);
}

/* 状态栏：一次操作的报告（装了什么、加载结果）要活过下一次自动刷新，
   否则"点了按钮但报告被 3 秒后的例行刷新冲掉"会让人以为没生效。 */
function setStatus(text, bad = false, holdMs = 0) {
  state.note = { text, bad, until: Date.now() + holdMs };
  paintStatus("");
}

const note = (text, bad = false) => setStatus(text, bad, 25000);

function paintStatus(fallback) {
  const node = document.getElementById("status-text");
  const held = state.note && Date.now() < state.note.until ? state.note : null;
  node.textContent = held ? held.text : String(fallback || "");
  node.style.color = held && held.bad ? "var(--bad)" : "";
}

function fmt(value, digits = 0) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!isFinite(n)) return "—";
  return n.toFixed(digits);
}

function fmtRate(bps) {
  if (bps === null || bps === undefined) return "攒基线中";
  const n = Number(bps);
  if (n < 1024) return n.toFixed(0) + " B/s";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB/s";
  return (n / 1048576).toFixed(2) + " MB/s";
}

function fmtUptime(sec) {
  if (!sec && sec !== 0) return "—";
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return (d ? d + "天" : "") + h + "小时" + m + "分";
}

/* 迷你趋势图：自己画 SVG，不引图表库 */
function sparkline(values, color, opts = {}) {
  if (!values || values.length < 2) return el("span", { class: "empty", text: "攒样本中…" });
  const w = 200, h = 34;
  const max = opts.max !== undefined ? opts.max : Math.max(...values, 1e-6);
  const min = opts.min !== undefined ? opts.min : Math.min(...values, 0);
  const span = max - min || 1;
  const step = w / (values.length - 1);
  const pts = values.map((v, i) => [i * step, h - ((v - min) / span) * (h - 4) - 2]);
  const line = pts.map((p) => p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ");
  const svg = svgEl("svg", { viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: "none" });
  svg.appendChild(svgEl("polyline", { points: `0,${h} ${line} ${w},${h}`, fill: color, "fill-opacity": "0.13", stroke: "none" }));
  svg.appendChild(svgEl("polyline", { points: line, fill: "none", stroke: color, "stroke-width": "1.4" }));
  const last = pts[pts.length - 1];
  svg.appendChild(svgEl("circle", { cx: last[0].toFixed(1), cy: last[1].toFixed(1), r: "2", fill: color }));
  return svg;
}

function metric(label, value, unit, sub, opts = {}) {
  const kids = [el("div", { class: "label", text: label }), el("div", { class: "value" }, [String(value), unit ? el("span", { class: "unit", text: unit }) : null])];
  if (opts.series) kids.push(sparkline(opts.series, opts.color || "#57c7d4", opts));
  else if (opts.pct !== undefined) kids.push(el("div", { class: "bar" + (opts.tone ? " " + opts.tone : "") }, [el("i", { style: `width:${Math.max(0, Math.min(100, opts.pct))}%` })]));
  if (sub) kids.push(el("div", { class: "sub", text: sub }));
  return el("div", { class: "metric" + (opts.tone ? " is-" + opts.tone : "") }, kids);
}

function toneFor(pct, warnAt, hotAt) {
  if (pct === null || pct === undefined) return "";
  if (pct >= hotAt) return "hot";
  if (pct >= warnAt) return "warnc";
  return "ok";
}

/* ---------------- 取数 ---------------- */

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (state.token) headers["X-Astrmai-Token"] = state.token;
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const res = await fetch(API + path, Object.assign({}, options, { headers }));
  let payload = {};
  try {
    payload = await res.json();
  } catch (err) {
    payload = { ok: false, reason: "响应不是 JSON: " + err.name };
  }
  if (res.status === 401) {
    // 会话过期或没登录：回到登录屏，而不是弹一个过时的 token 输入框
    state.auth = Object.assign({ authenticated: false, must_change: false }, state.auth || {});
    authStatus().then((status) => loginScreen(status));
  }
  if (res.status === 403 && payload.must_change) {
    authStatus().then((status) => renderChangeScreen(status));
  }
  return { status: res.status, payload };
}

const apiGet = async (path) => (await api(path)).payload;
const apiPost = async (path, body) => (await api(path, { method: "POST", body: JSON.stringify(body || {}) })).payload;

async function load() {
  if (!state.data.load) state.data.load = await apiGet("/load");
  return state.data.load;
}

async function overview(force = false) {
  if (force || !state.data.overview) state.data.overview = await apiGet("/overview");
  return state.data.overview;
}

/* ---------------- 登录与强制改密 ---------------- */

async function authStatus() {
  const s = await apiGet("/auth/status");
  state.auth = s;
  return s;
}

function loginScreen(status) {
  const input = el("input", { type: "password", placeholder: "面板密码", autocomplete: "current-password" });
  const note = el("p", { class: "note", text: "" });
  const btn = el("button", {
    class: "act",
    text: "登录",
    onclick: async () => {
      btn.disabled = true;
      note.textContent = "登录中…";
      const res = await apiPost("/auth/login", { password: input.value });
      btn.disabled = false;
      if (!res.ok) {
        note.textContent = res.reason || "登录失败";
        if (res.reason && res.reason.includes("锁")) setStatus(res.reason, true);
        return;
      }
      const again = await authStatus();
      if (again.must_change) {
        renderChangeScreen(again);
      } else {
        renderShell();
        await refresh(true);
      }
    },
  });
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") btn.click();
  });
  const stage = document.getElementById("stage");
  stage.textContent = "";
  stage.appendChild(
    el("div", {}, [
      pageHead("登录", (status.kdf || "") + " 哈希存储 · 明文不落盘"),
      card("astrmai 控制台", status.lockout_left ? `已锁定 ${status.lockout_left}s` : "", [
        el("div", { class: "row" }, [input, btn]),
        note,
        el("p", {
          class: "note",
          text: status.default_password_in_use
            ? (status.default_password_hint
              ? `初始口令是 ${status.default_password_hint}（公开写在文档里），登录后必须立刻改掉 —— 它只接受来自本机的登录。`
              : "初始口令只在这台机器上显示（也公开写在部署文档里），远端来源不认它；要么在本机登录改掉，要么让运维先在服务器上登一次。")
            : `密码至少 ${status.min_length || 12} 位；连错 5 次会按来源锁定一段时间。`,
        }),
      ]),
    ])
  );
}

function renderChangeScreen(status) {
  const cur = el("input", { type: "password", placeholder: "当前（初始）密码", autocomplete: "current-password" });
  const next = el("input", { type: "password", placeholder: `新密码，至少 ${status.min_length || 12} 位`, autocomplete: "new-password" });
  const again = el("input", { type: "password", placeholder: "再输一遍新密码", autocomplete: "new-password" });
  const note = el("p", { class: "note", text: "" });
  const btn = el("button", {
    class: "act",
    text: "改密并继续",
    onclick: async () => {
      if (next.value !== again.value) {
        note.textContent = "两次输入的新密码不一样";
        return;
      }
      btn.disabled = true;
      const res = await apiPost("/auth/change_password", { current: cur.value, new: next.value });
      btn.disabled = false;
      if (!res.ok) {
        note.textContent = res.reason || "改密失败";
        return;
      }
      note.textContent = "已改密，旧会话全部作废，请重新登录";
      state.auth = await authStatus();
      setTimeout(() => loginScreen(state.auth), 900);
    },
  });
  const stage = document.getElementById("stage");
  stage.textContent = "";
  stage.appendChild(
    el("div", {}, [
      pageHead("必须先改初始密码", "改完之前，面板的其它功能一律不放行"),
      card("修改密码", "服务端只存 scrypt 哈希，明文不进文件、不进日志、不进任何响应", [
        el("div", { class: "row" }, [cur]),
        el("div", { class: "row" }, [next]),
        el("div", { class: "row" }, [again]),
        el("div", { class: "row" }, [btn]),
        note,
      ]),
    ])
  );
}

async function logout() {
  await apiPost("/auth/logout", {});
  state.auth = await authStatus();
  loginScreen(state.auth);
}

function renderShell() {
  const stage = document.getElementById("stage");
  const box = stage.querySelector(".token-box");
  if (box) box.remove();
  renderNav();
  mountLogout();
}

/* 自动刷新会整页重渲染：正在填的表单不能被它清空 */
function captureInputs() {
  const kept = {};
  for (const node of document.querySelectorAll("#stage input[name], #stage select[name]")) {
    kept[node.getAttribute("name")] = node.type === "checkbox" ? node.checked : node.value;
  }
  return kept;
}

function restoreInputs(kept) {
  for (const [name, value] of Object.entries(kept)) {
    const node = document.querySelector(`#stage [name="${name}"]`);
    if (!node) continue;
    if (node.type === "checkbox") node.checked = !!value;
    else node.value = value;
  }
}

function authGate(status) {
  if (!status.authenticated) {
    loginScreen(status);
    return false;
  }
  if (status.must_change) {
    renderChangeScreen(status);
    return false;
  }
  return true;
}

/* ---------------- 顶栏 ---------------- */

function renderKpis(ld) {
  const box = document.getElementById("kpi-strip");
  box.textContent = "";
  const host = ((ld || {}).latest || {}).host || {};
  const fusion = ((ld || {}).latest || {}).fusion || {};
  const proc = ((ld || {}).latest || {}).process || {};
  const lag = fusion.loop_lag_ms;
  const items = [
    ["CPU", fmt(host.cpu_percent, 1), "%", toneFor(host.cpu_percent, 70, 90)],
    ["内存", fmt(host.mem_percent, 0), "%", toneFor(host.mem_percent, 80, 92)],
    ["磁盘", fmt(host.disk_percent, 0), "%", toneFor(host.disk_percent, 85, 95)],
    ["循环延迟", fmt(lag, 0), "ms", lag === undefined ? "" : lag > 250 ? "hot" : lag > 80 ? "warnc" : "ok"],
    ["入站", fusion.msg_rate && fusion.msg_rate.in !== null ? fmt(fusion.msg_rate.in, 2) : "—", "/s", ""],
    ["出站", fusion.msg_rate && fusion.msg_rate.out !== null ? fmt(fusion.msg_rate.out, 2) : "—", "/s", ""],
    ["本进程", fmt(proc.rss_mb, 0), "MB", ""],
  ];
  for (const [label, value, unit, tone] of items) {
    box.appendChild(el("div", { class: "kpi" + (tone ? " " + tone : "") }, [el("b", { text: value + unit }), el("span", { text: label })]));
  }
}

function lamp(label, kind, detail) {
  return el("span", { class: `lamp is-${kind}`, title: detail }, [el("span", { class: "dot" }), label + " · " + detail]);
}

function renderLamps(ov) {
  const box = document.getElementById("lamps");
  box.textContent = "";
  if (!ov) return;
  const eng = ov.engine || {};
  const proc = ov.process || {};
  const job = proc.job_guard || {};
  const priv = ov.privacy || {};
  box.appendChild(lamp("引擎", eng.booted ? "ok" : eng.boot_error ? "bad" : "warn", eng.booted ? `麦麦 ${eng.boot_ms}ms` : eng.boot_error ? "启动失败" : "未就绪"));
  box.appendChild(lamp("runner", (proc.runners_live || []).length ? "ok" : "warn", `${(proc.runners_live || []).length} 个存活`));
  box.appendChild(lamp("子进程兜底", job.mode === "self" ? "ok" : job.mode === "pids" ? "warn" : "bad", job.mode === "self" ? "作业对象连坐" : job.mode === "pids" ? "仅点名回收" : "无兜底"));
  box.appendChild(lamp("遥测", priv.telemetry === "已掐" ? "ok" : "bad", priv.telemetry || "未知"));
}

/* ---------------- 页面：负载总览（首页） ---------------- */

async function viewLoad() {
  const [ld, ov] = [await load(), await overview()];
  const host = (ld.latest || {}).host || {};
  const proc = (ld.latest || {}).process || {};
  const fusion = (ld.latest || {}).fusion || {};
  const s = ld.series || {};
  const probe = (ld.latest || {}).probe || {};

  if (!ld.ok) {
    return el("div", {}, [pageHead("负载总览"), el("p", { class: "empty", text: ld.reason || "采样器还没数据" })]);
  }

  const warnings = [];
  if (host.cpu_percent >= 90) warnings.push(`CPU ${fmt(host.cpu_percent, 1)}% 已接近满载`);
  if (host.mem_percent >= 90) warnings.push(`内存 ${fmt(host.mem_percent, 0)}% 告急，麦麦的 FAISS/记忆子系统容易先被牵连`);
  if (host.disk_percent >= 90) warnings.push(`磁盘剩余 ${fmt(host.data_free_mb / 1024, 1)} GB，日志与 SQLite 快写满了`);
  if ((fusion.loop_lag_ms || 0) > 250) warnings.push(`事件循环延迟 ${fmt(fusion.loop_lag_ms, 0)}ms —— 两个脑子共用一个 loop，多半有同步阻塞`);
  if ((fusion.runners || []).some((r) => r.gone)) warnings.push("有 runner PID 已经消失（supervisor 可能刚重启过它）");
  if (probe.error) warnings.push("采样器报错：" + probe.error);

  const cards = el("div", { class: "metrics" }, [
    metric("CPU", fmt(host.cpu_percent, 1), "%", `${host.cores || "?"} 核` + (host.cpu_mhz ? `｜${host.cpu_mhz}MHz` : ""), { series: s.cpu, color: "#e0b04a", max: 100, min: 0, tone: toneFor(host.cpu_percent, 70, 90) }),
    metric("内存", fmt(host.mem_percent, 0), "%", `${host.mem_used_mb} / ${host.mem_total_mb} MB`, { series: s.mem, color: "#9a86e8", max: 100, min: 0, tone: toneFor(host.mem_percent, 80, 92) }),
    metric("磁盘", fmt(host.disk_percent, 0), "%", `${host.disk_used_gb} / ${host.disk_total_gb} GB｜数据盘余 ${fmt(host.data_free_mb / 1024, 1)} GB`, { pct: host.disk_percent, tone: toneFor(host.disk_percent, 85, 95) }),
    metric("网络", fmtRate(host.net_up_bps), "", "↑ 下行 " + fmtRate(host.net_down_bps), { series: s.net_up, color: "#57c7d4" }),
    metric("事件循环延迟", fmt(fusion.loop_lag_ms, 0), "ms", `asyncio 任务 ${fusion.asyncio_tasks}`, { series: s.loop_lag, color: "#d9605b", max: 120, min: 0 }),
    metric("astrmai 进程", fmt(proc.rss_mb, 0), "MB RSS", `线程 ${proc.threads}｜句柄 ${proc.handles ?? "—"}｜PID ${proc.pid}`, { series: s.proc_rss, color: "#7fbf7a" }),
    metric("入站速率", fusion.msg_rate && fusion.msg_rate.in !== null ? fmt(fusion.msg_rate.in, 2) : "—", "/s", `累计转发 ${fusion.counters ? fusion.counters.forwarded : 0}`, { series: s.msgs_in, color: "#9a86e8" }),
    metric("出站速率", fusion.msg_rate && fusion.msg_rate.out !== null ? fmt(fusion.msg_rate.out, 2) : "—", "/s", `累计出站 ${fusion.counters ? fusion.counters.sent : 0}`, { series: s.msgs_out, color: "#e0b04a" }),
    metric("开机至今", fmtUptime(host.uptime_sec), "", `采样 ${probe.samples || 0} 次｜窗口 ${Math.round((ld.window_sec || 0) / 60)} 分钟`, {}),
  ]);

  const runners = (fusion.runners || []).map((r) => [
    String(r.pid),
    r.gone ? badge("已消失", "bad") : badge(r.status || "?", "ok"),
    r.gone ? "" : fmt(r.rss_mb, 1) + " MB",
    r.gone ? "" : fmt(r.cpu_percent, 1) + "%",
    r.gone ? "" : String(r.threads),
  ]);

  const eng = ov.engine || {};
  const fed = ov.federation || {};
  const vis = fed.mirror_visibility || {};
  const conn = ov.connections || {};
  const connTone = conn.connected ? "ok" : "bad";
  const connText = conn.ok
    ? `${conn.connected ? "已连接" : "未连接"}｜反向 WS ${conn.total_connections ?? 0} 条`
    : "读不到：" + (conn.error || "?");

  return el("div", {}, [
    pageHead("负载总览", `每 ${probe.interval || 2}s 采样一次，只驻内存不落盘`),
    warnings.length ? card("需要留神的", `${warnings.length} 条`, el("ul", { class: "plain" }, warnings.map((w) => el("li", { text: w })))) : null,
    card("平台连接", connText, el("div", { class: "row" }, (conn.platforms || []).map((p) =>
      el("span", { class: "conn-item" }, [
        el("strong", { text: p.id || p.type }),
        " ",
        p.state === "已连接" ? badge(`已连接 ×${p.connections}`, "ok") : badge(p.state || "未连接", "warn"),
        (p.api_clients || []).length ? el("span", { class: "dim", text: `（api ${(p.api_clients || []).filter((c) => c.alive).length}/${p.api_clients.length}，event ${(p.event_clients || []).filter((c) => c.alive).length}/${p.event_clients.length}）` }) : null,
      ])
    ))),
    cards,
    el("div", { class: "grid cols-3" }, [
      card("AstrBot 道", (ov.versions || {}).astrbot, facts([
        ["它自己的面板", (ov.upstream_panels || {}).astrbot_dashboard],
        ["正在跑的星", `${(ov.stars || {}).total ?? "—"} 个（第三方 ${(ov.stars || {}).third_party ?? "—"}）`],
        ["镜像给麦麦 ab_*", (fed.ab_tools_live || {}).count ?? "—"],
        ["ab_ 可见性", vis.ab_ || "—"],
      ]), "astrbot"),
      card("麦麦道", (ov.versions || {}).maibot, facts([
        ["引擎", eng.booted ? `已启动 ${eng.boot_ms}ms` : "未就绪"],
        ["它自己的面板", (ov.upstream_panels || {}).maibot_webui],
        ["插件 runner", runners.length ? runners.map((r) => r[0]).join(", ") : "无"],
        ["mai_/nm_ 可见性", `${vis.mai_ || vis.nm_ || "—"} / ${vis.nm_ || "—"}`],
      ]), "maibot"),
      card("融合层", (ov.versions || {}).astrmai, facts([
        ["裁决归属", Object.entries((ov.arbitration || {}).decisions || {}).map(([k, v]) => `${k}=${v}`).join(" ") || "还没有消息"],
        ["会话数", eng.sessions || 0],
        ["子进程兜底", ((ov.process || {}).job_guard || {}).mode === "self" ? "作业对象连坐" : "见「双脑状态」"],
        ["遥测 / 市场出网", `${(ov.privacy || {}).telemetry} / ${(ov.privacy || {}).market_network}`],
      ]), "fusion"),
    ]),
    card(
      "麦麦插件 runner（out-of-process 子进程）",
      "它们各自吃多少 CPU/内存要单独看，混在宿主里看不出来",
      table(["PID", "状态", "RSS", "CPU", "线程"], runners, "没有存活的 runner（没装麦麦插件，或它们没起来）")
    ),
  ]);
}

/* ---------------- 页面：双脑状态 / 裁决 / 会话 ---------------- */

async function viewEngines() {
  const ov = await overview(true);
  const bindings = ov.model_bindings || {};
  const rows = Object.entries(bindings.tasks || {}).map(([task, v]) => [
    task,
    v.provider || "—",
    v.model || "—",
    v.host || "—",
    v.credentialed ? badge("凭据已配置", "ok") : badge("无凭据", "warn"),
    v.extra ? `+${v.extra}` : "",
  ]);
  const proc = ov.process || {};
  const connRows = ((ov.connections || {}).platforms || []).map((p) => [
    p.id || "—",
    p.type,
    p.state === "已连接" ? badge(`已连接 ×${p.connections}`, "ok") : badge(p.state || "未连接", "warn"),
    (p.api_clients || []).map((c) => `${c.self_id}${c.alive ? "" : "(死)"}`).join(", ") || "—",
    String((p.event_clients || []).length),
  ]);
  return el("div", {}, [
    pageHead("双脑状态", "两个引擎各自的运行时事实"),
    card("平台连接（NapCat / OneBot v11 反向 WS）", (ov.connections || {}).error || "没有反向 WS 连着就收不到任何消息", table(
      ["实例", "类型", "状态", "api 客户端 (X-Self-ID)", "event 客户端"], connRows,
      (ov.connections || {}).ok ? "还没有平台实例" : "读不到"
    )),
    el("div", { class: "grid cols-2" }, [
      card("AstrBot", (ov.versions || {}).astrbot, facts([
        ["版本", (ov.versions || {}).astrbot],
        ["自带面板", (ov.upstream_panels || {}).astrbot_dashboard],
        ["镜像可见性 ab_/abcmd_", `${(ov.federation?.mirror_visibility || {}).ab_} / ${(ov.federation?.mirror_visibility || {}).abcmd_}`],
        ["此刻可镜像工具", (ov.federation?.ab_tools_live || {}).count ?? "—"],
      ]), "astrbot"),
      card("麦麦 MaiBot", (ov.versions || {}).maibot, facts([
        ["版本", (ov.versions || {}).maibot],
        ["引擎启动", ov.engine.booted ? `${ov.engine.boot_ms} ms` : "未就绪"],
        ["自带面板", (ov.upstream_panels || {}).maibot_webui],
        ["mai_/nm_ 工具", `${(ov.federation?.mai_tools || {}).total || 0} / ${(ov.federation?.native || {}).tools || 0}`],
        ["runner", (proc.runners_live || []).join(", ") || "无"],
        ["子进程兜底", (proc.job_guard || {}).mode === "self" ? "作业对象（含硬杀连坐）" : JSON.stringify(proc.job_guard || {})],
        ["自主性", (ov.mai_autonomy || {}).ok ? `群聊频率 ${ov.mai_autonomy.talk_value}｜私聊${ov.mai_autonomy.private_paused ? "已暂停" : "正常"}` : ov.mai_autonomy?.reason || "—"],
      ]), "maibot"),
    ]),
    card("模型任务绑定（麦麦侧，凭据只显示有没有）", bindings.ok ? "" : bindings.reason || "", table(["任务", "提供商", "模型", "主机", "凭据", "备用"], rows, "读不到")),
    card("出站 TLS 与记忆库", "TLS ≤1.2 是这台机上模型中转能握上手的前提；记忆库只读", facts([
      ["TLS 上限已钉", (ov.tls || {}).applied ? "是" : "否（本机没跑 tls.apply()）"],
      ["TLS 实际上限", (ov.tls || {}).maximum || "—"],
      ["记忆库", (ov.memory || {}).ok ? Object.entries(ov.memory.counts || {}).map(([k, v]) => `${k}=${v}`).join("｜") || (ov.memory.db || "已连接") : (ov.memory || {}).reason || "未知"],
    ])),
    card("闸门与隐私", "不在名单里的会话连转发都不会发生", facts([
      ["群聊闸门", ov.privacy.group_enabled ? "开" : "关（默认沉默）"],
      ["私聊闸门", ov.privacy.private_enabled ? "开" : "关"],
      ["私聊白名单", (ov.privacy.allow_private || []).join("、") || "（空=不限）"],
      ["群号白名单", (ov.privacy.allow_groups || []).join("、") || "（空=不限）"],
      ["遥测出口", ov.privacy.telemetry],
      ["市场出网", ov.privacy.market_network],
    ])),
    card("runner 重启留痕", "supervisor 重启会换 PID，这里只记新增，不猜它为什么重启",
      table(["时间", "新 PID", "当时已知的 PID"],
        ((ov.process || {}).runner_history || []).map((h) => [new Date(h.time * 1000).toLocaleTimeString("zh-CN", { hour12: false }), h.added.join(", "), (h.known || []).join(", ") || "（无）"]),
        "没有换过 PID —— 稳定是好事")),
  ]);
}

async function viewLedger() {
  const ov = await overview(true);
  const arb = ov.arbitration || {};
  const rows = (arb.recent || []).map((r) => [
    new Date(r.ts * 1000).toLocaleTimeString("zh-CN", { hour12: false }),
    r.chat_type === "group" ? "群 " + r.target_id : "私 " + r.target_id,
    el("span", { class: "owner-" + r.owner, text: r.owner }),
    r.reason || "",
    r.forwarded ? badge("已转发", "ok") : badge("未转发", ""),
    r.preview || "",
    r.detail || "",
  ]);
  const tally = arb.decisions || {};
  return el("div", {}, [
    pageHead("裁决流水", "每条消息归谁、为什么 —— 这条上游两家的面板各自看不到对方"),
    el("div", { class: "row" }, [
      badge("maibot " + (tally.maibot || 0), "mai"),
      badge("astrbot " + (tally.astrbot || 0), "ab"),
      badge("none " + (tally.none || 0), ""),
    ]),
    table(["时间", "会话", "归属", "判据", "转发", "内容摘要", "备注"], rows, "还没有消息进来（先在私聊白名单里加你的号）"),
  ]);
}

async function viewSessions() {
  const data = await apiGet("/sessions");
  const rows = (data.items || []).map((s) => [
    s.chat_type === "group" ? "群聊" : "私聊",
    s.target_id,
    el("span", { class: "mono", text: s.umo }),
    s.last_preview || "",
  ]);
  return el("div", {}, [pageHead("会话", `共 ${data.total || 0} 个；出站靠 unified_msg_origin 找回平台连接`), table(["类型", "目标", "unified_msg_origin", "最近内容"], rows, "还没有会话")]);
}

/* ---------------- 页面：插件（三家） ---------------- */

function loadButton(source, name) {
  return el("button", {
    class: "act",
    text: "加载",
    onclick: async (ev) => {
      ev.target.disabled = true;
      const out = await apiPost("/plugins/load", { source, name });
      note(out.ok ? `加载：${out.report}` : `加载失败：${out.reason}`, !out.ok);
      await refresh(true);
    },
  });
}

function uninstallButton(source, name) {
  return el("button", {
    class: "ghost",
    text: "卸载",
    onclick: async (ev) => {
      if (!confirm(`真的要从 ${source} 目录里删掉 ${name}？`)) return;
      ev.target.disabled = true;
      const out = await apiPost("/plugins/uninstall", { source, name });
      note(out.ok ? `已卸载：${out.removed}` : `卸载失败：${out.reason}`, !out.ok);
      await refresh(true);
    },
  });
}

function licBadge(r) {
  const id = String(r.license || "unknown");
  if (id.toLowerCase() === "unknown") return badge("unknown", "bad");
  return badge(id, r.license_confidence === "high" || r.confidence === "high" ? "ok" : "warn");
}

async function viewPlugins(which) {
  const data = await apiGet("/plugins");
  state.data.plugins = data;
  const failed = Object.entries((data.astrbot || {}).failed || {});

  if (which === "astrbot") {
    const rows = (data.astrbot.items || []).map((p) => [
      p.name || p.dir,
      p.version || "",
      p.author || "",
      p.reserved ? badge("内置", "") : badge("插件", "ok"),
      licBadge(p),
      p.desc || "",
      el("div", { class: "row" }, [loadButton("astrbot", p.dir || p.name), p.reserved ? "" : uninstallButton("astrbot", p.dir || p.name)]),
    ]);
    return el("div", {}, [
      pageHead("AstrBot 星", data.manager_attached ? "可运行时加载，不必重启" : "拿不到 PluginManager，只能重启生效"),
      table(["名字", "版本", "作者", "类型", "许可证", "简介", "动作"], rows, "没有插件"),
      failed.length ? card("加载失败清单", "落盘了但没进星表", table(["目录", "错误"], failed.map(([k, v]) => [k, v]), "")) : null,
      card(
        "加载拒绝留痕（默认不代装 pip 依赖）",
        "被拒的加载动作都在这；要装的话用 --deps 显式放行，先想清楚那是谁写的代码",
        table(["时间", "来源", "插件", "原因"], (data.load_denials || []).map((d) => [new Date(d.time * 1000).toLocaleTimeString("zh-CN", { hour12: false }), d.source, d.name, d.detail]), "还没有被拒过的加载")
      ),
    ]);
  }

  if (which === "maibot") {
    const rows = (data.maibot.items || []).map((p) => [
      p.name || p.dir,
      p.id || "",
      p.enabled === null ? badge("开关未知", "warn") : p.enabled ? badge("已启用", "ok") : badge("停用", ""),
      p.license && p.license !== "unknown" ? badge(p.license, "ok") : badge("unknown", "bad"),
      p.release ? el("span", { class: "mono", text: p.release }) : "",
      p.error || "",
      el("div", { class: "row" }, [loadButton("maibot", p.id || p.dir), uninstallButton("maibot", p.dir)]),
    ]);
    return el("div", {}, [
      pageHead("麦麦插件", `runner: ${(data.maibot.runner_pids || []).join(", ") || "无"}（out-of-process，随宿主作业对象连坐）`),
      table(["名字", "id", "开关", "许可证", "commit", "备注", "动作"], rows, "没有插件"),
    ]);
  }

  const rows = (data.astrmai.items || []).map((p) => [
    p.name || p.dir,
    p.version || "",
    p.tools === null || p.tools === undefined ? badge("未加载，工具数未知", "warn") : badge(p.tools + " 个工具", "ok"),
    p.license && p.license !== "unknown" ? badge(p.license, "ok") : badge("unknown", "bad"),
    (p.tool_names || []).join(", "),
    p.error || "",
    el("div", { class: "row" }, [loadButton("astrmai", p.dir)]),
  ]);
  return el("div", {}, [
    pageHead("astrmai 原生插件", "同一份代码注册进两个脑子，前缀 nm_"),
    table(["名字", "版本", "工具", "许可证", "工具名", "备注", "动作"], rows, "没有原生插件"),
  ]);
}

/* ---------------- 页面：工具联邦 / 市场 ---------------- */

async function viewTools() {
  const data = await apiGet("/tools");
  const ab = data.astrbot || {};
  const mai = data.maibot || {};
  const by = ab.by_prefix || {};
  const rows = (ab.items || []).map((t) => {
    const kind = t.name.startsWith("abcmd_") ? "abcmd" : t.name.startsWith("mai_") ? "mai" : t.name.startsWith("ab_") ? "ab" : t.name.startsWith("nm_") ? "nm" : "";
    return [t.name, kind ? badge(kind + "_", kind) : badge("AstrBot 自有", ""), t.description || "", t.active ? badge("可用", "ok") : badge("停用", "warn"), (t.module || "").replace(/^data\.plugins\./, "")];
  });
  const maiRows = (mai.plugin_specs || []).map((s) => [s.name, s.provider || "", s.description || ""]);
  const resync = el("button", {
    class: "act",
    text: "重新同步 mai_*",
    onclick: async (ev) => {
      ev.target.disabled = true;
      const out = await apiPost("/tools/resync", {});
      ev.target.disabled = false;
      note("重扫结果：" + JSON.stringify(out.summary || out.reason || out));
      await refresh(true);
    },
  });
  return el("div", {}, [
    pageHead("工具联邦", "mai_=麦麦→AstrBot｜ab_=AstrBot→麦麦｜abcmd_=AstrBot 命令→麦麦｜nm_=astrmai 原生"),
    el("div", { class: "row" }, [
      badge("mai_ " + (by.mai_ || 0), "mai"),
      badge("abcmd_ " + (by.abcmd_ || 0), "abcmd"),
      badge("ab_ " + (by.ab_ || 0), "ab"),
      badge("nm_ " + (by.nm_ || 0), "nm"),
      badge("AstrBot 自有 " + (by["其它"] || 0), ""),
      el("span", { class: "grow" }),
      resync,
    ]),
    card("AstrBot 工具表（含联邦注入的全部前缀）", ab.error || `共 ${ab.total || 0} 条`, table(["名字", "来源", "说明", "状态", "处理器模块"], rows, "没有工具")),
    card("麦麦插件工具（component_query 视图）", mai.error || "运行中的麦麦认为可用的插件工具", table(["名字", "提供方", "说明"], maiRows, "麦麦还没有插件工具")),
    (() => {
      const cm = data.command_mirror || {};
      if (!cm.ok) return card("命令镜像逐条清单", "读不到：" + (cm.reason || "?"), "");
      const cmRows = (cm.items || []).map((i) => [i.tool, i.module.replace(/^data\.plugins\./, ""), i.desc, i.active ? badge("注册着", "ok") : badge("停用", "warn")]);
      return card("命令镜像逐条清单（abcmd_）", `可见性 ${cm.visibility}（deferred 时麦麦要用 tool_search 自己发现）`, table(["工具名", "来源命令", "说明", "状态"], cmRows, cm.items.length ? "" : "还没有命令镜像"));
    })(),
  ]);
}

/* 市场：两家官方目录各占一页，打开就列出来，点行直接装。
   状态一律挂在 state.hold 上 —— 自动刷新每 3 秒重建整棵 DOM，
   搜索结果要是只存在闭包里，会被自己的刷新刷没（点安装时尤其明显）。 */

const MARKETS = {
  astrbot: {
    title: "AstrBot 官方市场",
    hint: "目录取自 AstrBot 官方插件集；官方目录不带许可证字段，装的时候才从包里判定",
  },
  maibot: {
    title: "麦麦官方市场",
    hint: "目录取自麦麦官方接口；这一页会出网到 hyybuth.xyz:10059（与麦麦遥测同域）",
  },
};

function marketHold(source) {
  const key = "market:" + source;
  if (!state.hold[key]) state.hold[key] = { query: "", offset: 0, limit: 15, res: null, error: "", detail: "" };
  return state.hold[key];
}

async function marketFetch(source, force = false) {
  const h = marketHold(source);
  h.fetching = true;
  const res = await apiPost("/market/search", { source, query: h.query, limit: h.limit, offset: h.offset, force: !!force });
  h.fetching = false;
  h.fetched = { query: h.query, offset: h.offset, limit: h.limit };
  if (res && res.ok === false) {
    h.error = res.reason || "目录不可达";
    h.res = null;
  } else {
    h.res = res;
    h.error = "";
  }
}

/* 翻页/换关键词要重新取，否则点"下一页"只是把同一份数据重画一遍 */
function marketStale(h) {
  return !h.fetched || h.fetched.query !== h.query || h.fetched.offset !== h.offset || h.fetched.limit !== h.limit;
}

async function marketView(source) {
  const meta = MARKETS[source];
  const h = marketHold(source);
  if (marketStale(h) && !h.fetching) {
    // 目录要几秒到几十秒：先把"读取中"画出来，取完再自己刷一次。
    // 以前这里 await 在渲染路径里，整页停在上一屏，看起来像点错了菜单。
    h.fetching = true;
    marketFetch(source).then(() => refresh(true));
  }

  const queryIn = el("input", { type: "text", name: `${source}-market-query`, placeholder: "关键词（留空＝整目录）" });
  const search = async () => {
    h.query = (queryIn.value || "").trim();
    h.offset = 0;
    await marketFetch(source);
    await refresh(true);
  };
  queryIn.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") search();
  });
  const items = (h.res && h.res.plugins) || [];
  const from = items.length ? h.offset + 1 : 0;
  const pageLine = h.error
    ? h.error
    : h.res
      ? `第 ${from}–${h.offset + items.length} 条 / 共 ${h.res.total} 条（目录源：${h.res.catalog}）`
      : "目录读取中…";

  const rows = items.map((p) => [
    el("span", { class: "mono", text: p.id || "" }),
    el("span", {}, [
      document.createTextNode(String(p.display_name || p.name || "")),
      p.installed ? badge("已安装", "ok") : null,
    ]),
    p.author || "",
    p.license && p.license !== "unknown" ? badge(p.license, "warn") : badge("unknown", "bad"),
    el("span", { class: "mono", text: String(p.repo || "").replace("https://github.com/", "") }),
    el("div", { class: "row" }, [
      el("button", {
        class: "act",
        text: p.installed ? "覆盖重装并加载" : "安装并加载",
        onclick: async (ev) => {
          ev.target.disabled = true;
          ev.target.textContent = "安装中…";
          const out = await apiPost("/market/install", { source, id: p.id });
          const load = out.load || {};
          const ok = !!(out.ok && load.ok !== false);
          h.detail = ok ? (load.report || "已安装，运行时加载没给出报告") : (out.reason || JSON.stringify(out).slice(0, 200));
          note(ok ? `已装 ${p.name || p.id}｜许可证 ${out.license || "装时才判定"}` : `装失败 ${p.name || p.id}：${out.reason || ""}`, !ok);
          await marketFetch(source);
          await refresh(true);
        },
      }),
    ]),
  ]);

  return el("div", {}, [
    pageHead(meta.title, meta.hint),
    el("div", { class: "row" }, [
      queryIn,
      el("button", { class: "act", text: "搜索", onclick: search }),
      el("button", {
        class: "ghost",
        text: "重新载入目录",
        onclick: async () => {
          await marketFetch(source, true);  // 强制重取：命中缓存时这句才是真话
          await refresh(true);
        },
      }),
      el("span", { class: "grow" }),
      el("button", {
        class: "ghost",
        text: "← 上一页",
        disabled: h.offset <= 0 ? "" : null,
        onclick: async () => {
          h.offset = Math.max(0, h.offset - h.limit);
          await marketFetch(source);  // 别指望下个渲染周期替我们发现 offset 变了 —— 我就是在这儿丢过一次翻页
          await refresh(true);
        },
      }),
      el("span", { class: "mono", text: pageLine }),
      el("button", {
        class: "ghost",
        text: "下一页 →",
        disabled: !(h.res && h.res.has_more) ? "" : null,
        onclick: async () => {
          h.offset += h.limit;
          await marketFetch(source);
          await refresh(true);
        },
      }),
    ]),
    h.res && h.res.notice ? el("div", { class: "note", text: h.res.notice }) : null,
    h.res && h.res.stale ? el("div", { class: "note", text: h.res.stale }) : null,
    h.detail ? el("div", { class: "result-line", text: h.detail } ) : null,
    card(
      `${items.length} 条可装`,
      "点安装会下载、落盘、并立刻试着装进两个正在跑的脑子；需要 pip 依赖的会被政策先拒绝",
      table(["标识", "名称", "作者", "许可证", "仓库", "动作"], rows, h.error || (h.fetching ? "目录读取中…" : "目录里还没有条目"))
    ),
  ]);
}

/* ---------------- 页面：日志 / 设置 / 凭据 ---------------- */

async function viewLogs() {
  const qs = "?limit=250" + (state.logLevel ? "&level=" + state.logLevel : "");
  const data = await apiGet("/logs" + qs);
  const box = el("div", {});
  for (const row of data.items || []) {
    box.appendChild(
      el("div", { class: "logline" }, [
        el("span", { text: new Date(row.ts * 1000).toLocaleTimeString("zh-CN", { hour12: false }) }),
        el("span", { class: "lv-" + row.level, text: row.level }),
        el("span", { text: row.name }),
        el("span", { class: "msg", text: row.message }),
      ])
    );
  }
  const levelSel = el("select", { name: "log-level", onchange: (ev) => { state.logLevel = ev.target.value; refresh(true); } }, [
    el("option", { value: "", text: "全部级别" }),
    el("option", { value: "INFO", text: "INFO" }),
    el("option", { value: "WARNING", text: "WARNING" }),
    el("option", { value: "ERROR", text: "ERROR" }),
  ]);
  levelSel.value = state.logLevel;
  return el("div", {}, [
    pageHead("日志", "只在内存环形缓冲里，不落盘、不外传"),
    el("div", { class: "row" }, [levelSel, el("span", { class: "note", text: `缓冲 ${data.buffered || 0} 条；两家引擎的日志都汇到这一条流` })]),
    box,
  ]);
}

async function viewSettings() {
  const data = await apiGet("/settings");
  const editors = {};
  const fields = el("div", {});
  for (const f of data.fields || []) {
    let input;
    if (f.kind === "bool") {
      input = el("input", { type: "checkbox", name: "set-" + f.field });
      input.checked = !!f.value;
    } else if (f.kind === "num") {
      input = el("input", { type: "number", name: "set-" + f.field, value: f.value ?? "", min: f.min, max: f.max, step: f.step });
    } else {
      input = el("input", { type: "text", name: "set-" + f.field, value: Array.isArray(f.value) ? f.value.join(",") : f.value || "" });
    }
    editors[f.field] = input;
    fields.appendChild(
      el("div", { class: "row" }, [
        el("span", { style: "min-width:170px;display:inline-block", text: f.label }),
        input,
        el("span", { class: "note", text: f.note || "" }),
      ])
    );
  }
  const out = el("div", { class: "result-line", text: "" });
  const save = el("button", {
    class: "act",
    text: "保存并生效",
    onclick: async (ev) => {
      const patch = {};
      for (const [name, node] of Object.entries(editors)) patch[name] = node.type === "checkbox" ? node.checked : node.value;
      ev.target.disabled = true;
      const res = await apiPost("/settings", { patch });
      ev.target.disabled = false;
      out.textContent = JSON.stringify(res, null, 1);
      note(res.ok ? "已写入并当场生效（needs_restart 的下次启动才生效）" : "部分字段被拒绝", !res.ok);
    },
  });
  return el("div", {}, [
    pageHead("设置", "改的是运行中的闸门；同时写进 astrmai_state/settings.json 供重启还原"),
    card("闸门", "", [fields, el("div", { class: "row" }, [save]), out]),
    card("两家上游自带的面板", "都不该开着：端口和凭据面会翻倍", facts(Object.entries(data.upstream_panels || {}))),
  ]);
}

async function viewSecurity() {
  const [data, ov, auth] = [await apiGet("/settings"), await overview(), await authStatus()];
  const env = data.runtime_env || {};
  const priv = ov.privacy || {};
  const events = (auth.recent_auth || []).map((e) => [new Date(e.ts * 1000).toLocaleTimeString("zh-CN", { hour12: false }), e.kind, e.who, e.note]);
  const locks = (auth.recent_locks || []).map((e) => [new Date(e.ts * 1000).toLocaleTimeString("zh-CN", { hour12: false }), e.kind, e.who, e.note]);
  return el("div", {}, [
    pageHead("凭据与隐私", "这台机器上谁能看、什么会出网、口令怎么存"),
    card("访问控制现状", auth.tls ? "已启用 TLS" : "未启用 TLS（只该在回环或已加密链路里用）", facts([
      ["面板地址", `${auth.tls ? "https" : "http"}://${env.panel_host}:${env.panel_port}`],
      ["登录", auth.authenticated ? "本会话已登录" : "未登录"],
      ["口令存储", `${auth.kdf}（内存困难哈希）｜明文不落盘、不进日志、不进响应`],
      ["强制改初始密码", auth.must_change ? "还没改，其它功能不放行" : "已改过"],
      ["机器 token", auth.token_configured ? "已配置（同样只存哈希）" : "未配置"],
      ["活动会话", `${auth.sessions} 个｜空闲 30 分钟 / 绝对 8 小时过期，改密即全废`],
      ["爆破锁定", auth.lockout_left ? `本来源已锁 ${auth.lockout_left}s` : `连错 ${5} 次锁 60s，之后指数递增到 15 分钟`],
      ["写操作", "额外要求 Origin 与 Host 一致（挡 CSRF）"],
    ])),
    el("div", { class: "grid cols-2" }, [
      card("最近登录事件", "只记来源与结果，不记口令", table(["时间", "事件", "来源", "备注"], events, "还没有登录事件")),
      card("最近锁定记录", "按来源统计失败次数", table(["时间", "事件", "来源", "备注"], locks, "没有被锁过")),
    ]),
    card("什么会出网", "", facts([
      ["遥测心跳", priv.telemetry],
      ["市场查询", priv.market_network],
      ["模型请求主机", Object.values((ov.model_bindings || {}).tasks || {}).filter((v) => v.host).map((v) => v.host).join("、") || "—"],
      ["凭据外泄", "所有响应过 scrub()：凭据字段只报有没有，值一律不外传"],
    ])),
    card("运行环境（只读）", "这些由启动参数决定，面板不改它们", facts([
      ["ASTRMAI_TELEMETRY", env.telemetry],
      ["ASTRMAI_MARKET_NETWORK", env.market_network],
      ["被环境变量钉住的字段", (data.env_overridden || []).join(", ") || "无"],
      ["落盘设置", (data.stored_keys || []).join(", ") || "无"],
    ])),
  ]);
}

/* ---------------- 页面：许可证 / 关于 ---------------- */

async function viewLicenses() {
  const data = await apiGet("/licenses");
  const rows = (data.items || []).map((r) => [
    r.component,
    r.source,
    r.undetermined ? badge("unknown", "bad") : badge(r.license, r.confidence === "high" ? "ok" : "warn"),
    r.basis || "",
    r.commit ? el("span", { class: "mono", text: r.commit }) : "",
  ]);
  return el("div", {}, [
    pageHead("许可证清单", data.ok === false ? data.reason : `共 ${data.total || 0} 项，无法确定 ${data.unknown_count || 0} 项`),
    (data.unknown_count || 0) > 0 ? card("这些还没法确定", "不分发时是信息，要分发时是依据", el("ul", { class: "plain" }, (data.unknown || []).map((n) => el("li", { text: n })))) : null,
    table(["组件", "来源", "许可证", "依据", "commit"], rows, "还没有装过任何插件"),
  ]);
}

async function viewAbout() {
  const data = await apiGet("/about");
  return el("div", {}, [
    pageHead("分发口径", `${data.product} ${data.version}`),
    card("astrmai", data.license, [facts([["整体许可", data.license]]), el("p", { class: "note", text: data.conveyance }), el("p", { class: "note", text: data.provenance })]),
    el("div", { class: "grid cols-2" }, [
      card("上游", "两个引擎各自的许可", table(["项目", "许可证"], (data.upstream || []).map((u) => [u.name, u.license]), "")),
      card("隐私与凭据", "面板自己也守这几条", el("ul", { class: "plain" }, (data.privacy || []).concat([data.secrets_policy]).map((t) => el("li", { text: t })))),
    ]),
  ]);
}

/* ---------------- 页面：AstrBot 功能面（provider / 人格 / 对话 / cron） ---------------- */

function actionButton(label, path, body, confirmText) {
  return el("button", {
    class: "ghost",
    text: label,
    onclick: async (ev) => {
      if (confirmText && !window.confirm(confirmText)) return;
      ev.target.disabled = true;
      const out = await apiPost(path, body);
      ev.target.disabled = false;
      note(out.ok ? out.reason : `失败：${out.reason || out.error || "未知"}`, !out.ok);
      if (out.ok) await refresh(true);
    },
  });
}

async function viewProviders() {
  const data = await apiGet("/providers");
  const rows = (data.items || []).map((p) => [
    el("span", { class: "mono", text: p.id }),
    p.type || "—",
    p.model || "—",
    p.api_base || "—",
    p.key_configured ? badge(`密钥 ×${p.key_count}`, "ok") : badge("无密钥", "bad"),
    p.enable ? badge("启用", "ok") : badge("停用", ""),
    p.is_default ? badge("默认", "ab") : (p.loaded ? "" : "未加载"),
    el("span", { class: "row" }, [
      actionButton("测试", "/providers/apply", { action: "test", id: p.id }),
      p.is_default ? "" : actionButton("设默认", "/providers/apply", { action: "default", id: p.id }),
      actionButton(p.enable ? "停用" : "启用", "/providers/apply", { action: "enable", id: p.id, enabled: !p.enable }),
      actionButton("删除", "/providers/apply", { action: "delete", id: p.id }, `删除 provider ${p.id}？运行中的实例会被终止。`),
    ]),
  ]);
  const idInput = el("input", { type: "text", name: "prov-id", placeholder: "my-provider" });
  const typeInput = el("input", { type: "text", name: "prov-type", value: "openai_chat_completion" });
  const modelInput = el("input", { type: "text", name: "prov-model", placeholder: "gpt-4o-mini / deepseek-chat …" });
  const baseInput = el("input", { type: "text", name: "prov-base", placeholder: "https://api.example.com/v1" });
  const keyInput = el("input", { type: "password", name: "prov-key", placeholder: "API key（只写不读回）" });
  const createBtn = el("button", {
    class: "act",
    text: "创建并加载",
    onclick: async (ev) => {
      const config = { id: idInput.value.trim(), type: typeInput.value.trim(), model: modelInput.value.trim(), api_base: baseInput.value.trim() };
      if (keyInput.value.trim()) config.key = [keyInput.value.trim()];
      if (!config.id) { note("id 不能为空", true); return; }
      ev.target.disabled = true;
      const out = await apiPost("/providers/apply", { action: "create", config });
      ev.target.disabled = false;
      note(out.ok ? out.reason : `创建失败：${out.reason}`, !out.ok);
      if (out.ok) { keyInput.value = ""; await refresh(true); }
    },
  });
  return el("div", {}, [
    pageHead("服务提供商", "AstrBot 的 provider 管理面：创建即热加载，密钥只写不读回"),
    data.ok === false ? card("拿不到", data.reason || "") : null,
    card("已配置的 provider", data.default_provider_id ? `当前默认：${data.default_provider_id}` : "还没设默认（AstrBot 会用第一个可用的）", table(
      ["ID", "类型", "模型", "主机", "密钥", "状态", "默认", "动作"], rows, data.items ? "还没有配置任何 provider" : (data.reason || "读不到")
    )),
    card("新建 provider（OpenAI 兼容格式）", "创建后会走真加载：连不上就会创建失败并回滚，不会留下死配置", el("div", {}, [
      el("div", { class: "row" }, [el("span", { style: "min-width:60px;display:inline-block", text: "id" }), idInput]),
      el("div", { class: "row" }, [el("span", { style: "min-width:60px;display:inline-block", text: "type" }), typeInput, el("span", { class: "note", text: "openai_chat_completion 等" })]),
      el("div", { class: "row" }, [el("span", { style: "min-width:60px;display:inline-block", text: "model" }), modelInput]),
      el("div", { class: "row" }, [el("span", { style: "min-width:60px;display:inline-block", text: "api_base" }), baseInput]),
      el("div", { class: "row" }, [el("span", { style: "min-width:60px;display:inline-block", text: "key" }), keyInput, el("span", { class: "note", text: "写进配置后任何页面都不会再显示它" })]),
      el("div", { class: "row" }, [createBtn]),
    ])),
  ]);
}

async function viewPersonas() {
  const data = await apiGet("/personas");
  const rows = (data.items || []).map((p) => [
    el("span", { class: "mono", text: p.persona_id }),
    (p.system_prompt || "").slice(0, 60) + (p.system_prompt && p.system_prompt.length > 60 ? "…" : ""),
    p.dialog_count ? `${p.dialog_count} 句开场白` : "—",
    p.is_default ? badge("系统默认", "ab") : actionButton("删除", "/personas/apply", { action: "delete", persona_id: p.persona_id }, `删除人格 ${p.persona_id}？`),
  ]);
  const pidInput = el("input", { type: "text", name: "per-id", placeholder: "my-persona" });
  const promptInput = el("textarea", { name: "per-prompt", rows: 4, placeholder: "系统提示词（system prompt）" });
  const dialogsInput = el("textarea", { name: "per-dialogs", rows: 2, placeholder: "开场白，一行一句（可留空）" });
  const createBtn = el("button", {
    class: "act",
    text: "创建人格",
    onclick: async (ev) => {
      const begin_dialogs = dialogsInput.value.split("\n").map((s) => s.trim()).filter(Boolean);
      const out = await apiPost("/personas/apply", { action: "create", persona_id: pidInput.value.trim(), system_prompt: promptInput.value, begin_dialogs });
      note(out.ok ? out.reason : `创建失败：${out.reason}`, !out.ok);
      if (out.ok) { pidInput.value = ""; promptInput.value = ""; dialogsInput.value = ""; await refresh(true); }
    },
  });
  return el("div", {}, [
    pageHead("人格", "AstrBot 的 persona 管理：改的是同一个 data_v4.db，官方面板里建的一样生效"),
    data.ok === false ? card("拿不到", data.reason || "") : null,
    card("已有人格", data.default_persona ? `默认人格：${data.default_persona}` : "", table(["persona_id", "系统提示词", "开场白", "动作"], rows, data.items ? "还没有人格（系统 default 是初始化时建的）" : (data.reason || "读不到"))),
    card("新建人格", "default 是系统保留 ID，不能建", el("div", {}, [
      el("div", { class: "row" }, [el("span", { style: "min-width:70px;display:inline-block", text: "persona_id" }), pidInput]),
      el("div", { class: "row" }, [el("span", { style: "min-width:70px;display:inline-block", text: "提示词" }), promptInput]),
      el("div", { class: "row" }, [el("span", { style: "min-width:70px;display:inline-block", text: "开场白" }), dialogsInput]),
      el("div", { class: "row" }, [createBtn]),
    ])),
  ]);
}

async function viewConversations() {
  const page = Number(new URLSearchParams(location.search).get("cp") || "1");
  const data = await apiGet("/conversations?page=" + page);
  const items = data.items || [];
  const detail = state.hold["convDetail"] || "";
  let detailBox = null;
  if (detail) {
    const hist = await apiGet(`/conversations/history?cid=${encodeURIComponent(detail)}`);
    detailBox = card(`对话 ${detail}`, (hist.persona_id ? `人格 ${hist.persona_id}｜` : "") + `${(hist.messages || []).length} 条消息`, el("div", {}, (hist.messages || []).map((m) =>
      el("div", { class: "logline" }, [
        el("span", { class: "owner-" + (m.role === "assistant" ? "astrbot" : "maibot"), text: m.role }),
        el("span", { class: "msg", text: m.content }),
      ])
    )));
  }
  const rows = items.map((c) => [
    el("button", { class: "ghost", text: "看", onclick: () => { state.hold["convDetail"] = c.cid; refresh(true); } }),
    el("span", { class: "mono", text: (c.cid || "").slice(0, 8) }),
    c.platform_id || "—",
    (c.user_id || "").slice(0, 30),
    c.title || c.persona_id || "—",
    String(c.message_count),
    c.token_usage ? String(c.token_usage) : "—",
    c.updated_at ? new Date(c.updated_at * 1000).toLocaleString("zh-CN", { hour12: false }) : "—",
  ]);
  const totalPages = Math.max(1, Math.ceil((data.total || 0) / (data.limit || 20)));
  return el("div", {}, [
    pageHead("对话历史", `AstrBot 的 conversations 表，只读；共 ${data.total ?? "—"} 条`),
    data.ok === false ? card("拿不到", data.reason || "") : null,
    detailBox,
    card("对话列表", detail ? `正在看 ${detail.slice(0, 8)}（点别的「看」换一条）` : "点「看」展开消息正文", table(
      ["", "cid", "平台", "会话", "标题/人格", "消息数", "token", "更新时间"], rows, data.items ? "还没有对话记录" : (data.reason || "读不到")
    )),
    el("div", { class: "row" }, [
      el("button", { class: "ghost", text: "← 上一页", disabled: page <= 1 ? "" : null, onclick: () => { history.replaceState(null, "", "?cp=" + (page - 1)); refresh(true); } }),
      el("span", { class: "mono", text: `第 ${page} / ${totalPages} 页` }),
      el("button", { class: "ghost", text: "下一页 →", disabled: page >= totalPages ? "" : null, onclick: () => { history.replaceState(null, "", "?cp=" + (page + 1)); refresh(true); } }),
    ]),
  ]);
}

async function viewCron() {
  const data = await apiGet("/cron");
  const rows = (data.items || []).map((j) => [
    el("span", { class: "mono", text: j.job_id }),
    j.name || "—",
    j.type || "—",
    j.cron || "（一次性）",
    j.enabled ? badge("启用", "ok") : badge("停用", ""),
    j.next_run || "—",
    j.last_run || "—",
    j.last_error ? el("span", { class: "bad", text: j.last_error.slice(0, 60) }) : "",
    el("span", { class: "row" }, [
      actionButton("立即跑", "/cron/apply", { action: "run_now", job_id: j.job_id }),
      actionButton(j.enabled ? "停用" : "启用", "/cron/apply", { action: "toggle", job_id: j.job_id, enabled: !j.enabled }),
      actionButton("删除", "/cron/apply", { action: "delete", job_id: j.job_id }, `删除定时任务 ${j.name || j.job_id}？`),
    ]),
  ]);
  return el("div", {}, [
    pageHead("定时任务", "AstrBot 的 cron：改的是同一个调度器，立即执行的结果看日志页"),
    data.ok === false ? card("拿不到", data.reason || "") : null,
    card("任务列表", "", table(["job_id", "名称", "类型", "cron 表达式", "状态", "下次", "上次", "错误", "动作"], rows, data.items ? "还没有定时任务" : (data.reason || "读不到"))),
  ]);
}

const VIEWS = {
  load: viewLoad,
  engines: viewEngines,
  ledger: viewLedger,
  sessions: viewSessions,
  "plugins:astrbot": () => viewPlugins("astrbot"),
  "plugins:maibot": () => viewPlugins("maibot"),
  "plugins:astrmai": () => viewPlugins("astrmai"),
  tools: viewTools,
  "abfeature:providers": viewProviders,
  "abfeature:personas": viewPersonas,
  "abfeature:conversations": viewConversations,
  "abfeature:cron": viewCron,
  "market:astrbot": () => marketView("astrbot"),
  "market:maibot": () => marketView("maibot"),
  logs: viewLogs,
  settings: viewSettings,
  security: viewSecurity,
  licenses: viewLicenses,
  about: viewAbout,
};

/* ---------------- 多级菜单 ---------------- */

const collapsed = JSON.parse(sessionStorage.getItem("astrmai_nav") || "{}");

function crumbOf(key) {
  const out = [];
  (function walk(nodes, trail) {
    for (const n of nodes) {
      const next = n.label ? trail.concat(n.label) : trail;
      if (n.key === key) {
        out.push(...next);
        return true;
      }
      if (n.children && walk(n.children, next)) return true;
    }
    return false;
  })(NAV, []);
  return out;
}

function renderNav() {
  const nav = document.getElementById("nav");
  nav.textContent = "";
  for (const group of NAV) {
    nav.appendChild(el("div", { class: "group-label", text: group.label }));
    nav.appendChild(renderNodes(group.children));
  }
}

function renderNodes(nodes) {
  const frag = document.createDocumentFragment();
  for (const node of nodes) {
    if (node.children) {
      const isOpen = !collapsed[node.key];
      const box = el("div", { class: "sub" + (isOpen ? "" : " collapsed") });
      const head = el("button", {
        class: "branch",
        onclick: () => {
          collapsed[node.key] = !collapsed[node.key];
          sessionStorage.setItem("astrmai_nav", JSON.stringify(collapsed));
          renderNav();
        },
      }, [node.label, el("span", { class: "twisty", text: isOpen ? "−" : "+" })]);
      box.appendChild(renderNodes(node.children));
      frag.appendChild(head);
      frag.appendChild(box);
    } else {
      frag.appendChild(
        el("button", {
          class: state.route === node.key ? "leaf" : "leaf",
          "aria-current": state.route === node.key ? "page" : "false",
          text: node.label,
          onclick: () => go(node.key),
        })
      );
    }
  }
  return frag;
}

function go(key) {
  if (!VIEWS[key]) return;
  state.route = key;
  state.crumb = crumbOf(key);
  renderNav();
  refresh(true);
}

/* ---------------- 刷新循环 ---------------- */

async function refresh(force = false) {
  // 一次只跑一个刷新：目录读取可能要十几秒，让定时器插进来会互相覆盖
  if (state.busy) {
    state.pendingRefresh = true;
    return;
  }
  state.busy = true;
  const stage = document.getElementById("stage");
  const keepScroll = window.scrollY;
  try {
    const status = await authStatus();
    if (!authGate(status)) return;
    if (force) {
      state.data.load = await apiGet("/load");
      state.data.overview = await apiGet("/overview");
    }
    const ld = state.data.load || (await apiGet("/load"));
    renderKpis(ld);
    renderLamps(state.data.overview || (await overview()));
    const kept = captureInputs();
    const node = await VIEWS[state.route]();
    stage.textContent = "";
    stage.appendChild(node);
    restoreInputs(kept);
    setStatus(`${state.crumb.join(" / ")} · ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`);
    document.getElementById("clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  } catch (err) {
    setStatus("读取失败：" + err.message, true, 8000);
  } finally {
    state.busy = false;
    window.scrollTo(0, keepScroll);
    if (state.pendingRefresh) {
      state.pendingRefresh = false;
      refresh(true);
    }
  }
}

let timer = null;
function setAuto(on) {
  if (timer) clearInterval(timer);
  timer = on ? setInterval(() => refresh(true), 3000) : null;
}

document.getElementById("refresh").addEventListener("click", () => refresh(true));
document.getElementById("autoscroll").addEventListener("change", (ev) => setAuto(ev.target.checked));

function mountLogout() {
  const lamps = document.getElementById("lamps");
  if (lamps.querySelector("#logout")) return;
  lamps.appendChild(
    el("button", { id: "logout", class: "ghost", text: "退出", onclick: logout })
  );
}

async function boot() {
  const status = await authStatus();
  if (!status.authenticated) {
    loginScreen(status);
  } else if (status.must_change) {
    renderChangeScreen(status);
  } else {
    renderNav();
    mountLogout();
    go("load");
  }
  setAuto(document.getElementById("autoscroll").checked);
}

boot();
