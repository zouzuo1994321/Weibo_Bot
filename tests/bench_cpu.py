#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CPU 热点微基准（v1.6.11 优化前的量化基线）。

只测「每秒/每 30 秒被重复执行」的那几个函数，单位毫秒。
"""
import os
import sys
import time
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "_src_v151k")
sys.path.insert(0, SRC)

_TMP = tempfile.mkdtemp(prefix="wb_cpu_")
os.environ["WEIBOBOT_DATA_DIR"] = _TMP

import ai_engine                      # noqa: E402
import model_manager                  # noqa: E402
import api_bridge                     # noqa: E402


def bench(fn, n=20, warm=3):
    for _ in range(warm):
        try:
            fn()
        except Exception:
            pass
    t0 = time.perf_counter()
    for _ in range(n):
        try:
            fn()
        except Exception:
            pass
    dt = (time.perf_counter() - t0) / n * 1000.0
    return dt


def main():
    print("=" * 64)
    print("CPU 热点微基准 — 数据目录:", _TMP)
    print("=" * 64)

    rows = []
    rows.append(("ai_engine.load_config()  [每次 get_state 都调]",
                 bench(ai_engine.load_config)))
    rows.append(("model_manager.maybe_autostart()  [每次 get_state 都调]",
                 bench(model_manager.maybe_autostart)))

    api = api_bridge.Api()
    rows.append(("_build_state()  [每 5s 重建一次]",
                 bench(api._build_state, n=5, warm=1)))

    # get_state 命中缓存的成本（前端每秒 1 次）
    api_bridge._STATE_CACHE["ts"] = time.time()
    api_bridge._STATE_CACHE["data"] = {"ok": True}
    rows.append(("get_state()  [每秒 1 次，命中缓存]",
                 bench(api.get_state, n=30)))

    # 温度探测：v1.6.11 改为常驻宿主 —— 冷启动只付一次，之后走稳态
    t0 = time.perf_counter()
    v = api_bridge._read_cpu_temp()
    dt_cold = (time.perf_counter() - t0) * 1000.0
    rows.append(("_read_cpu_temp()  冷启动（起宿主+WMI 装配，仅一次）", dt_cold))

    xs = []
    for _ in range(5):
        t1 = time.perf_counter()
        api_bridge._read_cpu_temp()
        xs.append((time.perf_counter() - t1) * 1000.0)
    dt_temp = sorted(xs)[len(xs) // 2]
    rows.append(("_read_cpu_temp()  稳态（常驻宿主一问一答）", dt_temp))

    for name, ms in rows:
        print("  %-56s %8.2f ms" % (name, ms))
    print("  温度读回值:", v)
    print("=" * 64)

    # 折算：概览页开着时的每分钟额外开销
    pct = lambda ms, per_min: ms * per_min  # noqa: E731
    ms_cfg = rows[0][1]
    ms_auto = rows[1][1]
    ms_build = rows[2][1]
    print("每分钟重复开销估算（概览页常开）：")
    print("  get_state 内的配置读盘        : %8.1f ms/min  (60 次)" % pct(ms_cfg, 60))
    print("  get_state 内的模型自启动检查  : %8.1f ms/min  (60 次)" % pct(ms_auto, 60))
    print("  _build_state 全量重建         : %8.1f ms/min  (12 次)" % pct(ms_build, 12))
    print("  读温度（v1.6.10 旧实现）      : %8.1f ms/min  (2 次 × 400ms)"
          % pct(400.0, 2))
    print("  读温度（v1.6.11 常驻宿主）    : %8.1f ms/min  (2 次 × 稳态)"
          % pct(dt_temp, 2))
    print("  ------------------------------------------------------")
    print("  合计（优化后）                : %8.1f ms/min"
          % (pct(ms_cfg, 60) + pct(ms_auto, 60) + pct(ms_build, 12)
             + pct(dt_temp, 2)))
    print("=" * 64)

    api_bridge._close_temp_host()


if __name__ == "__main__":
    main()
