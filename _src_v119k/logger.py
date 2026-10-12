# -*- coding: utf-8 -*-
"""操作日志：同时写入本地文件与数据库（供前端查询）。

为便于后续排查问题与定位 BUG，日志支持：
  - 多级：DEBUG / INFO / WARN / ERROR
  - 模块分类（category）：可按子系统筛选，快速缩小排查范围
  - 冗余开关（verbose）：关闭时不写 DEBUG 日志，避免正常运行日志被淹没
  - 异常堆栈：exception() 自动附带完整 traceback

文件格式：[时间][级别][模块] 消息

v1.1.8 改异步落盘：log() 仅把写入任务入队，由独立守护线程负责实际
文件 / 数据库写入，避免前端 JS API 调用线程被磁盘 IO 或 SQLite 写入阻塞。

【v1.1.9j 性能修复】
  1. 文件句柄复用：不再每条日志 `open(...,'a')` 一次（在 SMB 上每次都要做权限
     协商 + 元数据写，叠加严重），改为长期持有的句柄 + 缓冲刷盘，由后台线程
     定期 flush。
  2. 文件大小上限 + 自动轮转：app.log 曾涨到 55MB，读取一次要 1s+。现在超过
     MAX_LOG_BYTES 自动轮转为 app.log.1（只保留 1 个历史文件），避免无限膨胀。
  3. read_file_lines 改为「从文件尾部倒序读块」，只读需要的尾部数据，
     不再把整个大文件 readlines() 进内存。
"""
import atexit
import datetime
import os
import queue
import threading
import traceback

from paths import LOG_PATH

_lock = threading.Lock()

# 异步落盘队列 + 守护线程（v1.1.8）：调用方 log() 只入队，不阻塞。
_write_queue = queue.Queue()
_worker = None
_running = False
_stop = threading.Event()

# v1.1.9j：文件大小上限（轮转阈值），默认 8MB；保留 1 个历史文件。
MAX_LOG_BYTES = 8 * 1024 * 1024

# v1.1.9j：长期持有的写句柄（由后台写入线程使用）
_file_fh = None
_fh_lock = threading.Lock()


def _flush_fh():
    fh = _file_fh
    if fh is None:
        return
    try:
        fh.flush()
    except Exception:
        pass


def _worker_loop():
    """后台写入线程：串行处理文件 + DB 落盘，并定期 flush 文件缓冲。"""
    import time
    last_flush = time.time()
    while True:
        try:
            item = _write_queue.get(timeout=1.0)
        except queue.Empty:
            _flush_fh()
            last_flush = time.time()
            if _stop.is_set():
                break
            continue
        if item is None:
            break
        try:
            _write_file(*item)
            _write_db(*item)
        except Exception:
            pass
        now = time.time()
        if now - last_flush >= 1.0:
            _flush_fh()
            last_flush = now


def _ensure_worker():
    global _worker, _running
    if _running:
        return
    _running = True
    _worker = threading.Thread(target=_worker_loop, daemon=True)
    _worker.start()


def _close_fh():
    global _file_fh
    with _fh_lock:
        if _file_fh is not None:
            try:
                _file_fh.flush()
                _file_fh.close()
            except Exception:
                pass
            _file_fh = None


def _flush_on_exit():
    """进程退出前把队列里剩余的日志同步写完，避免丢失尾部日志。"""
    try:
        _stop.set()
        while not _write_queue.empty():
            item = _write_queue.get_nowait()
            if item is None:
                break
            _write_file(*item)
            _write_db(*item)
    except Exception:
        pass
    _close_fh()


atexit.register(_flush_on_exit)


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


def _rotate_if_needed():
    """文件超过上限则轮转：app.log -> app.log.1（覆盖旧的历史文件）。"""
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) >= MAX_LOG_BYTES:
            _close_fh()
            bak = LOG_PATH + ".1"
            try:
                if os.path.exists(bak):
                    os.remove(bak)
            except Exception:
                pass
            try:
                os.replace(LOG_PATH, bak)
            except Exception:
                pass
    except Exception:
        pass


def _get_fh():
    """获取（或建立）长期持有的追加写句柄。"""
    global _file_fh
    if _file_fh is not None:
        return _file_fh
    with _fh_lock:
        if _file_fh is None:
            _rotate_if_needed()
            _file_fh = open(LOG_PATH, "a", encoding="utf-8", buffering=8192)
    return _file_fh


def _write_file(level: str, message: str, category: str):
    try:
        fh = _get_fh()
        fh.write(f"[{_now()}][{level}][{category}] {message}\n")
    except Exception:
        # 句柄失效（轮转 / 外部删除）：丢弃句柄，下次重建
        global _file_fh
        _file_fh = None


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
    """记录一条日志：入队后由守护线程异步落盘（文件 + 数据库），不阻塞调用方。"""
    _ensure_worker()
    try:
        _write_queue.put((level, message, category))
    except Exception:
        # 极端情况下（队列不可用）退化为同步写入，保证不丢日志
        try:
            _write_file(level, message, category)
            _write_db(level, message, category)
        except Exception:
            pass


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
    """从日志文件**尾部**读取最近 limit 行（v1.1.9j：不再整文件 readlines）。

    实现：从文件末尾按 64KB 块向前读，累计换行数达到 limit 即停止；
    即使 app.log 有几十 MB，也只读到最后几十 KB，毫秒级返回。
    """
    if not os.path.exists(LOG_PATH):
        return []
    limit = max(1, int(limit or 1))
    try:
        size = os.path.getsize(LOG_PATH)
        block = 64 * 1024
        data = b""
        with open(LOG_PATH, "rb") as f:
            pos = size
            while pos > 0:
                read_size = min(block, pos)
                pos -= read_size
                f.seek(pos)
                data = f.read(read_size) + data
                if data.count(b"\n") > limit:
                    break
        text = data.decode("utf-8", errors="ignore")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines = lines[:-1]
        return lines[-limit:]
    except Exception:
        return []
