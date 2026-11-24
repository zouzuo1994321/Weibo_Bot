# -*- coding: utf-8 -*-
"""v1.6.12 转发设置页视觉小样：确认两个新开关真的渲染出来、且状态跟配置一致。

不是像素级回归，只做两件事：
  ① 用假桥把真实 index.html + app.js 拉起来，切到「转发设置」页截一张图；
  ② 断言 #fwdCountJitter / #fwdImagesOnly 存在，且勾选状态与 STATE.config 一致。

输出：tests/_shots/forward_settings.png
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
OUT = os.path.join(ROOT, "tests", "_shots")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

STUB = """<script>
// 假桥：只回答本探针关心的几个方法，其余一律 null
window.pywebview = { api: new Proxy({}, {
  get: function (t, k) {
    return function () {
      var ANS = {
        get_state: { loading: false, accounts: [], monitors: [],
          scheduler: { running: false },
          config: { interval_minutes: 30, forward_count: 5,
                    forward_count_jitter: true, forward_images_only: false,
                    ai_enabled: false, working_hours: { enabled: false, start: "09:00", end: "23:00" },
                    poll_jitter_ratio: 0.15, monitor_auto_refresh: { enabled: false, minutes: 10 },
                    media_download: { enabled: false, interval_minutes: 30, max_pages: 5 } } },
        get_ai_settings: {}, get_ai_engine_status: {}, get_ai_deploy_status: {},
        get_system_load: {}, list_accounts: [], get_poll_progress: { active: false }
      };
      return Promise.resolve(ANS[k] !== undefined ? ANS[k] : null);
    };
  }
}) };
</script>
"""

FINISH = """<script>
setTimeout(function () {
  try {
    var sp = document.getElementById("bootSplash");
    if (sp) sp.style.display = "none";
    var vs = document.querySelectorAll(".view");
    for (var i = 0; i < vs.length; i++) vs[i].classList.remove("active");
    document.getElementById("view-forward").classList.add("active");
    STATE = { config: { interval_minutes: 30, forward_count: 5,
                        forward_count_jitter: true, forward_images_only: false,
                        ai_enabled: false,
                        working_hours: { enabled: false, start: "09:00", end: "23:00" } } };
    renderForward();
    var j = document.getElementById("fwdCountJitter");
    var m = document.getElementById("fwdImagesOnly");
    // 量几何：开关圆心分别对比「标题行中心」与「整行中心」。
    // 【v1.6.16】用户要求开关与整行垂直居中，故新增 rowDelta（这才是判据）；
    // titleDelta 保留作对照 —— 它能证明改动前后确实从「贴标题行」变成了「整行居中」。
    function geo(chk) {
      if (!chk) return null;
      var row = chk.closest(".fwd-opt-row");
      var sw = row.querySelector(".switch");
      var tt = row.querySelector("b");
      if (!sw || !tt) return null;
      var a = sw.getBoundingClientRect(), b = tt.getBoundingClientRect();
      var r = row.getBoundingClientRect();
      return { swCenter: +(a.top + a.height / 2).toFixed(1),
               titleCenter: +(b.top + b.height / 2).toFixed(1),
               titleDelta: +((a.top + a.height / 2) - (b.top + b.height / 2)).toFixed(1),
               rowCenter: +(r.top + r.height / 2).toFixed(1),
               rowDelta: +((a.top + a.height / 2) - (r.top + r.height / 2)).toFixed(1),
               rowH: +r.height.toFixed(1),
               swH: +a.height.toFixed(1), titleH: +b.height.toFixed(1) };
    }
    // 【v1.6.13】两卡等高核验：直接量 .fwd-grid2 内两张卡的外框高度
    var cards = document.querySelectorAll(".fwd-grid2 > .card");
    var heights = [];
    for (var ci = 0; ci < cards.length; ci++) {
      heights.push({ h: +cards[ci].getBoundingClientRect().height.toFixed(1),
                     title: (cards[ci].querySelector("h3") || {}).textContent || "" });
    }
    // 量「内容自然高度」：把网格改成 align-items:start，卡片不再被拉伸，量到的是真实内容需求
    var grid = document.querySelector(".fwd-grid2");
    var natural = [];
    if (grid) {
      var oldAlign = grid.style.alignItems;
      grid.style.alignItems = "start";
      for (var bi = 0; bi < cards.length; bi++) {
        natural.push(+cards[bi].getBoundingClientRect().height.toFixed(1));
      }
      grid.style.alignItems = oldAlign || "";
    }
    document.title = "RESULT:" + JSON.stringify({
      jitter: j ? j.checked : null,
      imagesOnly: m ? m.checked : null,
      jitterDisabled: j ? j.disabled : null,
      offRow: !!(j && j.closest(".fwd-opt-row") && j.closest(".fwd-opt-row").classList.contains("is-off")),
      html: !!document.querySelector(".fwd-opt-list"),
      geoJitter: geo(j), geoImages: geo(m),
      inModeCard: !!(m && m.closest(".card") && m.closest(".card").querySelector(".fwd-mode-row")),
      heights: heights, natural: natural
    });
  } catch (e) { document.title = "JSERR:" + e.message; }
}, 400);
</script>
"""


def main():
    if not os.path.isfile(EDGE):
        print("找不到 Edge，跳过：%s" % EDGE)
        return 0
    os.makedirs(OUT, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="fwd_ui_")
    try:
        for f in ("style.css", "app.js", "logo.png"):
            src = os.path.join(UI, f)
            if os.path.isfile(src):
                shutil.copy(src, os.path.join(tmp, f))
        html = open(os.path.join(UI, "index.html"), encoding="utf-8").read()
        html = html.replace('<script src="app.js"></script>',
                            STUB + '<script src="app.js"></script>' + FINISH, 1)
        page = os.path.join(tmp, "ui.html")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(html)

        shot = os.path.join(OUT, "forward_settings.png")
        if os.path.isfile(shot):
            os.remove(shot)
        cmd = [EDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
               "--window-size=1280,1100", "--virtual-time-budget=6000",
               "--screenshot=" + shot, "--dump-dom",
               "file:///" + page.replace("\\", "/")]
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", timeout=120)
        dom = p.stdout or ""
        m = re.search(r"<title>(.*?)</title>", dom, re.S)
        title = (m.group(1) if m else "").strip()
        if title.startswith("JSERR"):
            print("页面脚本报错：%s" % title[:300])
            return 1
        if not title.startswith("RESULT:"):
            print("未拿到 RESULT：%s" % title[:200])
            return 1
        d = json.loads(title[len("RESULT:"):])
        print("开关渲染：容器=%r 波动勾选=%r 含图勾选=%r 波动禁用=%r 半透明标记=%r"
              % (d["html"], d["jitter"], d["imagesOnly"],
                 d["jitterDisabled"], d["offRow"]))
        for k in ("geoJitter", "geoImages"):
            print("  几何 %s：%s" % (k, d.get(k)))
        # 【v1.6.16】判据：开关圆心必须落在整行垂直中线（±1px），且不再贴标题行
        deltas = []
        for k in ("geoJitter", "geoImages"):
            g = d.get(k) or {}
            if g.get("rowDelta") is not None:
                deltas.append(abs(g["rowDelta"]))
        centered = bool(deltas) and max(deltas) <= 1.0
        print("  开关相对整行居中：%r（最大偏差 %.1f px）"
              % (centered, max(deltas) if deltas else -1))
        print("  含图开关在转发方式卡内：%r" % d.get("inModeCard"))
        print("  两卡外框高度：%s" % d.get("heights"))
        print("  两卡内容自然高度：%s" % d.get("natural"))
        hs = [x["h"] for x in (d.get("heights") or [])]
        equal = len(hs) == 2 and abs(hs[0] - hs[1]) < 0.5
        print("  两卡等高：%r（差 %s px）"
              % (equal, (round(abs(hs[0] - hs[1]), 1) if len(hs) == 2 else "n/a")))
        ok = (d["html"] and d["jitter"] is True and d["imagesOnly"] is False and centered)
        print("截图：%s（存在=%r）" % (shot, os.path.isfile(shot)))
        print("\n%s" % ("PASS" if ok else "FAIL"))
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
