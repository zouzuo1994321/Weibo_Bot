# -*- coding: utf-8 -*-
"""
API 桥接层：暴露给前端（JS）调用的接口。
前端通过 window.pywebview.api.<method>(...).then(...) 调用。
"""
import base64
import json
import os
import random
import subprocess
import sys
import time
import webbrowser
from datetime import datetime

from account_manager import get_account_manager
from ai_generator import generate as ai_generate, list_personas
import ai_engine
from config_manager import get_config
from diag_log import diag
from exporter import export, export_all, export_filtered, export_rows
from forward_engine import (run_once, _download_avatar, _download_post_images,
                            manual_run_async, get_poll_progress)
from history_db import get_history_db
from logger import (info, warn, error, debug, read_file_lines,
                    set_verbose, is_verbose, CAT_CORE, CAT_MEDIA)
from monitor_manager import get_monitor_manager
from pathlib import Path
from paths import (APP_NAME, COPYRIGHT, COPYRIGHT_FULL, VERSION_ARCHIVE_DIR,
                   MAJOR_VERSION, BASE_DIR, DATA_DIR,
                   get_media_dir, get_image_dir, get_path_settings,
                   set_data_paths, wizard_state)
from licensemgr import (get_status, generate_feature_code, activate_license,
                      deactivate_license, can_forward, forward_block_reason,
                      FEATURE_CODE_VALID_DAYS)
from scheduler import get_scheduler
from media_engine import (get_media_progress, start_media_download_async,
                          user_media_dir, maybe_run_scheduled_media_download)
from weibo_client import (get_user_info, get_latest_posts, diagnose_account,
                          repost, get_post_detail)
import model_manager

# ---------------- 系统负载采样（v1.4.0） ----------------
# CPU / 内存 / 网络上下行速率由 psutil 提供；GPU 负载（NVIDIA）由 pynvml 提供。
# 后台线程每 2s 采样一次，前端轮询 get_system_load 只读取快照，零阻塞 UI 线程。
import threading as _thr
try:
    import psutil as _psutil
except Exception:
    _psutil = None
try:
    import pynvml as _pynvml
    _pynvml.nvmlInit()
    _NVML_OK = True
except Exception:
    _pynvml = None
    _NVML_OK = False

_SYS_LOAD = {"cpu": 0.0, "mem": 0.0, "net_up": 0.0, "net_down": 0.0,
             "gpu": None, "gpu_name": "", "cpu_temp": None, "gpu_temp": None}
_SYS_LOAD_LOCK = _thr.Lock()
_NET_PREV = {"ts": 0.0, "sent": 0, "recv": 0}
_SYS_LOAD_THREAD_STARTED = False

# ---------------- CPU 温度（v1.4.6） ----------------
# Windows 上 psutil 没有温度接口（sensors_temperatures 仅 Linux / FreeBSD 实现），
# 系统级唯一来源是 WMI 的 ACPI 温度区（单位 0.1K）。wmic 自 Win11 24H2 起已被移除，
# 故用 PowerShell Get-CimInstance 查 root\wmi\MSAcpi_ThermalZoneTemperature，
# 并以 root\cimv2 的 Win32_PerfFormattedData_Counters_ThermalZoneInformation 兜底。
# 单次探测 0.5~1s（首次约 6s 冷启动），绝不能塞进 2s 采样循环里 —— 故到期时另起一次性
# 守护线程执行，成功缓存 30s、失败（台式机常无 ACPI 温度区）退避 15min，绝不反复起进程。
_CPU_TEMP = {"val": None, "next_due": 0.0, "busy": False}
_CPU_TEMP_LOCK = _thr.Lock()
_CPU_TEMP_TTL_OK = 30.0
_CPU_TEMP_TTL_FAIL = 900.0

_PS_CPU_TEMP = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$t=@();"
    "$t+=Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature"
    "|ForEach-Object{$_.CurrentTemperature/10-273.15};"
    "$t+=Get-CimInstance -ClassName Win32_PerfFormattedData_Counters_ThermalZoneInformation"
    "|ForEach-Object{($_.HighPrecisionTemperature/10)-273.15};"
    "$t|ForEach-Object{[math]::Round($_,1)}"
)


def _read_cpu_temp():
    """读 CPU / 主板温度（°C）。无温度区、非 Windows 或查询失败一律返回 None（前端显示「—」）。"""
    if not sys.platform.startswith("win"):
        return None
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_CPU_TEMP],
            capture_output=True, timeout=12,
            # CREATE_NO_WINDOW：GUI 程序里绝不能闪出黑色控制台
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
    except Exception:
        return None
    vals = []
    for tok in (r.stdout or b"").decode("utf-8", "ignore").split():
        try:
            v = float(tok)
        except Exception:
            continue
        # ACPI 常返回 0 或荒诞值；只保留 5~110 的合理区间
        if 5.0 <= v <= 110.0:
            vals.append(v)
    return round(max(vals), 1) if vals else None


def _schedule_cpu_temp_probe():
    """温度到期才探测，且放到独立线程，绝不拖慢 2s 负载采样。"""
    now = time.time()
    with _CPU_TEMP_LOCK:
        if _CPU_TEMP["busy"] or now < _CPU_TEMP["next_due"]:
            return
        _CPU_TEMP["busy"] = True

    def _work():
        try:
            val = _read_cpu_temp()
        except Exception:
            val = None
        with _CPU_TEMP_LOCK:
            _CPU_TEMP["val"] = val
            _CPU_TEMP["busy"] = False
            _CPU_TEMP["next_due"] = time.time() + (
                _CPU_TEMP_TTL_OK if val is not None else _CPU_TEMP_TTL_FAIL)

    _thr.Thread(target=_work, daemon=True).start()


def _get_cpu_temp():
    with _CPU_TEMP_LOCK:
        return _CPU_TEMP["val"]

# ---------------- 账户健康（v1.4.0） ----------------
# 共享健康快照：由后台线程每小时重算，前端轮询只读此缓存，绝不阻塞 UI 线程。
_ACCOUNT_HEALTH = {"level": "empty", "unhealthy": [], "message": "尚未检测", "ts": 0.0}
_ACCOUNT_HEALTH_LOCK = _thr.Lock()
_ACCOUNT_HEALTH_THREAD_STARTED = False
_HEALTH_API_REF = None          # 由 get_account_health() 首次调用时注入 Api 实例


def _account_health_worker():
    """每小时对全部账户做一次真实诊断（登录态 + 真实抓取 + 转发能力）。

    【v1.4.4】巡检结果经 _run_check_all 回写账户持久化状态后，立即用
    _recompute_account_health_now() 重算健康快照 —— 概览徽章与「账户管理」
    页状态永远同源一致，不再出现「账户页正常、徽章尚未检测」的割裂。
    """
    while True:
        try:
            if _HEALTH_API_REF is not None:
                _HEALTH_API_REF._run_check_all()
                _recompute_account_health_now()
                _state_dirty()   # 检测时间/状态已回写，前端各页同步刷新
        except Exception:
            pass
        time.sleep(3600.0)


def _ensure_account_health_thread():
    global _ACCOUNT_HEALTH_THREAD_STARTED
    if _ACCOUNT_HEALTH_THREAD_STARTED or _HEALTH_API_REF is None:
        return
    _ACCOUNT_HEALTH_THREAD_STARTED = True
    _thr.Thread(target=_account_health_worker, daemon=True).start()


# 各账户持久化状态 → 中文原因（与前端 ACC_STATUS_META 一致）
_ACC_STATUS_TEXT = {
    "cookie_expired": "Cookie 失效",
    "risk_control": "被风控",
    "risk": "被风控",
    "network_error": "网络异常",
    "no_repost_perm": "无转发权限",
    "unknown": "未检测",
}


def _build_account_health():
    """【v1.4.4】从账户已持久化的最近检测结果构建健康快照（零网络调用）。

    此前此处在后台线程里对每个账户重新跑一遍网络诊断，导致：
    ① 概览徽章长时间停留在「尚未检测」（诊断慢、线程异常被静默吞掉）；
    ② 与账户管理页刚完成的手动检测结果不一致。
    现改为直接读取账户的持久化状态（手动「检测」/「检测全部」/每小时巡检
    都会经 set_check_result 回写），徽章与账户页状态永远同源、即刻同步。
    """
    try:
        accounts = get_account_manager().list_accounts()
    except Exception as e:
        warn(f"构建账户健康快照失败：{e}", CAT_CORE)
        return {"level": "empty", "unhealthy": [], "message": "尚未检测",
                "ts": time.time()}
    if not accounts:
        # 【v1.4.3】无账户 = 提示级（黄色），故障才是红色
        return {"level": "empty", "unhealthy": [],
                "message": "尚未添加任何账户", "ts": time.time()}
    unhealthy = []
    pending = []
    for acc in accounts:
        st = acc.get("status") or "unknown"
        name = acc.get("display_name", acc["id"])
        if st == "ok":
            continue
        if st == "unknown":
            pending.append(name)
            continue
        unhealthy.append({"name": name,
                          "reasons": [_ACC_STATUS_TEXT.get(st, st)],
                          "status": st})
    if unhealthy:
        level = "crit"
        msg = "发现 %d 个账户异常：" % len(unhealthy) + "；".join(
            u["name"] for u in unhealthy)
    elif pending:
        level = "empty"   # 有账户但从未检测：黄色提示
        msg = "%d 个账户待检测" % len(pending)
    else:
        level = "ok"
        msg = "全部已记录账户状态正常"
    return {"level": level, "unhealthy": unhealthy, "message": msg,
            "ts": time.time()}


def _recompute_account_health_now():
    """【v1.4.4】立即从账户持久化状态重算健康快照（零网络）。

    供 检测/检测全部/添加/删除账户 后调用 —— 徽章随操作即刻更新，
    不必等 60s 轮询或每小时巡检。
    """
    try:
        res = _build_account_health()
    except Exception:
        return
    with _ACCOUNT_HEALTH_LOCK:
        _ACCOUNT_HEALTH.update(res)


def _sample_system_load():
    global _NET_PREV
    if _psutil is None:
        return
    cpu = _psutil.cpu_percent(interval=None)          # 依赖后台线程持续调用累计
    mem = _psutil.virtual_memory().percent
    try:
        net = _psutil.net_io_counters()
    except Exception:
        net = None
    now = time.time()
    up = down = 0.0
    if net is not None and _NET_PREV["ts"] > 0:
        dt = now - _NET_PREV["ts"]
        if dt > 0:
            up = max(0.0, (net.bytes_sent - _NET_PREV["sent"]) / dt)
            down = max(0.0, (net.bytes_recv - _NET_PREV["recv"]) / dt)
    if net is not None:
        _NET_PREV = {"ts": now, "sent": net.bytes_sent, "recv": net.bytes_recv}
    gpu = None
    gpu_name = ""
    gpu_temp = None
    if _NVML_OK:
        try:
            dev = _pynvml.nvmlDeviceGetHandleByIndex(0)
            gpu = float(_pynvml.nvmlDeviceGetUtilizationRates(dev).gpu)
            gpu_name = _pynvml.nvmlDeviceGetName(dev)
            # 【v1.4.6】GPU 温度：NVML 直读核心温度；无 NVIDIA 显卡则保持 None
            gt = _pynvml.nvmlDeviceGetTemperature(
                dev, getattr(_pynvml, "NVML_TEMPERATURE_GPU", 0))
            if gt is not None:
                gpu_temp = float(gt)
        except Exception:
            gpu = None
            gpu_temp = None
    # CPU 温度：到期才另起线程探测（WMI 较慢），此处只读缓存
    _schedule_cpu_temp_probe()
    cpu_temp = _get_cpu_temp()
    with _SYS_LOAD_LOCK:
        _SYS_LOAD["cpu"] = cpu
        _SYS_LOAD["mem"] = mem
        _SYS_LOAD["net_up"] = up
        _SYS_LOAD["net_down"] = down
        _SYS_LOAD["gpu"] = gpu
        _SYS_LOAD["gpu_name"] = gpu_name
        _SYS_LOAD["cpu_temp"] = cpu_temp
        _SYS_LOAD["gpu_temp"] = gpu_temp


def _system_load_worker():
    while True:
        try:
            _sample_system_load()
        except Exception:
            pass
        time.sleep(2.0)


def _ensure_system_load_thread():
    global _SYS_LOAD_THREAD_STARTED
    if not _SYS_LOAD_THREAD_STARTED:
        _SYS_LOAD_THREAD_STARTED = True
        _thr.Thread(target=_system_load_worker, daemon=True).start()

# ---------------- 异步 / 缓存辅助（v1.1.8） ----------------
import ctypes
import winctl  # 窗口控制：Win32 ctypes 直驱，绕过 pywebview 代理（修复 #19 拖动/磁吸、#20 托盘）

_STATE_CACHE = {"ts": 0.0, "data": None, "refreshing": False}
# v1.1.9g：把 TTL 从 2s 提到 5s。get_state 由前端「系统概览」每秒轮询，且
# pywebview 的 js_bridge_call 在 WebView2 的 UI 线程同步执行 —— 每多一次真实重建，
# UI 线程就多卡一次（history.db 若在 NAS 上尤其严重）。5s 缓存让绝大多数轮询直接命中。
_STATE_TTL = 5.0  # 秒

# 【v1.4.3】状态 epoch：变更类接口（add_account / start_scheduler / set_monitor_pinned 等）
# 每次变更 +1。get_state 把它随状态返回，前端凭「epoch >= 目标值」确认拿到的是
# **包含本次变更**的新状态，而不是 TTL 内的旧缓存 —— 这是「操作后界面不刷新」的根治。
_STATE_EPOCH = 0


def _state_dirty():
    """【v1.4.3】变更类操作后调用：立即使状态缓存过期并推进 epoch。

    根因：此前变更接口从不失效 _STATE_CACHE，操作后前端立刻 loadState() 仍命中
    TTL=5s 内的旧缓存 → 界面「点了没反应」，要等概览 5s 轮询 / 监控 15s tick
    才能看到变化，而「账户管理」页根本没有自动刷新。置 ts=0 后下一次 get_state
    会走「返回旧数据 + 后台重建」路径，重建完成的数据 epoch 已推进，
    前端 refreshStateFresh() 凭 epoch 轮询等待，亚秒级即可看到变更结果。
    """
    global _STATE_EPOCH
    _STATE_EPOCH += 1
    _STATE_CACHE["ts"] = 0.0


def _empty_state():
    """冷启动时立即返回的「空壳状态」。

    【v1.1.9k 0010】为根治启动未响应而新增：get_state 在无缓存时不再同步建状态，
    而是后台重建、立即返回本骨架。前端拿到它就能先把界面渲染出来（不空白），
    缓存就绪后再由 refreshOverviewLive 刷新为真实数据。

    注意：config 走 `get_config().to_dict()`（纯内存 + 小 JSON 文件读取，耗时 <5ms），
    这样 renderForward/renderAI/renderSchedule 等直接访问 STATE.config.xxx 的
    渲染函数不会因字段缺失抛异常；真正慢的是 69MB history.db 的查询，那部分
    一律留到后台线程。
    """
    try:
        from version import get_version
        _v = get_version()
    except Exception:
        _v = {"major": "v1.1.9k", "internal": "unknown",
              "display": "v1.1.9k (内部版本未知)"}
    try:
        _cfg = get_config().to_dict()
    except Exception:
        _cfg = {}
    try:
        _personas = list_personas()
    except Exception:
        _personas = []
    try:
        _aie = _ai_engines_state()
    except Exception:
        _aie = {"primary": "builtin", "fallback": "builtin", "labels": {},
                "valid_engines": [], "config": {}, "engines": {}}
    try:
        _dp = get_path_settings()
    except Exception:
        _dp = {}
    return {
        "app": APP_NAME,
        "copyright": COPYRIGHT,
        "copyright_full": COPYRIGHT_FULL,
        "major_version": MAJOR_VERSION,
        "version": _v,
        "config": _cfg,
        "accounts": [],
        "monitors": [],
        "scheduler": {"running": False, "interval_minutes":
                      _cfg.get("interval_minutes", 30),
                      "in_working_hours": False, "last_run": None},
        "stats": {},
        "personas": _personas,
        "license": {},
        "ai_deploy": {},
        "ai_engines": _aie,       # 【v1.2.0】三类 AI 引擎轻量快照
        "data_paths": _dp,        # 【v1.2.0】数据路径设置
        "storage": _storage_state(),   # 【v1.2.2】存储扫描进度（内存快照）
        "wizard": {},             # 【v1.2.2】冷启动时先不给，等后台刷新
        "running_from_unc": RUNNING_FROM_UNC,
        "unc_reason": UNC_REASON,
        "epoch": 0,               # 【v1.4.3】空壳 epoch=0；真实状态由 _build_state 盖当前 epoch
        # 前端据此判断「数据仍在加载」，避免把空壳误当真实数据展示
        "loading": True,
    }


def _detect_unc():
    """检测 BASE_DIR 是否位于网络 / UNC 路径（NAS 部署或网络映射盘）。

    返回 (is_unc, reason)。onefile 打包后 data 落在本地临时盘，不会误报；
    开发态或把源码放在网络盘运行时，可据此向前端提示运行环境风险。
    """
    d = BASE_DIR
    try:
        if d.startswith("\\\\"):
            return True, "UNC 网络路径"
        drive = os.path.splitdrive(d)[0]
        if drive:
            dt = ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")
            if dt == 4:  # DRIVE_REMOTE = 4
                return True, "网络映射盘"
    except Exception:
        pass
    return False, ""


RUNNING_FROM_UNC, UNC_REASON = _detect_unc()


def _ver():
    try:
        from version import get_version
        return get_version()
    except Exception:
        return {"major": MAJOR_VERSION, "internal": "2609040001"}


def _ai_engines_state():
    """【v1.2.0】三引擎的**轻量**状态快照（零网络 I/O）。

    重要：_build_state() 会在 WebView2 的 UI 线程上被调用，
    任何网络探测（Ollama / 云端）都可能阻塞数秒 → 必须避免。
    因此这里只给「零成本」的判定：
      · builtin恒可用；
      · local_gguf 只查模型文件是否存在（os.path，纳秒级）；
      · local / cloud 一律标为「待探测」，由前端进入 AI 页时
        调get_ai_settings() 显式探测（那时已在后台，不卡 UI）。
    """
    try:
        cfg = ai_engine.load_config()
        primary = cfg.get("active_engine", "builtin")
        fallback = cfg.get("fallback_engine", "builtin")
        out = {
            "primary": primary,
            "fallback": fallback,
            "labels": ai_engine.ENGINE_LABELS,
            "valid_engines": list(ai_engine.VALID_ENGINES),
            "config": _mask_secret(cfg),
            "engines": {},
        }
        for e in ai_engine.VALID_ENGINES:
            out["engines"][e] = {
                "engine": e, "label": ai_engine.ENGINE_LABELS[e],
                "available": (e == "builtin"),
                "detail": ("零依赖、始终可用。" if e == "builtin" else "待探测"),
            }
        try:
            import model_manager
            ok = bool(model_manager.model_available())
            out["engines"]["local_gguf"]["available"] = ok
            out["engines"]["local_gguf"]["detail"] = (
                "已就绪" if ok else "模型尚未下载（data/models/）")
        except Exception:
            pass
        return out
    except Exception as e:
        debug(f"AI 引擎快照失败：{e}")
        return {"primary": "builtin", "fallback": "builtin",
                "labels": {}, "valid_engines": [], "config": {},
                "engines": {}}


def _storage_state():
    """storage.get_status() 的安全包装（**零磁盘 I/O**，纯内存字典）。

    _build_state() 跑在 WebView2 的 UI 线程上，所以这里绝不能触发任何
    os.walk —— 真正的遍历由 storage 的后台线程做，UI 线程只读结果。
    """
    try:
        import storage
        return storage.get_status()
    except Exception as e:
        debug("存储状态读取失败：%s" % e)
        return {"running": False, "phase": "idle", "pct": 0,
                "bytes": 0, "files": 0, "results": {}, "dirs": [],
                "total_mb": 0, "total_gb": 0}


def _native_pick_folder(title="选择目录", initial=""):
    """Windows 原生「浏览文件夹」对话框（SHBrowseForFolderW + ctypes）。

    返回用户选中的绝对路径；取消 / 出错返回 ""。

    为什么不用 tkinter：本项目用的 managed Python 3.13 未编译 tkinter，
    `import tkinter` 直接 ModuleNotFoundError（v1.2.3 修复的真实故障）。
    为什么不用 pywebview.create_file_dialog：EdgeChromium 后端未实现该方法，
    且 js_bridge 跑在 WebView2 UI 线程上，同步等模态框有死锁风险。

    实现要点：
      · BIF_NEWDIALOGSTYLE 拿到「新建文件夹」按钮 + 可拖拽调整大小的新版对话框；
      · BIF_EDITBOX 让对话框带一个可编辑路径的编辑框，方便直接粘贴长路径；
      · 回调里响应 BFFM_INITIALIZED 发 BFFM_SETSELECTIONW 定位初始目录
        （SHBrowseForFolderW 本身没有 initialdir 参数，只能这样设）；
      · 回调对象必须由局部变量持有，否则会被 GC 掉导致崩溃。

    【v1.3.0 关键修复 · 「浏览…」点击无反应的真因】
      pywebview 对每个 JS API 调用都新开一个**裸线程**执行
      （webview/util.py `js_bridge_call` → `Thread(target=_call)`），
      该线程**没有做 COM 初始化**；而 SHBrowseForFolderW 是 Shell COM 组件，
      要求调用线程处于 STA COM。未初始化时函数静默返回 NULL——
      代码把它当「用户取消」返回 ""，前端表现为「点了浏览没反应、对话框根本不弹」。
      修复：进入时 CoInitializeEx(APARTMENTTHREADED)，退出时配对 CoUninitialize；
      若线程已被初始化为其他模式（RPC_E_CHANGED_MODE），不拥有引用、不释放。
    """
    _com_owner = False
    try:
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.windll.shell32
        user32 = ctypes.windll.user32
        ole32 = ctypes.windll.ole32
        if not hasattr(shell32, "SHBrowseForFolderW"):
            return ""

        # js 桥裸线程无 COM 上下文——必须先初始化 STA，否则对话框弹不出来
        COINIT_APARTMENTTHREADED = 0x2
        _hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        _com_owner = (_hr >= 0)   # S_OK / S_FALSE 都要配对 CoUninitialize

        BIF_NEWDIALOGSTYLE = 0x0040
        BIF_EDITBOX = 0x0010
        BIF_RETURNONLYFSDIRS = 0x0080
        BFFM_INITIALIZED = 1
        BFFM_SETSELECTIONW = 0x0467      # WM_USER(0x400) + 2
        WM_USER = 0x0400

        class _BROWSEINFO(ctypes.Structure):
            _fields_ = [
                ("hwndOwner", wintypes.HWND),
                ("pidlRoot", ctypes.c_void_p),
                ("pszDisplayName", wintypes.LPWSTR),
                ("lpszTitle", wintypes.LPCWSTR),
                ("ulFlags", wintypes.UINT),
                ("lpfn", ctypes.c_void_p),
                ("lParam", wintypes.LPARAM),
                ("iImage", ctypes.c_int),
            ]

        # 初始目录：只在确实是目录时用，否则让系统用默认位置
        init = ""
        try:
            if initial and os.path.isdir(initial):
                init = os.path.abspath(initial)
        except Exception:
            init = ""

        _CALLBACK = ctypes.WINFUNCTYPE(
            ctypes.c_int, wintypes.HWND, wintypes.UINT,
            wintypes.LPARAM, wintypes.LPARAM)

        def _cb(hwnd, msg, _lp, _data):
            if msg == BFFM_INITIALIZED and init:
                user32.SendMessageW(
                    hwnd, WM_USER + 2, 1, init)
            return 0

        cb = _CALLBACK(_cb)      # 必须持有引用，防止被 GC

        # 主窗口句柄（让对话框成为主窗口的模态子窗口，不会跑到后面去）
        owner = 0
        try:
            from winctl import find_hwnd
            owner = find_hwnd() or 0
        except Exception:
            owner = 0

        display = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        bi = _BROWSEINFO()
        bi.hwndOwner = owner
        bi.pidlRoot = None
        # 注意：字段声明是 LPWSTR，直接赋 create_unicode_buffer 会抛
        # TypeError: incompatible types, c_wchar_Array instance instead of
        # c_wchar_p instance —— v1.2.3 首版就是这么写的，结果异常被外层
        # try 吞掉、对话框根本没弹，表现为「点了浏览没反应」。必须 cast。
        bi.pszDisplayName = ctypes.cast(display, wintypes.LPWSTR)
        bi.lpszTitle = title or "选择目录"
        bi.ulFlags = BIF_NEWDIALOGSTYLE | BIF_EDITBOX | BIF_RETURNONLYFSDIRS
        bi.lpfn = ctypes.cast(cb, ctypes.c_void_p)
        bi.lParam = 0
        bi.iImage = 0

        pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return ""
        try:
            path = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            if shell32.SHGetPathFromIDListW(pidl, path):
                return path.value
            return ""
        finally:
            ctypes.windll.ole32.CoTaskMemFree(ctypes.c_void_p(pidl))
    except Exception as e:
        try:
            warn("_native_pick_folder 失败：%s" % e)
        except Exception:
            pass
        return ""
    finally:
        # 只有本函数真正初始化了 COM 才负责释放；否则（已是其他模式）不动
        if _com_owner:
            try:
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass


def _mask_secret(cfg):
    """把云端 API Key 换成掩码再发给前端（sk-…abcd）。

    前端只拿掩码显示「已配置」，用户留空保存时后端保持原值不变，
    因此明文密钥**永不**出现在 get_state / 导出数据里。
    """
    try:
        import copy
        c = copy.deepcopy(cfg or {})
        key = (c.get("cloud", {}) or {}).get("api_key", "") or ""
        if key:
            c["cloud"]["api_key"] = (key[:3] + "…" + key[-4:]) if len(key) > 10 else "***"
            c["cloud"]["api_key_set"] = True
        else:
            c["cloud"]["api_key"] = ""
            c["cloud"]["api_key_set"] = False
        return c
    except Exception:
        return cfg


class Api:
    def __init__(self):
        self.login_window = None
        self.main_window = None
        # 自动账户健康检测节流：距上次自动检测超过该秒数才再跑
        self._last_auto_check = 0
        self._auto_check_interval = 300
        self._tray = None  # 托盘图标对象（pystray.Icon），由启动器线程赋值  # 5 分钟

    # ---------------- 窗口控制（原生标题栏 + 系统托盘） ----------------
    # v1.1.9i：窗口改为 Windows 原生标题栏，最小化/最大化/关闭/拖动/缩放全部由系统
    # 提供，前端不再有自绘标题栏按钮，故此处只保留「托盘」相关接口。
    def _stop_tray(self):
        try:
            if self._tray is not None:
                self._tray.stop()
                self._tray = None
        except Exception:
            self._tray = None

    def window_to_tray(self):
        """最小化到系统托盘：隐藏主窗口（Win32 直驱，绕过 pywebview 代理）。"""
        try:
            if not winctl.hide():
                return {"ok": False, "error": "主窗口未找到"}
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def restore_from_tray(self):
        """从托盘恢复：显示并前置主窗口（Win32 直驱，绕过 pywebview 代理）。"""
        try:
            if not winctl.show_restore():
                return {"ok": False, "error": "主窗口未找到"}
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def quit_app(self):
        """彻底退出：停托盘并销毁主窗口（托盘菜单「退出」共用）。"""
        self._stop_tray()
        if self.main_window:
            try:
                self.main_window.destroy()
            except Exception:
                pass
        return {"ok": True}

    # ---------------- 启动诊断埋点（v1.5.0 Part2-A） ----------------
    def boot_stage(self, pct=0, stage=""):
        """前端开屏各阶段回传（fire-and-forget）。

        每次启动全程只调用约 10 次，每次只是追加一行 diag 日志（<1ms），
        对 WebView2 UI 线程的开销可以忽略。卡死时能对照看出：
        「后端走到哪一步 / 前端走到哪一步 / 桥是否还活着」。
        """
        diag("BOOT_STAGE", "pct=%s %s" % (pct, stage))
        return {"ok": True}

    def ui_ready(self, pct=100, stage=""):
        """前端真正进入主界面（开屏层淡出）时回传的埋点（只发一次）。

        外部启动测试以 diag.log 出现 UI_READY 为「成功进入主界面」的
        判定标准 —— 不再只看「窗口出现」。
        """
        diag("UI_READY", "pct=%s %s" % (pct, stage))
        return {"ok": True}

    def get_window_pos(self):
        """读取当前窗口逻辑坐标与尺寸（Win32 直驱定位 HWND）。"""
        try:
            p = winctl.get_pos()
            if not p:
                return {"ok": False, "error": "主窗口未找到"}
            return {"ok": True,
                    "x": int(p["x"]),
                    "y": int(p["y"]),
                    "width": int(p["width"]),
                    "height": int(p["height"])}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def snap_window(self):
        """把主窗口吸附到屏幕工作区边缘（贴近 16px 内自动贴合）。

        原生标题栏下系统 Aero Snap 只在拖到最边缘时触发，且会强制改变窗口尺寸；
        这里只做**位置贴合**，保留用户当前窗口大小，符合「边缘磁吸」的直觉。
        前端在拖动结束（或用定时器）时调用即可。
        """
        try:
            return winctl.snap_to_edges()
        except Exception as e:
            return {"ok": False, "error": str(e)}


    # ---------------- 状态 ----------------
    def _build_state(self):
        """真正重建状态字典（含 DB / 文件 I/O，可能较慢）。"""
        cfg = get_config().to_dict()
        acc = get_account_manager().list_accounts()
        mon = get_monitor_manager().list()
        sch = get_scheduler().status()
        db = get_history_db()
        return {
            "app": APP_NAME,
            "copyright": COPYRIGHT,
            "copyright_full": COPYRIGHT_FULL,
            "major_version": MAJOR_VERSION,
            "version": _ver(),
            "config": cfg,
            "accounts": acc,
            "monitors": mon,
            "scheduler": sch,
            "stats": db.stats(),
            "personas": list_personas(),
            "license": get_status(),
            "ai_deploy": model_manager.get_state(),   # 【v1.2.0】llama.cpp 模型部署进度
            # 【v1.2.0】三类 AI 引擎：builtin 内置离线 / local 本地 Ollama /
            #            local_gguf 本地 llama.cpp / cloud 云端在线
            "ai_engines": _ai_engines_state(),
            # 【v1.2.0 / v1.2.2】存储设置：四个大目录均可自定义
            "data_paths": get_path_settings(),
            # 【v1.2.2】存储占用：纯内存状态（真实扫描在后台线程里跑）
            "storage": _storage_state(),
            # 【v1.2.2】首次启动向导状态（done=False 时前端弹向导）
            "wizard": wizard_state(),
            "running_from_unc": RUNNING_FROM_UNC,      # 是否运行于网络 / UNC 路径
            "unc_reason": UNC_REASON,
            "epoch": _STATE_EPOCH,   # 【v1.4.3】盖当前 epoch，前端据此确认拿到了变更后的新状态
        }

    def get_state(self):
        """返回全局状态。

        【关键性能修复 v1.1.9g】pywebview 的 js_bridge_call 会在 WebView2 的 UI 线程
        上「同步」执行本方法。若这里做 DB / 文件 I/O（尤其 history.db 位于 NAS/SMB
        时），UI 线程就会被长时间占住 → Windows 标记「未响应」，标题栏/按钮全部失灵。

        因此改为「缓存优先 + 后台刷新」：
          - 命中缓存（TTL 内）→ 立即返回，零 I/O；
          - 缓存过期但有旧数据 → 立即返回旧数据，同时起后台线程重建；
          - 冷启动（无任何缓存）→ 同步重建一次（仅一次，可接受）。
        """
        # 轻量副作用：自动检测 / 模型自动下载，二者本身都走后台线程
        try:
            self._auto_check_accounts()
        except Exception:
            pass
        try:
            model_manager.maybe_autostart()
        except Exception:
            pass

        _now = time.time()
        cached = _STATE_CACHE["data"]
        age = _now - _STATE_CACHE["ts"]
        if cached is not None:
            # 命中缓存：直接返回
            if age < _STATE_TTL:
                return cached
            # 过期：先返回旧数据，后台异步重建（UI 线程不阻塞）
            self._refresh_state_async()
            return cached

        # ------------------------------------------------------------------
        # 【v1.1.9k 0010 根治「启动 10 次有 9 次未响应」】
        # 冷启动时**绝不在 UI 线程同步建状态**。
        # 旧实现在此处 time.sleep(0.1)*20（阻塞 UI 线程最多 2s），
        # 再退化成同步 _build_state()（含 69MB DB 查询 + 文件 I/O，数秒）——
        # 而这些都发生在 WebView2 的 UI 线程上，Windows 直接判定「未响应」。
        # 「10 次里只有 1 次响应快」正是竞态：只有后台预热恰好先完成的那次才快。
        #
        # 现在：后台建状态，**立即返回空壳**，前端拿到骨架先渲染，
        # 缓存就绪后再走 refreshOverviewLive 刷新真实数据。
        # ------------------------------------------------------------------
        self._refresh_state_async()
        return _empty_state()

    def _refresh_state_async(self):
        """后台线程重建状态缓存（带节流，避免并发重建）。"""
        import threading
        if _STATE_CACHE.get("refreshing"):
            return
        _STATE_CACHE["refreshing"] = True

        def _worker():
            try:
                data = self._build_state()
                _STATE_CACHE["ts"] = time.time()
                _STATE_CACHE["data"] = data
                diag("STATE_CACHE_BUILT")   # 【v1.5.0】诊断：后台状态重建完成
            except Exception as e:
                diag("STATE_BUILD_FAIL", str(e)[:200])
            finally:
                _STATE_CACHE["refreshing"] = False

        threading.Thread(target=_worker, daemon=True).start()

    def get_data_overview(self):
        """为「数据总览」页提供聚合统计与曲线序列。"""
        try:
            return {"ok": True, "data": get_history_db().data_overview()}
        except Exception as e:
            error(f"数据总览查询失败：{e}")
            return {"ok": False, "error": str(e)}

    def get_data_overview_by_date(self, date):
        """按指定日期（YYYY-MM-DD）查询当日抓取 / 转发趋势，供趋势曲线日期选择器使用。"""
        try:
            return {"ok": True, "data": get_history_db().data_overview_date(date)}
        except Exception as e:
            error(f"按日期查询数据总览失败：{e}")
            return {"ok": False, "error": str(e)}

    def export_data_overview(self, fmt="json"):
        """导出整理好的数据总览（JSON/CSV），方便用 AI 进一步分析。"""
        import webview
        try:
            data = get_history_db().data_overview()
            suffix = ".json" if fmt == "json" else ".csv"
            paths = webview.create_file_dialog(
                webview.SAVE_DIALOG,
                file_types=(fmt.upper() + " files", "*" + suffix),
                save_filename=f"data_overview{suffix}")
            if not paths:
                return {"ok": False, "error": "已取消保存"}
            dest = paths if isinstance(paths, str) else paths[0]
            if fmt == "json":
                with open(dest, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            else:
                rows = []
                for scope, vals in data["summary"].items():
                    rows.append({"范围": scope, "抓取": vals["monitored"], "转发": vals["forwarded"]})
                with open(dest, "w", newline="", encoding="utf-8-sig") as f:
                    import csv
                    w = csv.DictWriter(f, fieldnames=["范围", "抓取", "转发"])
                    w.writeheader()
                    w.writerows(rows)
            return {"ok": True, "path": dest}
        except Exception as e:
            error(f"导出数据总览失败：{e}")
            return {"ok": False, "error": str(e)}

    def get_config(self):
        return get_config().to_dict()

    def save_config(self, patch):
        try:
            patch = json.loads(patch) if isinstance(patch, str) else patch
        except Exception:
            return {"ok": False, "error": "配置格式错误"}
        get_config().update(patch)
        info(f"配置已更新：{list(patch.keys())}")
        _state_dirty()   # 【v1.4.3】配置改动立即反映到各页
        return {"ok": True}

    # ---------------- 系统负载（v1.4.0） ----------------
    def get_system_load(self):
        """返回最近的系统负载快照（CPU/内存/网络上下行/GPU）。
        数据由后台线程持续采样，本方法只读取共享字典，绝不阻塞 UI 线程。"""
        _ensure_system_load_thread()
        with _SYS_LOAD_LOCK:
            d = dict(_SYS_LOAD)
        return {
            "cpu": round(float(d["cpu"]), 1),
            "mem": round(float(d["mem"]), 1),
            "net_up": float(d["net_up"]),
            "net_down": float(d["net_down"]),
            "gpu": (None if d["gpu"] is None else round(float(d["gpu"]), 1)),
            "gpu_name": d["gpu_name"] or "",
            # 【v1.4.6】温度：None 表示本机取不到（无 GPU / 无 ACPI 温度区），前端显示「—」
            "cpu_temp": (None if d["cpu_temp"] is None else round(float(d["cpu_temp"]), 1)),
            "gpu_temp": (None if d["gpu_temp"] is None else round(float(d["gpu_temp"]), 1)),
        }

    # ---------------- 账户 ----------------
    def list_accounts(self):
        return get_account_manager().list_accounts()

    def _auto_check_accounts(self):
        """节流式自动检测：仅在距上次检测超过间隔时执行，避免频繁联网。
        放在后台线程，不阻塞 get_state（即不拖慢界面初始化）。"""
        import threading, time
        now = time.time()
        if now - self._last_auto_check < self._auto_check_interval:
            return
        self._last_auto_check = now  # 立即标记，避免重复触发

        def _worker():
            try:
                self._run_check_all()
                # v1.1.4：自动续期 —— 检测合格后，对临近失效的账户滑动续期（会话保活）
                if get_config().get("auto_renew_cookie", True):
                    renewed = self.auto_renew_accounts()
                    if renewed:
                        info(f"自动续期完成 · 已延长 {len(renewed)} 个账户", CAT_CORE)
            except Exception as e:
                warn(f"自动账户检测异常：{e}")

        threading.Thread(target=_worker, daemon=True).start()

    def _probe_uid(self):
        """取一个用于「真实抓取探测」的 UID（优先监控列表首个对象）。

        v1.1.1：账户检测必须包含真实抓取探测 —— 只有存在 XSRF-TOKEN 而 SUB 已失效时，
        登录接口可能给出假阳性，唯有真正抓一次数据才能确认账户可用。
        """
        try:
            mons = get_monitor_manager().list()
            if mons:
                return mons[0].get("uid", "")
        except Exception:
            pass
        return ""

    def _persist_diag(self, am, acc_id, diag):
        """把诊断结果（状态 / 昵称 / 自身 UID / 续期 Cookie）写回账户。"""
        wb = ""
        login = diag.get("login")
        if isinstance(login, dict):
            wb = login.get("screen_name", "")
        am.set_check_result(acc_id, diag["status"], diag["message"],
                            wb or None, diag.get("self_uid") or None)
        # 会话保活：把续期后的 Cookie 写回账户
        if diag.get("renewed_cookie"):
            if am.update_cookie(acc_id, diag["renewed_cookie"],
                                "检测时已自动续期 Cookie（会话保活）"):
                debug(f"账户 {acc_id} Cookie 已自动续期", CAT_CORE)
        return diag

    def _run_check_all(self):
        am = get_account_manager()
        probe_uid = self._probe_uid()
        for acc in am.list_accounts():
            cookie = am.get_cookie(acc["id"])
            if not cookie:
                am.set_check_result(acc["id"], "unknown", "无 Cookie")
                continue
            # v1.1.3：带上已缓存的账户自身 UID，省掉一次首页解析
            diag = diagnose_account(cookie, probe_uid=probe_uid,
                                    self_uid=acc.get("wb_uid", ""))
            self._persist_diag(am, acc["id"], diag)

    def check_account(self, acc_id):
        """手动检测单个账户：登录态 + **真实抓取能力** + 转发能力，并回填微博昵称。

        v1.1.1：新增真实抓取探测（与轮询同一代码路径），修复「检测正常却抓不到数据」
        的假阳性 —— 此前仅凭 XSRF-TOKEN 存在就判定「登录正常、可转发」。
        """
        am = get_account_manager()
        cookie = am.get_cookie(acc_id)
        if not cookie:
            return {"ok": False, "error": "该账户没有可用 Cookie"}
        acc = next((a for a in am.list_accounts() if a["id"] == acc_id), None)
        diag = diagnose_account(cookie, probe_uid=self._probe_uid(),
                                self_uid=(acc or {}).get("wb_uid", ""))
        self._persist_diag(am, acc_id, diag)
        info(f"账户检测 {acc_id}：{diag['status']} - {diag['message']}")
        # 逐步诊断信息写入日志，便于在「运行日志」页定位判定依据（HTTP 状态码 + 响应片段）
        for s in (diag.get("login") or {}).get("steps") or []:
            debug(f"账户检测 {acc_id} 步骤 · {s}", CAT_CORE)
        _state_dirty()   # 【v1.4.3】检测结果（状态/昵称）已回写，立即失效状态缓存
        _recompute_account_health_now()   # 【v1.4.4】徽章即刻同步，不再停留「尚未检测」
        return {"ok": True, "status": diag["status"], "message": diag["message"],
                "login": diag["login"], "repost": diag["repost"],
                "probe": diag.get("probe")}

    def check_all_accounts(self):
        """手动检测全部账户。"""
        self._run_check_all()
        accs = get_account_manager().list_accounts()
        summary = {a["display_name"]: a["status"] for a in accs}
        info(f"已检测全部账户：{summary}")
        _state_dirty()   # 【v1.4.3】
        _recompute_account_health_now()   # 【v1.4.4】徽章即刻同步
        return {"ok": True, "accounts": summary}

    # ---------------- 账户健康（v1.4.0） ----------------
    def _compute_account_health(self):
        """【v1.4.4】账户健康快照：读取账户持久化状态（零网络，详见 _build_account_health）。

        每小时一次的真实网络巡检（登录态 + 真实抓取 + 转发能力）由后台线程
        _account_health_worker 经 _run_check_all 完成，结果回写账户后快照自动同步。
        """
        return _build_account_health()

    def get_account_health(self):
        """返回最近一次账户健康快照（前端轮询用，1h 过期则异步重算）。

        本方法只读取共享字典，绝不阻塞 UI 线程；首次调用注入 Api 实例并启动
        每小时重算后台线程，满足「每小时检测已记录账户状态」的需求。
        """
        global _HEALTH_API_REF
        _HEALTH_API_REF = self
        _ensure_account_health_thread()
        with _ACCOUNT_HEALTH_LOCK:
            d = dict(_ACCOUNT_HEALTH)
        now = time.time()
        if d.get("ts", 0) == 0 or now - d["ts"] > 3600.0:
            # 首次或已过期：后台异步重算，本次先返回旧快照
            _thr.Thread(target=self._refresh_account_health_async,
                        daemon=True).start()
        return d

    def _refresh_account_health_async(self):
        try:
            res = self._compute_account_health()
            with _ACCOUNT_HEALTH_LOCK:
                _ACCOUNT_HEALTH.update(res)
        except Exception:
            pass

    # ---------------- Cookie 续期（v1.1.4） ----------------
    def _renew_account_cookie(self, am, acc):
        """对单个账户做一次会话预热（滑动续期），成功则回写续期后的 Cookie。

        返回 True 表示 Cookie 确有变化并已落盘；否则返回 False。
        """
        acc_id = acc["id"]
        cookie = am.get_cookie(acc_id)
        if not cookie:
            return False
        try:
            from weibo_client import warm_session
            w = warm_session(cookie, acc.get("wb_uid", ""))
        except Exception as e:
            warn(f"账户 {acc_id} 续期预热异常：{e}", CAT_CORE)
            return False
        if not w.get("ok"):
            return False
        # 只要预热成功就记录一次续期时间，避免短时内重复打扰微博接口
        am.set_last_renew(acc_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        if w.get("cookie") and w["cookie"] != cookie:
            am.update_cookie(acc_id, w["cookie"],
                             "会话预热滑动续期（Cookie 已自动延长）")
            debug(f"账户 {acc_id} Cookie 已滑动续期", CAT_CORE)
            return True
        return False

    def auto_renew_accounts(self):
        """自动续期：对「预测剩余有效期 <= 7 天」且「距上次续期 > 12 小时」的账户滑动续期。

        仅对仍处登录态（cookie 含 SUB）的账户生效；续期即访问一次网页端触发 Set-Cookie，
        延长 ALF/SUBP，从而减少用户必须手动重新登录的次数。
        """
        am = get_account_manager()
        renewed = []
        threshold = 7 * 86400
        min_interval = 12 * 3600
        now = time.time()
        for acc in am.list_accounts():
            cookie = am.get_cookie(acc["id"])
            if not cookie or "SUB=" not in cookie:
                continue   # 缺少登录凭证，续期无意义
            try:
                from weibo_client import predict_cookie_expiry
                from account_manager import _ts_from_str
                exp = predict_cookie_expiry(cookie, last_ok_at_ts=_ts_from_str(acc.get("last_ok_at", "")))
            except Exception:
                continue
            if not exp.get("has_expiry"):
                continue
            remaining = exp.get("remaining_seconds", 1e12)
            if remaining > threshold:
                continue   # 还很充裕，不必续期
            # 节流：距上次续期不足 12 小时则跳过
            last_renew = acc.get("last_renew", "")
            if last_renew:
                try:
                    lr = datetime.strptime(last_renew,
                                           "%Y-%m-%d %H:%M:%S").timestamp()
                    if now - lr < min_interval:
                        continue
                except Exception:
                    pass
            if self._renew_account_cookie(am, acc):
                renewed.append(acc["display_name"])
        return renewed

    def renew_account(self, acc_id):
        """手动续期单个账户：做一次会话预热并回写续期后的 Cookie。"""
        am = get_account_manager()
        acc = next((a for a in am.list_accounts() if a["id"] == acc_id), None)
        if not acc:
            return {"ok": False, "error": "账户不存在"}
        ok = self._renew_account_cookie(am, acc)
        if ok:
            info(f"账户手动续期成功：{acc['display_name']}")
            _state_dirty()   # 【v1.4.3】续期时间已回写
            return {"ok": True, "message": "续期成功，Cookie 已滑动延长"}
        return {"ok": False,
                "message": "本次未发生变化（或已是最新、续期失败）"}

    def keepAliveAll(self):
        """手动触发全账户双通道会话保活，返回汇总（供前端「立即保活」按钮）。"""
        try:
            return {"ok": True, "data": get_scheduler().keepalive_all()}
        except Exception as e:
            error(f"保活触发失败：{e}")
            return {"ok": False, "error": str(e)}


    def add_account(self, remark, cookie):
        ok, msg = get_account_manager().add_account(remark, cookie)
        if ok:
            info(f"已添加账户：{remark}")
            _state_dirty()   # 【v1.4.3】立即失效状态缓存，前端亚秒级看到新账户
            # 【v1.4.4】健康快照立即重算：新账户「未检测」= 黄色提示，徽章即刻同步
            _recompute_account_health_now()
        else:
            warn(f"添加账户失败：{msg}")
        return {"ok": ok, "message": msg}

    def remove_account(self, acc_id):
        ok = get_account_manager().remove_account(acc_id)
        if ok:
            _state_dirty()
            _recompute_account_health_now()   # 【v1.4.4】删除后徽章即刻同步
        return {"ok": ok}

    def open_system_browser(self):
        webbrowser.open("https://weibo.com/login.php")
        return {"ok": True, "message": "已打开系统浏览器，请登录后复制 Cookie 粘贴到输入框"}

    def open_external(self, url):
        try:
            webbrowser.open(url)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_login_browser(self):
        """内置浏览器登录：打开独立窗口加载微博登录页。"""
        import webview
        # 【v1.4.4】根因修复：微博登录页（login.php → passport SSO 跳转）会触发
        # WebView2 的 NewWindowRequested，而 pywebview 默认 OPEN_EXTERNAL_LINKS_IN_BROWSER=True
        # 时会 webbrowser.open 把该跳转交给系统默认浏览器 —— 表现为「点『内置浏览器登录』
        # 却打开了本机 Edge」。置 False 后新窗口请求一律在当前内嵌窗口内导航，
        # 登录全程留在窗口内，get_cookies() 才能取到登录态 Cookie。
        try:
            webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = False
        except Exception:
            pass
        if self.login_window is not None and self.login_window.gui is not None:
            try:
                self.login_window.show()
                return {"ok": True}
            except Exception:
                pass
        try:
            self.login_window = webview.create_window(
                "微博登录 - 请在窗口内登录",
                url="https://weibo.com/login.php",
                width=1000, height=720,
            )
            return {"ok": True, "message": "已打开内置浏览器，登录后点击『获取Cookie』"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def capture_login_cookies(self):
        """从内置登录窗口读取 Cookie。

        注意：pywebview 的 get_cookies() 返回的每个元素是 http.cookies.SimpleCookie
        （字典子类，键为 cookie 名，值为 Morsel）。Morsel 通过 .key / .value 属性
        取值，不能用 .get('name')（那只会去查名为 'name' 的 cookie，恒为 None）。
        下面兼容 SimpleCookie 与旧版 dict 两种返回结构。
        """
        import webview
        if self.login_window is None:
            return {"ok": False, "error": "请先打开内置浏览器并完成登录"}
        try:
            cookies = self.login_window.get_cookies()
        except Exception as e:
            return {"ok": False, "error": f"读取 Cookie 失败：{e}"}
        if not cookies:
            return {"ok": False, "error": "未读取到 Cookie，请确认已登录"}
        parts = []
        for c in cookies:
            name = value = None
            # 情况一：pywebview（edge/cef 等）返回的 SimpleCookie
            if hasattr(c, "values"):
                for morsel in c.values():
                    name = getattr(morsel, "key", None) or (
                        morsel.get("key") if hasattr(morsel, "get") else None)
                    value = getattr(morsel, "value", None) or (
                        morsel.get("value") if hasattr(morsel, "get") else None)
                    break
            # 情况二：旧版直接返回 dict（含 name/value 字段）
            elif isinstance(c, dict):
                name = c.get("name") or c.get("Name")
                value = c.get("value") or c.get("Value")
            if name and value:
                parts.append(f"{name}={value}")
        cookie_str = "; ".join(parts)
        if not cookie_str:
            return {"ok": False, "error": "Cookie 解析为空，请确认已成功登录"}
        # 判断是否真正登录（存在 SUB 等关键 cookie）
        logged = any(k in cookie_str for k in ("SUB=", "SUBP=", "SUHB="))
        return {"ok": True, "cookie": cookie_str, "logged_in": logged}

    def close_login_browser(self):
        if self.login_window is not None:
            try:
                self.login_window.destroy()
            except Exception:
                pass
        return {"ok": True}

    # ---------------- 监控 ----------------
    def add_monitor(self, uid):
        uid = str(uid).strip()
        if not uid:
            return {"ok": False, "error": "UID 不能为空"}
        # 尝试用首个账户 cookie 获取昵称与头像
        screen_name = ""
        avatar_local = ""
        accs = get_account_manager().list_accounts()
        if accs:
            from account_manager import get_account_manager as gam
            cookie = gam().get_cookie(accs[0]["id"])
            if cookie:
                info_resp = get_user_info(uid, cookie)
                if info_resp.get("ok"):
                    screen_name = info_resp.get("screen_name", "")
                    avatar_url = info_resp.get("avatar_url", "")
                    if avatar_url:
                        avatar_local = _download_avatar(avatar_url, uid, cookie)
        ok, msg = get_monitor_manager().add(uid, screen_name)
        if ok and avatar_local:
            get_monitor_manager().update(uid, {"avatar_local": avatar_local})
        if ok:
            info(f"已添加监控 UID={uid} 昵称={screen_name}")
            _state_dirty()   # 【v1.4.3】
        else:
            warn(f"添加监控失败：{msg}")
        return {"ok": ok, "message": msg, "screen_name": screen_name, "avatar_local": avatar_local}

    def remove_monitor(self, uid):
        ok = get_monitor_manager().remove(uid)
        if ok:
            _state_dirty()   # 【v1.4.3】
        return {"ok": ok}

    def set_monitor_pinned(self, uid, pinned):
        """置顶 / 取消置顶某个监控对象。pinned 为 bool。"""
        ok = get_monitor_manager().set_pinned(str(uid), bool(pinned))
        if ok:
            info(f"监控对象 UID={uid} 置顶状态已设为 {pinned}")
            _state_dirty()   # 【v1.4.3】置顶状态立即失效缓存，前端免等 TTL
        else:
            warn(f"设置置顶失败：未找到 UID={uid}")
        return {"ok": ok}

    def refresh_monitors(self):
        """刷新并判定所有监控对象：用监控实际调用的接口拉取最新微博，
        成功置 online 并刷新昵称/最新微博/检测时间，失败置 error。"""
        accs = get_account_manager().list_accounts()
        if not accs:
            return {"ok": False, "error": "需要至少一个账户 Cookie 才能刷新"}
        from account_manager import get_account_manager as gam
        acc_id = accs[0]["id"]
        cookie = gam().get_cookie(acc_id)
        mon = get_monitor_manager()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        details = []
        ok_count = err_count = 0
        for it in mon.list():
            uid = it["uid"]
            resp = get_latest_posts(uid, cookie, page=1, count=5)
            # v1.1.1 会话保活：抓取成功时可能带回续期后的 Cookie，写回并沿用
            if resp.get("ok") and resp.get("renewed_cookie"):
                if gam().update_cookie(acc_id, resp["renewed_cookie"],
                                       "刷新监控时已自动续期 Cookie（会话保活）"):
                    debug(f"账户 {acc_id} Cookie 已自动续期", CAT_CORE)
                cookie = resp["renewed_cookie"]
            if resp.get("ok") and resp.get("posts"):
                posts = resp["posts"]
                # 昵称优先从用户信息取，其次用首条微博
                name = it.get("screen_name", "")
                if not name:
                    info_resp = get_user_info(uid, cookie)
                    if info_resp.get("ok") and info_resp.get("screen_name"):
                        name = info_resp["screen_name"]
                # 「最新微博」展示跳过置顶（置顶通常排在最前，非真实新动态）
                non_top = [p for p in posts if not p.get("is_top")]
                display = non_top or posts
                # 刷新头像
                avatar_local = it.get("avatar_local", "")
                info_resp = get_user_info(uid, cookie)
                if info_resp.get("ok"):
                    name = info_resp.get("screen_name", name) or name
                    avatar_url = info_resp.get("avatar_url", "")
                    if avatar_url:
                        avatar_local = _download_avatar(avatar_url, uid, cookie) or avatar_local
                mon.update(uid, {
                    "screen_name": name,
                    "status": "online",
                    "last_post_id": posts[0]["id"],
                    "last_post_text": display[0]["text"][:200],
                    "last_check": now,
                    "avatar_local": avatar_local,
                })
                ok_count += 1
                details.append({"uid": uid, "ok": True,
                                "screen_name": name,
                                "detail": f"在线，最新微博：{display[0]['text'][:30]}"})
            else:
                mon.update(uid, {"status": "error", "last_check": now})
                err_count += 1
                # v1.1.1：失败原因结构化，前端可直接给出「重新登录」等可操作提示
                details.append({"uid": uid, "ok": False,
                                "status": resp.get("status", "error"),
                                "detail": resp.get("reason") or resp.get("error", "拉取失败")})
        info(f"监控刷新完成：{ok_count} 在线 / {err_count} 异常")
        _state_dirty()   # 【v1.4.3】昵称/状态/检测时间已回写，立即失效缓存
        first_err = next((d for d in details if not d["ok"]), None)
        summary = (f"在线 {ok_count} / 异常 {err_count}" +
                   (f"；首个异常 {first_err['uid']}：{first_err['detail'][:60]}"
                    if first_err else ""))
        return {"ok": True, "updated": ok_count, "error_count": err_count,
                "details": details, "summary": summary}

    # ---------------- 转发 / 调度 ----------------
    def run_once(self):
        return run_once()

    def manual_run_async(self):
        """后台异步执行一轮完整轮询，轮询完毕后立即转发一条（不保留抖动延迟）。
        前端通过 get_poll_progress 轮询进度与结果；转发内容经 run_once 去重（不重复）。"""
        return manual_run_async(force_immediate=True)

    def get_poll_progress(self):
        """返回当前轮询进度（用于「立即轮询一次」进度条）。"""
        try:
            return get_poll_progress()
        except Exception as e:
            return {"active": False, "error": str(e)}

    def forward_random_now(self):
        """立即转发：在监控列表中随机挑一个对象，转发其最新一条（非置顶）微博。
        手动触发，不依赖轮询增量状态；命中黑名单则跳过。
        （回退至 v2609080004 行为：直接随机挑选并转发，不先轮询。）"""
        am = get_account_manager()
        accs = am.list_accounts()
        if not accs:
            return {"ok": False, "error": "没有可用账户，请先登录"}
        if not can_forward():
            return {"ok": False, "error": forward_block_reason() or "转发功能暂不可用"}
        acc = accs[0]
        cookie = am.get_cookie(acc["id"])
        mons = get_monitor_manager().list()
        if not mons:
            return {"ok": False, "error": "监控列表为空，请先添加监控对象"}

        target = random.choice(mons)
        uid = target["uid"]
        screen_name = target.get("screen_name", "")
        resp = get_latest_posts(uid, cookie, page=1, count=10)
        if not resp.get("ok") or not resp.get("posts"):
            # v1.1.1：区分「Cookie 失效」「风控」「网络异常」，给出可操作提示
            detail = resp.get("reason") or resp.get("error") or "空"
            if resp.get("status") == "cookie_expired":
                detail = "账户 Cookie 已失效，请在「账户管理」删除该账户后重新登录"
            return {"ok": False, "error": f"拉取 UID {uid} 失败：{detail}"}
        posts = [p for p in resp["posts"] if not p.get("is_top")]
        if not posts:
            return {"ok": False, "error": f"UID {uid} 无非置顶微博可转发"}
        chosen = random.choice(posts)

        cfg = get_config()
        ai_on = cfg.get("ai_enabled", False)
        persona = cfg.get("persona", "幽默")
        custom = cfg.get("custom_personas", {}).get(persona)
        comment = ""
        ftype = "normal"
        if ai_on:
            try:
                comment = ai_generate(chosen["text"], persona, custom)
                ftype = "ai"
            except Exception as e:
                warn(f"UID={uid} AI 文案生成失败，降级为普通转发：{e}")
                comment = ""
                ftype = "normal"
        acc_name = acc.get("display_name", "")

        # 关键词黑名单检测
        blacklist = cfg.get("blacklist", []) or []
        haystack = (chosen["text"] + " " + comment).lower()
        hit = [w for w in blacklist if w and w.lower() in haystack]
        db = get_history_db()
        if hit:
            db.add_forward(acc["id"], acc_name, uid, screen_name, chosen["id"],
                           chosen["text"], comment, ftype,
                           "skipped", reason=f"命中黑名单：{','.join(hit)}", persona=persona)
            return {"ok": True, "skipped": True, "uid": uid,
                    "screen_name": screen_name,
                    "detail": f"命中黑名单跳过：{','.join(hit)}"}

        r = repost(chosen["id"], comment, cookie, uid=uid)
        if r.get("ok"):
            db.add_forward(acc["id"], acc_name, uid, screen_name, chosen["id"],
                           chosen["text"], comment, ftype, "success", persona=persona)
            info(f"手动立即转发成功：UID={uid} persona={persona}")
            return {"ok": True, "uid": uid, "screen_name": screen_name,
                    "detail": f"已转发（{ftype}）：{comment or '纯转发'}"}
        db.add_forward(acc["id"], acc_name, uid, screen_name, chosen["id"],
                       chosen["text"], comment, ftype,
                       "failed", reason=r.get("error", "未知错误"), persona=persona)
        error(f"手动立即转发失败：UID={uid} {r.get('error')}")
        return {"ok": False, "error": f"转发失败：{r.get('error', '未知错误')}"}

    def start_scheduler(self):
        ok = get_scheduler().start()
        _state_dirty()   # 【v1.4.3】运行状态立即失效缓存，前端点「启动」徽章马上变「运行中」
        return {"ok": ok, "running": get_scheduler().is_running()}

    def stop_scheduler(self):
        get_scheduler().stop()
        _state_dirty()   # 【v1.4.3】
        return {"ok": True, "running": False}

    def scheduler_status(self):
        return get_scheduler().status()

    # ---------------- AI ----------------
    def list_personas(self):
        return list_personas()

    def preview_ai(self, text, persona, custom=None):
        try:
            custom = json.loads(custom) if isinstance(custom, str) and custom else custom
        except Exception:
            custom = None
        out = ai_generate(text or "这是一条测试微博，关于人工智能和开源软件的发展。",
                          persona or "幽默", custom)
        return {"ok": True, "text": out}

    def save_custom_persona(self, name, desc):
        """保存自定义人格。

        desc 可为：
        - 字符串：新版「人设介绍」自然语言描述（如「你是猫娘，说话软萌」）
        - 列表：旧版模板列表（兼容历史数据，内部归一为文本存储）
        """
        try:
            desc = json.loads(desc) if isinstance(desc, str) else desc
        except Exception:
            desc = desc  # 当作纯文本处理
        if isinstance(desc, (list, tuple)):
            desc = "\n".join(str(t) for t in desc)
        desc = (desc or "").strip()
        if not desc:
            return {"ok": False, "error": "请填写人格介绍"}
        cfg = get_config()
        cp = cfg.get("custom_personas", {}) or {}
        cp[name] = desc
        cfg.set("custom_personas", cp)
        _state_dirty()   # 【v1.4.3】人格列表立即刷新
        return {"ok": True}

    # ---------------- 授权 ----------------
    def get_license_status(self):
        st = get_status()
        valid_until = int(time.time()) + FEATURE_CODE_VALID_DAYS * 86400
        return {"ok": True, "status": st,
                "feature_code": generate_feature_code(),
                "feature_code_valid_until": valid_until,
                "feature_code_valid_days": FEATURE_CODE_VALID_DAYS}

    def regenerate_feature_code(self):
        """重新生成特征码（时间戳 + 随机串变化）。"""
        return {"ok": True, "feature_code": generate_feature_code()}

    def activate_license(self, key):
        r = activate_license(key)
        if r.get("ok"):
            info(f"授权已激活：{r.get('type_name')}（到期 {r.get('exp_str')}）")
            _state_dirty()   # 【v1.4.3】
        else:
            warn(f"授权激活失败：{r.get('error')}")
        return r

    def deactivate_license(self):
        r = deactivate_license()
        if r.get("ok", True):
            _state_dirty()   # 【v1.4.3】
        return r

    # ---------------- 自定义人格 ----------------
    def delete_custom_persona(self, name):
        cfg = get_config()
        cp = cfg.get("custom_personas", {}) or {}
        if name not in cp:
            return {"ok": False, "error": "未找到该自定义人格"}
        del cp[name]
        cfg.set("custom_personas", cp)
        info(f"已删除自定义人格：{name}")
        _state_dirty()   # 【v1.4.3】

    # ------- 旧版二引擎 API（v1.1.x）—— 保留为兼容层，内部映射到三引擎 -------
    # v1.2.0 起引擎标识统一为 builtin / local / local_gguf / cloud；
    # 旧的 "rule" / "model" 在这里做一次翻译，老前端/老脚本不会被打断。
    _LEGACY_ENGINE_MAP = {"rule": "builtin", "model": "local_gguf"}

    def get_ai_engine_status(self):
        r = self.get_ai_settings()
        r["ai_enabled"] = get_config().get("ai_enabled", False)
        r["deploy"] = model_manager.get_state()
        return r

    def set_ai_engine(self, engine):
        """兼容入口：只设「主路线」，备路线不动。"""
        eng = self._LEGACY_ENGINE_MAP.get(engine, engine)
        if eng not in ai_engine.VALID_ENGINES:
            return {"ok": False, "error": "未知引擎路线：%s" % engine}
        cfg = ai_engine.load_config()
        cfg["active_engine"] = eng
        ai_engine.save_config(cfg)
        # 同步老配置项，避免其它仍读 config.json 的代码判断不一致
        try:
            get_config().set("ai_engine",
                             "model" if eng == "local_gguf" else "rule")
        except Exception:
            pass
        info(f"AI 主路线已切换为：{eng}")
        _state_dirty()   # 【v1.4.3】
        if eng == "local_gguf":
            try:
                model_manager.maybe_autostart()
            except Exception as e:
                warn(f"切换后触发模型下载失败：{e}")
        return {"ok": True, "ai_engine": eng}

    def start_model_download(self):
        return model_manager.start_download()

    def get_model_progress(self):
        return model_manager.get_state()

    def open_models_dir(self):
        path = model_manager.ensure_models_dir()
        try:
            os.startfile(path)  # Windows：打开资源管理器
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e), "path": path}
        return {"ok": True}

    # ================= 【v1.2.0】三类 AI 引擎（统一抽象层） =================
    # 引擎：builtin 内置离线 / local 本地 Ollama / local_gguf 本地 llama.cpp
    #      / cloud 云端在线（OpenAI 兼容）
    # 原则：只读增强、绝不静默降级—— 主失败切备，备失败落builtin，全过程留痕。

    def get_ai_settings(self):
        """返回三引擎完整配置 + 状态 + 当前部署进度（llama.cpp 模型）。"""
        try:
            cfg = ai_engine.load_config()
            st = ai_engine.all_status(cfg)
            st["config"] = _mask_secret(cfg)
            return st
        except Exception as e:
            error(f"读取 AI 设置失败：{e}")
            return {"ok": False, "error": str(e)}

    def save_ai_settings(self, active_engine=None, fallback_engine=None,
                         local_host=None, local_model=None, local_auto_start=None,
                         gguf_auto_download=None,
                         cloud_base_url=None, cloud_api_key=None,
                         cloud_model=None):
        """保存 AI 设置。api_key 传空字符串表示「保持原值不变」。

        这样前端不回显明文密钥（只回显掩码），也不会因留空把已有 key 清掉。
        """
        try:
            cfg = ai_engine.load_config()
            if active_engine in ai_engine.VALID_ENGINES:
                cfg["active_engine"] = active_engine
            if fallback_engine in ai_engine.VALID_ENGINES:
                cfg["fallback_engine"] = fallback_engine
            if local_host is not None and str(local_host).strip():
                cfg["local"]["host"] = str(local_host).strip().rstrip("/")
            if local_model is not None:
                cfg["local"]["model"] = str(local_model).strip()
            if local_auto_start is not None:
                cfg["local"]["auto_start"] = bool(local_auto_start)
            if gguf_auto_download is not None:
                cfg["local_gguf"]["auto_download"] = bool(gguf_auto_download)
            if cloud_base_url is not None and str(cloud_base_url).strip():
                cfg["cloud"]["base_url"] = str(cloud_base_url).strip().rstrip("/")
            if cloud_model is not None:
                cfg["cloud"]["model"] = str(cloud_model).strip()
            if cloud_api_key:                      # 空 = 不改
                cfg["cloud"]["api_key"] = str(cloud_api_key).strip()

            if not ai_engine.save_config(cfg):
                return {"ok": False, "error": "配置写入失败，请检查数据目录是否可写"}
            info(f"AI 设置已保存 · 主={cfg['active_engine']} 备={cfg['fallback_engine']}")
            _state_dirty()   # 【v1.4.3】
            # 主路线为local_gguf 且缺模型时，按auto_download 自动开下载
            if (cfg["active_engine"] == "local_gguf"
                    and cfg["local_gguf"].get("auto_download")):
                try:
                    model_manager.maybe_autostart()
                except Exception as e:
                    warn(f"保存后触发模型下载失败：{e}")
            r = self.get_ai_settings()
            r["ok"] = True
            return r
        except Exception as e:
            error(f"保存 AI 设置失败：{e}")
            return {"ok": False, "error": str(e)}

    def probe_ai_engine(self, engine=None, local_host=None, local_model=None,
                        cloud_base_url=None, cloud_model=None,
                        cloud_api_key=None):
        """草稿配置探测：探测设置页**当前控件取值**而非已保存值。

        这样「下拉选了 Ollama、状态却显示 builtin」的错位就不会出现。
        未传的键（None）保持已保存值不变。
        """
        try:
            cfg = ai_engine.load_config()
            if engine in ai_engine.VALID_ENGINES:
                cfg["active_engine"] = engine
            if local_host:
                cfg["local"]["host"] = str(local_host).strip().rstrip("/")
            if local_model is not None:
                cfg["local"]["model"] = str(local_model).strip()
            if cloud_base_url:
                cfg["cloud"]["base_url"] = str(cloud_base_url).strip().rstrip("/")
            if cloud_model is not None:
                cfg["cloud"]["model"] = str(cloud_model).strip()
            if cloud_api_key:
                cfg["cloud"]["api_key"] = str(cloud_api_key).strip()
            # 探测时**不做**自动拉起 Ollama，避免用户点一下就装东西
            cfg["local"]["auto_start"] = False
            st = ai_engine.ai_status(cfg)
            st["ok"] = True
            return st
        except Exception as e:
            error(f"AI 引擎探测失败：{e}")
            return {"ok": False, "error": str(e)}

    def list_local_ai_models(self, host=None):
        """读取本机 Ollama 已安装模型清单。"""
        try:
            cfg = ai_engine.load_config()
            h = str(host).strip().rstrip("/") if host else cfg["local"]["host"]
            models = ai_engine.list_local_models(h)
            return {"ok": True, "host": h, "models": models}
        except Exception as e:
            return {"ok": False, "error": str(e), "models": []}

    def launch_local_ai(self):
        """后台拉起本机 Ollama。"""
        try:
            ok, msg = ai_engine.launch_local()
            return {"ok": ok, "message": msg}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def test_ai_engine(self, engine=None):
        """实测当前引擎：发一句最短prompt 看能否返回文案。"""
        try:
            eng = engine or ai_engine.get_active_engine()
            r = ai_engine.ask("用一句中文打个招呼，10字以内。",
                              max_tokens=32, engine=eng, temperature=0.3)
            r["engine"] = r.get("engine", eng)
            return r
        except Exception as e:
            return {"ok": False, "engine": engine or "", "text": "", "error": str(e)}

    def ai_self_check(self):
        """AI 子系统自检（配置 / 探测 / 生成链路）。"""
        try:
            return ai_engine.self_check()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------------- 历史 / 日志 ----------------
    def get_monitored_posts(self, uid=None, limit=200, offset=0):
        return get_history_db().get_monitored_posts(uid, limit, offset)

    def repair_history_images(self, limit=100):
        """修复历史记录里被抓成表情包的微博图片。

        逐条按微博 ID 重新请求详情接口取 `pic_infos`（真正的推文配图），
        重新下载并覆盖同名文件后写回数据库；纯文本微博则清空其错误图片。
        """
        try:
            from account_manager import get_account_manager
            acc_mgr = get_account_manager()
            accounts = acc_mgr.list_accounts()
            if not accounts:
                return {"ok": False, "error": "没有可用账户，请先登录"}
            cookie = acc_mgr.get_cookie(accounts[0]["id"])

            db = get_history_db()
            try:
                limit = max(1, min(int(limit or 100), 1000))
            except Exception:
                limit = 100
            posts = db.get_monitored_posts(limit=limit)
            fixed = cleaned = failed = 0
            debug(f"开始修复历史图片 · 共 {len(posts)} 条", CAT_CORE)
            for p in posts:
                pid = str(p.get("post_id") or "")
                if not pid:
                    continue
                try:
                    d = get_post_detail(pid, cookie)
                except Exception as e:
                    failed += 1
                    debug(f"修复失败 {pid}：{e}", CAT_CORE)
                    time.sleep(0.5)
                    continue
                if not d.get("ok"):
                    failed += 1
                    time.sleep(0.5)
                    continue
                imgs = d.get("images") or []
                if not imgs:
                    # 详情接口明确无配图 → 清掉此前误存的表情包
                    if p.get("images"):
                        db.update_post_images(pid, [])
                        cleaned += 1
                    time.sleep(0.4)
                    continue
                saved = _download_post_images(imgs, p.get("uid") or "", pid, cookie)
                if saved:
                    db.update_post_images(pid, saved)
                    fixed += 1
                else:
                    failed += 1
                time.sleep(0.6)   # 控速，避免触发风控
            msg = (f"历史图片修复完成 · 重新下载 {fixed} 条 · 清除误存表情 {cleaned} 条"
                   f" · 失败/无权限 {failed} 条")
            info(msg, CAT_CORE)
            return {"ok": True, "fixed": fixed, "cleaned": cleaned,
                    "failed": failed, "total": len(posts), "message": msg}
        except Exception as e:
            error(f"历史图片修复失败：{e}", CAT_CORE)
            return {"ok": False, "error": str(e)}

    def get_forwards(self, limit=200, offset=0):
        return get_history_db().get_forwards(limit, offset)

    def get_logs(self, limit=500, offset=0, level="ALL", category="ALL", keyword=""):
        """查询日志，支持级别 / 模块分类筛选与关键字搜索。"""
        db = get_history_db()
        rows = db.get_logs(limit, offset, level, category, keyword)
        try:
            total = db.count_logs(level, category, keyword)
        except Exception:
            total = len(rows)
        return {"rows": rows, "total": total}

    def read_log_file(self, limit=500):
        return read_file_lines(limit)

    def clear_logs(self):
        """清空运行日志（数据库），并返回是否成功。"""
        try:
            get_history_db().clear_logs()
            info("运行日志已清空", CAT_CORE)
            return {"ok": True}
        except Exception as e:
            error(f"清空日志失败：{e}", CAT_CORE)
            return {"ok": False, "error": str(e)}

    def set_log_verbose(self, flag):
        """开启/关闭冗余调试日志（DEBUG 级，记录 HTTP 细节与每步决策）。"""
        try:
            set_verbose(bool(flag))
            try:
                cfg = get_config()
                cfg.set("log_verbose", bool(flag))
            except Exception:
                pass
            info(f"冗余调试日志已{'开启' if flag else '关闭'}", CAT_CORE)
            return {"ok": True, "verbose": bool(flag)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_log_verbose(self):
        return {"verbose": is_verbose()}

    def export_logs(self, fmt="txt"):
        """导出当前筛选后的日志到用户指定位置。"""
        import webview
        suffix = {"csv": ".csv", "json": ".json", "txt": ".txt"}.get(fmt, ".txt")
        try:
            win = getattr(self, "main_window", None)
            res = win.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=BASE_DIR,
                save_filename=f"weibobot_logs{suffix}",
                file_types=("日志文件 (*.txt;*.csv;*.json)",),
            )
            path = res[0] if isinstance(res, (list, tuple)) and res else (res or "")
            if not path:
                return {"ok": False, "error": "已取消", "path": ""}
            rows = get_history_db().get_logs(limit=100000, offset=0)
            if fmt == "json":
                import json as _json
                with open(path, "w", encoding="utf-8") as f:
                    _json.dump(rows, f, ensure_ascii=False, indent=2)
            elif fmt == "csv":
                import csv
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["id", "level", "message", "created_at"])
                    for r in rows:
                        w.writerow([r["id"], r["level"], r["message"], r["created_at"]])
            else:
                with open(path, "w", encoding="utf-8") as f:
                    for r in rows:
                        f.write(f"[{r['created_at']}][{r['level']}] {r['message']}\n")
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e), "path": ""}

    # ---------------- 导出 ----------------
    def export_blacklist(self, items_json):
        """导出关键词黑名单为 txt（每行一个，可再粘回「批量编辑」导入）。"""
        import webview
        try:
            items = json.loads(items_json) if isinstance(items_json, str) else (items_json or [])
        except Exception:
            return {"ok": False, "error": "数据格式错误"}
        items = [str(x).strip() for x in items if str(x).strip()]
        if not items:
            return {"ok": False, "error": "列表为空"}
        try:
            paths = webview.windows[0].create_file_dialog(
                webview.SAVE_DIALOG, save_filename="weibobot_blacklist.txt")
        except Exception:
            paths = None
        if not paths:
            return {"ok": False, "error": "已取消保存"}
        dest = paths if isinstance(paths, str) else paths[0]
        try:
            with open(dest, "w", encoding="utf-8") as f:
                f.write("# 微博bot小助手 关键词黑名单\n")
                f.write(f"# 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                        f"　共 {len(items)} 个\n\n")
                for w in items:
                    f.write(w + "\n")
            return {"ok": True, "path": dest, "count": len(items)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @staticmethod
    def _pick_folder(title="选择导出位置"):
        """弹出系统文件夹选择框，返回路径字符串；取消则返回 ''。"""
        import webview
        try:
            folder = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        except Exception:
            try:
                folder = webview.windows[0].create_file_dialog(
                    webview.FOLDER_DIALOG, directory="")
            except Exception:
                folder = None
        if not folder:
            return ""
        return folder if isinstance(folder, str) else folder[0]

    def export_data(self, kind, fmt):
        """导出某一类全部记录：选择**文件夹**，产出「数据文件 + 图片副本」。"""
        dest = self._pick_folder("选择导出位置（将自动创建导出文件夹）")
        if not dest:
            return {"ok": False, "error": "已取消保存"}
        return export(kind, fmt, dest)

    def export_all_data(self):
        import webview
        try:
            folder = webview.windows[0].create_file_dialog(
                webview.FOLDER_DIALOG)
        except Exception:
            folder = None
        if not folder:
            return {"ok": False, "error": "已取消"}
        dest_dir = folder if isinstance(folder, str) else folder[0]
        return export_all(dest_dir)

    def export_filtered(self, kind, fmt, filters):
        """按前端筛选条件导出历史记录（数据文件 + 引用到的图片副本）。"""
        try:
            filters = json.loads(filters) if isinstance(filters, str) else filters
        except Exception:
            return {"ok": False, "error": "筛选条件格式错误"}
        dest = self._pick_folder("选择导出位置（将自动创建「筛选导出」文件夹）")
        if not dest:
            return {"ok": False, "error": "已取消保存"}
        return export_filtered(kind, fmt, dest, filters)

    def export_selected(self, kind, ids_json, fmt):
        """导出选中的若干条历史记录（数据文件 + 引用到的图片副本）。"""
        try:
            ids = json.loads(ids_json) if isinstance(ids_json, str) else ids_json
        except Exception:
            return {"ok": False, "error": "选择项格式错误"}
        if not ids:
            return {"ok": False, "error": "未选择任何记录"}
        db = get_history_db()
        if kind == "monitored":
            rows = db.get_monitored_posts_by_ids(ids)
        elif kind == "forwards":
            rows = db.get_forwards_by_ids(ids)
        else:
            return {"ok": False, "error": "未知类型"}
        if not rows:
            return {"ok": False, "error": "未找到对应记录"}
        dest = self._pick_folder("选择导出位置（将自动创建「选中导出」文件夹）")
        if not dest:
            return {"ok": False, "error": "已取消保存"}
        return export_rows(kind, rows, fmt, dest)

    def open_path(self, path):
        """在文件管理器中打开导出目录 / 文件。"""
        try:
            if not path or not os.path.exists(path):
                return {"ok": False, "error": "路径不存在"}
            if sys.platform.startswith("win"):
                os.startfile(path)
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", path])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def load_image_base64(self, rel_path: str):
        """读取本地图片并返回 base64 Data URL，供前端预览。

        图片统一存储为相对 DATA_DIR 的 ``images\\...``，兼容旧版 ``data\\images\\...``。
        """
        try:
            rel = os.path.normpath(rel_path.replace("/", os.sep))
            # 兼容旧版：data\images\... → images\...
            if rel.lower().startswith("data" + os.sep):
                rel = rel[len("data" + os.sep):]
            candidates = [
                os.path.abspath(os.path.join(DATA_DIR, rel)),
                os.path.abspath(os.path.join(BASE_DIR, rel)),
            ]
            for p in candidates:
                if os.path.isfile(p):
                    with open(p, "rb") as f:
                        data = f.read()
                    ext = os.path.splitext(p)[1].lower()
                    mime_map = {
                        ".png": "image/png", ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg", ".gif": "image/gif",
                        ".webp": "image/webp",
                    }
                    mime = mime_map.get(ext, "image/jpeg")
                    return {"ok": True,
                            "data_url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}
            return {"ok": False, "error": f"图片文件不存在：{rel_path}"}
        except Exception as e:
            return {"ok": False, "error": f"读取图片失败：{e}"}

    # ---------------- 视频 / 相册 ----------------
    def get_media_config(self):
        """获取视频/相册下载设置。"""
        return get_config().get("media_download", {}) or {}

    def save_media_config(self, patch):
        """保存视频/相册下载设置（局部更新）。"""
        try:
            cfg = get_config()
            md = dict(cfg.get("media_download", {}) or {})
            md.update(patch or {})
            cfg.set("media_download", md)
            info(f"视频/相册设置已更新：{list((patch or {}).keys())}")
            _state_dirty()   # 【v1.4.3】
            return {"ok": True, "config": md}
        except Exception as e:
            error(f"保存视频/相册设置失败：{e}")
            return {"ok": False, "error": str(e)}

    def start_media_download(self):
        """手动执行一次下载（后台线程，首次全量、后续增量）。"""
        return start_media_download_async()

    def get_media_progress(self):
        """下载进度快照，供前端轮询。"""
        return get_media_progress()

    def get_media_summary(self):
        """媒体库统计：对象数 / 视频数 / 图片数 / 占用空间。

        【v1.4.17】回退：直接查库（v1.4.16 曾优先读索引文件，实测体验不如旧版）。
        索引文件降级为「备份 / 导入」用途，不再参与前台读取。
        """
        return get_history_db().get_media_summary()

    def clear_media_records(self):
        """清理软件内的下载记录（media_files 表），**不删除已下载的文件**。

        清理后增量判定重置：再次执行「立即执行下载」会重新拉取全部资源，
        并对文件名一致的文件直接覆盖。
        """
        try:
            get_history_db().clear_media_files()
            info("已清理下载记录（本地文件保留，增量判定已重置）", CAT_MEDIA)
            _state_dirty()   # 【v1.4.3】媒体统计（0 个对象/0 张）立即刷新
            return {"ok": True}
        except Exception as e:
            error(f"清理下载记录失败：{e}", CAT_MEDIA)
            return {"ok": False, "error": str(e)}

    def list_media_objects(self):
        """列出媒体库中已有的监控对象（含占用大小）。

        【v1.4.17】回退：直接查库（索引不再参与前台读取）。
        """
        return get_history_db().list_media_objects_with_size()

    def search_media(self, keyword):
        """按 UID / 昵称 / 正文搜索媒体文件。

        【v1.4.17】回退：直接查库（索引不再参与前台读取）。
        """
        return get_history_db().search_media(keyword or "")

    def backup_media_index(self):
        """【v1.4.17】备份索引：按当前台账生成索引 JSON，另存到用户指定路径。

        索引文件不再用于前台读取，只作为台账的可移植快照 —— 换机器 / 重装时
        可用「导入索引」把它回灌回数据库。
        """
        import webview
        try:
            from media_index import build_index_data
            data = build_index_data()
            n = len(data.get("files") or [])
            default_name = "media_index_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S")
            win = getattr(self, "main_window", None)
            res = win.create_file_dialog(
                webview.SAVE_DIALOG, directory=get_media_dir(),
                save_filename=default_name,
                file_types=("媒体索引 (*.json)",))
            path = res[0] if isinstance(res, (list, tuple)) and res else (res or "")
            if not path:
                return {"ok": False, "error": "已取消", "path": "", "count": 0}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            info(f"媒体索引已备份 {n} 条 → {path}", CAT_MEDIA)
            return {"ok": True, "path": path, "count": n}
        except Exception as e:
            error(f"备份媒体索引失败：{e}", CAT_MEDIA)
            return {"ok": False, "error": str(e), "path": "", "count": 0}

    def import_media_index(self):
        """【v1.4.17】导入索引：读索引 JSON，把其中台账行回灌到数据库。

        已存在的 (url, media_type) 自动跳过（走 INSERT OR IGNORE），不会重复。
        """
        import webview
        try:
            from media_index import read_index_file
            win = getattr(self, "main_window", None)
            res = win.create_file_dialog(
                webview.OPEN_DIALOG, directory=get_media_dir(),
                allow_multiple=False,
                file_types=("媒体索引 (*.json)",))
            path = res[0] if isinstance(res, (list, tuple)) and res else (res or "")
            if not path:
                return {"ok": False, "error": "已取消", "added": 0, "total": 0}
            data = read_index_file(path)
            if data is None:
                return {"ok": False, "error": "文件不是有效的媒体索引", "added": 0, "total": 0}
            files = data.get("files") or []
            db = get_history_db()
            added = 0
            for f in files:
                try:
                    ok = db.add_media_file(
                        f.get("uid", ""), f.get("screen_name", ""),
                        f.get("post_id", ""), f.get("media_type", "image"),
                        f.get("url", ""), f.get("file_path", ""),
                        f.get("file_name", ""), f.get("size", 0) or 0,
                        f.get("post_text", ""), f.get("post_created_at", ""))
                    if ok:
                        added += 1
                except Exception:
                    continue
            _state_dirty()
            info(f"媒体索引已导入 · 文件 {path} · 回灌 {added}/{len(files)} 条", CAT_MEDIA)
            return {"ok": True, "added": added, "total": len(files)}
        except Exception as e:
            error(f"导入媒体索引失败：{e}", CAT_MEDIA)
            return {"ok": False, "error": str(e), "added": 0, "total": 0}

    def get_latest_media_thumb(self, uid):
        """【v1.4.17】取某对象**最新一张图片**的信息（不传 base64，供前端懒加载）。

        卡片右侧只显示一张最新预览图；若直接返回 base64 会让列表接口变重，
        故这里只返回 file_path / file_name，前端再按需调 load_media_base64 取图。
        """
        try:
            rows = get_history_db().get_media_by_uid(uid, "image", 1)
            if not rows:
                return {"ok": False}
            r = rows[0]
            return {"ok": True, "file_path": r.get("file_path", ""),
                    "file_name": r.get("file_name", "")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_media_by_uid(self, uid, media_type="all"):
        """取某个监控对象的媒体列表。"""
        return get_history_db().get_media_by_uid(uid, media_type)

    def load_media_base64(self, rel_path):
        """读取媒体文件并返回 base64 Data URL（图片预览用）。"""
        try:
            rel = os.path.normpath(str(rel_path).replace("/", os.sep))
            p = os.path.abspath(os.path.join(get_media_dir(), rel))
            if not os.path.isfile(p):
                return {"ok": False, "error": f"文件不存在：{rel_path}"}
            with open(p, "rb") as f:
                data = f.read()
            ext = os.path.splitext(p)[1].lower()
            mime_map = {
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
                ".mp4": "video/mp4", ".mov": "video/quicktime",
                ".m4v": "video/mp4", ".webm": "video/webm",
            }
            mime = mime_map.get(ext, "application/octet-stream")
            return {"ok": True,
                    "data_url": f"data:{mime};base64,{base64.b64encode(data).decode()}",
                    "size": len(data)}
        except Exception as e:
            return {"ok": False, "error": f"读取媒体失败：{e}"}

    def get_media_file_url(self, rel_path):
        """返回媒体文件的 file:// URL（视频预览用，避免 base64 过大卡界面）。"""
        try:
            rel = os.path.normpath(str(rel_path).replace("/", os.sep))
            p = os.path.abspath(os.path.join(get_media_dir(), rel))
            if not os.path.isfile(p):
                return {"ok": False, "error": "文件不存在"}
            return {"ok": True, "url": Path(p).as_uri(),
                    "path": p, "size": os.path.getsize(p)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------------- 监控对象 导入 / 导出 ----------------
    def export_monitors(self, fmt="json"):
        """导出监控列表（json / csv / txt）到用户指定位置。"""
        import webview
        suffix = {"csv": ".csv", "json": ".json", "txt": ".txt"}.get(fmt, ".txt")
        try:
            win = getattr(self, "main_window", None)
            res = win.create_file_dialog(
                webview.SAVE_DIALOG, directory=BASE_DIR,
                save_filename=f"monitors{suffix}",
                file_types=("监控列表 (*.json;*.csv;*.txt)",))
            path = res[0] if isinstance(res, (list, tuple)) and res else (res or "")
            if not path:
                return {"ok": False, "error": "已取消", "path": ""}
            items = get_monitor_manager().list()
            if fmt == "json":
                with open(path, "w", encoding="utf-8") as f:
                    json.dump([{"uid": it["uid"], "screen_name": it.get("screen_name", "")}
                               for it in items], f, ensure_ascii=False, indent=2)
            elif fmt == "csv":
                import csv as _csv
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    w = _csv.writer(f)
                    w.writerow(["uid", "screen_name"])
                    for it in items:
                        w.writerow([it["uid"], it.get("screen_name", "")])
            else:
                with open(path, "w", encoding="utf-8") as f:
                    for it in items:
                        f.write(f"{it['uid']}\n")
            info(f"导出监控对象 {len(items)} 个 → {path}")
            return {"ok": True, "path": path, "count": len(items)}
        except Exception as e:
            error(f"导出监控对象失败：{e}")
            return {"ok": False, "error": str(e), "path": ""}

    def import_monitors_from_file(self):
        """从文件导入监控列表（json / csv / txt）。"""
        import webview
        try:
            win = getattr(self, "main_window", None)
            res = win.create_file_dialog(
                webview.OPEN_DIALOG, directory=BASE_DIR,
                file_types=("监控列表 (*.json;*.csv;*.txt)", "所有文件 (*.*)"))
            path = res[0] if isinstance(res, (list, tuple)) and res else (res or "")
            if not path:
                return {"ok": False, "error": "已取消"}
            try:
                with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
                    text = f.read()
            except Exception as e:
                return {"ok": False, "error": f"读取文件失败：{e}"}
            return self.import_monitors_text(text)
        except Exception as e:
            error(f"导入监控对象失败：{e}")
            return {"ok": False, "error": str(e)}

    def import_monitors_text(self, text):
        """按文本导入监控对象。

        支持三种格式：
          1) JSON 数组：[{"uid":"123","screen_name":"昵称"}] 或 {"monitors":[...]}
          2) CSV / 文本：每行 `UID,昵称`（也兼容逗号、制表符、分号分隔）
          3) 纯 UID 或微博主页链接（https://weibo.com/u/1234567890），每行一个
        已存在的 UID 自动跳过，不会重复添加。
        """
        import re as _re
        try:
            mon = get_monitor_manager()
            text = (text or "").strip()
            if not text:
                return {"ok": False, "error": "内容为空"}

            rows = []
            if text.startswith("[") or text.startswith("{"):
                try:
                    data = json.loads(text)
                    if isinstance(data, dict):
                        data = (data.get("monitors") or data.get("list")
                                or data.get("data") or [])
                    for it in data:
                        if isinstance(it, dict):
                            rows.append((
                                str(it.get("uid") or it.get("id") or "").strip(),
                                str(it.get("screen_name") or it.get("name")
                                    or it.get("nickname") or "").strip()))
                        else:
                            rows.append((str(it).strip(), ""))
                except Exception as e:
                    return {"ok": False, "error": f"JSON 解析失败：{e}"}
            else:
                for line in text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = _re.split(r"[,，\t;]+", line, 1)
                    uid = parts[0].strip()
                    name = parts[1].strip() if len(parts) > 1 else ""
                    rows.append((uid, name))

            added, skipped, failed, msgs = 0, 0, 0, []
            for uid, name in rows:
                if not uid:
                    continue
                # 兼容微博主页链接，提取其中的 UID
                m = (_re.search(r"weibo\.(?:com|cn)/(?:u/)?(\d{5,})", uid)
                     or _re.match(r"^(\d{5,})$", uid))
                if m:
                    uid = m.group(1)
                if not uid.isdigit():
                    failed += 1
                    msgs.append(f"无效 UID：{uid}")
                    continue
                ok, msg = mon.add(uid, name)
                if ok:
                    added += 1
                elif "已在监控列表" in str(msg):
                    skipped += 1
                else:
                    failed += 1
                    msgs.append(f"{uid}：{msg}")

            info(f"导入监控对象完成 · 新增 {added} · 已存在跳过 {skipped} · 失败 {failed}")
            if added:
                _state_dirty()   # 【v1.4.3】导入成功后列表立即刷新
            return {"ok": True, "added": added, "skipped": skipped,
                    "failed": failed, "messages": msgs[:10]}
        except Exception as e:
            error(f"导入监控对象异常：{e}")
            return {"ok": False, "error": str(e)}

    def open_media_dir(self, uid=""):
        """打开媒体目录（传入 uid 则打开该对象的目录）。"""
        try:
            if uid:
                items = {it["uid"]: it for it in get_monitor_manager().list()}
                it = items.get(str(uid)) or {}
                target = user_media_dir(str(uid), it.get("screen_name", ""))
            else:
                target = get_media_dir()
            os.makedirs(target, exist_ok=True)
            try:
                os.startfile(target)          # Windows 资源管理器
            except Exception:
                webbrowser.open(Path(target).as_uri())
            return {"ok": True, "path": target}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------------- 【v1.2.0 / v1.2.2】数据路径设置 ----------------
    def get_data_paths(self):
        """读取当前生效的数据路径设置（含授权相关文件的锁定说明）。"""
        try:
            return {"ok": True, "settings": get_path_settings()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_data_paths(self, image_dir=None, media_dir=None,
                       models_dir=None, history_dir=None, wizard_done=None):
        """保存自定义路径。传 "" 表示恢复默认，传 None 表示该项不改。

        【v1.2.2】扩到四个目录（images / media / models / history）。
        改了 history_dir 会自动迁移 history.db 并重开数据库连接。
        """
        try:
            r = set_data_paths(image_dir=image_dir, media_dir=media_dir,
                               models_dir=models_dir, history_dir=history_dir,
                               wizard_done=wizard_done)
            if r.get("changed"):
                info("数据路径已更新：%s" % "；".join(r["changed"]))
                _state_dirty()   # 【v1.4.3】路径/向导状态立即刷新
            if r.get("note"):
                info("历史库：%s" % r["note"])
            # 历史目录变了 → 丢掉 DB 单例，让下次查询按新路径重开
            if any(c.startswith("历史记录") for c in (r.get("changed") or [])):
                try:
                    from history_db import reset_history_db
                    reset_history_db()
                except Exception as e:
                    warn("重开历史数据库失败：%s" % e)
            # 路径变了，旧的占用统计全部作废，重新扫
            if r.get("changed"):
                try:
                    import storage
                    storage.start_scan(force=True)
                except Exception:
                    pass
            if r.get("error"):
                warn(f"数据路径设置存在问题：{r['error']}")
            return r
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------------- 【v1.2.2】首次启动向导 ----------------
    def get_wizard_state(self):
        """首启向导状态：是否已完成、四个目录的当前值与默认值。"""
        try:
            return {"ok": True, "wizard": wizard_state()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def finish_wizard(self, image_dir=None, media_dir=None,
                      models_dir=None, history_dir=None):
        """确认首启向导：落盘四个目录设置并打上 wizard_done 标记。

        全部传 "" / None 表示「都用默认位置」，此时只写标记不写路径。
        """
        try:
            r = set_data_paths(image_dir=image_dir, media_dir=media_dir,
                               models_dir=models_dir, history_dir=history_dir,
                               wizard_done=True)
            r["wizard"] = wizard_state()
            if r.get("error"):
                warn("首启向导：%s" % r["error"])
            try:
                import storage
                storage.start_scan(force=True)
            except Exception:
                pass
            return r
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------------- 【v1.2.2】存储占用（异步，绝不阻塞 UI） ----------------
    def start_storage_scan(self, force=False):
        """发起一次后台目录扫描（非阻塞）。"""
        try:
            import storage
            started = storage.start_scan(force=bool(force))
            return {"ok": True, "started": started, "status": storage.get_status()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_storage_status(self):
        """取扫描进度与结果。**纯内存读取**，前端每秒轮询也无压力。"""
        try:
            import storage
            return {"ok": True, "status": storage.get_status()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def clear_storage_dir(self, which="", mode="cache"):
        """一键清理某个目录。

        mode="cache"（默认，**安全**）：只删可重新下载的文件（图片/视频/
        头像），保留模型、数据库、导出与日志。
        mode="all"：清空该目录。历史记录目录**禁止**此模式（DB 正被占用）。
        """
        try:
            import storage
            r = storage.clear_dir(which, mode)
            if r.get("ok"):
                info("清理 %s（%s）：删除 %d 个文件，释放 %s"
                     % (which, mode, r.get("removed_files", 0), r.get("freed")))
            else:
                warn("清理 %s 失败：%s" % (which, r.get("error")))
            return r
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def pick_storage_dir(self, which="image_dir"):
        """弹出系统文件夹选择框，让用户挑一个目录。

        【v1.2.3 关键修复】原先用 tkinter.filedialog.askdirectory，
        但本项目用的 managed Python 3.13 **没有编译 tkinter**，
        一调就抛 `No module named 'tkinter'` → 前端只看到报错 toast，
        永远弹不出选择框（用户实测反馈）。

        改为直接调 Windows 原生 `SHBrowseForFolderW`（纯 ctypes，
        零第三方依赖、零子进程、毫秒级），并带 BFFM_INITIALIZED
        回调把初始目录定位到当前生效路径。为什么不改用 pywebview 的
        `window.create_file_dialog(FOLDER_DIALOG)`：当前版本的
        EdgeChromium 后端没有实现该方法（会直接 AttributeError），
        且 js_bridge 调用本身跑在 WebView2 的 UI 线程上，在那里
        再同步等一个模态对话框有死锁风险。
        """
        try:
            from paths import (get_history_dir, get_image_dir,
                               get_media_dir, get_models_dir)
            if which not in ("image_dir", "media_dir", "models_dir", "history_dir"):
                which = "image_dir"
            cur = {"image_dir": get_image_dir, "media_dir": get_media_dir,
                   "models_dir": get_models_dir, "history_dir": get_history_dir}[which]()
            p = _native_pick_folder("选择目录", cur)
            return {"ok": True, "path": p or ""}
        except Exception as e:
            warn("pick_storage_dir 失败：%s" % e)
            return {"ok": False, "error": f"无法打开目录选择框：{e}"}

    def open_storage_dir(self, which="image_dir"):
        """在资源管理器里打开某个目录。"""
        try:
            import storage
            if which in ("image_dir", "media_dir", "models_dir", "history_dir"):
                return storage.open_dir(which)
            return {"ok": False, "error": "未知目录"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def pick_data_path(self, which="image_dir"):
        """【兼容 v1.2.1 前端】文件夹选择框。"""
        r = self.pick_storage_dir(which if which in (
            "image_dir", "media_dir", "models_dir", "history_dir") else "image_dir")
        return r

    def open_data_dir(self, which="data"):
        """打开数据目录。which: data | image | media | models"""
        try:
            if which == "image":
                target = get_image_dir()
            elif which == "media":
                target = get_media_dir()
            elif which == "models":
                from model_manager import models_dir
                target = models_dir()
            else:
                target = DATA_DIR
            os.makedirs(target, exist_ok=True)
            try:
                os.startfile(target)
            except Exception:
                webbrowser.open(Path(target).as_uri())
            return {"ok": True, "path": target}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_data_paths_usage(self):
        """【v1.2.2 已弃用】原来在这里同步 os.walk 统计各目录占用。

        这正是「启动 / 进入数据页就未响应」的元凶之一——遍历一个上万张图的
        media/ 目录要几秒到几十秒，而本方法跑在 WebView2 的 UI 线程上。
        现改为返回「请改用 get_storage_status()」的引导，同时**不再做任何
        磁盘遍历**，老前端调它也不会卡。
        """
        return {"ok": False, "deprecated": True,
                "error": "占用统计已改为异步接口，请改用 get_storage_status()"}
