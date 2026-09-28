# -*- coding: utf-8 -*-
"""v1.1.5 Cookie 失效预测修正补丁（验证式替换，不依赖 Edit 工具）。

核心修复：旧实现只解析 ALF（长期自动登录令牌，~30 天），给出「剩 22 天」的乐观预测，
而真实 SUB 会话约 7 天无活动即失效、自动续期阈值(<=7天)永远触发不到。
修正后以「会话起点(登录时间/最近成功活动) + 7 天」为失效点，必要时退回 ALF。
"""
import io
import os

BASE = r"Z:\【01】自研软件\【26-11】微博bot小助手（正式版）\src"

FILES = {
    "weibo_client.py": os.path.join(BASE, "weibo_client.py"),
    "account_manager.py": os.path.join(BASE, "account_manager.py"),
    "api_bridge.py": os.path.join(BASE, "api_bridge.py"),
}


def read_text(path):
    with io.open(path, "rb") as f:
        return f.read().decode("utf-8")


def write_text(path, text):
    with io.open(path, "wb") as f:
        f.write(text.encode("utf-8"))


# ---------------------- 补丁定义 ----------------------
# 每个补丁：(文件名, 旧字符串, 新字符串)
PATCHES = []


# ---- weibo_client.py：整体替换 predict_cookie_expiry + 新增两个辅助函数 ----
WC_OLD = """def predict_cookie_expiry(cookie: str) -> dict:
    \"\"\"预测 Cookie（登录态）失效时间。

    返回 dict：
      has_expiry  是否解析到有效期时间戳
      expired     是否已过期
      source      取值来源键名（ALF / SSOLoginState / SUBP）
      expiry_ts   失效 Unix 时间戳（秒）
      expiry_str  失效时间（本地，YYYY-MM-DD HH:MM）
      remaining_seconds  剩余秒数
      remaining_text     剩余时长中文描述
      level       safe（充足）/ warn（临近，<=7 天）/ expired（已过期）/ unknown（无数据）
    \"\"\"
    d = _cookie_dict(cookie)
    cands = []
    for key in ("ALF", "SSOLoginState", "ALF_SSOINFO"):
        v = d.get(key)
        if not v:
            continue
        m = re.search(r"\\d{10,11}", v)
        if not m:
            continue
        try:
            ts = int(m.group(0))
        except ValueError:
            continue
        # 合理性区间：2017-05 ~ 2096-09（Unix 秒）
        if 1_500_000_000 < ts < 4_000_000_000:
            cands.append((key, ts))
    if not cands:
        # 兜底：从 SUBP 大字符串里抠一个 10 位时间戳（多为 ALF 值）
        subp = d.get("SUBP", "")
        for m in re.finditer(r"(\\d{10})", subp):
            try:
                ts = int(m.group(1))
            except ValueError:
                continue
            if 1_500_000_000 < ts < 4_000_000_000:
                # SUBP 里常含多个时间戳（如生日、注册时间等），取最大者更可能是 ALF
                cands.append(("SUBP", ts))
                break
    if not cands:
        return {"has_expiry": False, "expired": False, "level": "unknown"}
    # 取「最早」的过期时间作为保守预测（多个键理论上一致，取最小更稳妥）
    best_key, best_ts = min(cands, key=lambda x: x[1])
    now = int(time.time())
    remaining = best_ts - now
    expired = remaining <= 0
    if expired:
        level = "expired"
    elif remaining <= 7 * 86400:
        level = "warn"
    else:
        level = "safe"
    try:
        expiry_str = datetime.fromtimestamp(best_ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        expiry_str = ""
    return {
        "has_expiry": True,
        "expired": expired,
        "source": best_key,
        "expiry_ts": best_ts,
        "expiry_str": expiry_str,
        "remaining_seconds": remaining,
        "remaining_text": _humanize_delta(remaining),
        "level": level,
    }"""

WC_NEW = """def _extract_subp_login_ts(cookie: str) -> int:
    \"\"\"从 SUBP 提取最早的时间戳，作为本次登录/会话起点。

    微博 SUBP 内嵌多个 10 位 Unix 时间戳（注册、登录等），取最小者通常最接近
    「本次登录时刻」，用于推算 7 天 SUB 会话窗口起点。
    \"\"\"
    d = _cookie_dict(cookie)
    subp = d.get("SUBP", "")
    now = int(time.time())
    cands = []
    for m in re.finditer(r"(\\d{10})", subp):
        try:
            ts = int(m.group(1))
        except ValueError:
            continue
        # 合理性区间，且不能晚于现在
        if 1_500_000_000 < ts < 4_000_000_000 and ts <= now + 60:
            cands.append(ts)
    return min(cands) if cands else 0


def _build_expiry_result(expiry_ts: int, source: str) -> dict:
    \"\"\"根据失效时间戳构造统一返回结构。\"\"\"
    now = int(time.time())
    remaining = expiry_ts - now
    expired = remaining <= 0
    if expired:
        level = "expired"
    elif remaining <= 7 * 86400:
        level = "warn"
    else:
        level = "safe"
    try:
        expiry_str = datetime.fromtimestamp(expiry_ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        expiry_str = ""
    return {
        "has_expiry": True,
        "expired": expired,
        "source": source,
        "expiry_ts": expiry_ts,
        "expiry_str": expiry_str,
        "remaining_seconds": remaining,
        "remaining_text": _humanize_delta(remaining),
        "level": level,
    }


def predict_cookie_expiry(cookie: str, last_ok_at_ts: int = 0) -> dict:
    \"\"\"预测 Cookie（登录态）失效时间。

    微博登录态存在两个相互独立的有效期：
      1) ALF / SSOLoginState / ALF_SSOINFO —— 长期「自动登录」令牌，最长可达约 30 天；
      2) SUB 会话 —— 真正的网页端登录态，约 7 天无活动即失效（每次成功请求滑动续期）。

    旧实现只解析 ALF，给出「剩 22 天」的乐观预测，而真实 SUB 会话第 7 天已失效、
    自动续期阈值（<=7 天）永远触发不到。

    修正：优先以「会话起点(登录时间/最近一次成功活动) + 7 天」作为失效点；
    仅当 ALF 过期更早时才退回 ALF 时间。这样自动续期能在第 7 天前及时触发。

    参数：
      cookie           Cookie 字符串
      last_ok_at_ts    该账户最近一次「成功活动」的 Unix 时间戳（秒），来自账户字段
                       last_ok_at（成功检测 / 成功续期 / 被使用时回写），用于把 7 天窗口
                       随活跃使用不断后延（滑动续期语义）。
    \"\"\"
    d = _cookie_dict(cookie)
    # 1) 解析 ALF 系长期令牌
    alf_ts = 0
    alf_key = ""
    for key in ("ALF", "SSOLoginState", "ALF_SSOINFO"):
        v = d.get(key)
        if not v:
            continue
        m = re.search(r"\\d{10,11}", v)
        if not m:
            continue
        try:
            ts = int(m.group(0))
        except ValueError:
            continue
        if 1_500_000_000 < ts < 4_000_000_000:
            alf_ts, alf_key = ts, key
            break
    # 2) 会话起点：SUBP 最早时间戳（登录时刻） 与 last_ok_at 取较晚者
    login_ts = _extract_subp_login_ts(cookie)
    session_start = 0
    if login_ts and last_ok_at_ts:
        session_start = max(login_ts, last_ok_at_ts)
    elif login_ts:
        session_start = login_ts
    elif last_ok_at_ts:
        session_start = last_ok_at_ts
    # 3) 优先 SUB 7 天会话窗口
    if session_start:
        session_ts = session_start + 7 * 86400
        if alf_ts == 0 or session_ts < alf_ts:
            return _build_expiry_result(session_ts, "SUB_SESSION")
    # 4) 兜底：ALF 长期令牌
    if alf_ts:
        return _build_expiry_result(alf_ts, alf_key)
    return {"has_expiry": False, "expired": False, "level": "unknown"}"""

PATCHES.append(("weibo_client.py", WC_OLD, WC_NEW))


# ---- account_manager.py ----

# (c)+(a): 新增 _ts_from_str 辅助 + 修改 _predict_expiry 签名
AM_PRED_SIG_OLD = """def _predict_expiry(encrypted_cookie: str) -> dict:"""
AM_PRED_SIG_NEW = """def _ts_from_str(s: str) -> int:
    \"\"\"把 'YYYY-MM-DD HH:MM:SS' 解析为 Unix 秒；失败返回 0。\"\"\"
    if not s:
        return 0
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return 0


def _predict_expiry(encrypted_cookie: str, last_ok_at_ts: int = 0) -> dict:"""
PATCHES.append(("account_manager.py", AM_PRED_SIG_OLD, AM_PRED_SIG_NEW))

# (b): _predict_expiry 内部返回
AM_PRED_RET_OLD = """        return predict_cookie_expiry(cookie)"""
AM_PRED_RET_NEW = """        return predict_cookie_expiry(cookie, last_ok_at_ts=last_ok_at_ts)"""
PATCHES.append(("account_manager.py", AM_PRED_RET_OLD, AM_PRED_RET_NEW))

# (e): load() 迁移补齐 last_ok_at
AM_LOAD_OLD = """    def load(self):
        if os.path.exists(ACCOUNTS_PATH):
            try:
                with open(ACCOUNTS_PATH, "r", encoding="utf-8") as f:
                    self.accounts = json.load(f)
            except Exception:
                self.accounts = []
        return self.accounts"""
AM_LOAD_NEW = """    def load(self):
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
        return self.accounts"""
PATCHES.append(("account_manager.py", AM_LOAD_OLD, AM_LOAD_NEW))

# (d): add_account 增加 last_ok_at 字段
AM_ADD_OLD = """            "last_renew": "",          # 最近一次 Cookie 续期时间（滑动续期/自动续期回写）
        }"""
AM_ADD_NEW = """            "last_renew": "",          # 最近一次 Cookie 续期时间（滑动续期/自动续期回写）
            "last_ok_at": "",          # 最近一次「成功活动」时间（成功检测/续期/使用），用于 7 天窗口滑动
        }"""
PATCHES.append(("account_manager.py", AM_ADD_OLD, AM_ADD_NEW))

# (f1): list_accounts 输出 last_ok_at
AM_LIST_LR_OLD = """                "last_renew": a.get("last_renew", ""),"""
AM_LIST_LR_NEW = """                "last_renew": a.get("last_renew", ""),
                "last_ok_at": a.get("last_ok_at", ""),"""
PATCHES.append(("account_manager.py", AM_LIST_LR_OLD, AM_LIST_LR_NEW))

# (f2): list_accounts 传 last_ok_at 给预测
AM_LIST_EXP_OLD = """                "cookie_expiry": _predict_expiry(a.get("cookie", "")),"""
AM_LIST_EXP_NEW = """                "cookie_expiry": _predict_expiry(a.get("cookie", ""), _ts_from_str(a.get("last_ok_at", ""))),"""
PATCHES.append(("account_manager.py", AM_LIST_EXP_OLD, AM_LIST_EXP_NEW))

# (g): set_check_result 成功检测回写 last_ok_at
AM_CHK_OLD = """                a["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if wb_nickname:"""
AM_CHK_NEW = """                a["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if status == "ok":
                    a["last_ok_at"] = a["last_check"]
                if wb_nickname:"""
PATCHES.append(("account_manager.py", AM_CHK_OLD, AM_CHK_NEW))

# (h): update_cookie 回写 last_ok_at
AM_UPD_OLD = """            a["cookie"] = _encrypt(new_cookie.strip())
            if reason:"""
AM_UPD_NEW = """            a["cookie"] = _encrypt(new_cookie.strip())
            a["last_ok_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if reason:"""
PATCHES.append(("account_manager.py", AM_UPD_OLD, AM_UPD_NEW))

# (i): mark_used 回写 last_ok_at
AM_USE_OLD = """            if a["id"] == acc_id:
                a["last_used"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.save()"""
AM_USE_NEW = """            if a["id"] == acc_id:
                a["last_used"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                a["last_ok_at"] = a["last_used"]
                self.save()"""
PATCHES.append(("account_manager.py", AM_USE_OLD, AM_USE_NEW))


# ---- api_bridge.py ----
AB_RENEW_OLD = """                from weibo_client import predict_cookie_expiry
                exp = predict_cookie_expiry(cookie)"""
AB_RENEW_NEW = """                from weibo_client import predict_cookie_expiry
                from account_manager import _ts_from_str
                exp = predict_cookie_expiry(cookie, last_ok_at_ts=_ts_from_str(acc.get("last_ok_at", "")))"""
PATCHES.append(("api_bridge.py", AB_RENEW_OLD, AB_RENEW_NEW))


# ---------------------- 执行 ----------------------
def main():
    # 预读
    texts = {name: read_text(path) for name, path in FILES.items()}
    results = []
    for name, old, new in PATCHES:
        t = texts[name]
        cnt = t.count(old)
        if cnt == 0:
            results.append((name, "MISS", "old block not found"))
            continue
        if cnt > 1:
            results.append((name, "AMBIG", "old block appears %d times" % cnt))
            continue
        texts[name] = t.replace(old, new, 1)
        results.append((name, "OK", "patched"))

    # 检查是否有失败
    failed = [r for r in results if r[1] != "OK"]
    if failed:
        for r in results:
            print(r)
        print("ABORT: 存在未匹配的补丁，未写入任何文件。")
        return 1

    # 全部匹配，先备份再写回
    for name, path in FILES.items():
        bak = path + ".v114bak"
        with io.open(bak, "wb") as f:
            f.write(read_text(path).encode("utf-8"))
        write_text(path, texts[name])
        print(("BACKUP+WRITE %s -> %s" % (name, bak)))

    for r in results:
        print(r)
    print("ALL_PATCHED_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
