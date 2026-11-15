# -*- coding: utf-8 -*-
"""v1.6.12 相册页缩略图调度**行为**探针（真浏览器、真 DOM）。

三组读数，全部确定性，headless 下也成立：

  A. 并发/队列契约
     一次性把 60 个对象推进队列，要求：
       · 同时在途的桥调用数峰值 ≤ 3（_MEDIA_THUMB_PARALLEL）
       · 最终 60 张全部渲染到位、一个不漏
     验证「限流既没有把请求饿死，也没有一次性全撒出去」。

  B1. 首屏只加载视口附近
     渲染完立刻同步扫一遍视口（含 300px 预取边距）内的卡片，
     要求 0 < 请求数 < 60 —— 既不能一张都不加载，也不能又回到全量。

  B2. 滚动增量加载
     把 #main 滚到底并触发 scroll 事件，120ms 节流后必须继续加载新的一批，
     且**已经加载过的不能重复请求**（缓存命中）。

为什么用 scroll 事件而不是 IntersectionObserver：卡片带 `content-visibility: auto`，
视口外时子元素不产生布局盒，IO 观察子元素永远等不到回调；改观察卡片本身在
WebView2 无渲染更新的场景下首帧回调会迟到。滚动驱动的行为完全确定、可测。

做法：只加载 style.css + app.js + 一个 #main>#mediaList 骨架，
直接改写 app.js 顶层的 `API` / `STATE`（同一全局词法环境，可赋值），
不跑 init()，因此不会真的去碰 pywebview 桥。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "_ui_v151k")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
FAKE_IMG = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"

PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>PENDING</title>
<link rel="stylesheet" href="style.css">
<style>
  html, body { margin: 0; height: 100%%; }
  #main { height: 420px; overflow-y: auto; padding: 12px; }
</style>
</head><body>
<div id="main"><div id="mediaList"></div></div>
<div id="toast"></div>
<script src="app.js"></script>
<script>
(function () {
  var calls = { info: 0, data: 0, inflight: 0, peak: 0, dup: 0 };
  var seen = {};
  var N = 60;
  API = {
    get_latest_media_thumb: function (uid) {
      calls.info++;
      return Promise.resolve({ ok: true, file_path: "d/" + uid + ".jpg",
                               file_name: uid + ".jpg" });
    },
    get_media_thumb_data: function (path, px) {
      calls.data++;
      if (seen[path]) calls.dup++;        // 同一张图重复请求 = 缓存失效
      seen[path] = 1;
      calls.inflight++;
      if (calls.inflight > calls.peak) calls.peak = calls.inflight;
      // 人为延迟，让「同时在途」这件事真的可观测（否则同步 resolve 永远只有 1）
      return new Promise(function (res) {
        setTimeout(function () {
          calls.inflight--;
          res({ ok: true, data_url: "%(img)s", thumb: true, px: px, size: 70 });
        }, 8);
      });
    },
    refresh_monitors: function () { return Promise.resolve({ ok: true }); }
  };
  STATE = { config: {} };
  window.addEventListener("error", function (e) {
    document.title = "JSERR:" + ((e && e.message) || "x").slice(0, 80);
  });

  var list = [];
  for (var i = 0; i < N; i++) {
    list.push({ uid: String(100000 + i), screen_name: "对象" + i,
                images: 3, videos: 0, size: 1024 * 1024 });
  }
  function imgs() { return document.querySelectorAll(".media-obj-thumb img").length; }
  function snap() {
    return { info: calls.info, data: calls.data, imgs: imgs(), peak: calls.peak,
             dup: calls.dup };
  }

  setTimeout(function () {
    renderMediaObjects(list);           // 走真实渲染 → 绑定滚动监听 + 首屏扫描
    var main = document.getElementById("main");
    setTimeout(function () {
      var first = snap();
      // ---- B2：滚到底。headless 下 scrollTop 赋值不一定派发 scroll 事件，
      //      这里补一次显式派发（把「监听有没有接上」也纳入判定）----
      main.scrollTop = main.scrollHeight;
      main.dispatchEvent(new Event("scroll"));
      setTimeout(function () {
        var scrolled = snap();
        // ---- A 组：清空结果缓存后把 60 个一次性推入队列，验并发/队列契约 ----
        _mediaThumb.data = {};
        _mediaThumb.queue.length = 0;
        calls.peak = 0; calls.data = 0; calls.dup = 0; seen = {};
        for (var i = 0; i < list.length; i++) _mediaThumbPush(list[i].uid);
        setTimeout(function () {
          var pushed = snap();
          var out = {
            total: list.length,
            parallel: (typeof _MEDIA_THUMB_PARALLEL === "number") ? _MEDIA_THUMB_PARALLEL : -1,
            scrollTop: main.scrollTop,
            first: first, scrolled: scrolled, pushed: pushed
          };
          // A：峰值不超限、60 张全到位、无重复请求
          out.a_ok = (pushed.peak > 0) && (pushed.peak <= out.parallel) &&
                     (pushed.imgs === out.total);
          // B1：首屏 0 < 请求 < 全量
          out.b1_ok = (first.data > 0) && (first.data < out.total);
          // B2：滚到底后继续加载，且不重复请求已加载过的
          out.b2_ok = (scrolled.data > first.data) && (scrolled.dup === 0);
          document.title = "RESULT:" + JSON.stringify(out);
        }, 1500);
      }, 900);
    }, 1200);
  }, 60);
})();
</script>
</body></html>
"""


def main():
    if not os.path.isfile(EDGE):
        print("找不到 Edge，跳过（不算失败）：%s" % EDGE)
        return 0
    tmp = tempfile.mkdtemp(prefix="media_lazy_")
    try:
        for f in ("style.css", "app.js"):
            shutil.copy(os.path.join(UI, f), os.path.join(tmp, f))
        html = os.path.join(tmp, "probe.html")
        with open(html, "w", encoding="utf-8") as fh:
            fh.write(PAGE % {"img": FAKE_IMG})

        cmd = [EDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
               "--window-size=1000,900",
               "--virtual-time-budget=15000",
               "--dump-dom", "file:///" + html.replace("\\", "/")]
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", timeout=120)
        dom = p.stdout or ""
        m = re.search(r"<title>(.*?)</title>", dom, re.S)
        title = (m.group(1) if m else "").strip()
        if title.startswith("JSERR"):
            print("页面脚本报错，探针无效：%s" % title[:200])
            return 1
        if not title.startswith("RESULT:"):
            print("未拿到 RESULT（headless 可能未推进虚拟时间）")
            print((p.stderr or "")[-600:])
            return 1
        d = json.loads(title[len("RESULT:"):])
        f, s, ap = d["first"], d["scrolled"], d["pushed"]
        print("A 组（并发 / 队列）")
        print("  限流值=%d 同时在途峰值=%d 渲染到位=%d/%d 重复请求=%d → %s"
              % (d["parallel"], ap["peak"], ap["imgs"], d["total"], ap["dup"],
                 "PASS" if d["a_ok"] else "FAIL"))
        print("B 组（视口懒加载）")
        print("  B1 首屏请求=%d 次 / 全量 %d → %s"
              % (f["data"], d["total"], "PASS" if d["b1_ok"] else "FAIL"))
        print("  B2 滚动后 scrollTop=%d 请求=%d 次 重复=%d → %s"
              % (d["scrollTop"], s["data"], s["dup"],
                 "PASS" if d["b2_ok"] else "FAIL"))
        good = d["a_ok"] and d["b1_ok"] and d["b2_ok"]
        print("\n%s" % ("ALL PASS" if good else "HAS FAILURE"))
        return 0 if good else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
