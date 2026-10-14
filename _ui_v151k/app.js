/* 微博bot小助手 前端逻辑（原生 JS，通过 window.pywebview.api 调用后端） */
"use strict";

/* 【v1.5.0 诊断】捕获顶层/未处理异常，写入窗口标题供外部启动测试诊断
   （例如 pywebview 桥未就绪导致的静默失败、导致开屏永远卡 4%）。 */
window.addEventListener("error", function (e) {
  try { document.title = "JSERR:" + ((e && e.message) || "unknown").slice(0, 90); } catch (x) {}
});
window.addEventListener("unhandledrejection", function (e) {
  try {
    var _m = e && e.reason ? (e.reason.message || String(e.reason)) : "unknown";
    document.title = "REJECT:" + _m.slice(0, 90);
  } catch (x) {}
});


let API = null;
let STATE = null;
let histKind = "monitored";
let HIST_SELECTED = new Set();   // 历史记录选中的行 id
let MON_AUTO_TIMER = null;       // 监控自动刷新定时器
let MON_LAST_REFRESH = 0;
let _pollTimer = null;           // 「立即轮询一次」进度轮询定时器

function apiCall(method, ...args) {
  if (!API) return Promise.reject("pywebview 未就绪");
  return API[method](...args);
}

function toast(msg, type) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "toast show" + (type ? " " + type : "");
  setTimeout(() => { t.className = "toast"; }, 2200);
}

function esc(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* 用系统默认浏览器打开外链（交给后端 webbrowser.open，避免 WebView2 内跳转） */
function openExternal(url) {
  if (!url) return;
  apiCall("open_external", String(url)).catch(err => {
    console.error("open_external failed:", err);
    toast("打开链接失败", "err");
  });
}

/* 复制文本到剪贴板（优先 navigator.clipboard，降级 execCommand） */
function copyText(text) {
  const value = String(text == null ? "" : text);
  const done = () => toast("已复制：" + value, "ok");
  const fail = () => toast("复制失败，请手动复制：" + value, "err");
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(value).then(done).catch(() => {
      if (_legacyCopy(value)) done(); else fail();
    });
    return;
  }
  if (_legacyCopy(value)) done(); else fail();
}

/* 剪贴板降级方案：临时 textarea + execCommand（WebView2 旧内核可用） */
function _legacyCopy(value) {
  try {
    const ta = document.createElement("textarea");
    ta.value = value;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch (e) {
    return false;
  }
}

/* 【v1.4.0】数字输入框自定义上下按钮：统一风格 + 点击步进。
   包裹层 + 两个 absolute 按钮，兼容静态与动态渲染（MutationObserver）。 */
function decorateNumberInput(inp) {
  if (!inp || inp.type !== "number" || inp.dataset.numDecorated) return;
  /* 【v1.4.9】这些输入框不加自定义上下按钮 —— 它们空间窄、按钮会压住 placeholder，
     且用户实际习惯直接键入。用原生 number 输入 + 隐藏 spin 按钮即可。
     · 分页相关：自定义每页条数（页码跳转框已于 v1.4.23 移除）
     · 间隔 / 页数类：监控刷新间隔、转发间隔、相册间隔、扫描页数 */
  var NO_STEP = ["pageSizeNum", "monRefreshMin", "intervalMin", "mediaInterval", "mediaPages"];
  if (NO_STEP.indexOf(inp.id) >= 0) {
    inp.dataset.numDecorated = "1";   // 标记为已处理，MutationObserver 不再重复进入
    inp.classList.add("no-step");
    return;
  }
  inp.dataset.numDecorated = "1";
  var wrap = document.createElement("span");
  wrap.className = "num-wrap";
  inp.parentNode.insertBefore(wrap, inp);
  wrap.appendChild(inp);
  function mkStep(cls, ch, delta) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "num-step " + cls;
    b.textContent = ch;
    b.setAttribute("tabindex", "-1");
    b.addEventListener("mousedown", function (e) { e.preventDefault(); });
    b.addEventListener("click", function (e) {
      e.preventDefault();
      var min = inp.min !== "" ? parseFloat(inp.min) : null;
      var max = inp.max !== "" ? parseFloat(inp.max) : null;
      var step = parseFloat(inp.step) || 1;
      var val = parseFloat(inp.value);
      if (isNaN(val)) val = (min !== null && !isNaN(min)) ? min : 0;
      val += delta * step;
      if (min !== null && !isNaN(min)) val = Math.max(min, val);
      if (max !== null && !isNaN(max)) val = Math.min(max, val);
      inp.value = val;
      inp.dispatchEvent(new Event("input", { bubbles: true }));
      inp.dispatchEvent(new Event("change", { bubbles: true }));
    });
    return b;
  }
  wrap.appendChild(mkStep("up", "▲", 1));
  wrap.appendChild(mkStep("down", "▼", -1));
}
function decorateNumberInputs(root) {
  if (!root || !root.querySelectorAll) return;
  var list = root.querySelectorAll('input[type="number"]');
  for (var i = 0; i < list.length; i++) decorateNumberInput(list[i]);
}

/* ---------------- 初始化 ---------------- */
async function init() {
  API = window.pywebview.api;

  // 【v1.1.9k 0010 根治「启动 10 次有 9 次未响应」】
  // 旧写法是 init() 第一件事就 `await loadState()`，而 loadState 里的
  // apiCall("get_state") 走 pywebview 桥、会在 WebView2 的 **UI 线程上同步执行**。
  // 只要后端冷启动要几百毫秒到数秒（69MB history.db 查询 + 迁移检查），
  // 整个窗口在此期间无法处理任何输入 → Windows 判定「未响应」。
  // 现在改为：**先把界面骨架渲染出来让窗口立刻可交互**，再去拉状态。
  bindNav();
  /* 【v1.4.12】侧栏滚动 / 窗口尺寸变化时，高亮滑块重新贴合当前选中行 */
  const _side = document.getElementById("sidebar");
  if (_side) _side.addEventListener("scroll", () => syncNavHighlight(), { passive: true });
  window.addEventListener("resize", () => { syncNavHighlight(); layoutFwdRing(); });
  bindDataOverviewTabs();
  updateOverviewClock();
  /* 【v1.4.9】开屏进度：界面骨架已就绪，开始接触后端 */
  bootProgress(15, "界面骨架已就绪，正在连接后端…");

  setInterval(updateWhBar, 30000);      // 工作时间条实时更新
  setInterval(monAutoTick, 15000);     // 监控列表定时刷新
  setInterval(refreshOverviewLive, 1000);  // 【v1.4.18】概览轮询改为秒级（后端缓存优先）
  setInterval(refreshSystemLoad, 2000);    // 【v1.4.0】系统负载轮询
  setInterval(checkAccountHealth, 60000);  // 【v1.4.0】账户健康轮询（后端缓存，1h 才真重算）
  setInterval(updateOverviewClock, 1000);  // 概览时钟每秒刷新（纯前端）
  setInterval(refreshAccountsLive, 1000);  // 【v1.4.18】账户管理页秒级刷新（仅激活时拉取）
  tickForwardRing();   // 【v1.4.18】启动本日转发进度环动画循环

  // 【v1.4.0】数字输入框自定义上下按钮：先装饰已存在的，再用 MutationObserver 覆盖动态渲染的
  decorateNumberInputs(document);
  var _numObserver = new MutationObserver(function (muts) {
    for (var i = 0; i < muts.length; i++) {
      var m = muts[i];
      if (!m.addedNodes) continue;
      for (var j = 0; j < m.addedNodes.length; j++) {
        var n = m.addedNodes[j];
        if (n.nodeType !== 1) continue;
        if (n.matches && n.matches('input[type="number"]')) decorateNumberInput(n);
        if (n.querySelectorAll) decorateNumberInputs(n);
      }
    }
  });
  _numObserver.observe(document.body, { childList: true, subtree: true });

  // 【v1.4.0】账户健康：首屏先拉一次（后端未就绪时由 60s 轮询兜底）
  bootProgress(30, "正在检测账户状态…");
  checkAccountHealth();
  // 【v1.4.4】开屏兜底：任何异常路径下 8s 后强制淡出，绝不让开屏卡死
  setTimeout(hideBootLoading, 8000);

  // 异步拉状态：不阻塞首屏。get_state 冷启动已改为「立即返回空壳 + 后台重建」。
  bootProgress(45, "正在读取主状态（账户 / 监控 / 转发配置）…");
  loadState()
    .then(() => {
      bootProgress(70, "正在渲染账户与转发配置…");
      renderAbout();
      // 【v1.2.2】首启向导：只在后端明确告知「还没设置过路径」时弹，
      // 且放在 loadState 之后 —— 保证遮罩盖在已渲染好的界面上。
      checkWizard();
      // 【v1.2.2】拉一次存储扫描状态并按需轮询。系统概览的「数据载入中」
      // 进度条靠它驱动 —— 后台统计期间界面完全可交互。
      bootProgress(82, "正在扫描存储目录…");
      applyStorageStatus(STATE && STATE.storage);
      _ensureStoragePoll();
      // 拿到空壳说明后端还在后台建状态，短时间内重试几次，
      // 让真实数据尽快到位（否则要干等 5s 的概览轮询才刷新）。
      if (STATE && STATE.loading) {
        bootProgress(90, "后端正在后台构建完整状态，继续等待…");
        retryLoadRealState();
      } else {
        bootProgress(100, "加载完成");
      }
    })
    .catch(err => {
      console.error("loadState failed:", err);
      bootProgress(100, "主状态读取失败，已跳过部分数据");
    });

  syncLogVerbose();
}

/* 空壳 → 真实数据 的快速重试（最多 ~6s，间隔递增）。
   后端 _build_state 一旦完成，_STATE_CACHE 就有数据，随后的 get_state 立即返回真实值。 */
async function retryLoadRealState(attempt) {
  const n = attempt || 0;
  if (n > 12) { bootProgress(100, "等待后端状态超时，已进入主界面"); hideBootLoading(); return; }   // 约 6s 后放弃，交给 5s 轮询兜底
  await new Promise(r => setTimeout(r, 300 + n * 120));
  if (STATE && !STATE.loading) { bootProgress(100, "加载完成"); return; }         // 已有真实数据，交给 loadState 渲染
  /* 【v1.4.9】重试期间让进度条缓慢逼近 99%，并把当前重试次数显示出来，
     这样「卡住」时能看出是后端迟迟不回，还是前端没在等。 */
  bootProgress(90 + Math.min(8, n * 0.7), "后端仍在构建状态，第 " + (n + 1) + " 次重试…");
  try {
    await loadState();
  } catch (e) { /* 忽略，下一轮再试 */ }
  if (STATE && STATE.loading) retryLoadRealState(n + 1);
  else { bootProgress(100, "加载完成"); renderAbout(); }
}

/* 同步「冗余调试日志」开关的当前状态 */
async function syncLogVerbose() {
  try {
    const res = await apiCall("get_log_verbose");
    const el = document.getElementById("logVerbose");
    if (el && res) el.checked = !!res.verbose;
  } catch (e) { /* 忽略：日志页面尚未渲染 */ }
}

/* 【v1.4.3】状态 epoch 同步：后端每次变更类操作推进 epoch 并随 get_state 返回，
   前端记下已渲染到的 epoch，用于 refreshStateFresh 判断「是否已拿到变更后的新状态」 */
let _seenEpoch = 0;
function _syncEpoch(st) {
  if (st && typeof st.epoch === "number") _seenEpoch = Math.max(_seenEpoch, st.epoch);
}

/* 【v1.4.3】从 loadState 抽出的全量渲染：拿到新 STATE 后统一重绘所有页面 */
function renderAllViews() {
  bootProgress(100, "加载完成");
  hideBootLoading();
  renderOverview();
  renderAccounts();
  syncAutoRenew();
  renderMonitors();
  renderForward();
  renderAI();
  renderBlacklist();
  renderSchedule();
  renderLicense();
  renderMonAutoRefresh();
  applyUncBanner();
  applyStorageStatus(STATE.storage);
  _ensureStoragePoll();
  checkAccountHealth();   /* 【v1.4.4】徽章随每次状态刷新即刻同步（后端快照零网络，开销极低） */
}

/* 【v1.4.3】变更类操作后的「确定拿到新状态」刷新。
   根因回顾：后端 get_state 是缓存优先（TTL=5s），此前操作后立刻 loadState()
   拿到的还是旧缓存 → 「添加账户后列表不出现」「启动轮询后仍显示未运行」
   「置顶没反应」，要等概览 5s 轮询 / 监控 15s tick 兜底，账户页则完全不刷新。
   现在：后端变更接口会推进 epoch 并使缓存过期；这里循环 get_state，
   直到返回的 epoch >= 目标值（缓存已重建、包含本次变更）才渲染，
   通常 200~600ms 内完成。最多等 4s，超时按最后一次拿到的状态渲染。 */
async function refreshStateFresh(timeoutMs) {
  const target = _seenEpoch + 1;
  const t0 = Date.now();
  for (;;) {
    const st = await apiCall("get_state");
    if (st && !st.loading) {
      const fresh = (typeof st.epoch !== "number") || st.epoch >= target;
      if (fresh || Date.now() - t0 > (timeoutMs || 4000)) {
        STATE = st; _syncEpoch(st);
        renderAllViews();
        return st;
      }
    }
    if (Date.now() - t0 > (timeoutMs || 4000)) return null;
    await new Promise(r => setTimeout(r, 200));
  }
}

async function loadState() {
  const st = await apiCall("get_state");
  if (!st) return;
  // 【v1.1.9k 0010】后端冷启动先返回空壳（loading:true）。此时立即把版本号 /
  // 版权等静态信息渲染出来让界面不空白，但**不**跑依赖完整数据的渲染函数，
  // 否则账户列表、监控列表会闪一下再被清空。
  const loading = !!st.loading;
  STATE = st;
  _syncEpoch(st);
  document.getElementById("verLabel").textContent = STATE.version.major;
  const copyLabel = document.getElementById("copyLabel");
  copyLabel.textContent = STATE.copyright;
  copyLabel.onclick = () => apiCall("open_external", "https://github.com/zouzuo1994321/Weibo_Bot");
  if (loading) {
    showBootLoading();
    applyUncBanner();
    // 【v1.2.2】即使是空壳，也把扫描状态渲染出来 —— 后台扫描早于状态缓存
    // 就已经开始了，空壳里的 storage 字段是有值的。
    applyStorageStatus(st.storage);
    return;
  }
  renderAllViews();
}

/* ---------------- 首屏加载提示 ----------------
   冷启动时后端在后台构建状态，这里给一行轻量提示，避免用户以为界面坏了。 */
function showBootLoading() {
  let el = document.getElementById("bootLoading");
  if (!el) {
    el = document.createElement("div");
    el.id = "bootLoading";
    el.className = "boot-loading";
    const main = document.getElementById("main");
    if (main) main.appendChild(el);
  }
  el.textContent = "正在加载数据…";
  el.style.display = "block";
}

/* 【v1.4.9】开屏进度：百分比 + 当前正在加载的具体内容。
   目的：冷启动涉及 onefile 解压、DB 连接、状态重建等多个环节，
   以前只有一个「正在加载数据…」的静态文案，卡住时无法判断卡在哪一步。
   现在每个关键节点调用 bootProgress(百分比, 阶段文案)，
   百分比驱动进度条长度（CSS 变量 --bs-pct），阶段文案显示在下方。 */
let _bootPct = 0;
function bootProgress(pct, stage) {
  const n = Math.max(0, Math.min(100, Math.round(pct)));
  // 单调递增，避免各阶段乱序调用导致进度条回退
  if (n > _bootPct) _bootPct = n;
  const bar = document.querySelector("#bootSplash .bs-bar i");
  if (bar) bar.style.setProperty("--bs-pct", _bootPct + "%");
  const pv = document.getElementById("bsPct");
  if (pv) pv.textContent = _bootPct + "%";
  const sv = document.getElementById("bsStage");
  if (sv && stage) sv.textContent = stage;
  /* 【v1.5.0】开屏阶段埋点：把每个进度阶段回传后端 diag 日志（fire-and-forget）。
     全程只调用约 10 次、每次 <1ms，可忽略开销；卡死时能对照
     「后端走到哪 / 前端走到哪 / js 桥是否还活着」。 */
  try {
    if (window.pywebview && window.pywebview.api) {
      window.pywebview.api.boot_stage(_bootPct, stage || "").catch(function () {});
    }
  } catch (e) { /* 诊断埋点绝不影响主流程 */ }
}

/* 【v1.5.0】UI_READY 只发一次的幂等标记 */
let _uiReadySent = false;

function hideBootLoading() {
  const el = document.getElementById("bootLoading");
  if (el) el.style.display = "none";
  /* 【v1.4.4】开屏动画淡出（幂等：重复调用只生效一次） */
  const sp = document.getElementById("bootSplash");
  if (sp && !sp.classList.contains("hide")) {
    sp.classList.add("hide");
    setTimeout(() => { if (sp.parentNode) sp.parentNode.removeChild(sp); }, 700);
  }
  /* 【v1.5.0】UI_READY 埋点：开屏层淡出 = 真正进入主界面（幂等，只发一次）。
     外部启动测试以后端 diag.log 出现 UI_READY 作为「成功进入主界面」判定，
     不再只看「窗口出现」—— 修复上一轮测试不严谨的问题。 */
  if (!_uiReadySent) {
    _uiReadySent = true;
    try {
      if (window.pywebview && window.pywebview.api) {
        window.pywebview.api.ui_ready(100, "splash_faded").catch(function () {});
      }
    } catch (e) { /* 诊断埋点绝不影响主流程 */ }
  }
}

function applyUncBanner() {
  const el = document.getElementById("uncBanner");
  if (!el) return;
  if (STATE && STATE.running_from_unc) {
    const reason = STATE.unc_reason || "网络路径";
    el.textContent = "⚠ 检测到程序运行于" + reason + "：数据写入、自动更新等功能可能受 NAS 只读权限或网络限制影响，建议将本程序复制到本地磁盘后运行。";
    el.style.display = "block";
  } else {
    el.style.display = "none";
  }
}

function refreshAll() { loadState().then(() => toast("已刷新")); }

/* ---------------- 导航 ----------------
   【v1.2.2】侧栏改为分组折叠结构：
     系统概览 / 基础功能(账户管理·监控列表·转发设置·相册监控)
              / AI 功能(AI 部署·AI 人格)
              / 数据管理(存储管理·历史记录·数据总览·运行日志)
              / 软件授权 / 关于软件
   分组头可点击折叠；进入某个子页时自动展开它所属的分组，
   否则用户会点到一个看不见高亮项的面板。 */
function _openGroupOf(view) {
  const item = document.querySelector('.nav-item[data-view="' + view + '"]');
  const g = item ? item.closest(".nav-group") : null;
  if (g) g.classList.add("open");
}

/* 【v1.4.12】侧栏高亮滑块：把 #navHighlight 平滑移动到目标行上。
   · top / height 用目标行相对 #sidebar 内容盒的 offset 计算（含滚动补偿）；
   · 平滑移动交给 CSS transition（transform: translateY + height）。
   【v1.4.13】同时按目标行类型切换竖条位置：子项（.sub）竖条在 22px
   （与缩进导轨线对齐），顶级项 / 分组标题在 x=0。用 #navHighlight.sub 承载。
   首次定位（_navHiReady 为 false）时关掉过渡直接落位，避免从 y=0 滑过来。 */
let _navHiReady = false;
function moveNavHighlight(el, animate) {
  const hi = document.getElementById("navHighlight");
  const side = document.getElementById("sidebar");
  if (!hi || !side || !el) return;
  const top = el.offsetTop;
  const h = el.offsetHeight;
  const first = !_navHiReady;
  if (first || animate === false) hi.style.transition = "none";
  hi.style.height = h + "px";
  hi.style.transform = "translateY(" + top + "px)";
  /* 竖条位置跟随行类型：子项 → 22px（导轨线处），其余 → 0 */
  hi.classList.toggle("sub", el.classList.contains("sub"));
  if (first || animate === false) {
    // 强制回流后再恢复过渡，确保「落位」这一帧不产生动画
    void hi.offsetHeight;
    hi.style.transition = "";
    _navHiReady = true;
  }
  hi.classList.add("on");
}
/* 侧栏滚动 / 窗口尺寸变化时，滑块需要重新对齐当前选中行 */
function syncNavHighlight() {
  const cur = document.querySelector(".nav-item.active");
  if (cur) moveNavHighlight(cur, false);
}

function bindNav() {
  // 分组头：点击折叠 / 展开
  document.querySelectorAll(".nav-group-head").forEach(h => {
    h.onclick = () => {
      const g = h.parentElement;
      if (g) g.classList.toggle("open");
    };
  });

  document.querySelectorAll(".nav-item").forEach(el => {
    el.onclick = () => {
      document.querySelectorAll(".nav-item").forEach(n => n.classList.remove("active"));
      document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
      el.classList.add("active");
      const view = document.getElementById("view-" + el.dataset.view);
      if (view) view.classList.add("active");
      _openGroupOf(el.dataset.view);
      /* 【v1.4.12】高亮滑块平滑移动到新选中行。
         若目标行所在分组原本是折叠的，_openGroupOf 会触发 max-height 展开动画
         （0.22s），行位置在动画期间持续下移；故此处先在展开起始帧对齐一次
         （此时行已可见、位置接近终值），动画结束后再补一次精确对齐。 */
      moveNavHighlight(el);
      setTimeout(() => moveNavHighlight(el, false), 240);
      if (el.dataset.view === "history") switchHist("monitored");
      if (el.dataset.view === "dataoverview") loadDataOverview();
      /* 【v1.2.2】进入「存储管理」时刷新路径设置 + 发起后台占用扫描 */
      if (el.dataset.view === "storage") { loadDataPaths(); refreshStorage(false); }
      if (el.dataset.view === "license") renderLicense();
      if (el.dataset.view === "logs") loadLogs();
      if (el.dataset.view === "schedule") updateWhBar();
      if (el.dataset.view === "media") loadMediaPage();
      /* 【v1.2.2】AI 部署独立成页：进入时刷新引擎状态与部署进度 */
      if (el.dataset.view === "aideploy") loadAiSettings();
    };
  });

  // 默认展开「基础功能」组，让首屏不至于只有孤零零一个「系统概览」
  const first = document.querySelector('.nav-group[data-group="basic"]');
  if (first) first.classList.add("open");

  /* 【v1.4.12】初始高亮：HTML 里 #view-overview 是默认 .active，
     但侧栏「系统概览」行没有 active 类 —— 补上并让滑块落到它上面。
     放在 rAF 里等首帧布局完成，offsetTop/Height 才准确。 */
  const ovItem = document.querySelector('.nav-item[data-view="overview"]');
  if (ovItem) {
    ovItem.classList.add("active");
    requestAnimationFrame(() => moveNavHighlight(ovItem));
  }
}

/* ---------------- 概览 ---------------- */
function renderOverview() {
  updateOverviewClock();
  const s = STATE.stats;
  // 【v1.4.18】四张统计卡改为静态结构，这里只更新数字，避免整块 innerHTML 重建
  // 把「本日转发次数」卡上的进度环（SVG 圆角描边）反复销毁、丢失动画状态。
  setText("numMonitors", STATE.monitors.length);
  setText("numAccounts", STATE.accounts.length + "/5");
  setText("numPosts", s.monitored_posts_today || 0);
  setText("numForwards", s.forwards_today || 0);
  layoutFwdRing();   // 卡片尺寸可能因布局变化而变，重排进度环几何

  const sch = STATE.scheduler;
  const tag = document.getElementById("schedState");
  tag.textContent = sch.running ? "运行中" : "未运行";
  tag.className = "tag " + (sch.running ? "green" : "blue");
  document.getElementById("schedInfo").textContent =
    `间隔 ${sch.interval_minutes} 分钟 · 当前${sch.in_working_hours ? "在工作时间内" : "不在工作时间"}` +
    (sch.last_run ? ` · 上次运行 ${sch.last_run}` : "") +
    " · 每轮仅从新微博中随机转发 1 条";
  const lic = STATE.license || {};
  const licLine = document.getElementById("licLine");
  if (licLine) {
    if (lic.licensed) {
      licLine.innerHTML = `<span class="tag green">已授权 · ${esc(lic.type_name)}</span>`;
    } else if (lic.trial) {
      const d = Math.floor((lic.remaining_seconds || 0) / 86400);
      const h = Math.floor(((lic.remaining_seconds || 0) % 86400) / 3600);
      licLine.innerHTML = `<span class="tag yellow">试用剩余 ${d} 天 ${h} 小时</span>`;
    } else {
      licLine.innerHTML = `<span class="tag red">未授权 · 转发已禁用</span>`;
    }
  }
  const box = document.getElementById("lastResult");
  if (box) box.textContent = formatLastResult(sch.last_result, sch.last_run);
}

/* 安全设置元素文本（元素不存在时静默跳过） */
function setText(id, v) {
  const el = document.getElementById(id);
  if (el) el.textContent = (v === undefined || v === null) ? "" : String(v);
}

/* ---------------- 本日转发进度环（v1.4.18，v1.4.19 修正） ---------------- */
/* 「本日转发次数」卡沿外框的高亮进度环：显示当前距离下一次排程转发的剩余进度。
   调度运行中 → 环从顶部 12 点方向沿卡片四边顺时针生长；
   到达排程转发时间（progress ≥ 1，即进度 100%）→ 高亮绕满一圈、驻留 3s、
   再 1.5s 渐隐；next_run 推进（新周期开始）→ 清除完成态重新生长。
   【v1.4.19】v1.4.18 用 rotate(-90) 想把起点转到顶部，但 rotate 作用在非正方形
   矩形上会把整个环转出卡片（宽高互换）。现改为手写 <path>：路径起点本身就是
   顶部中点、顺时针闭合，配合经典 dashoffset 技术天然从 12 点方向生长。
   【v1.4.20】进度周期的起点/终点修正：起点用上一次轮询真实完成的 last_run，
   终点用本次排程的实际转发时刻 next_forward_run（含抖动），未排程时回退 next_run。
   旧逻辑用 next_run - interval 反推起点 + 以 next_run 为终点，会把 12 分钟爬取耗时
   错算进周期、且把 100% 对齐到「下一次轮询」而非真实转发时刻，表现为「特别慢」。
   【v1.4.22】满环驻留 10s + 渐隐 1.5s 之后彻底熄灭（连 fwd-active 一起清掉 + 环清空），
   不再复燃；等新周期（next_run 推进）重新走进度时才重新点亮。 */
let _fwdPrevNextRun = null;     // 上一帧的 next_run（用于检测新周期开始）
let _fwdDoneFor = null;         // 已触发满环驻留的 next_run（每个周期只触发一次）
let _fwdPerim = 0;              // 路径周长（像素）
function layoutFwdRing() {
  const card = document.getElementById("statForwards");
  const svg = document.getElementById("fwdRing");
  const track = document.getElementById("fwdRingTrack");
  const prog = document.getElementById("fwdRingProg");
  if (!card || !svg || !track || !prog) return;
  const W = card.clientWidth, H = card.clientHeight;
  if (!W || !H) return;
  const sw = 3;      // 描边线宽
  const r = 10;      // 圆角半径（与卡片圆角观感一致）
  const x = sw / 2, y = sw / 2, w = W - sw, h = H - sw;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  // 顺时针路径，起点 = 顶部中点 (x + w/2, y)：顶边 → 右上角 → 右边 → 右下角
  // → 底边 → 左下角 → 左边 → 左上角 → 顶边回到起点（Z 闭合）
  const d = `M ${x + w / 2} ${y} `
    + `H ${x + w - r} A ${r} ${r} 0 0 1 ${x + w} ${y + r} `
    + `V ${y + h - r} A ${r} ${r} 0 0 1 ${x + w - r} ${y + h} `
    + `H ${x + r} A ${r} ${r} 0 0 1 ${x} ${y + h - r} `
    + `V ${y + r} A ${r} ${r} 0 0 1 ${x + r} ${y} Z`;
  [track, prog].forEach(el => el.setAttribute("d", d));
  // 圆角矩形周长 = 2(w+h) - 8r + 2πr
  _fwdPerim = 2 * (w + h) - 8 * r + 2 * Math.PI * r;
  prog.style.strokeDasharray = String(_fwdPerim);
}

function triggerFwdDone() {
  const card = document.getElementById("statForwards");
  const prog = document.getElementById("fwdRingProg");
  if (!card) return;
  card.classList.add("fwd-active", "fwd-done");
  card.classList.remove("fwd-fading");
  if (prog) prog.style.strokeDashoffset = "0";   // 绕满一圈
  clearTimeout(window._fwdFadeT1);
  clearTimeout(window._fwdFadeT2);
  // 【v1.4.20】满环高亮驻留 10s（原 3s）。若驻留期间 next_run 推进（新周期开始），
  // tickForwardRing 的「新周期检测」会立即清掉高亮并重新生长；否则 10s 满环后再 1.5s 渐隐。
  window._fwdFadeT1 = setTimeout(() => card.classList.add("fwd-fading"), 10000);   // 高亮驻留 10s
  window._fwdFadeT2 = setTimeout(() => {
    // 【v1.4.22】渐隐结束后必须「彻底熄灭」：fwd-active 仍会让环以满环 offset（0）
    // 重新亮起（fwd-fading 的 opacity:0 一旦移除就复燃）。现连 fwd-active 一起清掉，
    // 并把环清空回空环状态，直到新周期（next_run 推进）重新走进度时再点亮。
    // 守卫：若此刻已进入新周期（fwd-done/fwd-fading 已被新周期检测清掉），直接跳过，
    // 避免误杀新周期正在生长的进度环。
    if (!card.classList.contains("fwd-done") && !card.classList.contains("fwd-fading")) return;
    card.classList.remove("fwd-done", "fwd-fading", "fwd-active");
    if (prog) prog.style.strokeDashoffset = String(_fwdPerim);
  }, 10000 + 1500);
}

function tickForwardRing() {
  const card = document.getElementById("statForwards");
  const prog = document.getElementById("fwdRingProg");
  const sch = (STATE && STATE.scheduler) || {};
  if (!card || !prog) { requestAnimationFrame(tickForwardRing); return; }
  const perim = _fwdPerim || 0;
  if (!sch.running || !sch.next_run) {
    // 调度未运行：隐藏进度环
    card.classList.remove("fwd-active", "fwd-done", "fwd-fading");
    prog.style.strokeDashoffset = String(perim);
    _fwdPrevNextRun = null;
    _fwdDoneFor = null;
    requestAnimationFrame(tickForwardRing);
    return;
  }
  // 【v1.4.20】进度周期修正：
  //   起点 = 上一次轮询真实完成时刻 last_run（"%Y-%m-%d %H:%M:%S" 字符串）；
  //   终点 = 本次排程的实际转发时刻 next_forward_run（浮点秒，含抖动），未排程时回退 next_run。
  //   旧逻辑用 next_run - interval 反推起点，会把 12 分钟爬取耗时错算进周期开头，
  //   且 100% 对齐到「下一次轮询」而非真实转发时刻，表现为「特别慢」「到点不到 100%」。
  const intervalMs = (sch.interval_minutes || 30) * 60 * 1000;
  const endMs = (sch.next_forward_run || sch.next_run) * 1000;
  let startMs = null;
  if (sch.last_run) {
    const t = Date.parse(String(sch.last_run).replace(/-/g, "/"));  // "2026-10-13 22:07:27" → 可解析时间戳
    if (!isNaN(t)) startMs = t;
  }
  if (startMs === null || startMs >= endMs) {
    startMs = endMs - intervalMs;          // 兜底：无 last_run 或数据异常时退化为整间隔
  }
  let progress = (Date.now() - startMs) / (endMs - startMs);
  if (progress < 0) progress = 0;
  if (progress > 1) progress = 1;
  // 新周期开始（next_run 推进）→ 清除上一周期的完成态，进度环重新生长
  if (_fwdPrevNextRun !== null && sch.next_run !== _fwdPrevNextRun) {
    card.classList.remove("fwd-done", "fwd-fading");
    _fwdDoneFor = null;
  }
  _fwdPrevNextRun = sch.next_run;
  // 【v1.4.19】到达排程转发时间（进度 100%）即触发「满环 + 驻留 3s + 渐隐」，
  // 不再等 next_run 推进；每个周期（同一 next_run）只触发一次。
  if (progress >= 1 && _fwdDoneFor !== sch.next_run) {
    _fwdDoneFor = sch.next_run;
    triggerFwdDone();
  }
  // 满环驻留 / 渐隐期间保持满环不重绘；渐隐结束后本周期已消费，不再重新点亮
  if (_fwdDoneFor !== sch.next_run) {
    card.classList.add("fwd-active");
    prog.style.strokeDashoffset = String(perim * (1 - progress));
  }
  requestAnimationFrame(tickForwardRing);
}

/* 将最近一次轮询结果格式化成运行日志样式，方便阅读 */
function formatLastResult(res, ts) {
  if (!res) return "尚未运行";
  const t = ts || _fmtLogTs(new Date());
  const lines = [];
  if (!res.ok) {
    lines.push(`[${t}][ERROR] 轮询失败：${res.error || "未知错误"}`);
    return lines.join("\n");
  }
  const results = (res.results || []).slice();
  const total = results.length;
  const hasNew = results.reduce((s, r) => s + (r.new_count || 0), 0);
  lines.push(`[${t}][INFO] 本轮轮询结束 · 监控 ${total} 个对象 · 共 ${hasNew} 条新微博 · 转发 ${res.forwarded || 0} 条`);
  if (res.message) lines.push(`[${t}][INFO] ${res.message}`);
  const order = { forwarded: 0, failed: 1, error: 2, skipped: 3, none: 4 };
  results.sort((a, b) => (order[a.action] ?? 5) - (order[b.action] ?? 5));
  for (const r of results) {
    const lv = r.action === "forwarded" ? "INFO" : r.action === "failed" || r.action === "error" ? "ERROR" : "INFO";
    const name = r.screen_name || r.uid;
    lines.push(`[${t}][${lv}] @${name}(${r.uid}) · 新微博 ${r.new_count || 0} · ${r.detail || "-"}`);
  }
  return lines.join("\n");
}

function _fmtLogTs(d) {
  const pad = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/* 概览界面每秒自动刷新：仅当概览页处于激活态时拉取最新状态并重渲染，
   避免手动点击刷新；不影响其它页面（不会重置输入/滚动）。 */
async function refreshOverviewLive() {
  if (!API) return;
  if (document.hidden) return;                 // 最小化到托盘 / 后台时不轮询
  if (window._ovRefreshing) return;            // 避免上一次未返回时叠加调用
  const ov = document.getElementById("view-overview");
  if (!ov || !ov.classList.contains("active")) return;
  window._ovRefreshing = true;
  try {
    const st = await apiCall("get_state");
    // 【v1.1.9k 0010】后端冷启动会先返回空壳（loading:true），此时真实数据
    // 还在后台构建中。若拿它覆盖 STATE，界面会闪回空白（账户/列表清零）。
    // 直接跳过，等下一轮轮询拿到真实数据再刷新。
    if (st && !st.loading) { STATE = st; renderOverview(); }
  }   catch (e) { /* 忽略瞬时错误，下一轮重试 */ }
  finally { window._ovRefreshing = false; }
}

/* 【v1.4.18】账户管理页秒级刷新：仅当账户管理页激活、窗口可见、且无账户操作进行中时，
   每秒拉取一次 get_state 并重绘账户表格，让状态/检测时间/有效期等近实时更新。 */
async function refreshAccountsLive() {
  if (!API) return;
  if (document.hidden) return;
  if (window._accBusy) return;                    // 正在检测/续期/增删账户，避让避免打断
  const acc = document.getElementById("view-accounts");
  if (!acc || !acc.classList.contains("active")) return;
  if (window._accRefreshing) return;
  window._accRefreshing = true;
  try {
    const st = await apiCall("get_state");
    if (st && !st.loading) { STATE = st; renderAccounts(); }
  } catch (e) { /* 忽略瞬时错误，下一轮重试 */ }
  finally { window._accRefreshing = false; }
}

/* 【v1.4.1】系统负载轮询：仅在「系统概览」激活且窗口可见时拉取，读取后端快照（后台线程采样，零阻塞）。
   布局已并入统计卡：单行纯数值（CPU/内存/GPU 百分比 + 网络速率），无进度条。 */
async function refreshSystemLoad() {
  if (!API) return;
  if (document.hidden) return;
  var ov = document.getElementById("view-overview");
  if (!ov || !ov.classList.contains("active")) return;
  if (window._loadRefreshing) return;
  window._loadRefreshing = true;
  try {
    var L = await apiCall("get_system_load");
    if (!L) return;
    var set = function (id, txt) {
      var el = document.getElementById(id);
      if (el) el.textContent = txt;
    };
    /* 【v1.4.6】温度：取不到（无 GPU / 无 ACPI 温度区）显示「—」。
       【v1.4.9】常态统一用淡黄色（样式表 .ll-temp 定义），仅在 ≥90 时转红提示；
       不再改内联 color，避免与定宽样式相互覆盖，也让整行宽度稳定不抖动。 */
    var setTemp = function (id, t) {
      var el = document.getElementById(id);
      if (!el) return;
      if (t === null || t === undefined) {
        el.textContent = "—";
        el.style.color = "";
        return;
      }
      el.textContent = t + "°C";
      el.style.color = (t >= 90) ? "var(--red)" : "";
    };
    set("loadCpuVal", L.cpu + "%");
    setTemp("loadCpuTempVal", L.cpu_temp);
    set("loadMemVal", L.mem + "%");
    set("loadNetVal", "↑ " + fmtRate(L.net_up) + " / ↓ " + fmtRate(L.net_down));
    set("loadGpuVal", (L.gpu === null || L.gpu === undefined) ? "—" : L.gpu + "%");
    setTemp("loadGpuTempVal", L.gpu_temp);
  } catch (e) { /* 忽略瞬时错误 */ }
  finally { window._loadRefreshing = false; }
}
function fmtRate(bps) {
  var b = bps || 0;
  if (b >= 1024 * 1024) return (b / 1024 / 1024).toFixed(1) + " MB/s";
  if (b >= 1024) return (b / 1024).toFixed(1) + " KB/s";
  return Math.round(b) + " B/s";
}

/* ---------------- 账户健康（v1.4.0） ---------------- */
window._healthAlarmSig = "";

async function checkAccountHealth() {
  if (!API) return;
  try {
    var r = await apiCall("get_account_health");
    if (!r) return;
    renderHealthIndicator(r);
    if (r.level === "crit") {
      triggerHealthAlarm(r);
    } else {
      clearHealthAlarm();
    }
  } catch (e) { /* 忽略瞬时错误 */ }
}

function renderHealthIndicator(r) {
  var box = document.getElementById("healthIndicator");
  var txt = document.getElementById("healthText");
  if (!box) return;
  var level = (r && r.level) || "empty";
  box.classList.remove("ok", "crit", "empty", "warn");
  box.classList.add(level === "crit" ? "crit" : (level === "empty" ? "empty" : "ok"));
  if (txt) txt.textContent = (r && r.message) ? r.message : "账户健康检测中…";
}

function triggerHealthAlarm(r) {
  document.body.classList.add("alarm");
  // 仅在「异常账户集合」变化时弹窗，避免每分钟重复打扰
  var sig = (r.unhealthy || []).map(function (u) {
    return u.name + ":" + (u.reasons || []).join("|");
  }).join(";;");
  if (sig !== window._healthAlarmSig) {
    window._healthAlarmSig = sig;
    showHealthAlarm(r);
  }
}

function clearHealthAlarm() {
  document.body.classList.remove("alarm");
  window._healthAlarmSig = "";
  var m = document.getElementById("healthAlarmModal");
  if (m) m.classList.remove("show");
}

function showHealthAlarm(r) {
  var m = document.getElementById("healthAlarmModal");
  var body = document.getElementById("healthAlarmBody");
  if (!m || !body) return;
  var html = '<p class="ha-tip">以下已记录账户检测到异常，请尽快处理（重新登录 / 检查网络 / 降低频率）：</p><ul class="ha-list">';
  (r.unhealthy || []).forEach(function (u) {
    html += '<li><b>' + esc(u.name) + '</b>：' + esc((u.reasons || []).join("、")) + '</li>';
  });
  html += '</ul>';
  body.innerHTML = html;
  m.classList.add("show");
}

function closeHealthAlarm() {
  var m = document.getElementById("healthAlarmModal");
  if (m) m.classList.remove("show");
}

/* ---------------- 账户 ---------------- */
const ACC_STATUS_META = {
  ok: { cls: "green", text: "正常" },
  cookie_expired: { cls: "red", text: "Cookie失效" },
  risk_control: { cls: "amber", text: "被风控" },
  risk: { cls: "amber", text: "被风控" },
  network_error: { cls: "blue", text: "网络异常" },
  no_repost_perm: { cls: "yellow", text: "无转发权限" },
  unknown: { cls: "blue", text: "未检测" },
};

function accountStatusTag(a) {
  const s = a.status || "unknown";
  const meta = ACC_STATUS_META[s] || ACC_STATUS_META.unknown;
  const detail = a.check_detail ? ` title="${esc(a.check_detail)}"` : "";
  return `<span class="tag ${meta.cls}"${detail}>${meta.text}</span>`;
}

function cookieExpiryCell(a) {
  const e = a.cookie_expiry;
  if (!e || !e.has_expiry) {
    return `<td><div class="cookie-exp none"><div class="ce-date">—</div><div class="ce-remain">无法预测</div></div></td>`;
  }
  if (e.expired) {
    return `<td><div class="cookie-exp expired"><div class="ce-date">${esc(e.expiry_str)}</div><div class="ce-remain">已过期</div></div></td>`;
  }
  const lvl = e.level === "warn" ? "warn" : "safe";
  return `<td><div class="cookie-exp ${lvl}" title="失效时间：${esc(e.expiry_str)}（来源 ${esc(e.source||"")}）">
      <div class="ce-date">${esc(e.expiry_str)}</div>
      <div class="ce-remain">${esc(e.remaining_text || "")}</div>
    </div></td>`;
}

function renderAccounts() {
  const list = STATE.accounts;
  document.getElementById("accCount").textContent = list.length;
  const tb = document.getElementById("accList");
  if (!list.length) { tb.innerHTML = `<tr><td colspan="7" class="empty">暂无账户，请先登录并添加</td></tr>`; return; }
  tb.innerHTML = list.map(a => `
    <tr>
      <td>${esc(a.display_name || a.remark || "未命名账户")}</td>
      <td class="muted">${esc(a.created_at)}</td>
      <td class="muted">${esc(a.last_used || "—")}</td>
      <td>${accountStatusTag(a)}</td>
      ${cookieExpiryCell(a)}
      <td class="muted">${esc(a.last_check || "—")}</td>
      <td><button class="small" onclick="checkAccount('${a.id}')">检测</button> <button class="small ghost" onclick="renewAccount('${a.id}')">续期</button> <button class="small danger" onclick="removeAccount('${a.id}')">删除</button></td>
    </tr>`).join("");
}

async function checkAccount(id) {
  window._accBusy = true;
  try {
    toast("正在检测该账户（登录态 + 真实抓取 + 转发能力）…");
    const r = await apiCall("check_account", id);
    if (!r.ok) { toast(r.error, "err"); return; }
    const label = {
      ok: "正常，抓取与转发均可用",
      cookie_expired: "Cookie 已失效，请重新登录",
      risk_control: "被微博风控拦截，请降低频率",
      network_error: "网络异常",
      no_repost_perm: "无转发权限",
      unknown: "登录态未确认（以实际抓取为准）"
    };
    toast(`检测：${label[r.status] || r.status}（${r.message}）`,
          r.status === "ok" ? "ok" : "err");
    await refreshStateFresh();
  } finally { window._accBusy = false; }
}
async function checkAllAccounts() {
  window._accBusy = true;
  try {
    toast("正在检测全部账户…");
    const r = await apiCall("check_all_accounts");
    if (r.ok) { toast("全部账户检测完成", "ok"); await refreshStateFresh(); }
    else toast(r.error || "检测失败", "err");
  } finally { window._accBusy = false; }
}

async function addAccount() {
  window._accBusy = true;
  try {
    const remark = document.getElementById("accName").value.trim();
    const cookie = document.getElementById("accCookie").value.trim();
    if (!cookie) { toast("Cookie 不能为空", "err"); return; }
    const r = await apiCall("add_account", remark, cookie);
    if (r.ok) { toast("账户已添加", "ok"); document.getElementById("accName").value = ""; document.getElementById("accCookie").value = ""; await refreshStateFresh(); }
    else toast(r.message, "err");
  } finally { window._accBusy = false; }
}

async function removeAccount(id) {
  window._accBusy = true;
  try {
    await apiCall("remove_account", id);
    toast("已删除账户"); await refreshStateFresh();
  } finally { window._accBusy = false; }
}

async function renewAccount(id) {
  window._accBusy = true;
  try {
    toast("正在续期该账户 Cookie（会话预热）…");
    const r = await apiCall("renew_account", id);
    if (r.ok) toast(r.message || "续期成功", "ok");
    else toast(r.message || r.error || "续期失败", "err");
    await refreshStateFresh();
  } finally { window._accBusy = false; }
}

/* 自动续期开关：从 STATE.config 同步 UI，并保存用户切换 */
async function syncAutoRenew() {
  const el = document.getElementById("autoRenewSwitch");
  if (!el) return;
  el.checked = !!(STATE && STATE.config && STATE.config.auto_renew_cookie);
}
async function saveAutoRenew() {
  const el = document.getElementById("autoRenewSwitch");
  const v = !!(el && el.checked);
  await apiCall("save_config", JSON.stringify({ auto_renew_cookie: v }));
  toast(v ? "已开启自动续期 Cookie" : "已关闭自动续期 Cookie", "ok");
}

async function openBuiltinLogin() {
  const r = await apiCall("open_login_browser");
  if (!r.ok) toast(r.error, "err"); else toast("已打开内置浏览器，请登录");
}
async function openSystemLogin() {
  const r = await apiCall("open_system_browser");
  toast(r.message || "已打开系统浏览器");
}
async function captureCookie() {
  const r = await apiCall("capture_login_cookies");
  if (!r.ok) { toast(r.error, "err"); return; }
  document.getElementById("accCookie").value = r.cookie;
  toast(r.logged_in ? "已获取登录态 Cookie" : "已获取 Cookie（未检测到登录态，请确认已登录）", r.logged_in ? "ok" : "err");
}
async function closeLogin() { await apiCall("close_login_browser"); toast("已关闭登录窗口"); }

/* ---------------- 监控 ---------------- */
function _monitorAvatar(m) {
  const ch = esc((m.screen_name || m.uid || "?").charAt(0).toUpperCase());
  const local = m.avatar_local || "";
  if (local) {
    return `<img class="mon-avatar" data-uid="${esc(m.uid)}" src="" alt="${ch}" title="${ch}" onerror="this.onerror=null;this.classList.add('mon-avatar-fallback');this.src='data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';">`;
  }
  return `<div class="mon-avatar">${ch}</div>`;
}
function _monitorStatusTag(status) {
  if (status === "online") return `<span class="mon-status online"><i></i>在线</span>`;
  if (status === "error") return `<span class="mon-status error"><i></i>异常</span>`;
  return `<span class="mon-status unknown"><i></i>未知</span>`;
}
/* 监控列表 - 筛选 / 分页 / 置顶 */
let MON_FILTERS = { keyword: "", status: "all", pinnedOnly: false };
let MON_PAGE = 1;
let MON_PAGE_SIZE = 20;
let MON_FILTERED = [];
let MON_PAGE_ROWS = [];
const MON_PAGE_PRESETS = [10, 20, 50, 100];

function renderMonitors() {
  const all = (STATE.monitors || []).slice();
  // 置顶对象排在前面（稳定排序，其余保持原有相对顺序）
  all.sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0));
  const kw = (MON_FILTERS.keyword || "").trim().toLowerCase();
  const rows = all.filter(m => {
    if (MON_FILTERS.status !== "all" && (m.status || "unknown") !== MON_FILTERS.status) return false;
    if (MON_FILTERS.pinnedOnly && !m.pinned) return false;
    if (kw) {
      const hay = ((m.screen_name || "") + " " + (m.uid || "")).toLowerCase();
      if (!hay.includes(kw)) return false;
    }
    return true;
  });
  MON_FILTERED = rows;
  const totalPages = Math.max(1, Math.ceil(MON_FILTERED.length / MON_PAGE_SIZE));
  if (MON_PAGE > totalPages) MON_PAGE = totalPages;
  renderMonPage();
}

/* v1.1.1：账户健康横幅 —— 抓取失败的根因（Cookie 失效 / 风控）直接暴露在监控页顶部，
   避免用户只看到一排「异常」却不知道要重新登录。 */
function accountHealthBanner() {
  const accs = STATE.accounts || [];
  if (!accs.length) {
    return `<div class="mon-alert ghost">尚未添加微博账户，无法抓取任何监控对象。请先到「账户管理」登录并添加账户。</div>`;
  }
  const name = a => esc(a.display_name || a.remark || a.id);
  const expired = accs.filter(a => a.status === "cookie_expired");
  if (expired.length) {
    return `<div class="mon-alert red">账户「${name(expired[0])}」Cookie 已失效 —— 所有监控对象都会抓取失败。
      请到「账户管理」删除该账户后重新登录（重新登录会写入新的登录凭证 SUB）。</div>`;
  }
  const risk = accs.filter(a => a.status === "risk_control" || a.status === "risk");
  if (risk.length) {
    return `<div class="mon-alert amber">账户「${name(risk[0])}」被微博风控拦截（HTTP 432/418/403）。
      建议调大轮询间隔、减少并发，稍后自动恢复。</div>`;
  }
  const net = accs.filter(a => a.status === "network_error");
  if (net.length) {
    return `<div class="mon-alert ghost">账户「${name(net[0])}」检测时网络异常，请检查本机网络后重试。</div>`;
  }
  const unknown = accs.filter(a => a.status === "unknown" || !a.status);
  if (unknown.length === accs.length) {
    return `<div class="mon-alert ghost">账户尚未完成检测（或登录态无法确认），可点击「账户管理 → 检测」确认。</div>`;
  }
  return "";
}

function renderMonPage() {
  const total = MON_FILTERED.length;
  const cEl = document.getElementById("monCount");
  if (cEl) cEl.textContent = total;
  const box = document.getElementById("monList");
  const banner = accountHealthBanner();
  if (!total) {
    const isFilter = MON_FILTERS.keyword || MON_FILTERS.status !== "all" || MON_FILTERS.pinnedOnly;
    box.innerHTML = banner + `<div class="empty">${isFilter ? "没有符合筛选条件的监控对象" : "暂无监控对象，请在上方添加微博 UID"}</div>`;
    renderMonPagination();
    return;
  }
  const start = (MON_PAGE - 1) * MON_PAGE_SIZE;
  const pageRows = MON_FILTERED.slice(start, start + MON_PAGE_SIZE);
  MON_PAGE_ROWS = pageRows;
  box.innerHTML = banner + pageRows.map(renderMonCard).join("");
  loadMonitorAvatars(pageRows);
  renderMonPagination();
}

function renderMonCard(m) {
  const name = m.screen_name || "未获取昵称";
  const preview = (m.last_post_text || "").slice(0, 90);
  const pinned = !!m.pinned;
  return `<div class="mon-item ${pinned ? "mon-pinned" : ""}" data-uid="${esc(m.uid)}">
    ${_monitorAvatar(m)}
    <div class="mon-body">
      <div class="mon-head">
        ${pinned ? '<span class="mon-pin-icon" title="已置顶">📌</span>' : ""}
        <span class="mon-name">${esc(name)}</span>
        <span class="mon-uid muted">@${esc(m.uid)}</span>
      </div>
      <div class="mon-preview muted" title="${esc(m.last_post_text || "")}">${preview ? esc(preview) : "暂无最新微博"}</div>
      <div class="mon-meta muted">上次检测 ${esc(m.last_check || "—")}</div>
    </div>
    <div class="mon-actions">
      ${_monitorStatusTag(m.status)}
      <button class="small ghost" onclick="togglePin('${esc(m.uid)}')">${pinned ? "取消置顶" : "置顶"}</button>
      <button class="small danger" onclick="removeMonitor('${esc(m.uid)}')">移除</button>
    </div>
  </div>`;
}

function applyMonFilter() {
  const kw = document.getElementById("monKeyword")?.value || "";
  const st = document.getElementById("monStatus")?.value || "all";
  const pin = document.getElementById("monPinnedOnly")?.checked || false;
  MON_FILTERS = { keyword: kw, status: st, pinnedOnly: pin };
  MON_PAGE = 1;
  renderMonitors(); // 重新计算筛选/分页
}

function clearMonFilters() {
  const kw = document.getElementById("monKeyword");
  const st = document.getElementById("monStatus");
  const pin = document.getElementById("monPinnedOnly");
  if (kw) kw.value = "";
  if (st) st.value = "all";
  if (pin) pin.checked = false;
  MON_FILTERS = { keyword: "", status: "all", pinnedOnly: false };
  MON_PAGE = 1;
  renderMonitors(); // 清空筛选后重新渲染全部列表
}

async function togglePin(uid) {
  const m = (STATE.monitors || []).find(x => x.uid === uid);
  const next = !(m && m.pinned);
  const r = await apiCall("set_monitor_pinned", uid, next);
  if (r && r.ok) { await refreshStateFresh(); }
  else toast("置顶操作失败", "err");
}

function renderMonPagination() {
  const box = document.getElementById("monPagination");
  if (!box) return;
  const total = MON_FILTERED.length;
  if (total <= 0) { box.innerHTML = ""; return; }
  const totalPages = Math.max(1, Math.ceil(total / MON_PAGE_SIZE));
  const start = (MON_PAGE - 1) * MON_PAGE_SIZE + 1;
  const end = Math.min(MON_PAGE * MON_PAGE_SIZE, total);
  const isCustom = !MON_PAGE_PRESETS.includes(MON_PAGE_SIZE);
  const pageSizeBtns = MON_PAGE_PRESETS.map(n =>
    `<button class="small page-size-btn ${MON_PAGE_SIZE === n ? "active" : "ghost"}" onclick="changeMonPageSize(${n})">${n}</button>`
  ).join("");
  box.innerHTML = `
    <div class="page-size-group" title="每页显示条数">
      ${pageSizeBtns}
      <button class="small page-size-btn ${isCustom ? "active" : "ghost"}" onclick="showCustomMonPageSize()">自定义</button>
    </div>
    <span id="monCustomPageWrap" style="display:${isCustom ? "inline-flex" : "none"};">
      <input id="monPageSizeNum" type="number" min="1" placeholder="条" value="${isCustom ? MON_PAGE_SIZE : ""}">
      <button class="small ghost" onclick="applyCustomMonPageSize()">应用</button>
    </span>
    <div class="page-nav-group">
      <button class="small ghost" onclick="changeMonPage(-1)" ${MON_PAGE <= 1 ? "disabled" : ""}>上一页</button>
      <span class="muted page-info">${MON_PAGE} / ${totalPages}（${start}-${end} / ${total}）</span>
      <button class="small ghost" onclick="changeMonPage(1)" ${MON_PAGE >= totalPages ? "disabled" : ""}>下一页</button>
    </div>`;
}

function changeMonPageSize(size) {
  MON_PAGE_SIZE = parseInt(size, 10);
  MON_PAGE = 1;
  renderMonPage();
}
function showCustomMonPageSize() {
  const wrap = document.getElementById("monCustomPageWrap");
  if (wrap) {
    wrap.style.display = "inline-flex";
    const inp = document.getElementById("monPageSizeNum");
    if (inp) { inp.value = MON_PAGE_SIZE; inp.focus(); }
  }
}
function applyCustomMonPageSize() {
  const n = parseInt(document.getElementById("monPageSizeNum").value, 10);
  if (!n || n < 1) { toast("请输入有效的每页条数", "err"); return; }
  if (n > 2000) { toast("每页最多 2000 条", "err"); return; }
  changeMonPageSize(n);
}
function changeMonPage(delta) {
  const totalPages = Math.max(1, Math.ceil(MON_FILTERED.length / MON_PAGE_SIZE));
  MON_PAGE = Math.max(1, Math.min(totalPages, MON_PAGE + delta));
  renderMonPage();
}

async function loadMonitorAvatars(list) {
  for (const m of list) {
    const local = m.avatar_local;
    if (!local) continue;
    const img = document.querySelector(`.mon-avatar[data-uid="${esc(m.uid)}"]`);
    if (!img) continue;
    try {
      const r = await apiCall("load_image_base64", local);
      if (r.ok) img.src = r.data_url;
      else fallbackMonitorAvatar(img, m);
    } catch (e) { fallbackMonitorAvatar(img, m); }
  }
}
function fallbackMonitorAvatar(img, m) {
  const ch = esc((m.screen_name || m.uid || "?").charAt(0).toUpperCase());
  const div = document.createElement("div");
  div.className = "mon-avatar";
  div.textContent = ch;
  if (img.parentNode) img.parentNode.replaceChild(div, img);
}

async function addMonitor() {
  const uid = document.getElementById("monUid").value.trim();
  if (!uid) { toast("UID 不能为空", "err"); return; }
  const r = await apiCall("add_monitor", uid);
  if (r.ok) { toast("已添加：" + (r.screen_name || uid), "ok"); document.getElementById("monUid").value = ""; await refreshStateFresh(); }
  else toast(r.message, "err");
}
async function removeMonitor(uid) { await apiCall("remove_monitor", uid); toast("已移除"); await refreshStateFresh(); }
async function refreshMonitors() {
  toast("正在刷新并判定监控对象…");
  const r = await apiCall("refresh_monitors");
  if (r.ok) {
    const lines = (r.details || []).map(d =>
      `${d.uid}：${d.ok ? "在线" : "异常(" + esc(d.detail) + ")"}`);
    const msg = r.summary || `刷新完成：在线 ${r.updated} / 异常 ${r.error_count}`;
    toast(msg, r.error_count ? "err" : "ok");
    if (lines.length) console.log("监控判定：\n" + lines.join("\n"));
    await refreshStateFresh();
  } else toast(r.error, "err");
}

/* 监控列表定时自动刷新昵称/状态 */
function renderMonAutoRefresh() {
  const cfg = (STATE.config.monitor_auto_refresh) || { enabled: false, minutes: 10 };
  const cb = document.getElementById("monAutoRefresh");
  const mi = document.getElementById("monRefreshMin");
  if (cb) cb.checked = !!cfg.enabled;
  if (mi) mi.value = cfg.minutes || 10;
  const st = document.getElementById("monAutoState");
  if (st) st.textContent = cfg.enabled ? `已开启（每 ${cfg.minutes} 分钟）` : "未开启";
}

async function saveMonAutoRefresh() {
  const enabled = document.getElementById("monAutoRefresh").checked;
  const minutes = parseInt(document.getElementById("monRefreshMin").value, 10) || 10;
  await apiCall("save_config", JSON.stringify({ monitor_auto_refresh: { enabled, minutes } }));
  toast("自动刷新设置已保存", "ok");
  await refreshStateFresh();
  MON_LAST_REFRESH = Date.now();
}

function monAutoTick() {
  const activeView = document.querySelector(".nav-item.active");
  const onMonitors = activeView && activeView.dataset.view === "monitors";
  if (!onMonitors) return;
  const cfg = (STATE && STATE.config.monitor_auto_refresh) || { enabled: false, minutes: 10 };
  if (!cfg.enabled) return;
  const interval = (cfg.minutes || 10) * 60000;
  if (Date.now() - MON_LAST_REFRESH >= interval) {
    MON_LAST_REFRESH = Date.now();
    apiCall("refresh_monitors").then(r => {
      if (r.ok) refreshStateFresh();
    }).catch(() => {});
  }
}

/* ---------------- 转发设置 ---------------- */
function renderForward() {
  const c = STATE.config;
  document.getElementById("intervalMin").value = c.interval_minutes;
  document.getElementById("aiSwitch").checked = !!c.ai_enabled;
  const normal = document.getElementById("normalSwitch");
  if (normal) normal.checked = !c.ai_enabled;   // 两种转发方式互斥
  loadAiSettings();          // 【v1.2.0】三引擎：主/备 + 参数 + 状态
  refreshAiDeploy();
}
async function saveInterval() {
  const v = parseInt(document.getElementById("intervalMin").value, 10);
  if (!v || v < 1) { toast("间隔需大于0", "err"); return; }
  await apiCall("save_config", JSON.stringify({ interval_minutes: v }));
  toast("间隔已保存", "ok"); await refreshStateFresh();
}
/* 转发方式互斥切换：开启其一自动关闭另一种，且至少保留一种 */
async function onForwardModeChange(which) {
  const ai = document.getElementById("aiSwitch");
  const normal = document.getElementById("normalSwitch");
  if (which === "ai") {
    normal.checked = !ai.checked;
  } else {
    ai.checked = !normal.checked;
  }
  if (!ai.checked && !normal.checked) {
    // 用户把当前项关掉时，自动切到另一种，保证至少一种生效
    if (which === "ai") { normal.checked = true; }
    else { ai.checked = true; }
  }
  await apiCall("save_config", JSON.stringify({ ai_enabled: ai.checked }));
  toast("转发设置已保存", "ok"); await refreshStateFresh();
}
async function saveForwardMode() {
  const ai = document.getElementById("aiSwitch").checked;
  const normal = document.getElementById("normalSwitch").checked;
  if (!ai && !normal) { toast("请至少选择一种转发方式", "err"); return; }
  await apiCall("save_config", JSON.stringify({ ai_enabled: ai }));
  toast("转发设置已保存", "ok"); await refreshStateFresh();
}

async function forwardRandomNow() {
  // 回退至 v2609080004 行为：直接随机挑选并转发（不同步轮询），由后端同步返回结果
  toast("正在随机挑选并转发…");
  const r = await apiCall("forward_random_now");
  if (r.ok) {
    if (r.skipped) toast("已跳过：" + r.detail, "err");
    else toast(`已转发：${r.screen_name || r.uid} - ${r.detail}`, "ok");
  } else {
    toast(r.error || "转发失败", "err");
  }
  await refreshStateFresh();
}

/* ================= 【v1.2.0】三类 AI 引擎（主/备 + 参数 + 探测） ================= */
let _AI_CFG = null;        // 最近一次 get_ai_settings 的返回
let _AI_PROBING = false;

/* 主/备下拉的选项文案（后端 labels 为准，这里兜底） */
const AI_ENGINE_FALLBACK_LABELS = {
  builtin: "内置离线AI", local: "本地离线AI（Ollama）",
  local_gguf: "本地离线AI（llama.cpp）", cloud: "云端在线AI"
};

function _aiMsg(text, kind) {
  const el = document.getElementById("aiEngineMsg");
  if (!el) return;
  el.textContent = text || "";
  // .path-msg 默认 display:none，必须按有无内容切 .show
  el.className = "path-msg" + (text ? " show" : "") + (kind ? " " + kind : "");
}

/** 按主/备路线选中情况显示对应的参数区（local / local_gguf / cloud） */
function syncAiEngineBoxes() {
  const sel = ["aiPrimaryEngine", "aiFallbackEngine"]
    .map(id => (document.getElementById(id) || {}).value)
    .filter(Boolean);
  const need = {
    aiLocalBox: sel.includes("local"),
    aiGgufBox: sel.includes("local_gguf"),
    aiCloudBox: sel.includes("cloud")
  };
  for (const id in need) {
    const el = document.getElementById(id);
    if (el) el.style.display = need[id] ? "" : "none";
  }
}

function _aiFillInputs(c) {
  c = c || {};
  const loc = c.local || {}, cloud = c.cloud || {}, gg = c.local_gguf || {};
  const set = (id, v) => { const e = document.getElementById(id); if (e) e.value = (v == null ? "" : v); };
  const chk = (id, v) => { const e = document.getElementById(id); if (e) e.checked = !!v; };
  set("aiLocalHost", loc.host);
  set("aiLocalModel", loc.model);
  chk("aiLocalAutoStart", loc.auto_start);
  chk("aiGgufAutoDownload", gg.auto_download);
  set("aiCloudBase", cloud.base_url);
  set("aiCloudModel", cloud.model);
  // 密钥**永不**回填明文：已配置时只提示，留空 = 保持不变
  set("aiCloudKey", "");
  const ke = document.getElementById("aiCloudKey");
  if (ke) ke.placeholder = cloud.api_key_set
    ? ("已配置（" + (cloud.api_key || "***") + "），留空 = 保持不变")
    : "尚未配置，请粘贴 API Key";
}

/** 渲染某条路线的状态行 */
function _aiRenderStatus(which, st) {
  const el = document.getElementById(which === "primary" ? "aiPrimaryStatus" : "aiFallbackStatus");
  if (!el) return;
  if (!st) { el.textContent = "—"; el.className = "path-usage"; return; }
  const label = st.label || AI_ENGINE_FALLBACK_LABELS[st.engine] || st.engine;
  el.textContent = label + "：" + (st.detail || "");
  el.className = "path-usage" + (st.available ? "" : " warn");
  el.title = st.detail || "";
}

/** 拉取 AI 设置 + 状态并渲染（进入转发设置页 / 保存后调用） */
async function loadAiSettings() {
  let r = null;
  try { r = await apiCall("get_ai_settings"); } catch (e) { /* 桥未就绪 */ }
  if (!r || !r.ok) {
    // 退化：至少用轻量快照把主/备下拉填上
    const snap = (STATE && STATE.ai_engines) || null;
    if (snap) {
      const p = document.getElementById("aiPrimaryEngine");
      const f = document.getElementById("aiFallbackEngine");
      if (p) p.value = snap.primary || "builtin";
      if (f) f.value = snap.fallback || "builtin";
      _aiFillInputs(snap.config);
      syncAiEngineBoxes();
    }
    return;
  }
  _AI_CFG = r;
  const p = document.getElementById("aiPrimaryEngine");
  const f = document.getElementById("aiFallbackEngine");
  if (p) p.value = r.primary || "builtin";
  if (f) f.value = r.fallback || "builtin";
  _aiFillInputs(r.config);
  syncAiEngineBoxes();
  const eng = r.engines || {};
  _aiRenderStatus("primary", eng[r.primary]);
  _aiRenderStatus("fallback", eng[r.fallback]);
  // 顺带刷新 llama.cpp 部署进度条
  if (r.deploy) updateAiDeployUI(r.deploy);
}

/** 草稿配置探测：探测「当前控件取值」而非已保存值，消除选择与状态错位 */
async function probeAiEngine() {
  if (_AI_PROBING) return;
  const engine = (document.getElementById("aiPrimaryEngine") || {}).value;
  const which = document.getElementById("aiFallbackEngine");
  const msg = document.getElementById("aiEngineMsg");
  _AI_PROBING = true;
  _aiMsg("探测中…（本地引擎约 2 秒，云端约 8 秒）");
  try {
    /* 后端 probe_ai_engine(engine=None, **draft) —— 草稿键用命名参数传 */
    const draft = {
      local_host: (document.getElementById("aiLocalHost") || {}).value,
      local_model: (document.getElementById("aiLocalModel") || {}).value,
      cloud_base_url: (document.getElementById("aiCloudBase") || {}).value,
      cloud_model: (document.getElementById("aiCloudModel") || {}).value,
      cloud_api_key: (document.getElementById("aiCloudKey") || {}).value
    };
    const r = await apiCall.apply(null,
      ["probe_ai_engine", engine].concat(Object.values(draft)));
    if (r && r.engine) {
      _aiRenderStatus(which && which.value === r.engine ? "fallback" : "primary", r);
      _aiMsg((r.available ? "✓ " : "✗ ") + r.label + "：" + r.detail,
              r.available ? "ok" : "err");
      // 探测 local 时顺便把已安装模型拉下来
      if (r.engine === "local" && r.available) await loadLocalAiModels();
      if (r.engine === "local" && !r.available) {
        const sel = document.getElementById("aiLocalModels");
        if (sel) sel.innerHTML = '<option value="">未检测到本机 Ollama</option>';
      }
    } else {
      _aiMsg("探测失败：" + ((r && r.error) || "未知错误"), "err");
    }
  } catch (e) {
    _aiMsg("探测异常：" + e, "err");
  } finally {
    _AI_PROBING = false;
  }
}

/** 【v1.3.2】「检测本地 AI 引擎」按钮专用：显式探测本机 Ollama 并加载已安装模型。
    原先按钮直接调 probeAiEngine()——探测的是主路线下拉框当前选中的引擎，
    下拉停在「内置离线AI」时探测结果为 builtin，模型列表永远不会加载。 */
let _LOCAL_PROBING = false;
async function probeLocalAi() {
  if (_LOCAL_PROBING) return;
  _LOCAL_PROBING = true;
  const host = (document.getElementById("aiLocalHost") || {}).value || "";
  _aiMsg("正在检测本机 Ollama（" + (host || "http://127.0.0.1:11434") + "）…");
  try {
    /* 后端 probe_ai_engine(engine, local_host, local_model, cloud_base, cloud_model, cloud_key) —— 强制探测 local */
    const r = await apiCall("probe_ai_engine", "local", host, "", "", "", "");
    if (r && r.engine === "local") {
      _aiMsg((r.available ? "✓ " : "✗ ") + r.label + "：" + r.detail,
              r.available ? "ok" : "err");
      if (r.available) {
        await loadLocalAiModels();
      } else {
        const sel = document.getElementById("aiLocalModels");
        if (sel) sel.innerHTML = '<option value="">未检测到本机 Ollama</option>';
      }
    } else {
      _aiMsg("检测失败：" + ((r && r.error) || "未知错误"), "err");
    }
  } catch (e) {
    _aiMsg("检测异常：" + e, "err");
  } finally {
    _LOCAL_PROBING = false;
  }
}

async function loadLocalAiModels() {
  const host = (document.getElementById("aiLocalHost") || {}).value;
  const sel = document.getElementById("aiLocalModels");
  if (!sel) return;
  const r = await apiCall("list_local_ai_models", host || "");
  const ms = (r && r.models) || [];
  sel.innerHTML = ms.length
    ? ('<option value="">已安装 ' + ms.length + ' 个模型（选择以填入）</option>' +
       ms.map(m => '<option value="' + esc(m) + '">' + esc(m) + '</option>').join(""))
    : '<option value="">未检测到本机 Ollama</option>';
}

async function launchLocalAi() {
  _aiMsg("正在尝试拉起 Ollama…");
  const r = await apiCall("launch_local_ai");
  _aiMsg((r && r.message) || "操作完成", (r && r.ok) ? "ok" : "err");
  if (r && r.ok) setTimeout(probeAiEngine, 1500);
}

/** 保存全部 AI 设置 */
async function saveAiEngines() {
  const val = id => { const e = document.getElementById(id); return e ? e.value.trim() : ""; };
  const cb = id => { const e = document.getElementById(id); return e ? e.checked : false; };
  const r = await apiCall("save_ai_settings",
    val("aiPrimaryEngine"), val("aiFallbackEngine"),
    val("aiLocalHost"), val("aiLocalModel"), cb("aiLocalAutoStart"),
    cb("aiGgufAutoDownload"),
    val("aiCloudBase"), val("aiCloudKey"), val("aiCloudModel"));
  if (r && r.ok) {
    _aiMsg("已保存。主路线：" + (r.primary || "") + " · 备路线：" + (r.fallback || ""), "ok");
    toast("AI 设置已保存", "ok");
    _AI_CFG = r;
    const eng = r.engines || {};
    _aiRenderStatus("primary", eng[r.primary]);
    _aiRenderStatus("fallback", eng[r.fallback]);
    if (r.deploy) updateAiDeployUI(r.deploy);
    if (r.primary === "local_gguf" && r.deploy && !r.deploy.exists) startDeployPoll();
    await refreshStateFresh();
  } else {
    _aiMsg("保存失败：" + ((r && r.error) || "未知错误"), "err");
    toast((r && r.error) || "保存失败", "err");
  }
}

/** 实测当前主路线：发一句最短 prompt 看能否返回 */
async function testAiEngine() {
  const engine = (document.getElementById("aiPrimaryEngine") || {}).value;
  _aiMsg("实测中…（本地引擎可能需数秒加载模型）");
  const r = await apiCall("test_ai_engine", engine);
  if (r && r.ok) {
    const lb = AI_ENGINE_FALLBACK_LABELS[r.engine] || r.engine;
    _aiMsg("✓ " + lb + " 实测通过：" + (r.text || "（空）"), "ok");
  } else {
    const lb = AI_ENGINE_FALLBACK_LABELS[r && r.engine] || (engine || "");
    _aiMsg("✗ " + lb + " 实测失败：" + ((r && r.error) || "未知错误"), "err");
  }
}

/** AI 子系统自检 */
async function aiSelfCheck() {
  _aiMsg("自检中…");
  const r = await apiCall("ai_self_check");
  if (!r || !r.checks) { _aiMsg("自检失败：" + ((r && r.error) || "无返回"), "err"); return; }
  const lines = r.checks.map(c => (c.ok ? "✓ " : "✗ ") + c.name + (c.detail ? " · " + c.detail : ""));
  _aiMsg(lines.join("\n"), r.ok ? "ok" : "err");
  const el = document.getElementById("aiEngineMsg");
  if (el) el.style.whiteSpace = "pre-wrap";
  toast(r.ok ? "AI 自检全部通过" : "AI 自检发现问题", r.ok ? "ok" : "err");
}

/* 旧版二引擎入口（保留以兼容旧 HTML/书签） */
async function saveAiEngine() {
  const p = document.getElementById("aiPrimaryEngine");
  const val = p ? p.value : "builtin";
  const r = await apiCall("set_ai_engine", val);
  if (r && r.ok) { toast("AI 主路线已保存", "ok"); await refreshStateFresh(); }
  else toast((r && r.error) || "保存失败", "err");
}

async function startModelDownload() {
  const r = await apiCall("start_model_download");
  if (r && r.ok) {
    toast(r.already ? "模型已存在，无需下载" : "已开始下载模型（后台进行）", "ok");
    startDeployPoll();
  } else {
    toast((r && r.error) || "操作失败", "err");
  }
}

async function openModelsDir() {
  await apiCall("open_models_dir");
}

let _deployTimer = null;
function startDeployPoll() {
  if (_deployTimer) return;
  _deployTimer = setInterval(async () => {
    const r = await apiCall("get_model_progress");
    if (!r) { stopDeployPoll(); return; }
    updateAiDeployUI(r);
    if (!r.downloading) stopDeployPoll();
  }, 1000);
}
function stopDeployPoll() {
  if (_deployTimer) { clearInterval(_deployTimer); _deployTimer = null; }
}

function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + " " + u[i];
}

function updateAiDeployUI(s) {
  const bar = document.getElementById("aiDeployBar");
  const pct = document.getElementById("aiDeployPct");
  const status = document.getElementById("aiDeployStatus");
  const hint = document.getElementById("aiDeployHint");
  if (!bar) return;
  const p = Math.max(0, Math.min(100, Math.round((s.progress || 0) * 100)));
  bar.style.width = p + "%";
  pct.textContent = p + "%";
  let msg = "";
  if (s.downloading) {
    msg = `下载中… ${fmtBytes(s.downloaded_bytes)} / ${fmtBytes(s.total_bytes)}（${fmtBytes(s.speed)}/s）`;
  } else if (s.exists && s.loaded) {
    msg = `已就绪：模型已下载并加载（${s.engine}）`;
  } else if (s.exists && !s.loaded) {
    msg = `模型已下载，将在首次使用时加载（${s.engine}）`;
  } else if (s.error) {
    msg = `错误：${s.error}`;
  } else {
    msg = `模型文件未下载（将保存到 ${s.models_dir}）`;
  }
  if (status) status.textContent = msg;
  if (hint) hint.textContent = s.exists ? "" : "（首次启用主路线会自动下载，约 1GB）";
}

async function refreshAiDeploy() {
  const r = await apiCall("get_model_progress");
  if (!r) return;
  updateAiDeployUI(r);
  if (r.downloading) startDeployPoll();
}

/* ---------------- AI 人格 ---------------- */
let SELECTED_PERSONA = "";
let EDITING_CUSTOM = null;   // 正在编辑的自定义人格名称（null 表示新建）

function allPersonas() {
  const builtins = STATE.personas || [];
  const customs = Object.keys(STATE.config.custom_personas || {});
  return { builtins, customs };
}

function renderAI() {
  const { builtins, customs } = allPersonas();
  const current = STATE.config.persona || builtins[0] || "";
  SELECTED_PERSONA = current;
  document.getElementById("currentPersonaLabel").textContent = current || "—";

  // 标题右侧：当前引擎与状态（【v1.2.0】读三引擎快照，旧二引擎标识做映射）
  const aiOn = STATE.config.ai_enabled;
  const snap = STATE.ai_engines || {};
  const _LEG = { rule: "builtin", model: "local_gguf" };
  const eng = snap.primary || _LEG[STATE.config.ai_engine] || "builtin";
  const fbk = snap.fallback || "builtin";
  const engLabel = (snap.labels && snap.labels[eng]) ||
                   AI_ENGINE_FALLBACK_LABELS[eng] || eng;
  const statusEl = document.getElementById("aiModelStatus");
  if (statusEl) {
    let routeTxt = "引擎：<b>" + esc(engLabel) + "</b>";
    if (fbk && fbk !== eng) {
      routeTxt += " <span class='muted'>（备：" +
        esc((snap.labels && snap.labels[fbk]) || AI_ENGINE_FALLBACK_LABELS[fbk] || fbk) +
        "）</span>";
    }
    statusEl.innerHTML =
      routeTxt +
      `<span class="dot ${aiOn ? "on" : "off"}"></span>${aiOn ? "已开启" : "已关闭"}` +
      ` · 当前人格：<b>${esc(current || "—")}</b>`;
  }

  const box = document.getElementById("personaList");
  const buildCard = (p, isCustom) => {
    const active = p === SELECTED_PERSONA ? " active" : "";
    const badge = isCustom ? '<span class="tag blue" style="margin-left:6px;">自定义</span>' : "";
    const del = isCustom
      ? `<button class="persona-del" title="删除该人格" onclick="event.stopPropagation(); deleteCustom('${esc(p)}')">×</button>`
      : "";
    return `<div class="persona-card${active}" data-name="${esc(p)}">
      <div class="persona-name">${esc(p)}${badge}</div>${del}
    </div>`;
  };
  let html = "";
  if (builtins.length) {
    html += `<div class="persona-group"><div class="persona-group-title">内置人格</div><div class="persona-grid">` +
      builtins.map(p => buildCard(p, false)).join("") + `</div></div>`;
  }
  if (customs.length) {
    html += `<div class="persona-group"><div class="persona-group-title">自定义人格</div><div class="persona-grid">` +
      customs.map(p => buildCard(p, true)).join("") + `</div></div>`;
  }
  if (!builtins.length && !customs.length) {
    html = '<div class="empty">暂无人格可选</div>';
  }
  box.innerHTML = html;
  box.querySelectorAll(".persona-card").forEach(c => {
    c.onclick = () => selectPersona(c.dataset.name);
  });
}

function selectPersona(name) {
  SELECTED_PERSONA = name;
  document.getElementById("currentPersonaLabel").textContent = name;
  document.querySelectorAll(".persona-card").forEach(c => {
    c.classList.toggle("active", c.dataset.name === name);
  });
  // 选中「自定义人格」时，将其人设介绍载入编辑区，便于直接修改后覆盖保存
  const custom = (STATE.config.custom_personas || {})[name];
  if (custom) {
    document.getElementById("custName").value = name;
    document.getElementById("custDesc").value = Array.isArray(custom) ? custom.join("\n") : custom;
    EDITING_CUSTOM = name;
    showCustomEditHint(name);
  } else {
    // 选中内置人格则清空编辑区，避免误覆盖
    document.getElementById("custName").value = "";
    document.getElementById("custDesc").value = "";
    EDITING_CUSTOM = null;
    clearCustomEditHint();
  }
}

function showCustomEditHint(name) {
  const el = document.getElementById("custEditHint");
  if (el) el.textContent = `正在编辑「${name}」：修改后点击「保存自定义人格」即可覆盖更新。`;
}

function clearCustomEditHint() {
  const el = document.getElementById("custEditHint");
  if (el) el.textContent = "";
}

async function savePersona() {
  if (!SELECTED_PERSONA) { toast("请先选择一个人格", "err"); return; }
  await apiCall("save_config", JSON.stringify({ persona: SELECTED_PERSONA }));
  toast("人格已保存", "ok"); await refreshStateFresh();
}

async function previewAI() {
  const text = document.getElementById("aiTest").value;
  const persona = SELECTED_PERSONA || STATE.config.persona;
  if (!persona) { toast("请先选择人格", "err"); return; }
  const custom = (STATE.config.custom_personas || {})[persona];
  const r = await apiCall("preview_ai", text, persona, custom ? JSON.stringify(custom) : "");
  const box = document.getElementById("aiPreview");
  box.textContent = r.text || "（无输出）";
  box.classList.add("has-content");
}

async function saveCustom() {
  const name = document.getElementById("custName").value.trim();
  const desc = document.getElementById("custDesc").value.trim();
  if (!name || !desc) { toast("请填写人格名称与人设介绍", "err"); return; }
  // 处于编辑态且改了名称：先删除旧条目，避免残留重复人格
  if (EDITING_CUSTOM && EDITING_CUSTOM !== name) {
    await apiCall("delete_custom_persona", EDITING_CUSTOM);
  }
  const r = await apiCall("save_custom_persona", name, JSON.stringify(desc));
  if (r.ok) {
    toast("自定义人格已保存", "ok");
    EDITING_CUSTOM = name;
    showCustomEditHint(name);
    await refreshStateFresh();
  } else toast(r.error, "err");
}

async function deleteCustom(name) {
  if (!confirm(`确定删除自定义人格「${name}」？`)) return;
  const r = await apiCall("delete_custom_persona", name);
  if (r.ok) {
    if (SELECTED_PERSONA === name) SELECTED_PERSONA = "";
    if (EDITING_CUSTOM === name) {
      EDITING_CUSTOM = null;
      clearCustomEditHint();
      document.getElementById("custName").value = "";
      document.getElementById("custDesc").value = "";
    }
    toast("已删除自定义人格", "ok");
    await refreshStateFresh();
  } else toast(r.error || "删除失败", "err");
}

/* ---------------- 黑名单 ---------------- */
let blTemp = [];

/* 支持：换行 / 逗号(中英) / 顿号 / 分号(中英) / 空格 多种分隔符，一次解析多个关键词 */
function parseKeywords(text) {
  return String(text || "")
    .split(/[\r\n,，、;；\s]+/)
    .map(s => s.trim())
    .filter(Boolean);
}

function renderBlacklist() {
  blTemp = (STATE.config.blacklist || []).slice();
  drawChips();
}

function blIsDirty() {
  const saved = (STATE.config && STATE.config.blacklist) || [];
  if (saved.length !== blTemp.length) return true;
  return blTemp.some((w, i) => w !== saved[i]);
}

function drawChips() {
  const box = document.getElementById("blChips");
  const searchEl = document.getElementById("blSearch");
  const kw = (searchEl && searchEl.value || "").trim().toLowerCase();

  const cnt = document.getElementById("blCount");
  if (cnt) cnt.textContent = blTemp.length;
  const dirty = blIsDirty();
  const dEl = document.getElementById("blDirty");
  if (dEl) dEl.style.display = dirty ? "inline" : "none";
  const btn = document.getElementById("btnSaveBl");
  if (btn) btn.classList.toggle("dirty", dirty);

  if (!blTemp.length) {
    box.innerHTML = '<span class="muted">暂无关键词 —— 添加后，包含这些词的微博会被自动跳过</span>';
    return;
  }
  const list = blTemp.map((w, i) => ({ w, i }))
    .filter(x => !kw || x.w.toLowerCase().includes(kw));
  if (!list.length) {
    box.innerHTML = `<span class="muted">没有匹配「${esc(kw)}」的关键词</span>`;
    return;
  }
  box.innerHTML = list.map(({ w, i }) =>
    `<span class="chip bl-chip"><span class="bl-chip-idx">${i + 1}</span>${esc(w)}<button title="删除" onclick="delBl(${i})">×</button></span>`
  ).join("");
}

function onBlInputKey(e) {
  if (e.key === "Enter") { e.preventDefault(); addBlacklist(); }
}

function addBlacklist() {
  const input = document.getElementById("blInput");
  const list = parseKeywords(input.value);
  if (!list.length) return;
  let added = 0, dup = 0;
  for (const v of list) {
    if (blTemp.includes(v)) { dup++; continue; }
    blTemp.push(v);
    added++;
  }
  input.value = "";
  drawChips();
  if (added) toast(`已添加 ${added} 个关键词${dup ? `，${dup} 个已存在已跳过` : ""}`, "ok");
  else toast("这些关键词都已存在", "err");
}

function delBl(i) { blTemp.splice(i, 1); drawChips(); }

function clearBlacklist() {
  if (!blTemp.length) { toast("列表已经是空的", "err"); return; }
  if (!confirm(`确认清空全部 ${blTemp.length} 个关键词？\n（清空后需点击「保存黑名单」才会生效）`)) return;
  blTemp = [];
  drawChips();
  toast("已清空列表，记得保存", "ok");
}

function toggleBlBulk() {
  const box = document.getElementById("blBulkBox");
  const open = box.style.display === "none";
  box.style.display = open ? "block" : "none";
  if (open) document.getElementById("blBulkText").value = blTemp.join("\n");
}

function submitBlBulk() {
  const raw = document.getElementById("blBulkText").value;
  const list = [];
  const seen = new Set();
  for (const v of parseKeywords(raw)) {
    if (!seen.has(v)) { seen.add(v); list.push(v); }
  }
  blTemp = list;
  drawChips();
  toggleBlBulk();
  toast(`已重建列表：共 ${list.length} 个关键词（记得保存）`, "ok");
}

async function exportBlacklist() {
  if (!blTemp.length) { toast("列表为空，无需导出", "err"); return; }
  const r = await apiCall("export_blacklist", JSON.stringify(blTemp));
  if (r && r.ok) toast(`已导出 ${r.count} 个关键词：${r.path}`, "ok");
  else toast((r && r.error) || "导出失败", "err");
}

async function saveBlacklist() {
  const r = await apiCall("save_config", JSON.stringify({ blacklist: blTemp }));
  if (r && r.ok === false) { toast((r && r.error) || "保存失败", "err"); return; }
  await refreshStateFresh();
  renderBlacklist();
  toast(`黑名单已保存（${blTemp.length} 个关键词）`, "ok");
}

/* ---------------- 工作时间 ---------------- */
function _toMin(hhmm) {
  const [h, m] = (hhmm || "0:0").split(":").map(Number);
  return (h || 0) * 60 + (m || 0);
}
function renderSchedule() {
  // 【v1.2.2】同 updateWhBar：config 未就绪时先跳过，别抛错打断整轮渲染
  const w = (STATE && STATE.config && STATE.config.working_hours) || null;
  if (!w) return;
  document.getElementById("whSwitch").checked = !!w.enabled;
  document.getElementById("whStart").value = w.start;
  document.getElementById("whEnd").value = w.end;
  updateWhBar();
}
function updateWhBar() {
  // 【v1.2.2】工作时间已并入转发设置的子标签页，可能在 renderSchedule()
  // 跑之前就被 switchForwardTab() 调到；此时 STATE.config 可能还没填好
  // working_hours，直接取 w.start 会抛错并中断整个探测循环。故先兜底。
  const w = (STATE && STATE.config && STATE.config.working_hours) || null;
  if (!w) return;
  const start = _toMin(w.start), end = _toMin(w.end);
  const now = new Date();
  const nowMin = now.getHours() * 60 + now.getMinutes();
  // 当前是否在工作时间
  let active;
  if (start <= end) active = nowMin >= start && nowMin <= end;
  else active = nowMin >= start || nowMin <= end;  // 跨天
  const tag = document.getElementById("whNow");
  if (tag) {
    if (!w.enabled) { tag.textContent = "未启用"; tag.className = "tag blue"; }
    else if (active) { tag.textContent = "当前：工作时间内"; tag.className = "tag green"; }
    else { tag.textContent = "当前：非工作时间"; tag.className = "tag yellow"; }
  }
  // 时间条
  const fill = document.getElementById("whFill");
  const mark = document.getElementById("whNowMark");
  if (fill && mark) {
    const seg = (start <= end) ? [[start, end]] : [[start, 1440], [0, end]];
    // 用两个填充段表示（跨天拆成两段）
    let segs = seg.map(([a, b]) => {
      const left = (a / 1440) * 100;
      const width = ((b - a) / 1440) * 100;
      return `<div class="time-bar-fill" style="left:${left}%;width:${width}%;"></div>`;
    }).join("");
    // 若只有一段，覆盖原 fill；否则用 innerHTML 重建（简单处理：始终用单元素）
    if (seg.length === 1) {
      fill.style.display = "block";
      fill.style.background = "";
      fill.style.left = (seg[0][0] / 1440 * 100) + "%";
      fill.style.width = ((seg[0][1] - seg[0][0]) / 1440 * 100) + "%";
      mark.style.left = (nowMin / 1440 * 100) + "%";
    } else {
      // 跨天：用一个渐变背景近似（两段）
      const w1 = (1440 - seg[0][0]) / 1440 * 100;
      const w2 = seg[1][1] / 1440 * 100;
      fill.style.display = "block";
      fill.style.left = "0%";
      fill.style.width = "100%";
      fill.style.background =
        `linear-gradient(90deg, transparent 0%, var(--accent) ${w1}%, var(--accent) ${100 - w2}%, transparent 100%)`;
      mark.style.left = (nowMin / 1440 * 100) + "%";
    }
  }
}
async function saveWorkingHours() {
  const enabled = document.getElementById("whSwitch").checked;
  const start = document.getElementById("whStart").value;
  const end = document.getElementById("whEnd").value;
  await apiCall("save_config", JSON.stringify({ working_hours: { enabled, start, end } }));
  toast("工作时间已保存", "ok"); await refreshStateFresh();
}

/* ---------------- 调度 ---------------- */
async function startScheduler() {
  const r = await apiCall("start_scheduler");
  if (r.running) toast("自动轮询已启动", "ok"); else toast("启动失败", "err");
  await refreshStateFresh();
}
async function stopScheduler() {
  await apiCall("stop_scheduler"); toast("已停止"); await refreshStateFresh();
}
/* 统一的「后台异步轮询 + 进度条」驱动：manualRun 与 forwardRandomNow 共用。
   apiName: 后端接口名（manual_run_async / forward_random_now）；
   startLabel: 进度条初始文案。 */
function runPollWithProgress(apiName, startLabel) {
  const wrap = document.getElementById("pollProgressWrap");
  const fill = document.getElementById("pollProgressBar");
  const label = document.getElementById("pollProgressLabel");
  // 【v1.5.1 修复·BUG2】上一轮「完成 1.8s 后隐藏」的定时器若还没触发，
  // 会在新一轮轮询进行中把整条进度条误藏（实测复现：完成后 1.8s 内再次
  // 「立即轮询一次」→ 进度条中途消失）。进入时先清掉遗留定时器。
  clearTimeout(window._pollHideTimer);
  wrap.style.display = "flex";
  // 重置宽度时跳过过渡：否则从 0% 起步的 .25s 过渡会让「快速完成的轮询」
  // 在用户眼里「进度条几乎不显示」（条还贴在 0% 处，文案却已跳到完成）。
  fill.style.transition = "none";
  fill.style.width = "0%";
  void fill.offsetWidth;               // 强制 reflow，让上面的 transition:none 生效
  fill.style.transition = "";          // 恢复 CSS 里的宽度过渡
  label.textContent = startLabel;

  // 若已有进度轮询在跑，避免创建重复定时器（挂接在已有的那次进度轮询上）
  if (_pollTimer) return;

  // 后台异步执行，立即返回，避免阻塞界面
  apiCall(apiName).then((r) => {
    if (!r || !r.started) {
      label.textContent = (r && r.error) ? r.error : "操作进行中…";
    }
  });

  // 每 300ms 轮询进度，直到 active 变为 false
  _pollTimer = setInterval(async () => {
    try {
      const p = await apiCall("get_poll_progress");
      if (!p) return;
      if (p.active) {
        let pct = 5;
        if (p.total > 0) pct = Math.round((p.done / p.total) * 100);
        else if (p.phase === "forward") pct = 95;
        fill.style.width = Math.min(100, pct) + "%";
        label.textContent = `轮询中 ${p.done}/${p.total}` + (p.current ? ` · ${p.current}` : "");
      } else {
        clearInterval(_pollTimer);
        _pollTimer = null;
        // 完成时直达满格（跳过过渡）：快速轮询的锅——用户点完按钮条还没长
        // 就结束了，0%→100% 的 .25s 过渡会让条「看起来没显示」。
        fill.style.transition = "none";
        fill.style.width = "100%";
        void fill.offsetWidth;
        fill.style.transition = "";
        const res = p.result || {};
        if (res.ok) {
          if (res.forwarded) label.textContent = "已转发 · " + (res.message || "本轮转发 1 条");
          else label.textContent = "完成 · " + (res.message || "");
        } else {
          label.textContent = "完成 · " + (res.error || res.message || "无结果");
        }
        // 【v1.5.1 修复·BUG2】隐藏定时器存句柄，下次 runPollWithProgress 进入时
        // 会 clearTimeout，避免旧定时器误藏新一轮的进度条。
        clearTimeout(window._pollHideTimer);
        window._pollHideTimer = setTimeout(() => { wrap.style.display = "none"; }, 1800);
        refreshStateFresh();  // 刷新概览（含「最近一次轮询结果」）
      }
    } catch (e) { /* 忽略单次轮询异常，下一轮继续 */ }
  }, 300);
}

async function manualRun() {
  runPollWithProgress("manual_run_async", "正在启动轮询…");
}

/* 概览左上角实时时钟（日期 + 时间 + 星期） */
function updateOverviewClock() {
  const el = document.getElementById("ovClock");
  if (!el) return;
  const d = new Date();
  const wd = ["日", "一", "二", "三", "四", "五", "六"][d.getDay()];
  const p = (n) => String(n).padStart(2, "0");
  el.textContent =
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}  ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}  星期${wd}`;
}

/* ---------------- 历史 ---------------- */
let HIST_ROWS = [];
let HIST_FILTERS = { kind: "monitored", keyword: "", dateFrom: "", dateTo: "", status: "", ftype: "", hasImg: false };
let HIST_PAGE = 1;
let HIST_PAGE_SIZE = 20;
let HIST_FILTERED = [];
let HIST_PAGE_ROWS = [];

async function switchHist(kind) {
  histKind = kind;
  HIST_SELECTED.clear();
  updateSelCount();
  HIST_FILTERS.kind = kind;
  document.getElementById("histTitle").textContent =
    kind === "monitored" ? "被监控对象发过的微博" : "本软件转发记录";
  document.getElementById("btnHistMon").classList.toggle("active", kind === "monitored");
  document.getElementById("btnHistFwd").classList.toggle("active", kind === "forwards");
  renderHistFilters();
  if (kind === "monitored") {
    HIST_ROWS = await apiCall("get_monitored_posts", null, 500, 0);
  } else {
    HIST_ROWS = await apiCall("get_forwards", 500, 0);
  }
  applyHistFilter();
}

/* 修复历史记录里被抓成表情包的图片 */
async function repairHistoryImages() {
  if (!confirm("修复历史图片？\n\n将按微博详情接口重新抓取每条微博的真正配图（pic_infos），覆盖此前误存的表情包。\n最多处理最近 200 条，请耐心等待。")) return;
  const btn = (typeof event !== "undefined" && event) ? event.target : null;
  if (btn) { btn.disabled = true; btn.textContent = "修复中…"; }
  try {
    const res = await apiCall("repair_history_images", 200);
    if (res && res.ok) {
      toast(`修复完成：重新下载 ${res.fixed} 条，清除误存表情 ${res.cleaned} 条，失败 ${res.failed} 条`);
      await switchHist(histKind);
    } else {
      toast("修复失败：" + ((res && res.error) || "未知错误"));
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "修复历史图片"; }
  }
}

function renderHistFilters() {
  const box = document.getElementById("histFilters");
  box.className = "row hist-filters";
  if (histKind === "monitored") {
    box.innerHTML = `
      <div class="hist-filter-group">
        <label class="field hist-filter-keyword">关键词（昵称/UID/内容）
          <input id="hfKeyword" placeholder="输入关键词" oninput="applyHistFilter()">
        </label>
        <label class="field hist-filter-date">开始日期
          <input id="hfDateFrom" type="date" onchange="applyHistFilter()">
        </label>
        <label class="field hist-filter-date">结束日期
          <input id="hfDateTo" type="date" onchange="applyHistFilter()">
        </label>
        <label class="field hist-filter-check">
          <span class="hist-check-label">仅看有图</span>
          <span class="hist-check-box"><input type="checkbox" id="hfHasImg" onchange="applyHistFilter()"></span>
        </label>
        <button class="ghost hist-filter-clear" onclick="clearHistFilters()">清空筛选</button>
      </div>`;
  } else {
    box.innerHTML = `
      <div class="hist-filter-group">
        <label class="field hist-filter-keyword">关键词（账户/UID/内容）
          <input id="hfKeyword" placeholder="输入关键词" oninput="applyHistFilter()">
        </label>
        <label class="field" style="width:110px;">状态
          <select id="hfStatus" onchange="applyHistFilter()">
            <option value="">全部</option><option value="success">成功</option><option value="skipped">跳过</option><option value="failed">失败</option>
          </select>
        </label>
        <label class="field" style="width:110px;">转发类型
          <select id="hfFtype" onchange="applyHistFilter()">
            <option value="">全部</option><option value="ai">AI</option><option value="normal">普通</option>
          </select>
        </label>
        <label class="field hist-filter-date">开始日期
          <input id="hfDateFrom" type="date" onchange="applyHistFilter()">
        </label>
        <label class="field hist-filter-date">结束日期
          <input id="hfDateTo" type="date" onchange="applyHistFilter()">
        </label>
        <button class="ghost hist-filter-clear" onclick="clearHistFilters()">清空筛选</button>
      </div>`;
  }
}

function _readHistFilters() {
  const keyword = document.getElementById("hfKeyword")?.value.trim().toLowerCase() || "";
  const dateFrom = document.getElementById("hfDateFrom")?.value || "";
  const dateTo = document.getElementById("hfDateTo")?.value || "";
  const status = document.getElementById("hfStatus")?.value || "";
  const ftype = document.getElementById("hfFtype")?.value || "";
  const hasImg = document.getElementById("hfHasImg")?.checked || false;
  HIST_FILTERS = { kind: histKind, keyword, dateFrom, dateTo, status, ftype, hasImg };
}

function _matchDate(dateStr, fromStr, toStr) {
  if (!dateStr) return true;
  const ds = dateStr.slice(0, 10);
  if (fromStr && ds < fromStr) return false;
  if (toStr && ds > toStr) return false;
  return true;
}

function applyHistFilter() {
  _readHistFilters();
  let rows = HIST_ROWS;
  const kw = HIST_FILTERS.keyword;
  if (kw) {
    rows = rows.filter(r => {
      const hay = [r.uid, r.screen_name, r.content, r.account_name, r.original_content, r.forward_content].join(" ").toLowerCase();
      return hay.includes(kw);
    });
  }
  if (HIST_FILTERS.kind === "monitored") {
    if (HIST_FILTERS.hasImg) rows = rows.filter(r => (r.images || []).length > 0);
    rows = rows.filter(r => _matchDate(r.fetched_at || r.created_at, HIST_FILTERS.dateFrom, HIST_FILTERS.dateTo));
  } else {
    if (HIST_FILTERS.status) rows = rows.filter(r => r.status === HIST_FILTERS.status);
    if (HIST_FILTERS.ftype) rows = rows.filter(r => r.forward_type === HIST_FILTERS.ftype);
    rows = rows.filter(r => _matchDate(r.created_at, HIST_FILTERS.dateFrom, HIST_FILTERS.dateTo));
  }
  HIST_FILTERED = rows;
  HIST_PAGE = 1;
  renderHistPage();
}

function renderHistPage() {
  const total = HIST_FILTERED.length;
  const start = (HIST_PAGE - 1) * HIST_PAGE_SIZE;
  const pageRows = HIST_FILTERED.slice(start, start + HIST_PAGE_SIZE);
  HIST_PAGE_ROWS = pageRows;
  if (HIST_FILTERS.kind === "monitored") {
    renderMonitored(pageRows);
  } else {
    renderForwards(pageRows);
  }
  renderPagination(total);
  syncSelAll();
}

function renderPagination(total) {
  const box = document.getElementById("histPagination");
  const totalPages = Math.max(1, Math.ceil(total / HIST_PAGE_SIZE));
  const start = total ? (HIST_PAGE - 1) * HIST_PAGE_SIZE + 1 : 0;
  const end = Math.min(HIST_PAGE * HIST_PAGE_SIZE, total);
  const presets = [10, 20, 50, 100];
  const isCustom = !presets.includes(HIST_PAGE_SIZE);
  const pageSizeBtns = presets.map(n =>
    `<button class="small page-size-btn ${HIST_PAGE_SIZE === n ? "active" : "ghost"}" onclick="changePageSize(${n})">${n}</button>`
  ).join("");

  box.innerHTML = `
    <div class="page-size-group" title="每页显示条数">
      ${pageSizeBtns}
      <button class="small page-size-btn ${isCustom ? "active" : "ghost"}" onclick="showCustomPageSize()">自定义</button>
    </div>
    <span id="customPageWrap" style="display:${isCustom ? "inline-flex" : "none"};">
      <input id="pageSizeNum" type="number" min="1" placeholder="条" value="${isCustom ? HIST_PAGE_SIZE : ""}">
      <button class="small ghost" onclick="applyCustomPageSize()">应用</button>
    </span>
    <div class="page-nav-group">
      <button class="small ghost" onclick="changeHistPage(-1)" ${HIST_PAGE <= 1 ? "disabled" : ""}>上一页</button>
      <span class="muted page-info">${HIST_PAGE} / ${totalPages}（${start}-${end} / ${total}）</span>
      <button class="small ghost" onclick="changeHistPage(1)" ${HIST_PAGE >= totalPages ? "disabled" : ""}>下一页</button>
    </div>
  `;
}

function onPageSizeChange(v) {
  if (v === "custom") {
    showCustomPageSize();
    return;
  }
  changePageSize(parseInt(v, 10));
}

function showCustomPageSize() {
  const wrap = document.getElementById("customPageWrap");
  if (wrap) {
    wrap.style.display = "inline-flex";
    const inp = document.getElementById("pageSizeNum");
    if (inp) { inp.value = HIST_PAGE_SIZE; inp.focus(); }
  }
}

function applyCustomPageSize() {
  const n = parseInt(document.getElementById("pageSizeNum").value, 10);
  if (!n || n < 1) { toast("请输入有效的每页条数", "err"); return; }
  if (n > 2000) { toast("每页最多 2000 条", "err"); return; }
  changePageSize(n);
}

function changePageSize(size) {
  HIST_PAGE_SIZE = parseInt(size, 10);
  HIST_PAGE = 1;
  renderHistPage();
}

function changeHistPage(delta) {
  const totalPages = Math.max(1, Math.ceil(HIST_FILTERED.length / HIST_PAGE_SIZE));
  HIST_PAGE = Math.max(1, Math.min(totalPages, HIST_PAGE + delta));
  renderHistPage();
}

function clearHistFilters() {
  document.getElementById("hfKeyword").value = "";
  document.getElementById("hfDateFrom").value = "";
  document.getElementById("hfDateTo").value = "";
  if (document.getElementById("hfStatus")) document.getElementById("hfStatus").value = "";
  if (document.getElementById("hfFtype")) document.getElementById("hfFtype").value = "";
  if (document.getElementById("hfHasImg")) document.getElementById("hfHasImg").checked = false;
  applyHistFilter();
}

function _chkCell(id) {
  const checked = HIST_SELECTED.has(id) ? "checked" : "";
  return `<td class="col-chk"><input type="checkbox" class="histChk" data-id="${id}" ${checked} onchange="toggleHistSelect(${id}, this.checked)"></td>`;
}

function _histAvatar(r) {
  const ch = esc((r.screen_name || r.uid || "?").charAt(0).toUpperCase());
  const local = r.avatar_local || "";
  if (local) {
    return `<img class="hist-avatar hist-avatar-img" data-avatar="${esc(local)}" src="" alt="${ch}" title="${ch}" onerror="this.onerror=null;this.classList.add('hist-avatar-fallback');this.src='data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';">`;
  }
  return `<div class="hist-avatar">${ch}</div>`;
}

async function loadHistAvatars() {
  document.querySelectorAll(".hist-avatar-img").forEach(async img => {
    const local = img.dataset.avatar;
    if (!local) return;
    try {
      const r = await apiCall("load_image_base64", local);
      if (r.ok) img.src = r.data_url;
      else img.classList.add("hist-avatar-fallback");
    } catch (e) { img.classList.add("hist-avatar-fallback"); }
  });
}

function renderMonitored(rows) {
  const body = document.getElementById("histBody");
  if (!rows.length) { body.innerHTML = '<div class="empty">暂无符合条件的记录</div>'; return; }
  body.innerHTML = `
    <div class="hist-list">
      ${rows.map(r => `
        <div class="hist-card">
          <div class="hist-card-hd">
            ${_histAvatar(r)}
            <div class="hist-card-title">
              <div class="hist-name">${esc(r.screen_name || "—")} <span class="muted">@${esc(r.uid)}</span></div>
              <div class="hist-time muted">${esc(r.fetched_at || r.created_at)}</div>
            </div>
            <label class="hist-chk"><input type="checkbox" class="histChk" data-id="${r.id}" ${HIST_SELECTED.has(r.id) ? "checked" : ""} onchange="toggleHistSelect(${r.id}, this.checked)"></label>
          </div>
          <div class="hist-card-body">
            <div class="hist-content">${esc(r.content)}</div>
            ${renderImageThumbs(r.images || [])}
          </div>
        </div>
      `).join("")}
    </div>`;
  loadHistAvatars();
  bindImagePreviews();
}
function renderForwards(rows) {
  const body = document.getElementById("histBody");
  if (!rows.length) { body.innerHTML = '<div class="empty">暂无符合条件的记录</div>'; return; }
  body.innerHTML = `
    <div class="hist-list">
      ${rows.map(r => {
        const st = r.status === "success" ? '<span class="tag green">成功</span>'
          : r.status === "skipped" ? `<span class="tag yellow" title="${esc(r.reason)}">跳过</span>`
          : '<span class="tag red">失败</span>';
        return `
        <div class="hist-card">
          <div class="hist-card-hd">
            <div class="hist-avatar">${esc((r.account_name || "?").charAt(0).toUpperCase())}</div>
            <div class="hist-card-title">
              <div class="hist-name">${esc(r.account_name || "—")} <span class="muted">${esc(r.screen_name || "")}</span></div>
              <div class="hist-time muted">${esc(r.created_at)}</div>
            </div>
            <label class="hist-chk"><input type="checkbox" class="histChk" data-id="${r.id}" ${HIST_SELECTED.has(r.id) ? "checked" : ""} onchange="toggleHistSelect(${r.id}, this.checked)"></label>
          </div>
          <div class="hist-card-body">
            <div class="hist-section">
              <div class="hist-section-label muted">原微博</div>
              <div class="hist-content">${esc(r.original_content)}</div>
            </div>
            <div class="hist-section">
              <div class="hist-section-label muted">转发内容</div>
              <div class="hist-content">${esc(r.forward_content || "（纯转发）")}</div>
            </div>
          </div>
          <div class="hist-card-ft">
            ${r.forward_type === "ai" ? '<span class="tag blue">AI</span>' : '<span class="tag">普通</span>'}
            ${st}
          </div>
        </div>`;
      }).join("")}
    </div>`;
}

function toggleHistSelect(id, checked) {
  if (checked) HIST_SELECTED.add(id); else HIST_SELECTED.delete(id);
  updateSelCount();
  syncSelAll();
}
function selectAllToggle(checked) {
  HIST_PAGE_ROWS.forEach(r => {
    if (checked) HIST_SELECTED.add(r.id); else HIST_SELECTED.delete(r.id);
  });
  // 同步当前页复选框
  document.querySelectorAll(".histChk").forEach(c => { c.checked = checked; });
  updateSelCount();
  syncSelAll();
}
function syncSelAll() {
  const selAll = document.getElementById("selAll");
  if (!selAll || !HIST_PAGE_ROWS.length) return;
  const allSel = HIST_PAGE_ROWS.every(r => HIST_SELECTED.has(r.id));
  const someSel = HIST_PAGE_ROWS.some(r => HIST_SELECTED.has(r.id));
  selAll.checked = allSel;
  selAll.indeterminate = !allSel && someSel;
}
function updateSelCount() {
  const el = document.getElementById("selCount");
  if (el) el.textContent = HIST_SELECTED.size;
  const btn = document.getElementById("btnExportSel");
  if (btn) btn.disabled = HIST_SELECTED.size === 0;
}

function renderImageThumbs(images) {
  if (!images.length) return "—";
  return images.map((p, i) =>
    `<img class="thumb" data-path="${esc(p)}" alt="图${i+1}" title="点击预览">`
  ).join("");
}

function bindImagePreviews() {
  // 绑定点击预览 + 异步加载缩略图（修复：此前 img 无 src 且无人加载，缩略图永远空白）
  document.querySelectorAll("img.thumb:not([data-loaded])").forEach(img => {
    img.dataset.loaded = "1";
    img.onclick = () => previewImage(img.dataset.path);
    apiCall("load_image_base64", img.dataset.path).then(r => {
      if (r && r.ok) img.src = r.data_url;
      else img.alt = "×";           // 文件缺失时显示占位符
    }).catch(() => { img.alt = "×"; });
  });
}

async function previewImage(relPath) {
  const r = await apiCall("load_image_base64", relPath);
  if (r.ok) {
    document.getElementById("imgPreviewBox").innerHTML = `<img src="${r.data_url}" alt="preview">`;
    document.getElementById("imgOverlay").style.display = "flex";
  } else {
    toast(r.error || "图片加载失败", "err");
  }
}
function closeImgPreview() {
  document.getElementById("imgOverlay").style.display = "none";
  document.getElementById("imgPreviewBox").innerHTML = "";
}

/* 导出统一为「文件夹」形态：数据文件 + 引用到的图片副本 + README */
const EXPORT_FMTS = ["csv", "json", "xlsx"];
function pickExportFmt(title) {
  const v = prompt(title, "csv");
  if (!v) return "";
  const f = String(v).trim().toLowerCase();
  if (!EXPORT_FMTS.includes(f)) { toast("格式仅支持 csv / json / xlsx", "err"); return ""; }
  return f;
}
function afterExport(r) {
  const img = r.images ? `，含图片 ${r.images} 张` : "";
  toast(`已导出 ${r.count != null ? r.count + " 条" : "全部数据"}${img}：${r.path}`, "ok");
  apiCall("open_path", r.path);   // 自动打开导出目录，方便直接查看图片
}
async function exportFiltered() {
  const fmt = pickExportFmt("导出当前筛选：输入 csv / json / xlsx");
  if (!fmt) return;
  _readHistFilters();
  const r = await apiCall("export_filtered", histKind, fmt, JSON.stringify(HIST_FILTERS));
  if (r.ok) afterExport(r); else toast(r.error || "导出失败", "err");
}
async function exportSelected() {
  if (HIST_SELECTED.size === 0) { toast("请先勾选要导出的记录", "err"); return; }
  const fmt = pickExportFmt("导出选中记录：输入 csv / json / xlsx");
  if (!fmt) return;
  const ids = JSON.stringify([...HIST_SELECTED]);
  const r = await apiCall("export_selected", histKind, ids, fmt);
  if (r.ok) afterExport(r); else toast(r.error || "导出失败", "err");
}
async function exportAll() {
  const r = await apiCall("export_all_data");
  if (r.ok) afterExport({ ...r, count: r.count }); else toast(r.error || "导出失败", "err");
}
async function exportKind(kind) {
  const fmt = pickExportFmt("导出格式：输入 csv / json / xlsx");
  if (!fmt) return;
  const r = await apiCall("export_data", kind, fmt);
  if (r.ok) afterExport(r); else toast(r.error || "导出失败", "err");
}

/* ---------------- 数据总览 ---------------- */
let DATA_OVERVIEW = null;
let DATA_OVERVIEW_SERIES = "today_hourly";
let DATA_OVERVIEW_DATE = null;   // 选中历史某天（YYYY-MM-DD）的数据；null 表示使用区间 tab
let _dovChart = null;            // 趋势曲线当前渲染状态（供悬浮提示使用）

async function loadDataOverview() {
  try {
    const r = await apiCall("get_data_overview");
    if (!r || !r.ok) { toast(r.error || "数据总览加载失败", "err"); return; }
    DATA_OVERVIEW = r.data;
    const dp = document.getElementById("dataOverviewDate");
    if (dp) dp.max = new Date().toISOString().slice(0, 10);  // 不可选未来日期
    renderDataOverview();
  } catch (e) { toast("数据总览加载异常", "err"); }
}

function renderDataOverview() {
  const ov = DATA_OVERVIEW_DATE || DATA_OVERVIEW;
  if (!ov) return;
  const isDate = !!DATA_OVERVIEW_DATE;
  const dateLabel = isDate ? (DATA_OVERVIEW_DATE.date || "") : "";
  const summary = ov.summary || {};
  const top = ov.top_forwarded || [];
  const today = summary.today || {};
  // 日期模式下，本月统计回退到总览数据（DATA_OVERVIEW 始终含本月）
  const baseMonth = (DATA_OVERVIEW && DATA_OVERVIEW.summary && DATA_OVERVIEW.summary.month) || {};
  const month = isDate ? baseMonth : (summary.month || {});

  // 核心指标：日期模式下前两项展示该具体日期的抓取 / 转发
  const topName = top.length ? top[0].screen_name : "—";
  const monVal = isDate ? (summary.monitored || 0) : (today.monitored || 0);
  const fwdVal = isDate ? (summary.forwarded || 0) : (today.forwarded || 0);
  const lblMon = isDate ? `抓取(${dateLabel})` : "本日抓取";
  const lblFwd = isDate ? `转发(${dateLabel})` : "本日转发";
  document.getElementById("dataOverviewSummary").innerHTML = [
    [lblMon, monVal],
    [lblFwd, fwdVal],
    ["本月抓取", month.monitored || 0],
    ["本月转发", month.forwarded || 0],
    ["高频被转发", topName],
  ].map(([l, n]) => `<div class="stat"><div class="num">${n}</div><div class="lbl">${l}</div></div>`).join("");

  // 高频被转发对象 TOP10
  const topBox = document.getElementById("dataOverviewTop");
  if (!top.length) {
    topBox.innerHTML = `<li class="muted">暂无转发数据</li>`;
  } else {
    topBox.innerHTML = top.map((t, i) => `
      <li>
        <span><span class="rank">${i + 1}</span>${esc(t.screen_name || t.uid || "未知")}</span>
        <span class="count">${t.count} 次</span>
      </li>`).join("");
  }

  // 趋势图
  renderDataOverviewChart();
}

function renderDataOverviewChart() {
  let series;
  if (DATA_OVERVIEW_DATE) {
    series = (DATA_OVERVIEW_DATE.series || {}).hourly || [];
  } else {
    if (!DATA_OVERVIEW) return;
    series = (DATA_OVERVIEW.series || {})[DATA_OVERVIEW_SERIES] || [];
  }
  const wrap = document.getElementById("dataOverviewChartWrap");
  const svg = document.getElementById("dataOverviewChart");
  if (!series.length) {
    wrap.innerHTML = `<div class="empty" style="height:200px;">暂无该区间数据</div>`;
    return;
  }
  // 恢复 svg（之前 empty 可能替换掉了）
  if (!svg || svg.tagName !== "svg") {
    wrap.innerHTML = `<svg id="dataOverviewChart" class="data-chart" viewBox="0 0 800 260" preserveAspectRatio="none"></svg>`;
  }
  drawLineChart("dataOverviewChart", series, "monitored", "forwarded", "label");
}

function drawLineChart(svgId, data, keyA, keyB, labelKey) {
  const svg = document.getElementById(svgId);
  if (!svg) return;
  const W = 800, H = 260, pad = { top: 20, right: 20, bottom: 36, left: 44 };
  const labels = data.map(d => d[labelKey]);
  const maxV = Math.max(1, ...data.map(d => Math.max(d[keyA] || 0, d[keyB] || 0)));
  const chartW = W - pad.left - pad.right;
  const chartH = H - pad.top - pad.bottom;
  const step = data.length > 1 ? chartW / (data.length - 1) : chartW;

  const points = (key) => data.map((d, i) => {
    const x = pad.left + i * step;
    const v = d[key] || 0;
    const y = pad.top + chartH - (v / maxV) * chartH;
    return { x, y, v };
  });

  const pathD = (pts) => pts.map((p, i) => (i ? "L" : "M") + `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
  const ptsA = points(keyA), ptsB = points(keyB);

  // 网格线 + Y 轴文字
  let gridLines = "";
  let yLabels = "";
  const ySteps = 5;
  for (let i = 0; i <= ySteps; i++) {
    const v = Math.round((maxV / ySteps) * i);
    const y = pad.top + chartH - (i / ySteps) * chartH;
    gridLines += `<line x1="${pad.left}" y1="${y.toFixed(1)}" x2="${W - pad.right}" y2="${y.toFixed(1)}" stroke="rgba(255,255,255,.08)"/>`;
    yLabels += `<text x="${pad.left - 8}" y="${y + 4}" text-anchor="end" fill="var(--text-dim)" font-size="11">${v}</text>`;
  }

  // X 轴标签，点太多时抽样
  let xLabels = "";
  const skip = Math.max(1, Math.ceil(labels.length / 10));
  labels.forEach((lab, i) => {
    if (i % skip !== 0 && i !== labels.length - 1) return;
    const x = pad.left + i * step;
    xLabels += `<text x="${x}" y="${H - 12}" text-anchor="middle" fill="var(--text-dim)" font-size="11">${esc(lab)}</text>`;
  });

  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = `
    ${gridLines}${yLabels}${xLabels}
    <path d="${pathD(ptsA)}" fill="none" stroke="var(--accent)" stroke-width="2.5"/>
    <path d="${pathD(ptsB)}" fill="none" stroke="var(--green)" stroke-width="2.5"/>
    ${ptsA.map(p => `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="3" fill="var(--accent)"/>`).join("")}
    ${ptsB.map(p => `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="3" fill="var(--green)"/>`).join("")}
  `;
}

function bindDataOverviewTabs() {
  document.querySelectorAll("#dataOverviewTabs .chart-tab").forEach(btn => {
    btn.onclick = () => {
      document.querySelectorAll("#dataOverviewTabs .chart-tab").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      DATA_OVERVIEW_SERIES = btn.dataset.series;
      clearDataOverviewDate();   // 切换到区间 tab 时退出"按日期查看"
      renderDataOverviewChart();
    };
  });
}

// 趋势曲线日期选择器：查询历史某天的小时级抓取 / 转发曲线
async function onDataOverviewDateChange() {
  const el = document.getElementById("dataOverviewDate");
  const d = el && el.value;
  if (!d) { clearDataOverviewDate(); return; }
  try {
    const r = await apiCall("get_data_overview_by_date", d);
    if (!r || !r.ok) { toast(r.error || "查询失败", "err"); return; }
    DATA_OVERVIEW_DATE = r.data;
    renderDataOverview();
    renderDataOverviewChart();
  } catch (e) { toast("按日期查询异常", "err"); }
}

function clearDataOverviewDate() {
  DATA_OVERVIEW_DATE = null;
  const el = document.getElementById("dataOverviewDate");
  if (el) el.value = "";
  renderDataOverview();
}

async function exportDataOverview(fmt) {
  const r = await apiCall("export_data_overview", fmt);
  if (r && r.ok) toast("已导出：" + r.path, "ok");
  else toast(r.error || "导出失败", "err");
}

/* ================= 【v1.2.0】数据路径设置 =================
   · 本机数据目录（授权相关）**固定不可改**，只读展示；
   · images / media 可自定义，改完点「保存路径设置」落盘即生效。 */
/* ==================================================================
   【v1.2.2】存储管理 / 首次启动向导 / 数据载入进度
   ------------------------------------------------------------------
   设计要点（对应用户需求「软件启动会未响应的原因我找到了，因为要扫描
   /data 和下级的位置，所以会卡」）：
     · 首次启动弹向导，让用户确认四个大目录的位置并写入 data_paths.json；
     · 之后每次启动只需读那一个小 JSON，**不再遍历任何目录**；
     · 占用统计一律走后台线程（后端 storage.py），前端每秒只读一个
       内存字典 —— 零磁盘 I/O，UI 线程永远不会被占住。
   ================================================================== */

let _DATA_PATHS = null;
let _STORAGE_TIMER = null;

/* 四个可配置目录的键 —— 与后端 paths.DIR_KEYS 一致 */
const _DIR_KEYS = ["image_dir", "media_dir", "models_dir", "history_dir"];
const _DIR_LABELS = {
  image_dir: "监控列表头像",
  media_dir: "相册 / 视频",
  models_dir: "本地 AI 模型",
  history_dir: "历史记录",
};

function _fmtBytes(n) {
  if (n === null || n === undefined || n < 0) return "—";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
  return (n / 1073741824).toFixed(2) + " GB";
}

/* 后端目录键是 snake_case（image_dir），而 HTML 里的元素 id 是 camelCase
   （imageDirInput）。这里统一走这个映射，避免两处各拼各的导致找不到元素。
   注意 image_dir去掉 "_dir" 后要补回 "Dir" 段，否则会拼成 imageInput。 */
function _dirDomId(key, suffix) {
  return key.replace(/_dir$/, "") + "Dir" + suffix;
  // image_dir + "Input" → imageDirInput；history_dir + "Usage" → historyDirUsage
}

function _storageMsg(text, kind) {
  const el = document.getElementById("storageMsg");
  if (!el) { if (text) toast(text, kind === "err" ? "err" : "ok"); return; }
  if (!text) { el.className = "path-msg"; el.textContent = ""; return; }
  el.className = "path-msg show " + (kind === "err" ? "err" : "ok");
  el.textContent = text;
}

/* 危险操作二次确认。本项目统一用原生 confirm()，不用自定义弹窗——
   原生对话框由浏览器直接阻塞在 UI 线程上，绝不会和后台扫描抢线程。 */
function confirmDialog(msg) {
  return window.confirm(msg);
}

function _setUsage(elId, u, label) {
  const el = document.getElementById(elId);
  if (!el) return;
  if (!u) { el.textContent = "—"; el.className = "path-usage"; return; }
  if (!u.exists) {
    el.textContent = label + "：目录尚未创建（首次使用时会自动建立）";
    el.className = "path-usage";
    return;
  }
  if (u.error) {
    el.textContent = label + "：统计失败（" + u.error + "）";
    el.className = "path-usage warn";
    return;
  }
  let s = label + "：" + u.files + " 个文件 · 占用 " + _fmtBytes(u.bytes);
  if (u.free_gb >= 0) s += " · 所在磁盘剩余 " + u.free_gb + " GB";
  el.textContent = s;
  el.className = "path-usage" + (u.free_gb >= 0 && u.free_gb < 2 ? " warn" : "");
}

/* ---------------- 存储设置 ---------------- */
async function loadDataPaths() {
  let st = null;
  if (STATE && STATE.data_paths && STATE.data_paths.data_dir) st = STATE.data_paths;
  else {
    const r = await apiCall("get_data_paths");
    st = r && r.ok ? r.settings : null;
  }
  if (!st) return;
  _DATA_PATHS = st;

  const dv = document.getElementById("dataDirVal");
  if (dv) { dv.textContent = st.data_dir || "—"; dv.title = st.data_dir || ""; }

  // 四个目录：只在用户没在输入时才覆盖，避免打字被打断
  _DIR_KEYS.forEach(k => {
    const inp = document.getElementById(_dirDomId(k, "Input"));
    const custom = st[k + "_custom"];
    if (inp && document.activeElement !== inp) {
      inp.value = custom ? st[k] : "";
      inp.placeholder = custom ? st[k] : "留空 = 使用默认目录";
    }
    const tag = document.getElementById(_dirDomId(k, "Tag"));
    if (tag) {
      tag.textContent = custom ? "自定义" : "默认";
      tag.className = "path-tag" + (custom ? " custom" : "");
    }
    // 默认路径写在 .path-default 上
    const def = inp && inp.closest(".path-item")
      ? inp.closest(".path-item").querySelector(".path-default") : null;
    if (def) def.textContent = "默认：" + (st[k + "_default"] || "—");
  });
}

async function saveStoragePaths() {
  _storageMsg("");
  const args = _DIR_KEYS.map(k => {
    const el = document.getElementById(_dirDomId(k, "Input"));
    return el ? el.value.trim() : "";
  });
  const r = await apiCall.apply(null, ["set_data_paths"].concat(args));
  if (r && r.ok) {
    const ch = (r.changed && r.changed.length) ? r.changed.join("；") : "无改动";
    let msg = "已保存（" + ch + "）。新产生的文件将写入新目录。";
    if (r.note) msg += " " + r.note;
    _storageMsg(msg, "ok");
    if (STATE) STATE.data_paths = r.settings;
    await loadDataPaths();
    refreshStorage(true);
    toast("存储路径已保存", "ok");
  } else {
    _storageMsg("保存失败：" + ((r && r.error) || "未知错误"), "err");
  }
}

async function resetStoragePath(which) {
  if (!_DIR_KEYS.includes(which)) return;
  const args = _DIR_KEYS.map(k => (k === which ? "" : null));
  const r = await apiCall.apply(null, ["set_data_paths"].concat(args));
  if (r && r.ok) {
    _storageMsg(_DIR_LABELS[which] + " 已恢复默认目录。", "ok");
    if (STATE) STATE.data_paths = r.settings;
    await loadDataPaths();
    refreshStorage(true);
  } else {
    _storageMsg("恢复失败：" + ((r && r.error) || "未知错误"), "err");
  }
}

async function pickStoragePath(which) {
  const r = await apiCall("pick_storage_dir", which);
  if (r && r.ok && r.path) {
    const el = document.getElementById(_dirDomId(which, "Input"));
    if (el) { el.value = r.path; el.focus(); }
    _storageMsg("已选择目录，点「保存路径设置」后生效。", "ok");
  } else if (r && !r.ok && r.error) {
    _storageMsg(r.error, "err");
  }
}

async function openStoragePath(which) {
  if (which === "data") {
    const r = await apiCall("open_data_dir", "data");
    if (r && r.ok) toast("已打开：" + r.path, "ok");
    else toast((r && r.error) || "打开失败", "err");
    return;
  }
  const r = await apiCall("open_storage_dir", which);
  if (r && r.ok) toast("已打开：" + r.path, "ok");
  else toast((r && r.error) || "打开失败", "err");
}

/* ---------------- 一键清理 ---------------- */
async function clearStorageDir(which, mode) {
  const label = _DIR_LABELS[which] || which;
  if (mode === "all") {
    const ok = await confirmDialog(
      "确认清空「" + label + "」目录的全部内容？\n\n" +
      "这会删除该目录下的**所有**文件，包括模型与数据库（历史记录目录除外，\n" +
      "它受保护无法整体清空）。删除后不可恢复，确定继续吗？");
    if (!ok) return;
  } else {
    const ok = await confirmDialog(
      "确认清理「" + label + "」的缓存？\n\n" +
      "只删除可重新下载的图片 / 视频，保留数据库、日志与模型。");
    if (!ok) return;
  }
  _storageMsg("正在清理…");
  const r = await apiCall("clear_storage_dir", which, mode);
  if (r && r.ok) {
    _storageMsg("已清理 " + r.removed_files + " 个文件，释放 " + r.freed +
                (r.kept ? "（保留 " + r.kept + " 个数据库 / 日志类文件）" : ""), "ok");
    toast("清理完成，释放 " + r.freed, "ok");
  } else {
    _storageMsg("清理失败：" + ((r && r.error) || "未知错误"), "err");
    toast("清理失败", "err");
  }
  refreshStorage(true);
}

/* ---------------- 占用扫描（轮询内存状态，零磁盘 I/O） ---------------- */
async function refreshStorage(force) {
  const r = await apiCall("start_storage_scan", !!force);
  if (!r) return;
  applyStorageStatus(r.status);
  // 扫描中才轮询；完成即停，避免长期空转
  _ensureStoragePoll();
}

function _ensureStoragePoll() {
  if (_STORAGE_TIMER) return;
  _STORAGE_TIMER = setInterval(async () => {
    const r = await apiCall("get_storage_status");
    if (!r || !r.ok) { clearInterval(_STORAGE_TIMER); _STORAGE_TIMER = null; return; }
    applyStorageStatus(r.status);
    if (!r.status.running) {
      clearInterval(_STORAGE_TIMER);
      _STORAGE_TIMER = null;
    }
  }, 600);
}

function applyStorageStatus(st) {
  if (!st) return;

  /* 概览页的「数据载入进度」卡 */
  const card = document.getElementById("loadProgressCard");
  if (card) {
    if (st.running) {
      card.style.display = "block";
      const pct = Math.max(0, Math.min(100, st.pct || 0));
      const bar = document.getElementById("loadBar");
      const pt = document.getElementById("loadPct");
      const ph = document.getElementById("loadPhaseText");
      const su = document.getElementById("loadSummary");
      if (bar) bar.style.width = pct + "%";
      if (pt) pt.textContent = pct + "%";
      if (ph) ph.textContent = (st.current ? "正在统计：" + st.current : "正在统计…");
      if (su) su.textContent = "已统计 " + st.files + " 个文件 · " + _fmtBytes(st.bytes || 0);
    } else if (st.phase === "done") {
      card.style.display = "none";
    }
  }

  /* 存储管理页的进度条与汇总 */
  const bar = document.getElementById("storageBar");
  const pct = Math.max(0, Math.min(100, st.pct || 0));
  if (bar) bar.style.width = pct + "%";
  const pe = document.getElementById("storagePct");
  if (pe) pe.textContent = pct + "%";
  const ph = document.getElementById("storageScanPhase");
  if (ph) {
    ph.textContent = st.running
      ? "正在统计" + (st.current ? "：" + st.current : "…")
      : (st.phase === "done" ? "统计完成" :
         st.phase === "cancelled" ? "已取消" :
         st.phase === "error" ? "统计出错" : "尚未统计");
  }
  const sm = document.getElementById("storageSummary");
  if (sm) {
    sm.textContent = st.phase === "done"
      ? ("四个目录合计 " + st.files + " 个文件 · 占用 " + _fmtBytes(st.bytes || 0))
      : (st.running ? "后台统计中，可继续使用软件…" : "尚未统计");
  }

  /* 每行的占用文案 */
  if (st.dirs) {
    st.dirs.forEach(d => {
      _setUsage(_dirDomId(d.key, "Usage"), d, d.name);
    });
  }
}

/* ==================================================================
   【v1.2.2】首次启动向导
   ================================================================== */
let _WIZARD_DIRS = [];

async function checkWizard() {
  let w = null;
  if (STATE && STATE.wizard && STATE.wizard.dirs) w = STATE.wizard;
  else {
    const r = await apiCall("get_wizard_state");
    w = r && r.ok ? r.wizard : null;
  }
  if (!w || w.done) return false;
  showWizard(w);
  return true;
}

function showWizard(w) {
  _WIZARD_DIRS = w.dirs || [];
  const dd = document.getElementById("wizardDataDir");
  if (dd) dd.textContent = w.data_dir || "—";

  const box = document.getElementById("wizardDirs");
  if (!box) return;
  box.innerHTML = "";
  _WIZARD_DIRS.forEach(d => {
    const row = document.createElement("div");
    row.className = "wizard-dir-row";
    row.innerHTML =
      '<div class="wizard-dir-head">' +
        "<b>" + esc(d.name) + "（" + esc(d.en) + "）</b>" +
        '<span class="muted">' + esc(d.desc || "") + "</span>" +
      "</div>" +
      '<div class="wizard-dir-input-row">' +
        '<input type="text" class="path-input" id="wz_' + d.key + '" spellcheck="false" ' +
          'value="' + esc(d.value || "") + '" placeholder="留空 = 使用默认位置" />' +
        '<button class="ghost" onclick="pickWizardDir(\'' + d.key + '\')">浏览…</button>' +
        '<button class="ghost" onclick="resetWizardDir(\'' + d.key + '\')">默认</button>' +
      "</div>";
    box.appendChild(row);
  });

  const mask = document.getElementById("wizardMask");
  if (mask) mask.style.display = "flex";
}

async function pickWizardDir(key) {
  const r = await apiCall("pick_storage_dir", key);
  if (r && r.ok && r.path) {
    const el = document.getElementById("wz_" + key);
    if (el) el.value = r.path;
  } else if (r && !r.ok && r.error) {
    toast(r.error, "err");
  }
}

function resetWizardDir(key) {
  const el = document.getElementById("wz_" + key);
  if (!el) return;
  const d = _WIZARD_DIRS.find(x => x.key === key);
  el.value = d ? (d.default || "") : "";
}

async function finishWizard(useDefault) {
  const args = _DIR_KEYS.map(k => {
    if (useDefault) return "";
    const el = document.getElementById("wz_" + k);
    return el ? el.value.trim() : "";
  });
  const r = await apiCall.apply(null, ["finish_wizard"].concat(args));
  const mask = document.getElementById("wizardMask");
  if (mask) mask.style.display = "none";
  if (r && r.ok) {
    if (STATE) {
      STATE.wizard = r.wizard;
      STATE.data_paths = r.settings;
    }
    toast(useDefault ? "已使用默认目录位置" : "存储位置已保存", "ok");
    applyStorageStatus(r.status);
    refreshStorage(true);
  } else {
    toast("保存失败：" + ((r && r.error) || "未知错误"), "err");
    // 失败就把向导重新显示出来，别让用户困在遮罩后面
    if (r && r.wizard) showWizard(r.wizard);
    else if (mask) mask.style.display = "flex";
  }
}

/* ==================================================================
   【v1.2.2】转发设置子标签页（工作时间 / 关键词黑名单）
   ================================================================== */
function switchForwardTab(which) {
  document.querySelectorAll(".fwd-subtab").forEach(b => {
    b.classList.toggle("active", b.dataset.subtab === which);
  });
  document.querySelectorAll(".fwd-subpane").forEach(p => {
    p.classList.toggle("active", p.id === "fwdpane-" + which);
  });
  if (which === "hours") updateWhBar();
}


/* ---------------- 日志 ---------------- */
let _logKeywordTimer = null;
function onLogKeywordInput() {
  clearTimeout(_logKeywordTimer);
  _logKeywordTimer = setTimeout(loadLogs, 350);   // 输入防抖
}

function logLineHtml(r) {
  const cls = { DEBUG: "log-d", INFO: "log-i", WARN: "log-w", ERROR: "log-e" }[r.level] || "log-i";
  return `<div class="log-line ${cls}">`
    + `<span class="log-time">[${esc(r.created_at)}]</span> `
    + `<span class="log-level">[${esc(r.level)}]</span> `
    + `<span class="log-msg">${esc(r.message)}</span></div>`;
}

async function loadLogs() {
  const lv = document.getElementById("logLevel");
  const cat = document.getElementById("logCategory");
  const kw = document.getElementById("logKeyword");
  const level = lv ? lv.value : "ALL";
  const category = cat ? cat.value : "ALL";
  const keyword = kw ? kw.value.trim() : "";

  const res = await apiCall("get_logs", 500, 0, level, category, keyword);
  // 兼容后端返回 {rows,total} 或纯数组两种形态
  const rows = (res && res.rows) ? res.rows : (Array.isArray(res) ? res : []);
  const total = (res && typeof res.total === "number") ? res.total : rows.length;

  const box = document.getElementById("logView");
  box.innerHTML = rows.length
    ? rows.map(logLineHtml).reverse().join("")
    : `<div class="log-line log-i"><span class="log-msg">没有符合条件的日志</span></div>`;

  const stat = document.getElementById("logStat");
  if (stat) stat.textContent = `共 ${total} 条 · 当前显示 ${rows.length} 条`;
  box.scrollTop = box.scrollHeight;   // 新问题在底部（按 id 倒序后已反转）
}

async function onLogVerboseChange() {
  const el = document.getElementById("logVerbose");
  const on = el ? el.checked : false;
  const res = await apiCall("set_log_verbose", on);
  if (res && res.ok) toast(`冗余调试日志已${on ? "开启" : "关闭"}`);
  else if (el) el.checked = !on;
}

async function clearAllLogs() {
  if (!confirm("确定清空全部运行日志？此操作不可恢复。")) return;
  const res = await apiCall("clear_logs");
  if (res && res.ok) { toast("运行日志已清空"); loadLogs(); }
  else toast("清空失败：" + ((res && res.error) || "未知错误"));
}

async function exportLogs(fmt) {
  const res = await apiCall("export_logs", fmt);
  if (res && res.ok) toast("已导出：" + res.path);
  else if (res && res.error && !/取消/.test(res.error)) toast("导出失败：" + res.error);
}

function clearLogView() { document.getElementById("logView").innerHTML = ""; }

/* ---------------- 关于 ---------------- */
function renderAbout() {
  document.getElementById("aboutCard").innerHTML = `
    <div class="about-hero card">
      <!-- 【v1.4.0】关于页 Logo 也接入视角跟随：底版 + 眼珠图层（与侧栏同源） -->
      <div class="about-logo" id="aboutLogo">
        <img src="logo-base.png" alt="logo">
        <img src="logo-eye.png" class="brand-logo-eye" alt="" aria-hidden="true">
      </div>
      <div class="about-hero-info">
        <div class="about-name">${esc(STATE.app)}</div>
        <div class="about-version">${esc(STATE.version.display)}</div>
        <div class="about-copy">${esc(STATE.copyright)}</div>
      </div>
      <button class="ghost about-log-btn" onclick="openChangelog()">查看更新详情</button>
    </div>

    <div class="about-grid">
      <div class="card about-tile">
        <div class="about-tile-icon">🛡️</div>
        <div class="about-tile-title">本地运行</div>
        <div class="about-tile-desc">所有数据均保存在本地，不上传云端。</div>
      </div>
      <div class="card about-tile">
        <div class="about-tile-icon">🔒</div>
        <div class="about-tile-title">Cookie 加密</div>
        <div class="about-tile-desc">账户 Cookie 本地加密存储，请勿泄露。</div>
      </div>
      <div class="card about-tile">
        <div class="about-tile-icon">🤖</div>
        <div class="about-tile-title">离线 AI</div>
        <div class="about-tile-desc">内置轻量内容生成引擎，无需联网大模型。</div>
      </div>
      <div class="card about-tile">
        <div class="about-tile-icon">📖</div>
        <div class="about-tile-title">开源软件</div>
        <div class="about-tile-desc">未经授权禁止商用。更新日志与源码见 README。</div>
      </div>
    </div>

    <div class="card about-social" data-page-node-id="aboutSocial">
      <div class="about-social-title">关注作者 · 联系交流</div>
      <div class="about-social-grid">
        <a class="about-social-item" href="javascript:void(0)" onclick="openExternal('https://github.com/zouzuo1994321')" title="https://github.com/zouzuo1994321">
          <img class="about-social-ico" src="icon-github.png" alt="GitHub">
          <span class="about-social-name">GitHub</span>
          <span class="about-social-val">zouzuo1994321</span>
        </a>
        <a class="about-social-item" href="javascript:void(0)" onclick="openExternal('https://space.bilibili.com/13715')" title="https://space.bilibili.com/13715">
          <img class="about-social-ico" src="icon-bilibili.png" alt="Bilibili">
          <span class="about-social-name">哔哩哔哩</span>
          <span class="about-social-val">space.bilibili.com/13715</span>
        </a>
        <a class="about-social-item" href="javascript:void(0)" onclick="openExternal('https://weibo.com/u/5189652182')" title="https://weibo.com/u/5189652182">
          <img class="about-social-ico" src="icon-weibo.png" alt="微博">
          <span class="about-social-name">微博</span>
          <span class="about-social-val">weibo.com/u/5189652182</span>
        </a>
        <a class="about-social-item" href="javascript:void(0)" onclick="copyText('921103025')" title="点击复制邮箱">
          <svg class="about-social-ico about-social-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
            <rect x="2" y="4" width="20" height="16" rx="3"/><path d="M2.5 7 L12 13 L21.5 7"/>
          </svg>
          <span class="about-social-name">邮箱</span>
          <span class="about-social-val">921103025</span>
        </a>
      </div>
    </div>

    <div class="card about-notice">
      <div class="notice-col">
        <h4>使用须知</h4>
        <ul>
          <li>转发接口基于微博公开接口，请遵守平台规则，合理设置轮询间隔。</li>
          <li>本软件仅供学习与个人合法使用，使用者须自行承担使用后果。</li>
          <li>作者不对滥用行为、账号封禁或数据丢失承担责任。</li>
        </ul>
      </div>
      <div class="disclaimer-col">
        <h4>免责声明</h4>
        <p>本软件按「现状」提供，不作任何明示或暗示的担保。使用者须确保其行为符合当地法律法规及微博平台规则。因使用或无法使用本软件导致的任何损失，作者概不负责。</p>
      </div>
    </div>
  `;
  if (window.__registerLogoGaze) window.__registerLogoGaze(document.getElementById('aboutLogo'));
  renderChangelogModal();
}

/* ---------------- 版本更新详情（弹窗） ----------------
   数据内嵌于前端，保证离线可用（本软件为单机离线运行）。
   与 README.md「版本迭代记录」保持同步维护。 */
const CHANGELOG = [
  {
    ver: "v1.5.1", internal: "2610140005", date: "2026-10-14",
    title: "修复转发卡进度环生长期高光 + 轮询进度条间歇不显示（两个偶发 BUG）",
    items: [
      "【BUG1·进度环生长期高光】本日转发次数卡的进度环从启动生长到满环的整段过程，卡片外框一直带着橙色高光（border-color + box-shadow），像「出错了」一样干扰阅读。现生长期（fwd-active）只亮进度环本身，外框高光仅保留给到达排程转发时刻后的满环驻留期（fwd-done）。",
      "【BUG2·轮询进度条间歇不显示】stub-DOM 时序复现定位到竞态：上一轮轮询完成时挂了一个无句柄的「1.8 秒后隐藏进度条」定时器，若在 1.8 秒内再次点击「立即轮询一次」，旧定时器会在新一轮轮询进行中把整条进度条误藏——快速连续轮询时必现、单次操作不出现，正对应「有时候不显示」。现隐藏定时器改用句柄管理，每轮进入时先清除遗留定时器；同时完成时进度条直达满格（跳过 0%→100% 的 0.25s 宽度过渡），修掉「轮询极快完成时条还贴在 0% 处、文案却已跳完成」的观感。无人干预时的 1.8s 自动收起行为保留不变。"
    ]
  },
  {
    ver: "v1.5.0", internal: "2610140004", date: "2026-10-14",
    title: "启动可靠性加固：单实例互斥 + 启动诊断日志 + 根治退出拖尾 + 开屏卡死修复",
    items: [
      "【单实例互斥】关窗后进程要拖约 12.5 秒才真正退出（WebView2 拆环境），这段时间再开新实例可能撞资源竞争导致白屏卡死。现在用命名互斥体兜底：上一个实例还在退出时，新实例明确弹窗提示「请稍候」，不再静默叠一个打不开的窗口。",
      "【根治退出拖尾】窗口关闭（含托盘「退出」）后立即停托盘、落库收尾并硬退出，进程即关即走，不再拖 12.5 秒——彻底消除「半死实例占资源」的高发窗口。SQLite 与配置写盘均为崩溃安全设计，硬退无副作用。",
      "【启动诊断日志】启动全链路（互斥→目录→迁移→CA→窗口→WebView→前端各阶段→进入主界面→退出）分阶段写入数据目录 diag.log，毫秒级时间戳；此后再出现「打不开/未响应」，日志可直接定位卡在哪一步。",
      "【删除页码跳页控件】监控列表与历史记录分页栏的「页码输入框 + GO 按钮」整体移除，翻页只保留上一页/下一页与每页条数。",
      "【修复开屏卡死（4% 不动）】旧启动触发 `if (window.pywebview) boot()` 会在 pywebview 桥的 `window.pywebview` 已挂载、但 `.api` 尚未就绪的竞态下抢跑，导致 init 拿到 undefined 的 API，其后所有 api 调用（含 boot_stage 埋点）被静默吞掉——表现为窗口出现、开屏永远停在 4%、诊断日志毫无前端阶段。改为：仅在 `window.pywebview.api` 真正就绪时才 boot，并加 pywebviewready 监听 + 250ms 轮询兜底（至多 8s），彻底消除该静默失败。",
      "【20 轮严谨启动测试】测试判定标准升级：不再只看「窗口出现」，必须等到诊断日志出现 UI_READY（开屏淡出、真正进入主界面）才算成功，并用未响应检测盯住开屏→主界面区间。",
      "【修复「一进软件就未响应」·五层根因（2610140004 补充修复）】经 py-spy 实时线程栈 + WebView2 ProcessFailed 崩溃转储逐层定位：① pywebview 注入期 `get_functions()` 递归遍历 js_api 属性，走进 pywebview Window 的 .native（WinForms 控件），每次 getattr 跨 COM 边界切回 UI 线程，成百上千次把消息循环拖死（线程栈实测卡在 get_functions）——改为只收集 api 顶层方法、不再向下递归；② on_webview_ready 与 on_navigation_completed 双路并发注入互相竞争（线程栈实测两个 generate_js_object）——统一走带去重的单次注入；③ 固定 2.5s 延迟注入时页面文档尚未提交，桥脚本被写进导航前空文档随后被覆盖（实测 window.location.href 求值落空）——改为轮询 document.readyState 至真实文档加载完成再注入，注入后校验 window.pywebview.api 确已挂载、失败自动重试；④ UI 改走 file:// 直载，不再经过本地 HTTP 服务，彻底避开系统代理对回环请求的干扰（本应用数据全走 pywebview 桥，前端无 fetch/XHR，无副作用）；⑤ 第三方全局钩子 DLL（WPS 输入法 qingnse64.dll 等）注入致 WebView2 GPU/工具进程崩溃、CoreWebView2 失效——附加 --disable-gpu --no-sandbox 浏览器参数绕开崩溃链，并对 clear_user_data 增加 CoreWebView2 空值守卫。修复后源码直跑实测：桥 3 秒内挂载、BOOT_STAGE 15%→100%、UI_READY、窗口全程响应、监控数据实时刷新。"
    ]
  },
  {
    ver: "v1.4.22", internal: "2610130017", date: "2026-10-13",
    title: "满环高亮渐隐后彻底熄灭（修复熄灭后立即复燃）",
    items: [
      "【现象】满环驻留 10 秒、1.5 秒渐隐结束后，卡片高亮随即又重新亮起（满环状态一直挂着），直到下一次轮询才恢复——预期应是熄灭后保持熄灭，等新周期重新走进度时再点亮。",
      "【根因】渐隐由 fwd-fading 的 opacity:0 实现，渐隐结束的定时器只移除了 fwd-done / fwd-fading、留下了 fwd-active；而此时环的 dashoffset 仍是满环时的 0，于是「fwd-active + 满环」立即复燃。",
      "【修复】渐隐结束后连 fwd-active 一起清掉并把环清空回空环状态（带守卫：若此刻已进入新周期则跳过，避免误杀正在生长的进度环）；环保持熄灭，直到 next_run 推进、新周期重新走进度时再点亮。"
    ]
  },
  {
    ver: "v1.4.21", internal: "2610130016", date: "2026-10-13",
    title: "修复进度环仍对不齐 100%（next_forward_run 未透传）",
    items: [
      "【根因】run_once 把 _forward_one 的返回值合并进按 UID 聚合的结果项时只取了 action / detail 两个字段，排程转发的 run_at（实际执行时刻）被丢弃——scheduler 永远捕不到它，next_forward_run 恒为空，前端进度环一直回退到「终点 = 下一次轮询 next_run」，表现为 22:39:09 到点环只有约 12%（正好等于按 15 分钟回退周期算出的比例）。",
      "【修复】run_once 现在把 run_at 一并透传进对应 UID 的结果项；scheduler._capture_scheduled_run_at 从结果里提取写入 next_forward_run，前端进度环起点 = last_run、终点 = 真实转发时刻，匀速推进并在排程时刻（如 22:39:09）正好满环 + 高亮驻留 10 秒。"
    ]
  },
  {
    ver: "v1.4.20", internal: "2610130015", date: "2026-10-13",
    title: "进度环周期对齐真实转发时刻（修复『特别慢 / 到点不到 100%』）",
    items: [
      "【进度环终点对齐】旧逻辑以 next_run（下一次轮询）作为 100% 目标，导致进度环要等到下一次轮询才开始重算，表现为『特别慢』且真实转发时刻到不了 100%。现改为以本次排程的实际转发时刻 next_forward_run（含抖动）作为满环目标，未排程时回退 next_run。",
      "【进度环起点修正】旧逻辑用 next_run - interval 反推周期起点，把约 12 分钟的爬取耗时错算进周期开头，导致开局即偏高、推进缓慢。现以 last_run（上一次轮询真实完成时刻）作为周期起点，进度随时间匀速推进，在真实转发时刻（如 22:10:14）达到 100%。",
      "【满环高亮驻留 10s】到达 100% 后高亮绕满一圈并驻留 10 秒（原 3 秒），若驻留期间 next_run 推进（新周期开始）立即重新生长，否则 10 秒后 1.5 秒渐隐。",
      "【后端联动】scheduler 新增 next_forward_run 字段（在 run_once 捕获 _forward_one 回传的 run_at 并写入 status）；forward_engine 的 scheduled 分支回传 run_at 时间戳，供前端对齐满环。"
    ]
  },
  {
    ver: "v1.4.19", internal: "2610130014", date: "2026-10-13",
    title: "进度环起点修正 + 排程时刻满环 + 历史库持久化修复",
    items: [
      "【进度环起点修正】v1.4.18 的 rotate(-90) 作用在非正方形卡片上会把整个环转出卡片外（宽高互换）。改为手写 SVG path：路径起点即顶部 12 点方向、顺时针闭合，进度从正上方沿卡片四边生长。",
      "【排程时刻满环】到达排程转发时间（进度 100%）即触发「满环 + 高亮驻留 3s + 渐隐」，不再等下一周期 next_run 推进；每个周期只触发一次，新周期自动重新生长。",
      "【历史库持久化修复（重要）】修复 history_db 连接误用空路径参数导致每次启动都落在「关闭即删」的临时库上的问题——这正是重启后去重失效、重复抓取、本日抓取/转发统计无法保留的根因。修复后数据正式写入 history.db 并跨重启保留。"
    ]
  },
  {
    ver: "v1.4.18", internal: "2610130013", date: "2026-10-13",
    title: "系统概览动效 + 秒级刷新",
    items: [
      "【本日转发次数卡进度环】「系统概览 → 本日转发次数」卡片沿外框新增高亮进度环：调度运行中，环沿卡片四边生长，显示当前距离下一次排程转发的剩余进度；一个轮询周期结束（next_run 推进）时环绕满一圈、高亮驻留 3 秒，再 1.5 秒渐隐，随后进入下一周期重新生长。调度未运行时环自动隐藏。四张统计卡改为静态结构、只更新数字，避免整块重建把进度环反复销毁、丢失动画状态。",
      "【系统概览秒级刷新】概览轮询从 5 秒改为 1 秒（仅概览页激活且窗口可见时拉取，后端状态缓存优先）。",
      "【账户管理秒级刷新】账户管理页新增独立秒级轮询（仅该页激活时每秒拉取 get_state 并重绘账户表格），让状态/检测时间/Cookie 有效期等近实时更新；检测/续期/增删账户操作进行期间自动避让，不重绘打断。"
    ]
  },
  {
    ver: "v1.4.17", internal: "2610130012", date: "2026-10-13",
    title: "媒体检索回退查库 + 备份/导入索引 + 卡片最新图预览",
    items: [
      "【媒体检索回退为直接查库】上一版（v1.4.16）让「相册监控 → 媒体检索」优先读索引文件，实测体验不如旧版（列表要等后台建索引才准、切换页面时数据偶有滞后）。现整体回退：统计 / 对象列表 / 关键词搜索一律**直接查数据库**，与旧版行为一致；索引文件不再参与前台读取。",
      "【按钮改为「备份索引」/「导入索引」】原「重建索引」「显示全部」取消。**「备份索引」**——按当前台账生成一份索引 JSON，弹系统保存对话框另存到你指定位置（默认文件名带时间戳）；**「导入索引」**——选一个索引 JSON 读入，把其中的台账记录**回灌到数据库**（按 URL + 类型去重，已存在的自动跳过），适合换机器 / 重装 / 恢复时把记录带回来。",
      "【卡片右侧显示最新图片预览】对象卡片改为左右两栏：左侧昵称 / UID / 张数 / 占用 / 打开目录，右侧显示该对象**最新一张图片**的缩略图（104×104，居中裁切）。为保证打开速度，接口分两步——先只取「最新图信息」（不传图片数据），前端再按需拉取缩略图；无图时显示占位符「—」。",
      "【取消点击卡片展开相册】原「点击对象卡片可展开查看其相册图片」功能整体移除 —— 一次展开要取几十张图的 base64，对软件内存与硬盘读取压力过大。查看某个对象的全部图片请点「打开目录」在资源管理器中浏览。"
    ]
  },
  {
    ver: "v1.4.16", internal: "2610130011", date: "2026-10-13",
    title: "媒体库索引文件 + 台账误删修复",
    items: [
      "【媒体目录下新增索引文件】「相册监控 → 媒体检索」以前每次都直接对 `media_files` 整表聚合查询，重开软件后列表常为空、且每次进页面都要重新检索一遍。现改在媒体目录下维护独立索引文件 `<media_dir>/media_index.json`（含全库汇总、各对象图片/视频数与占用、完整文件清单 + 生成时间），查页面优先读索引，毫秒级出结果并跨会话复用。索引跟着媒体目录走，用户换目录 / 整体拷走媒体库时一并带走。",
      "【修复媒体台账被误清空】v1.4.7 的媒体自愈对账（启动后台跑 `reconcile_media_files()`）判定条件是「DB 有记录 **且** 磁盘文件存在」。当 DB 从旧目录迁移过来、而媒体文件没跟着搬时，整表 29084 行会被判为僵尸行**全部删除** —— 相册列表于是永远是空的，每次开机自愈都把台账再清一遍。现加两道闸：媒体目录不存在 / 整体为空时直接跳过对账；待删比例超过 50% 时判定为「文件整体不在位」而非「零散删除」，拒绝执行并记警告。正常的零星自愈不受影响。",
      "【索引缺失自动重建】索引文件不存在 / JSON 损坏 / 版本结构不符时，前台自动回退实时查库并触发一次后台重建；启动时也会校验一次索引。写入采用原子替换（先写 .tmp 再 os.replace），避免断电或被杀进程留下半截文件。",
      "【下载完成自动回写索引】媒体下载落盘结束后在后台线程重建索引（带 1.5s 合并抖动，短时间多次触发只跑最后一次），绝不阻塞下载主流程与界面。",
      "【界面新增索引状态】「媒体检索」页新增状态提示条（绿点=已启用索引并显示生成时间 / 黄点=尚未生成、正在建立）与「重建索引」按钮，对象卡片补显「占用 xx MB」。"
    ]
  },
  {
    ver: "v1.4.15", internal: "2610130010", date: "2026-10-13",
    title: "子项悬停底色同步缩进 22px",
    items: [
      "【子项悬停底色同步缩进 22px】上一版把子项「选中」高亮（滑块 + 竖条）收进了 22px，但「悬停」底色仍来自基础规则 `.nav-item:hover` 的 `background-color`，从侧栏最左 x=0 铺满整行 —— 于是悬停时比选中态多出 22px，两者左边界对不齐。现让子项的悬停底色也改用 `background-image` 横向渐变绘制：0→22px 透明、22px 之后才是琥珀色，左缘与滑块 / 竖条 / 缩进导轨线完全对齐。",
      "【与导轨线叠为两层背景】子项原本用 `background-image` 画缩进导轨线，故悬停底色与导轨线叠成两层（逗号分隔，先写的在上层）：第 1 层是悬停底色横向渐变，第 2 层是 22px 处的 1px 导轨线；未悬停时第 1 层为全透明（由 `--sub-hover` 变量控色），视觉上等同不存在。同时显式给子项悬停补 `background-color: transparent`，避免基类规则把整行刷上底色。",
      "【悬停 / 选中 / 未选中共用同一条左边界】三种状态下子项底色的左边界统一在 22px，鼠标移入移出时只有颜色渐显渐隐，左边界纹丝不动，不再出现「悬停时整行亮、选中时只剩右侧亮」的跳变。"
    ]
  },
  {
    ver: "v1.4.14", internal: "2610130009", date: "2026-10-13",
    title: "子项高亮底块左边界同步右移 22px",
    items: [
      "【子项高亮底块收进 22px】上一版把子项的橙色竖条移到了 22px（与缩进导轨线对齐），但高亮底块仍从侧栏最左 x=0 铺满整行 —— 结果竖条左侧多出一小块橙色，看起来像粘了块色斑。现把底块左边界也收到 22px：`#navHighlight` 的 `left` 改为跟随 `--bar-x`（子项 22px、顶级项 0），底块与竖条一起右移，左缘与竖条严丝合缝。",
      "【底块随行类型平滑横移】`left` 也加入过渡（0.3s 缓动），切换子项 ↔ 顶级项时底块左边界与竖条一起平滑横移，不会生硬跳变；顶级项 / 分组标题仍从 x=0 铺满整行，保持原有视觉。"
    ]
  },
  {
    ver: "v1.4.13", internal: "2610130008", date: "2026-10-13",
    title: "子项高亮竖条移到缩进导轨线位置",
    items: [
      "【子项竖条位置对齐缩进导轨线】上一版滑块统一把竖条画在侧栏最左边缘（x=0），与子项的缩进导轨线（22px）错开了两处竖线，看起来像两条平行的线各说各话。现按需求把子项的竖条移到 22px —— 与「存储管理 / 历史记录 / 数据总览 / 运行日志」等子项的缩进导轨线落在同一竖列，竖条与导轨线合而为一，视觉上更整齐。",
      "【顶级项与分组标题竖条仍在最左侧】「系统概览 / 软件授权 / 关于软件」与各分组标题没有缩进导轨线，如果竖条也移到 22px 会显得凭空悬在中间；因此这三类行选中时竖条仍停在 x=0，各类行的竖条各归其位。",
      "【竖条改用背景渐变绘制，位置平滑过渡】竖条不再用 border-left 提供（border 的位置固定在元素左缘、且会占进内容盒），改为用 background-image 渐变画在滑块内部的指定 x 处（background-size: 3px 100%），位置由 CSS 变量控制。这样切换子项 ↔ 顶级项时，竖条还能顺带做一次横向平移（0.3s 缓动），与滑块的纵向滑动同步，不会突兀闪现。"
    ]
  },
  {
    ver: "v1.4.12", internal: "2610130007", date: "2026-10-13",
    title: "侧栏高亮改为滑动滑块 + 网络速率标签贴合数值",
    items: [
      "【侧栏选中高亮改为「滑动滑块」】此前每个导航行各自画自己的选中底色，切换时是「旧块淡出、新块淡入」两段独立动画，视觉上像两个光斑各自闪一下。现改为整栏共用一个绝对定位的高亮块（#navHighlight）：切换时高亮块整体从旧位置平滑滑到新位置（transform: translateY，0.3s cubic-bezier 缓动），移动过程连续可见，切换手感更顺滑。",
      "【高亮块形态按需求对齐】底块从侧栏最左 x=0 铺满整行，橙色 3px 竖条压在高亮块最左边缘上（与底块同一元素）—— 即竖条随高亮块一起滑动，切换时不会出现「底色先到、竖条后到」的割裂感。选中行自身只负责文字变橙，不再叠加第二层底色，避免两处高亮叠加导致颜色加深。",
      "【高亮块与目标行精确对齐】滑块的高度由目标行的实际高度决定（分组标题行 38px、普通行 35px 各自适配），位置取目标行相对侧栏内容盒的偏移；侧栏滚动、窗口缩放、分组展开动画结束后都会重新贴合，避免高亮块与文字错位。首次进入时直接落位不播动画（不会从顶部滑下来）。",
      "【网络速率数值不再与标签隔开】上一版把网络值锁成 210px 死宽 + 右对齐，而「↑ 106.5 KB/s / ↓ 22.6 KB/s」实测仅约 125px 宽，多出的 85px 全变成「网络」标签与数值之间的空白。现收窄为 175px（恰好覆盖「↑ 1023.9 MB/s / ↓ 1023.9 MB/s」的极限情况）并改左对齐，标签紧邻数值。保持定宽的原因：若改成随内容伸缩，数值位数变化时会把右侧 GPU 段推开（实测有 53px 位移），与「界面不乱动」相冲突；定宽 + 左对齐可同时满足「贴合」与「不位移」。"
    ]
  },
  {
    ver: "v1.4.11", internal: "2610130006", date: "2026-10-13",
    title: "系统负载行位置硬锁定 + 子项高亮竖条恢复与平滑过渡",
    items: [
      "【系统负载整行位置硬锁定】之前 CPU / GPU 的百分比与温度之间用 gap 排布，数值宽度天然浮动（7.8% ↔ 100%、KB/s ↔ MB/s、49°C ↔ 100°C），任一数值一变就把右侧所有元素推走，整行来回抖。现改为全段定宽硬锁：每个数值位（CPU 44px、内存 46px、GPU 44px、网络 210px、温度 46px）设固定宽度 + 右对齐，数字位数变化只在槽内发生；各段容器统一 flex: 0 0 auto，任意数值刷新时所有元素位置零位移（实测各段 offsetLeft 变化量恒为 0），界面不会再乱动。",
      "【子项恢复左侧高亮竖条】按新要求把侧栏子项选中时的 3px 橙色竖条加回来。竖条由 border-left 提供而非伪元素 —— 宽度恒定 3px，只变色不改变盒模型，切换时元素不会位移；同时选中态隐藏子项原有的缩进导轨线，避免左缘叠出双线。",
      "【子项切换过渡更丝滑】子项的底色、字色、竖条颜色统一改走 0.28s ease 缓动（悬停态用 0.18s 更快跟手），切换子项时高亮块平滑移动、颜色渐变过去，不再生硬跳变。"
    ]
  },
  {
    ver: "v1.4.10", internal: "2610130005", date: "2026-10-13",
    title: "修复上下按钮未隐藏、子项竖条未去掉、温度未锁宽",
    items: [
      "【修复「上下按钮没有删除」】根因：隐藏原生 spin 按钮的 CSS 规则写成了 .num-wrap > input[type=number]，而取消步进的输入框（.no-step）根本不会生成 .num-wrap 包裹层 —— 规则完全不命中，WebView2 的原生上下箭头依旧显示。现补全局兜底规则 input[type=number]（含 ::-webkit-inner-spin-button / ::-webkit-outer-spin-button 与 appearance: textfield），所有数字框一律不显示原生步进。",
      "【修复「子项高亮竖条没去掉」】根因：竖条来自父级规则 .nav-item / .nav-item.active 的 border-left: 3px，子项同时带 .nav-item 类因此一并继承；上一版去删 .nav-item.sub 里本来就不存在的 border-left，等于改错了地方。现改为在 .nav-item.sub / .nav-item.sub.active 上显式 border-left-color: transparent，顶级项与分组标题的竖条保留。",
      "【修复「温度没有锁定宽度」】上一版只设了 min-width: 44px，而 min-width 只保下限 —— 数值由「—」变「27.9°C」仍会把元素撑开，整行随之抖动。现改用 width: 46px + flex: 0 0 46px 硬锁宽度，右对齐。",
      "【清除授权按钮样式统一】「清除授权」由 ghost danger（描边）改为与「清空日志」一致的实心 danger 按钮，两处均为红底白字。"
    ]
  },
  {
    ver: "v1.4.9", internal: "2610130004", date: "2026-10-13",
    title: "开屏加载进度可视化 + 输入框/侧栏/温度细节修正",
    items: [
      "【开屏显示加载进度百分比与当前步骤】开屏动画在进度条下方新增「百分比 + 当前正在加载的内容」两行信息，每到一个关键节点就更新一次：界面骨架就绪 → 连接后端 → 检测账户状态 → 读取主状态（账户/监控/转发配置）→ 渲染配置 → 扫描存储目录 → 后端仍在构建状态（附第 N 次重试）。冷启动涉及 onefile 解压、数据库连接、后台状态重建等多个环节，此前只有一个静态的「正在加载数据…」，卡住时无法判断卡在哪一步；现在可以一眼看出是前端没在等、还是后端迟迟没回。进度条同步改为「确定进度」模式（主体长度真实反映百分比，保留一道来回微光表示进程仍活着）。",
      "【分页与间隔输入框取消上下步进按钮，只保留手工填入】页码跳转框、自定义每页条数框的右侧上下小按钮在窄框里始终与前两位文字打架；监控刷新间隔、转发间隔、相册间隔、每次扫描页数也一并去掉步进按钮 —— 这些字段用户实际都是直接键入。原生数字输入框的自旋按钮保持隐藏，输入区完整显示占位文字。",
      "【系统负载温度显示优化】温度数值改为定宽显示（44px、右对齐），CPU / GPU 温度字宽变化不再让整行元素抖动；常态颜色由暗灰改为淡黄色（#e8c46a）以突出显示，仅 ≥90°C 仍转红作为过热提示，不再用中间态黄色。",
      "【子栏目去掉最左侧高亮竖条】侧栏子项选中时不再显示 3px 橙色竖条，只靠底色与字色区分；顶级项与分组标题的竖条保留，避免两级竖条并存造成的视觉噪点。"
    ]
  },
  {
    ver: "v1.4.8", internal: "2610130003", date: "2026-10-13",
    title: "左侧导航栏全面去圆角、高亮块等宽对齐",
    items: [
      "【侧栏导航彻底去圆角】此前只把悬停态与选中态的右侧圆角去掉，分组标题（.nav-group-head）仍保留圆角，且各层级行宽不一致。现统一：顶级项、分组标题、子项一律 border-radius: 0，整栏看不到任何圆角。",
      "【悬停/选中高亮块宽度统一】原先三种行各有不同外边距 —— 顶级项 margin: 0 8px、子项 margin: 1px 8px 1px 22px、分组标题无外边距，导致高亮块宽窄不一、参差不齐。现全部外边距归零，层级缩进改由 padding-left 实现（顶级 18px / 分组标题 16px / 子项 38px），所有高亮块宽度完全一致（实测 16 行全部 x=0、宽 199px），视觉上成一条整齐竖列。",
      "【顺带修一处隐性 bug】高亮底色改用 background-color 而非 background 简写 —— 简写会把子项缩进导轨线的 background-image 重置为 none，导致悬停时导轨线消失。"
    ]
  },
  {
    ver: "v1.4.7", internal: "2610130002", date: "2026-10-13",
    title: "相册重启免重识别 + 界面细节四处统一",
    items: [
      "【修复「每次软件重启后相册都要重新识别」】根因：增量下载判定只认数据库记录，不校验文件是否还在磁盘 —— 一旦记录还在而文件已被删（换媒体目录、点「清空全部」、库被迁移/恢复而文件没跟上），该URL 会被永久判为「已下载」而跳过，相册列表再也长不回来。修复：has_media_url() 改为「记录 + 磁盘文件存在」双条件；启动后台线程做一次媒体对账 reconcile_media_files()，清掉僵尸记录（日志输出检查/清理条数），此后这些 URL 由相册监控自动重新抓取，页面无需任何手动操作。",
      "【侧栏悬停去圆角】导航项悬停时右侧的圆角与选中态不一致，悬停改为直角（border-radius: 0），与选中态一致。",
      "【存储管理按钮样式统一】「清理缓存 / 清空全部」原为 ghost 描边样式，字重与颜色偏弱不易辨认；改为与「清空日志」一致的实心 danger 按钮。",
      "【数字输入框步进按钮统一】页码跳转框等窄输入框里，步进按钮几乎填满整框并压住 placeholder，观感像「UI 重叠」；步进按钮统一收紧为 16px 宽、上下各留 4px 中缝，右内边距 26px，页码框 / 自定义页码框加宽到 64px（ID 选择器特异性导致的内边距被覆盖已显式修正），所有数字框（含相册间隔）观感一致。"
    ]
  },
  {
    ver: "v1.4.6", internal: "2610130001", date: "2026-10-13",
    title: "系统负载新增 CPU / GPU 温度 + 修复保活任务报错",
    items: [
      "【系统负载新增 CPU / GPU 温度】概览负载行在 CPU、GPU 数值后追加温度：GPU 温度由 NVML 直读核心温度（无 NVIDIA 显卡显示「—」）；CPU 温度因 Windows 上 psutil 无温度接口，改用 PowerShell Get-CimInstance 查 WMI 温度区（wmic 自 Win11 24H2 起已移除），取不到时显示「—」。探测较慢故走独立守护线程 + 缓存（成功 30s、失败退避 15min），绝不拖慢 2s 负载采样；≥80°C 变黄、≥90°C 变红提示。",
      "【修复「保活任务异常：name 'get_account_manager' is not defined」】根因：scheduler.py 的 keepalive_all() 调用了 get_account_manager()，但该模块顶部从未导入它（NameError 被调度循环捕获并记为「保活任务异常」），导致每轮轮询的会话保活探针实际从未执行、Cookie 得不到主动续期。修复：补 from account_manager import get_account_manager。"
    ]
  },
  {
    ver: "v1.4.5", internal: "2610120037", date: "2026-10-12",
    title: "开屏 Logo 眼珠视角跟随 + 橙色流光移到窗口四边",
    items: [
      "【开屏 Logo 眼珠视角跟随】开屏动画的 Logo 眼珠（.bs-eye）接入与侧栏 / 关于页同一套视角跟随系统（指数缓动 + 死区 + 吸附 + 待机呼吸），鼠标移动时眼珠实时跟随视线方向，开屏层淡出后实例自动清理。",
      "【橙色流光移到窗口四边】开屏时绕 Logo 旋转的橙色外环（.bs-ring）移除，改为沿软件界面（窗口视口）四边平滑循环流动的橙色渐变光带（#bootSplash::after，conic-gradient 旋转角度 + mask 环形裁剪，与故障报警光带同款技法），随开屏层一起淡出。"
    ]
  },
  {
    ver: "v1.4.4", internal: "2610120036", date: "2026-10-12",
    title: "健康徽章同步 + 内置浏览器登录修复 + 开屏动画 + 流光迭代",
    items: [
      "【健康徽章同步账户状态】根治「账户管理页已显示「正常」，概览健康徽章仍提示「尚未检测」」的割裂问题——账户健康快照改为零网络读取账户持久化状态（后端 _build_account_health / _recompute_account_health_now），检测/检测全部/增删账户操作后即刻重算快照并推进状态 epoch；前端 renderAllViews 每次刷新也调用 checkAccountHealth()，徽章与账户状态亚秒级同步。",
      "【内置浏览器登录修复】点击「内置浏览器登录」仍弹出本机默认 Edge 打开 weibo.com 登录页——根因是 pywebview 默认 OPEN_EXTERNAL_LINKS_IN_BROWSER=True，微博 SSO 跳转触发 NewWindowRequested 被转交系统浏览器；open_login_browser 开头置 webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER']=False，登录窗口真正内嵌在软件 WebView2 中。",
      "【开屏动画】主界面数据加载期间新增品牌化开屏动画（#bootSplash）：Logo 双层呼吸 + 外环流光旋转 + 名称 + 进度条 +「正在加载数据…」提示，数据就绪后淡出，8s 兜底强制收起，避免主界面等待生硬。",
      "【流光效果迭代】账户故障报警的四周流光由分段虚线滚动（橙色块感）改为平滑流动的渐变光带——body.alarm::after 改用 @property --flowAngle 驱动 conic-gradient 旋转 + mask 环形裁剪，光带连续无分段，参考用户提供的 GIF 效果。"
    ]
  },
  {
    ver: "v1.4.3", internal: "2610120035", date: "2026-10-12",
    title: "状态即时刷新 + 健康徽章配色修正 + 相册说明精简",
    items: [
      "【状态即时刷新】后端 get_state 新增 epoch 机制：所有变更类接口（添加/删除账户、启动/停止轮询、置顶监控、保存配置等 22 处）操作后立即使状态缓存失效并推进 epoch；前端 refreshStateFresh() 循环拉取直到拿到 epoch 已推进的新状态，操作后亚秒级即刷新，根治「添加账户后已记录账户仍显示 0/5」「点启动轮询后仍显示未运行」「置顶/取消置顶响应慢」等界面刷新滞后问题。",
      "【健康徽章配色修正】账户健康徽章逻辑：未添加任何账户 = 黄色（提示级，level=\"empty\"），存在故障账户 = 红色（level=\"crit\"），健康 = 绿色；修正此前无账户却显示绿色的问题（index.html 初始态改为 empty，style.css 新增 .health-indicator.empty 黄色样式）。",
      "【相册说明精简】删除相册监控说明中冗余的「（对应 ?tabtype=album）」字样，文案更干净。"
    ]
  },
  {
    ver: "v1.4.2", internal: "2610120034", date: "2026-10-12",
    title: "转发设置精简：删除分区外框 + 转发方式卡压缩等高",
    items: [
      "【删除分区外框】移除「基础转发配置」「工作时间限制」两个分区外框与标题，轮询间隔 / 转发方式 / 工作时间三卡直接进网格，页面更简洁。",
      "【转发方式卡压缩等高】开关组改为两列布局并修复说明文字整块换行问题（原 flex 换行把开关组撑到 89px 高），卡片自然高度由 302px 压缩至约 220px，与「轮询间隔」卡渲染等高，消除底部留白。"
    ]
  },
  {
    ver: "v1.4.1", internal: "2610120033", date: "2026-10-12",
    title: "UI 三项精修：转发外框去色 + 负载并入统计卡 + 眼珠尺寸恢复",
    items: [
      "【转发设置外框去色】「基础转发配置」「工作时间限制」两个分区外框去掉红/蓝颜色边框与底色，改为与其他卡片一致的中性样式（1px 边框 + 卡片底色），标题文字同步改中性色。",
      "【系统负载并入统计卡】移除独立「系统负载」卡片；CPU / 内存 / 网络 / GPU 两行合并为一行纯数值（百分比 + 网络速率），显示在概览统计瓦片下方，不再显示进度条。",
      "【眼珠尺寸恢复】修复 v1.4.0 重导出眼珠图层时误保留 300×300 全画布导致瞳孔渲染缩小到约 1/3 的问题；换回 97×97 满画布眼珠图，侧栏与关于页眼珠恢复上一版尺寸。"
    ]
  },
  {
    ver: "v1.4.0", internal: "2610120032", date: "2026-10-12",
    title: "眼珠重影修复 + 系统负载 + 数字框统一 + 转发重排 + 账户健康告警",
    items: [
      "【眼珠重影修复 + 关于页视角跟随】修复关于页底部多出一颗「幽灵眼珠」（logo-mix-2.psd 眼珠层自带半透明残影，按 alpha 阈值清零重导出 logo-eye.png / logo-base.png / logo.png）；关于软件页 Logo 改为双层结构并接入鼠标视角跟随。",
      "【系统概览新增负载显示】概览区新增 CPU / 内存 / 网络上下行 / GPU 负载（后台 2s 采样线程，前端仅在概览激活时拉取快照，零阻塞）。",
      "【数字输入框上下按钮风格统一】全局 number input 用 MutationObserver 装饰为自定义上下步进按钮，统一软件风格并保留 min/max/step。",
      "【转发设置重排】轮询间隔 + 转发方式置于红框区（上排两卡），工作时间置于蓝框区（下排），分区更清晰。",
      "【账户健康告警体系】每小时后台检测已记录账户的登录态 + 真实抓取 + 转发能力；异常时概览指示灯变红、软件弹窗告警、界面四周流光滚动报警。"
    ]
  },
  {
    ver: "v1.3.2", internal: "2610120031", date: "2026-10-12",
    title: "侧栏高亮去圆角 + 修复「检测本地 AI 引擎」不显示已安装模型",
    items: [
      "【侧栏高亮（active）右侧去圆角】原 .nav-item 设 border-radius:0 8px 8px 0，高亮块贴着内容区延伸，右端本应平直却带圆角；现 .nav-item.active 补 border-radius:0，高亮项与内容区严丝合缝。",
      "【修复「检测本地 AI 引擎」点了却没显示已安装模型（根因二层）】① 前端：原按钮直接调 probeAiEngine()，该函数探测的是主路线下拉框当前选中的引擎；下拉停在「内置离线AI」时探测结果恒为 builtin，条件 r.engine==='local' 永不成立，模型列表永远不加载。现新增专用 probeLocalAi()，强制 apiCall('probe_ai_engine','local',host,…) 显式探测本机 Ollama，可用即 await loadLocalAiModels() 拉取模型清单。② 后端：Ollama 走 127.0.0.1:11434，urllib.request.urlopen 默认使用系统代理；用户开着 Clash/v2ray 等代理时，本机请求会被发往代理网关而失败，即便 Ollama 正常也误报「未检测到本机 Ollama」。新增 _no_proxy_urlopen()（urllib.request.ProxyHandler({}) 清空代理）专供本地回环地址的探测/列表/生成调用，不再受系统代理影响。"
    ]
  },
  {
    ver: "v1.3.1", internal: "2610120030", date: "2026-10-12",
    title: "全新 Logo / 图标 + 侧栏 Logo 鼠标视角跟随 + 转发设置间距微调",
    items: [
      "【全新 Logo / 图标】应用 Logo 与 exe / 托盘图标更换为新版设计：从 logo-mix.psd 高清导出 300×300 合成图，并重新生成多尺寸 logo.ico（16~256px），侧栏、首启向导、favicon、托盘图标同步更新。",
      "【侧栏 Logo 视角跟随】从 PSD 中提取「眼珠」独立图层（97×97），叠加在无眼珠底版上，眼珠实时跟随鼠标转动。视线系统参考「证书管理库」：指数缓动（τ=0.12s）+ 死区防抖 + 吸附 + 光标静止 1.35s 后进入 7s 周期待机呼吸；位移严格限幅在眼白余量内（左35 / 右48 / 上15 / 下8 画布px 等比换算），不会转出眼眶。系统开启「减少动态效果」时自动停用。",
      "【转发设置间距】「关键词黑名单」卡片与上方两卡网格之间补 16px 间隔（黑名单卡在网格之外，吃不到 grid 的 row-gap），与网格行列间距保持同一节奏。"
    ]
  },
  {
    ver: "v1.3.0", internal: "2610120029", date: "2026-10-12",
    title: "修复首启向导「浏览」无反应 + 转发设置卡片重排 + UI 细节整理",
    items: [
      "【修复 · 首启向导/存储管理「浏览…」点击无反应】根因：pywebview 对每个 JS API 调用都新开一个裸线程（webview/util.py js_bridge_call），该线程未做 COM 初始化，而 SHBrowseForFolderW 属 Shell COM 组件，未初始化 STA COM 时静默返回 NULL，被代码当作「用户取消」——对话框根本没弹。修复：选目录前先 CoInitializeEx(APARTMENTTHREADED)、退出时 CoUninitialize 配对释放；「浏览…」现在可正常弹出系统目录选择框。",
      "【转发设置卡片重排】「轮询间隔」与「工作时间」合并为一张卡片；「关键词黑名单」单独成卡并常驻显示；取消 v1.2.2 的「工作时间 / 关键词黑名单」子标签页，全部设置一屏可见。",
      "【侧栏字体统一】顶级导航项（系统概览 / 软件授权 / 关于软件）与分组标题「基础功能」字号统一为 12px 加粗；分组子项保持 13px 常规体，层级更清晰。",
      "【工作时间条】橙色时段条右端不再带圆角——时间轴是连续刻度，延伸到右端应为平直边缘。",
      "【AI 部署】「保存 AI 设置 / 实测当前引擎 / 自检」按钮行上方补 16px 间隔（原间距规则只写在转发设置作用域下，独立成页后失效）。",
      "【存储管理】「本机数据目录（授权相关·固定）」卡片下方补间隔，与「占用统计」卡片保持一致节奏。",
      "【工程清理】删除构建中间目录（work_v119k_m / spec_v119k_m）与 __pycache__ / .pyc 缓存，释放约 74MB。"
    ]
  },
  {
    ver: "v1.2.2", internal: "2610120028", date: "2026-10-12",
    title: "根治「启动未响应」+ 左侧导航栏分组重构 + 新增存储管理与首启向导",
    items: [
      "【核心：根治软件启动未响应】用户定位到的根因是「要扫描 /data 和下级的位置，所以会卡」，本次确认属实并做了彻底修复。技术成因有两层：一是 `os.walk` 一个装了上万张图的 media/ 目录要几秒到几十秒；二是**更关键** —— pywebview 的 `js_bridge_call` 是在 WebView2 的 **UI 线程**上同步执行 Python 方法的，任何一次这样的遍历都会把整个界面冻住，Windows 随即判定「未响应」。原来的 `get_data_paths_usage()` 就是这么干的。",
      "【修复手段一：懒创建】`ensure_dirs()` 过去会 makedirs 出 images / media / history 三个大目录，等于在启动路径上直接诱使后续代码去遍历它们。现在它只建 DATA_DIR（小目录、纯本地、几乎零成本），四个大目录改为**懒创建**（`ensure_dir(key)`，谁写谁建，见 forward_engine 头像下载、media_engine 用户目录、model_manager 取模型）。",
      "【修复手段二：全异步扫描】新增 `storage.py`：所有 `os.walk` 都挪到后台守护线程，分片推进（每 2000 个文件报一次进度，进度条平滑跳动而不是长时间停在 0%），支持取消，带 60 秒节流避免频繁重扫。`get_storage_status()` 是**纯内存读取、零磁盘 I/O**，所以前端可以每秒轮询而不卡。原来的 `get_data_paths_usage()` 保留为弃用桩，老前端调它也不会再卡。",
      "【首次启动向导】全新用户首次启动会弹全屏遮罩，让用户确认 images / media / models / history 四个目录的保存位置。向后兼容做得比较克制：v1.2.1 的老用户已经手动配过 image / media，判定为「已完成」，**不会被打扰**弹向导。向导里也明确说明：软件授权与运行配置（license.json、config.json、app.log 等）固定存放在 `%LOCALAPPDATA%\\微博bot小助手\\data`，不可更改，以保证授权校验稳定。",
      "【后续启动秒开】之后每次启动只读那一个很小的 `data_paths.json` 配置，**不再遍历任何目录**。四个目录的占用统计在界面出来之后由后台线程慢慢跑，在「系统概览」的「数据载入中」卡片里展示进度条，期间软件完全可交互。",
      "【左侧导航栏分组重构】参考「标书检查工具箱」项目，侧栏改为分组折叠结构：**系统概览** / **基础功能**（账户管理·监控列表·转发设置·相册监控）/ **AI 功能**（AI 部署·AI 人格）/ **数据管理**（存储管理·历史记录·数据总览·运行日志）/ **软件授权** / **关于软件**。分组头可点击折叠，进入子页时会自动展开它所属的分组（否则用户会点到一个看不见高亮项的面板）。",
      "【AI 部署独立成页】原内嵌在转发设置里的「AI 部署」卡片抽出来成为独立的「AI 部署」模块，进入该页时自动刷新引擎状态与部署进度。三类 AI（内置离线 / Ollama / llama.cpp / 云端）仍共用同一套「人格 + 原文锚定」逻辑，主路线失败自动切备路线，转发永不中断。",
      "【工作时间与黑名单并入转发设置】原独立的「工作时间」「关键词黑名单」两个页面取消，改为转发设置页内的子标签页（转发设置原有的轮询间隔 / 转发方式 / 保留 等设置保持不变）。顺带给 `updateWhBar()` 与 `renderSchedule()` 加了空值守卫 —— 子标签页可能在 config 就绪前被调到，原先会抛 `Cannot read properties of undefined`。",
      "【新增存储管理页】四个目录各自一行：路径输入框（留空 = 使用默认）、自定义/默认标签、实时占用（文件数 + 占用大小 + 所在磁盘剩余）、浏览、打开、恢复默认，以及两级清理按钮。「清理缓存」只删可重新下载的图片视频、保留数据库与模型（安全）；「清空全部」连数据库和模型一起删。**历史记录目录明确拒绝整体清空** —— 其中的 history.db 正被本程序占用，强删会导致数据丢失且当前会话报错，会返回友好提示引导改用「清理缓存」或关软件后手动处理。",
      "【数据总览改名回滚】按需求把 v1.2.1 的「数据管理」改回「数据总览」，并把数据路径设置卡整体移出到新的存储管理页，数据总览页回归纯数据展示。",
      "【历史记录目录动态化】原 `HISTORY_DIR`（exe 同级的版本留档目录）与用户数据目录同名易混淆，重命名为 `VERSION_ARCHIVE_DIR` 明确职责；新的 history_dir 是数据目录（存 history.db）。改了历史记录目录会自动迁移 history.db 及其 -wal / -shm 与导出目录，**失败自动回滚**避免库被切成两半。DB 路径从模块常量 `DB_PATH` 改为函数 `get_db_path()`（默认参数在 import 时求值，拿不到运行期改动），并新增 `reset_history_db()` 丢弃单例让下次按新路径重开。四个目录默认值全部落在 DATA_DIR 下，老用户零迁移。",
      "【本轮验证】13 个页面全量布局体检通过（每页 scrollW == clientW，无横向溢出）；HTML 嵌套校验通过（v1.2.0 的严重回归教训延续）；headless 探针实测分组折叠、子标签页、存储页逐行占用文案、首启向导渲染全部正常。过程中修掉一个真实BUG：后端目录键是 snake_case（`image_dir`）、HTML 元素 id 是 camelCase（`imageDirUsage`），映射函数漏了 `Dir` 段导致存储页所有占用文案与路径框都渲染不出来。"
    ]
  },
  {
    ver: "v1.2.1", internal: "2610120027", date: "2026-10-12",
    title: "修复 v1.2.0 引入的 DOM 结构错位（全局界面重叠）+ 数据总览更名数据管理",
    items: [
      "【严重：v1.2.0 的回归 BUG —— 全局界面大面积错位】上一版用脚本替换「AI 部署进度」与「AI 引擎路线」两张旧卡时，结束位置按 strip()==\"</div>\" 定位，**匹配到了内层的 </div>**（闭合 .fwd-actions 的那个），外层卡片的 </div> 因此变成孤儿。结果是 `<section id=\"view-forward\">` 在中途被提前闭合，**从「转发设置」往后的所有 section 全部溢出到 `<main>` 之外**。",
      "【症状对照】① 「工作时间」页标题跑到窗口左上角、内容压到侧边栏底下；② 「数据总览」侧边栏被挤成一条 ~15px 窄线、正文顶到 x=0；③ 转发设置 / AI 人格 / 授权等后续页面同样被压扁。这是**我方代码引入**的结构错误，不是环境或内核问题。",
      "【定位方法】写了一个 HTMLParser 栈式嵌套校验脚本，一次报出5 处不平衡：`<main>`/`<section>` 未闭合、末尾多出 `</section>` `</main>` `</div>`。**凡是批量替换 HTML，必须跑这道校验**——headless 截图只能看出「错位」，看不出「哪一行多了个标签」。",
      "【修复】删掉那个多余的 `</div>`，嵌套校验复跑 **HTML_NEST_OK**。",
      "【需求二：数据总览 → 数据管理】导航项、页标题、toast 文案、代码注释全部改名；内部标识 dataoverview / get_data_overview / view-dataoverview **保持不变**（属于接口约定，改了会波及历史调用与图表缓存）。",
      "【需求一/三：转发设置卡过高】主/备路线原本用单列 .path-grid 上下堆叠，把「AI 部署」卡拉得远高于同网格的「轮询间隔」，后者被顶出大片空白。新增 `.path-grid.cols-2` 两列变体（≤900px 自动回退单列），主/备**并排**显示。",
      "【顺带压缩 AI 卡高度】builtin 的状态描述原文50+ 字，同一份文案会同时渲染在「主路线」「备路线」两行，把卡片撑得很高。精简为「零依赖、始终可用，无需联网或模型运行时。」（27 字）。实测三卡等高 328px，整页 694px，一屏内可览。",
      "【顺带修一处版本号不一致】`paths.MAJOR_VERSION` 一直停在 \"v1.1.9k\"，而 `version.py` 的 MAJOR 早已迭代到 v1.2.x —— `api_bridge` 暴露的 major_version（界面顶部显示）因此长期显示旧版本号。已与 version.py 对齐为 v1.2.1。",
      "【验证】全页面布局体检 **13/13 页通过**：sidebar x=0 w=200、main x=200 并排正常，所有 section scrollWidth == clientWidth（无横向溢出），view-forward 从 845px 降到 694px；转发设置专项：三卡等高、主备并排、参数区在 builtin 下全部隐藏；三引擎回归 **50/50 ALL_OK**；`py_compile` + `node --check` + HTML 嵌套校验全部通过。"
    ]
  },
  {
    ver: "v1.2.0", internal: "2610120026", date: "2026-10-12",
    title: "数据路径设置 + AI 部署升级为三类引擎（大版本迭代）",
    items: [
      "【需求一：数据路径设置】在「数据管理」新增「数据路径设置」卡。软件授权与运行配置（license.json / trial.json / .acc_key / config.json / history.db / app.log）**固定存放**在 %LOCALAPPDATA%\\微博bot小助手\\data，不可更改，以保证授权校验基线稳定；只有**监控列表头像**（images）与**相册 / 视频**（media）两个大目录可自定义到其他磁盘。",
      "【路径实现要点】新增 paths.get_image_dir() / get_media_dir() **动态解析函数**——原先 13 处使用点都是静态导入 IMAGE_DIR / MEDIA_DIR 常量，拿不到运行期改动，现已全部替换；配置落data/data_paths.json，用 os.replace 原子替换避免半截文件。",
      "【数据不会丢】修改路径**不自动搬迁**已有文件（只影响新产生的文件）；数据库里存的是**相对路径**，所以搬迁后历史记录依然能正常预览。",
      "【界面能力】每个目录显示「已用空间 / 所在磁盘剩余」，剩余不足 2GB 时标黄；保存前会探测目标目录可创建、可写入 8KB、剩余空间是否够用，探测不过直接拒绝并说明原因。",
      "【需求二：AI 部署升级为三类引擎】参照「标书检查工具箱」的统一 AI 抽象层重写：内置离线AI（builtin，jieba + 人格模板 + 意图识别，零依赖始终可用）/ 本地离线AI（local = Ollama，local_gguf = llama.cpp + Qwen2.5 GGUF）/ 云端在线AI（cloud，OpenAI 兼容 /chat/completions）。",
      "【主备双路线】可分别指定主路线与备路线。主失败**自动切备**，备失败才落回 builtin，返回值带 chain 记录完整尝试链路；转发永不中断，也不会静默假装成功。",
      "【草稿配置探测】探测的是设置页**当前控件取值**而非已保存值，从根上消除「下拉选了 Ollama、状态却显示内置引擎」的错位；探测时不会顺手拉起 Ollama。",
      "【密钥安全】云端 API Key 单独存于 data/ai_settings.json，不进 config.json / sqlite，导出数据里不会出现；界面上只回显掩码（sk-a…wxyz），留空保存 = 保持原值不变。",
      "【401/403 视为可达】某些厂商 /models 端点需特定权限，401/403 不代表 chat 不可用，因此判定为「服务可达」，避免误杀。",
      "【llama.cpp 路线保留三层去重】local_gguf 走 ai_generator.model_generate（跨条历史注入 + 结构级随机化 + 安全条款），不被统一 prompt 调用绕过；为此把原 generate() 拆出generate_by_rule（内置路线）与 clean_model_output（统一清洗）。",
      "【性能护栏】_build_state() 在 WebView2 UI 线程上调用，三引擎状态快照做成**零网络 I/O**版本（builtin 恒可用、local_gguf 只查文件是否存在、local/cloud 标「待探测」），真实网络探测只在前端进入 AI 页时经 get_ai_settings() 显式触发。",
      "【向后兼容】旧的二引擎 API（get_ai_engine_status / set_ai_engine）保留为兼容层，内部把 rule→builtin、model→local_gguf 翻译；config.json 的 ai_engine 字段保留但已标注弃用。",
      "【版本规则落地】大版本迭代改**第二位**：v1.1.9k → **v1.2.0**；内部版本号 **2610120026**（date=年月 261012 + seq=0026）。",
      "【验证】三引擎回归测试 **50/50ALL_OK**：覆盖配置读写/损坏兜底、密钥掩码、四引擎探测、cloud 401 判可达、主备降级链路（cloud→local→builtin）、同引擎不重复试、local_gguf 走三层去重、all_status 一致性；数据路径回归 **20/20 ALL_OK**；headless 实测两张新卡无横向溢出、密钥不回显明文。"
    ]
  },
  {
    ver: "v1.1.9k", internal: "2610090011", date: "2026-10-12",
    title: "根治「全站 network_error」：CA 证书包自愈重写",
    items: [
      "【症状】微博拉取、相册拉取、账户登录检测**同时**全部失败，日志刷屏 Could not find a suitable TLS CA certificate bundle, invalid path: ...\\Temp\\_MEI00000c5c2\\certifi\\cacert.pem。表现为「扫描 0 条 / 新下载 0」，但重启一次就恢复正常。",
      "【根因】PyInstaller onefile 把 certifi 的 cacert.pem 解压到 %TEMP%\\_MEIxxxxxx\\，该目录一旦被 Windows 存储感知 / 磁盘清理 / 杀软删掉，requests 就会对**每一个** HTTPS 请求抛错 → 全站 network_error。",
      "【致命缺陷一：备份从未被创建】旧逻辑是「certifi 的包存在 → 顺手备份」，可一旦 _MEI 被清理，第一次进入自愈时备份必然为空 → 永远无法自愈。备份必须**无条件预置**，不能等失效。",
      "【致命缺陷二：备份没跟着数据目录迁移】程序把数据目录迁到 %LOCALAPPDATA% 时，迁移清单里漏了 cacert.pem，旧备份仍留在老目录，新目录里压根没有这个文件。已加入 _MIGRATE_FILES 并做兜底重建。",
      "【致命缺陷三：_ca_healed 是假性成功】它在**尝试之前**就置 True，自愈失败后所有后续请求都被 if _ca_healed: return 挡掉，**再也不会重试**。实测日志里 5621 条 CA 错误却一条「已自愈」记录都没有。现改为只有真正拿到可用证书包才置标志，失败允许下次重试。",
      "【重写 ca_bundle.py】启动最早期无条件预置备份（赶在任何网络请求之前）；多级兜底源逐级尝试：本地 DATA_DIR 副本 → _MEIPASS 解压产物 → sys.base_prefix 真实 certifi → 老数据目录历史备份 → Windows 自带 schannel.pem / curl-ca-bundle.crt；并按「存在 + 非空 + 含 PEM 头」三重校验，避免拿到损坏文件。",
      "【顺手修一处漏网】图片下载原先用的是裸 requests.get，**绕过了 _session()**，因此不会触发 CA 检查——CA 失效时图片下载必然一起挂。已统一改走 _session()。",
      "【降低发生概率】新增 _harden_meipass()：启动时把整个 _MEI 目录树的文件时间戳顶到现在，续上存储感知的 7 天保鲜期，降低临时目录被回收的概率。",
      "【验证】CA 自愈回归测试 4 个场景 **11/11 ALL_OK**：① certifi 正常时备份被预置；② _MEI 被清理时自动切到本地备份并写入三个环境变量，requests 确认接受该 verify；③ 所有源都不可用时返回失败且**允许重试**（回归旧版死锁）；④ 源恢复后能自愈成功。"
    ]
  },
  {
    ver: "v1.1.9k", internal: "2610090010", date: "2026-10-09",
    title: "修复关于页入侵其他模块 / 授权两框统一 / 根治启动未响应",
    items: [
      "【严重：关于软件页入侵所有模块】上一版给 #view-about 加了 display:flex，而 ID 选择器优先级高于 .view{display:none}，导致关于软件页**永远显示、切页也切不走**，内容出现在账户管理等所有页面下方。现改为 display 由 .view/.view.active 决定，仅在激活时追加 flex 布局。",
      "【关于页 12px 间距仍未生效（上一版修错层级）】renderAbout() 把所有卡片渲染进 <div id=\"aboutCard\">，flex gap 只作用在 #view-about 的直接子项（h2/sub/aboutCard）上，卡片之间仍是 0 间距。现把 flex+gap 下移到 #aboutCard 这一层，实测 hero→四宫格、四宫格→社媒卡、社媒卡→须知卡 均为 12px。",
      "【授权两框样式统一】授权密钥由 <input> 改为 rows=1 的 <textarea>——固定高度的 input 在 WebView2 里会把文字垂直居中，与左侧 textarea 顶部起点对不齐，且不同内核行为还不一致。改用 textarea 后两框字体/字号/行高/内边距/边框/尺寸逐项相同。",
      "【根治启动「10 次有 9 次未响应」】实测 history.db 已 84MB，冷启动 _build_state() 需 356ms（首次 SQLite 连接），而这段代码与 get_state 同步跑在 WebView2 的 UI 线程上；旧实现还会 time.sleep(0.1)×20 再退化为同步建状态，「10 次里只有 1 次快」正是竞态。现改为：get_state 冷启动立即返回空壳（实测 3ms）+ 后台线程建状态；前端 init() 不再 await loadState，先渲染骨架让窗口立即可交互，并用 retryLoadRealState 快速重试取回真实数据。",
      "首屏新增「正在加载数据…」轻提示，避免等待期间被误认为卡死。"
    ]
  },
  {
    ver: "v1.1.9k", internal: "2610090009", date: "2026-10-09",
    title: "UI 根治：关于页重叠 / 下载任务居中 / 授权密钥一致性",
    items: [
      "【关于软件页 重叠修复】#view-about 改为 flex 纵向布局 + gap:12px 统一控制区块间距；所有卡片 margin 清零（原卡片 margin 与父级间距叠加、且网格容器不包住子项 margin，导致「关注作者·联系交流」「使用须知」标题紧贴上方卡片，视觉重叠）。四宫格改 grid-auto-rows:1fr 强制同行瓦片等高。",
      "【下载任务 开关居中】开关由 flex-end + margin 补偿改为 align-self:center，与「按监控间隔自动下载」两行文字的中心线始终重合（原底部对齐使开关明显偏下）。",
      "【授权密钥 一致性】删除 #featCode 写死的 font-size:12px（它使特征码 12px、密钥 13px 不一致）；两框统一 .lic-box 13px 等宽字体。input 去掉固定 height、改 height:auto + min-height:76px，文字真正顶部靠左（原固定高度的 input 被浏览器垂直居中，即用户看到的「0000-0000 没顶部靠左」）。",
      "【UI 目录定位加固】_ui_dir() 候选列表移除一批早已删除的旧 staging 目录（v119j/i/h/g/f、v118d、v118、v117b），避免旧目录盖过包内 UI 造成「版本号新、界面旧」；项目根旧的 ui/ 目录已归档到 _archive_ui_old/。",
      "打包归档流程内建：构建脚本自动同步 CHANGELOG 版本号、复制到根目录、归档 history/，并校验三处哈希一致。"
    ]
  },
  {
    ver: "v1.1.9k", internal: "2610090008", date: "2026-10-09",
    title: "打包归档闭环 · 确认修复进包",
    items: [
      "补齐交付流程：打完包后自动更新根目录「微博bot小助手.exe」并归档到 history/微博bot小助手_<内部版本>.exe。",
      "本版含上一版全部 UI 修复：转发设置结构错位、转发方式开关居中、授权两框对齐、密钥 placeholder 同字体顶左、关于页瓦片等高、AI 部署进度与引擎路线换位。",
      "三份产物（dist / 根目录 / history）哈希一致校验通过；真实启动冒烟 2.5 秒出主窗口。"
    ]
  },
  {
    ver: "v1.1.9k", internal: "2610090006", date: "2026-10-09",
    title: "UI 精修：转发设置结构修复 / 授权对齐 / 关于页",
    items: [
      "修复转发设置页 DOM 结构错位：转发方式卡片此前被误置于区块外，导致页面元素重叠，现归位到四卡网格内。",
      "转发方式卡片开关与文字改为垂直居中对齐（原顶部对齐视觉偏上）。",
      "软件授权页「本机特征码」与「授权密钥」两个框起始位置对齐（统一说明行占位高度）。",
      "授权密钥 placeholder 改为与特征码一致的等宽字体，并顶部靠左显示（不再是居中/异体字）。",
      "关于软件页瓦片同行等高、免责声明列防止长文本溢出卡片右边界。",
      "「AI 部署进度」与「AI 引擎路线」两张卡片互换位置，改为先看进度再看引擎路线。",
      "补齐 CSS 变量 --radius-sm，消除未定义变量告警。"
    ]
  },
  {
    ver: "v1.1.9k", internal: "2610090003", date: "2026-10-09",
    title: "UI 整理：关于页 / 相册监控 / 授权 / 日志",
    items: [
      "关于软件页统一卡片间距并整理到一屏内，新增「查看更新详情」弹窗（即本弹窗）。",
      "关键词黑名单「新增关键词」输入框与「添加」按钮底部对齐。",
      "相册监控「下载任务」行开关、间隔、扫描页数、保存设置高度统一。",
      "「每对象页数」更名为「每次扫描对象页数」。",
      "软件授权页特征码与授权密钥输入框等高，左右两卡整体等高。",
      "运行日志「清空日志」按钮换行问题修复，独立成行右侧对齐。",
      "工程目录整体迁移到本地盘 E 盘，归拢历史版本、清理临时文件。"
    ]
  },
  {
    ver: "v1.1.9k", internal: "2610090002", date: "2026-10-09",
    title: "根治启动「未响应」",
    items: [
      "运行数据目录迁移到本地盘（%LOCALAPPDATA%），首次启动自动迁移老数据。",
      "数据库改为「在线备份导入」，解决打包后老库迁移失败问题。",
      "日志改长句柄 + 8MB 轮转 + 尾部读取；logs 补索引、启动后台裁剪到 12 万行。",
      "新增边缘磁吸（贴边仅调位置、不改窗口尺寸）。",
      "显式声明 DPI 感知（PerMonitorV2）。"
    ]
  },
  {
    ver: "v1.1.9i", internal: "2610090001", date: "2026-10-09",
    title: "回退到 Windows 原生标题栏",
    items: [
      "放弃自绘无边框标题栏（拖动 / 命中测试不稳定），改回系统原生标题栏。",
      "窗口拖动、缩放、最小化、最大化、关闭、Aero Snap 全部由系统提供。",
      "保留「最小化到系统托盘」能力（pystray 守护线程 + Win32 直驱）。"
    ]
  },
  {
    ver: "v1.1.7", internal: "2610080001", date: "2026-10-08",
    title: "机器绑定授权 + 托盘",
    items: [
      "授权改为机器绑定（特征码 + 授权密钥），默认 7 天免费试用。",
      "新增系统托盘图标，支持最小化到托盘与托盘菜单退出。"
    ]
  },
  {
    ver: "v1.1.6", internal: "2609290001", date: "2026-09-29",
    title: "日志异步化 + Cookie 续期加固",
    items: [
      "日志全面异步化（入队即返回，后台线程落盘），业务线程不再被磁盘 I/O 拖住。",
      "get_state 增加 2 秒缓存，概览页轮询不再每秒读文件 / SQLite。",
      "新增运行环境检测：从网络路径运行时顶部提示「建议复制到本地磁盘」。",
      "修复会话续期未同步滑动 last_ok_at 导致活账户 7 天后被误判失效。"
    ]
  },
  {
    ver: "v1.1.5", internal: "2609210002", date: "2026-09-28",
    title: "修复 Cookie 失效预测错误",
    items: [
      "失效预测改为以 SUB 会话窗口优先（约 7 天无活动即失效）。",
      "新增账户字段 last_ok_at，成功检测 / 续期 / 使用时回写。"
    ]
  },
  {
    ver: "v1.1.4", internal: "2609210001", date: "2026-09-21",
    title: "Cookie 失效预测 + 自动续期",
    items: [
      "新增 Cookie 有效期预测，账户表格展示失效时间与剩余时长。",
      "新增 Cookie 自动续期（会话保活）与每行手动「续期」按钮。",
      "添加账户 UI 压缩，收进一屏。"
    ]
  },
  {
    ver: "v1.1.3", internal: "2609140001", date: "2026-09-14",
    title: "修复全站网络错误 + 账户检测",
    items: [
      "修复 certifi 证书包被外部清理导致全部 HTTPS 请求失败（新增 cacert.pem 备份自愈）。",
      "修复 /ajax/profile/info 强制要求 uid 导致的昵称无法回填（新增 get_self_uid）。"
    ]
  },
  {
    ver: "v1.1.2", internal: "2609120003", date: "2026-09-12",
    title: "修复跨域误判 Cookie 失效",
    items: [
      "修复移动端接口跨域误判导致「全新 Cookie 被提示已失效」。",
      "只有与 Cookie 同源的 weibo.com 接口才有权判定「已失效」。"
    ]
  },
  {
    ver: "v1.1.1", internal: "2609120001", date: "2026-09-12",
    title: "修复「检测正常却抓不到微博」",
    items: [
      "check_login 重写为四级判定，不再把 XSRF-TOKEN 当作已登录依据。",
      "账户检测追加真实抓取探测，只有真能取到数据才算可用。",
      "新增 Cookie 自动续期（会话保活）与结构化失败原因。"
    ]
  },
  {
    ver: "v1.1.0", internal: "2609090005", date: "2026-09-09",
    title: "导出统一含图片 + 黑名单 UI 优化",
    items: [
      "三种导出统一为「文件夹」形态，均包含图片与 README。",
      "修复 XLSX 导出因缺少 openpyxl 不可用。",
      "关键词黑名单新增工具栏、批量编辑、搜索与未保存提示。"
    ]
  }
];

function renderChangelogModal() {
  if (document.getElementById("changelogMask")) return;
  const el = document.createElement("div");
  el.id = "changelogMask";
  el.className = "cl-mask";
  el.innerHTML = `
    <div class="cl-dialog" onclick="event.stopPropagation()">
      <div class="cl-head">
        <div class="cl-head-title">版本更新详情</div>
        <button class="ghost cl-close" onclick="closeChangelog()">关闭</button>
      </div>
      <div class="cl-body">
        ${CHANGELOG.map(v => `
          <div class="cl-item">
            <div class="cl-item-head">
              <span class="cl-ver">${esc(v.ver)}</span>
              <span class="cl-date">${esc(v.date)} · 内部 ${esc(v.internal)}</span>
            </div>
            <div class="cl-title">${esc(v.title)}</div>
            <ul class="cl-list">${v.items.map(t => `<li>${esc(t)}</li>`).join("")}</ul>
          </div>
        `).join("")}
      </div>
    </div>`;
  el.onclick = () => closeChangelog();
  document.body.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
}

function openChangelog() {
  let el = document.getElementById("changelogMask");
  if (!el) { renderChangelogModal(); el = document.getElementById("changelogMask"); }
  if (el) { el.style.display = "flex"; requestAnimationFrame(() => el.classList.add("show")); }
}

function closeChangelog() {
  const el = document.getElementById("changelogMask");
  if (!el) return;
  el.classList.remove("show");
  setTimeout(() => { el.style.display = "none"; }, 180);
}

/* ---------------- 授权 ---------------- */
function renderLicense() {
  const lic = STATE.license || {};
  const icon = document.getElementById("licIcon");
  const title = document.getElementById("licTitle");
  const desc = document.getElementById("licDesc");
  const meta = document.getElementById("licMeta");
  const card = document.getElementById("licStatusCard");
  const feat = document.getElementById("featCode");
  const featValid = document.getElementById("featValid");

  if (lic.licensed) {
    card.className = "card lic-card lic-green";
    icon.textContent = "✓";
    title.textContent = `已授权 · ${esc(lic.type_name)}`;
    const exp = lic.exp_str || (lic.exp === 0 ? "永久有效" : "");
    desc.textContent = `有效期至 ${exp}`;
    meta.textContent = "转发功能可用";
    meta.className = "lic-meta green";
  } else if (lic.trial) {
    card.className = "card lic-card lic-yellow";
    icon.textContent = "⏳";
    title.textContent = "试用中";
    const sec = lic.remaining_seconds || 0;
    const days = Math.floor(sec / 86400);
    const hrs = Math.floor((sec % 86400) / 3600);
    desc.textContent = `剩余 ${days} 天 ${hrs} 小时（共 ${lic.trial_days} 天试用）`;
    meta.textContent = "试用期结束后需激活授权才能转发";
    meta.className = "lic-meta yellow";
  } else {
    card.className = "card lic-card lic-red";
    icon.textContent = "✕";
    title.textContent = "未授权";
    desc.textContent = "转发功能已不可用";
    meta.textContent = "请生成特征码并联系作者获取授权密钥";
    meta.className = "lic-meta red";
  }

  // 特征码（优先用接口返回，避免重复生成导致有效期刷新）
  if (window.__licFeature) {
    feat.value = window.__licFeature;
    if (window.__licValidUntil) {
      featValid.textContent = "有效期至：" + new Date(window.__licValidUntil * 1000).toLocaleString();
    }
  } else {
    // 首次进入：拉取一次特征码与有效期
    apiCall("get_license_status").then(st => {
      if (st.ok) {
        window.__licFeature = st.feature_code;
        window.__licValidUntil = st.feature_code_valid_until;
        document.getElementById("featCode").value = st.feature_code;
        document.getElementById("featValid").textContent =
          "有效期至：" + new Date(st.feature_code_valid_until * 1000).toLocaleString();
      }
    }).catch(() => {});
  }
}

async function genFeatureCode() {
  const r = await apiCall("regenerate_feature_code");
  if (r.ok) {
    window.__licFeature = r.feature_code;
    document.getElementById("featCode").value = r.feature_code;
    // 重新拉取有效期
    const st = await apiCall("get_license_status");
    if (st.ok) window.__licValidUntil = st.feature_code_valid_until;
    const fv = document.getElementById("featValid");
    if (window.__licValidUntil) fv.textContent = "有效期至：" + new Date(window.__licValidUntil * 1000).toLocaleString();
    toast("已生成特征码", "ok");
  } else toast("生成失败", "err");
}

function copyFeatureCode() {
  const v = document.getElementById("featCode").value;
  if (!v) { toast("请先生成特征码", "err"); return; }
  if (navigator.clipboard) {
    navigator.clipboard.writeText(v).then(() => toast("特征码已复制", "ok"),
      () => toast("复制失败，请手动复制", "err"));
  } else {
    const t = document.getElementById("featCode");
    t.select(); document.execCommand("copy");
    toast("特征码已复制", "ok");
  }
}

async function activateLicense() {
  const key = document.getElementById("licKey").value.trim();
  if (!key) { toast("请粘贴授权密钥", "err"); return; }
  const r = await apiCall("activate_license", key);
  if (r.ok) {
    toast(`授权成功：${r.type_name}`, "ok");
    document.getElementById("licKey").value = "";
    await refreshStateFresh();
    const st = await apiCall("get_license_status");
    if (st.ok) { window.__licFeature = st.feature_code; window.__licValidUntil = st.feature_code_valid_until; }
  } else toast(r.error || "激活失败", "err");
}

async function deactivateLicense() {
  if (!confirm("确定清除当前授权？清除后若试用已结束将无法转发。")) return;
  const r = await apiCall("deactivate_license");
  if (r.ok) { toast("已清除授权", "ok"); await refreshStateFresh(); }
  else toast(r.error || "失败", "err");
}


/* ---------------- 监控对象 导入 / 导出 ---------------- */
function showMonImportBox() {
  const b = document.getElementById("monImportBox");
  if (b) {
    b.style.display = "block";
    const t = document.getElementById("monImportText");
    if (t) t.focus();
  }
}

function hideMonImportBox() {
  const b = document.getElementById("monImportBox");
  if (b) b.style.display = "none";
  const r = document.getElementById("monImportResult");
  if (r) r.textContent = "";
}

async function submitMonImport() {
  const t = document.getElementById("monImportText");
  const text = t ? t.value : "";
  if (!text.trim()) { toast("请先粘贴要导入的内容"); return; }
  const res = await apiCall("import_monitors_text", text);
  const el = document.getElementById("monImportResult");
  if (!res || !res.ok) {
    if (el) el.textContent = "导入失败：" + ((res && res.error) || "未知错误");
    return;
  }
  if (el) el.textContent = `新增 ${res.added} · 跳过 ${res.skipped} · 失败 ${res.failed}`;
  toast(`导入完成：新增 ${res.added} 个，跳过 ${res.skipped} 个`);
  if (t) t.value = "";
  hideMonImportBox();
  await refreshStateFresh();
  renderMonitors();
}

async function importMonitorsFile() {
  const res = await apiCall("import_monitors_from_file");
  if (res && res.ok) {
    toast(`导入完成：新增 ${res.added} · 跳过 ${res.skipped} · 失败 ${res.failed}`);
    await refreshStateFresh();
    renderMonitors();
  } else if (res && res.error && !/取消/.test(res.error)) {
    toast("导入失败：" + res.error);
  }
}

async function exportMonitors(fmt) {
  const res = await apiCall("export_monitors", fmt);
  if (res && res.ok) toast(`已导出 ${res.count} 个监控对象 → ${res.path}`);
  else if (res && res.error && !/取消/.test(res.error)) toast("导出失败：" + res.error);
}

/* ---------------- 视频 / 相册 ---------------- */
let _mediaTimer = null;
let _mediaKwTimer = null;

function escAttr(s) { return esc(s).replace(/'/g, "&#39;"); }

async function loadMediaPage() {
  await loadMediaConfig();
  await loadMediaSummary();
  await loadMediaObjects();
  // 若下载任务仍在执行（例如切走又切回），恢复进度轮询
  try {
    const p = await apiCall("get_media_progress");
    if (p && p.active) startMediaProgressPolling();
  } catch (e) { /* 忽略 */ }
}

/* 【v1.4.17】备份索引：按当前台账生成 JSON 另存到用户指定路径 */
async function backupMediaIndex() {
  const res = await apiCall("backup_media_index");
  if (res && res.ok) toast(`已备份索引 ${res.count} 条`);
  else if (res && res.error === "已取消") { /* 用户主动取消，不提示 */ }
  else toast("备份失败：" + ((res && res.error) || "未知错误"));
}

/* 【v1.4.17】导入索引：读 JSON 并把台账记录回灌到数据库 */
async function importMediaIndex() {
  const res = await apiCall("import_media_index");
  if (res && res.ok) {
    toast(`已导入索引：回灌 ${res.added} / ${res.total} 条`);
    await loadMediaSummary();
    await loadMediaObjects();
  } else if (res && res.error === "已取消") { /* 用户主动取消，不提示 */ }
  else toast("导入失败：" + ((res && res.error) || "未知错误"));
}

async function loadMediaConfig() {
  const c = await apiCall("get_media_config");
  if (!c) return;
  const chk = (id, v) => { const el = document.getElementById(id); if (el) el.checked = !!v; };
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  chk("mediaEnabled", c.enabled);
  set("mediaInterval", c.interval_minutes || 30);
  set("mediaPages", c.max_pages || 5);
}

function onMediaEnabledChange() { /* 需点击「保存设置」生效 */ }

async function saveMediaConfig() {
  const patch = {
    enabled: document.getElementById("mediaEnabled").checked,
    interval_minutes: parseInt(document.getElementById("mediaInterval").value || "30", 10),
    max_pages: parseInt(document.getElementById("mediaPages").value || "5", 10),
  };
  const res = await apiCall("save_media_config", patch);
  if (res && res.ok) toast("相册监控设置已保存");
  else toast("保存失败：" + ((res && res.error) || "未知错误"));
}

async function loadMediaSummary() {
  const s = await apiCall("get_media_summary");
  const el = document.getElementById("mediaSummary");
  if (!el || !s) return;
  const mb = (s.size || 0) / 1024 / 1024;
  el.textContent = `媒体库：${s.objects || 0} 个对象 · 图片 ${s.images || 0} 张 · 占用 ${mb.toFixed(1)} MB`;
}

async function loadMediaObjects() {
  const list = await apiCall("list_media_objects") || [];
  renderMediaObjects(list);
}

function onMediaKeywordInput() {
  clearTimeout(_mediaKwTimer);
  _mediaKwTimer = setTimeout(searchMedia, 350);
}

async function searchMedia() {
  const kw = (document.getElementById("mediaKeyword").value || "").trim();
  if (!kw) { loadMediaObjects(); return; }
  const rows = await apiCall("search_media", kw) || [];
  const map = {};
  for (const r of rows) {
    if (!map[r.uid]) map[r.uid] = { uid: r.uid, screen_name: r.screen_name || "", videos: 0, images: 0 };
    if (r.media_type === "video") map[r.uid].videos++;
    else map[r.uid].images++;
  }
  renderMediaObjects(Object.values(map));
}

function renderMediaObjects(list) {
  const box = document.getElementById("mediaList");
  if (!box) return;
  if (!list.length) {
    box.innerHTML = `<div class="muted" style="font-size:13px;">暂无媒体文件，点击「立即执行下载」开始抓取。</div>`;
    return;
  }
  box.innerHTML = `<div class="media-obj-grid">` + list.map(o => {
    const mb = (o.size || 0) / 1024 / 1024;
    const sizeTxt = mb >= 1 ? ` · 占用 ${mb.toFixed(1)} MB` : "";
    return `
    <div class="media-obj-card">
      <div class="media-obj-main">
        <div class="media-obj-info">
          <div class="media-obj-name">${esc(o.screen_name || "(无昵称)")}</div>
          <div class="media-obj-uid">UID ${esc(o.uid)}</div>
          <div class="media-obj-counts">相册 <b>${o.images || 0}</b> 张${esc(sizeTxt)}</div>
          <div class="row" style="gap:8px; margin-top:10px;">
            <button class="ghost" onclick="openMediaDir('${escAttr(o.uid)}')">打开目录</button>
          </div>
        </div>
        <div class="media-obj-thumb" id="mediaThumb_${escAttr(o.uid)}">
          <span class="mot-empty">—</span>
        </div>
      </div>
    </div>`;
  }).join("") + `</div>`;
  // 懒加载每个对象的最新一张预览图（只取信息，再按需读图）
  list.forEach(o => loadMediaThumb(o.uid));
}

/* 【v1.4.17】卡片右侧最新图片预览：先取最新图信息，再按需拉 base64。
   只加载「最新一张」，避免旧版点击展开整个相册带来的软件 / 硬盘压力。 */
async function loadMediaThumb(uid) {
  const box = document.getElementById("mediaThumb_" + uid);
  if (!box) return;
  let info = null;
  try { info = await apiCall("get_latest_media_thumb", uid); } catch (e) { /* 忽略 */ }
  if (!info || !info.ok || !info.file_path) return;
  let res = null;
  try { res = await apiCall("load_media_base64", info.file_path); } catch (e) { /* 忽略 */ }
  if (!res || !res.ok || !res.data_url) return;
  const el = document.getElementById("mediaThumb_" + uid);
  if (!el) return;   // 渲染可能已被刷新替换
  el.innerHTML = `<img src="${res.data_url}" title="${escAttr(info.file_name || "")}" alt="">`;
}

/* 【v1.4.17】原 toggleMediaObject（点击卡片展开整个相册）已移除 ——
   一次性取几十张 base64 对软件内存与磁盘压力过大，改为卡片右侧只显示最新一张预览图。 */

async function previewMedia(rel, type) {
  const box = document.getElementById("mediaPreview");
  const mask = document.getElementById("mediaPreviewMask");
  if (!box || !mask) return;
  box.innerHTML = `<div class="muted">加载中…</div>`;
  mask.classList.add("show");
  const r = await apiCall("load_media_base64", rel);
  box.innerHTML = (r && r.ok)
    ? `<img src="${r.data_url}">`
    : `<div class="muted" style="color:#ff8a8a;">无法加载：${esc((r && r.error) || "未知错误")}</div>`;
}

function closeMediaPreview() {
  const mask = document.getElementById("mediaPreviewMask");
  const box = document.getElementById("mediaPreview");
  if (mask) mask.classList.remove("show");
  if (box) box.innerHTML = "";
}

async function startMediaDownload() {
  const res = await apiCall("start_media_download");
  if (res && res.ok) { toast("下载任务已启动"); startMediaProgressPolling(); }
  else toast("启动失败：" + ((res && res.error) || "未知错误"));
}

function startMediaProgressPolling() {
  clearInterval(_mediaTimer);
  _mediaTimer = setInterval(async () => {
    let p;
    try { p = await apiCall("get_media_progress"); } catch (e) { return; }
    if (!p) return;
    const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
    const bar = document.getElementById("mediaBar");
    const pctEl = document.getElementById("mediaPct");
    const stEl = document.getElementById("mediaStatus");
    if (bar) bar.style.width = pct + "%";
    if (pctEl) pctEl.textContent = pct + "%";
    if (stEl) {
      stEl.textContent = p.active
        ? `下载中 ${p.done}/${p.total} · 当前：${p.current || "-"} · 扫描 ${p.posts} 条微博`
          + ` · 新下载 ${p.files_done} · 增量跳过 ${p.files_skipped} · 失败 ${p.files_failed}`
        : (p.message || "空闲");
    }
    if (!p.active) {
      clearInterval(_mediaTimer);
      _mediaTimer = null;
      await loadMediaSummary();
      await loadMediaObjects();
    }
  }, 1000);
}

async function openMediaDir(uid) {
  const res = await apiCall("open_media_dir", uid || "");
  if (res && !res.ok) toast("打开失败：" + (res.error || ""));
}

/* 清理下载记录：只重置增量判定，不动本地文件 */
async function clearMediaRecords() {
  if (!confirm("确认清理下载记录？\n\n· 仅清空软件内的下载记录，已下载的图片文件会完整保留\n· 清理后增量判定重置，再次执行下载将重新拉取并覆盖同名文件")) return;
  const res = await apiCall("clear_media_records");
  if (res && res.ok) {
    toast("下载记录已清理（本地文件保留，增量判定已重置）");
    await loadMediaSummary();
    await loadMediaObjects();
  } else {
    toast("清理失败：" + ((res && res.error) || "未知错误"));
  }
}

/* ---------------- 启动 ---------------- */

/* v1.1.9k：最小化到托盘。按钮已从侧栏移到「系统概览 → 调度状态」按钮行，
   通过 onclick="toTray()" 直接调用（无需再单独绑定）。 */
function toTray() {
  apiCall("window_to_tray").catch(err => {
    console.error("window_to_tray failed:", err);
    toast("最小化到托盘失败", "err");
  });
}

function bindTrayButton() {
  // 兼容旧位置：若页面里仍存在侧栏 #btnTray，也给它绑上（现在按钮走 onclick）。
  const btn = document.getElementById("btnTray");
  if (btn && !btn._bound) {
    if (!btn.getAttribute("onclick")) btn.onclick = () => toTray();
    btn._bound = true;
  }
}

function bindEdgeSnap() {
  // v1.1.9k：边缘磁吸。原生标题栏下窗口由系统拖动，前端拿不到拖动过程事件，
  // 因此用「窗口位置轮询 + 变化后吸附」的方式：位置稳定下来就尝试贴边。
  if (window._snapTimer) return;
  let last = "";
  window._snapTimer = setInterval(async () => {
    try {
      const p = await apiCall("get_window_pos");
      if (!p || !p.ok) return;
      const key = p.x + "," + p.y + "," + p.width + "," + p.height;
      if (key === last) return;          // 没动，不处理
      last = key;
      // 位置刚变化 → 稍等它稳定下来再吸附（避免拖动过程中反复贴）
      clearTimeout(window._snapDebounce);
      window._snapDebounce = setTimeout(() => {
        apiCall("snap_window").catch(() => {});
      }, 220);
    } catch (e) { /* 忽略 */ }
  }, 500);
}

function boot() {
  // 窗口改为 Windows 原生标题栏（v1.1.9i），无需再绑定自绘标题栏控件；
  // 托盘按钮单独绑定（不依赖后端，点了就要有反应）。
  bindTrayButton();
  bindEdgeSnap();
  try {
    const p = init();
    if (p && typeof p.catch === "function") {
      p.catch((err) => { console.error("init failed:", err); });
    }
  } catch (err) {
    console.error("init threw:", err);
  }
}
/* 【v1.5.0 修复】健壮启动：只在桥真正就绪（window.pywebview.api 存在）时才 boot，
   兼容三种时序：
     ① pywebviewready 正常触发且 api 已就绪 → 立即 boot；
     ② 解析时 window.pywebview 已存在但 .api 尚未挂载（pywebview 偶发时序）
        → 不提前 boot，改为轮询等到 api 就绪再 boot；
     ③ pywebviewready 偶发不触发 → 轮询兜底，至多 ~8s。
   彻底消除「窗口出现但开屏永远卡 4%」的静默失败：旧逻辑 `if (window.pywebview) boot()`
   会在 .api 还是 undefined 时抢跑，导致 init() 拿到 undefined 的 API，其后所有
   api 调用（含 boot_stage 埋点）被 try/catch 吞掉 → diag 完全无 BOOT_STAGE。 */
(function () {
  var _booted = false;
  function ensureBoot() {
    if (_booted) return;
    if (window.pywebview && window.pywebview.api) {
      _booted = true;
      boot();
    }
  }
  if (window.pywebview && window.pywebview.api) {
    ensureBoot();
  } else {
    window.addEventListener("pywebviewready", ensureBoot);
    var _watch = setInterval(function () {
      if (window.pywebview && window.pywebview.api) {
        clearInterval(_watch);
        ensureBoot();
      }
    }, 250);
    setTimeout(function () {
      clearInterval(_watch);
    }, 8000);
  }
})();

/* ================= 【v1.3.1】Logo 视角跟随（initLogoGaze） =================
   从 logo-mix.psd 提取「眼珠」图层叠加在无眼珠底版上，眼珠跟随鼠标转动。
   视线系统参考「证书管理库」widgets.py 的 GazeField：
   指数缓动（TAU）+ 死区（DEAD_ZONE）+ 吸附（SNAP）+ 待机呼吸（BREATH_*）。
   位移限幅用 PSD 实测眼白余量（画布 300px）：
     眼珠 bbox (65,149)-(162,246)，眼白 bbox (30,134)-(210,254)
     → 左余 35 / 右余 48 / 上余 15 / 下余 8，按 logo 显示尺寸等比换算。 */
(function () {
  // 【v1.4.0】视角跟随：支持多实例（侧栏 Logo + 关于页 Logo），共享一个鼠标监听与缓动循环。
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  var CANVAS = 300;
  var SLACK = { l: 35, r: 48, u: 15, d: 8 };  // 眼珠在眼白内的活动余量（画布像素）
  var GAZE_TAU = 0.12;       // 缓动时间常数（秒）：越小越跟手
  var DEAD_ZONE = 0.18;      // 死区（显示像素）：防静止边缘 0/1 档反复翻
  var SNAP = 0.04;           // 吸附：目标差小于它就直接到位
  var REACH = 260;           // 光标离 logo 多远视线打满（显示像素）
  var BREATH_PERIOD = 7.0;   // 待机呼吸周期（秒）
  var BREATH_AMP = 0.45;     // 待机呼吸幅度（显示像素）
  var IDLE_AFTER = 1.35;     // 光标静止多久进入待机（秒）
  var IDLE_TAU = 0.55;       // 待机系数缓动（秒）：待机开关绝不硬切

  var gazes = [];
  function makeGaze(box) {
    if (!box) return null;
    /* 【v1.4.5】开屏 Logo 的眼珠类名是 .bs-eye，与侧栏/关于页的 .brand-logo-eye 一并支持 */
    var eye = box.querySelector('.brand-logo-eye, .bs-eye');
    if (!eye) return null;
    var g = { box: box, eye: eye, tx: 0, ty: 0, gx: 0, gy: 0, lastMove: 0, idleF: 0 };
    gazes.push(g);
    return g;
  }
  // 供 renderAbout 等动态渲染后调用，把新生成的 Logo 纳入视角跟随
  window.__registerLogoGaze = function (box) { return makeGaze(box); };

  window.addEventListener('mousemove', function (e) {
    for (var i = 0; i < gazes.length; i++) {
      var g = gazes[i];
      var r = g.box.getBoundingClientRect();
      if (!r.width) continue;                  // 隐藏（如未切到「关于软件」）时跳过
      var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      var dx = e.clientX - cx, dy = e.clientY - cy;
      var d = Math.sqrt(dx * dx + dy * dy) || 1;
      var k = Math.min(1, d / REACH);          // 越远看得越偏，封顶
      var nx = dx / d * k, ny = dy / d * k;    // 单位方向 × 强度（|n| ≤ 1）
      var s = r.width / CANVAS;                // 画布像素 → 显示像素
      g.tx = nx * (ny >= 0 ? SLACK.r : SLACK.l) * s;   // 非对称余量限幅，永不转出眼眶
      g.ty = ny * (ny >= 0 ? SLACK.d : SLACK.u) * s;
      g.lastMove = performance.now();
    }
  }, { passive: true });

  var phase = 0, lastT = performance.now();
  function frame(now) {
    var dt = Math.min(0.05, (now - lastT) / 1000); lastT = now;
    // 自动清理已脱离文档的实例（如旧「关于软件」节点被 innerHTML 替换）
    if (gazes.length > 4) gazes = gazes.filter(function (g) { return document.contains(g.box); });
    phase += dt * (Math.PI * 2 / BREATH_PERIOD);
    for (var i = 0; i < gazes.length; i++) {
      var g = gazes[i];
      var r = g.box.getBoundingClientRect();
      if (!r.width) continue;                  // 隐藏时不驱动，避免坐标错乱
      var idleTarget = (now - g.lastMove > IDLE_AFTER * 1000) ? 1 : 0;
      g.idleF += (idleTarget - g.idleF) * (1 - Math.exp(-dt / IDLE_TAU));
      var bx = 0, by = 0;
      if (g.idleF > 0.01) {
        bx = Math.cos(phase) * BREATH_AMP * g.idleF;
        by = Math.sin(phase * 0.8) * BREATH_AMP * 0.5 * g.idleF;
      }
      // 指数缓动 → 吸附 → 死区
      var a = 1 - Math.exp(-dt / GAZE_TAU);
      var wx = g.tx + bx, wy = g.ty + by;
      g.gx += (wx - g.gx) * a;
      g.gy += (wy - g.gy) * a;
      if (Math.abs(g.gx - wx) < SNAP) g.gx = wx;
      if (Math.abs(g.gy - wy) < SNAP) g.gy = wy;
      if (g.idleF < 0.01 && Math.abs(g.gx) < DEAD_ZONE && Math.abs(g.gy) < DEAD_ZONE) { g.gx = 0; g.gy = 0; }
      g.eye.style.setProperty('--gx', g.gx.toFixed(2) + 'px');
      g.eye.style.setProperty('--gy', g.gy.toFixed(2) + 'px');
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  function initGaze() {
    makeGaze(document.getElementById('brandLogo'));
    /* 【v1.4.5】开屏 Logo 眼珠同样接入视角跟随；开屏层被 hideBootLoading 移除后，
       frame 循环的 document.contains 清理逻辑会自动把该实例摘除。 */
    makeGaze(document.querySelector('#bootSplash .bs-logo'));
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initGaze);
  } else {
    initGaze();
  }
})();
