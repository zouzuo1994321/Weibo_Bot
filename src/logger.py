# -*- coding: utf-8 -*-
"""操作日志：同时写入本地文件与数据库（供前端查询）。"""
import datetime
import os
import threading

from paths import LOG_PATH

_lock = threading.Lock()


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write_file(level: str, message: str):
    try:
        with _lock:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{_now()}][{level}] {message}\n")
    except Exception:
        pass


def _write_db(level: str, message: str):
    try:
        # 延迟导入，避免循环依赖
        from history_db import get_history_db
        db = get_history_db()
        db.add_log(level, message)
    except Exception:
        pass


def log(level: str, message: str):
    """记录一条日志（文件 + 数据库）。"""
    _write_file(level, message)
    _write_db(level, message)


def info(message: str):
    log("INFO", message)


def warn(message: str):
    log("WARN", message)


def error(message: str):
    log("ERROR", message)


def debug(message: str):
    log("DEBUG", message)


def read_file_lines(limit: int = 500):
    if not os.path.exists(LOG_PATH):
        return []
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return [l.rstrip("\n") for l in lines[-limit:]]
    except Exception:
        return []
