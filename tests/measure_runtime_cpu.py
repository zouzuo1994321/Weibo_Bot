# -*- coding: utf-8 -*-
"""实测 v1.6.11 运行期 CPU：主进程 + 温度宿主进程。

判读口径：
  · 主进程 CPU% —— 概览页开着、每秒取一次状态时的常态占用
  · 温度宿主 —— 应该只在概览页可见时存在，且单次查询 15ms 级别
  · 累计「起过几个 powershell」 —— 旧实现每 30s 一个，60 秒内至少 2 个
"""
import time

import psutil

TARGET = "微博bot小助手.exe"


def find_main():
    for p in psutil.process_iter(["pid", "name"]):
        if (p.info.get("name") or "").lower() == TARGET.lower():
            return psutil.Process(p.info["pid"])
    return None


def temp_hosts():
    out = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        if (p.info.get("name") or "").lower() not in ("powershell.exe", "pwsh.exe"):
            continue
        cmd = " ".join(p.info.get("cmdline") or [])
        if "Win32_PerfFormattedData_Counters_ThermalZone" in cmd:
            out.append(p.info["pid"])
    return out


def main():
    m = find_main()
    if not m:
        print("未找到运行中的 %s" % TARGET)
        return 1
    print("主进程 PID=%d  名称=%s" % (m.pid, m.name()))

    # 统计窗口内是否反复起 powershell：持续采样
    seen = set()
    t0 = time.time()
    c0 = m.cpu_times()
    samples = []
    while time.time() - t0 < 60:
        seen.update(temp_hosts())
        samples.append((round(time.time() - t0), temp_hosts()))
        time.sleep(2)

    c1 = m.cpu_times()
    dt = time.time() - t0
    cpu_s = (c1.user - c0.user) + (c1.system - c0.system)
    ncore = psutil.cpu_count(logical=True) or 1
    print("\n观察 %.0f 秒：" % dt)
    print("  主进程占用 CPU 时间 : %.2f 秒" % cpu_s)
    print("  折算单核百分比      : %.2f %%" % (cpu_s / dt * 100))
    print("  折算整机百分比(%d核) : %.2f %%" % (ncore, cpu_s / dt * 100 / ncore))
    print("  内存 WorkingSet     : %.1f MB" % (m.memory_info().rss / 1048576))
    print("\n  「现存的温度宿主进程」逐 2 秒采样：")
    line = ""
    for t, pids in samples:
        line += ("%d:%s " % (t, "有" if pids else "无"))
    print("   " + line)
    print("  窗口内出现过的温度宿主 PID：%s" % (sorted(seen) if seen else "无"))
    print("\n  判读：")
    print("   · 概览页可见时应有「有」，切走后应转「无」（空闲 90s 自退）")
    print("   · 旧实现每 30 秒新起一个 powershell，60 秒内会看到 2 个不同 PID")
    print("   · 新实现进程只起一次并复用，PID 应只有一个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
