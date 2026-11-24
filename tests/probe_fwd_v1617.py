# -*- coding: utf-8 -*-
"""行为级验证 v1.6.17 的三处改动（不联网、不写用户库）。

验的是「行为」而不是「代码里有没有这行字」：
  1. `_is_no_permission()` 的真值表 —— 内容不可转发 vs 真故障必须分得开；
  2. `_schedule_forward()` 到期执行后**必须**留下日志，且按结果选级别
     （forwarded/skipped → info，failed → warn）——旧版成功路径一个字不写；
  3. `fetch_uid()` 遇到「判空」不刷 WARN，并登记到本轮判空集合。

用法：python tests/probe_fwd_v1617.py
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "_src_v151k"))

import forward_engine as fe  # noqa: E402

FAILS = []


def expect(cond, label, extra=""):
    print("  %s %s%s" % ("PASS" if cond else "FAIL", label,
                         ("  · " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


# ---------------------------------------------------------------- 1. 真值表
print("场景一：内容不可转发的判定（_is_no_permission）")
POS = [
    '未知响应(ok=0, code=None) ｜ 原始回包=\'{"ok":0,"message":"由于作者设置了可见性，你没有权限转发此微博"}\'',
    "由于作者设置了可见性，你没有权限转发此微博",
    "该微博已被删除",
    "无权转发",
]
NEG = [
    "HTTP 432 风控",
    "Cookie 已失效",
    "网络异常或接口超时",
    "",
]
for s in POS:
    expect(fe._is_no_permission(s) is True, "应判为不可转发：%s" % s[:28])
for s in NEG:
    expect(fe._is_no_permission(s) is False, "不应判为不可转发：%r" % s[:20])

# ---------------------------------------------------------------- 2. 到期留痕
print("场景二：延迟转发到期必须留痕（_schedule_forward）")
_captured = []
_orig = (fe.info, fe.warn, fe.error)
try:
    fe.info = lambda m, *a, **k: _captured.append(("INFO", m))
    fe.warn = lambda m, *a, **k: _captured.append(("WARN", m))
    fe.error = lambda m, *a, **k: _captured.append(("ERROR", m))

    fe._schedule_forward(0.05, lambda: {"action": "forwarded", "detail": "已转发（normal）：纯转发"},
                         meta={"uid": "1001", "screen_name": "甲", "post_id": "p1"})
    fe._schedule_forward(0.05, lambda: {"action": "skipped", "detail": "内容不可转发，已跳过"},
                         meta={"uid": "1002", "screen_name": "乙", "post_id": "p2"})
    fe._schedule_forward(0.05, lambda: {"action": "failed", "detail": "转发失败：HTTP 432"},
                         meta={"uid": "1003", "screen_name": "丙", "post_id": "p3"})

    def _boom():
        raise RuntimeError("模拟异常")
    fe._schedule_forward(0.05, _boom, meta={"uid": "1004", "screen_name": "丁"})

    # 必须**在 try 里**等后台线程跑完再还原：还原得早，线程醒来时拿到的就是原函数，
    # 日志会写进真实日志文件，捕获列表永远是空的（第一版就踩了这个坑）。
    time.sleep(0.8)
finally:
    fe.info, fe.warn, fe.error = _orig

lines = [m for _, m in _captured]
print("   捕获 %d 条日志：" % len(lines))
for _, m in _captured:
    print("     ·", m[:96])
expect(len(_captured) == 4, "四条排程各留一条痕迹", "实际 %d 条" % len(_captured))
expect(sum(1 for lv, _ in _captured if lv == "INFO") == 2,
       "forwarded / skipped 记 INFO")
expect(sum(1 for lv, _ in _captured if lv == "WARN") == 1, "failed 记 WARN")
expect(sum(1 for lv, _ in _captured if lv == "ERROR") == 1, "抛异常记 ERROR")
expect(all("排程到期执行" in m for m in lines[:3]), "三条正常路径都带「排程到期执行」")
expect(any("耗时" in m for m in lines[:3]), "带耗时")
expect(any("UID=1001" in m for m in lines), "带对象标识")

# ---------------------------------------------------------------- 3. 判空降噪
print("场景三：判空不刷 WARN，且登记到本轮判空集合（fetch_uid）")
_w = []
_orig_warn, _orig_get = fe.warn, fe.get_latest_posts
_orig_upd = fe.get_monitor_manager
try:
    fe.warn = lambda m, *a, **k: _w.append(m)
    fe.get_latest_posts = lambda *a, **k: {
        "ok": False, "status": "empty",
        "reason": "未获取到微博数据（该用户可能暂无公开微博）",
        "error": "mymblog ok=1 list=[]"}
    fe._round_empty_uids.clear()
    r = fe.fetch_uid("999888", "SUB=x", {"screen_name": "空对象"})
finally:
    fe.warn, fe.get_latest_posts = _orig_warn, _orig_get
    fe.get_monitor_manager = _orig_upd

expect(len(r) == 5 and r[4] is False, "元组长度与 ok 标志不变（向后兼容）",
       "len=%d ok=%s" % (len(r), r[4] if len(r) > 4 else "?"))
expect(len(_w) == 0, "判空没有产生任何 WARN", "实际 %d 条" % len(_w))
expect("999888" in fe._round_empty_uids, "已登记到本轮判空集合")

print()
if FAILS:
    print("FAILED (%d)：%s" % (len(FAILS), "; ".join(FAILS)))
    sys.exit(1)
print("ALL_V1617_CHECKS_PASS")
