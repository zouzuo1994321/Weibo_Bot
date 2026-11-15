#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""账户表差量更新实测（v1.6.11）。

`renderAccounts` 现在先拼 HTML 字符串，与上次相同就跳过赋值。风险是「该刷新时
不刷新」（例如字段变了但字符串没跟上），所以既要验证「不变就不重建 DOM」，
也要验证「一变就真的重建」。

判定方式：比较 `<tbody>` 下第一个 `<tr>` 的**对象身份**。差量生效时，第二次渲染
后该节点还是同一个对象；真正变化时必须是新对象。同时验证单元格文本确实更新。
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
  var ACCS = [
    {id:"a1", display_name:"账号甲", created_at:"2026-10-01 09:00:00",
     last_used:"2026-11-14 08:00:00", status:"ok", last_check:"2026-11-14 09:00:00",
     verify_result:"valid", last_verified_at:"2026-11-14 09:00:00",
     cookie_expiry:{has_expiry:true, expired:false, level:"safe",
                    remaining_text:"还剩 6 天", expiry_str:"2026-11-20 09:00:00"}},
    {id:"a2", display_name:"账号乙", created_at:"2026-10-02 09:00:00",
     last_used:"2026-11-13 08:00:00", status:"ok", last_check:"2026-11-14 09:00:00",
     verify_result:"", last_verified_at:"",
     cookie_expiry:{has_expiry:true, expired:false, level:"warn",
                    remaining_text:"还剩 1 天", expiry_str:"2026-11-15 09:00:00"}}
  ];
  var STATE = {
    loading: false,
    stats: { monitored_posts_today: 1, forwards_today: 1, forwards_total: 1,
             monitors: 1, accounts: 2, media_files: 0 },
    monitors: [{uid:"1", name:"对象A"}],
    accounts: ACCS,
    config: { forward_count: 1 },
    license: { licensed: true, type_name: "永久授权" },
    scheduler: { running: false, interval_minutes: 30, in_working_hours: true,
                 last_run: null, next_run: null, last_result: null },
    hot: { items: [], pinned: [] }
  };
  var ANS = {
    get_state: STATE,
    get_system_load: { cpu: 1, mem: 2, net_up: 0, net_down: 0, gpu: null,
                       gpu_temp: null, cpu_temp: null },
    get_account_health: { level: "ok", message: "正常", unhealthy: [] },
    get_hot_search: { items: [], pinned: [], updated_at: 0 },
    get_app_info: { version: "v1.6.11", internal: "2611140007" },
    list_personas: [], get_poll_progress: { running: false }
  };
  window.__STATE = STATE;
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

MEASURE = r"""
<script>
window.addEventListener("load", function () {
  setTimeout(function () {
    var out = {};
    try {
      var tb = document.getElementById("accList");

      // ---- 首次渲染 ----
      renderAccounts();
      var rows0 = tb.querySelectorAll("tr");
      var node0 = rows0[0];
      out.rows0 = rows0.length;

      // ---- 数据不变，再渲染一次 ----
      renderAccounts();
      var node1 = tb.querySelectorAll("tr")[0];
      out.sameNodeWhenUnchanged = (node1 === node0);

      // ---- 数据变了（改状态 + 有效期文案），必须重建 ----
      window.__STATE.accounts[0].status = "cookie_expired";
      window.__STATE.accounts[0].verify_result = "expired";
      window.__STATE.accounts[0].last_check = "2026-11-14 10:30:00";
      renderAccounts();
      var rows2 = tb.querySelectorAll("tr");
      var node2 = rows2[0];
      out.rebuiltWhenChanged = (node2 !== node0);
      out.rowCountOk = (rows2.length === out.rows0);
      out.textChanged = rows2[0].textContent.indexOf("10:30:00") >= 0
                        || rows2[0].textContent.indexOf("失效") >= 0;
      out.cellDump = rows2[0].textContent.replace(/\s+/g, " ").slice(0, 90);

      // ---- 账户清空：显示空态 ----
      window.__STATE.accounts = [];
      renderAccounts();
      var txt = tb.textContent || "";
      out.emptyShown = txt.indexOf("暂无账户") >= 0;
      out.emptyRows = tb.querySelectorAll("tr").length;

      // ---- 从空态恢复：必须重新画出两行 ----
      window.__STATE.accounts = [{id:"a9", display_name:"账号丙",
        created_at:"2026-10-03 09:00:00", last_used:"", status:"ok",
        last_check:"", verify_result:"", last_verified_at:"",
        cookie_expiry:{has_expiry:false}}];
      renderAccounts();
      out.restoredRows = tb.querySelectorAll("tr").length;
      out.restoredName = (tb.textContent || "").indexOf("账号丙") >= 0;
    } catch (e) {
      out.err = String(e);
    }
    document.title = "ANS:" + JSON.stringify(out);
  }, 1800);
});
</script>
"""


def build(tmp):
    for name in ("style.css", "index.html", "app.js"):
        shutil.copy2(os.path.join(UI, name), os.path.join(tmp, name))
    p = os.path.join(tmp, "index.html")
    html = open(p, encoding="utf-8").read()
    html = html.replace('<script src="app.js"', FAKE + '<script src="app.js"', 1)
    html = html.replace("</body>", MEASURE + "</body>", 1)
    open(p, "w", encoding="utf-8").write(html)


def run(tmp):
    out = subprocess.run(
        [EDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--virtual-time-budget=6000", "--window-size=1280,900", "--dump-dom",
         "file:///" + os.path.join(tmp, "index.html").replace("\\", "/")],
        capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="ignore")
    dom = out.stdout or ""
    m = re.search(r"ANS:(\{.*?\})</title>", dom)
    if not m:
        m = re.search(r"ANS:(\{.*?\})", dom)
    if not m:
        return None, dom[:400]
    return json.loads(m.group(1).replace("&quot;", '"')), ""


def main():
    ok, bad = [], []

    def chk(name, cond, extra=""):
        (ok if cond else bad).append(name)
        print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                              ("  · " + extra) if extra else ""))

    print("=" * 74)
    print("账户表差量更新实测")
    print("=" * 74)

    tmp = tempfile.mkdtemp(prefix="wb_diff_")
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

    chk("首次渲染出 2 行", r.get("rows0") == 2, "rows=%r" % r.get("rows0"))
    chk("数据未变 → 不重建 DOM（节点身份不变）",
        r.get("sameNodeWhenUnchanged") is True,
        "sameNode=%r" % r.get("sameNodeWhenUnchanged"))
    chk("数据变化 → 必须重建 DOM（节点身份改变）",
        r.get("rebuiltWhenChanged") is True,
        "rebuilt=%r" % r.get("rebuiltWhenChanged"))
    chk("重建后行数正确", r.get("rowCountOk") is True, "")
    chk("重建后单元格文本确实更新",
        r.get("textChanged") is True, "首行=「%s」" % (r.get("cellDump") or ""))
    chk("清空账户 → 显示空态",
        r.get("emptyShown") is True and r.get("emptyRows") == 1,
        "空态=%r 行数=%r" % (r.get("emptyShown"), r.get("emptyRows")))
    chk("从空态恢复 → 正常重新渲染",
        r.get("restoredRows") == 1 and r.get("restoredName") is True,
        "行数=%r 命中账号名=%r"
        % (r.get("restoredRows"), r.get("restoredName")))

    print("=" * 74)
    print("通过 %d 项 / 失败 %d 项" % (len(ok), len(bad)))
    for b in bad:
        print("   FAIL:", b)
    print("=" * 74)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
