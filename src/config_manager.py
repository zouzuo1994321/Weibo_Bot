# -*- coding: utf-8 -*-
"""全局配置管理：读取与保存软件设置。"""
import json
import os
import threading

from paths import CONFIG_PATH

# 默认配置
DEFAULT_CONFIG = {
    "interval_minutes": 30,          # 轮询间隔（分钟）
    "ai_enabled": False,             # AI 增强转发开关
    "persona": "幽默",               # 当前转发人格
    "custom_personas": {},           # 自定义人格: {name: [templates]}
    "blacklist": [],                 # 关键词黑名单
    "working_hours": {               # 工作时间
        "enabled": False,
        "start": "09:00",
        "end": "23:00",
    },
    "max_accounts": 5,               # 最多记录账户数
    "forward_mode": "random_one",    # 转发模式：random_one 随机一条
    "monitor_auto_refresh": {        # 监控列表自动刷新昵称/状态
        "enabled": False,
        "minutes": 10,
    },
}


class ConfigManager:
    _lock = threading.Lock()

    def __init__(self):
        self.data = dict(DEFAULT_CONFIG)
        self.load()

    def load(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self.data.update(loaded)
            except Exception:
                pass
        # 补全缺失字段
        for k, v in DEFAULT_CONFIG.items():
            if k not in self.data:
                self.data[k] = v
        return self.data

    def save(self):
        with ConfigManager._lock:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def update(self, patch: dict):
        self.data.update(patch)
        self.save()

    def to_dict(self):
        return dict(self.data)


_config = None


def get_config() -> ConfigManager:
    global _config
    if _config is None:
        _config = ConfigManager()
    return _config
