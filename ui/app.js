/* 微博bot小助手 前端逻辑（原生 JS，通过 window.pywebview.api 调用后端） */
"use strict";

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

/* ---------------- 初始化 ---------------- */
async function init() {
  API = window.pywebview.api;
  await loadState();
  bindNav();
  bindDataOverviewTabs();
  renderAbout();
  setInterval(updateWhBar, 30000);   // 工作时间条实时更新
  setInterval(monAutoTick, 15000);    // 监控列表定时刷新
  setInterval(refreshOverviewLive, 1000);  // 概览界面每秒自动刷新
  setInterval(updateOverviewClock, 1000);  // 概览时钟每秒刷新
  updateOverviewClock();
  syncLogVerbose();
}

/* 同步「冗余调试日志」开关的当前状态 */
async function syncLogVerbose() {
  try {
    const res = await apiCall("get_log_verbose");
    const el = document.getElementById("logVerbose");
    if (el && res) el.checked = !!res.verbose;
  } catch (e) { /* 忽略：日志页面尚未渲染 */ }
}

async function loadState() {
  STATE = await apiCall("get_state");
  document.getElementById("verLabel").textContent = STATE.version.major;
  const copyLabel = document.getElementById("copyLabel");
  copyLabel.textContent = STATE.copyright;
  copyLabel.onclick = () => apiCall("open_external", "https://github.com/zouzuo1994321/Weibo_Bot");
  renderOverview();
  renderAccounts();
  renderMonitors();
  renderForward();
  renderAI();
  renderBlacklist();
  renderSchedule();
  renderLicense();
  renderMonAutoRefresh();
}

function refreshAll() { loadState().then(() => toast("已刷新")); }

/* ---------------- 导航 ---------------- */
function bindNav() {
  document.querySelectorAll(".nav-item").forEach(el => {
    el.onclick = () => {
      document.querySelectorAll(".nav-item").forEach(n => n.classList.remove("active"));
      document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
      el.classList.add("active");
      document.getElementById("view-" + el.dataset.view).classList.add("active");
      if (el.dataset.view === "history") switchHist("monitored");
      if (el.dataset.view === "dataoverview") loadDataOverview();
      if (el.dataset.view === "license") renderLicense();
      if (el.dataset.view === "logs") loadLogs();
      if (el.dataset.view === "schedule") updateWhBar();
      if (el.dataset.view === "media") loadMediaPage();
    };
  });
}

/* ---------------- 概览 ---------------- */
function renderOverview() {
  updateOverviewClock();
  const s = STATE.stats;
  const grid = document.getElementById("statGrid");
  grid.innerHTML = [
    ["监控对象", STATE.monitors.length],
    ["已记录账户", STATE.accounts.length + "/5"],
    ["本日抓取", s.monitored_posts_today || 0],
    ["本日转发次数", s.forwards_today || 0],
  ].map(([l, n]) => `<div class="stat"><div class="num">${n}</div><div class="lbl">${l}</div></div>`).join("");

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
  const ov = document.getElementById("view-overview");
  if (!ov || !ov.classList.contains("active")) return;
  try {
    const st = await apiCall("get_state");
    if (st) { STATE = st; renderOverview(); }
  } catch (e) { /* 忽略瞬时错误，下一秒重试 */ }
}

/* ---------------- 账户 ---------------- */
function accountStatusTag(a) {
  const s = a.status || "unknown";
  const detail = a.check_detail ? ` title="${esc(a.check_detail)}"` : "";
  if (s === "ok") return `<span class="tag green"${detail}>正常</span>`;
  if (s === "cookie_expired") return `<span class="tag red"${detail}>Cookie失效</span>`;
  if (s === "no_repost_perm") return `<span class="tag yellow"${detail}>无转发权限</span>`;
  return `<span class="tag blue"${detail}>未检测</span>`;
}

function renderAccounts() {
  const list = STATE.accounts;
  document.getElementById("accCount").textContent = list.length;
  const tb = document.getElementById("accList");
  if (!list.length) { tb.innerHTML = `<tr><td colspan="6" class="empty">暂无账户，请先登录并添加</td></tr>`; return; }
  tb.innerHTML = list.map(a => `
    <tr>
      <td>${esc(a.display_name || a.remark || "未命名账户")}</td>
      <td class="muted">${esc(a.created_at)}</td>
      <td class="muted">${esc(a.last_used || "—")}</td>
      <td>${accountStatusTag(a)}</td>
      <td class="muted">${esc(a.last_check || "—")}</td>
      <td><button class="small" onclick="checkAccount('${a.id}')">检测</button> <button class="small danger" onclick="removeAccount('${a.id}')">删除</button></td>
    </tr>`).join("");
}

async function checkAccount(id) {
  toast("正在检测该账户…");
  const r = await apiCall("check_account", id);
  if (!r.ok) { toast(r.error, "err"); return; }
  const label = { ok: "登录正常，可转发", cookie_expired: "Cookie 已失效", no_repost_perm: "无转发权限", unknown: "未知" };
  toast(`检测：${label[r.status] || r.status}（${r.message}）`, r.status === "ok" ? "ok" : "err");
  await loadState();
}
async function checkAllAccounts() {
  toast("正在检测全部账户…");
  const r = await apiCall("check_all_accounts");
  if (r.ok) { toast("全部账户检测完成", "ok"); await loadState(); }
  else toast(r.error || "检测失败", "err");
}

async function addAccount() {
  const remark = document.getElementById("accName").value.trim();
  const cookie = document.getElementById("accCookie").value.trim();
  if (!cookie) { toast("Cookie 不能为空", "err"); return; }
  const r = await apiCall("add_account", remark, cookie);
  if (r.ok) { toast("账户已添加", "ok"); document.getElementById("accName").value = ""; document.getElementById("accCookie").value = ""; await loadState(); }
  else toast(r.message, "err");
}

async function removeAccount(id) {
  await apiCall("remove_account", id);
  toast("已删除账户"); await loadState();
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

function renderMonPage() {
  const total = MON_FILTERED.length;
  const cEl = document.getElementById("monCount");
  if (cEl) cEl.textContent = total;
  const box = document.getElementById("monList");
  if (!total) {
    const isFilter = MON_FILTERS.keyword || MON_FILTERS.status !== "all" || MON_FILTERS.pinnedOnly;
    box.innerHTML = `<div class="empty">${isFilter ? "没有符合筛选条件的监控对象" : "暂无监控对象，请在上方添加微博 UID"}</div>`;
    renderMonPagination();
    return;
  }
  const start = (MON_PAGE - 1) * MON_PAGE_SIZE;
  const pageRows = MON_FILTERED.slice(start, start + MON_PAGE_SIZE);
  MON_PAGE_ROWS = pageRows;
  box.innerHTML = pageRows.map(renderMonCard).join("");
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
  if (r && r.ok) { await loadState(); }
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
    </div>
    <div class="page-jump-group" title="跳转到指定页">
      <input id="monJumpPageNum" type="number" min="1" max="${totalPages}" placeholder="页码">
      <button class="small ghost" onclick="jumpMonPage()">GO</button>
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
function jumpMonPage() {
  const input = document.getElementById("monJumpPageNum");
  if (!input) return;
  const totalPages = Math.max(1, Math.ceil(MON_FILTERED.length / MON_PAGE_SIZE));
  let n = parseInt(input.value, 10);
  if (!n || n < 1) { toast("请输入有效的页码", "err"); return; }
  n = Math.max(1, Math.min(totalPages, n));
  MON_PAGE = n;
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
  if (r.ok) { toast("已添加：" + (r.screen_name || uid), "ok"); document.getElementById("monUid").value = ""; await loadState(); }
  else toast(r.message, "err");
}
async function removeMonitor(uid) { await apiCall("remove_monitor", uid); toast("已移除"); await loadState(); }
async function refreshMonitors() {
  toast("正在刷新并判定监控对象…");
  const r = await apiCall("refresh_monitors");
  if (r.ok) {
    const lines = (r.details || []).map(d =>
      `${d.uid}：${d.ok ? "在线" : "异常(" + esc(d.detail) + ")"}`);
    const msg = r.summary || `刷新完成：在线 ${r.updated} / 异常 ${r.error_count}`;
    toast(msg, r.error_count ? "err" : "ok");
    if (lines.length) console.log("监控判定：\n" + lines.join("\n"));
    await loadState();
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
  await loadState();
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
      if (r.ok) loadState();
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
  const eng = document.getElementById("aiEngine");
  if (eng) eng.value = c.ai_engine || "rule";
  refreshAiDeploy();
}
async function saveInterval() {
  const v = parseInt(document.getElementById("intervalMin").value, 10);
  if (!v || v < 1) { toast("间隔需大于0", "err"); return; }
  await apiCall("save_config", JSON.stringify({ interval_minutes: v }));
  toast("间隔已保存", "ok"); await loadState();
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
  toast("转发设置已保存", "ok"); await loadState();
}
async function saveForwardMode() {
  const ai = document.getElementById("aiSwitch").checked;
  const normal = document.getElementById("normalSwitch").checked;
  if (!ai && !normal) { toast("请至少选择一种转发方式", "err"); return; }
  await apiCall("save_config", JSON.stringify({ ai_enabled: ai }));
  toast("转发设置已保存", "ok"); await loadState();
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
  await loadState();
}

/* ---------------- AI 引擎路线 + 部署进度 ---------------- */
async function saveAiEngine() {
  const eng = document.getElementById("aiEngine").value;
  const r = await apiCall("set_ai_engine", eng);
  if (r && r.ok) {
    toast("AI 引擎路线已保存", "ok");
    if (eng === "model") toast("已触发模型下载（见下方部署进度）", "ok");
    await loadState();
  } else {
    toast((r && r.error) || "保存失败", "err");
  }
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

  // 标题右侧：当前引擎与状态
  const aiOn = STATE.config.ai_enabled;
  const eng = STATE.config.ai_engine || "rule";
  const engLabel = eng === "model"
    ? "本地大模型 (llama.cpp · Qwen2.5-1.5B)"
    : "轻量规则引擎 (jieba)";
  const statusEl = document.getElementById("aiModelStatus");
  if (statusEl) {
    statusEl.innerHTML =
      `引擎：<b>${engLabel}</b>` +
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
  toast("人格已保存", "ok"); await loadState();
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
    await loadState();
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
    await loadState();
  } else toast(r.error || "删除失败", "err");
}

/* ---------------- 黑名单 ---------------- */
let blTemp = [];
function renderBlacklist() {
  blTemp = (STATE.config.blacklist || []).slice();
  drawChips();
}
function drawChips() {
  const box = document.getElementById("blChips");
  box.innerHTML = blTemp.length ? blTemp.map((w, i) =>
    `<span class="chip">${esc(w)}<button onclick="delBl(${i})">×</button></span>`).join("")
    : '<span class="muted">暂无关键词</span>';
}
function addBlacklist() {
  const v = document.getElementById("blInput").value.trim();
  if (!v) return;
  if (!blTemp.includes(v)) blTemp.push(v);
  document.getElementById("blInput").value = "";
  drawChips();
}
function delBl(i) { blTemp.splice(i, 1); drawChips(); }
async function saveBlacklist() {
  await apiCall("save_config", JSON.stringify({ blacklist: blTemp }));
  toast("黑名单已保存", "ok"); await loadState();
}

/* ---------------- 工作时间 ---------------- */
function _toMin(hhmm) {
  const [h, m] = (hhmm || "0:0").split(":").map(Number);
  return (h || 0) * 60 + (m || 0);
}
function renderSchedule() {
  const w = STATE.config.working_hours;
  document.getElementById("whSwitch").checked = !!w.enabled;
  document.getElementById("whStart").value = w.start;
  document.getElementById("whEnd").value = w.end;
  updateWhBar();
}
function updateWhBar() {
  const w = STATE.config.working_hours;
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
  toast("工作时间已保存", "ok"); await loadState();
}

/* ---------------- 调度 ---------------- */
async function startScheduler() {
  const r = await apiCall("start_scheduler");
  if (r.running) toast("自动轮询已启动", "ok"); else toast("启动失败", "err");
  await loadState();
}
async function stopScheduler() {
  await apiCall("stop_scheduler"); toast("已停止"); await loadState();
}
/* 统一的「后台异步轮询 + 进度条」驱动：manualRun 与 forwardRandomNow 共用。
   apiName: 后端接口名（manual_run_async / forward_random_now）；
   startLabel: 进度条初始文案。 */
function runPollWithProgress(apiName, startLabel) {
  const wrap = document.getElementById("pollProgressWrap");
  const fill = document.getElementById("pollProgressBar");
  const label = document.getElementById("pollProgressLabel");
  wrap.style.display = "flex";
  fill.style.width = "0%";
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
        fill.style.width = "100%";
        const res = p.result || {};
        if (res.ok) {
          if (res.forwarded) label.textContent = "已转发 · " + (res.message || "本轮转发 1 条");
          else label.textContent = "完成 · " + (res.message || "");
        } else {
          label.textContent = "完成 · " + (res.error || res.message || "无结果");
        }
        setTimeout(() => { wrap.style.display = "none"; }, 1800);
        loadState();  // 刷新概览（含「最近一次轮询结果」）
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
    <div class="page-jump-group" title="跳转到指定页">
      <input id="jumpPageNum" type="number" min="1" max="${totalPages}" placeholder="页码">
      <button class="small ghost" onclick="jumpToPage()">GO</button>
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

function jumpToPage() {
  const input = document.getElementById("jumpPageNum");
  if (!input) return;
  const totalPages = Math.max(1, Math.ceil(HIST_FILTERED.length / HIST_PAGE_SIZE));
  let n = parseInt(input.value, 10);
  if (!n || n < 1) { toast("请输入有效的页码", "err"); return; }
  n = Math.max(1, Math.min(totalPages, n));
  HIST_PAGE = n;
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

async function exportFiltered() {
  const fmt = prompt("导出格式：输入 csv / json / xlsx", "csv");
  if (!fmt) return;
  _readHistFilters();
  const r = await apiCall("export_filtered", histKind, fmt, JSON.stringify(HIST_FILTERS));
  if (r.ok) toast("已导出：" + r.path, "ok"); else toast(r.error || "导出失败", "err");
}
async function exportSelected() {
  if (HIST_SELECTED.size === 0) { toast("请先勾选要导出的记录", "err"); return; }
  const fmt = prompt("导出选中记录：输入 csv / json / xlsx", "csv");
  if (!fmt) return;
  const ids = JSON.stringify([...HIST_SELECTED]);
  const r = await apiCall("export_selected", histKind, ids, fmt);
  if (r.ok) toast(`已导出 ${r.count} 条：${r.path}`, "ok"); else toast(r.error || "导出失败", "err");
}
async function exportAll() {
  const r = await apiCall("export_all_data");
  if (r.ok) toast("已导出全部数据：" + r.path, "ok"); else toast(r.error || "导出失败", "err");
}
async function exportKind(kind) {
  const fmt = prompt("导出格式：输入 csv / json / xlsx", "csv");
  if (!fmt) return;
  const r = await apiCall("export_data", kind, fmt);
  if (r.ok) toast("已导出：" + r.path, "ok"); else toast(r.error || "导出失败", "err");
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
      <img src="logo.png" class="about-logo" alt="logo">
      <div class="about-hero-info">
        <div class="about-name">${esc(STATE.app)}</div>
        <div class="about-version">${esc(STATE.version.display)}</div>
        <div class="about-copy">${esc(STATE.copyright)}</div>
      </div>
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
    await loadState();
    const st = await apiCall("get_license_status");
    if (st.ok) { window.__licFeature = st.feature_code; window.__licValidUntil = st.feature_code_valid_until; }
  } else toast(r.error || "激活失败", "err");
}

async function deactivateLicense() {
  if (!confirm("确定清除当前授权？清除后若试用已结束将无法转发。")) return;
  const r = await apiCall("deactivate_license");
  if (r.ok) { toast("已清除授权", "ok"); await loadState(); }
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
  await loadState();
  renderMonitors();
}

async function importMonitorsFile() {
  const res = await apiCall("import_monitors_from_file");
  if (res && res.ok) {
    toast(`导入完成：新增 ${res.added} · 跳过 ${res.skipped} · 失败 ${res.failed}`);
    await loadState();
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
  box.innerHTML = `<div class="media-obj-grid">` + list.map(o => `
    <div class="media-obj-card" onclick="toggleMediaObject('${escAttr(o.uid)}')">
      <div class="media-obj-name">${esc(o.screen_name || "(无昵称)")}</div>
      <div class="media-obj-uid">UID ${esc(o.uid)}</div>
      <div class="media-obj-counts">相册 <b>${o.images || 0}</b> 张</div>
      <div class="row" style="gap:8px; margin-top:10px;">
        <button class="ghost" onclick="event.stopPropagation(); openMediaDir('${escAttr(o.uid)}')">打开目录</button>
      </div>
      <div id="mediaObj_${escAttr(o.uid)}"></div>
    </div>`).join("") + `</div>`;
}

async function toggleMediaObject(uid) {
  const box = document.getElementById("mediaObj_" + uid);
  if (!box) return;
  if (box.innerHTML.trim()) { box.innerHTML = ""; return; }   // 再次点击收起
  box.innerHTML = `<div class="muted" style="font-size:12px;">加载中…</div>`;
  const rows = await apiCall("get_media_by_uid", uid, "image") || [];
  const imgs = rows;
  let html = "";
  if (imgs.length) {
    html += `<div class="media-subtitle">相册（${imgs.length}）</div><div class="media-thumbs">`
      + imgs.slice(0, 24).map(r =>
        `<img class="media-thumb" data-rel="${escAttr(r.file_path)}" title="${escAttr(r.file_name)}"
              onclick="event.stopPropagation(); previewMedia('${escAttr(r.file_path)}','image')">`).join("")
      + `</div>`;
  }
  if (!html) html = `<div class="muted" style="font-size:12px;">该对象暂无相册图片</div>`;
  box.innerHTML = html;
  // 异步加载图片缩略图（避免一次性阻塞）
  box.querySelectorAll("img.media-thumb").forEach(async (img) => {
    const r = await apiCall("load_media_base64", img.dataset.rel);
    if (r && r.ok) img.src = r.data_url;
    else img.alt = "×";
  });
}

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
if (window.pywebview) {
  init();
} else {
  window.addEventListener("pywebviewready", init);
}
