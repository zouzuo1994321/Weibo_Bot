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
    "ai_engine": "rule",             # AI 引擎路线：model=本地大模型(llama.cpp) / rule=轻量规则(jieba)
    "persona": "幽默",               # 当前转发人格
    "custom_personas": {},           # 自定义人格: {name: [templates]}
    "blacklist": [],                 # 关键词黑名单
    "working_hours": {               # 工作时间
        "enabled": False,
        "start": "09:00",
        "end": "23:00",
    },
    "forward_time_jitter": True,     # 转发时间在工作时段内随机偏移（避免固定时刻批量转发）
    "poll_jitter_ratio": 0.15,       # 轮询间隔 / 转发延后 抖动比例（±15%）
    "max_accounts": 5,               # 最多记录账户数
    "forward_mode": "random_one",    # 转发模式：random_one 随机一条
    "monitor_auto_refresh": {        # 监控列表自动刷新昵称/状态
        "enabled": False,
        "minutes": 10,
    },
    "media_download": {              # 相册监控（自动下载监控对象的相册图片）
        "enabled": False,            #   自动下载开关
        "interval_minutes": 30,      #   执行间隔（分钟），与监控轮询节奏一致
        "max_pages": 5,              #   每个监控对象最多翻页数（每页约 10 条）
        "last_run_ts": 0,            #   上次执行时间戳（用于间隔判定）
    },
    "log_verbose": False,            # 冗余调试日志（DEBUG 级）
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
