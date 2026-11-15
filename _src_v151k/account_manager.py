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


# ---------------------------------------------------------------------------
# 【v1.6.5】Cookie 结构化存储（带域条目）
#
# 旧实现只存一份扁平串「name=value; name=value」，domain 维度被压掉：同一个 SUB 在
# `.weibo.com` / `.weibo.cn` / `.sina.com.cn` 下各有一份，压平后互相覆盖，续期回来的
# 那一份会把另一份顶掉。现在额外存一份带域条目（加密 JSON），并且：
#   · 对外 get_cookie() 仍返回扁平串 —— 转发 / 检测 / 热搜 / 预览全部零改动；
#   · cookie_jar 是唯一真相，cookie 字段由它同步导出，两者不会漂移；
#   · 老数据在 load() 里自动迁移，domain 留空 = 通用条目，行为与旧版一致。
# ---------------------------------------------------------------------------
def _jar_load(acc: dict) -> list:
    """解密并解析该账户的带域条目；没有则返回空列表。"""
    token = acc.get("cookie_jar", "")
    if not token:
        return []
    try:
        raw = _decrypt(token)
        if not raw:
            return []
        rows = json.loads(raw)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _jar_store(acc: dict, entries: list):
    """写入带域条目，并同步导出扁平 cookie 字段（唯一真相是 entries）。"""
    from weibo_client import _norm_entries, entries_to_cookie
    rows = _norm_entries(entries)
    acc["cookie_jar"] = _encrypt(json.dumps(rows, ensure_ascii=False))
    acc["cookie"] = _encrypt(entries_to_cookie(rows))
    return rows


def _predict_expiry(encrypted_cookie: str, last_ok_at_ts: int = 0) -> dict:
    """解密后预测 Cookie 失效时间（供 list_accounts 脱敏输出）。

    解析 / 续期相关逻辑在 weibo_client，这里只做安全封装：任何异常都返回空预测，
    不影响账户列表正常渲染。

    【v1.6.7】last_ok_at_ts 传入的应是「最后成功活动」与「最后实测确认」两者较晚者，
    预测日期因此锚定在**真实探测到有效的时刻**上，而不是登录时刻的推算。
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
            # 【v1.6.7】补齐实测字段；老数据用 last_ok_at 兜底播种（那也是一次真实成功请求）
            if "last_verified_at" not in a:
                a["last_verified_at"] = a.get("last_ok_at", "")
                a["verify_result"] = "valid" if a["last_verified_at"] else ""
                changed = True
            # 【v1.6.9】清掉 v1.6.7/v1.6.8 误标的「已失效」。
            # 那两个版本的移动端探针会把「跨域口径未登录」写成 expired，留下的标记
            # **没有 verify_expired_at 时间戳**（当时的 set_verified 不记）。
            # 判据只看事实：账户状态仍是「正常」且确实有过成功活动 → 说明那个 expired
            # 是误报，退回「未实测」比继续挂着红字诚实。新版本写的 expired 一定带时间戳，
            # 因此这里不会误清真正的失效判定。
            if (a.get("verify_result") == "expired"
                    and not a.get("verify_expired_at")
                    and a.get("status") == "ok"
                    and (a.get("last_ok_at") or a.get("last_used"))):
                a["verify_result"] = ""
                changed = True
                try:
                    from logger import info as _inf, CAT_ACCT
                    _inf("清理历史误标的「实测已失效」：%s（状态正常且有成功活动记录，"
                         "该标记来自 v1.6.8 及更早的跨域探针误报）"
                         % (a.get("remark") or a.get("wb_nickname") or a.get("id")),
                         CAT_ACCT)
                except Exception:
                    pass
            # 【v1.6.5】把老的扁平串迁成带域条目（domain 留空 = 通用条目，行为不变）
            if "cookie_jar" not in a:
                raw = _decrypt(a.get("cookie", ""))
                try:
                    if raw:
                        from weibo_client import parse_cookie_entries
                        a["cookie_jar"] = _encrypt(
                            json.dumps(parse_cookie_entries(raw), ensure_ascii=False))
                    else:
                        a["cookie_jar"] = ""
                except Exception:
                    a["cookie_jar"] = ""
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

    def add_account(self, remark: str, cookie: str, entries=None):
        """添加账户；超过上限返回 False。

        remark 为用户自定义备注；wb_nickname 为登录/检测后从 Cookie 回填的微博昵称。
        entries（v1.6.5）为从内置浏览器读到的**带域条目**；传了就按域存，
        没传就把 cookie 解析成「空域通用条目」（与旧版行为一致）。
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
            "cookie": "",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_used": "",
            # 账户健康检测（手动/自动）
            "status": "unknown",        # unknown / ok / cookie_expired / no_repost_perm
            "last_check": "",
            "check_detail": "",
            "last_renew": "",          # 最近一次 Cookie 续期时间（滑动续期/自动续期回写）
            "last_ok_at": "",          # 最近一次「成功活动」时间（成功检测/续期/使用），用于 7 天窗口滑动
            # 【v1.6.7】登录态实测：微博的 Cookie 到期时间只约束浏览器何时删它，
            # 不代表服务端认多久（实测 SUB 写着一年、服务端 7 天就作废），所以「真实有效期」
            # 无法查询、只能探测。这两个字段记录探针的实测结果，账户表据此显示「实测 xx 确认」。
            "last_verified_at": "",    # 最近一次**实测确认登录态有效**的时刻
            "verify_result": "",       # valid / expired / ""（未实测）
            "verify_expired_at": "",   # v1.6.9：被判失效的时刻，用于区分真失效与误报
        }
        from weibo_client import _norm_entries as _norm, parse_cookie_entries
        _jar_store(acc, _norm(entries) or parse_cookie_entries(cookie.strip()))
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
                "last_verified_at": a.get("last_verified_at", ""),
                "verify_result": a.get("verify_result", ""),
                "cookie_expiry": _predict_expiry(
                    a.get("cookie", ""),
                    max(_ts_from_str(a.get("last_ok_at", "")),
                        _ts_from_str(a.get("last_verified_at", "")))),
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
        """返回扁平 Cookie 串（由带域条目导出；无条目时回退老字段）。"""
        for a in self.accounts:
            if a["id"] == acc_id:
                rows = _jar_load(a)
                if rows:
                    try:
                        from weibo_client import entries_to_cookie
                        s = entries_to_cookie(rows)
                        if s:
                            return s
                    except Exception:
                        pass
                return _decrypt(a.get("cookie", ""))
        return ""

    def get_cookie_entries(self, acc_id: str):
        """返回该账户的带域条目列表（无则空列表）。"""
        for a in self.accounts:
            if a["id"] == acc_id:
                return _jar_load(a)
        return []

    def update_cookie(self, acc_id: str, new_cookie: str, reason: str = "", entries=None):
        """回写续期后的 Cookie（v1.1.1 会话保活）。

        微博在网页请求响应里会通过 Set-Cookie 续期 SUB / SUBP / XSRF-TOKEN / ALF，
        把续期结果写回账户即等价于「滑动续期」，可显著延长 Cookie 寿命，
        减少用户必须手动重新登录的次数。仅在 Cookie 确有变化时落盘。

        entries（v1.6.5）可选：保活链路已带域时传进来，按 (name, domain) 精确落库；
        只给扁平串时，把值套回已有条目并**保留已学到的 domain**，不会因为一次旧路径
        回写就把域信息抹掉。
        """
        has_entries = bool(entries)
        if not has_entries and (not new_cookie or not new_cookie.strip()):
            return False
        try:
            from weibo_client import apply_flat_to_entries, entries_to_cookie, _norm_entries as _norm
        except Exception:
            return False
        for a in self.accounts:
            if a["id"] != acc_id:
                continue
            before_rows = _jar_load(a)
            old = entries_to_cookie(before_rows) or _decrypt(a.get("cookie", ""))
            if has_entries:
                rows = _norm(entries)
            else:
                rows = apply_flat_to_entries(before_rows, new_cookie.strip())
            after = entries_to_cookie(rows) or (new_cookie or "").strip()
            if old == after and a.get("cookie_jar"):
                return False
            _jar_store(a, rows)
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

    def set_verified(self, acc_id: str, ok: bool, source: str = "", detail: str = ""):
        """记录一次「登录态实测」结果（v1.6.7）。

        ok=True  → 写 last_verified_at + verify_result=valid，并顺带刷新 last_ok_at
                   （能确认有效，说明这次请求本身就是一次成功活动，7 天窗口照常滑动）。
        ok=False → 只标 verify_result=expired，**不动 last_verified_at**
                   （保留「最后确认有效」的历史事实，用户才知道是什么时候还好的）。

        注意：本方法**不写 status / check_detail** —— 账户状态与检测明细由
        set_check_result 统一管理，两处都写会互相覆盖，把诊断信息冲掉。

        【v1.6.9】新增 source / detail 两个只用于日志的参数，并**只在状态真的翻转时**
        打一条日志。这样「有效期列什么时候、被哪一路证据改成了什么」在运行日志里
        有完整轨迹 —— v1.6.7 的误判能持续好几天没被发现，正是因为没有任何留痕。

        返回 {"changed":bool, "prev":str, "now":str} 便于调用方复用判定结果。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for a in self.accounts:
            if a["id"] != acc_id:
                continue
            prev = a.get("verify_result") or "(空)"
            if ok:
                a["last_verified_at"] = now
                a["verify_result"] = "valid"
                a["last_ok_at"] = now
                a["verify_expired_at"] = ""      # v1.6.9：失效标记已撤销
            else:
                a["verify_result"] = "expired"
                a["verify_expired_at"] = now     # v1.6.9：留下「何时判失效」的时间证据
            cur = a["verify_result"]
            self.save()
            changed = prev != cur
            if changed:
                try:
                    from logger import info, warn, CAT_ACCT
                    who = a.get("remark") or a.get("wb_nickname") or acc_id
                    msg = (f"登录态实测标记变更：{who} · {prev} → {cur}"
                           f" · 来源={source or '未标注'}"
                           f" · 依据={detail or '无'}"
                           f" · 最后确认有效={a.get('last_verified_at') or '无记录'}")
                    (warn if cur == "expired" else info)(msg, CAT_ACCT)
                except Exception:
                    pass
            return {"changed": changed, "prev": prev, "now": cur}
        return {"changed": False, "prev": "", "now": ""}


_manager = None


def get_account_manager() -> AccountManager:
    global _manager
    if _manager is None:
        _manager = AccountManager()
    return _manager
