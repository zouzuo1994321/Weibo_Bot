# -*- coding: utf-8 -*-
"""
微博bot小助手 —— 独立桌面程序入口。
使用 pywebview 在原生窗口中运行（不依赖外部浏览器），前端为本地 HTML/CSS/JS。
"""
import os
import sys

# 将项目根目录与 src 目录加入模块搜索路径，保证各模块可被导入
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(BASE_DIR, "src")
for p in (BASE_DIR, SRC_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from paths import ensure_dirs, BASE_DIR as _BD  # noqa: E402
from paths import APP_NAME, MAJOR_VERSION  # noqa: E402

ensure_dirs()

from version import get_version  # noqa: E402
from api_bridge import Api  # noqa: E402


def main():
    import webview
    from logger import info, error, CAT_CORE

    ver = get_version()
    ver_note = f"{APP_NAME} {ver['display']}"
    info(f"===== {APP_NAME} 启动 ===== 版本 {ver['display']}（内部 {ver.get('internal', '')}）"
         f" · Python {sys.version.split()[0]} · 平台 {sys.platform}", CAT_CORE)
    info(f"运行环境：{'打包 exe' if getattr(sys, 'frozen', False) else '源码开发'} · 数据目录 {_BD}", CAT_CORE)

    # 关键：禁止把新窗口请求甩到系统浏览器。
    # 否则微博登录页（login/passport）触发的 NewWindowRequested 会被 pywebview
    # 用 webbrowser.open() 打开外部浏览器；改为在内部 webview 中加载同一会话。
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False

    # 打包后 UI 资源位于 _MEIPASS；开发环境使用项目内 ui/ 目录
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        ui_path = os.path.join(sys._MEIPASS, "ui", "index.html")
    else:
        ui_path = os.path.join(_BD, "ui", "index.html")

    api = Api()
    window = webview.create_window(
        title=ver_note,
        url=ui_path,
        js_api=api,
        width=1180,
        height=760,
        min_size=(900, 600),
        text_select=True,
        frameless=False,
    )
    # 记录窗口引用，便于登录窗口等场景
    api.main_window = window
    try:
        from monitor_manager import get_monitor_manager
        from account_manager import get_account_manager
        info(f"初始状态：监控对象 {get_monitor_manager().count()} 个"
             f" · 账户 {len(get_account_manager().list_accounts())} 个", CAT_CORE)
    except Exception as e:
        error(f"读取初始状态失败：{e}", CAT_CORE)
    # 使用 Edge WebView2 内核（edgechromium），无需 .NET/pythonnet 依赖；
    # 若本机未安装 WebView2 运行时，请先从微软官网安装。
    try:
        webview.start(gui="edgechromium", debug=False)
        info("===== 程序正常退出 =====", CAT_CORE)
    except Exception as e:
        error(f"edgechromium 内核启动失败，回退默认内核：{e}", CAT_CORE)
        webview.start(debug=False)
        info("===== 程序正常退出（回退内核）=====", CAT_CORE)


if __name__ == "__main__":
    main()
