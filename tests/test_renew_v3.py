# -*- coding: utf-8 -*-
"""v1.1.6 回归测试：
  A. Cookie 自动续期链路闭环 —— set_last_renew 必须同时滑动 last_ok_at
     （续期成功即证明会话活着；否则风控期间探测失败会导致 7 天窗口停滞）。
  B. logger 异步化 —— log() 立即返回不被 NAS 写盘阻塞，wait_flush 后落盘。
  C. predict_cookie_expiry v1.1.5 修正回归（SUB 7 天会话窗口优先）。

运行：python tests/test_renew_v3.py  （以 _src_new 为最高优先级搜索路径）
全部通过打印 ALL_V3_TESTS_PASS。
"""
import os
import sys
import time
import types
import tempfile
import json

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_NEW = os.path.join(BASE, "_src_new")
SRC = os.path.join(BASE, "src")
# 注意 insert(0) 的逆序特性：最终优先级为 _src_new > src > BASE
for p in (BASE, SRC, SRC_NEW):
    if p not in sys.path:
        sys.path.insert(0, p)

for _m in ("webview", "llama_cpp", "jieba"):
    sys.modules.setdefault(_m, types.ModuleType(_m))

# 关键：把账户文件与日志文件都指到临时目录，绝不触碰 NAS 上的真实数据
import paths  # noqa: E402
_tmpdir = tempfile.mkdtemp(prefix="weibobot_test_")
paths.ACCOUNTS_PATH = os.path.join(_tmpdir, "accounts.json")
paths.DATA_DIR = _tmpdir
paths.LOG_PATH = os.path.join(_tmpdir, "test.log")

import account_manager as am  # noqa: E402
import logger as lg  # noqa: E402
import weibo_client as wc  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' · ' + extra) if extra else ''}")


NOW = int(time.time())
D = 86400

print("== A. 续期链路：set_last_renew 滑动 last_ok_at ==")
mgr = am.AccountManager()
_ok, acc_id = mgr.add_account("测试账户", "SUB=abc;SUBP=x%dy;ALF=%d" % (NOW - 6 * D, NOW + 22 * D))
check("添加账户成功", bool(_ok) and bool(acc_id), str(acc_id))
ts = time.strftime("%Y-%m-%d %H:%M:%S")
r = mgr.set_last_renew(acc_id, ts)
acc = next(a for a in mgr.list_accounts() if a["id"] == acc_id)
check("set_last_renew 返回 True", r is True)
check("last_renew 已记录", acc.get("last_renew") == ts, acc.get("last_renew"))
check("last_ok_at 同步滑动", acc.get("last_ok_at") == ts, acc.get("last_ok_at"))

# 落盘验证：新实例读回
mgr2 = am.AccountManager()
acc2 = next(a for a in mgr2.list_accounts() if a["id"] == acc_id)
check("落盘后 last_ok_at 保持", acc2.get("last_ok_at") == ts, acc2.get("last_ok_at"))

# 预测联动：last_ok_at 刚滑动 -> SUB_SESSION 窗口后延到 ~7 天，等级 warn 而非 expired
check("预测来源 SUB_SESSION", acc2.get("cookie_expiry", {}).get("source") == "SUB_SESSION",
      str(acc2.get("cookie_expiry", {}).get("source")))
check("滑动后未过期(warn)", acc2.get("cookie_expiry", {}).get("level") == "warn",
      str(acc2.get("cookie_expiry", {}).get("level")))

print("== B. logger 异步化 ==")
t0 = time.time()
for i in range(50):
    lg.log("INFO", "测试日志 %d" % i, "CORE")
dt = time.time() - t0
check("50 条日志入队不阻塞(<0.2s)", dt < 0.2, "%.3fs" % dt)
flushed = lg.wait_flush(10)
check("队列排空", flushed)
with open(lg.LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
    lines = [l for l in f if "测试日志" in l]
check("日志已落盘(50 条)", len(lines) == 50, str(len(lines)))

print("== C. predict_cookie_expiry 回归（SUB 7 天窗口优先）==")
c_bug = "SUB=abc;SUBP=x%dy;ALF=%d" % (NOW - 7 * D, NOW + 22 * D)
r = wc.predict_cookie_expiry(c_bug)
check("旧 bug 场景 -> SUB_SESSION", r.get("source") == "SUB_SESSION", r.get("source"))
# 7 天整点登录允许有秒级过冲（remaining 可为极小负值），关键是 <=7 天即命中续期阈值
check("剩余 <=7 天（命中续期阈值）", r.get("remaining_seconds", 1e12) <= 7 * D,
      str(r.get("remaining_seconds")))

print()
if ok:
    print("ALL_V3_TESTS_PASS")
else:
    print("SOME_V3_TESTS_FAIL")
    sys.exit(1)
