# -*- coding: utf-8 -*-
"""
内部版本号管理：规则为 年月日(YYMMDD) + 4位当日迭代序号。
示例：2026-09-04 第 1 次构建 -> 2609040001
大版本号（对外发布名）为 v1.0，由 MAJOR 常量控制。

打包（PyInstaller onefile）后，version.py 与 version.json 会被解压到
_MEIPASS 临时目录；因此读取版本号时必须优先使用 _MEIPASS 路径，
否则会找不到 version.json 而误用 seq=0 的兜底版本号。
"""
import json
import os
import sys
from datetime import datetime

MAJOR = "v1.1.0"


def _base_dir():
    """返回 version.json 所在目录。

    - 开发环境：本文件同级目录；
    - 打包后（PyInstaller onefile）：_MEIPASS 临时目录（version.json 由 build.py 打入）。
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _base_dir()
STATE_FILE = os.path.join(BASE_DIR, "version.json")


def _load():
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(state):
    # 仅在开发环境写入；打包后 _MEIPASS 为只读临时目录，不应再写。
    if getattr(sys, "frozen", False):
        return
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def get_version():
    # 打包后若因故缺失 version.json，给出明确兜底（而非静态默认号）。
    if not os.path.exists(STATE_FILE):
        return {
            "major": MAJOR,
            "internal": "unknown",
            "display": f"{MAJOR} (内部版本未知)",
        }
    state = _load()
    return {
        "major": MAJOR,
        "internal": f"{state['date']}{state['seq']:04d}",
        "display": f"{MAJOR} (内部 {state['date']}{state['seq']:04d})",
    }


def bump_version():
    """构建一次则自增序号；若跨日则日期更新并重置序号。"""
    state = _load()
    today = datetime.now().strftime("%y%m%d")
    if state["date"] != today:
        state["date"] = today
        state["seq"] = 1
    else:
        state["seq"] += 1
    _save(state)
    return get_version()


if __name__ == "__main__":
    print(get_version()["display"])
