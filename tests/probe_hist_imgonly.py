# -*- coding: utf-8 -*-
"""v1.6.13「只看有图」默认开启 —— 行为探针。

为什么要单独跑一次：这条需求看着只是给 checkbox 加个 checked，实际上有三条路径会写它
（首次进页 switchHist / 切到转发记录 switchHist / 清空筛选 clearHistFilters），
任何一处漏改都会出现「默认开、点一下清空筛选就变关」的不一致。

断言：
  A. 初始 DOM 就是勾选态（index.html 的 checked 属性生效）
  B. switchHist("monitored") 后 = 勾选，且真的把无图行滤掉了（3 行 → 2 行）
  C. switchHist("forwards") 后 = 不勾选、控件隐藏（v1.6.10 的收起行为不能被破坏）
  D. 切回 monitored = 重新勾选、仍 2 行
  E. clearHistFilters() 后回到默认（勾选），不是「清空 = 全关」
  F. 手动取消勾选 → 3 行全出（说明默认值不是硬编码死过滤）

用法：msedge --headless=new + 假桥，不需要启动 exe。
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
// 3 条被监控微博（2 条带图 / 1 条纯文字）+ 1 条转发记录
var MON_ROWS = [
  { id: 1, uid: "u1", screen_name: "甲", content: "带图 1", images: ["a.jpg"],
    fetched_at: "2026-11-13 10:00", created_at: "2026-11-13 09:00" },
  { id: 2, uid: "u2", screen_name: "乙", content: "纯文字", images: [],
    fetched_at: "2026-11-13 10:00", created_at: "2026-11-13 09:00" },
  { id: 3, uid: "u3", screen_name: "丙", content: "带图 2", images: ["b.jpg"],
    fetched_at: "2026-11-13 10:00", created_at: "2026-11-13 09:00" }
];
var FWD_ROWS = [
  { id: 11, account_name: "主号", screen_name: "甲", status: "success",
    forward_type: "normal", original_content: "原", forward_content: "",
    created_at: "2026-11-13 10:00" }
];
window.pywebview = { api: new Proxy({}, {
  get: function (t, k) {
    return function () {
      var ANS = {
        get_state: { loading: false, accounts: [], monitors: [],
          scheduler: { running: false },
          config: { interval_minutes: 30, forward_count: 5,
                    forward_count_jitter: false, forward_images_only: false,
                    ai_enabled: false,
                    working_hours: { enabled: false, start: "09:00", end: "23:00" },
                    poll_jitter_ratio: 0.15,
                    monitor_auto_refresh: { enabled: false, minutes: 10 },
                    media_download: { enabled: false, interval_minutes: 30, max_pages: 5 } } },
        get_monitored_posts: MON_ROWS,
        get_forwards: FWD_ROWS,
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
function snap(tag) {
  var chk = document.getElementById("hfHasImg");
  var wrap = document.getElementById("histImgOnly");
  return { tag: tag,
           checked: chk ? chk.checked : null,
           hidden: wrap ? (wrap.style.display === "none") : null,
           rows: (typeof HIST_FILTERED !== "undefined" && HIST_FILTERED)
                 ? HIST_FILTERED.length : null,
           hasImg: (typeof HIST_FILTERS !== "undefined" && HIST_FILTERS)
                 ? HIST_FILTERS.hasImg : null };
}
setTimeout(async function () {
  var out = [];
  try {
    var sp = document.getElementById("bootSplash");
    if (sp) sp.style.display = "none";
    var vs = document.querySelectorAll(".view");
    for (var i = 0; i < vs.length; i++) vs[i].classList.remove("active");
    var hv = document.getElementById("view-history");
    if (hv) hv.classList.add("active");

    out.push(snap("A_initial"));                       // 读 HTML 的 checked 属性
    await switchHist("monitored"); out.push(snap("B_monitored"));
    await switchHist("forwards");  out.push(snap("C_forwards"));
    await switchHist("monitored"); out.push(snap("D_back"));
    clearHistFilters();            out.push(snap("E_cleared"));
    document.getElementById("hfHasImg").checked = false;
    applyHistFilter();             out.push(snap("F_unchecked"));
    document.title = "RESULT:" + JSON.stringify(out);
  } catch (e) {
    document.title = "JSERR:" + e.message + " | " + JSON.stringify(out);
  }
}, 500);
</script>
"""


def main():
    if not os.path.isfile(EDGE):
        print("找不到 Edge，跳过：%s" % EDGE)
        return 0
    tmp = tempfile.mkdtemp(prefix="hist_img_")
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

        cmd = [EDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
               "--window-size=1280,1100", "--virtual-time-budget=8000",
               "--dump-dom", "file:///" + page.replace("\\", "/")]
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", timeout=120)
        m = re.search(r"<title>(.*?)</title>", p.stdout or "", re.S)
        title = (m.group(1) if m else "").strip()
        if title.startswith("JSERR"):
            print("页面脚本报错：%s" % title[:400])
            return 1
        if not title.startswith("RESULT:"):
            print("未拿到 RESULT：%s" % title[:300])
            return 1
        out = json.loads(title[len("RESULT:"):])
        for s in out:
            print("  %-12s 勾选=%-5r 隐藏=%-5r 命中行=%-4r hasImg=%r"
                  % (s["tag"], s["checked"], s["hidden"], s["rows"], s["hasImg"]))
        d = {s["tag"]: s for s in out}
        checks = [
            ("A 初始即勾选",            d["A_initial"]["checked"] is True),
            ("B 进页面勾选+滤出2行",    d["B_monitored"]["checked"] is True
                                       and d["B_monitored"]["rows"] == 2),
            ("C 转发记录页不勾选+隐藏", d["C_forwards"]["checked"] is False
                                       and d["C_forwards"]["hidden"] is True),
            ("D 切回恢复勾选",          d["D_back"]["checked"] is True
                                       and d["D_back"]["rows"] == 2),
            ("E 清空筛选回默认开",      d["E_cleared"]["checked"] is True),
            ("F 取消后可看全部3行",     d["F_unchecked"]["checked"] is False
                                       and d["F_unchecked"]["rows"] == 3),
        ]
        bad = 0
        for name, ok in checks:
            print("    %s %s" % ("OK  " if ok else "FAIL", name))
            if not ok:
                bad += 1
        print("\n%s" % ("PASS" if bad == 0 else "FAIL（%d 项）" % bad))
        return 0 if bad == 0 else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
