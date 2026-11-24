# -*- coding: utf-8 -*-
"""验证「排程偏移分层抽样」（v1.6.17）—— 一圈一圈走得开。

要证的两件事：
  1. 同一轮内 N 条排程的相邻间隔有**下限**（段长 × 0.4），不再出现 0~2 秒；
  2. 所有偏移仍落在原窗口 `[0, 间隔×ratio]` 内（"多久内发完"的上限没被改大）。

对照组（必须存在，否则「测出很均匀」无法解读）：用旧口径
`uniform(0, cap)` 独立抽样 N 次，同样的判据**必须被判失败**。
如果对照组也"通过"，说明断言写错了，不是代码变好了。

真机背景（修复前，两天 179 个圈）：最短 0 秒、<10 秒占 15.1%、中位 30 秒。

用法：python tests/probe_fwd_spacing.py
"""
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "_src_v151k"))

import forward_engine as fe  # noqa: E402

CFG = {"interval_minutes": 15, "poll_jitter_ratio": 0.15}
CAP = 15 * 60 * 0.15          # 135 秒
TRIALS = 300
CASES = (1, 2, 3, 5, 10)


def _gaps(offs):
    s = sorted(offs)
    return [b - a for a, b in zip(s, s[1:])]


def check_stratified():
    print("场景一：分层抽样（新口径）")
    ok_all = True
    for n in CASES:
        step = CAP / n
        floor = step * 0.4 * 0.98      # 留 2% 浮点余量
        worst_gap, worst_spread = 1e9, 0.0
        for _ in range(TRIALS):
            offs = [fe._compute_forward_offset(CFG, slot=(i, n)) for i in range(n)]
            if min(offs) < 0 or max(offs) > CAP + 1e-6:
                print("   n=%-2d ★ 越界：min=%.2f max=%.2f cap=%.2f"
                      % (n, min(offs), max(offs), CAP))
                ok_all = False
            g = _gaps(offs)
            if g:
                worst_gap = min(worst_gap, min(g))
            worst_spread = max(worst_spread, max(offs) - min(offs))
        if n == 1:
            print("   n=1  单条：偏移 %.1f~%.1f 秒（窗口内随机，无相邻间隔可测）"
                  % (CAP * 0.2, CAP * 0.8))
            continue
        passed = worst_gap >= floor
        ok_all = ok_all and passed
        print("   n=%-2d 段长 %5.1fs · 间隔下限要求 %5.1fs · 实测最差 %5.1fs · %s"
              % (n, step, floor, worst_gap, "PASS" if passed else "FAIL"))
    return ok_all


def check_control():
    """对照组：旧口径独立抽样，同样的判据必须失败。"""
    print("场景二：对照组（旧口径 uniform 独立抽样，应当被判失败）")
    undetected = 0
    for n in (3, 5, 10):
        step = CAP / n
        floor = step * 0.4 * 0.98
        caught = 0
        for _ in range(TRIALS):
            offs = [random.uniform(0, CAP) for _ in range(n)]
            if min(_gaps(offs)) < floor:
                caught += 1
        rate = 100.0 * caught / TRIALS
        if caught == 0:
            undetected += 1
        print("   n=%-2d 对照组被判失败 %5.1f%% 的抽样" % (n, rate))
    print("   → 对照组全部被捕获" if undetected == 0
          else "   → ★ 有 %d 组连对照组都测不出，断言无效" % undetected)
    return undetected == 0


def main():
    print("间隔 15 分钟 · ratio 0.15 · 窗口 %.0f 秒 · 每组 %d 次抽样\n" % (CAP, TRIALS))
    a = check_stratified()
    print()
    b = check_control()
    print()
    if a and b:
        print("ALL_SPACING_CHECKS_PASS")
        return 0
    print("SPACING_CHECKS_FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
