# -*- coding: utf-8 -*-
"""
路径与全局常量定义。
打包为 exe 后，数据目录会放在 exe 同级的 data/ 下，保证数据持久化。
"""
import os
import sys

# 判断是否为 PyInstaller 打包后的运行环境
FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    # exe 所在目录（保证数据持久化，不被解压到临时目录）
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    # 开发环境：项目根目录
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 运行时数据目录
DATA_DIR = os.path.join(BASE_DIR, "data")
IMAGE_DIR = os.path.join(DATA_DIR, "images")
HISTORY_DIR = os.path.join(BASE_DIR, "history")  # exe 历史版本留档
DB_PATH = os.path.join(DATA_DIR, "history.db")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
ACCOUNTS_PATH = os.path.join(DATA_DIR, "accounts.json")
LOG_PATH = os.path.join(DATA_DIR, "app.log")

# 大版本号
MAJOR_VERSION = "v1.0"

# 应用元信息
APP_NAME = "微博bot小助手"
COPYRIGHT = "Copyright  2026 肆月Aperture"
COPYRIGHT_FULL = (
    "Copyright  2026 肆月Aperture\n"
    "本软件为开源软件，未经授权禁止用于任何商业用途。"
)


def ensure_dirs():
    """确保运行时所需目录存在。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(IMAGE_DIR, exist_ok=True)
    os.makedirs(HISTORY_DIR, exist_ok=True)
