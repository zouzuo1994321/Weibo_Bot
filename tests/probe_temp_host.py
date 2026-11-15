#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证「常驻 PowerShell 温度查询宿主」的可行性 —— 逐次查询耗时。

思路：进程只起一次，之后每次通过 stdin 送一行命令、从 stdout 读一行结果。
进程启动的 198ms 与 .NET/CIM 首次装配成本只付一次。
"""
import subprocess
import time

CF = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
IDLE = getattr(subprocess, "IDLE_PRIORITY_CLASS", 0x00000040)

SCRIPT = (
    "$ErrorActionPreference='SilentlyContinue';"
    "while($true){"
    "  $line=[Console]::In.ReadLine();"
    "  if($line -eq $null){break}"
    "  if($line -eq 'Q'){break}"
    "  $v=@();"
    "  $v+=Get-CimInstance -ClassName "
    "Win32_PerfFormattedData_Counters_ThermalZoneInformation "
    "|ForEach-Object{($_.HighPrecisionTemperature/10)-273.15};"
    "  if($v.Count -eq 0){"
    "    $v+=Get-CimInstance -Namespace root/wmi "
    "-ClassName MSAcpi_ThermalZoneTemperature "
    "|ForEach-Object{$_.CurrentTemperature/10-273.15};"
    "  }"
    "  $ok=@($v|Where-Object{$_ -is [double] -and $_ -ge 5 -and $_ -le 110});"
    "  if($ok.Count -gt 0){[Console]::Out.WriteLine([math]::Round(($ok|Measure-Object -Maximum).Maximum,1))}"
    "  else{[Console]::Out.WriteLine('-')}"
    "}"
)


def main():
    print("=" * 70)
    print("常驻 PowerShell 温度宿主 — 逐次查询耗时")
    print("=" * 70)

    t0 = time.perf_counter()
    p = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=CF | IDLE, text=True, encoding="utf-8", bufsize=1,
    )
    # 预热：第一次查询要把 CIM 类装配起来
    p.stdin.write("T\n")
    p.stdin.flush()
    first = p.stdout.readline().strip()
    boot = (time.perf_counter() - t0) * 1000
    print("  启动+首次查询（预热）        %8.0f ms   -> %r" % (boot, first))

    xs = []
    for i in range(6):
        t1 = time.perf_counter()
        p.stdin.write("T\n")
        p.stdin.flush()
        v = p.stdout.readline().strip()
        xs.append((time.perf_counter() - t1) * 1000)
        print("  第 %d 次查询                   %8.1f ms   -> %r" % (i + 1, xs[-1], v))

    p.stdin.write("Q\n")
    p.stdin.flush()
    try:
        p.wait(timeout=5)
    except Exception:
        p.kill()

    print("  --------------------------------------------------------------")
    print("  稳态单次查询（取中位）        %8.1f ms" % sorted(xs)[len(xs) // 2])
    print("  对比：每次起新进程            %8.0f ms" % 400)
    print("=" * 70)


if __name__ == "__main__":
    main()
