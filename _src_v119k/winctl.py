# -*- coding: utf-8 -*-
"""Win32 窗口控制（绕过 pywebview 代理，直接操作主窗口 HWND）。

【v1.1.9i 重大变更】彻底放弃「无边框窗口（frameless）」路线。
历史上为自绘标题栏 + 边缘缩放 + 拖动磁吸，先后叠加了：
  fix_native_style（补 WS_THICKFRAME）、WM_NCHITTEST 子类化、begin_native_move 等，
每加一层都引入新问题（拉伸/拖动互相冲突、窗口过程子类化导致「未响应」、命中测试
选不中标题栏）。经验证该路线在 pywebview(edgechromium) 下不可靠，遂全部移除。

现在窗口使用 Windows 原生标题栏（frameless=False）：拖动、缩放、最小化/最大化/
关闭、Aero Snap 磁吸、双击最大化全部由系统原生提供，无需任何自绘或子类化。
本模块只保留「托盘相关」的直驱调用（隐藏 / 恢复 / 前置），这些仍需直接操作 HWND。

坐标系：对外暴露的 x/y/width/height 均为「逻辑像素」（与 JS 同坐标系）——内部
GetWindowRect 取到的物理像素按窗口所在显示器 DPI 缩放回逻辑像素。
"""
import ctypes
from ctypes import wintypes

# user32 仅在 Windows 存在；本应用为 Windows 单文件 exe，加载时即要求 Windows。
try:
    _user32 = ctypes.windll.user32
except Exception:
    _user32 = None

APP_TITLE = "微博bot小助手"

# ShowWindow 命令
SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_SHOWMAXIMIZED = 3
SW_RESTORE = 9

# SetWindowPos 标志（保留常量，供后续窗口操作复用）
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_SHOWWINDOW = 0x0040


def _setup_protos():
    """设置关键 API 的参数 / 返回类型，避免 64 位下传参错乱。"""
    if _user32 is None:
        return
    ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [ENUMPROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindowRect.restype = wintypes.BOOL
    _user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    _user32.SetWindowPos.restype = wintypes.BOOL
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsIconic.restype = wintypes.BOOL
    if hasattr(_user32, "GetMonitorInfoW"):
        _user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        _user32.GetMonitorInfoW.restype = wintypes.BOOL
    if hasattr(_user32, "GetDpiForWindow"):
        _user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        _user32.GetDpiForWindow.restype = wintypes.UINT
    if hasattr(_user32, "GetDpiForSystem"):
        _user32.GetDpiForSystem.argtypes = []
        _user32.GetDpiForSystem.restype = wintypes.UINT


_setup_protos()


def find_hwnd():
    """按标题精确匹配定位主窗口 HWND；找不到返回 0。"""
    result = {"h": 0}

    def cb(hwnd, lparam):
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value == APP_TITLE:
            result["h"] = hwnd
            return 0  # 停止枚举
        return 1

    ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows(ENUMPROC(cb), 0)
    return result["h"]


def get_dpi_scale(hwnd):
    """返回该窗口所在显示器的 DPI 缩放比（96 DPI = 1.0）。"""
    try:
        if hasattr(_user32, "GetDpiForWindow"):
            dpi = _user32.GetDpiForWindow(hwnd)
            if dpi:
                return dpi / 96.0
    except Exception:
        pass
    try:
        if hasattr(_user32, "GetDpiForSystem"):
            dpi = _user32.GetDpiForSystem()
            if dpi:
                return dpi / 96.0
    except Exception:
        pass
    return 1.0


def get_pos():
    """返回主窗口逻辑坐标（x, y, width, height）；找不到返回 None。"""
    if _user32 is None:
        return None
    hwnd = find_hwnd()
    if not hwnd:
        return None
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    scale = get_dpi_scale(hwnd)
    return {
        "x": int(rect.left / scale),
        "y": int(rect.top / scale),
        "width": int((rect.right - rect.left) / scale),
        "height": int((rect.bottom - rect.top) / scale),
    }


def hide():
    """隐藏主窗口（最小化到托盘）。"""
    if _user32 is None:
        return False
    hwnd = find_hwnd()
    if not hwnd:
        return False
    _user32.ShowWindow(hwnd, SW_HIDE)
    return True


def show_restore():
    """从托盘恢复：还原（若最小化/隐藏）并置前。"""
    if _user32 is None:
        return False
    hwnd = find_hwnd()
    if not hwnd:
        return False
    _user32.ShowWindow(hwnd, SW_RESTORE)
    try:
        _user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    return True


# ---------------- 边缘磁吸（SNAP） ----------------
# 【v1.1.9k】原生标题栏下，Windows 的 Aero Snap 只在拖动到**屏幕最边缘**时触发，
# 且左右半边会强制占满半屏。用户要的是「贴近边缘 16px 内自动贴合」的吸附感，
# 系统原生不提供，这里在拖动结束时做一次位置吸附：
#   · 取窗口所在显示器的**工作区**（不含任务栏）；
#   · 左右边 / 上下边 / 四角，距离 <= SNAP_THRESHOLD 逻辑像素时贴合；
#   · 仅微调位置，**不改窗口尺寸**（避免与用户预期不符）。
SNAP_THRESHOLD = 16          # 逻辑像素
SPI_GETWORKAREA = 0x0030

if _user32 is not None:
    try:
        _user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        _user32.MonitorFromWindow.restype = wintypes.HANDLE
        _user32.SystemParametersInfoW.argtypes = [
            wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
        _user32.SystemParametersInfoW.restype = wintypes.BOOL
        _user32.MoveWindow.argtypes = [
            wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.BOOL]
        _user32.MoveWindow.restype = wintypes.BOOL
    except Exception:
        pass

MONITOR_DEFAULTTONEAREST = 2


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", wintypes.DWORD)]


def _work_area(hwnd):
    """返回窗口所在显示器的**工作区**（物理像素）或 None。"""
    if _user32 is None:
        return None
    try:
        mon = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not mon:
            return None
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not _user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return None
        r = mi.rcWork
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


def snap_to_edges(threshold=None):
    """把主窗口吸附到所在显示器工作区边缘（贴近时）。

    返回 {'ok': bool, 'moved': bool, 'x':..., 'y':...}（逻辑像素）。
    仅在**位置**上贴合，不改变窗口大小。
    """
    if _user32 is None:
        return {"ok": False, "error": "user32 不可用"}
    hwnd = find_hwnd()
    if not hwnd:
        return {"ok": False, "error": "主窗口未找到"}
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return {"ok": False, "error": "GetWindowRect 失败"}
    area = _work_area(hwnd)
    if not area:
        return {"ok": False, "error": "工作区读取失败"}
    al, at, ar, ab = area
    try:
        scale = get_dpi_scale(hwnd) or 1.0
    except Exception:
        scale = 1.0
    thr = int((SNAP_THRESHOLD if threshold is None else threshold) * scale)

    w = rect.right - rect.left
    h = rect.bottom - rect.top
    x, y = rect.left, rect.top
    nx, ny = x, y

    # 左边 / 右边
    if abs(x - al) <= thr:
        nx = al
    elif abs((x + w) - ar) <= thr:
        nx = ar - w
    # 上边 / 下边
    if abs(y - at) <= thr:
        ny = at
    elif abs((y + h) - ab) <= thr:
        ny = ab - h

    # 避免窗口被推出工作区（例如窗口比工作区还大）
    if nx < al:
        nx = al
    if ny < at:
        ny = at

    moved = (nx != x) or (ny != y)
    if moved:
        if not _user32.MoveWindow(hwnd, int(nx), int(ny), int(w), int(h), True):
            return {"ok": False, "error": "MoveWindow 失败"}
    return {"ok": True, "moved": moved,
            "x": int(nx / scale), "y": int(ny / scale)}
