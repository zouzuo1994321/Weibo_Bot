#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""拆解 CPU 温度探测的 1 秒都花在哪（v1.6.11 优化依据）。"""
import subprocess
import time

CF = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def t(cmd, n=3):
    xs = []
    out = ""
    for _ in range(n):
        t0 = time.perf_counter()
        r = subprocess.run(cmd, capture_output=True, timeout=30, creationflags=CF)
        xs.append((time.perf_counter() - t0) * 1000)
        out = (r.stdout or b"").decode("utf-8", "ignore").strip()[:70]
    return min(xs), out


PS = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]

CASES = {
    "powershell 空转（仅 1）": ["powershell", "-NoProfile", "-NonInteractive",
                          "-Command", "1"],
    "Acpi_ThermalZoneTemperature": PS + [
        "Get-CimInstance -Namespace root/wmi -ClassName "
        "MSAcpi_ThermalZoneTemperature | ForEach-Object "
        "{[math]::Round($_.CurrentTemperature/10-273.15,1)}"],
    "PerfFormatted_Counters_Thermal": PS + [
        "Get-CimInstance -ClassName "
        "Win32_PerfFormattedData_Counters_ThermalZoneInformation | ForEach-Object "
        "{[math]::Round(($_.HighPrecisionTemperature/10)-273.15,1)}"],
    "两者合并（现状 _PS_CPU_TEMP）": PS + [
        "$t=@();$t+=Get-CimInstance -Namespace root/wmi -ClassName "
        "MSAcpi_ThermalZoneTemperature | ForEach-Object "
        "{$_.CurrentTemperature/10-273.15};$t|ForEach-Object "
        "{[math]::Round($_,1)}"],
}


def main():
    print("=" * 70)
    print("CPU 温度探测耗时拆解")
    print("=" * 70)
    for k, v in CASES.items():
        ms, out = t(v)
        print("  %-34s %8.0f ms   -> %r" % (k, ms, out))
    print("=" * 70)


if __name__ == "__main__":
    main()
