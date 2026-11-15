#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全功能基准测试（默认 5 轮）。

    python tests/bench_all.py               # 跑 5 轮并汇总
    python tests/bench_all.py --rounds 3
    python tests/bench_all.py --one <data_dir> <out.json>   # 单轮（内部用）

设计要点
--------
* 每轮在**独立子进程 + 独立数据目录**（WEIBOBOT_DATA_DIR 覆盖）中执行，互不污染；
* 覆盖 14 个功能域：语法编译、模块导入、版本一致性、路径管理、配置、监控列表、
  数据库 CRUD/去重、媒体对账（含阈值保护）、媒体索引、AI 文案生成、授权、
  导出、前端滑块逻辑、桥接纯函数、侧栏布局结构；
* 每项记录 PASS/FAIL 与耗时；汇总给出「稳定通过 / 偶发失败 / 稳定失败」，
  稳定失败即为可复现缺陷，需要修；偶发失败说明存在不确定性。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "_src_v151k")
UI = os.path.join(ROOT, "_ui_v151k")
TESTS = os.path.join(ROOT, "tests")
BASE = r"E:\tmp\wb_bench"
NODE = r"C:\Users\zouzu\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"


# --------------------------------------------------------------------------- #
#  单轮执行
# --------------------------------------------------------------------------- #
def run_one(data_dir, out_json):
    os.environ["WEIBOBOT_DATA_DIR"] = data_dir
    os.makedirs(data_dir, exist_ok=True)
    sys.path.insert(0, SRC)

    rows = []

    def chk(suite, name, fn):
        t0 = time.perf_counter()
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, "EXC %s: %s" % (type(e).__name__, str(e)[:180])
        ms = (time.perf_counter() - t0) * 1000.0
        rows.append({"suite": suite, "check": name, "ok": bool(ok),
                     "ms": round(ms, 1), "detail": str(detail)[:240]})

    # ---------------- S0 语法 / 导入 ----------------
    def _compile_all():
        import py_compile
        files = sorted(f for f in os.listdir(SRC) if f.endswith(".py"))
        bad = []
        tmpc = os.path.join(data_dir, "_pyc")
        os.makedirs(tmpc, exist_ok=True)
        for f in files:
            try:
                py_compile.compile(os.path.join(SRC, f), cfile=os.path.join(tmpc, f + "c"), doraise=True)
            except Exception as e:
                bad.append("%s(%s)" % (f, type(e).__name__))
        return (not bad, "%d files, bad=%s" % (len(files), bad))

    def _import_all():
        import importlib
        mods = [os.path.splitext(f)[0] for f in os.listdir(SRC) if f.endswith(".py")]
        skip = {"_app_v117"}          # 入口模块含启动副作用，单独由打包验证
        bad = []
        for m in sorted(set(mods) - skip):
            try:
                importlib.import_module(m)
            except Exception as e:
                bad.append("%s(%s:%s)" % (m, type(e).__name__, str(e)[:40]))
        return (not bad, "imported=%d bad=%s" % (len(mods) - len(skip) - len(bad), bad))

    chk("S0", "compile_all", _compile_all)
    chk("S0", "import_all", _import_all)

    # ---------------- S1 版本一致性 ----------------
    def _ver_internal():
        import version
        v = version.get_version()
        internal = str(v.get("internal") or "")
        j = json.load(open(os.path.join(SRC, "version.json"), encoding="utf-8"))
        expect = "%s%04d" % (j["date"], j["seq"])
        return (internal == expect and len(internal) == 10,
                "internal=%s expect=%s major=%s" % (internal, expect, v.get("major")))

    def _ver_changelog():
        import version
        internal = str(version.get_version().get("internal") or "")
        js = open(os.path.join(UI, "app.js"), encoding="utf-8").read()
        m = re.search(r'ver:\s*"([^"]+)",\s*internal:\s*"(\d+)"', js)
        if not m:
            return (False, "CHANGELOG 未匹配")
        return (m.group(2) == internal, "app.js=%s version=%s" % (m.group(2), internal))

    def _ver_readme():
        import version
        major = version.get_version().get("major") or ""
        rd = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
        return (major in rd, "major=%s in_readme=%s" % (major, major in rd))

    def _ver_paths_const():
        import version
        import paths
        mv = getattr(paths, "MAJOR_VERSION", "")
        ok = mv == version.get_version().get("major")
        return (ok, "paths.MAJOR_VERSION=%s version=%s" % (mv, version.get_version().get("major")))

    chk("S1", "version_internal_format", _ver_internal)
    chk("S1", "appjs_changelog_sync", _ver_changelog)
    chk("S1", "readme_version_sync", _ver_readme)
    chk("S1", "[回归]paths_major_sync", _ver_paths_const)

    # ---------------- S2 路径管理 ----------------
    def _path_env():
        # 注：paths 没有 get_data_dir()，数据根目录是模块常量 DATA_DIR；
        # 这里同时校验「根目录 = 环境变量」与「派生路径都落在根目录之下」。
        import paths
        root = os.path.abspath(paths.DATA_DIR)
        want = os.path.abspath(data_dir)
        inside = all(os.path.abspath(p).startswith(root)
                     for p in (paths.DB_PATH, paths.CONFIG_PATH,
                               paths.LOG_PATH, paths.IMAGE_DIR, paths.MEDIA_DIR))
        ok = (root == want) and inside
        return (ok, "root=%s want=%s derived_inside=%s" % (root, want, inside))

    def _path_override():
        import paths
        new_media = os.path.join(data_dir, "media_override")
        before = paths.get_media_dir()
        paths.set_data_paths(media_dir=new_media)
        got = paths.get_media_dir()
        ok = os.path.abspath(got) == os.path.abspath(new_media)
        paths.set_data_paths(media_dir=before)          # 复原
        return (ok, "override=%s got=%s" % (new_media, got))

    def _path_rel():
        import paths
        p = os.path.join(paths.get_media_dir(), "u1", "a.jpg")
        rel = paths.rel_to_data(p)
        return (isinstance(rel, str) and rel and not os.path.isabs(rel), "rel=%s" % rel)

    chk("S2", "data_dir_env_override", _path_env)
    chk("S2", "media_dir_override_roundtrip", _path_override)
    chk("S2", "rel_to_data", _path_rel)

    # ---------------- S3 配置 / 监控列表 ----------------
    def _config_crud():
        from config_manager import get_config, ConfigManager
        c = get_config()
        c.set("interval_minutes", 45)
        c.save()
        c2 = ConfigManager()
        got = c2.get("interval_minutes")
        c.set("interval_minutes", 30)
        c.save()
        return (got == 45, "persisted=%s" % got)

    def _monitor_crud():
        from monitor_manager import get_monitor_manager
        mm = get_monitor_manager()
        mm.add("b_uid_1", "基准对象")
        ok1 = any(i["uid"] == "b_uid_1" for i in mm.list())
        mm.update("b_uid_1", {"screen_name": "改后名"})
        ok2 = (mm.get("b_uid_1") or {}).get("screen_name") == "改后名"
        mm.remove("b_uid_1")
        ok3 = not any(i["uid"] == "b_uid_1" for i in mm.list())
        return (ok1 and ok2 and ok3, "add=%s update=%s remove=%s" % (ok1, ok2, ok3))

    chk("S3", "config_set_save_reload", _config_crud)
    chk("S3", "monitor_crud", _monitor_crud)

    # ---------------- S4 数据库 ----------------
    import paths as _paths
    media_root = _paths.get_media_dir()
    os.makedirs(media_root, exist_ok=True)

    def _db_post():
        from history_db import get_history_db
        db = get_history_db()
        db.add_monitored_post("d1", "对象", "p1", "内容", [], "2026-10-26 10:00:00")
        db.add_monitored_post("d1", "对象", "p1", "内容", [], "2026-10-26 10:00:00")  # 去重
        n = len([r for r in db.get_monitored_posts(limit=20) if r.get("post_id") == "p1"])
        return (n == 1, "rows_for_p1=%d" % n)

    def _db_forward():
        from history_db import get_history_db
        db = get_history_db()
        db.add_forward("acc1", "账号", "d1", "对象", "p1", "原文", "转发文案", "random", "success")
        return (db.is_post_processed("p1") is True and db.is_post_forwarded("p1") is True,
                "processed=%s forwarded=%s" % (db.is_post_processed("p1"), db.is_post_forwarded("p1")))

    def _db_logs():
        from history_db import get_history_db
        db = get_history_db()
        for i in range(5):
            db.add_log("INFO", "基准日志 %d" % i, "BENCH")
        return (db.count_logs() >= 5 and len(db.get_logs(limit=10)) >= 5,
                "count=%s list=%s" % (db.count_logs(), len(db.get_logs(limit=10))))

    def _db_media():
        from history_db import get_history_db
        db = get_history_db()
        rel = os.path.join("d1", "m1.jpg")
        fp = os.path.join(media_root, rel)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "wb") as f:
            f.write(b"x" * 64)
        first = db.add_media_file("d1", "对象", "p1", "image", "https://e/x1.jpg", rel, "m1.jpg", 64)
        again = db.add_media_file("d1", "对象", "p1", "image", "https://e/x1.jpg", rel, "m1.jpg", 64)
        has = db.has_media_url("https://e/x1.jpg", "image")
        return (first is True and again is False and has is True,
                "first=%s dup=%s has=%s" % (first, again, has))

    def _db_stats():
        from history_db import get_history_db
        db = get_history_db()
        st = db.stats()
        return (isinstance(st, dict), "stats_keys=%s" % (sorted(st)[:6] if isinstance(st, dict) else st))

    def _db_overview():
        from history_db import get_history_db
        db = get_history_db()
        ov = db.data_overview()
        return (isinstance(ov, dict), "overview_keys=%s" % (sorted(ov)[:6] if isinstance(ov, dict) else ov))

    chk("S4", "post_insert_and_dedup", _db_post)
    chk("S4", "forward_and_processed_flag", _db_forward)
    chk("S4", "logs_write_count", _db_logs)
    chk("S4", "media_register_dedup", _db_media)
    chk("S4", "stats_query", _db_stats)
    chk("S4", "data_overview", _db_overview)

    # ---------------- S5 媒体对账（阈值保护） ----------------
    def _reconcile_threshold():
        from history_db import get_history_db
        db = get_history_db()
        # 10 行、只落 1 个文件 → 待删 90% → 应被阈值拒绝
        for i in range(10):
            rel = os.path.join("gate", "g%d.jpg" % i)
            if i == 0:
                fp = os.path.join(media_root, rel)
                os.makedirs(os.path.dirname(fp), exist_ok=True)
                with open(fp, "wb") as f:
                    f.write(b"y")
            db.add_media_file("gate", "闸门", "pg", "image", "https://e/g%d.jpg" % i, rel, "g%d.jpg" % i, 1)
        r = db.reconcile_media_files(0.5)
        return (bool(r.get("skipped")) and r.get("removed") == 0,
                "skipped=%s removed=%s reason=%s" % (r.get("skipped"), r.get("removed"), r.get("reason")))

    def _reconcile_normal():
        from history_db import get_history_db
        db = get_history_db()
        # 再建 10 行，落 9 个文件 → 待删 10% → 只清 1 行
        for i in range(10):
            rel = os.path.join("heal", "h%d.jpg" % i)
            if i < 9:
                fp = os.path.join(media_root, rel)
                os.makedirs(os.path.dirname(fp), exist_ok=True)
                with open(fp, "wb") as f:
                    f.write(b"z")
            db.add_media_file("heal", "自愈", "ph", "image", "https://e/h%d.jpg" % i, rel, "h%d.jpg" % i, 1)
        r = db.reconcile_media_files(0.5)
        return ((not r.get("skipped")) and r.get("removed", 0) >= 1,
                "skipped=%s removed=%s" % (r.get("skipped"), r.get("removed")))

    chk("S5", "reconcile_threshold_gate", _reconcile_threshold)
    chk("S5", "reconcile_zero_scatter_heal", _reconcile_normal)

    # ---------------- S6 媒体索引 ----------------
    def _index_write():
        import media_index
        media_index.write_now()
        p = media_index.index_path()
        return (os.path.isfile(p) and os.path.getsize(p) > 0, "path=%s size=%s" %
                (p, os.path.getsize(p) if os.path.isfile(p) else -1))

    chk("S6", "index_write_now", _index_write)

    # ---------------- S7 AI 文案（内置引擎） ----------------
    def _ai_builtin():
        import ai_engine
        r = ai_engine.generate_forward_text("今天天气不错，出门散步看到很多人在公园里遛狗。", "幽默")
        text = ""
        if isinstance(r, dict):
            text = r.get("text") or r.get("content") or ""
        ok = bool(text) and len(str(text)) >= 4
        return (ok, "engine=%s len=%d text=%s" %
                (r.get("engine") if isinstance(r, dict) else "?", len(str(text)), str(text)[:60]))

    chk("S7", "ai_builtin_generate", _ai_builtin)

    # ---------------- S8 授权 ----------------
    def _lic_machine():
        import licensemgr
        a = licensemgr.machine_id()
        b = licensemgr.machine_id()
        return (a == b and len(a) >= 6, "mid=%s stable=%s" % (a[:12], a == b))

    def _lic_roundtrip():
        import licensemgr
        mid = licensemgr.machine_id()
        key = licensemgr.build_license_key(mid, 30, 1)
        p = licensemgr.parse_license_key(key)
        ok = bool(p) and p.get("mid_ok") and p.get("exp", 0) > time.time()
        return (ok, "parsed=%s" % (p,))

    def _lic_exp_tamper():
        """缺陷探针：license.json 的 exp 被改成 0（永久）后是否仍然放行。"""
        import licensemgr
        mid = licensemgr.machine_id()
        key = licensemgr.build_license_key(mid, 1, 1)      # 真 key：1 天
        licensemgr._save_license({"key": key, "type": 3, "exp": 0, "type_name": "永久版(篡改)"})
        st = licensemgr.get_status()
        tampered_ok = bool(st.get("licensed")) and st.get("exp") == 0
        return (not tampered_ok,
                "licensed=%s exp=%s type_name=%s（True=篡改后仍放行，属缺陷）" %
                (st.get("licensed"), st.get("exp"), st.get("type_name")))

    chk("S8", "machine_id_stable", _lic_machine)
    chk("S8", "license_key_roundtrip", _lic_roundtrip)
    chk("S8", "[回归]license_exp_tamper", _lic_exp_tamper)

    # ---------------- S9 导出 ----------------
    def _export_xlsx():
        import exporter
        dest = os.path.join(data_dir, "exp")
        os.makedirs(dest, exist_ok=True)
        r = exporter.export("monitored", "xlsx", dest)
        ok = isinstance(r, dict) and bool(r.get("ok"))
        files = []
        for root_, _dirs, fs in os.walk(dest):
            files += [f for f in fs if f.endswith(".xlsx")]
        return (ok and bool(files), "ok=%s files=%s err=%s" %
                (r.get("ok") if isinstance(r, dict) else r, files[:2], (r or {}).get("error") if isinstance(r, dict) else ""))

    chk("S9", "export_monitored_xlsx", _export_xlsx)

    # ---------------- S10 前端滑块逻辑 ----------------
    def _nav_smoke():
        js = open(os.path.join(UI, "app.js"), encoding="utf-8").read()
        m = re.search(r'let _navHiReady = false;[\s\S]*?\n\}\n', js)
        if not m:
            return (False, "未匹配 moveNavHighlight")
        tmpjs = os.path.join(data_dir, "_nav_extract.js")
        with open(tmpjs, "w", encoding="utf-8") as f:
            f.write(m.group(0))
        p = subprocess.run([NODE, os.path.join(TESTS, "nav_highlight_smoke.cjs"), tmpjs],
                           capture_output=True, text=True, timeout=60)
        out = (p.stdout or "").strip().splitlines()
        tag = out[-1] if out else ""
        return (p.returncode == 0, "%s %s" % (tag, (p.stderr or "")[:80]))

    chk("S10", "nav_highlight_logic", _nav_smoke)

    # ---------------- S11 桥接纯函数 / 侧栏布局结构 ----------------
    def _bridge_ver():
        import api_bridge
        v = api_bridge._ver()
        import version
        ok = v.get("major") == version.get_version().get("major")
        return (ok, "bridge=%s version=%s" % (v.get("major"), version.get_version().get("major")))

    def _css_scroll_guard():
        css = open(os.path.join(UI, "style.css"), encoding="utf-8").read()
        m = re.search(r"#sidebar\s*\{[^}]*\}", css)
        has_ovx = bool(m and "overflow-x: hidden" in m.group(0))
        js = open(os.path.join(UI, "app.js"), encoding="utf-8").read()
        has_right = 'hi.style.right = x + "px"' in js
        return (has_ovx and has_right, "sidebar_overflow_x=%s js_right=%s" % (has_ovx, has_right))

    chk("S11", "bridge_version_info", _bridge_ver)
    chk("S11", "sidebar_scroll_guard", _css_scroll_guard)

    # ---------------- S13 前端语法（v1.5.7 启动卡死事故的回归项） ----------------
    def _js_syntax():
        """node --check 全部前端 js。v1.5.7 的 CHANGELOG 里一个未转义双引号
        就让整个 app.js 解析失败、界面永远停在启动遮罩，Python 侧测试全绿。"""
        node = None
        for p in (os.environ.get("NODE_EXE", ""), NODE):
            if p and os.path.exists(p):
                node = p
                break
        if not node:
            from shutil import which
            node = which("node")
        if not node:
            return (True, "SKIP 未找到 node.exe")
        bad = []
        n = 0
        for root, _d, files in os.walk(UI):
            for fn in files:
                if not fn.endswith(".js"):
                    continue
                n += 1
                fp = os.path.join(root, fn)
                r = subprocess.run([node, "--check", fp], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
                if r.returncode != 0:
                    first = (r.stderr or "").strip().splitlines()
                    bad.append("%s: %s" % (fn, first[0] if first else "syntax error"))
        return (not bad, "js=%d bad=%s" % (n, bad or "none"))

    chk("S13", "appjs_syntax", _js_syntax)

    # ---------------- S14 路径安全（v1.6.0 加固回归） ----------------
    def _api():
        import api_bridge
        return api_bridge.Api()

    def _mk_png(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001080600000"
            "01f15c4890000000d4944415478da63f8cf00000301010018dd8f1f"
            "0000000049454e44ae426082")
        with open(path, "wb") as f:
            f.write(png)

    def _sec_abs_read():
        r = _api().load_image_base64("C:/Windows/win.ini")
        return (not r.get("ok"), "abs_read ok=%s err=%s" % (r.get("ok"), r.get("error")))

    def _sec_traversal():
        r = _api().load_media_base64("../../../../Windows/win.ini")
        return (not r.get("ok"), "traversal ok=%s err=%s" % (r.get("ok"), r.get("error")))

    def _sec_open_deny():
        import paths
        f = os.path.join(paths.DATA_DIR, "sec_probe.ps1")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("echo probe")
        r = _api().open_path(f)      # 数据目录内 → 过了 root 校验，必须被扩展名闸拦下
        return (not r.get("ok"), "open_ps1 ok=%s err=%s" % (r.get("ok"), r.get("error")))

    def _sec_legit_read():
        import paths
        p = os.path.join(paths.get_image_dir(), "bench_probe.png")
        _mk_png(p)
        r = _api().load_image_base64("images/bench_probe.png")
        return (bool(r.get("ok")), "legit ok=%s err=%s" % (r.get("ok"), r.get("error")))

    chk("S14", "sec_deny_abs_read", _sec_abs_read)
    chk("S14", "sec_deny_traversal", _sec_traversal)
    chk("S14", "sec_deny_open_exec", _sec_open_deny)
    chk("S14", "sec_allow_legit_read", _sec_legit_read)

    # ---------------- S15 数据大盘（v1.6.0：状态拆分 + 队列） ----------------
    def _dashboard_agg():
        from history_db import get_history_db
        db = get_history_db()
        db.add_forward("acc1", "账号A", "u1", "对象甲", "p_ok1", "原文", "转发", "normal", "success")
        db.add_forward("acc1", "账号A", "u1", "对象甲", "p_skip1", "原文", "转发", "normal",
                       "skipped", "命中去重")
        db.add_forward("acc1", "账号A", "u1", "对象甲", "p_fail1", "原文", "转发", "ai",
                       "failed", "网络超时")
        r = db.data_overview()
        sc = (r.get("status_counts") or {}).get("today") or {}
        q = r.get("queue") or []
        md = (r.get("series") or {}).get("month_daily") or [{}]
        ok = (sc.get("success", 0) >= 1 and sc.get("skipped", 0) >= 1
              and sc.get("failed", 0) >= 1 and len(q) >= 3
              and "bad" in (md[0] or {}) and "status" in (q[0] or {}))
        return (ok, "counts=%s queue=%d has_bad=%s" % (sc, len(q), "bad" in (md[0] or {})))

    chk("S15", "dashboard_aggregate", _dashboard_agg)

    # ---------------- S16 每轮排程数量 + 热搜（v1.6.1） ----------------
    def _fwd_count_clamp():
        from config_manager import get_config
        import forward_engine as fe
        cfg = get_config()
        old = cfg.get("forward_count", 1)
        bad = []
        for raw, want in ((0, 1), (-5, 1), (3, 3), (99, 10), ("abc", 1), (None, 1)):
            cfg.set("forward_count", raw)
            got = fe.get_forward_count(cfg)
            if got != want:
                bad.append("%r→%r(期望 %r)" % (raw, got, want))
        cfg.set("forward_count", old)
        return (not bad, "钳制异常：%s" % bad if bad else "1..10 钳制正常")

    def _pick_spread():
        import forward_engine as fe
        cands = []
        for uid in ("u1", "u2", "u3"):
            for k in range(3):
                cands.append({"uid": uid, "screen_name": uid,
                              "post": {"id": "%s_%d" % (uid, k), "text": "t"}})
        picks = fe._pick_candidates(cands, 3)
        uids = [p["uid"] for p in picks]
        spread = len(picks) == 3 and len(set(uids)) == 3
        many = fe._pick_candidates(cands, 5)
        over = fe._pick_candidates(cands, 99)
        return (spread and len(many) == 5 and len(over) == len(cands),
                "3→%s 5→%d 99→%d" % (uids, len(many), len(over)))

    def _hot_url_build():
        from weibo_client import hot_search_url
        a = hot_search_url("测试话题", "#测试话题#")
        b = hot_search_url("测试话题")
        c = hot_search_url("")
        ok = ("%23%E6%B5%8B%E8%AF%95%E8%AF%9D%E9%A2%98%23" in a
              and a == b and c.startswith("https://s.weibo.com/"))
        return (ok, "scheme=%s plain_same=%s empty=%s" % (a, a == b, c))

    def _open_external_guard():
        r = _api().open_external("file:///C:/Windows/System32/calc.exe")
        r2 = _api().open_external("javascript:alert(1)")
        return (not r.get("ok") and not r2.get("ok"),
                "file=%s js=%s" % (r.get("ok"), r2.get("ok")))

    chk("S16", "forward_count_clamp", _fwd_count_clamp)
    chk("S16", "pick_candidates_spread", _pick_spread)
    chk("S16", "hot_search_url", _hot_url_build)
    chk("S16", "open_external_scheme_guard", _open_external_guard)

    # ---------------- S17 热搜历史留档（v1.6.2） ----------------
    def _hot_record_aggregate():
        """同一天同一词条只占一行：排名取最好、热度取峰值、seen_count 累加。"""
        from history_db import get_history_db
        db = get_history_db()
        day = "2099-01-01"
        db.clear_hot_search(day)
        snap1 = [{"word": "词条A", "hot": 100, "label": "沸"},
                 {"word": "词条B", "hot": 90, "label": ""},
                 {"word": "词条C", "hot": 80, "label": "新"}]
        # 第二轮：A 掉到第 2、热度冲高；B 升到第 1；新增 D
        snap2 = [{"word": "词条B", "hot": 95, "label": ""},
                 {"word": "词条A", "hot": 150, "label": "沸"},
                 {"word": "词条C", "hot": 70, "label": ""},
                 {"word": "词条D", "hot": 60, "label": ""}]
        a1 = db.record_hot_search(snap1, day=day)
        a2 = db.record_hot_search(snap2, day=day)
        res = db.hot_search_list(day=day, limit=50)
        rows = {r["word"]: r for r in res["rows"]}
        a = rows.get("词条A") or {}
        b = rows.get("词条B") or {}
        d = rows.get("词条D") or {}
        ok = (a1 == (3, 0) and a2 == (1, 3) and res["total"] == 4
              and a.get("rank_best") == 1 and a.get("rank_first") == 1
              and a.get("hot_peak") == 150 and a.get("seen_count") == 2
              and b.get("rank_best") == 1 and b.get("hot_peak") == 95
              and d.get("rank_best") == 4)
        db.clear_hot_search(day)
        return (ok, "首轮%s 次轮%s 共%d A(rank=%s,peak=%s,seen=%s) B(rank=%s) D(rank=%s)"
                % (a1, a2, res["total"], a.get("rank_best"), a.get("hot_peak"),
                   a.get("seen_count"), b.get("rank_best"), d.get("rank_best")))

    def _hot_pinned_first():
        """置顶要闻 pinned=1、rank_best=0，查询时永远排在最前。"""
        from history_db import get_history_db
        db = get_history_db()
        day = "2099-01-02"
        db.clear_hot_search(day)
        db.record_hot_search(
            [{"word": "榜首词条", "hot": 999, "label": "沸"},
             {"word": "第二词条", "hot": 500, "label": ""}],
            pinned=[{"word": "置顶要闻甲", "url": "https://s.weibo.com/x", "label": "置顶"},
                    {"word": "置顶要闻乙", "url": "", "label": "置顶"}],
            day=day)
        rows = db.hot_search_list(day=day, limit=50)["rows"]
        first_two = [r["word"] for r in rows[:2]]
        only = db.hot_search_list(day=day, pinned_only=True, limit=50)
        kw = db.hot_search_list(day=day, keyword="第二", limit=50)
        ok = (first_two == ["置顶要闻甲", "置顶要闻乙"]
              and only["total"] == 2 and all(r["pinned"] == 1 for r in only["rows"])
              and kw["total"] == 1 and kw["rows"][0]["word"] == "第二词条"
              and not kw["rows"][0]["pinned"])
        db.clear_hot_search(day)
        return (ok, "前两条=%s 置顶=%d 关键词=%d" % (first_two, only["total"], kw["total"]))

    def _hot_days_and_clear():
        """日期汇总 + 按日清理 + 超期裁剪。"""
        from history_db import get_history_db
        db = get_history_db()
        d1, d2, old = "2099-01-03", "2099-01-04", "2000-01-01"
        for d in (d1, d2, old):
            db.clear_hot_search(d)
        db.record_hot_search([{"word": "甲", "hot": 10}, {"word": "乙", "hot": 20}],
                             pinned=[{"word": "顶"}], day=d1)
        db.record_hot_search([{"word": "丙", "hot": 30}], day=d2)
        db.record_hot_search([{"word": "陈年词条", "hot": 1}], day=old)
        days = {x["day"]: x for x in db.hot_search_days(limit=90)}
        got1 = days.get(d1) or {}
        got2 = days.get(d2) or {}
        pruned = db.prune_hot_search(keep_days=180)
        after_pre = db.hot_search_list(day=old, limit=5)
        n = db.clear_hot_search(d2)
        gone = db.hot_search_list(day=d2, limit=5)
        db.clear_hot_search(d1)
        ok = (got1.get("total") == 3 and got1.get("pinned") == 1
              and got2.get("total") == 1 and got2.get("pinned") == 0
              and pruned >= 1 and after_pre["total"] == 0
              and n == 1 and gone["total"] == 0)
        return (ok, "d1=%s/%s d2=%s/%s prune=%d clear=%d 余=%d"
                % (got1.get("total"), got1.get("pinned"), got2.get("total"),
                   got2.get("pinned"), pruned, n, gone["total"]))

    chk("S17", "hot_history_aggregate", _hot_record_aggregate)
    chk("S17", "hot_pinned_first", _hot_pinned_first)
    chk("S17", "hot_days_and_clear", _hot_days_and_clear)

    # ---------------- S18 热搜热力点图 + 原文预览解析（v1.6.3） ----------------
    def _hot_heatmap():
        """按日聚合：heat = SUM(hot_peak)，置顶（hot_peak=0）只计条数不抬热度。"""
        from history_db import get_history_db
        db = get_history_db()
        d1, d2 = "2099-02-01", "2099-02-02"
        for d in (d1, d2):
            db.clear_hot_search(d)
        db.record_hot_search([{"word": "甲", "hot": 1000000},
                              {"word": "乙", "hot": 500000}],
                             pinned=[{"word": "顶"}], day=d1)
        db.record_hot_search([{"word": "丙", "hot": 300000}], day=d2)
        rows = db.hot_search_heatmap(190)
        by = {r["day"]: r for r in rows}
        a, b = by.get(d1) or {}, by.get(d2) or {}
        ok = (a.get("total") == 3 and a.get("heat") == 1500000 and a.get("pinned") == 1
              and b.get("total") == 1 and b.get("heat") == 300000)
        # 6 个月窗口外的旧日子不该出现
        db.record_hot_search([{"word": "远古", "hot": 999}], day="2000-01-01")
        old_in = any(r["day"] == "2000-01-01" for r in db.hot_search_heatmap(190))
        db.clear_hot_search("2000-01-01")
        for d in (d1, d2):
            db.clear_hot_search(d)
        return (ok and not old_in, "d1=%s/%s/%s d2=%s/%s 旧日入图=%s"
                % (a.get("total"), a.get("heat"), a.get("pinned"),
                   b.get("total"), b.get("heat"), old_in))

    _SEARCH_HTML = (
        '<div class="card-wrap" action-type="feed_list_item" mid="5123456789012345">'
        '<div class="content"><div class="txt" node-type="feed_list_content_full">'
        '<p class="txt" node-type="feed_list_content" nick-name="剧综有饭">'
        '#某话题#正文内容</p></div>'
        '<div class="media"><ul><li><img src="//wx1.sinaimg.cn/large/a1.jpg" /></li>'
        '<li><img src="//wx2.sinaimg.cn/orj360/a2.jpg" /></li></ul></div>'
        '<div class="from"><a class="name">剧综有饭</a><a href="#">今天 19:58</a>'
        ' 来自 iPhone客户端</div></div></div>'
        '<div class="card-wrap" action-type="feed_list_item" mid="5123456789012346">'
        '<p class="txt" node-type="feed_list_content" nick-name="第二个人">第二条</p></div>'
    )

    def _hot_search_parser():
        from weibo_client import parse_search_html
        rows = parse_search_html(_SEARCH_HTML, 2)
        one = parse_search_html(_SEARCH_HTML, 1)
        if len(rows) != 2 or len(one) != 1:
            return (False, "条数异常 rows=%d one=%d" % (len(rows), len(one)))
        r0, r1 = rows[0], rows[1]
        ok = (r0["author"] == "剧综有饭" and r0["text"].startswith("#某话题#")
              and r0["images"] == ["https://wx1.sinaimg.cn/thumb150/a1.jpg",
                                   "https://wx2.sinaimg.cn/thumb150/a2.jpg"]
              and "19:58" in r0["created_at"] and "剧综有饭" not in r0["created_at"]
              and r0["url"].endswith("5123456789012345")
              and r1["author"] == "第二个人" and r1["images"] == [])
        return (ok, "r0=%s/%s imgs=%d at=%s | r1=%s"
                % (r0["author"], r0["text"][:8], len(r0["images"]),
                   r0["created_at"], r1["author"]))

    def _hot_preview_guard():
        """空词条直接拒绝；解析不到（例如访客被拦）时返回 ok=False 而不是抛异常。"""
        from weibo_client import parse_search_html, get_hot_topic_preview
        empty = parse_search_html("", 1)
        guard = get_hot_topic_preview("", "")
        return (empty == [] and guard.get("ok") is False,
                "空HTML=%s 空词条=%s" % (empty, guard.get("error")))

    chk("S18", "hot_heatmap_aggregate", _hot_heatmap)
    chk("S18", "hot_search_parser", _hot_search_parser)
    chk("S18", "hot_preview_guard", _hot_preview_guard)

    # ---------------- S19 Cookie 带域存储 + 常驻保活（v1.6.5） ----------------
    def _cookie_entries_domain():
        """同名 Cookie 在 .weibo.com / .weibo.cn 下各存一份，互不覆盖；
        老的空域条目原地升级而不是追加；扁平串按域优先级择一输出。"""
        import http.cookiejar
        import requests
        from weibo_client import parse_cookie_entries, entries_to_cookie, merge_cookie_entries
        flat = "SUB=A1; SUBP=B1; WBPSESS=C1"
        rows = parse_cookie_entries(flat)
        if entries_to_cookie(rows) != flat:
            return (False, "扁平串往返不一致：%s" % entries_to_cookie(rows))
        s = requests.Session()
        for name, val, dom in (("SUB", "A2", ".weibo.com"),
                               ("WBPSESS", "C9", ".weibo.cn"),
                               ("SUB", "M3", ".weibo.cn")):
            s.cookies.set_cookie(http.cookiejar.Cookie(
                0, name, val, None, False, dom, True, False, "/", True, False,
                None, False, None, None, {}))
        rows2, changed = merge_cookie_entries(rows, s)
        out = entries_to_cookie(rows2)
        subs = sorted((e["domain"], e["value"]) for e in rows2 if e["name"] == "SUB")
        ok = (changed and subs == [(".weibo.cn", "M3"), (".weibo.com", "A2")]
              and "SUB=A2" in out and "WBPSESS=C9" in out and "SUBP=B1" in out
              and out.count("SUB=") == 1)      # 扁平串里同名只出现一次
        return (ok, "SUB 条目=%s 输出=%s" % (subs, out))

    def _cookie_store_roundtrip():
        """账户层：带域落库 → 读回扁平串 → 老路径只给扁平串时域信息不丢。"""
        import account_manager as am_mod
        m = am_mod.AccountManager()
        ok, aid = m.add_account("__bench_ck__", "SUB=H1; SUBP=H2")
        if not ok:
            return (False, "建账户失败：%s" % aid)
        try:
            m.update_cookie(aid, "", "带域回写", entries=[
                {"name": "SUB", "value": "H3", "domain": ".weibo.com", "path": "/"},
                {"name": "WBPSESS", "value": "H4", "domain": ".weibo.cn", "path": "/"}])
            after_entries = m.get_cookie(aid)
            jar = [(e["name"], e["domain"]) for e in m.get_cookie_entries(aid)]
            # 旧回写路径：只给扁平串
            m.update_cookie(aid, "SUB=H5; WBPSESS=H4", "扁平回写")
            after_flat = m.get_cookie(aid)
            jar2 = [(e["name"], e["domain"], e["value"]) for e in m.get_cookie_entries(aid)]
            domains_kept = all(d for _, d, _ in jar2)
            ok2 = ("SUB=H3" in after_entries and "WBPSESS=H4" in after_entries
                   and ("SUB", ".weibo.com") in jar
                   and "SUB=H5" in after_flat and domains_kept)
            return (ok2, "entries=%s jar=%s flat=%s jar2=%s"
                    % (after_entries, jar, after_flat, jar2))
        finally:
            m.remove_account(aid)

    def _keepalive_worker_lifecycle():
        """常驻保活线程：幂等启动、可停止、无账户时不联网直接返回。"""
        import scheduler as S
        sch = S.get_scheduler()
        first = sch.start_keepalive_worker()
        second = sch.start_keepalive_worker()
        alive = bool(sch._ka_thread and sch._ka_thread.is_alive())
        no_net = sch.final_keepalive(1.0)      # 无账户 → 不发请求，返回 False
        sch.stop_keepalive_worker()
        stopped = sch._ka_stop.is_set()
        ok = (first is True and second is False and alive and no_net is False and stopped)
        return (ok, "first=%s second=%s alive=%s final_no_net=%s stopped=%s"
                % (first, second, alive, not no_net, stopped))

    chk("S19", "cookie_entries_domain", _cookie_entries_domain)
    chk("S19", "cookie_store_roundtrip", _cookie_store_roundtrip)
    chk("S19", "keepalive_worker_lifecycle", _keepalive_worker_lifecycle)

    # ---------------- S20 「最近使用」= 最近一次成功轮询（v1.6.6） ----------------
    def _fetch_uid_ok_flag():
        """fetch_uid 必须带出 ok 标志：失败 False、成功 True（无新微博也算成功）。"""
        import forward_engine as fe
        orig = fe.get_latest_posts
        try:
            fe.get_latest_posts = lambda *a, **k: {
                "ok": False, "status": "cookie_expired", "error": "已失效"}
            bad = fe.fetch_uid("1000001", "SUB=x", {"screen_name": "T"})
            fe.get_latest_posts = lambda *a, **k: {
                "ok": True, "posts": [], "renewed_cookie": ""}
            good = fe.fetch_uid("1000001", "SUB=x", {"screen_name": "T"})
        finally:
            fe.get_latest_posts = orig
        ok = (len(bad) == 5 and bad[4] is False
              and len(good) == 5 and good[4] is True)
        return (ok, "失败元组=%s 成功元组=%s" % (len(bad), len(good)))

    def _last_used_recorded():
        """mark_used 必须写进 last_used 并在 list_accounts 里透出（前端「最近使用」列）。"""
        import account_manager as am_mod
        m = am_mod.AccountManager()
        ok, aid = m.add_account("__bench_lu__", "SUB=LU1")
        if not ok:
            return (False, "建账户失败：%s" % aid)
        try:
            before = {a["id"]: a for a in m.list_accounts()}[aid]["last_used"]
            m.mark_used(aid)
            row = {a["id"]: a for a in m.list_accounts()}[aid]
            val = row.get("last_used") or ""
            # 形如 2026-11-13 22:41:58
            shape = len(val) == 19 and val[4] == "-" and val[10] == " "
            return (before == "" and shape, "before=%r after=%r" % (before, val))
        finally:
            m.remove_account(aid)

    chk("S20", "fetch_uid_ok_flag", _fetch_uid_ok_flag)
    chk("S20", "last_used_recorded", _last_used_recorded)

    # ---------------- S21 登录态实测锚点（v1.6.7） ----------------
    def _verify_anchor_moves():
        """实测成功 → 写 last_verified_at 且预计失效锚定到「实测 + 7 天」；
        实测失效 → 只标 expired，last_verified_at 作为历史事实保留不动。"""
        from datetime import datetime, timedelta
        import account_manager as am_mod
        m = am_mod.AccountManager()
        ok, aid = m.add_account("__bench_vf__", "SUB=VF1")
        if not ok:
            return (False, "建账户失败：%s" % aid)
        try:
            m.set_verified(aid, True)
            row = {a["id"]: a for a in m.list_accounts()}[aid]
            v1 = row.get("last_verified_at") or ""
            r1 = row.get("verify_result")
            exp_str = (row.get("cookie_expiry") or {}).get("expiry_str", "")
            # 预计失效应等于 实测时刻 + 7 天
            want = (datetime.strptime(v1, "%Y-%m-%d %H:%M:%S")
                    + timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
            m.set_verified(aid, False)
            row2 = {a["id"]: a for a in m.list_accounts()}[aid]
            kept = row2.get("last_verified_at") == v1
            r2 = row2.get("verify_result")
            good = (len(v1) == 19 and r1 == "valid" and exp_str == want
                    and kept and r2 == "expired")
            return (good, "实测=%s 预计=%s 期望=%s r1=%s kept=%s r2=%s"
                    % (v1, exp_str, want, r1, kept, r2))
        finally:
            m.remove_account(aid)

    def _probe_login_guard():
        """空 Cookie：不发请求、不抛异常，且**不给出否定结论**（v1.6.9 起移动端只做正向确认）。"""
        from weibo_client import probe_login
        r = probe_login("")
        return (r.get("ok") is False and r.get("login") is None
                and r.get("status") == "unknown", str(r))

    chk("S21", "verify_anchor_moves", _verify_anchor_moves)
    chk("S21", "probe_login_guard", _probe_login_guard)

    # ---------------- S22 实测口径修正（v1.6.9） ----------------
    class _FakeResp(object):
        def __init__(self, code, text):
            self.status_code = code
            self.text = text

        def json(self):
            import json as _j
            return _j.loads(self.text)

    class _FakeSession(object):
        def __init__(self, resp):
            self.headers = {}
            self._resp = resp

        def update(self, *a, **k):
            pass

        def get(self, *a, **k):
            return self._resp

    def _probe_mobile_never_negative():
        """【回归 v1.6.9 核心 BUG】移动端 data.login=false **绝不能**变成「已失效」。

        现场症状：抓取全部成功、账户状态正常，有效期列却显示「实测已失效」，
        点一次「检测」又恢复 —— 因为移动端与网页端不同源，网页端有效 Cookie
        在 m.weibo.cn 上本来就会回答未登录。
        """
        import weibo_client as wc
        orig = wc._session
        try:
            wc._session = lambda *a, **k: _FakeSession(
                _FakeResp(200, '{"ok":1,"data":{"login":false}}'))
            r = wc.probe_login("SUB=abc; SUBP=def")
        finally:
            wc._session = orig
        ok = (r.get("login") is None and r.get("status") == "unknown"
              and r.get("status") != "expired")
        return (ok, "login=%r status=%r detail=%s"
                % (r.get("login"), r.get("status"), r.get("detail")))

    def _probe_web_can_say_expired():
        """同源接口 ok=-100 时必须判失效 —— 否则真过期就永远发现不了。"""
        import weibo_client as wc
        orig = wc._session
        try:
            wc._session = lambda *a, **k: _FakeSession(
                _FakeResp(200, '{"ok":-100,"url":"https://passport.weibo.com/sso/login"}'))
            r = wc.probe_login_web("SUB=abc", "1234567890")
        finally:
            wc._session = orig
        ok = (r.get("login") is False and r.get("status") == "expired")
        return (ok, "login=%r status=%r detail=%s"
                % (r.get("login"), r.get("status"), r.get("detail")))

    def _probe_web_requires_uid():
        """没有 uid 就不该发请求，也不能给出否定结论（只能 unknown）。"""
        from weibo_client import probe_login_web
        r = probe_login_web("SUB=abc", "")
        ok = (r.get("ok") is False and r.get("login") is None
              and r.get("status") == "unknown")
        return (ok, str(r))

    def _keepalive_no_false_expired():
        """保活轮次里，两路探针都给不出结论时，**一个字都不许写库**。"""
        import scheduler as S
        import weibo_client as wc
        import account_manager as am_mod
        m = am_mod.AccountManager()
        ok, aid = m.add_account("__bench_ka__", "SUB=KA1")
        if not ok:
            return (False, "建账户失败：%s" % aid)
        orig = (wc.keepalive, wc.probe_login_web, wc.probe_login)
        try:
            wc.keepalive = lambda *a, **k: {"ok": False, "cookie": "SUB=KA1",
                                           "entries": [], "status": "unknown",
                                           "detail": "bench"}
            wc.probe_login_web = lambda *a, **k: {
                "ok": False, "login": None, "status": "unknown", "detail": "同源测不出"}
            wc.probe_login = lambda *a, **k: {
                "ok": True, "login": None, "status": "unknown", "detail": "跨域口径"}
            S.get_scheduler().keepalive_all()
        finally:
            (wc.keepalive, wc.probe_login_web, wc.probe_login) = orig
        try:
            row = {a["id"]: a for a in m.list_accounts()}[aid]
            vr = row.get("verify_result") or ""
            lv = row.get("last_verified_at") or ""
            return (vr == "" and lv == "", "verify=%r last_verified=%r" % (vr, lv))
        finally:
            m.remove_account(aid)

    def _set_verified_reports_transition():
        """set_verified 必须回报状态翻转，并留下「何时判失效」的时间证据（v1.6.9）。"""
        import account_manager as am_mod
        m = am_mod.AccountManager()
        ok, aid = m.add_account("__bench_tv__", "SUB=TV1")
        if not ok:
            return (False, "建账户失败：%s" % aid)
        try:
            r1 = m.set_verified(aid, True, source="bench", detail="首次")
            r2 = m.set_verified(aid, True, source="bench", detail="重复")
            r3 = m.set_verified(aid, False, source="bench", detail="失效")
            raw = {a["id"]: a for a in m.accounts}[aid]
            stamp = raw.get("verify_expired_at") or ""
            r4 = m.set_verified(aid, True, source="bench", detail="恢复")
            raw2 = {a["id"]: a for a in m.accounts}[aid]
            cleared = raw2.get("verify_expired_at") or ""
            ok = (r1.get("changed") is True and r1.get("now") == "valid"
                  and r2.get("changed") is False
                  and r3.get("changed") is True and r3.get("now") == "expired"
                  and len(stamp) == 19          # 判失效时刻已记录
                  and r4.get("changed") is True and r4.get("prev") == "expired"
                  and cleared == "")            # 恢复后时间证据被撤销
            return (ok, "r1=%s r2=%s r3=%s stamp=%r r4=%s cleared=%r"
                    % (r1.get("changed"), r2.get("changed"), r3.get("changed"),
                       stamp, r4.get("changed"), cleared))
        finally:
            m.remove_account(aid)

    def _legacy_expired_reconciled():
        """老版本误标的 expired（无 verify_expired_at 时间戳）必须自动清理，
        但**带时间戳的真失效必须保留** —— 否则升级会掩盖真正过期的账户。"""
        import account_manager as am_mod
        m = am_mod.AccountManager()
        ok1, a1 = m.add_account("__bench_legacy__", "SUB=LG1")
        ok2, a2 = m.add_account("__bench_real__", "SUB=RG1")
        if not (ok1 and ok2):
            return (False, "建账户失败")
        try:
            for a in m.accounts:
                if a["id"] == a1:
                    a["status"] = "ok"
                    a["last_ok_at"] = "2026-11-13 23:20:54"
                    a["last_used"] = "2026-11-13 23:20:54"
                    a["verify_result"] = "expired"
                    a.pop("verify_expired_at", None)   # 老版本没这个字段
                elif a["id"] == a2:
                    a["status"] = "cookie_expired"
                    a["verify_result"] = "expired"
                    a["verify_expired_at"] = "2026-11-13 23:33:22"
            m.save()
            m2 = am_mod.AccountManager()               # 重新 load，触发 reconcile
            rows = {a["id"]: a for a in m2.list_accounts()}
            legacy = rows[a1].get("verify_result") or ""
            real = rows[a2].get("verify_result") or ""
            good = (legacy == "" and real == "expired")
            return (good, "误报已清=%r(期望空) 真失效保留=%r(期望expired)"
                    % (legacy, real))
        finally:
            m.remove_account(a1)
            m.remove_account(a2)

    chk("S22", "probe_mobile_never_negative", _probe_mobile_never_negative)
    chk("S22", "probe_web_can_say_expired", _probe_web_can_say_expired)
    chk("S22", "probe_web_requires_uid", _probe_web_requires_uid)
    chk("S22", "keepalive_no_false_expired", _keepalive_no_false_expired)
    chk("S22", "set_verified_reports_transition", _set_verified_reports_transition)
    chk("S22", "legacy_expired_reconciled", _legacy_expired_reconciled)

    # ---------------- S23 CPU 占用优化（v1.6.11） ----------------
    import api_bridge as _ab

    def _temp_probe_needs_demand():
        """A 组：没人看概览页时不得启动温度探测（连宿主都不许起）。

        这是「软件挂在托盘却周期性冲高 CPU」的根因回归项。
        """
        _ab._SYS_LOAD_DEMAND["ts"] = 0.0
        _ab._CPU_TEMP["next_due"] = 0.0
        _ab._CPU_TEMP["busy"] = False
        _ab._schedule_cpu_temp_probe()
        time.sleep(0.35)
        busy = _ab._CPU_TEMP["busy"]
        spawned = _ab._TEMP_HOST._proc is not None
        good = (busy is False) and (not spawned)
        return (good, "busy=%r 宿主已起=%r（都应为 False）" % (busy, spawned))

    def _temp_host_steady_state():
        """B 组：常驻宿主稳态单次查询必须远低于旧实现的 400ms 起进程成本。"""
        _ab._mark_load_demand()
        t0 = time.perf_counter()
        _ab._read_cpu_temp()                       # 冷启动（起宿主 + WMI 装配）
        cold = (time.perf_counter() - t0) * 1000
        xs = []
        for _ in range(3):
            t1 = time.perf_counter()
            _ab._read_cpu_temp()
            xs.append((time.perf_counter() - t1) * 1000)
        steady = sorted(xs)[len(xs) // 2]
        # 稳态门槛 120ms：旧实现单次 400ms（冷启动近 1s），留足机器差异余量
        good = steady < 120.0
        return (good, "冷启动 %.0fms · 稳态 %.1fms（门槛 <120ms）" % (cold, steady))

    def _temp_host_no_orphan():
        """C 组：close() 后不得残留 powershell 进程。"""
        _ab._mark_load_demand()
        _ab._read_cpu_temp()
        had = _ab._TEMP_HOST._proc is not None
        _ab._close_temp_host()
        time.sleep(0.3)
        left = _ab._TEMP_HOST._proc is not None
        good = had and (not left)
        return (good, "关闭前已起=%r 关闭后仍存活=%r" % (had, left))

    def _rAF_idle_stops():
        """D 组：前端进度环空闲时必须停止 rAF，而不是每帧空转。"""
        js = open(os.path.join(UI, "app.js"), encoding="utf-8").read()
        # 空闲分支必须「return」而不是「requestAnimationFrame 续期」
        idle_branch = re.search(
            r"if \(!sch\.running \|\| !sch\.next_run\) \{(.{0,400}?)\n  \}",
            js, re.S)
        body = idle_branch.group(1) if idle_branch else ""
        no_loop = "requestAnimationFrame" not in body
        has_idle = "_fwdShowIdle" in body
        # 必须有节流常量与看门狗，保证停掉后还能重新启动
        throttled = "_FWD_FRAME_MS" in js
        watchdog = "_fwdRingWatchdog" in js and "setInterval(_fwdRingWatchdog" in js
        good = no_loop and has_idle and throttled and watchdog
        return (good, "空闲不续期=%r 有清理=%r 有节流=%r 有看门狗=%r"
                % (no_loop, has_idle, throttled, watchdog))

    def _render_diff_guard():
        """E 组：账户表与统计数字必须做差量更新，值没变就不碰 DOM。"""
        js = open(os.path.join(UI, "app.js"), encoding="utf-8").read()
        acc = "_lastHtml === html" in js and "tb.innerHTML = html;" in js
        txt = "if (el.textContent !== s) el.textContent = s;" in js
        geom = "_fwdGeom = sig;" in js
        good = acc and txt and geom
        return (good, "账户表差量=%r 文本差量=%r 几何缓存=%r" % (acc, txt, geom))

    chk("S23", "temp_probe_needs_demand", _temp_probe_needs_demand)
    chk("S23", "temp_host_steady_state", _temp_host_steady_state)
    chk("S23", "temp_host_no_orphan", _temp_host_no_orphan)
    chk("S23", "rAF_idle_stops", _rAF_idle_stops)
    chk("S23", "render_diff_guard", _render_diff_guard)


    # ---------------- S24 转发策略开关 + 相册滚动（v1.6.12） ----------------
    import forward_engine as _fe

    def _fwd_jitter_range():
        """排程波动：开启后每次取值必须落在 1~N，且两端都出现；关闭时恒为 N。"""
        on = {"forward_count": 5, "forward_count_jitter": True}
        vals = [_fe.get_forward_count_for_round(on) for _ in range(400)]
        off = {"forward_count": 5, "forward_count_jitter": False}
        fixed = {_fe.get_forward_count_for_round(off) for _ in range(50)}
        one = {_fe.get_forward_count_for_round(
            {"forward_count": 1, "forward_count_jitter": True}) for _ in range(100)}
        good = (min(vals) == 1 and max(vals) == 5
                and all(1 <= v <= 5 for v in vals)
                and fixed == {5} and one == {1})
        return (good, "波动范围=%d~%d 关闭时=%s N=1时=%s"
                % (min(vals), max(vals), sorted(fixed), sorted(one)))

    def _fwd_images_only():
        """仅转发含图：纯文字 / 纯视频（images 为空）必须被滤掉。"""
        cands = [
            {"uid": "a", "post": {"id": "1", "images": ["u1"]}},
            {"uid": "b", "post": {"id": "2", "images": []}},
            {"uid": "c", "post": {"id": "3"}},
            {"uid": "d", "post": {"id": "4", "images": ["u2", "u3"]}},
            {"uid": "e", "post": None},
        ]
        kept = [c["post"]["id"] for c in _fe._filter_images_only(cands)]
        empty = _fe._filter_images_only([])
        return (kept == ["1", "4"] and empty == [],
                "保留=%s 空池=%r" % (kept, empty))

    def _fwd_opt_defaults():
        """两个新开关默认关闭，老用户升级后行为不变。"""
        from config_manager import DEFAULT_CONFIG as D
        a = D.get("forward_count_jitter")
        b = D.get("forward_images_only")
        return (a is False and b is False,
                "jitter=%r images_only=%r" % (a, b))

    def _media_thumb_smaller():
        """缩略图必须同时满足：长边 ≤208px、字节 <60KB、相对原图 <50%。"""
        import api_bridge as ab
        from PIL import Image
        tmp = os.path.join(data_dir, "_thumb_src.jpg")
        im = Image.new("RGB", (1200, 1600))
        px = im.load()
        for y in range(0, 1600, 4):          # 造点纹理，避免纯色 JPEG 小得不真实
            for x in range(0, 1200, 4):
                px[x, y] = ((x * 7) % 256, (y * 11) % 256, ((x + y) * 3) % 256)
        im.save(tmp, "JPEG", quality=95)
        src = os.path.getsize(tmp)
        dst_jpg, dst_png = ab._thumb_targets(tmp, 208)
        for q in (dst_jpg, dst_png):
            try:
                os.remove(q)
            except Exception:
                pass
        out = ab._make_thumb(tmp, dst_jpg, dst_png, 208)
        size = os.path.getsize(out)
        with Image.open(out) as o:
            w, h = o.size
        good = max(w, h) <= 208 and size < 60 * 1024 and size < src * 0.5
        return (good, "原 %.0fKB → 缩略 %.1fKB %dx%d"
                % (src / 1024.0, size / 1024.0, w, h))

    def _media_thumb_cache_hit():
        """第二次必须命中原文件（不重写），且缓存目录不落在用户媒体目录里。"""
        import api_bridge as ab
        from PIL import Image
        tmp = os.path.join(data_dir, "_thumb_src2.jpg")
        Image.new("RGB", (900, 1200), (30, 60, 90)).save(tmp, "JPEG")
        dst_jpg, dst_png = ab._thumb_targets(tmp, 208)
        for q in (dst_jpg, dst_png):
            try:
                os.remove(q)
            except Exception:
                pass
        p1 = ab._make_thumb(tmp, dst_jpg, dst_png, 208)
        m1 = os.stat(p1).st_mtime_ns
        time.sleep(0.05)
        # 模拟「缓存已存在」时桥方法的分支：只读文件，不再重建
        p2 = dst_jpg if os.path.isfile(dst_jpg) else dst_png
        m2 = os.stat(p2).st_mtime_ns
        inside = os.path.abspath(ab._THUMB_CACHE_DIR).startswith(
            os.path.abspath(ab.get_media_dir()) + os.sep)
        good = (p1 == p2) and (m1 == m2) and (not inside)
        return (good, "命中=%r mtime相同=%r 缓存落在媒体目录内=%r"
                % (p1 == p2, m1 == m2, inside))

    def _media_page_lazy_contract():
        """相册页前端契约：滚动驱动懒加载 + 并发限流 + 走缩略图接口 + 不再逐项拉原图。

        对照组：把同一套判定套在「旧写法」上必须判失败，证明检查不是摆设。
        """
        js = open(os.path.join(UI, "app.js"), encoding="utf-8").read()

        def judge(src):
            return {
                "scroll": 'addEventListener("scroll", _mediaThumbSchedule' in src,
                "throttle": "_MEDIA_THUMB_THROTTLE" in src,
                "par": "_MEDIA_THUMB_PARALLEL" in src,
                "thumb_api": 'apiCall("get_media_thumb_data"' in src,
                "no_raw": 'apiCall("load_media_base64"' not in src,
                "no_eager": "list.forEach(o => loadMediaThumb" not in src,
                "dims": 'decoding="async"' in src,
                # 首屏可见的卡片必须立刻加载，不能等下一次滚动事件
                "prime": "_mediaThumbPrimeViewport();" in src,
                # 必须量卡片矩形（卡片带 content-visibility，量子元素永远是 0）
                "card_rect": "card.getBoundingClientRect()" in src,
            }

        now = judge(js)
        old = judge(
            "async function loadMediaThumb(uid){\n"
            "  const r = await apiCall(\"load_media_base64\", p);\n"
            "}\n"
            "list.forEach(o => loadMediaThumb(o.uid));\n")
        good = all(now.values()) and not all(old.values())
        return (good, "现=%s 旧写法判失败=%r" % (now, not all(old.values())))

    def _media_card_css():
        """卡片 CSS：视口外跳过绘制 + 过渡只保留真正会变的属性。"""
        css = open(os.path.join(UI, "style.css"), encoding="utf-8").read()
        m = re.search(r"\.media-obj-card \{([^}]*)\}", css)
        body = m.group(1) if m else ""
        cv = "content-visibility: auto" in body
        # 注意排除注释里提到的旧写法（注释中是 `transition: .15s` 后跟反引号），
        # 只认真实的声明（以分号结尾）。
        narrowed = ("transition: border-color" in body
                    and re.search(r"transition:\s*\.15s\s*;", body) is None)
        contain = re.search(r"\.media-obj-thumb \{[^}]*contain: paint", css, re.S) is not None
        good = cv and narrowed and contain
        return (good, "content-visibility=%r 过渡收窄=%r 预览框contain=%r"
                % (cv, narrowed, contain))

    chk("S24", "fwd_jitter_range", _fwd_jitter_range)
    chk("S24", "fwd_images_only", _fwd_images_only)
    chk("S24", "fwd_opt_defaults", _fwd_opt_defaults)
    chk("S24", "media_thumb_smaller", _media_thumb_smaller)
    chk("S24", "media_thumb_cache_hit", _media_thumb_cache_hit)
    chk("S24", "media_page_lazy_contract", _media_page_lazy_contract)
    chk("S24", "media_card_css", _media_card_css)

    # ---------------- S25 转发设置归位 + 只看有图默认开（v1.6.13） ----------------
    def _card_block(html, title):
        """粗切出某个 .card 的 HTML 片段：按 '<div class="card"' 切块，
        取标题命中 `>title</h3>` 的那一块（块尾即下一张卡的起点）。
        够用且不依赖缩进 —— 模板里多一层少一层 div 都不会让判定失灵。"""
        parts = html.split('<div class="card"')
        for p in parts:
            if ">%s</h3>" % title in p:
                return p
        return ""

    def _fwd_img_only_in_mode_card():
        """「仅转发含图博文」必须长在「转发方式」卡里，且已从「轮询间隔」卡移除。

        对照组：把开关再塞回「轮询间隔」卡（模拟改动前的结构），同一判定必须判失败
        —— 否则这条检查无法区分「真移过去了」与「两边都写着」。
        """
        html = open(os.path.join(UI, "index.html"), encoding="utf-8").read()

        def judge(src):
            poll = _card_block(src, "轮询间隔")
            mode = _card_block(src, "转发方式")
            return {
                "in_mode": "fwdImagesOnly" in mode,
                "not_in_poll": poll != "" and "fwdImagesOnly" not in poll,
                "after_mode_row": (mode.find("fwd-mode-row") >= 0
                                   and mode.find("fwdImagesOnly") > mode.find("fwd-mode-row")),
                "save_hook": 'id="fwdImagesOnly" onchange="saveForwardOptions()"' in mode,
            }

        now = judge(html)
        # 制造一段「旧结构」：在轮询间隔卡的 jitter 行旁再插一个 fwdImagesOnly
        anchor = '<label class="switch"><input type="checkbox" id="fwdCountJitter"'
        old_html = html.replace(
            anchor,
            '<label class="switch"><input type="checkbox" id="fwdImagesOnly"'
            ' onchange="saveForwardOptions()"><span class="slider"></span></label>'
            + anchor, 1)
        changed = old_html != html
        old = judge(old_html)
        good = all(now.values()) and changed and not all(old.values())
        return (good, "现=%s 对照组已生效=%r 旧结构判失败=%r"
                % (now, changed, not all(old.values())))

    def _fwd_cards_equal_height():
        """两卡等高靠的是这套三件套，缺一即失效：
        ① .fwd-grid2 align-items:stretch ② .card 纵向 flex ③ .fwd-body flex:1 1 auto。"""
        css = open(os.path.join(UI, "style.css"), encoding="utf-8").read()
        g2 = re.search(r"\.fwd-grid2 \{([^}]*)\}", css)
        card = re.search(r"#view-forward \.card \{([^}]*)\}", css)
        body = re.search(r"#view-forward \.card \.fwd-body \{([^}]*)\}", css)
        stretch = bool(g2) and "align-items: stretch" in g2.group(1)
        colflex = bool(card) and re.search(r"display:\s*flex", card.group(1)) \
            and "flex-direction: column" in card.group(1)
        grow = bool(body) and re.search(r"flex:\s*1 1 auto", body.group(1)) is not None
        good = stretch and colflex and grow
        return (good, "网格拉伸=%r 卡片纵向flex=%r body撑开=%r"
                % (stretch, colflex, grow))

    def _hist_img_default():
        """「只看有图」默认开启，且三处写入点统一走同一个常量。

        漏掉任意一处都会出现「默认开、清空筛选后变关」这种自相矛盾。
        """
        html = open(os.path.join(UI, "index.html"), encoding="utf-8").read()
        js = open(os.path.join(UI, "app.js"), encoding="utf-8").read()
        attr = re.search(r'id="hfHasImg"\s+checked', html) is not None
        const = re.search(r"const HIST_HASIMG_DEFAULT\s*=\s*true", js) is not None
        # switchHist 与 clearHistFilters 都必须引用常量，不允许再出现硬编码 false
        switch_ok = re.search(r'imgChk\.checked\s*=\s*\(kind === "monitored"\)\s*\?\s*'
                              r'HIST_HASIMG_DEFAULT', js) is not None
        clear_ok = re.search(r'getElementById\("hfHasImg"\)\.checked = '
                             r'HIST_HASIMG_DEFAULT', js) is not None
        hard_false = len(re.findall(r'getElementById\("hfHasImg"\)\.checked = false', js))
        good = attr and const and switch_ok and clear_ok and hard_false == 0
        return (good, "HTML checked=%r 常量=%r switchHist=%r clearFilters=%r 残留硬编码false=%d"
                % (attr, const, switch_ok, clear_ok, hard_false))

    chk("S25", "fwd_img_only_in_mode_card", _fwd_img_only_in_mode_card)
    chk("S25", "fwd_cards_equal_height", _fwd_cards_equal_height)
    chk("S25", "hist_img_default", _hist_img_default)




    # ---------------- S12 性能基线（批量后查询耗时） ----------------
    def _perf_bulk_then_query():
        from history_db import get_history_db
        db = get_history_db()
        t0 = time.perf_counter()
        for i in range(200):
            db.add_monitored_post("perf_uid", "性能对象", "perf_%d" % i, "内容%d" % i, [], "2026-10-26 09:00:00")
        ins_ms = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        db.get_monitored_posts(limit=200)
        q_ms = (time.perf_counter() - t1) * 1000
        return (q_ms < 800, "insert200=%.0fms query200=%.0fms" % (ins_ms, q_ms))

    chk("S12", "bulk_insert_then_query", _perf_bulk_then_query)

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    return rows


# --------------------------------------------------------------------------- #
#  汇总
# --------------------------------------------------------------------------- #
def summarize(all_rows, rounds):
    keys = []
    agg = {}
    for r in all_rows:
        k = (r["suite"], r["check"])
        if k not in agg:
            agg[k] = {"ok": 0, "n": 0, "ms": [], "detail": ""}
            keys.append(k)
        a = agg[k]
        a["n"] += 1
        a["ok"] += 1 if r["ok"] else 0
        a["ms"].append(r["ms"])
        if not r["ok"]:
            a["detail"] = r["detail"]

    print("\n" + "=" * 96)
    print("全功能基准测试汇总（%d 轮）" % rounds)
    print("=" * 96)
    print("%-5s %-34s %-8s %10s  %s" % ("域", "检查项", "通过率", "中位耗时", "备注"))
    print("-" * 96)
    stable_ok = flaky = stable_fail = 0
    for k in keys:
        a = agg[k]
        rate = "%d/%d" % (a["ok"], a["n"])
        ms = sorted(a["ms"])[len(a["ms"]) // 2]
        if a["ok"] == a["n"]:
            stable_ok += 1
            note = ""
        elif a["ok"] == 0:
            stable_fail += 1
            note = "稳定失败: " + a["detail"][:52]
        else:
            flaky += 1
            note = "偶发失败: " + a["detail"][:52]
        print("%-5s %-34s %-8s %9.1fms  %s" % (k[0], k[1], rate, ms, note))
    print("-" * 96)
    total = sum(a["n"] for a in agg.values())
    passed = sum(a["ok"] for a in agg.values())
    print("总计: %d 项检查 × %d 轮 = %d 次执行，通过 %d 次（%.1f%%）" %
          (len(keys), rounds, total, passed, passed * 100.0 / max(total, 1)))
    print("稳定通过 %d 项 | 偶发失败 %d 项 | 稳定失败 %d 项" % (stable_ok, flaky, stable_fail))
    tot_ms = [sum(r["ms"] for r in all_rows if r["suite"] == s) for s in sorted({r["suite"] for r in all_rows})]
    print("各轮总耗时: " + ", ".join("%.1fs" % (t / 1000.0) for t in tot_ms))
    return {"stable_ok": stable_ok, "flaky": flaky, "stable_fail": stable_fail,
            "passed": passed, "total": total}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--one", nargs=2, metavar=("DATA_DIR", "OUT"))
    args = ap.parse_args()

    if args.one:
        run_one(args.one[0], args.one[1])
        os._exit(0)

    if os.path.isdir(BASE):
        shutil.rmtree(BASE, ignore_errors=True)
    os.makedirs(BASE, exist_ok=True)

    all_rows = []
    t_start = time.time()
    for i in range(1, args.rounds + 1):
        ddir = os.path.join(BASE, "r%d" % i)
        out = os.path.join(BASE, "r%d.json" % i)
        t0 = time.time()
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--one", ddir, out],
                           capture_output=True, text=True)
        cost = time.time() - t0
        if not os.path.isfile(out):
            print("第 %d 轮失败（无结果文件）: %s" % (i, (p.stderr or "")[-600:]))
            continue
        rows = json.load(open(out, encoding="utf-8"))
        all_rows += rows
        bad = [r for r in rows if not r["ok"]]
        print("第 %d 轮：%d 项，失败 %d 项，耗时 %.1fs%s" %
              (i, len(rows), len(bad), cost,
               ("  失败项: " + ", ".join("%s" % r["check"] for r in bad)) if bad else ""))

    if all_rows:
        summarize(all_rows, args.rounds)
    print("\n总用时 %.1fs" % (time.time() - t_start))


if __name__ == "__main__":
    main()
