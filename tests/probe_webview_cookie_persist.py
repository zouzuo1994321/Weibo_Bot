#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""实验：pywebview(private_mode=False, storage_path=X) 的 Cookie 到底落在哪？

    python tests/probe_webview_cookie_persist.py

为什么需要这个实验
------------------
CookieMonitor 报告说「内置浏览器 profile 里 0 条 Cookie」，我们据此推断是
os._exit(0) 硬杀导致 WebView2 没来得及落盘（D4）。但 v1.6.14 加了 0.9s 等待之后，
开发机上仍然是 0 条 —— 说明「没落盘」可能根本不是因为硬杀，而是 **profile 压根没被用上**。

pywebview 的 storage_path 在 winforms/edgechromium 后端里究竟是
「UserDataFolder」还是「只在 private_mode 下临时用」，版本之间行为不同。
本脚本用**一个全新的临时目录**直接验证：

  1. 窗口加载 weibo.com（任何一次访问微博都会下发若干 Cookie，不需要登录）；
  2. 等 12s 让浏览器把 Cookie 写盘；
  3. 正常 destroy 窗口，再等 3s；
  4. 在临时目录里找所有 Cookies 库，统计总条数与微博域条数。

判定：
  · 临时目录下出现 Cookies 库且条数 > 0  → profile 生效，D4 只需「等落盘」；
  · 出现库但条数恒为 0 / 只有空库       → WebView2 在别处写，需换注入方案；
  · 连库都没有                          → storage_path 完全没传下去。

只读、只在本机新建一个临时目录，跑完即删。
"""
import glob
import os
import shutil
import sqlite3
import sys
import tempfile
import time

import webview

TMP = os.path.join(tempfile.gettempdir(), "wbwv_probe_%d" % int(time.time()))
URL = "https://weibo.com/"


def _scan(root):
    """列出 root 下所有 Cookies 库及其条数。"""
    out = []
    for db in glob.glob(os.path.join(root, "**", "Cookies"), recursive=True):
        if not os.path.isfile(db):
            continue
        info = {"path": db, "size": os.path.getsize(db), "total": -1, "weibo": -1}
        try:
            uri = "file:%s?mode=ro&immutable=1" % db.replace("\\", "/")
            con = sqlite3.connect(uri, uri=True, timeout=2.0)
            try:
                info["total"] = con.execute("SELECT COUNT(*) FROM cookies").fetchone()[0]
                info["weibo"] = con.execute(
                    "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%weibo%' "
                    "OR host_key LIKE '%sina%'").fetchone()[0]
            finally:
                con.close()
        except Exception as e:
            info["error"] = str(e)
        out.append(info)
    return out


def _later():
    """start() 之后在另一个线程里干活：加载 → 等 → 关窗。"""
    time.sleep(12)
    print("\n[probe] 关闭窗口（正常 destroy，非硬杀）…")
    try:
        webview.windows[0].destroy()
    except Exception as e:
        print("[probe] destroy 失败：%s" % e)


def main():
    os.makedirs(TMP, exist_ok=True)
    print("[probe] storage_path = %s" % TMP)
    print("[probe] 加载 %s …" % URL)
    win = webview.create_window("cookie persist probe", url=URL,
                                width=900, height=640, background_color="#141416")
    webview.start(_later, private_mode=False, storage_path=TMP)
    # 关键时序观测：start() 一返回就扫一次（= 我们 0.9s 等待之前的状态），
    # 再看等待之后有没有变化。这直接回答「那 0.9s 到底有没有用」。
    t0 = time.time()
    rows_now = _scan(TMP)
    print("\n[probe] start() 返回后立刻扫描（+%.2fs）：" % (time.time() - t0))
    for r in rows_now:
        print("    %s size=%s 总条数=%s 微博域=%s"
              % (os.path.basename(os.path.dirname(os.path.dirname(r["path"]))),
                 r["size"], r["total"], r["weibo"]))

    print("[probe] 等 3s 让 WebView2 收尾…")
    time.sleep(3)

    rows = _scan(TMP)
    print("\n=== 等待 3s 之后 ===")
    if not rows:
        print("  没有任何 Cookies 库 —— storage_path 很可能没被当作 UserDataFolder")
    for r in rows:
        print("  %s\n    size=%s 总条数=%s 微博域=%s %s"
              % (r["path"], r["size"], r["total"], r["weibo"],
                 r.get("error", "")))

    total = sum(max(0, r["total"]) for r in rows)
    weibo = sum(max(0, r["weibo"]) for r in rows)
    print("\n结论：总条数=%d 微博域=%d" % (total, weibo))
    if total > 0:
        print("  → profile 生效，Cookie 确实写进了 storage_path 指定的目录。")
    elif rows:
        print("  → 库建出来了但一条 Cookie 都没有：WebView2 把 Cookie 写在别处。")
    else:
        print("  → 连库都没有：storage_path 没有被传给 WebView2。")

    # 顺便看看系统里其它 WebView2 profile 有没有刚写的 Cookie（证明写到了哪）
    print("\n=== 参考：%APPDATA%\\pywebview 下有没有刚写的库 ===")
    alt = os.path.join(os.environ.get("APPDATA", ""), "pywebview")
    if os.path.isdir(alt):
        for r in _scan(alt):
            mt = time.strftime("%m-%d %H:%M:%S", time.localtime(os.path.getmtime(r["path"])))
            print("  %s\n    mtime=%s 总条数=%s" % (r["path"], mt, r["total"]))
    else:
        print("  （不存在）")

    time.sleep(1)
    shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
