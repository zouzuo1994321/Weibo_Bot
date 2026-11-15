#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证 PDH 计数器能否直读温度（纯 ctypes，不起进程、约 1ms）。

PDH = Performance Data Helper，Windows 原生性能计数器 API（pdh.dll）。
若机器上存在 "Thermal Zone Information" 计数器组，这是最便宜的读温度方式：
  - 无子进程
  - 无 83MB 常驻内存
  - 单次调用微秒级
"""
import ctypes
import ctypes.wintypes as wt
import time

PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2
PDH_NO_DATA = 0x800007D5
PDH_INVALID_DATA = 0xC0000BC6
PDH_STATUS_OK = 0


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", wt.DWORD), ("doubleValue", ctypes.c_double)]


pdh = ctypes.WinDLL("pdh.dll")

# 候选计数器路径（不同 Windows 版本命名略有差异）
CANDIDATES = [
    r"\Thermal Zone Information(*)\Temperature",
    r"\Thermal Zone Information(*)\High Precision Temperature",
    r"\Energy Meter(*)\Power",
]


def _expand(path):
    """把带 (*) 的通配计数器实例展开成真实实例名。"""
    buf_sz = wt.DWORD(0)
    rc = pdh.PdhExpandWildCardPathW(None, path, None, ctypes.byref(buf_sz), 0)
    if rc != PDH_MORE_DATA and rc != PDH_STATUS_OK:
        return []
    buf = ctypes.create_unicode_buffer(buf_sz.value // 2 + 2)
    rc = pdh.PdhExpandWildCardPathW(None, path, buf, ctypes.byref(buf_sz), 0)
    if rc != PDH_STATUS_OK:
        return []
    return [s for s in buf[:].split("\x00") if s]


def read_temp_pdh():
    """返回摄氏度或 None。"""
    q = wt.HANDLE()
    if pdh.PdhOpenQueryW(None, 0, ctypes.byref(q)) != PDH_STATUS_OK:
        return None
    try:
        handles = []
        for path in CANDIDATES:
            for inst in _expand(path):
                h = wt.HANDLE()
                if pdh.PdhAddCounterW(q, inst, 0, ctypes.byref(h)) == PDH_STATUS_OK:
                    handles.append((inst, h))
        if not handles:
            return None
        # 第一次采集建立基线
        if pdh.PdhCollectQueryData(q) != PDH_STATUS_OK:
            return None
        time.sleep(0.002)
        if pdh.PdhCollectQueryData(q) != PDH_STATUS_OK:
            return None
        vals = []
        for inst, h in handles:
            v = PDH_FMT_COUNTERVALUE()
            rc = pdh.PdhGetFormattedCounterValue(
                h, PDH_FMT_DOUBLE, None, ctypes.byref(v))
            if rc == PDH_STATUS_OK and 5.0 <= v.doubleValue <= 110.0:
                vals.append((inst, round(v.doubleValue, 1)))
        if not vals:
            return None
        vals.sort(key=lambda x: -x[1])
        return vals[0][1], vals
    finally:
        pdh.PdhCloseQuery(q)


def main():
    print("=" * 70)
    print("PDH 直读温度验证")
    print("=" * 70)
    for path in CANDIDATES:
        ins = _expand(path)
        print("  %-46s -> %r" % (path, ins[:4] if ins else "无实例"))

    t0 = time.perf_counter()
    r = read_temp_pdh()
    cost = (time.perf_counter() - t0) * 1000
    print("  --------------------------------------------------------------")
    print("  read_temp_pdh()  %8.2f ms  -> %r" % (cost, r))

    if r:
        xs = []
        for _ in range(5):
            t1 = time.perf_counter()
            read_temp_pdh()
            xs.append((time.perf_counter() - t1) * 1000)
        print("  连续 5 次       %8.2f ms/次（中位）" % sorted(xs)[len(xs) // 2])
    print("=" * 70)


if __name__ == "__main__":
    main()
