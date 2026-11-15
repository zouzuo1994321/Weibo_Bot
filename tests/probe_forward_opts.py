# -*- coding: utf-8 -*-
"""v1.6.12 新开关行为契约探针：排程波动 / 仅转发含图博文。

不依赖网络，直接对 forward_engine 的两个纯函数做判定。
"""
import os
import sys
import random
from collections import Counter

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(BASE), "_src_v151k")
sys.path.insert(0, SRC)

import forward_engine as fe          # noqa: E402

ROWS = []


def chk(name, fn):
    try:
        ok, info = fn()
    except Exception as e:
        ok, info = False, "异常：%s: %s" % (type(e).__name__, e)
    ROWS.append((name, ok, info))
    print("%-34s %s  %s" % (name, "PASS" if ok else "FAIL", info))


def _mk(n):
    return {"forward_count": n}


def t_jitter_off_fixed():
    """关闭波动：无论跑多少次，返回值恒等于设定值。"""
    cfg = dict(_mk(5), forward_count_jitter=False)
    vals = {fe.get_forward_count_for_round(cfg) for _ in range(200)}
    return (vals == {5}, "取值集合=%s（应为 {5}）" % sorted(vals))


def t_jitter_on_range():
    """开启波动：取值必须全部落在 1~N，且两端都能出现。"""
    cfg = dict(_mk(5), forward_count_jitter=True)
    vals = [fe.get_forward_count_for_round(cfg) for _ in range(600)]
    lo, hi = min(vals), max(vals)
    good = lo == 1 and hi == 5 and all(1 <= v <= 5 for v in vals)
    cnt = Counter(vals)
    return (good, "范围=%d~%d 分布=%s（应 1~5 且两端出现）"
            % (lo, hi, dict(sorted(cnt.items()))))


def t_jitter_never_zero():
    """N=1 时开波动不得随机出 0 条。"""
    cfg = dict(_mk(1), forward_count_jitter=True)
    vals = {fe.get_forward_count_for_round(cfg) for _ in range(300)}
    return (vals == {1}, "取值集合=%s（应为 {1}）" % sorted(vals))


def t_jitter_clamped():
    """非法/越界设定值必须先被钳到 1~10 再随机。"""
    ok = True
    msgs = []
    for raw, want_hi in ((0, 1), (-3, 1), (99, 10), ("abc", 1), (None, 1)):
        cfg = {"forward_count": raw, "forward_count_jitter": True}
        vals = [fe.get_forward_count_for_round(cfg) for _ in range(300)]
        lo, hi = min(vals), max(vals)
        if not (lo >= 1 and hi == want_hi):
            ok = False
        msgs.append("%r→%d~%d" % (raw, lo, hi))
    return (ok, " ".join(msgs))


def t_images_filter_basic():
    """仅含图：纯文字 / 纯视频被滤掉，带图保留，顺序不变。"""
    cands = [
        {"uid": "a", "post": {"id": "1", "images": ["u1"]}},
        {"uid": "b", "post": {"id": "2", "images": []}},
        {"uid": "c", "post": {"id": "3"}},                       # 无 images 键
        {"uid": "d", "post": {"id": "4", "images": ["u2", "u3"]}},
        {"uid": "e", "post": {"id": "5", "images": None}},
    ]
    kept = fe._filter_images_only(cands)
    ids = [c["post"]["id"] for c in kept]
    return (ids == ["1", "4"], "保留 id=%s（应为 ['1','4']）" % ids)


def t_images_filter_robust():
    """脏数据不得抛异常（post 为 None / 非 dict / 列表本身为空）。"""
    try:
        a = fe._filter_images_only([])
        b = fe._filter_images_only([{"uid": "x", "post": None}, {"uid": "y"}])
        return (a == [] and b == [], "空池=%r 脏数据=%r" % (a, b))
    except Exception as e:
        return (False, "抛异常：%s" % e)


def t_images_only_then_pick():
    """联合行为：先过滤再挑 N 条，挑出来的必须条条带图。"""
    cands = []
    for i in range(20):
        cands.append({"uid": "u%d" % (i % 7),
                      "post": {"id": str(i), "images": (["x"] if i % 3 == 0 else [])}})
    kept = fe._filter_images_only(cands)
    picked = fe._pick_candidates(kept, 5)
    all_img = all(p["post"]["images"] for p in picked)
    return (len(kept) == 7 and all_img,
            "池 %d→%d 条，挑出 %d 条，全带图=%r" % (len(cands), len(kept), len(picked), all_img))


def t_config_defaults():
    """配置层：两个开关默认必须为 False（老用户升级后行为不变）。"""
    from config_manager import DEFAULT_CONFIG
    a = DEFAULT_CONFIG.get("forward_count_jitter")
    b = DEFAULT_CONFIG.get("forward_images_only")
    return (a is False and b is False,
            "forward_count_jitter=%r forward_images_only=%r" % (a, b))


def main():
    random.seed(20261113)
    chk("jitter_off_fixed", t_jitter_off_fixed)
    chk("jitter_on_range", t_jitter_on_range)
    chk("jitter_never_zero", t_jitter_never_zero)
    chk("jitter_clamped", t_jitter_clamped)
    chk("images_filter_basic", t_images_filter_basic)
    chk("images_filter_robust", t_images_filter_robust)
    chk("images_only_then_pick", t_images_only_then_pick)
    chk("config_defaults", t_config_defaults)
    bad = [r for r in ROWS if not r[1]]
    print("\n%d/%d 通过%s" % (len(ROWS) - len(bad), len(ROWS),
                              "" if not bad else "  失败：" + ", ".join(r[0] for r in bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
