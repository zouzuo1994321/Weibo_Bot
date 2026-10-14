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


def _ts_from_str(s: str) -> int:
    """把 'YYYY-MM-DD HH:MM:SS' 解析为 Unix 秒；失败返回 0。"""
    if not s:
        return 0
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return 0


def _predict_expiry(encrypted_cookie: str, last_ok_at_ts: int = 0) -> dict:
    """解密后预测 Cookie 失效时间（供 list_accounts 脱敏输出）。

    解析 / 续期相关逻辑在 weibo_client，这里只做安全封装：任何异常都返回空预测，
    不影响账户列表正常渲染。
    """
    if not encrypted_cookie:
        return {"has_expiry": False, "expired": False, "level": "unknown"}
    try:
        from weibo_client import predict_cookie_expiry
        cookie = _decrypt(encrypted_cookie)
        if not cookie:
            return {"has_expiry": False, "expired": False, "level": "unknown"}
        return predict_cookie_expiry(cookie, last_ok_at_ts=last_ok_at_ts)
    except Exception:
        return {"has_expiry": False, "expired": False, "level": "unknown"}


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
        # 迁移：补齐 last_ok_at 字段（旧数据没有）
        changed = False
        for a in self.accounts:
            if "last_ok_at" not in a:
                # 优先用最近续期/使用时间，最后退化为创建时间，作为「最近成功活动」估计
                a["last_ok_at"] = a.get("last_renew") or a.get("last_used") or a.get("created_at", "")
                changed = True
        if changed:
            self.save()
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
            "wb_uid": "",        # 账户自身 UID（v1.1.3：profile/info 已强制要求 uid，缓存后免解析首页）
            "cookie": _encrypt(cookie.strip()),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_used": "",
            # 账户健康检测（手动/自动）
            "status": "unknown",        # unknown / ok / cookie_expired / no_repost_perm
            "last_check": "",
            "check_detail": "",
            "last_renew": "",          # 最近一次 Cookie 续期时间（滑动续期/自动续期回写）
            "last_ok_at": "",          # 最近一次「成功活动」时间（成功检测/续期/使用），用于 7 天窗口滑动
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
                "wb_uid": a.get("wb_uid", ""),
                "display_name": display,
                "created_at": a.get("created_at", ""),
                "last_used": a.get("last_used", ""),
                "has_cookie": bool(a.get("cookie")),
                "status": a.get("status", "unknown"),
                "last_check": a.get("last_check", ""),
                "check_detail": a.get("check_detail", ""),
                "last_renew": a.get("last_renew", ""),
                "last_ok_at": a.get("last_ok_at", ""),
                "cookie_expiry": _predict_expiry(a.get("cookie", ""), _ts_from_str(a.get("last_ok_at", ""))),
            })
        return out

    def set_check_result(self, acc_id: str, status: str, detail: str,
                         wb_nickname: str = None, wb_uid: str = None):
        """记录账户健康检测结果，可选回填微博昵称与账户自身 UID。"""
        for a in self.accounts:
            if a["id"] == acc_id:
                a["status"] = status
                a["check_detail"] = detail
                a["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if status == "ok":
                    a["last_ok_at"] = a["last_check"]
                if wb_nickname:
                    a["wb_nickname"] = wb_nickname
                if wb_uid:
                    a["wb_uid"] = wb_uid
                self.save()
                return True
        return False

    def get_cookie(self, acc_id: str):
        for a in self.accounts:
            if a["id"] == acc_id:
                return _decrypt(a.get("cookie", ""))
        return ""

    def update_cookie(self, acc_id: str, new_cookie: str, reason: str = ""):
        """回写续期后的 Cookie（v1.1.1 会话保活）。

        微博在网页请求响应里会通过 Set-Cookie 续期 SUB / SUBP / XSRF-TOKEN / ALF，
        把续期结果写回账户即等价于「滑动续期」，可显著延长 Cookie 寿命，
        减少用户必须手动重新登录的次数。仅在 Cookie 确有变化时落盘。
        """
        if not new_cookie or not new_cookie.strip():
            return False
        for a in self.accounts:
            if a["id"] != acc_id:
                continue
            if _decrypt(a.get("cookie", "")) == new_cookie.strip():
                return False
            a["cookie"] = _encrypt(new_cookie.strip())
            a["last_ok_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if reason:
                a["check_detail"] = reason
            self.save()
            return True
        return False

    def mark_used(self, acc_id: str):
        for a in self.accounts:
            if a["id"] == acc_id:
                a["last_used"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                a["last_ok_at"] = a["last_used"]
                self.save()
                return

    def set_last_renew(self, acc_id: str, ts: str):
        """记录该账户最近一次 Cookie 续期时间（自动/手动滑动续期回写）。"""
        for a in self.accounts:
            if a["id"] == acc_id:
                a["last_renew"] = ts
                self.save()
                return True
        return False


_manager = None


def get_account_manager() -> AccountManager:
    global _manager
    if _manager is None:
        _manager = AccountManager()
    return _manager
