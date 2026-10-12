# -*- coding: utf-8 -*-
"""v1.2.1 内部版本号管理（沿用既定 date+seq 规则）。

版本号命名规则（用户约定）：
  · 大版本迭代 → 改**第二位**（如 v1.1.9k → v1.2.0）
  · 小版本迭代 → 改**第三位**（如 v1.2.0 → v1.2.1）
  · 内部版本号 → YYMMDD + 4位迭代序号（date=年月，seq=序号）
"""
import json
import os
import sys
from datetime import datetime

MAJOR = "v1.4.5"

def _base_dir():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = _base_dir()
STATE_FILE = os.path.join(BASE_DIR, "version.json")

def _load():
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def get_version():
    if not os.path.exists(STATE_FILE):
        return {"major": MAJOR, "internal": "unknown",
                "display": MAJOR + " (内部版本未知)"}
    s = _load()
    return {"major": MAJOR,
            "internal": "%s%04d" % (s["date"], s["seq"]),
            "display": "%s (内部 %s%04d)" % (MAJOR, s["date"], s["seq"])}

if __name__ == "__main__":
    print(get_version()["display"])
