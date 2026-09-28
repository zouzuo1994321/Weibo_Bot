# -*- coding: utf-8 -*-
"""v1.1.4 回归测试：Cookie 失效时间预测 + 自动续期（会话保活）。

覆盖两项新功能：
  A. weibo_client.predict_cookie_expiry —— 解析 ALF/SSOLoginState/SUBP 中的
     Unix 时间戳，给出剩余有效期与等级（safe/warn/expired/unknown）。
  B. api_bridge 的自动/手动续期 —— 对「SUB 登录态、剩余<=7天、距上次续期>12h」
     的账户触发 warm_session 滑动续期并回写；其余情况跳过（不骚扰微博接口）。
"""
import os
import sys
import time
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "src")
sys.path.insert(0, SRC)

# 桩掉 GUI / 原生模块，仅测续期逻辑（与 test_cookie_bugfix.py 同思路）
for _m in ("webview", "llama_cpp", "jieba"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

import api_bridge as ab  # noqa: E402
import weibo_client as wc  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' · ' + extra) if extra else ''}")


NOW = int(time.time())
D = 86400


def _ck(**kw):
    """按关键字拼一个最小 Cookie：SUB 必带，其余时间戳按天偏移。"""
    parts = ["SUB=_2A25KabcSUB"]
    for k, days in kw.items():
        if days is not None:
            parts.append(f"{k}={NOW + int(days) * D}")
    return "; ".join(parts)


print("\n=== A1. predict_cookie_expiry 等级判定 ===")
r = wc.predict_cookie_expiry(_ck(ALF=30))
check("30 天 → safe", r["level"] == "safe", r["level"])
check("30 天 → has_expiry", r["has_expiry"] and r["source"] == "ALF")

r = wc.predict_cookie_expiry(_ck(ALF=3))
check("3 天 → warn", r["level"] == "warn", r["level"])

r = wc.predict_cookie_expiry(_ck(ALF=-1))
check("已过期 → expired", r["level"] == "expired", r["level"])
check("已过期 → expired 标志为真", r["expired"] is True)

r = wc.predict_cookie_expiry(_ck(SSOLoginState=50))
check("SSOLoginState 50 天 → safe（source 正确）",
      r["level"] == "safe" and r["source"] == "SSOLoginState", r["source"])

r = wc.predict_cookie_expiry(_ck(XSRF_TOKEN=10))
check("仅 XSRF-TOKEN 无有效期信息 → unknown",
      r["level"] == "unknown" and not r["has_expiry"], r["level"])

print("\n=== A2. SUBP 兜底 + 多键取最早 ===")
# SUBP 兜底：没有 ALF 但有 SUBP 内嵌时间戳
r = wc.predict_cookie_expiry("SUB=abc; SUBP=0033WrSX_" + str(NOW + 10 * D) + "_tail")
check("SUBP 兜底解析到有效期 → source=SUBP",
      r["has_expiry"] and r["source"] == "SUBP", r["source"])

# 多键并存时取最早（最保守）
r = wc.predict_cookie_expiry(_ck(ALF=3, SSOLoginState=20))
check("多键取最早 → 3 天那条决定（warn）",
      r["source"] == "ALF" and r["level"] == "warn", f"{r['source']}/{r['level']}")

print("\n=== A3. 异常时间戳被忽略 ===")
# ALF 不在合理区间（过小 / 过大）应回退到 unknown
r = wc.predict_cookie_expiry("SUB=abc; ALF=999999999")   # 2001 年，偏小
check("ALF 过小被忽略 → unknown", r["level"] == "unknown", r["level"])
r = wc.predict_cookie_expiry("SUB=abc; ALF=5000000000")  # 2128 年，偏大
check("ALF 过大被忽略 → unknown", r["level"] == "unknown", r["level"])

print("\n=== A4. _humanize_delta 中文时长 ===")
check("整 30 天", wc._humanize_delta(30 * D) == "剩 30 天", wc._humanize_delta(30 * D))
check("3 天 5 小时", wc._humanize_delta(3 * D + 5 * 3600) == "剩 3 天5 小时",
      wc._humanize_delta(3 * D + 5 * 3600))
check("仅小时", wc._humanize_delta(5 * 3600) == "剩 5 小时", wc._humanize_delta(5 * 3600))
check("仅分钟", wc._humanize_delta(12 * 60) == "剩 12 分钟", wc._humanize_delta(12 * 60))
check("<=0 → 已过期", wc._humanize_delta(-100) == "已过期")


print("\n=== B1. 自动续期筛选逻辑（mock 账户管理 + warm_session） ===")
from unittest import mock


class FakeAM:
    """可控的账户管理桩。accounts: list[dict]，cookie_map: id->cookie。"""
    def __init__(self, accounts, cookie_map):
        self._accounts = accounts
        self._cookie_map = cookie_map
        self.renewed = []        # 记录被 update_cookie 的 (id, new_cookie, reason)
        self.last_renew_log = {}  # 记录被 set_last_renew 的 id -> ts

    def list_accounts(self):
        return self._accounts

    def get_cookie(self, acc_id):
        return self._cookie_map.get(acc_id, "")

    def set_last_renew(self, acc_id, ts):
        self.last_renew_log[acc_id] = ts

    def update_cookie(self, acc_id, new_cookie, reason=""):
        self.renewed.append((acc_id, new_cookie, reason))


def make_accounts():
    return [
        {"id": "a1", "display_name": "充裕账户", "wb_uid": "1", "last_renew": ""},
        {"id": "a2", "display_name": "临近账户", "wb_uid": "2", "last_renew": ""},
        {"id": "a3", "display_name": "无SUB账户", "wb_uid": "3", "last_renew": ""},
        {"id": "a4", "display_name": "刚续期账户", "wb_uid": "4", "last_renew": ""},
        {"id": "a5", "display_name": "无信息账户", "wb_uid": "5", "last_renew": ""},
    ]


cookies = {
    "a1": _ck(ALF=30),              # 充裕，跳过
    "a2": _ck(ALF=3),               # 临近，应续期
    "a3": "XSRF-TOKEN=abc",         # 无 SUB，跳过
    "a4": _ck(ALF=2),               # 临近，但因 12h 内刚续期而跳过
    "a5": "SUB=abc; XSRF-TOKEN=abc",  # 有 SUB 但无有效期，has_expiry=False 跳过
}
# a4 标记为 1 小时前已续期（节流）
accounts = make_accounts()
accounts[3]["last_renew"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - 3600))

NEW_COOKIE = _ck(ALF=40)  # 续期后服务端下发的「新」Cookie


def fake_warm(cookie, uid=""):
    # 对 a4 返回未变化（模拟续期接口无新下发）；其余返回已变化
    if "ALF=178" not in cookie and "ALF=17" not in cookie:
        pass
    # 简单判定：凡 cookie 里 ALF 偏移 < 7 天（临近账户 a2 / a4）且非 a4，返回新 cookie
    return {"ok": True, "cookie": NEW_COOKIE}


api = ab.Api()
with mock.patch.object(ab, "get_account_manager", return_value=FakeAM(accounts, cookies)), \
     mock.patch.object(wc, "warm_session", side_effect=lambda c, u="": (
         {"ok": True, "cookie": NEW_COOKIE} if c != cookies["a4"]
         else {"ok": True, "cookie": c})):
    renewed = api.auto_renew_accounts()

check("仅临近且未节流的 a2 被续期", "临近账户" in renewed, str(renewed))
check("充裕账户 a1 被跳过", "充裕账户" not in renewed)
check("无 SUB 账户 a3 被跳过", "无SUB账户" not in renewed)
check("12h 内刚续期 a4 被节流跳过", "刚续期账户" not in renewed)
check("无有效期信息 a5 被跳过", "无信息账户" not in renewed)
check("本次续期账户数 == 1", len(renewed) == 1, str(len(renewed)))

print("\n=== B2. 手动续期 renew_account ===")
am2 = FakeAM(accounts, cookies)
with mock.patch.object(ab, "get_account_manager", return_value=am2), \
     mock.patch.object(wc, "warm_session", return_value={"ok": True, "cookie": NEW_COOKIE}):
    res = api.renew_account("a2")
    check("手动续期成功账户 → ok", res.get("ok") is True, str(res))
    check("续期后回写新 Cookie", any(r[0] == "a2" for r in am2.renewed))

    # 账户不存在
    res = api.renew_account("nope")
    check("账户不存在 → ok=False", res.get("ok") is False)

    # 续期接口无变化（返回未变化 cookie）
    am3 = FakeAM(accounts, {"a2": _ck(ALF=3)})
    with mock.patch.object(wc, "warm_session", return_value={"ok": True, "cookie": _ck(ALF=3)}):
        res = api.renew_account("a2")
        check("无变化 → ok=False 且给出说明", res.get("ok") is False and "未发生变化" in (res.get("message") or ""))

print("\n结果：" + ("全部通过 ✔" if ok else "存在失败 ✘"))
sys.exit(0 if ok else 1)
