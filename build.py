# -*- coding: utf-8 -*-
"""
封装脚本：自增内部版本号 -> 用 PyInstaller 打包为独立 exe -> 归档旧版本。
用法：python build.py
产物：根目录「微博bot小助手.exe」（最新版）；旧版本移入 history/ 留档。
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
EXE_NAME = "微博bot小助手.exe"

def _resolve_python():
    """优先使用项目专属 venv（weibobot），其依赖与打包需求一致，
    可避免 managed 全局解释器缺失 cryptography 等模块导致打包出的 exe 报错。"""
    venv = os.path.join(
        os.path.expanduser("~"), ".workbuddy", "binaries", "python",
        "envs", "weibobot", "Scripts", "python.exe")
    if os.path.exists(venv):
        return venv
    return sys.executable

PY = _resolve_python()  # 打包用的 Python（项目 venv 优先）


def log(msg):
    print(f"[build] {msg}")


def main():
    # 1) 读取旧版本号（用于归档命名），随后自增
    from version import get_version
    old = get_version()
    old_internal = old["internal"]

    old_exe = os.path.join(BASE_DIR, EXE_NAME)
    if os.path.exists(old_exe):
        archive = os.path.join(HISTORY_DIR, f"微博bot小助手_{old_internal}.exe")
        shutil.move(old_exe, archive)
        log(f"旧版本已归档：history/微博bot小助手_{old_internal}.exe")

    new_ver = __import__("version").bump_version()
    log(f"新内部版本号：{new_ver['internal']}（{new_ver['display']}）")

    # 2) 打包
    os.makedirs(HISTORY_DIR, exist_ok=True)
    cmd = [
        PY, "-m", "PyInstaller",
        "--name", "微博bot小助手",
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
        "--collect-data", "webview",
        "--collect-submodules", "jieba",
        "--collect-data", "jieba",
        "--hidden-import", "llama_cpp",
        "--hidden-import", "llama_cpp.llama",
        "--collect-submodules", "llama_cpp",
        "--collect-data", "llama_cpp",
        "--icon", "logo.ico",
        "app.py",
    ]
    log("开始打包（PyInstaller）…")
    proc = subprocess.run(cmd, cwd=BASE_DIR)
    if proc.returncode != 0:
        log("打包失败，请查看上方错误信息。")
        sys.exit(1)

    # 3) 放置最新 exe 到根目录
    built = os.path.join(DIST_DIR, EXE_NAME)
    if not os.path.exists(built):
        log("未找到打包产物，打包可能失败。")
        sys.exit(1)
    shutil.move(built, old_exe)
    log(f"最新版已生成：{EXE_NAME}（内部版本 {new_ver['internal']}）")
    # 清理中间目录
    shutil.rmtree(BUILD_DIR, ignore_errors=True)
    shutil.rmtree(DIST_DIR, ignore_errors=True)
    log("完成。根目录为最新 exe，旧版本在 history/ 留档。")


if __name__ == "__main__":
    main()
