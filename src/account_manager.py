# -*- coding: utf-8 -*-
"""账户管理：最多记录 5 个微博账户的 Cookie，本地加密存储。"""
import json
import os
import threading
import uuid
from datetime import datetime

from cryptography.fernet import Fernet

from config_manager import get_config
from paths import ACCOUNTS_PATH, DATA_DIR, ensure_dirs

ensure_dirs()  # 确保 data 目录存在，便于写入加密密钥

KEY_PATH = os.path.join(DATA_DIR, ".acc_key")
_lock = threading.Lock()


def _load_key():
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, "rb") as f:
            return f.read()
    key = Fernet.generate_key()
    with open(KEY_PATH, "wb") as f:
        f.write(key)
    # 仅当前用户可读
    try:
        os.chmod(KEY_PATH, 0o600)
    except Exception:
        pass
    return key


_cipher = Fernet(_load_key())


def _encrypt(text: str) -> str:
    if not text:
        return ""
    return _cipher.encrypt(text.encode("utf-8")).decode("utf-8")


def _decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _cipher.decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception:
        return ""


class AccountManager:
    def __init__(self):
        self.accounts = []
        self.load()

    def load(self):
        if os.path.exists(ACCOUNTS_PATH):
            try:
                with open(ACCOUNTS_PATH, "r", encoding="utf-8") as f:
                    self.accounts = json.load(f)
            except Exception:
                self.accounts = []
        return self.accounts

    def save(self):
        with _lock:
            with open(ACCOUNTS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.accounts, f, ensure_ascii=False, indent=2)

    def max_accounts(self):
        return get_config().get("max_accounts", 5)

    def add_account(self, remark: str, cookie: str):
        """添加账户；超过上限返回 False。

        remark 为用户自定义备注；wb_nickname 为登录/检测后从 Cookie 回填的微博昵称。
        """
        if not cookie or not cookie.strip():
            return False, "Cookie 不能为空"
        if len(self.accounts) >= self.max_accounts():
            return False, f"已达到最大账户数 {self.max_accounts()}，无法继续添加"
        acc = {
            "id": uuid.uuid4().hex[:12],
            "remark": remark or f"账户{len(self.accounts)+1}",
            "wb_nickname": "",   # 从 Cookie 读取到的微博昵称（检测/登录后回填）
            "cookie": _encrypt(cookie.strip()),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_used": "",
            # 账户健康检测（手动/自动）
            "status": "unknown",        # unknown / ok / cookie_expired / no_repost_perm
            "last_check": "",
            "check_detail": "",
        }
        self.accounts.append(acc)
        self.save()
        return True, acc["id"]

    def remove_account(self, acc_id: str):
        before = len(self.accounts)
        self.accounts = [a for a in self.accounts if a["id"] != acc_id]
        if len(self.accounts) != before:
            self.save()
            return True
        return False

    def list_accounts(self):
        """返回脱敏后的账户列表（不含明文 cookie）。

        display_name = 微博昵称 + （用户备注）；昵称缺失时退化为备注。
        """
        out = []
        for a in self.accounts:
            wb = a.get("wb_nickname", "")
            remark = a.get("remark", a.get("nickname", ""))  # 兼容旧数据
            if wb:
                display = wb + (f"（{remark}）" if remark else "")
            else:
                display = remark or "未命名账户"
            out.append({
                "id": a["id"],
                "remark": remark,
                "wb_nickname": wb,
                "display_name": display,
                "created_at": a.get("created_at", ""),
                "last_used": a.get("last_used", ""),
                "has_cookie": bool(a.get("cookie")),
                "status": a.get("status", "unknown"),
                "last_check": a.get("last_check", ""),
                "check_detail": a.get("check_detail", ""),
            })
        return out

    def set_check_result(self, acc_id: str, status: str, detail: str, wb_nickname: str = None):
        """记录账户健康检测结果，可选回填微博昵称。"""
        for a in self.accounts:
            if a["id"] == acc_id:
                a["status"] = status
                a["check_detail"] = detail
                a["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if wb_nickname:
                    a["wb_nickname"] = wb_nickname
                self.save()
                return True
        return False

    def get_cookie(self, acc_id: str):
        for a in self.accounts:
            if a["id"] == acc_id:
                return _decrypt(a.get("cookie", ""))
        return ""

    def mark_used(self, acc_id: str):
        for a in self.accounts:
            if a["id"] == acc_id:
                a["last_used"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.save()
                return


_manager = None


def get_account_manager() -> AccountManager:
    global _manager
    if _manager is None:
        _manager = AccountManager()
    return _manager
