# -*- coding: utf-8 -*-
"""v1.1.9k 启动入口（Windows 原生标题栏 + 系统托盘）。

【重大变更】放弃无边框窗口（frameless）路线，改回系统原生标题栏：
  - 窗口拖动 / 缩放 / 最小化 / 最大化 / 关闭 / Aero Snap 磁吸 / 双击最大化
    全部由 Windows 原生提供，不再自绘标题栏、不再做 WM_NCHITTEST 子类化；
  - 保留「最小化到系统托盘」能力（pystray 守护线程 + Win32 直驱隐藏/恢复）。

历史教训：frameless + pywebview(edgechromium) 组合下，自绘标题栏的拖动/命中测试
始终无法稳定工作，故彻底回退。
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 【v1.1.9j】必须在任何窗口创建之前声明 DPI 感知。
# 之前 exe 的 manifest 没有 dpiAware 声明，Windows 对进程启用 DPI 虚拟化：
# 系统把 WebView2 渲染结果位图拉伸后再送给显示器，在高分屏 / 多屏混合缩放下
# 会导致渲染极慢、输入迟滞，表现就是「未响应」。
# 这里显式声明 PerMonitorV2；若系统不支持则回退 System DPI 感知。
def _init_dpi_awareness():
    try:
        import ctypes
        # PROCESS_PER_MONITOR_DPI_AWARE = 2（Win8.1+）
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    except Exception:
        pass


_init_dpi_awareness()

from paths import (FROZEN, BASE_DIR, ensure_dirs, start_legacy_migration)
import webview
from api_bridge import Api

try:
    import pystray
    from PIL import Image
    _HAS_TRAY = True
except Exception:
    _HAS_TRAY = False


def _ui_dir():
    """定位 UI 资源目录。

    【v1.1.9k 修正】优先级：
      ① frozen 时 **必须** 用打包进 exe 的 `_MEIPASS/ui`（这是唯一保证与版本一致的 UI）；
      ② 开发态回退到项目下的 `_ui_v119k`；
      ③ 最后才考虑同级 `ui/`（历史遗留，已归档到 `_archive_ui_old/`，正常不会命中）。

    之前的候选列表里塞了 _ui_v119j / v119i / v119h / v118d … 等**早已删除**的
    旧 staging 目录，一旦某个旧目录仍在磁盘上，就会盖过 _MEIPASS/ui 造成
    「exe 版本号是新的、界面却是旧的」。现只保留 v119k + 兜底。
    """
    candidates = []
    if FROZEN and hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, "ui"))
    candidates += [
        os.path.join(BASE_DIR, "_ui_v119k"),
        os.path.join(BASE_DIR, "ui"),   # 兜底：历史遗留目录（已移入 _archive_ui_old）
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return candidates[0] if candidates else BASE_DIR


def _ui_index_path():
    return os.path.join(_ui_dir(), "index.html")


def _tray_icon_image():
    logo = os.path.join(_ui_dir(), "logo.png")
    try:
        return Image.open(logo)
    except Exception:
        return Image.new("RGBA", (32, 32), (255, 130, 0, 255))


def _start_tray(api):
    """在守护线程里运行 pystray 图标；菜单：恢复 / 退出。"""
    if not _HAS_TRAY:
        return
    try:
        menu = pystray.Menu(
            pystray.MenuItem("显示 / 恢复", lambda icon, item: api.restore_from_tray()),
            pystray.MenuItem("退出", lambda icon, item: api.quit_app()),
        )
        icon = pystray.Icon("微博bot小助手", _tray_icon_image(), "微博bot小助手", menu)
        api._tray = icon
        icon.run()
    except Exception:
        try:
            api._tray = None
        except Exception:
            pass


def _prewarm_ca_bundle():
    """【v1.1.9k 0011】启动最早期就把 CA 证书包固化到 DATA_DIR/cacert.pem。

    为什么必须放在这里：onefile 模式下 certifi 的 cacert.pem 解压在
    %TEMP%\\_MEIxxxxxx\\certifi\\，该目录随时可能被「存储感知 / 磁盘清理 / 杀软」删掉。
    一旦删掉，requests 对**每个** HTTPS 请求都抛
    "Could not find a suitable TLS CA certificate bundle"，表现为全站 network_error。

    旧版把备份动作挂在「certifi 路径有效时顺手备份」上——可等到路径失效时已无源可备份，
    于是备份永远建不出来（实测本地 data/cacert.pem 一直不存在）。
    现在改成启动即无条件预置，并做一次自愈，让首个网络请求就用上本地证书包。
    """
    try:
        from ca_bundle import ensure_ca_bundle
        status, path = ensure_ca_bundle()
        if path:
            sys.stderr.write("[CA]证书包就绪 · %s\n" % status)
        else:
            sys.stderr.write("[CA] 警告：%s\n" % status)
    except Exception as e:
        sys.stderr.write("[CA] 预置异常：%s\n" % e)


def _harden_meipass():
    """【v1.1.9k 0011】给 _MEI 临时目录打上「最近使用」标记，降低被清理的概率。

    PyInstaller onefile 把 certifi/openssl 等解压到 %TEMP%\\_MEIxxxxxx\\，
    Windows「存储感知 / 磁盘清理」会按「最后访问时间」回收 %TEMP% 里的老目录，
    这正是 "invalid path: ...\\_MEI00000c5c2\\certifi\\cacert.pem" 的直接成因。
    这里把整个 _MEI 目录树的文件时间戳顶到现在，续上 7 天保鲜期。

    这是**降低概率**的缓解措施，真正的兜底是 _prewarm_ca_bundle()（已不依赖 _MEI）。
    """
    meipass = getattr(sys, "_MEIPASS", "")
    if not (FROZEN and meipass and os.path.isdir(meipass)):
        return
    try:
        now = time.time()
        n = 0
        for root, _dirs, files in os.walk(meipass):
            for fn in files:
                try:
                    os.utime(os.path.join(root, fn), (now, now))
                    n += 1
                except Exception:
                    pass
        sys.stderr.write("[CA] 已刷新 _MEIPASS 时间戳（%d 个文件）\n" % n)
    except Exception as e:
        sys.stderr.write("[CA] 刷新 _MEIPASS 时间戳失败：%s\n" % e)


def _warmup_storage():
    """【v1.2.2】启动后的后台预热：建四个大目录 + 异步扫描占用。

    这是用户反馈的「启动未响应」的正解：启动路径上**只做本地小目录的
    makedirs**，所有对 images/ media/ models/ history/ 的遍历都挪到这里，
    在守护线程里慢慢跑；界面早就出来了，进度由前端轮询
    get_storage_status() 展示。
    """
    try:
        from paths import ensure_all_user_dirs
        ensure_all_user_dirs()
    except Exception as e:
        sys.stderr.write("[STORAGE] 预建目录失败：%s\n" % e)
    try:
        import storage
        storage.start_scan(force=True)
    except Exception as e:
        sys.stderr.write("[STORAGE] 启动占用扫描失败：%s\n" % e)


def main():
    # 【v1.2.2】这里只建 DATA_DIR（小目录，本地盘，几乎零成本）。
    # 四个大目录一律懒创建，见 paths.ensure_dirs 的注释。
    ensure_dirs()
    # 历史记录目录被用户改过 → 把老history.db 挪过去（一次性，通常几毫秒）
    try:
        from paths import migrate_history_db
        moved, why = migrate_history_db()
        if moved:
            sys.stderr.write("[STORAGE] %s\n" % why)
    except Exception as e:
        sys.stderr.write("[STORAGE] 历史库迁移异常：%s\n" % e)
    # CA 证书包预置必须在任何网络请求（Api() 构造 / 账户自检）之前完成
    _prewarm_ca_bundle()
    _harden_meipass()
    index = _ui_index_path()
    api = Api()
    api.ui_dir = _ui_dir()
    # 【v1.1.9k】老数据迁移放在 Api() 之后：
    #  · 文件级迁移（json / .acc_key）随时可做；
    #  · 数据库迁移**必须**以 Api 已打开的 history.db 连接为目标做在线备份
    #    （本地库被连接独占，文件级复制/替换必然 WinError 32）。
    # 整个过程在后台线程，不阻塞首屏。
    try:
        start_legacy_migration()
    except Exception:
        pass
    # 原生标题栏窗口：frameless 不再使用（v1.1.9i 回退）。
    window = webview.create_window(
        title="微博bot小助手",
        url=index,
        js_api=api,
        width=1280,
        height=820,
        min_size=(1024, 680),
        text_select=True,
    )
    api.main_window = window
    # 后台预热状态缓存：首次 get_state 由前端在 pywebviewready 后立即调用，
    # 而它会在 WebView2 的 UI 线程同步执行；若此时才建立 DB 连接 / 读文件（尤其
    # history.db 位于 NAS），就会卡住 UI 线程 →「未响应」。这里提前用后台线程把
    # 状态缓存建好，使前端第一次调用即命中缓存、瞬时可返回。
    def _prewarm_state():
        try:
            api._refresh_state_async()
        except Exception:
            pass
    threading.Thread(target=_prewarm_state, daemon=True).start()
    # 【v1.2.2】四个大目录的预建与占用扫描 —— 后台慢慢跑，不阻塞首屏
    threading.Thread(target=_warmup_storage, daemon=True,
                     name="storage-warmup").start()
    # 启动系统托盘（守护线程），保证「最小化到托盘」随时可用
    if _HAS_TRAY:
        threading.Thread(target=_start_tray, args=(api,), daemon=True).start()
    webview.start()


if __name__ == "__main__":
    main()
