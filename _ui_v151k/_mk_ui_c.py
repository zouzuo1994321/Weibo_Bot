# -*- coding: utf-8 -*-
"""从 _ui_v117b 生成 _ui_v117c，并补齐无边框窗口层（标题栏/拖动/控件/磁吸落点）。
所有插入均使用真实换行，避免重蹈 /n 字面量覆辙。
"""
import os, shutil

BASE = "Z:/【01】自研软件/【26-11】微博bot小助手（正式版）"
SRC = os.path.join(BASE, "_ui_v117b")
DST = os.path.join(BASE, "_ui_v117c")
if os.path.exists(DST):
    print("DST exists, abort")
    raise SystemExit(1)
shutil.copytree(SRC, DST)

def patch_file(rel, old, new, times=1):
    p = os.path.join(DST, rel)
    t = open(p, encoding="utf-8").read()
    assert t.count(old) == times, f"[{rel}] anchor count={t.count(old)} expected={times}\nANCHOR: {old!r}"
    t = t.replace(old, new)
    # sanity: no literal /n defect introduced
    assert t.count("/n") == 0, f"[{rel}] stray /n introduced: {t.count('/n')}"
    open(p, "w", encoding="utf-8").write(t)
    print(f"patched {rel}")

# ---------------- index.html ----------------
APP_OPEN = '<div id="app" data-page-node-id="Nr2Ww3z5dZVHijyfflfzjJ">'
TITLEBAR = (APP_OPEN + "\n"
    "  <!-- 自定义标题栏（无边框窗口）：拖动区 + 最小化/最大化/关闭/托盘 -->\n"
    '  <div id="titlebar">\n'
    '    <div class="titlebar-drag" id="tbDrag">\n'
    '      <img src="logo.png" class="titlebar-logo" alt="logo">\n'
    '      <span class="titlebar-text">微博bot小助手</span>\n'
    "    </div>\n"
    '    <div class="window-controls">\n'
    '      <div class="wbtn wbtn-tray" id="btnTray" title="最小化到托盘"></div>\n'
    '      <div class="wbtn wbtn-min" id="btnMin" title="最小化"></div>\n'
    '      <div class="wbtn wbtn-max" id="btnMax" title="最大化 / 还原"></div>\n'
    '      <div class="wbtn wbtn-close" id="btnClose" title="关闭"></div>\n'
    "    </div>\n"
    "  </div>\n"
    '  <div class="app-body">')
patch_file("index.html", APP_OPEN, TITLEBAR)

# close app-body after </main>
patch_file("index.html", "</main>", "</main>\n  </div>")

# ---------------- style.css ----------------
# #app -> column
patch_file("style.css",
           "#app { display: flex; flex: 1; overflow: hidden; }",
           "#app { display: flex; flex-direction: column; flex: 1; overflow: hidden; }")
# add .app-body after the #app rule
patch_file("style.css",
           "#app { display: flex; flex-direction: column; flex: 1; overflow: hidden; }",
           "#app { display: flex; flex-direction: column; flex: 1; overflow: hidden; }\n"
           ".app-body { display: flex; flex: 1; overflow: hidden; min-height: 0; }", times=1)
# add tray button style after .wbtn-close rule
patch_file("style.css",
           ".wbtn-close { background: #ff5f56; }",
           ".wbtn-close { background: #ff5f56; }\n"
           ".wbtn-tray { background: var(--accent-2); }\n"
           '.wbtn-tray::before { content: "\\25be"; }')

# ---------------- app.js ----------------
# call bindWindowControls in init
patch_file("app.js", "  bindNav();", "  bindNav();\n  bindWindowControls();", times=1)
# inject the window-control + drag logic before the startup block
WBLOCK = (
    "/* ---------------- 无边框窗口：标题栏拖动 + 控件 + 边缘磁吸 ----------------\n"
    "function bindWindowControls() {\n"
    '  const tb = document.getElementById("titlebar");\n'
    '  const btnMin = document.getElementById("btnMin");\n'
    '  const btnMax = document.getElementById("btnMax");\n'
    '  const btnClose = document.getElementById("btnClose");\n'
    '  const btnTray = document.getElementById("btnTray");\n'
    '  if (btnMin) btnMin.onclick = () => apiCall("window_minimize");\n'
    '  if (btnMax) btnMax.onclick = () => apiCall("window_toggle_maximize");\n'
    '  if (btnClose) btnClose.onclick = () => apiCall("window_close");\n'
    '  if (btnTray) btnTray.onclick = () => apiCall("window_to_tray");\n'
    "  // 标题栏拖动：mousedown 取得窗口坐标，mousemove 调后端 window_move；贴近屏幕边缘时磁吸\n"
    "  let drag = null;\n"
    "  async function onDown(e) {\n"
    "    if (e.button !== 0) return;\n"
    '    if (e.target.closest(".wbtn")) return;   // 点按钮不触发拖动\n'
    '    const pos = await apiCall("get_window_pos");\n'
    "    if (!pos || !pos.ok) return;\n"
    "    drag = { sx: e.clientX, sy: e.clientY, wx: pos.x, wy: pos.y, w: pos.width, h: pos.height };\n"
    '    document.addEventListener("mousemove", onMove);\n'
    '    document.addEventListener("mouseup", onUp);\n'
    "  }\n"
    "  function onMove(e) {\n"
    "    if (!drag) return;\n"
    "    let nx = drag.wx + (e.clientX - drag.sx);\n"
    "    let ny = drag.wy + (e.clientY - drag.sy);\n"
    "    const SNAP = 16, SW = window.screen.availWidth, SH = window.screen.availHeight;\n"
    "    if (nx <= SNAP) nx = 0;                                  // 左边缘磁吸\n"
    "    else if (nx + drag.w - SW >= -SNAP) nx = SW - drag.w;     // 右边缘磁吸\n"
    "    if (ny <= SNAP) ny = 0;                                  // 顶部磁吸\n"
    '    apiCall("window_move", nx, ny);\n'
    "  }\n"
    "  function onUp() {\n"
    "    drag = null;\n"
    '    document.removeEventListener("mousemove", onMove);\n'
    '    document.removeEventListener("mouseup", onUp);\n'
    "  }\n"
    "  if (tb) {\n"
    '    tb.addEventListener("mousedown", onDown);\n'
    '    tb.addEventListener("dblclick", (e) => { if (!e.target.closest(".wbtn")) apiCall("window_toggle_maximize"); });\n'
    "  }\n"
    "}\n\n"
)
patch_file("app.js",
           "/* ---------------- 启动 ---------------- */",
           WBLOCK + "/* ---------------- 启动 ---------------- */", times=1)

print("ALL UI PATCHES APPLIED OK")
