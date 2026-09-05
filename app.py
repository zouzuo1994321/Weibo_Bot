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

    ver = get_version()
    ver_note = f"{APP_NAME} {ver['display']}"

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
    # 使用 Edge WebView2 内核（edgechromium），无需 .NET/pythonnet 依赖；
    # 若本机未安装 WebView2 运行时，请先从微软官网安装。
    try:
        webview.start(gui="edgechromium", debug=False)
    except Exception:
        # 回退到自动选择的内核
        webview.start(debug=False)


if __name__ == "__main__":
    main()
