# -*- coding: utf-8 -*-
"""调度器：按设定间隔在后台线程轮询，支持工作时间限制。"""
import random
import threading
import time
import traceback
from datetime import datetime

from config_manager import get_config
from forward_engine import run_once
from logger import info, warn, error, debug, CAT_SCHED
from media_engine import maybe_run_scheduled_media_download


def _in_working_hours() -> bool:
    cfg = get_config()
    wh = cfg.get("working_hours", {})
    if not wh.get("enabled"):
        return True
    try:
        start = datetime.strptime(wh.get("start", "00:00"), "%H:%M").time()
        end = datetime.strptime(wh.get("end", "23:59"), "%H:%M").time()
    except Exception:
        return True
    now = datetime.now().time()
    if start <= end:
        return start <= now <= end
    # 跨天（如 22:00 - 08:00）
    return now >= start or now <= end


class Scheduler:
    def __init__(self):
        self._running = False
        self._thread = None
        self.last_run = None
        self.next_run = None
        self.last_result = None
        self._lock = threading.Lock()

    def is_running(self):
        """只有标记为运行中、且后台线程确实存活，才算真正在运行。

        避免调度线程因异常退出后 `self._running` 仍为 True，
        导致界面一直显示“运行中”却永远不再轮询（假死状态）。
        """
        return bool(self._running) and self._thread is not None and self._thread.is_alive()

    def start(self):
        # 线程还活着 → 已在运行，无需重复启动
        if self._running and self._thread is not None and self._thread.is_alive():
            return False
        # 自愈：标记为运行中但线程已死（异常退出），重建线程而不阻塞用户重启
        if self._running and self._thread is not None and not self._thread.is_alive():
            warn("检测到调度线程已异常退出，正在自动重启", CAT_SCHED)
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        info("调度器已启动", CAT_SCHED)
        return True

    def stop(self):
        self._running = False
        info("调度器已停止", CAT_SCHED)
        return True

    def _loop(self):
        while self._running:
            interval = None
            try:
                cfg = get_config()
                base = max(1, int(cfg.get("interval_minutes", 30)))
                # 轮询间隔抖动：在 ±poll_jitter_ratio 范围内随机，避免固定节奏被识别为机器人
                ratio = cfg.get("poll_jitter_ratio", 0.2)
                try:
                    ratio = float(ratio)
                except Exception:
                    ratio = 0.2
                jitter = base * ratio
                interval = max(1, int(round(base + random.uniform(-jitter, jitter))))
                with self._lock:
                    self.next_run = (datetime.now().timestamp() + interval * 60)
                next_run_str = datetime.fromtimestamp(self.next_run).strftime("%H:%M")
                in_wh = _in_working_hours()
                info(f"调度器心跳 · 下次轮询约 {next_run_str}（间隔 {interval} 分钟）"
                 + ("" if in_wh else " · 当前非工作时间，等待中"), CAT_SCHED)
                # 视频 / 相册：到达每日设定时间且当天未执行过，则触发一次下载
                try:
                    maybe_run_scheduled_media_download()
                except Exception as e:
                    error(f"检查媒体下载定时失败：{e}", CAT_SCHED)
                if in_wh:
                    try:
                        res = run_once()
                        self.last_result = res
                        self.last_run = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    except Exception as e:
                        error(f"调度执行异常：{e}", CAT_SCHED)
                        error(traceback.format_exc(), CAT_SCHED)
                else:
                    warn("当前不在工作时间，跳过本轮轮询", CAT_SCHED)
            except Exception as e:
                # 兜底：循环体内任何未预料的异常都必须留下痕迹。
                # 否则线程会静默退出，界面仍显示“运行中”却永不再轮询——
                # 这正是历史上 `random` 未导入导致“自动轮询无法启动”且日志无任何报错的原因。
                error(f"调度循环异常：{e}", CAT_SCHED)
                error(traceback.format_exc(), CAT_SCHED)
            # 按（抖动后的）间隔等待；若本轮异常导致 interval 未算出，退回到配置间隔
            if interval is None:
                try:
                    interval = max(1, int(get_config().get("interval_minutes", 30)))
                except Exception:
                    interval = 30
            waited = 0
            while self._running and waited < interval * 60:
                time.sleep(5)
                waited += 5

    def status(self):
        cfg = get_config()
        return {
            "running": self.is_running(),
            "interval_minutes": cfg.get("interval_minutes", 30),
            "last_run": self.last_run,
            "next_run": self.next_run,
            "in_working_hours": _in_working_hours(),
            "last_result": self.last_result,
        }


_scheduler = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler
