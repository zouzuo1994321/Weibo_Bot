# -*- coding: utf-8 -*-
"""操作日志：同时写入本地文件与数据库（供前端查询）。

为便于后续排查问题与定位 BUG，日志支持：
  - 多级：DEBUG / INFO / WARN / ERROR
  - 模块分类（category）：可按子系统筛选，快速缩小排查范围
  - 冗余开关（verbose）：关闭时不写 DEBUG 日志，避免正常运行日志被淹没
  - 异常堆栈：exception() 自动附带完整 traceback

文件格式：[时间][级别][模块] 消息
"""
import datetime
import os
import threading
import traceback

from paths import LOG_PATH

_lock = threading.Lock()

# ---------- 日志分类（用于前端筛选与问题定位） ----------
CAT_CORE = "CORE"      # 核心 / 启动退出
CAT_CONF = "CONF"      # 配置读写
CAT_ACCT = "ACCT"      # 账户与登录态
CAT_MON = "MON"        # 监控列表
CAT_HTTP = "HTTP"      # 网络请求
CAT_FWD = "FWD"        # 转发流程
CAT_SCHED = "SCHED"    # 调度器
CAT_MEDIA = "MEDIA"    # 视频/相册下载
CAT_AI = "AI"          # AI 生成
CAT_LIC = "LIC"        # 授权

_verbose = False  # 冗余日志开关（控制 DEBUG 级是否落盘）


def set_verbose(flag: bool):
    """开启/关闭冗余调试日志（DEBUG 级）。"""
    global _verbose
    _verbose = bool(flag)


def is_verbose() -> bool:
    return _verbose


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write_file(level: str, message: str, category: str):
    try:
        with _lock:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{_now()}][{level}][{category}] {message}\n")
    except Exception:
        pass


def _write_db(level: str, message: str, category: str):
    try:
        # 延迟导入，避免循环依赖
        from history_db import get_history_db
        db = get_history_db()
        try:
            db.add_log(level, message, category)
        except TypeError:
            # 兼容未升级的旧签名
            db.add_log(level, message)
    except Exception:
        pass


def log(level: str, message: str, category: str = CAT_CORE):
    """记录一条日志（文件 + 数据库）。"""
    _write_file(level, message, category)
    _write_db(level, message, category)


def info(message: str, category: str = CAT_CORE):
    log("INFO", message, category)


def warn(message: str, category: str = CAT_CORE):
    log("WARN", message, category)


def error(message: str, category: str = CAT_CORE):
    log("ERROR", message, category)


def debug(message: str, category: str = CAT_CORE):
    """冗余调试日志：仅在 verbose 开启时记录。

    用于记录 HTTP 细节、每步决策等高频信息，
    默认关闭以保证常规日志可读，排查时可在界面一键开启。
    """
    if not _verbose:
        return
    log("DEBUG", message, category)


def exception(message: str, category: str = CAT_CORE):
    """记录异常及其完整堆栈（定位 BUG 的关键信息）。"""
    try:
        tb = traceback.format_exc().strip()
    except Exception:
        tb = "(无法获取堆栈)"
    if not tb or tb.lower().startswith("noneType"):
        tb = "(无活动异常堆栈)"
    error(f"{message} | 堆栈：{tb}", category)


def read_file_lines(limit: int = 500):
    if not os.path.exists(LOG_PATH):
        return []
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return [l.rstrip("\n") for l in lines[-limit:]]
    except Exception:
        return []
