/* 微博bot小助手 前端逻辑（原生 JS，通过 window.pywebview.api 调用后端） */
"use strict";

let API = null;
let STATE = null;
let histKind = "monitored";
let HIST_SELECTED = new Set();   // 历史记录选中的行 id
let MON_AUTO_TIMER = null;       // 监控自动刷新定时器
let MON_LAST_REFRESH = 0;

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
  renderAbout();
  setInterval(updateWhBar, 30000);   // 工作时间条实时更新
  setInterval(monAutoTick, 15000);    // 监控列表定时刷新
  setInterval(refreshOverviewLive, 1000);  // 概览界面每秒自动刷新
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
      if (el.dataset.view === "license") renderLicense();
      if (el.dataset.view === "logs") loadLogs();
      if (el.dataset.view === "schedule") updateWhBar();
    };
  });
}

/* ---------------- 概览 ---------------- */
function renderOverview() {
  const s = STATE.stats;
  const grid = document.getElementById("statGrid");
  grid.innerHTML = [
    ["监控对象", STATE.monitors.length],
    ["已记录账户", STATE.accounts.length + "/5"],
    ["本日抓取", s.monitored_posts_today || 0],
    ["转发次数", s.forwards],
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
  document.getElementById("lastResult").textContent =
    sch.last_result ? JSON.stringify(sch.last_result, null, 2) : "尚未运行";
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
  renderMonPage();
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
  renderMonPage();
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
}
async function saveInterval() {
  const v = parseInt(document.getElementById("intervalMin").value, 10);
  if (!v || v < 1) { toast("间隔需大于0", "err"); return; }
  await apiCall("save_config", JSON.stringify({ interval_minutes: v }));
  toast("间隔已保存", "ok"); await loadState();
}
async function saveForwardMode() {
  const ai = document.getElementById("aiSwitch").checked;
  await apiCall("save_config", JSON.stringify({ ai_enabled: ai }));
  toast("转发设置已保存", "ok"); await loadState();
}

async function forwardRandomNow() {
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

/* ---------------- AI 人格 ---------------- */
let SELECTED_PERSONA = "";

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
  const tpl = document.getElementById("custTpl").value.split("\n").map(s => s.trim()).filter(Boolean);
  if (!name || !tpl.length) { toast("请填写名称与至少一条模板", "err"); return; }
  const r = await apiCall("save_custom_persona", name, JSON.stringify(tpl));
  if (r.ok) {
    toast("自定义人格已保存", "ok");
    document.getElementById("custName").value = "";
    document.getElementById("custTpl").value = "";
    await loadState();
  } else toast(r.error, "err");
}

async function deleteCustom(name) {
  if (!confirm(`确定删除自定义人格「${name}」？`)) return;
  const r = await apiCall("delete_custom_persona", name);
  if (r.ok) {
    if (SELECTED_PERSONA === name) SELECTED_PERSONA = "";
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
async function manualRun() {
  toast("正在轮询一次…");
  const r = await apiCall("run_once");
  toast(r.message || "完成", r.ok ? "ok" : "err");
  await loadState();
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
  document.querySelectorAll(".thumb").forEach(img => {
    img.onclick = () => previewImage(img.dataset.path);
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

/* ---------------- 日志 ---------------- */
async function loadLogs() {
  const rows = await apiCall("get_logs", 500, 0);
  const box = document.getElementById("logView");
  box.textContent = rows.map(r => `[${r.created_at}][${r.level}] ${r.message}`).reverse().join("\n") || "暂无日志";
  box.scrollTop = box.scrollHeight;
}
function clearLogView() { document.getElementById("logView").textContent = ""; }

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


/* ---------------- 启动 ---------------- */
if (window.pywebview) {
  init();
} else {
  window.addEventListener("pywebviewready", init);
}
