# -*- coding: utf-8 -*-
"""v1.6.12 相册监控页视觉小样：确认卡片布局没被 content-visibility / contain 弄坏。

用真实缩略图（取缩略图缓存里的前几张，没有就现生成）填 6 张卡片，
切到「相册监控」页截一张图，并量三件事：
  · 预览框是否仍是 104×104 且在图内居中；
  · 卡片实际高度是否接近 contain-intrinsic-size 的估值（差太多会导致滚动条跳动）；
  · 图片是否真的带 width/height 属性（避免加载后跳版）。

输出：tests/_shots/media_page.png
"""
import base64
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

FINISH = """<script>
var THUMBS = window.__THUMBS;   // 若干真实缩略图的 data URL
var UIDS = %(uids)s;
setTimeout(function () {
  try {
    var sp = document.getElementById("bootSplash");
    if (sp) sp.style.display = "none";
    var vs = document.querySelectorAll(".view");
    for (var i = 0; i < vs.length; i++) vs[i].classList.remove("active");
    document.getElementById("view-media").classList.add("active");
    var list = UIDS.map(function (u, i) {
      return { uid: u, screen_name: "对象" + (i + 1) + "号", images: 120 + i,
               videos: 0, size: 3 * 1024 * 1024 };
    });
    renderMediaObjects(list);
    setTimeout(function () {
      var cards = document.querySelectorAll(".media-obj-card");
      var hs = [], thumbs = [];
      for (var i = 0; i < cards.length; i++) hs.push(Math.round(cards[i].getBoundingClientRect().height));
      var ts = document.querySelectorAll(".media-obj-thumb");
      for (var i = 0; i < ts.length; i++) {
        var r = ts[i].getBoundingClientRect();
        thumbs.push([Math.round(r.width), Math.round(r.height)]);
      }
      var imgs = document.querySelectorAll(".media-obj-thumb img");
      var firstImg = imgs[0];
      document.title = "RESULT:" + JSON.stringify({
        cards: cards.length, heights: hs, thumbs: thumbs.slice(0, 3),
        imgs: imgs.length,
        attrW: firstImg ? firstImg.getAttribute("width") : null,
        attrH: firstImg ? firstImg.getAttribute("height") : null,
        decoding: firstImg ? firstImg.getAttribute("decoding") : null
      });
    }, 900);
  } catch (e) { document.title = "JSERR:" + e.message; }
}, 400);
</script>
"""


def build_thumbs():
    """取缩略图缓存；没有就现场用 Pillow 生成几张。返回 (data urls, uids)。"""
    cache = os.path.join(os.environ.get("LOCALAPPDATA", ""), "微博bot小助手",
                         "data", "cache", "thumbs")
    data, uids = [], []
    if os.path.isdir(cache):
        names = sorted(os.listdir(cache))[:6]
        for i, n in enumerate(names):
            p = os.path.join(cache, n)
            with open(p, "rb") as f:
                data.append("data:image/jpeg;base64," + base64.b64encode(f.read()).decode())
            uids.append(str(700000 + i))
    if not data:
        from PIL import Image
        import io as _io
        for i in range(6):
            im = Image.new("RGB", (208, 208), (40 + i * 25, 90, 140))
            buf = _io.BytesIO()
            im.save(buf, "JPEG")
            data.append("data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode())
            uids.append(str(700000 + i))
    return data, uids


def main():
    if not os.path.isfile(EDGE):
        print("找不到 Edge，跳过：%s" % EDGE)
        return 0
    os.makedirs(OUT, exist_ok=True)
    thumbs, uids = build_thumbs()
    tmp = tempfile.mkdtemp(prefix="media_ui_")
    try:
        for f in ("style.css", "app.js", "logo.png"):
            src = os.path.join(UI, f)
            if os.path.isfile(src):
                shutil.copy(src, os.path.join(tmp, f))
        html = open(os.path.join(UI, "index.html"), encoding="utf-8").read()
        stub = ("""<script>
window.__THUMBS = %s;
window.__TI = 0;
window.pywebview = { api: new Proxy({}, { get: function (t, k) {
  return function () {
    if (k === "get_latest_media_thumb") {
      return Promise.resolve({ ok: true, file_path: "x/y.jpg", file_name: "y.jpg" });
    }
    if (k === "get_media_thumb_data") {
      var u = window.__THUMBS[window.__TI %% window.__THUMBS.length];
      window.__TI += 1;
      return Promise.resolve({ ok: true, data_url: u, thumb: true, size: 9000 });
    }
    return Promise.resolve(null);
  };
} }) };
</script>
""" % json.dumps(thumbs))
        tail = FINISH % {"uids": json.dumps(uids)}
        html = html.replace('<script src="app.js"></script>',
                            stub + '<script src="app.js"></script>' + tail, 1)
        page = os.path.join(tmp, "ui.html")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(html)

        shot = os.path.join(OUT, "media_page.png")
        if os.path.isfile(shot):
            os.remove(shot)
        cmd = [EDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
               "--window-size=1280,1000", "--virtual-time-budget=6000",
               "--screenshot=" + shot, "--dump-dom",
               "file:///" + page.replace("\\", "/")]
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", timeout=120)
        dom = p.stdout or ""
        m = re.search(r"<title>(.*?)</title>", dom, re.S)
        title = (m.group(1) if m else "").strip()
        if not title.startswith("RESULT:"):
            print("未拿到 RESULT：%s" % title[:200])
            return 1
        d = json.loads(title[len("RESULT:"):])
        print("卡片=%d 高度=%s" % (d["cards"], d["heights"]))
        print("预览框尺寸=%s 图片数=%d width=%s height=%s decoding=%s"
              % (d["thumbs"], d["imgs"], d["attrW"], d["attrH"], d["decoding"]))
        ok = (d["cards"] == 6 and d["imgs"] == 6
              and all(t == [104, 104] for t in d["thumbs"])
              and d["attrW"] == "104" and d["attrH"] == "104"
              and d["decoding"] == "async"
              and all(120 <= h <= 200 for h in d["heights"]))
        print("截图：%s（存在=%r）" % (shot, os.path.isfile(shot)))
        print("\n%s" % ("PASS" if ok else "FAIL"))
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
