# -*- coding: utf-8 -*-
"""
用 Python 3.8 打包「Win7 兼容版」独立 exe。
- 目标系统：Windows 7 / Server 2008 R2（最后一个支持 Win7 的 Python 是 3.8）。
- 已内嵌 VC++ 运行库 vcruntime140.dll（PyInstaller 自动打包）。
- 目标机需另装：Microsoft Edge WebView2 运行时（pywebview 的 edgechromium 内核硬性依赖，
  安装它会一并提供 UCRT，故无需单独内嵌 UCRT）。
用法：C:/Users/zouzu/py38/python.exe build_win7.py
产物：根目录「微博bot小助手_Win7兼容版.exe」
"""
import os
import shutil
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(BASE_DIR, "src")
HISTORY_DIR = os.path.join(BASE_DIR, "history")
DIST_DIR = os.path.join(BASE_DIR, "dist")
BUILD_DIR = os.path.join(BASE_DIR, "build")
EXE_NAME = "微博bot小助手_Win7兼容版.exe"

# 打包专用 Python 3.8（最后一个支持 Windows 7 的版本）
PY = r"C:\Users\zouzu\py38\python.exe"


def main():
    os.makedirs(HISTORY_DIR, exist_ok=True)
    cmd = [
        PY, "-m", "PyInstaller",
        "--name", "微博bot小助手_Win7兼容版",
        "--onefile",
        "--windowed",
        "--noconfirm",
        "--clean",
        f"--add-data", f"ui{os.pathsep}ui",
        f"--add-data", f"version.json{os.pathsep}.",
        f"--paths", SRC_DIR,
        f"--paths", BASE_DIR,
        "--hidden-import", "version",
        "--hidden-import", "webview",
        "--hidden-import", "webview.guilib",
        "--hidden-import", "webview.platforms.edgechromium",
        "--hidden-import", "webview.js",
        "--collect-data", "webview",
        "--collect-submodules", "jieba",
        "--collect-data", "jieba",
        "--icon", "logo.ico",
        "app.py",
    ]
    print("[build_win7] 开始打包（PyInstaller / Python 3.8）…")
    proc = subprocess.run(cmd, cwd=BASE_DIR)
    if proc.returncode != 0:
        print("[build_win7] 打包失败，请查看上方错误信息。")
        sys.exit(1)

    built = os.path.join(DIST_DIR, EXE_NAME)
    if not os.path.exists(built):
        print("[build_win7] 未找到打包产物，打包可能失败。")
        sys.exit(1)
    shutil.move(built, os.path.join(BASE_DIR, EXE_NAME))
    shutil.rmtree(BUILD_DIR, ignore_errors=True)
    shutil.rmtree(DIST_DIR, ignore_errors=True)
    print(f"[build_win7] 完成：{EXE_NAME}（已内嵌 vcruntime140.dll，适用 Windows 7/8.1/10/11）")


if __name__ == "__main__":
    main()
