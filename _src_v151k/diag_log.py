# -*- coding: utf-8 -*-
"""【v1.5.0】启动 / 退出分阶段诊断日志（Part 2 方案 A）。

背景：用户反馈「每 3 次里只有 1 次能正常打开进入界面」，且卡死发生在
开屏动画（正在加载数据…）→ 主界面之间。此前的外部启动测试只判
「窗口出现」，无法知道 Python 侧走到哪一步、WebView2 桥是否打通、
界面是否真正渲染完成 —— 测试不够严谨，故障也看不见。

本模块提供零依赖、线程安全的分阶段落盘日志，让每次启动的关键路径
都有毫秒级时间戳证据，用于定位真正的卡点：

  LAUNCH → SINGLE_INST_OK → DIRS_OK → MIGRATE_DONE → CA_OK → API_OK
  → WINDOW_CREATED → WEBVIEW_START（阻塞）
  → BOOT_STAGE n（前端开屏各阶段，经 js 桥回传）
  → UI_READY（开屏淡出 = 真正进入主界面，外部测试以此判定）
  → STATE_CACHE_BUILT（后台状态重建完成）
  → WINDOW_CLOSED → SHUTDOWN_BEGIN → EXIT_HARD

- 日志文件：<DATA_DIR>/diag.log（追加写，毫秒时间戳）
- 写入方式：os.open(O_APPEND) 单次 write，每行 <1ms（js 桥同步调用也安全）
- 超过 2MB 自动截断保留尾部 256KB，防止无限增长
- 任何异常一律静默吞掉：诊断日志绝不允许反过来影响启动主流程
"""
import os
import threading
import time

from paths import DATA_DIR

_LOG_PATH = os.path.join(DATA_DIR, "diag.log")
_LOCK = threading.Lock()
_MAX_BYTES = 2 * 1024 * 1024
_KEEP_BYTES = 256 * 1024
_truncate_checked = False


def diag(stage, detail=""):
    """追加一行诊断日志。绝不抛异常、绝不阻塞主流程。"""
    global _truncate_checked
    try:
        with _LOCK:
            if not _truncate_checked:
                _truncate_checked = True
                try:
                    if os.path.exists(_LOG_PATH) and \
                            os.path.getsize(_LOG_PATH) > _MAX_BYTES:
                        with open(_LOG_PATH, "rb") as f:
                            f.seek(-_KEEP_BYTES, 2)
                            tail = f.read()
                        tmp = _LOG_PATH + ".tmp"
                        with open(tmp, "wb") as f:
                            f.write(b"--- truncated ---\n" + tail)
                        os.replace(tmp, _LOG_PATH)
                except Exception:
                    pass
            now_ms = (int(time.time() * 1000)) % 1000
            line = "%s.%03d | %-18s | %s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), now_ms,
                stage, detail)
            fd = os.open(_LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line.encode("utf-8", "replace"))
            finally:
                os.close(fd)
    except Exception:
        pass


def log_path():
    """诊断日志绝对路径（测试脚本 / 设置页展示用）。"""
    return _LOG_PATH
