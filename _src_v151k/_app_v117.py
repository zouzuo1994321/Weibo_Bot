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
import urllib.parse

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
from diag_log import diag
import webview
from api_bridge import Api

try:
    import pystray
    from PIL import Image
    _HAS_TRAY = True
except Exception:
    _HAS_TRAY = False


# ---------------------------------------------------------------------------
# 【v1.5.0 Part2-A】单实例互斥锁
# 用户实测：关窗后进程要拖 ~12.5s 才真正退出（WebView2 拆环境）。
# 这段时间里紧接着再开新实例，就可能出现两个实例抢资源 → 新实例白屏卡死。
# 现在用命名互斥体兜底：上一次实例还活着（含退出拖尾期）时，新实例
# 明确弹窗提示「请稍候」，而不是静默叠一个打不开的窗口。
# ---------------------------------------------------------------------------
_G_MUTEX = None   # 持住互斥体句柄，进程存活期间绝不释放


def _acquire_single_instance():
    """尝试持有单实例互斥体。返回 False = 已有实例在跑，本进程应退出。"""
    global _G_MUTEX
    import ctypes
    MUTEX_NAME = "Local\\微博bot小助手_SingleInstance"
    try:
        k32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        _G_MUTEX = k32.CreateMutexW(None, False, MUTEX_NAME)
        if _G_MUTEX and ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
            diag("SINGLE_INST_BLOCK", "已有实例在运行/退出拖尾中")
            try:
                ctypes.windll.user32.MessageBoxW(
                    0,
                    "微博bot小助手 已在运行，或上一个实例正在退出（最长约十几秒）。\n\n"
                    "请稍候几秒后再打开。",
                    "微博bot小助手",
                    0x00000040)   # MB_ICONINFORMATION
            except Exception:
                pass
            return False
        diag("SINGLE_INST_OK", MUTEX_NAME)
        return True
    except Exception as e:
        # 判定失败时宁可放行也不误拦（互斥只是兜底，不该挡住正常启动）
        diag("SINGLE_INST_ERR", str(e)[:200])
        return True


def _ui_dir():
    """定位 UI 资源目录。

    【v1.1.9k 修正】优先级：
      ① frozen 时 **必须** 用打包进 exe 的 `_MEIPASS/ui`（这是唯一保证与版本一致的 UI）；
      ② 开发态回退到项目下的 `_ui_v151k`；
      ③ 最后才考虑同级 `ui/`（历史遗留兜底，正常不会命中）。

    之前的候选列表里塞了 _ui_v119j / v119i / v119h / v118d … 等**早已删除**的
    旧 staging 目录，一旦某个旧目录仍在磁盘上，就会盖过 _MEIPASS/ui 造成
    「exe 版本号是新的、界面却是旧的」。现只保留当前版本目录 + 兜底。
    """
    candidates = []
    if FROZEN and hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, "ui"))
    candidates += [
        os.path.join(BASE_DIR, "_ui_v151k"),
        os.path.join(BASE_DIR, "ui"),   # 兜底：历史遗留目录
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


def _reconcile_media():
    """【v1.4.7】启动后做一次媒体记录对账，清掉「记录在、文件没了」的僵尸行。

    必须在后台线程里调用（全表扫描 + 磁盘 stat，媒体量大时耗时可观）。
    失败一律吞掉 —— 这是自愈性维护动作，绝不能影响首屏与主流程。

    【v1.4.16】对账带安全阈值（见 history_db.reconcile_media_files），
    不会再出现「媒体文件整体不在位 → 台账被清空」；对账完成后回写媒体索引，
    并把索引文件本身的存在性也一并校验。
    """
    try:
        import time as _t
        _t.sleep(3.0)   # 等 Api 的 DB 连接与目录预建完成，避免与迁移/预热抢锁
        from history_db import get_history_db
        rep = get_history_db().reconcile_media_files()
        if rep and rep.get("removed"):
            sys.stderr.write("[MEDIA] 媒体自愈对账：检查 %s 条，清理僵尸记录 %s 条（将自动重新抓取）\n"
                             % (rep.get("checked"), rep.get("removed")))
        elif rep and rep.get("skipped"):
            sys.stderr.write("[MEDIA] 媒体自愈对账已跳过（%s），台账已保护\n"
                             % rep.get("reason"))
        # 【v1.4.16】启动即校验索引：缺失 / 损坏 / 结构不符 → 立即重建一份
        try:
            from media_index import load as _idx_load, schedule_rebuild
            if _idx_load() is None:
                sys.stderr.write("[MEDIA] 媒体索引缺失或失效，正在后台重建…\n")
                schedule_rebuild()
        except Exception as e:
            sys.stderr.write("[MEDIA] 媒体索引校验异常：%s\n" % e)
    except Exception as e:
        sys.stderr.write("[MEDIA] 媒体自愈对账异常：%s\n" % e)


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
    # 【v1.5.0 Part2-A】单实例互斥：上一个实例还在退出拖尾里时不再叠开新窗口
    if not _acquire_single_instance():
        return
    diag("LAUNCH", "pid=%d frozen=%s" % (os.getpid(), bool(FROZEN)))
    # 【v1.2.2】这里只建 DATA_DIR（小目录，本地盘，几乎零成本）。
    # 四个大目录一律懒创建，见 paths.ensure_dirs 的注释。
    ensure_dirs()
    diag("DIRS_OK")
    # 历史记录目录被用户改过 → 把老history.db 挪过去（一次性，通常几毫秒）
    try:
        from paths import migrate_history_db
        moved, why = migrate_history_db()
        if moved:
            sys.stderr.write("[STORAGE] %s\n" % why)
    except Exception as e:
        sys.stderr.write("[STORAGE] 历史库迁移异常：%s\n" % e)
    diag("MIGRATE_DONE")
    # CA 证书包预置必须在任何网络请求（Api() 构造 / 账户自检）之前完成
    _prewarm_ca_bundle()
    diag("CA_OK")
    _harden_meipass()
    index = _ui_index_path()
    # 【v1.5.0 修复·根因】UI 改走 file:// 直载，不再经过 pywebview 本地 HTTP 服务。
    # 本机系统代理 ProxyServer=127.0.0.1:8899 处于启用状态时，WebView2(Chromium)
    # 对 http://127.0.0.1:<port>/index.html 的回环请求会被代理拦截 / 挂起，
    # 页面导航始终不提交（实测 document.readyState 求值落空、桥注入全部失败）
    # → 前端停在开屏、主状态读取失败、窗口假死并最终自行关闭。
    # file:// 完全不碰网络栈；本应用数据全部经 window.pywebview 桥传递，
    # 前端无 fetch / XHR（已核实），改 file:// 无副作用；内置浏览器登录
    # 仍走 http，代理保持有效不受影响。
    index_url = 'file:///' + urllib.parse.quote(index.replace('\\', '/'))
    api = Api()
    api.ui_dir = _ui_dir()
    diag("API_OK")
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
        url=index_url,
        js_api=api,
        width=1280,
        height=820,
        min_size=(1024, 680),
        text_select=True,
    )
    api.main_window = window
    diag("WINDOW_CREATED", index)
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
    # 【v1.4.7】媒体自愈对账 —— 修「每次重启后相册都要重新识别」
    # 根因：media_files 表里可能残留「记录还在、磁盘文件已删」的僵尸行，
    # 而增量下载判定只认数据库记录，于是这些 URL 被永久判为「已下载」而跳过，
    # 相册列表永远长不回来，用户只能手动重新识别。
    # 这里在后台把这类僵尸记录清掉，清除后同一 URL 会被判为未下载，
    # 由相册监控 / 手动下载任务自动重新抓取，页面无需任何手动操作。
    threading.Thread(target=_reconcile_media, daemon=True,
                     name="media-reconcile").start()
    # 启动系统托盘（守护线程），保证「最小化到托盘」随时可用
    if _HAS_TRAY:
        threading.Thread(target=_start_tray, args=(api,), daemon=True).start()
    diag("WEBVIEW_START", "blocking")
    webview.start()
    # 【v1.5.0 Part2-B】窗口关闭（含托盘「退出」的 destroy 路径）后
    # 落盘收尾 + 硬退出，根治 ~12.5s 的 WebView2 拆环境拖尾。
    diag("WINDOW_CLOSED", "webview.start returned")
    _hard_exit(api)


def _hard_exit(api):
    """【v1.5.0 Part2-B】根治退出拖尾。

    实测：WM_CLOSE 后窗口 0.3s 消失，但进程要再拖 ~12.5s 才真正退出
    （WebView2/Edge 拆环境所致，稳定复现）。这段时间旧实例「半死不活」
    地占着资源，紧接着重开新实例就可能撞上资源竞争 —— 正是用户
    「3 次只 1 次能打开」的高发窗口。

    现在窗口一关就：停托盘 → 关数据库 → os._exit(0) 硬退出：
      - SQLite 天然崩溃安全（hot journal 下次打开自动回滚恢复）；
      - 配置 / 媒体索引等所有持久化写盘均为 tmp→replace 原子写，
        硬退不会产生半截文件；
      - 所有后台线程均为 daemon=True，无非守护线程需要等待。
    """
    diag("SHUTDOWN_BEGIN")
    try:
        api._stop_tray()
    except Exception:
        pass
    try:
        from history_db import get_history_db
        get_history_db().close()
    except Exception:
        pass
    diag("EXIT_HARD")
    os._exit(0)


if __name__ == "__main__":
    main()
