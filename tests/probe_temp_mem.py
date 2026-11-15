#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""量常驻 PowerShell 宿主的内存占用（决定是否加「空闲自杀」）。"""
import subprocess
import time

import psutil

CF = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
IDLE = getattr(subprocess, "IDLE_PRIORITY_CLASS", 0x00000040)

SCRIPT = (
    "$ErrorActionPreference='SilentlyContinue';"
    "while($true){"
    "  $line=[Console]::In.ReadLine();"
    "  if($line -eq $null -or $line -eq 'Q'){break}"
    "  $v=Get-CimInstance -ClassName "
    "Win32_PerfFormattedData_Counters_ThermalZoneInformation "
    "|ForEach-Object{($_.HighPrecisionTemperature/10)-273.15};"
    "  $ok=@($v|Where-Object{$_ -is [double]});"
    "  if($ok.Count -gt 0){[Console]::Out.WriteLine([math]::Round(($ok|Measure-Object -Maximum).Maximum,1))}"
    "  else{[Console]::Out.WriteLine('-')}"
    "}"
)


def main():
    p = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=CF | IDLE, text=True, encoding="utf-8", bufsize=1,
    )
    p.stdin.write("T\n")
    p.stdin.flush()
    p.stdout.readline()

    pr = psutil.Process(p.pid)
    time.sleep(2.0)
    m = pr.memory_info()
    print("=" * 66)
    print("常驻 PowerShell 宿主内存（已预热、空闲 2s）")
    print("  WorkingSet (物理驻留) : %6.1f MB" % (m.rss / 1048576))
    print("  PrivateBytes          : %6.1f MB" % (m.private / 1048576))
    print("  CPU 占用（空闲 2s 采样）: %s" % (pr.cpu_percent(interval=2.0),))
    # 子进程（WmiPrvSE 不计入）
    print("=" * 66)

    p.stdin.write("Q\n")
    p.stdin.flush()
    try:
        p.wait(timeout=5)
        print("  退出正常，退出码 =", p.returncode)
    except Exception:
        p.kill()
        print("  超时强杀")


if __name__ == "__main__":
    main()
