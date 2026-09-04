# -*- coding: utf-8 -*-
"""监控管理：维护被监控的微博 UID 列表（无数量上限），记录最新微博状态。"""
import json
import os
import threading
from datetime import datetime

from paths import DATA_DIR

MONITOR_PATH = os.path.join(DATA_DIR, "monitor.json")
_lock = threading.Lock()


class MonitorManager:
    def __init__(self):
        self.items = []
        self.load()

    def load(self):
        if os.path.exists(MONITOR_PATH):
            try:
                with open(MONITOR_PATH, "r", encoding="utf-8") as f:
                    self.items = json.load(f)
            except Exception:
                self.items = []
        return self.items

    def save(self):
        with _lock:
            with open(MONITOR_PATH, "w", encoding="utf-8") as f:
                json.dump(self.items, f, ensure_ascii=False, indent=2)

    def add(self, uid: str, screen_name: str = ""):
        uid = str(uid).strip()
        if not uid:
            return False, "UID 不能为空"
        for it in self.items:
            if it["uid"] == uid:
                return False, "该 UID 已在监控列表中"
        self.items.append({
            "uid": uid,
            "screen_name": screen_name,
            "avatar_url": "",
            "avatar_local": "",
            "last_post_id": "",
            "last_post_text": "",
            "last_check": "",
            "status": "unknown",   # unknown / online / offline / error
            "pinned": False,      # 是否置顶（置顶对象在列表中靠前排列）
            "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        self.save()
        return True, uid

    def remove(self, uid: str):
        before = len(self.items)
        self.items = [it for it in self.items if it["uid"] != uid]
        if len(self.items) != before:
            self.save()
            return True
        return False

    def list(self):
        return [dict(it) for it in self.items]

    def get(self, uid: str):
        for it in self.items:
            if it["uid"] == uid:
                return it
        return None

    def update(self, uid: str, patch: dict):
        for it in self.items:
            if it["uid"] == uid:
                it.update(patch)
                self.save()
                return True
        return False

    def set_pinned(self, uid: str, pinned: bool):
        for it in self.items:
            if it["uid"] == uid:
                it["pinned"] = bool(pinned)
                self.save()
                return True
        return False

    def count(self):
        return len(self.items)


_manager = None


def get_monitor_manager() -> MonitorManager:
    global _manager
    if _manager is None:
        _manager = MonitorManager()
    return _manager
