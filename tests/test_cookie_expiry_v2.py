# -*- coding: utf-8 -*-
"""v1.1.5 回归测试：Cookie 失效预测修正（SUB 7 天会话窗口优先）。

核心修复：旧实现只解析 ALF（长期自动登录令牌，~30 天），给出「剩 22 天」的乐观预测，
而真实 SUB 会话约 7 天无活动即失效、自动续期阈值(<=7天)永远触发不到。
修正后优先以「会话起点(登录时间/最近成功活动) + 7 天」为失效点。

覆盖：
  A. _extract_subp_login_ts —— 取 SUBP 中最早（<=now）的时间戳作为登录起点。
  B. predict_cookie_expiry —— ALF 远未来 + SUBP 登录点 7 天前 -> SUB_SESSION 且 <=7 天
     （旧的 ALF-only 会误判为 safe/22 天）；last_ok_at 滑动窗口；无数据 -> unknown；
     仅有 ALF（无 SUBP 有效时间戳）-> 退回 ALF（safe）。
  C. account_manager._ts_from_str / _predict_expiry(last_ok_at_ts=...) —— 透传 last_ok_at。
"""
import os
import sys
import time
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "src")
sys.path.insert(0, SRC)

for _m in ("webview", "llama_cpp", "jieba"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

import weibo_client as wc  # noqa: E402
import account_manager as am  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' · ' + extra) if extra else ''}")


NOW = int(time.time())
D = 86400


def _cookie(alf_days, login_days_ago, other_days_ago=None):
    """拼一个最小 Cookie：SUB 必带；ALF/SSOLoginState 表示长期令牌；SUBP 内嵌时间戳。"""
    alf_ts = NOW + alf_days * D
    subp = ""
    if login_days_ago is not None:
        subp += f"x{NOW - login_days_ago * D}y"
    if other_days_ago is not None:
        subp += f"z{NOW - other_days_ago * D}w"
    c = "SUB=abc;"
    if subp:
        c += f"SUBP={subp};"
    c += f"ALF={alf_ts};SSOLoginState={alf_ts}"
    return c


print("== A. _extract_subp_login_ts ==")
# SUBP 含多个时间戳，取最早（<=now）者
c1 = _cookie(alf_days=22, login_days_ago=2, other_days_ago=1)
ts = wc._extract_subp_login_ts(c1)
check("取 SUBP 最早时间戳(2天前)", ts == NOW - 2 * D, str(ts))

c_no_subp = _cookie(alf_days=22, login_days_ago=None)
check("无 SUBP -> 0", wc._extract_subp_login_ts(c_no_subp) == 0)

print("== B. predict_cookie_expiry（SUB 会话窗口优先）==")
# 用户真实场景：ALF 还有 22 天，但 SUB 会话已 7 天无活动 -> 应判为 SUB_SESSION 且 <=7 天
c_bug = _cookie(alf_days=22, login_days_ago=7)
r = wc.predict_cookie_expiry(c_bug)
check("不再误判为 ALF/22天(safe)", r.get("source") != "ALF", r.get("source"))
check("来源为 SUB_SESSION", r.get("source") == "SUB_SESSION", r.get("source"))
check("剩余 <=7 天（触发续期）", 0 <= r.get("remaining_seconds", 1e12) <= 7 * D, str(r.get("remaining_seconds")))
check("等级为 expired/warn", r.get("level") in ("warn", "expired"), r.get("level"))

# 登录点 2 天前 -> 窗口剩 ~5 天 -> warn
c_2 = _cookie(alf_days=22, login_days_ago=2)
r2 = wc.predict_cookie_expiry(c_2)
check("登录2天前 -> SUB_SESSION warn", r2.get("source") == "SUB_SESSION" and r2.get("level") == "warn", r2.get("level"))
check("剩余约 5 天", abs(r2.get("remaining_seconds", 0) - 5 * D) <= D, str(r2.get("remaining_seconds")))

print("== C. last_ok_at 滑动窗口 ==")
# 登录点很久(10天前)但最近一次成功活动在 1 天前 -> 窗口随之后延到 ~6 天
c_old = _cookie(alf_days=22, login_days_ago=10)
r_without = wc.predict_cookie_expiry(c_old)            # 无 last_ok_at
r_with = wc.predict_cookie_expiry(c_old, last_ok_at_ts=NOW - 1 * D)
check("无 last_ok_at -> 已过期", r_without.get("level") == "expired", r_without.get("level"))
check("有 last_ok_at(1天前) -> 未过期(warn)", r_with.get("level") == "warn", r_with.get("level"))
check("last_ok_at 后延了窗口", r_with.get("expiry_ts", 0) > r_without.get("expiry_ts", 0))

print("== D. 边界：无数据 / 仅 ALF ==")
c_empty = "SUB=abc"
r_e = wc.predict_cookie_expiry(c_empty)
check("无时间戳 -> unknown", r_e.get("level") == "unknown", r_e.get("level"))

# 仅有 ALF（SUBP 无有效时间戳）-> 退回 ALF 长期令牌（safe, ~22天）
c_alf_only = _cookie(alf_days=22, login_days_ago=None)
r_alf = wc.predict_cookie_expiry(c_alf_only)
check("仅 ALF -> 来源 ALF 系", r_alf.get("source") in ("ALF", "SSOLoginState"), r_alf.get("source"))
check("仅 ALF -> safe(约22天)", r_alf.get("level") == "safe" and r_alf.get("remaining_seconds", 0) > 7 * D, r_alf.get("level"))

print("== E. account_manager 透传 last_ok_at ==")
check("_ts_from_str('') -> 0", am._ts_from_str("") == 0)
ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - 1 * D))
check("_ts_from_str(合法) 解析正确", abs(am._ts_from_str(ts_str) - (NOW - 1 * D)) <= 2)

# 加密 cookie + last_ok_at 透传：应与裸 predict 一致
enc = am._encrypt(_cookie(alf_days=22, login_days_ago=2))
r_am = am._predict_expiry(enc, last_ok_at_ts=0)
check("_predict_expiry 透传 -> SUB_SESSION", r_am.get("source") == "SUB_SESSION", r_am.get("source"))

print()
if ok:
    print("ALL_V2_TESTS_PASS")
else:
    print("SOME_V2_TESTS_FAIL")
