#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v1.6.11 CPU 优化行为验证（不是性能数字，是**行为契约**）。

覆盖：
  A. 无人看概览页 → 温度探测完全不触发（连宿主都不起）
  B. 有人在看     → 到期才探测，两次之间不重复起
  C. 宿主空闲     → 自动退出，内存/CPU 归零
  D. close()      → 宿主进程确实结束，不留孤儿 powershell
  E. 进程被强杀的极端情况 → stdin EOF 让宿主自行退出（父死子亡）
"""
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "_src_v151k"))
os.environ["WEIBOBOT_DATA_DIR"] = tempfile.mkdtemp(prefix="wb_cpuopt_")

import api_bridge  # noqa: E402

OK, BAD = [], []


def check(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                          ("  · " + extra) if extra else ""))


def temp_host_pids():
    """当前存在的温度宿主进程（命令行里带本版本特征串）。"""
    out = []
    try:
        import psutil
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            if (p.info.get("name") or "").lower() not in ("powershell.exe", "pwsh.exe"):
                continue
            cmd = " ".join(p.info.get("cmdline") or [])
            if "Win32_PerfFormattedData_Counters_ThermalZone" in cmd:
                out.append(p.info["pid"])
    except Exception:
        pass
    return out


def main():
    print("=" * 70)
    print("v1.6.11 CPU 优化行为验证")
    print("=" * 70)

    # ---- A. 无人看 → 不探测 ----
    api_bridge._SYS_LOAD_DEMAND["ts"] = 0.0        # 从未有人要过数据
    api_bridge._CPU_TEMP["next_due"] = 0.0          # 到期
    api_bridge._CPU_TEMP["busy"] = False
    api_bridge._schedule_cpu_temp_probe()
    time.sleep(1.2)
    check("A1 无人看概览页时，温度探测直接跳过",
          api_bridge._CPU_TEMP["busy"] is False,
          "busy=%r（False 表示没有起探测线程）"
          % (api_bridge._CPU_TEMP["busy"],))
    check("A2 无人看时不会拉起 PowerShell 宿主",
          not temp_host_pids() and api_bridge._TEMP_HOST._proc is None,
          "宿主进程数=%d" % len(temp_host_pids()))

    # ---- B. 有人看 → 到期才探测 ----
    api_bridge._mark_load_demand()                  # 模拟前端请求负载
    api_bridge._CPU_TEMP["next_due"] = 0.0
    api_bridge._schedule_cpu_temp_probe()
    for _ in range(60):
        if not api_bridge._CPU_TEMP["busy"]:
            break
        time.sleep(0.1)
    check("B1 有人看且到期 → 探测执行并拿到温度",
          api_bridge._CPU_TEMP["val"] is not None,
          "cpu_temp=%r" % (api_bridge._CPU_TEMP["val"],))

    # 未到期时再调一次，不应重复探测
    pids_before = len(temp_host_pids())
    api_bridge._schedule_cpu_temp_probe()
    time.sleep(0.4)
    check("B2 未到期不重复探测（复用缓存）",
          len(temp_host_pids()) == pids_before,
          "宿主进程数 %d → %d" % (pids_before, len(temp_host_pids())))

    # ---- C. 空闲自退 ----
    old_idle = api_bridge._TEMP_IDLE_EXIT
    api_bridge._TEMP_IDLE_EXIT = 0.05              # 缩短到 50ms 便于验证
    api_bridge._TEMP_HOST._last_used = time.time() - 10
    api_bridge._TEMP_HOST._idle_watch = True
    api_bridge._TEMP_HOST._idle_tick()             # 单次自查（不睡 15s）
    api_bridge._TEMP_IDLE_EXIT = old_idle
    check("C1 空闲超时后宿主自动退出",
          api_bridge._TEMP_HOST._proc is None and not temp_host_pids(),
          "宿主进程数=%d" % len(temp_host_pids()))

    # ---- D. close() 清理 ----
    api_bridge._mark_load_demand()
    api_bridge._CPU_TEMP["next_due"] = 0.0
    api_bridge._schedule_cpu_temp_probe()
    for _ in range(60):
        if not api_bridge._CPU_TEMP["busy"]:
            break
        time.sleep(0.1)
    had = len(temp_host_pids())
    api_bridge._close_temp_host()
    time.sleep(0.6)
    check("D1 close() 后进程结束、无孤儿 powershell",
          had >= 1 and not temp_host_pids(),
          "关闭前 %d 个 → 关闭后 %d 个" % (had, len(temp_host_pids())))

    # ---- E. 父进程被强杀 → 宿主靠 stdin EOF 自退 ----
    code = (
        "import os,sys,tempfile\n"
        "sys.path.insert(0, %r)\n"
        "os.environ['WEIBOBOT_DATA_DIR']=tempfile.mkdtemp()\n"
        "import api_bridge\n"
        "print(api_bridge._read_cpu_temp(), flush=True)\n"
        "import time; time.sleep(60)\n" % os.path.join(ROOT, "_src_v151k")
    )
    p = subprocess.Popen([sys.executable, "-c", code],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True)
    p.stdout.readline()
    time.sleep(0.8)
    child_pids = temp_host_pids()
    p.kill()                                        # 模拟任务管理器强杀
    try:
        p.wait(timeout=5)
    except Exception:
        pass
    gone = False
    for _ in range(30):
        time.sleep(0.2)
        if not temp_host_pids():
            gone = True
            break
    check("E1 父进程被强杀后宿主自行退出（stdin EOF）",
          len(child_pids) >= 1 and gone,
          "强杀前 %d 个 → 6s 内 %d 个" % (len(child_pids), len(temp_host_pids())))

    print("=" * 70)
    print("通过 %d 项 / 失败 %d 项" % (len(OK), len(BAD)))
    for b in BAD:
        print("   FAIL:", b)
    print("=" * 70)
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
