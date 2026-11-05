# -*- coding: utf-8 -*-
"""启动耗时 A/B 测量：private_mode vs 持久化 profile。

用法: python measure_boot.py [--runs 3]
输出每次的 LAUNCH→UI_READY 毫秒。
"""
import os
import re
import subprocess
import sys
import time

ROOT = r"E:\【01】自研软件\【26-11】微博bot小助手（正式版）"
PY = r"C:\Users\zouzu\.workbuddy\binaries\python\envs\weibobot\Scripts\python.exe"
APP = os.path.join(ROOT, "_src_v151k", "_app_v117.py")
DIAG = os.path.join(os.environ["LOCALAPPDATA"], "微博bot小助手", "data", "diag.log")


def kill_all():
    subprocess.run(["taskkill", "/IM", "python.exe", "/F"],
                   capture_output=True) if False else None
    # 只杀本应用源码进程：用 wmic 太慢，改为按窗口标题找不到；这里靠单实例，
    # 上一轮进程在 poll 结束后由 p.kill() 收掉。
    pass


def read_lines():
    try:
        with open(DIAG, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def one_run(env_extra, timeout=30):
    before = len(read_lines())
    env = dict(os.environ)
    env.update(env_extra)
    p = subprocess.Popen([PY, APP], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=env)
    t0 = time.time()
    deadline = t0 + timeout
    launch_ts = ready_ts = None
    while time.time() < deadline:
        lines = read_lines()[before:]
        for ln in lines:
            m = re.match(r"(\S+ \S+) \|\s+(\w+)", ln)
            if not m:
                continue
            ts, tag = m.group(1), m.group(2)
            if tag == "LAUNCH" and launch_ts is None:
                launch_ts = ts
            if tag == "UI_READY":
                ready_ts = ts
        if ready_ts:
            break
        time.sleep(0.05)
    p.kill()
    try:
        p.wait(timeout=10)
    except Exception:
        pass
    if not (launch_ts and ready_ts):
        return None
    fmt = "%Y-%m-%d %H:%M:%S.%f"
    a = time.mktime(time.strptime(launch_ts, fmt))
    b = time.mktime(time.strptime(ready_ts, fmt))
    return int((b - a) * 1000)


def main():
    runs = 3
    if "--runs" in sys.argv:
        runs = int(sys.argv[sys.argv.index("--runs") + 1])
    for label, extra in (("private_mode=True", {"WB_PRIVATE_MODE": "1"}),
                         ("persistent profile", {"WB_PRIVATE_MODE": "0"})):
        vals = []
        for i in range(runs):
            v = one_run(extra)
            vals.append(v)
            print("  %-20s run%d: %s ms" % (label, i + 1, v if v else "TIMEOUT"))
            time.sleep(1.0)
        ok = [v for v in vals if v]
        if ok:
            print("== %s: n=%d min=%d median=%d" %
                  (label, len(ok), min(ok), sorted(ok)[len(ok) // 2]))
        print()


if __name__ == "__main__":
    main()
