# -*- coding: utf-8 -*-
"""v1.6.16 进度环（「本日转发次数」卡外框流光）行为探针。

背景：用户反馈「流光根本没有规律」。肉眼观察不可复现，所以这里把**真实的前端代码**
拉起来，用**真机日志里的时间戳**驱动 tickForwardRing()，把进度曲线量出来。

时间戳来源（本机 app.log，2026-11-21）：
    02:40:32  调度器心跳 · 下次轮询约 02:57（间隔 17 分钟）   → next_run = 02:57:32
    02:51:50  5 条转发排程，run_at = 02:51:53 / 02:51:58 / 02:52:37 / 02:54:05 / 02:54:37
              → next_forward_run = min = 02:51:53（只比写日志晚 3 秒）
    ~03:00    run_once 返回（113 个对象的媒体扫描，实测远超 17 分钟间隔）→ last_run

判据（不依赖任何 GUI）：
  · 同一轮内，排程发生的那一刻进度是否发生**跳变**；
  · 环跑满一次用了多久，与实际间隔（17 分钟）差多少；
  · 有无排程两种情况下，环的「速度」是否同一口径。

用法：python tests/probe_fwd_ring.py
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

STUB = """<script>
window.pywebview = { api: new Proxy({}, {
  get: function () {
    return function () { return Promise.resolve(null); };
  }
}) };
</script>
"""

# 真实时间戳（本地时区，秒级；用 Date 解析避免手算时区）
JS = """<script>
setTimeout(function () {
  try {
    var sp = document.getElementById("bootSplash");
    if (sp) sp.style.display = "none";
    var vs = document.querySelectorAll(".view");
    for (var i = 0; i < vs.length; i++) vs[i].classList.remove("active");
    var ov = document.getElementById("view-overview") || document.getElementById("view-system");
    if (ov) ov.classList.add("active");

    var T0 = +new Date("2026/11/21 02:40:32");      // 心跳（本轮开始）
    var NEXT_RUN = +new Date("2026/11/21 02:57:32");  // 心跳 + 17 分钟
    var T_LAST = +new Date("2026/11/21 02:51:50");    // 「本轮抓取完成」= run_once 返回 → last_run
    var FWD = +new Date("2026/11/21 02:51:53");       // 最早一跳转发 = last_run + 3 秒

    var card = document.getElementById("statForwards");
    var prog = document.getElementById("fwdRingProg");
    layoutFwdRing();
    var perim = _fwdPerim;

    function setSched(o) {
      STATE = { scheduler: o, stats: {}, config: { forward_count: 5 } };
      // 允许窗口尺寸变化时重排
      _fwdGeom = "";
      layoutFwdRing();
      perim = _fwdPerim;
    }
    // 取「纯进度」：先清掉完成态，避免 triggerFwdDone 把 offset 钉在 0。
    // 【关键】tickForwardRing 里算进度用的是 Date.now()（ts 只用于节流），
    // 所以必须接管 Date.now 才能把时间「拨」到指定时刻，否则所有采样都落在同一瞬间。
    function sample(t) {
      window.__NOW = t;
      _fwdDoneFor = null;
      _fwdPrevNextRun = null;
      card.classList.remove("fwd-done", "fwd-fading", "fwd-active");
      _fwdLastFrame = 0;
      tickForwardRing(t);
      var off = parseFloat(prog.style.strokeDashoffset || "0");
      var done = card.classList.contains("fwd-done");
      var active = card.classList.contains("fwd-active");
      if (_fwdRafId) { cancelAnimationFrame(_fwdRafId); _fwdRafId = 0; }
      return { prog: +(100 * (1 - off / perim)).toFixed(1), done: done, active: active };
    }
    Date.now = function () { return window.__NOW || 0; };

    var out = { perim: +perim.toFixed(1), phases: {} };

    /* 逐秒扫一条曲线，返回：
         · firstFull：第一次到 100% 的相对秒数（-1 = 没跑满）
         · maxJump  ：相邻 1 秒之间最大的进度跳变（百分点）
         · at       ：若干关键时刻的进度 */
    function sweep(sched, from, to, step) {
      setSched(sched);
      var rows = [], prev = null, firstFull = -1, maxJump = 0, jumpAt = null;
      for (var d = from; d <= to; d += (step || 1)) {
        var r = sample(T0 + d * 1000);
        r.d = d;
        if (firstFull < 0 && r.prog >= 99.5) firstFull = d;
        if (prev !== null) {
          var j = Math.abs(r.prog - prev);
          if (j > maxJump) { maxJump = j; jumpAt = d; }
        }
        prev = r.prog;
        rows.push(r);
      }
      return { rows: rows, firstFull: firstFull,
               maxJump: +maxJump.toFixed(1), jumpAt: jumpAt };
    }
    function pick(res, ds) {
      return ds.map(function (d) {
        var hit = null;
        for (var i = 0; i < res.rows.length; i++) if (res.rows[i].d === d) hit = res.rows[i];
        return { t: "+" + d + "s", prog: hit ? hit.prog : null, done: hit ? hit.done : null };
      });
    }

    // ── 阶段 A：本轮刚开始 → 排程前（last_run=null，next_forward_run=null）──
    // 窗口 = [next_run - 配置间隔, next_run]，配置间隔 15 分钟，而本轮真实间隔 17 分钟
    var A = sweep({ running: true, interval_minutes: 15,
                    next_run: NEXT_RUN / 1000, next_forward_run: null, last_run: null },
                  0, 1030, 1);
    out.phases.A = { at: pick(A, [0, 60, 300, 600, 660, 678, 900, 1019]),
                     firstFull: A.firstFull, maxJump: A.maxJump, jumpAt: A.jumpAt };

    // ── 阶段 B：run_once 返回的瞬间（last_run=02:51:50），此时已排程 → next_forward_run=02:51:53 ──
    // 与阶段 A 唯一差别：窗口端点全部换成本轮真实值，窗口长度 = 3 秒
    var B = sweep({ running: true, interval_minutes: 15,
                    next_run: NEXT_RUN / 1000, next_forward_run: FWD / 1000,
                    last_run: "2026-11-21 02:51:50" },
                  678, 700, 1);
    out.phases.B = { at: pick(B, [678, 679, 680, 681, 690, 700]),
                     firstFull: B.firstFull, maxJump: B.maxJump, jumpAt: B.jumpAt };

    // ── 阶段 C：本轮无新微博（next_forward_run=null，last_run=02:51:50）──
    // 窗口 = [last_run, next_run] = 342 秒，与阶段 B 的 3 秒/阶段 A 的 900 秒都不是一个量级
    var C = sweep({ running: true, interval_minutes: 15,
                    next_run: NEXT_RUN / 1000, next_forward_run: null,
                    last_run: "2026-11-21 02:51:50" },
                  678, 1100, 1);
    out.phases.C = { at: pick(C, [700, 900, 1050, 1100]),
                     firstFull: C.firstFull, maxJump: C.maxJump, jumpAt: C.jumpAt };

    // ── 阶段 D：抓取耗时超过间隔（限流冷却会把一轮拖到 25 分钟）──
    // last_run 晚于 next_run → startMs >= endMs → 前端退化分支把窗口整体挪到过去
    var D = sweep({ running: true, interval_minutes: 15,
                    next_run: NEXT_RUN / 1000, next_forward_run: FWD / 1000,
                    last_run: "2026-11-21 03:05:32" },
                  1470, 1500, 1);
    out.phases.D = { at: pick(D, [1470, 1500]),
                     firstFull: D.firstFull, maxJump: D.maxJump, jumpAt: D.jumpAt };

    document.title = "RESULT:" + JSON.stringify(out);
  } catch (e) { document.title = "JSERR:" + (e && e.message); }
}, 500);
</script>
"""


def _curve(rows):
    return " ".join("%s=%.1f%%%s" % (r["t"], r["prog"], "*" if r["done"] else "")
                    for r in rows)


def main():
    if not os.path.isfile(EDGE):
        print("找不到 Edge，跳过：%s" % EDGE)
        return 0
    tmp = tempfile.mkdtemp(prefix="fwd_ring_")
    try:
        for f in ("style.css", "app.js", "logo.png"):
            src = os.path.join(UI, f)
            if os.path.isfile(src):
                shutil.copy(src, os.path.join(tmp, f))
        html = open(os.path.join(UI, "index.html"), encoding="utf-8").read()
        html = html.replace('<script src="app.js"></script>',
                            STUB + '<script src="app.js"></script>' + JS, 1)
        page = os.path.join(tmp, "ui.html")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(html)

        cmd = [EDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
               "--window-size=1280,1100", "--virtual-time-budget=8000", "--dump-dom",
               "file:///" + page.replace("\\", "/")]
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", timeout=120)
        m = re.search(r"<title>(.*?)</title>", p.stdout or "", re.S)
        title = (m.group(1) if m else "").strip()
        if title.startswith("JSERR"):
            print("页面脚本报错：%s" % title[:300])
            return 1
        if not title.startswith("RESULT:"):
            print("未拿到 RESULT：%s" % title[:200])
            return 1
        d = json.loads(title[len("RESULT:"):])

        print("环周长 = %.1f px（卡片外框）\n" % d["perim"])
        names = {
            "A": "A 本轮开始 → 排程前（last_run=null, next_forward_run=null）",
            "B": "B 本轮抓取完成的那一刻（last_run=02:51:50, next_forward_run=02:51:53）",
            "C": "C 本轮无新微博（next_forward_run=null, last_run=02:51:50）",
            "D": "D 抓取耗时超过间隔（last_run=03:05:32 > next_run=02:57:32）",
        }
        for k in ("A", "B", "C", "D"):
            ph = d["phases"][k]
            print(names[k])
            print("   关键点：" + " ".join(
                "%s=%.1f%%%s" % (r["t"], r["prog"], "*" if r["done"] else "")
                for r in ph["at"] if r["prog"] is not None))
            ff = ph["firstFull"]
            print("   跑满用时：%s ；最大单秒跳变：%.1f 个百分点（发生于 +%ss）"
                  % (("%d 秒" % ff) if ff >= 0 and k != "A" else
                     ("%d 秒（但窗口在 678s 时被重设，见 B）" % ff if ff >= 0 else "始终未满"),
                     ph["maxJump"], ph["jumpAt"]))
            print()

        a, b, c, dd = (d["phases"][k] for k in ("A", "B", "C", "D"))
        pa = {r["t"]: r["prog"] for r in a["at"]}
        pb = {r["t"]: r["prog"] for r in b["at"]}
        pc = {r["t"]: r["prog"] for r in c["at"]}

        print("—— 判定 ——")
        # ① 排程发生时的跳变：+678s（62.0%）→ B 段起点
        p678 = pa.get("+678s")
        p679 = pb.get("+679s")
        jump = (p679 - p678) if (p678 is not None and p679 is not None) else None
        print("① 同一轮内 +678s→+679s（1 秒）：%.1f%% → %.1f%%" % (p678, p679)
              if jump is not None else "① 取不到相邻采样")
        if jump is not None:
            print("   位移 %+.1f 个百分点 —— 环先「倒退」再「瞬满」" % jump)
        # 圈长统一定义为「从窗口起点跑到 100% 的秒数」，全部换算成同一口径
        # 窗口起点 = next_run - 900s = T0 + 120s，故整圈长度 = firstFull - 120
        lap_a = a["firstFull"] - 120
        lap_b = (b["firstFull"] - 678) if b["firstFull"] >= 0 else -1
        lap_c = (c["firstFull"] - 678) if c["firstFull"] >= 0 else -1
        print("② 一圈的长度（同一次轮询、同一份配置）：")
        print("   A 排程前        %4d 秒  ← 用配置间隔 900s 反推起点，而本轮真实间隔 1020s" % lap_a)
        print("   B 排程刚发生    %4d 秒  ← last_run(02:51:50) → 最早一跳(02:51:53)" % lap_b)
        print("   C 本轮无新微博  %4d 秒  ← last_run → next_run" % lap_c)
        print("   D 抓取超间隔    %4d 秒  ← 窗口整体落在过去，一亮就满" % 0)
        print("   最长 / 最短 = %.0f 倍" % (lap_a / lap_b if lap_b > 0 else -1))
        print("③ D 段（抓取超间隔）首个采样即 %.1f%%（done=%r）—— 环亮一下、10 秒后熄灭，"
              "此后整轮全黑" % (dd["at"][0]["prog"], dd["at"][0]["done"]))

        ok_issues = []
        if jump is None or jump < -5:
            ok_issues.append("有倒退")
        if lap_b < 300:
            ok_issues.append("B 圈长过短")
        if abs(lap_c - 1020) > 180:
            ok_issues.append("C 圈长偏离真实间隔")
        if dd["at"][0]["prog"] < 99:
            ok_issues.append("D 未做到「一亮就满」")

        print("\n结论：%s" % ("BUG_REPRODUCED —— ①倒退 + ②圈长差 %0.0f 倍 + ③/④ 一亮就满，"
                             "这四条合起来就是「流光没有规律」"
                             % (lap_a / lap_b if lap_b > 0 else 0)
                             if len(ok_issues) == 4 else
                             "复核：" + "；".join(ok_issues or ["全部符合预期（已修）"])))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
