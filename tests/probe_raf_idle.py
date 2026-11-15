#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""前端进度环「空闲停摆」实测（v1.6.11）。

为什么要单独实测：`tickForwardRing` 的改动是「空闲时不再自我续期 rAF」。
这种问题看代码容易看错，而在无头浏览器里数「rAF 回调次数」又不可靠
（headless 的合成器可能根本不驱动 rAF，0 帧无法区分「正确停摆」和「环境不支持」）。

因此改为**直接测调度契约**：拦在 requestAnimationFrame 上数「有没有被再次排帧」，
然后手工调用 tickForwardRing 驱动，完全确定性、不依赖合成器时序。

  对照组：同样的拦法，一个裸 rAF 自续期循环 —— 必须能数到排帧，
          证明拦截器本身有效（否则 0 就毫无意义）。

  ① 调度未运行 → tickForwardRing 一帧后**不得**再排帧（旧实现会排）
  ② 调度运行中 → tickForwardRing 必须排下一帧
  ③ 运行中首帧 → strokeDashoffset 落在 (0, 周长) 之间，环真的在画
  ④ 100ms 内的下一帧 → 只排帧、不重写 dashoffset（节流生效）
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

FAKE = r"""
<script>
(function () {
  var NOW = Date.now() / 1000;
  var ANS = {
    get_state: {
      loading: false,
      stats: { monitored_posts_today: 3, forwards_today: 2, forwards_total: 10,
               monitors: 1, accounts: 0, media_files: 0 },
      monitors: [{uid:"1", name:"对象A"}],
      accounts: [],
      config: { forward_count: 1 },
      license: { licensed: true, type_name: "永久授权" },
      scheduler: { running: false, interval_minutes: 30, in_working_hours: true,
                   last_run: null, next_run: null, next_forward_run: null,
                   last_result: null },
      hot: { items: [], pinned: [] }
    },
    get_system_load: { cpu: 1, mem: 2, net_up: 0, net_down: 0, gpu: null,
                       gpu_temp: null, cpu_temp: null },
    get_account_health: { level: "ok", message: "正常", unhealthy: [] },
    get_hot_search: { items: [], pinned: [], updated_at: 0 },
    get_app_info: { version: "v1.6.11", internal: "2611140007" },
    list_personas: [], get_poll_progress: { running: false }
  };
  window.pywebview = { api: new Proxy({}, { get: function (t, k) {
    return function () {
      var v = window.__ANS[k];
      return Promise.resolve(v === undefined ? null : v);
    };
  } }) };
  window.__ANS = ANS;
})();
</script>
"""

SPY = r"""
<script>
(function () {
  window.__SCHED = [];
  var _raf = window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame = function (cb) {
    window.__SCHED.push(1);
    return _raf(cb);
  };
  /* 对照组：裸自续期循环。若它也数不到排帧，说明拦截器/环境有问题。 */
  window.__CONTROL = 0;
  (function loop() {
    window.__CONTROL++;
    window.requestAnimationFrame(loop);
  })();
})();
</script>
"""

MEASURE = r"""
<script>
window.addEventListener("load", function () {
  setTimeout(function () {
    var out = { control: window.__CONTROL };
    try {
      var card = document.getElementById("statForwards");
      var prog = document.getElementById("fwdRingProg");
      out.controlRaf = window.__CONTROL > 0;

      // ---------- ① 空闲：调度未运行 ----------
      STATE.scheduler.running = false;
      STATE.scheduler.next_run = null;
      window.__SCHED.length = 0;
      tickForwardRing(window.performance.now());
      out.idleSched = window.__SCHED.length;                 // 期望 0
      out.idleCls = card ? card.className : "";
      out.idleOff = prog ? prog.style.strokeDashoffset : "";

      // ---------- ② 运行中：必须排帧 ----------
      var nowS = Date.now() / 1000;
      STATE.scheduler.running = true;
      STATE.scheduler.next_run = nowS + 900;
      STATE.scheduler.next_forward_run = nowS + 900;
      // 让进度落在 50%：起点 900s 前，终点 900s 后
      var d = new Date((nowS - 900) * 1000);
      var p2 = function (n) { return (n < 10 ? "0" : "") + n; };
      STATE.scheduler.last_run = d.getFullYear() + "-" + p2(d.getMonth() + 1)
        + "-" + p2(d.getDate()) + " " + p2(d.getHours()) + ":"
        + p2(d.getMinutes()) + ":" + p2(d.getSeconds());
      window.__SCHED.length = 0;
      _fwdLastFrame = 0;                       // 确保不被上一场景的节流挡住
      tickForwardRing(window.performance.now());
      out.runSched = window.__SCHED.length;                   // 期望 1
      out.runCls = card ? card.className : "";
      out.runOff = prog ? prog.style.strokeDashoffset : "";
      out.runDash = prog ? prog.style.strokeDasharray : "";

      // ---------- ④ 100ms 内再调：只排帧、不改 dashoffset ----------
      var offBefore = prog ? prog.style.strokeDashoffset : "";
      window.__SCHED.length = 0;
      tickForwardRing(window.performance.now() + 10);
      out.throttleSched = window.__SCHED.length;              // 期望 1（仍要排帧）
      out.throttleSame = (prog ? prog.style.strokeDashoffset : "") === offBefore;
    } catch (e) {
      out.err = String(e);
    }
    document.title = "ANS:" + JSON.stringify(out);
  }, 2000);
});
</script>
"""


def build(tmp):
    for name in ("style.css", "index.html", "app.js"):
        shutil.copy2(os.path.join(UI, name), os.path.join(tmp, name))
    p = os.path.join(tmp, "index.html")
    html = open(p, encoding="utf-8").read()
    html = html.replace('<script src="app.js"', SPY + FAKE + '<script src="app.js"', 1)
    html = html.replace("</body>", MEASURE + "</body>", 1)
    open(p, "w", encoding="utf-8").write(html)


def run(tmp):
    out = subprocess.run(
        [EDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--virtual-time-budget=8000", "--window-size=1280,900", "--dump-dom",
         "file:///" + os.path.join(tmp, "index.html").replace("\\", "/")],
        capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="ignore")
    dom = out.stdout or ""
    m = re.search(r"ANS:(\{.*?\})</title>", dom)
    if not m:
        m = re.search(r"ANS:(\{.*?\})", dom)
    if not m:
        return None, dom[:500]
    return json.loads(m.group(1).replace("&quot;", '"')), ""


def main():
    ok, bad = [], []

    def chk(name, cond, extra=""):
        (ok if cond else bad).append(name)
        print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                              ("  · " + extra) if extra else ""))

    print("=" * 74)
    print("前端进度环调度契约实测")
    print("=" * 74)

    tmp = tempfile.mkdtemp(prefix="wb_raf_")
    build(tmp)
    r, err = run(tmp)
    if r is None:
        print("  [ERROR] 取不到测量结果：", err)
        return 1
    print("  原始测量:", json.dumps(r, ensure_ascii=False))
    print("  " + "-" * 70)

    if r.get("err"):
        print("  [ERROR] 页面内异常：", r["err"])
        return 1

    # 对照组：证明拦截器有效
    chk("对照 拦截器有效（裸 rAF 循环能数到排帧）",
        r.get("controlRaf") is True, "control=%r" % r.get("control"))

    chk("① 调度未运行时不再排帧（旧实现会续期）",
        r.get("idleSched") == 0, "排帧次数=%r（期望 0）" % r.get("idleSched"))
    chk("① 空闲时环已隐藏（无 fwd-active）",
        "fwd-active" not in (r.get("idleCls") or ""), "class=%r" % r.get("idleCls"))

    chk("② 调度运行中正常排下一帧",
        r.get("runSched") == 1, "排帧次数=%r（期望 1）" % r.get("runSched"))
    chk("③ 环已点亮（fwd-active）",
        "fwd-active" in (r.get("runCls") or ""), "class=%r" % r.get("runCls"))

    off = _num(r.get("runOff"))
    dash = _num(r.get("runDash"))
    chk("③ 进度环画到一半左右（dashoffset 落在 0~周长之间）",
        0 < off < dash if dash > 0 else False,
        "offset=%.1f 周长=%.1f 进度≈%.0f%%"
        % (off, dash, (1 - off / dash) * 100 if dash > 0 else -1))

    chk("④ 节流生效：100ms 内不再重绘，但仍排帧",
        r.get("throttleSched") == 1 and r.get("throttleSame") is True,
        "排帧=%r dashoffset未变=%r"
        % (r.get("throttleSched"), r.get("throttleSame")))

    print("=" * 74)
    print("通过 %d 项 / 失败 %d 项" % (len(ok), len(bad)))
    for b in bad:
        print("   FAIL:", b)
    print("=" * 74)
    return 1 if bad else 0


def _num(s):
    try:
        return float(s)
    except Exception:
        return -1.0


if __name__ == "__main__":
    sys.exit(main())
