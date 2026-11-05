# -*- coding: utf-8 -*-
"""20 轮重启测试（PF-4）。

判定标准（沿用 v1.5.0 定下的口径）：
  · 启动成功 = diag.log 出现本轮的 UI_READY（真正进入主界面），而不是只看窗口出现；
  · 超时 / 未出现 UI_READY / 进程提前退出 = 失败；
  · 每轮记录 LAUNCH → UI_READY 的毫秒数，最后给出 min / 中位 / max。

用法:
  python tests/restart_20.py                 # 默认跑 exe，20 轮
  python tests/restart_20.py --rounds 5
  python tests/restart_20.py --source        # 改跑源码（调试用）
"""
import argparse
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "微博bot小助手.exe")
PY = r"C:\Users\zouzu\.workbuddy\binaries\python\envs\weibobot\Scripts\python.exe"
SRC_APP = os.path.join(ROOT, "_src_v151k", "_app_v117.py")
DIAG = os.path.join(os.environ["LOCALAPPDATA"], "微博bot小助手", "data", "diag.log")
TS = "%Y-%m-%d %H:%M:%S.%f"


def read_lines():
    try:
        with open(DIAG, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def parse_ts(s):
    return time.mktime(time.strptime(s, TS))


def kill_all(timeout=15):
    """按镜像名杀**所有**实例。

    onefile 的 exe 是「引导进程 + 子进程」两个 PID，只 kill Popen 拿到的那个
    会留下子进程占着单实例互斥体 —— 下一轮就会命中 SINGLE_INST_BLOCK，
    然后弹窗等用户点确定，20 轮里能有 19 轮白等。
    """
    image = os.path.basename(EXE)
    deadline = time.time() + timeout
    while time.time() < deadline:
        subprocess.run(["taskkill", "/IM", image, "/F"],
                       capture_output=True, text=True, errors="replace")
        time.sleep(0.4)
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + image],
                           capture_output=True, text=True, errors="replace")
        if image.lower() not in (r.stdout or "").lower():
            return True
    return False


def one_round(launch_cmd, timeout):
    before = len(read_lines())
    t_proc = time.time()
    proc = subprocess.Popen(launch_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    launch_ts = ready_ts = None
    exited_early = False
    deadline = time.time() + timeout
    while time.time() < deadline:
        for ln in read_lines()[before:]:
            m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+) \|\s+(\w+)", ln)
            if not m:
                continue
            if m.group(2) == "LAUNCH" and launch_ts is None:
                launch_ts = m.group(1)
            elif m.group(2) == "UI_READY":
                ready_ts = m.group(1)
        if ready_ts:
            break
        if proc.poll() is not None:
            exited_early = True
            break
        time.sleep(0.05)
    try:
        proc.kill()
        proc.wait(timeout=15)
    except Exception:
        pass
    kill_all()
    if exited_early:
        return ("EXIT_EARLY", None)
    if not ready_ts:
        return ("TIMEOUT", None)
    # 统一用「本轮 LAUNCH → 本轮 UI_READY」的真实差值（不要用进程启动时刻兜底，
    # 那会把 kill/等待也算进去，20 轮会整齐地显示成同一个整数）。
    if launch_ts:
        return ("OK", int(round((parse_ts(ready_ts) - parse_ts(launch_ts)) * 1000)))
    return ("OK", None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--source", action="store_true", help="跑源码而非 exe")
    a = ap.parse_args()

    cmd = [PY, SRC_APP] if a.source else [EXE]
    if not a.source and not os.path.exists(EXE):
        print("找不到 exe：%s" % EXE)
        return 2
    kill_all()   # 起跑前先清干净，避免上一轮退出拖尾占着互斥体

    print("重启测试 %d 轮 · 目标=%s" % (a.rounds, os.path.basename(cmd[-1])))
    results = []
    for i in range(1, a.rounds + 1):
        status, ms = one_round(cmd, a.timeout)
        results.append((status, ms))
        print("  第 %2d 轮  %-10s %s" % (i, status, ("%d ms" % ms) if ms else "—"))
        time.sleep(1.2)

    ok = [ms for st, ms in results if st == "OK" and ms]
    fails = [(i + 1, st) for i, (st, ms) in enumerate(results) if st != "OK"]
    print("-" * 46)
    print("通过 %d/%d" % (len(ok), len(results)))
    if ok:
        ok_sorted = sorted(ok)
        print("耗时 min=%dms  中位=%dms  max=%dms" %
              (ok_sorted[0], ok_sorted[len(ok_sorted) // 2], ok_sorted[-1]))
    if fails:
        print("失败轮次：%s" % fails)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
