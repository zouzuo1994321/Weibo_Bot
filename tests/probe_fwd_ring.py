# -*- coding: utf-8 -*-
"""v1.6.16 进度环「一圈 = 一条转发」行为探针。

用户口径（原话）：
    确认排程后，根据目前时间到「已排程转发（预计 … 执行）」进行一圈，一圈完毕对应
    一条发布。如果排程 4 条就转 4 圈，如果一条就转一圈。转完排程后知道下一轮排程前，
    未排程时都保持空闲。无新微博的轮次没有终点也保持空闲。

真机时间戳（本机 app.log 2026-11-21）：
    02:51:50  本轮抓取完成 → last_run，同时 5 条转发排程为
              02:51:53 / 02:51:58 / 02:52:37 / 02:54:05 / 02:54:37
              （即 forward_plan_at=02:51:50，forward_runs=上面 5 个）

判据：
  A. 5 条排程 → 恰好 5 圈，跑满时刻与该条转发时刻相差 ≤2s；
  B. 圈内进度单调不减（不再出现 v1.6.15 那种「一秒倒退 28.7 个百分点」）；
  C. 全部发完后 → 空闲（环熄灭）；
  D. 没有排程（forward_runs 为空）→ 空闲；
  E. 调度未运行 → 空闲；
  F. 脏数据（字符串 / 0 / null / NaN / 重复）不得算出 NaN；
  G. 只排 1 条 → 只走 1 圈；
  H. 圈与圈切换有高亮脉冲（= 一条发布出去）。

旧口径的对照留档在 probe_fwd_ring_v1615.py（一圈 3 秒、最长/最短差 299 倍）。

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
  get: function () { return function () { return Promise.resolve(null); }; }
}) };
</script>
"""

JS = """<script>
setTimeout(function () {
  try {
    var sp = document.getElementById("bootSplash");
    if (sp) sp.style.display = "none";
    var vs = document.querySelectorAll(".view");
    for (var i = 0; i < vs.length; i++) vs[i].classList.remove("active");
    var ov = document.getElementById("view-overview") || document.getElementById("view-system");
    if (ov) ov.classList.add("active");

    var T = function (s) { return +new Date("2026/11/21 " + s); };
    var PLAN_AT = T("02:51:50") / 1000;
    var RUNS = [T("02:51:53") / 1000, T("02:51:58") / 1000, T("02:52:37") / 1000,
                T("02:54:05") / 1000, T("02:54:37") / 1000];

    var card = document.getElementById("statForwards");
    var prog = document.getElementById("fwdRingProg");
    layoutFwdRing();
    var perim = _fwdPerim;

    function setSched(o) {
      STATE = { scheduler: o, stats: {}, config: { forward_count: 5 } };
      _fwdGeom = "";
      layoutFwdRing();
      perim = _fwdPerim;
    }
    // 进度环算「现在」用的是 Date.now()（ts 只用于节流），必须接管才能把时间拨过去
    function sample(t) {
      window.__NOW = t;
      _fwdLastFrame = 0;
      tickForwardRing(t);
      var off = parseFloat(prog.style.strokeDashoffset || "0");
      var r = { prog: +(100 * (1 - off / perim)).toFixed(1),
                active: card.classList.contains("fwd-active"),
                done: card.classList.contains("fwd-done") };
      if (_fwdRafId) { cancelAnimationFrame(_fwdRafId); _fwdRafId = 0; }
      return r;
    }
    Date.now = function () { return window.__NOW || 0; };

    var out = { perim: +perim.toFixed(1) };

    // ── 场景一：5 条排程，逐秒走完整个计划 ──
    setSched({ running: true, interval_minutes: 15, next_run: T("02:57:32") / 1000,
               forward_runs: RUNS, forward_plan_at: PLAN_AT, next_forward_run: RUNS[0] });
    var from = PLAN_AT * 1000, to = RUNS[RUNS.length - 1] * 1000 + 5000;
    var rows = [], prevProg = null, prevIdx = -1, prevDone = false, mono = true;
    var fulls = [], pulses = 0;
    for (var t = from; t <= to; t += 1000) {
      var r = sample(t);
      r.t = Math.round((t - from) / 1000);
      rows.push(r);
      if (r.done && !(rows.length > 1 && rows[rows.length - 2].done)) pulses++;
      if (r.prog >= 99.5 && !(rows.length > 1 && rows[rows.length - 2].prog >= 99.5)) {
        fulls.push(r.t);
      }
      // 高亮脉冲期环刻意钉在 100%，这一帧不参与「倒退」判定
      if (prevProg !== null && prevIdx === _fwdLapIdx && !r.done && !prevDone
          && r.prog + 0.6 < prevProg) mono = false;
      prevProg = r.prog;
      prevIdx = _fwdLapIdx;
      prevDone = r.done;
    }
    out.plan = { rows: rows, fulls: fulls, pulses: pulses, monotonic: mono };

    // ── 场景二：全部发完之后（应空闲）──
    var after = [];
    [RUNS[RUNS.length - 1] * 1000 + 6000, RUNS[RUNS.length - 1] * 1000 + 60000].forEach(function (t) {
      var r = sample(t); r.t = Math.round((t - from) / 1000); after.push(r);
    });
    out.after_done = after;

    // ── 场景三：单条排程（应只有 1 圈）──
    setSched({ running: true, interval_minutes: 15, next_run: T("02:57:32") / 1000,
               forward_runs: [RUNS[0]], forward_plan_at: PLAN_AT, next_forward_run: RUNS[0] });
    var one = [], oneFull = 0;
    for (var t2 = from; t2 <= RUNS[0] * 1000 + 5000; t2 += 1000) {
      var r2 = sample(t2);
      if (r2.prog >= 99.5 && !(one.length && one[one.length - 1].prog >= 99.5)) oneFull++;
      one.push({ t: Math.round((t2 - from) / 1000), prog: r2.prog });
    }
    out.single = { rows: one, fulls: oneFull };

    // ── 场景四：本轮没排到转发（forward_runs 为空）──
    setSched({ running: true, interval_minutes: 15, next_run: T("02:57:32") / 1000,
               forward_runs: [], forward_plan_at: null, next_forward_run: null });
    out.idle_no_plan = sample(T("02:52:00"));

    // ── 场景五：调度未运行 ──
    setSched({ running: false, interval_minutes: 15, next_run: null,
               forward_runs: RUNS, forward_plan_at: PLAN_AT, next_forward_run: RUNS[0] });
    out.idle_not_running = sample(T("02:52:00"));

    // ── 场景六：脏数据不得算出 NaN ──
    setSched({ running: true, interval_minutes: 15, next_run: T("02:57:32") / 1000,
               forward_runs: ["abc", 0, null, RUNS[1], RUNS[0], RUNS[0], NaN],
               forward_plan_at: PLAN_AT, next_forward_run: RUNS[0] });
    out.dirty = sample(T("02:51:55"));   // 早于第 2 条（02:51:58），应仍在走圈

    document.title = "RESULT:" + JSON.stringify(out);
  } catch (e) { document.title = "JSERR:" + (e && e.message); }
}, 500);
</script>
"""


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
               "--window-size=1280,1100", "--virtual-time-budget=9000", "--dump-dom",
               "file:///" + page.replace("\\", "/")]
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", timeout=150)
        m = re.search(r"<title>(.*?)</title>", p.stdout or "", re.S)
        title = (m.group(1) if m else "").strip()
        if title.startswith("JSERR"):
            print("页面脚本报错：%s" % title[:300])
            return 1
        if not title.startswith("RESULT:"):
            print("未拿到 RESULT：%s" % title[:200])
            return 1
        d = json.loads(title[len("RESULT:"):])

        plan = d["plan"]
        rows = plan["rows"]
        print("环周长 = %.1f px（卡片外框）\n" % d["perim"])
        print("场景一：5 条排程（+3s / +8s / +47s / +135s / +167s）")
        step = max(1, len(rows) // 14)
        print("   进度采样：" + " ".join(
            "+%ss=%.0f%%" % (r["t"], r["prog"]) for r in rows[::step]))
        print("   跑满时刻（相对计划起点）：%s 秒" % plan["fulls"])
        print("   高亮脉冲 = %d 次 ；圈内单调不减 = %r" % (plan["pulses"], plan["monotonic"]))
        print()
        print("场景二：全部发完之后 → %s" % [
            {"t": "+%ss" % r["t"], "active": r["active"], "prog": r["prog"]}
            for r in d["after_done"]])
        print("场景三：只排 1 条 → 跑满 %d 次" % d["single"]["fulls"])
        print("场景四：没排到转发 → active=%r prog=%.1f%%"
              % (d["idle_no_plan"]["active"], d["idle_no_plan"]["prog"]))
        print("场景五：调度未运行 → active=%r prog=%.1f%%"
              % (d["idle_not_running"]["active"], d["idle_not_running"]["prog"]))
        print("场景六：脏数据 → active=%r prog=%.1f%%（不得 NaN）"
              % (d["dirty"]["active"], d["dirty"]["prog"]))

        exp_full = [3, 8, 47, 135, 167]      # 各条相对计划起点的偏移秒
        got = plan["fulls"]
        ok_full = len(got) == 5 and all(abs(a - b) <= 2 for a, b in zip(got, exp_full))
        ok_idle_after = all((not r["active"]) for r in d["after_done"])
        ok_single = d["single"]["fulls"] == 1
        ok_idle_plan = (not d["idle_no_plan"]["active"])
        ok_idle_run = (not d["idle_not_running"]["active"])
        ok_dirty = (d["dirty"]["active"] is True
                    and d["dirty"]["prog"] == d["dirty"]["prog"])
        ok_mono = plan["monotonic"]
        ok_pulse = plan["pulses"] >= 4      # 5 圈至少 4 次「上一条发出去」的切换脉冲

        print("\n—— 判定 ——")
        checks = [
            ("A 5 条排程 = 5 圈，跑满对齐各条发布时间", ok_full,
             "实测 %s / 期望 %s" % (got, exp_full)),
            ("B 圈内进度单调不减（不倒退）", ok_mono, ""),
            ("C 全部发完 → 空闲", ok_idle_after, ""),
            ("D 无排程 → 空闲", ok_idle_plan, ""),
            ("E 调度未运行 → 空闲", ok_idle_run, ""),
            ("F 脏数据不算出 NaN", ok_dirty, "prog=%.1f" % d["dirty"]["prog"]),
            ("G 单条排程只走 1 圈", ok_single, "跑满 %d 次" % d["single"]["fulls"]),
            ("H 圈间切换有高亮脉冲", ok_pulse, "%d 次" % plan["pulses"]),
        ]
        for name, ok, detail in checks:
            print("  %s %s%s" % ("PASS" if ok else "FAIL", name,
                                 ("  · " + detail) if detail else ""))
        bad = [c for c in checks if not c[1]]
        print("\n%s" % ("ALL_RING_CHECKS_PASS" if not bad else "%d 项未通过" % len(bad)))
        return 0 if not bad else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
