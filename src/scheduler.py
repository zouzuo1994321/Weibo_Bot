# -*- coding: utf-8 -*-
"""调度器：按设定间隔在后台线程轮询，支持工作时间限制。"""
import threading
import time
from datetime import datetime

from config_manager import get_config
from forward_engine import run_once
from logger import info, warn, error


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
        return self._running

    def start(self):
        if self._running:
            return False
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        info("调度器已启动")
        return True

    def stop(self):
        self._running = False
        info("调度器已停止")
        return True

    def _loop(self):
        while self._running:
            cfg = get_config()
            interval = max(1, int(cfg.get("interval_minutes", 30)))
            with self._lock:
                self.next_run = (datetime.now().timestamp() + interval * 60)
            if _in_working_hours():
                try:
                    res = run_once()
                    self.last_result = res
                    self.last_run = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                except Exception as e:
                    error(f"调度执行异常：{e}")
            else:
                warn("当前不在工作时间，跳过本轮轮询")
            # 按间隔等待
            waited = 0
            while self._running and waited < interval * 60:
                time.sleep(5)
                waited += 5

    def status(self):
        cfg = get_config()
        return {
            "running": self._running,
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
