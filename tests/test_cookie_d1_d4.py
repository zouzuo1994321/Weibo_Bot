#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Cookie 四项缺陷（D1 / D1b / D1c / D2 / D3 / D4）回归测试。

    python tests/test_cookie_d1_d4.py

背景：用户从 CookieMonitor 导出的报告里，这四类问题被逐条坐实 ——

  D1   同一次失败里既有 ok=-100 又有 HTTP 432 → 被报成 cookie_expired
  D1b  首轮全败无条件「预热 + 整套重试」，限流时请求翻倍
  D1c  18:33:27 判失效，18:45:22 同一份 Cookie 又抓成功（瞬时误判）
  D2   媒体扫描与轮询在 11 个分钟里同时开火 → 自己把自己打成 432
  D3   336 次「登录态实测」全部因缺少 uid 跳过（首页 uid 正则失配）
  D4   profile 里 0 条 Cookie（os._exit 硬杀，WebView2 没来得及落盘）

本测试全部离线（monkeypatch requests.Session.get），不打真实微博接口。
"""
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "_src_v151k")
sys.path.insert(0, SRC)
sys.path.insert(0, HERE)

# 独立数据目录，绝不碰用户真实账户
_TMP = os.path.join(ROOT, "tests", "_tmp_d1d4")
os.environ["WEIBOBOT_DATA_DIR"] = _TMP
# 每次从干净状态开始（否则上一轮留下的账户会让「初始 wb_uid 为空」失效）
if os.path.isdir(_TMP):
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)
os.makedirs(_TMP, exist_ok=True)

OK = True
FAILED = []


def check(name, cond, extra=""):
    global OK
    ok = bool(cond)
    OK = OK and ok
    if not ok:
        FAILED.append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           (" · " + str(extra)) if extra else ""))


# --------------------------------------------------------------------------- #
#  假响应 / 假 Session
# --------------------------------------------------------------------------- #
class FakeResp(object):
    def __init__(self, status, text, json_obj=None, headers=None):
        self.status_code = status
        self.text = text or ""
        self.content = (text or "").encode("utf-8")
        self.headers = headers or {}
        self._json = json_obj

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


COOKIE = ("SUB=_2A25KabcSUB; SUBP=0033WrSXsubp; XSRF-TOKEN=Vj8QkLmNoPqRsTuV; "
          "ALF=1780000000; SCF=abc")

CALLS = []


def _install(fn):
    """把假 get 装到 requests.Session 上，并记录每次请求的 URL。"""
    import requests

    def wrapper(self, url, *a, **kw):
        CALLS.append(url)
        return fn(self, url, *a, **kw)
    requests.Session.get = wrapper


print("\n=== D3 · 首页 uid 解析（旧正则全线失配） ===")
import weibo_client as wc  # noqa: E402

# 现代 weibo.com 首页：只有 Vue 初始状态里的 "uid":"5189652182"，没有 $CONFIG['uid']
MODERN_HOME = (
    '<html><body><div id="app"></div><script>window.__INITIAL_STATE__='
    '{"config":{"uid":"5189652182","nick":"肆月Aperture","aid":"01"},'
    '"feed":{}}</script></body></html>'
)
_install(lambda self, url, *a, **kw: FakeResp(200, MODERN_HOME))
uid, _ = wc.get_self_uid(COOKIE)
check("从 Vue 初始状态解析出自身 uid", uid == "5189652182", uid)

# 老写法仍要能解析（不能为了修新的把旧的丢了）
OLD_HOME = "<script>var $CONFIG={};$CONFIG['uid']='6996654711';</script>"
_install(lambda self, url, *a, **kw: FakeResp(200, OLD_HOME))
uid_old, _ = wc.get_self_uid(COOKIE)
check("老式 $CONFIG['uid'] 仍可解析", uid_old == "6996654711", uid_old)

# 页面里确实没有 uid → 必须老实返回空，不能瞎编
_install(lambda self, url, *a, **kw: FakeResp(200, "<html>nothing here</html>"))
uid_none, _ = wc.get_self_uid(COOKIE)
check("解析不到时返回空（不伪造）", uid_none == "", repr(uid_none))

print("\n=== D3 · probe_login_web 的契约（缺 uid 不发请求、不给否定结论） ===")


def scene_probe(home_html):
    def _get(self, url, *a, **kw):
        if url.rstrip("/") == "https://weibo.com":
            return FakeResp(200, home_html)
        if "profile/info" in url:
            return FakeResp(200, '{"ok":1}', {
                "ok": 1, "data": {"user": {"id": "5189652182",
                                           "screen_name": "肆月Aperture"}}})
        return FakeResp(200, "", {})
    return _get


_install(scene_probe(MODERN_HOME))
CALLS.clear()
r = wc.probe_login_web(COOKIE, "")          # 故意不传 uid
check("缺 uid → login=None（不写库）", r.get("login") is None, r.get("detail"))
check("缺 uid → status=unknown", r.get("status") == "unknown", r.get("status"))
check("缺 uid → 一个请求都不发（判定探针不加副作用）",
      len(CALLS) == 0, "发出 %d 个请求" % len(CALLS))
r = wc.probe_login_web(COOKIE, "5189652182")
check("给了 uid → 同源实测跑通", r.get("login") is True, r.get("detail"))
check("回传本次实测用的 uid", r.get("uid") == "5189652182", r.get("uid"))

print("\n=== D3 · diagnose/check_login 路径能否自愈补 uid ===")
_install(scene_probe(MODERN_HOME))
r = wc.check_login(COOKIE)
check("check_login 从现代首页解析到 uid", r.get("uid") == "5189652182", r.get("uid"))
check("self_uid 一并回传（供 _persist_diag 落盘）",
      r.get("self_uid") == "5189652182", r.get("self_uid"))

print("\n=== D3 · account_manager.set_wb_uid ===")
from account_manager import get_account_manager  # noqa: E402

am = get_account_manager()
ok_add, acc_id = am.add_account("D1D4测试账户", COOKIE)
check("账户已添加", ok_add and bool(acc_id), acc_id)
check("初始 wb_uid 为空", (am.list_accounts()[0].get("wb_uid") or "") == "")
check("回填 uid 成功", am.set_wb_uid(acc_id, "5189652182") is True)
check("uid 已落盘",
      (am.list_accounts()[0].get("wb_uid") or "") == "5189652182",
      am.list_accounts()[0].get("wb_uid"))
check("重复回填返回 False（幂等）", am.set_wb_uid(acc_id, "5189652182") is False)
check("非法 uid 被拒", am.set_wb_uid(acc_id, "abc") is False)
check("空 uid 被拒", am.set_wb_uid(acc_id, "") is False)
# 重新加载确认真的写进了文件（不是只在内存里）
am2 = type(am)()
check("重启后 wb_uid 仍在",
      (am2.list_accounts()[0].get("wb_uid") or "") == "5189652182",
      am2.list_accounts()[0].get("wb_uid"))

print("\n=== D3 · 保活线程自动补 uid（修复 336 次「实测跳过」的现场） ===")
import scheduler as SCH  # noqa: E402
import weibo_client as WC  # noqa: E402

ok_add2, acc_id2 = am.add_account("保活补uid账户", COOKIE)
check("第二个账户已添加（uid 为空）", ok_add2 and acc_id2, acc_id2)

_wc_orig = (WC.keepalive, WC.probe_login_web, WC.get_self_uid)
try:
    WC.keepalive = lambda *a, **k: {"ok": True, "cookie": COOKIE, "entries": [],
                                    "status": "ok", "detail": "bench"}
    WC.probe_login_web = lambda *a, **k: {"ok": True, "login": True,
                                          "status": "valid", "uid": "5189652182",
                                          "detail": "bench-同源有效"}
    WC.get_self_uid = lambda *a, **k: ("5189652182", COOKIE)
    SCH.get_scheduler().keepalive_all()
finally:
    WC.keepalive, WC.probe_login_web, WC.get_self_uid = _wc_orig

row = next((a for a in am.list_accounts() if a["id"] == acc_id2), {})
check("保活时补解析并落盘 wb_uid", row.get("wb_uid") == "5189652182", row.get("wb_uid"))
check("实测结论已写库（不再「跳过」）",
      row.get("verify_result") == "valid", row.get("verify_result"))

print("\n=== D1 · 风控必须优先于未登录 ===")
# 用户日志原样场景：mymblog 200+ok=-100，getProfileFeed 登录墙，m.container 432
NOT_LOGIN = '{"ok":-100,"url":"https://weibo.com/login.php"}'
RISK_432 = "<html>访问过于频繁</html>"


def scene_d1(self, url, *a, **kw):
    if "mymblog" in url:
        return FakeResp(200, NOT_LOGIN, {"ok": -100})
    if "getProfileFeed" in url:
        return FakeResp(200, NOT_LOGIN, {"ok": -100})
    if "container" in url or "m.weibo.cn" in url:
        return FakeResp(432, RISK_432)
    return FakeResp(200, "", {})


_install(scene_d1)
CALLS.clear()
res = wc.get_latest_posts("6996654711", COOKIE, page=1, count=10)
check("有 432 时判 risk_control（不再报失效）",
      res.get("status") == "risk_control", res.get("status"))
check("原因里点明风控", "风控" in (res.get("reason") or ""), res.get("reason"))
# D1b：命中风控时不得再预热重试（否则请求翻倍）
home_hits = [u for u in CALLS if u.rstrip("/") == "https://weibo.com"]
check("D1b 风控时跳过预热重试", len(home_hits) == 0, "首页预热请求=%d" % len(home_hits))

print("\n=== D1b · 非风控失败仍保留一次续期重试 ===")


def scene_d1b(self, url, *a, **kw):
    """三接口都说未登录，但**没有任何**风控信号 → 属于「疑似会话过期」，
    旧版就是在这种场景下预热重试的，这条能力必须保留。"""
    if url.rstrip("/") == "https://weibo.com":
        return FakeResp(200, "<html></html>")
    if "mymblog" in url or "getProfileFeed" in url:
        return FakeResp(200, NOT_LOGIN, {"ok": -100})
    if "api/config" in url:
        return FakeResp(200, '{"ok":0,"data":{"login":false}}',
                        {"ok": 0, "data": {"login": False}})
    return FakeResp(200, "", {})


_install(scene_d1b)
CALLS.clear()
wc.get_latest_posts("6996654711", COOKIE, page=1, count=10)
# warm_session 在有 uid 时访问的是 /u/<uid>，无 uid 时是首页，两种都算预热
home_hits = [u for u in CALLS if "/u/" in u or u.rstrip("/") == "https://weibo.com"]
check("普通失败仍会预热一次（能力未丢）", len(home_hits) >= 1,
      "%d 次：%s" % (len(home_hits), home_hits[:2]))

print("\n=== D1c · 抓取说未登录 → 必须同源复核 ===")


def scene_d1c(web_login=True):
    """抓取三接口全报未登录，但同源 profile/info 给出 web_login 的结论。"""
    def _get(self, url, *a, **kw):
        if url.rstrip("/") == "https://weibo.com":
            return FakeResp(200, MODERN_HOME)
        if "profile/info" in url:
            if web_login:
                return FakeResp(200, '{"ok":1}', {
                    "ok": 1, "data": {"user": {"id": "5189652182",
                                               "screen_name": "肆月Aperture"}}})
            return FakeResp(200, NOT_LOGIN, {"ok": -100})
        if "mymblog" in url or "getProfileFeed" in url:
            return FakeResp(200, NOT_LOGIN, {"ok": -100})
        if "api/config" in url:
            return FakeResp(200, '{"ok":0,"data":{"login":false}}',
                            {"ok": 0, "data": {"login": False}})
        return FakeResp(200, "", {})
    return _get


_install(scene_d1c(web_login=True))
d = wc.diagnose_account(COOKIE, probe_uid="6996654711", self_uid="5189652182")
check("同源说有效 → 不判失效", d.get("status") != "cookie_expired", d.get("status"))
check("说明是风控/限流而非失效",
      "同源" in (d.get("message") or ""), d.get("message"))

_install(scene_d1c(web_login=False))
d = wc.diagnose_account(COOKIE, probe_uid="6996654711", self_uid="5189652182")
check("同源也判未登录 → 才定 cookie_expired",
      d.get("status") == "cookie_expired", d.get("status"))

print("\n=== D2 · 全局请求节流 ===")
wc._THROTTLE["next_at"] = 0.0
wc._THROTTLE["paused_until"] = 0.0
wc._THROTTLE["interval"] = 0.2
t0 = time.time()
for _ in range(3):
    wc._throttle_acquire("https://weibo.com/ajax/statuses/mymblog")
dt = time.time() - t0
check("微博接口被排队（3 次 ≥ 0.4s）", dt >= 0.4, "%.3fs" % dt)

wc._THROTTLE["next_at"] = time.time() + 5      # 人为制造「下一个可用时刻在 5s 后」
wc._THROTTLE["interval"] = 0.0
t0 = time.time()
wc._throttle_acquire("https://wx1.sinaimg.cn/large/a.jpg")
dt_img = time.time() - t0
check("图床不参与节流", dt_img < 0.05, "%.3fs" % dt_img)

wc._THROTTLE["next_at"] = 0.0
wc._THROTTLE["paused_until"] = time.time() + 0.35
wc._THROTTLE["interval"] = 0.0
t0 = time.time()
wc._throttle_acquire("https://weibo.com/ajax/profile/info")
dt_pause = time.time() - t0
check("风控冷却期内请求会等待", dt_pause >= 0.3, "%.3fs" % dt_pause)
wc._THROTTLE["paused_until"] = 0.0
wc._THROTTLE["interval"] = 0.45

st = wc.throttle_stats()
check("stat 结构完整",
      set(st.keys()) == {"interval", "pauses", "waits", "cooling"}, st)

print("\n=== D4 · 退出落盘与 profile 观测（源码级核验） ===")
with open(os.path.join(SRC, "_app_v117.py"), encoding="utf-8") as f:
    app_src = f.read()
check("存在 profile Cookie 探针", "_wv_cookie_probe" in app_src)
check("启动前采样一次", '_wv_cookie_probe("启动前")' in app_src)
check("窗口关闭后采样一次", '_wv_cookie_probe("窗口关闭后")' in app_src)
check("硬退出前采样一次", '_wv_cookie_probe("硬退出前")' in app_src)
# os._exit 之前必须有等待：顺序不能反，反了等于没改
_m = re.search(r"time\.sleep\(([\d.]+)\)\s*\n\s*_wv_cookie_probe\(\"硬退出前\"\)", app_src)
check("os._exit 前有等待窗口", bool(_m) and float(_m.group(1)) >= 0.2,
      _m.group(1) if _m else "未找到")
_exit_at = app_src.rfind("os._exit(0)")
check("等待发生在 os._exit 之前", _m is not None and _m.start() < _exit_at)

print("\n=== 保活留痕（SUB 到期前后对比） ===")
with open(os.path.join(SRC, "scheduler.py"), encoding="utf-8") as f:
    sch_src = f.read()
check("存在 _sub_expires 工具", "def _sub_expires" in sch_src)
check("保活记录 SUB 到期前后", "保活留痕" in sch_src)
check("保活会回填 wb_uid", "set_wb_uid" in sch_src)

print("\n=== D3b · 首页外壳含 passport/login.php 时不得误判成登录墙（v1.6.15） ===")
# 新版 weibo.com 首页是 7KB JS 外壳，内联配置里带着 passport 的 SSO 地址。
# 旧顺序先用 _looks_not_login() 扫正文，命中 "passport" 就返回空 uid ——
# 于是补了新正则也没用，uid 永远为空，同源实测永远跳过（11-21 报告就是这个形态）。
SHELL_HOME = (
    '<html><head><script>window.$CONFIG={"uid":"5189652182","nick":"肆月Aperture",'
    '"loginUrl":"https://passport.weibo.com/sso/signin?entry=miniblog",'
    '"help":"https://weibo.com/login.php"};</script></head><body></body></html>'
)
check("对照组：正文确实含 passport（旧口径会误判）",
      wc._looks_not_login(200, SHELL_HOME) is True)
_install(lambda self, url, *a, **kw: FakeResp(200, SHELL_HOME))
uid_shell, _ = wc.get_self_uid(COOKIE)
check("外壳含 passport 仍能解析出 uid", uid_shell == "5189652182", uid_shell)

# 真·登录墙：落地 URL 被踢到 passport/login → 必须返回空
class FakeRespUrl(FakeResp):
    def __init__(self, status, text, url):
        FakeResp.__init__(self, status, text)
        self.url = url


_install(lambda self, url, *a, **kw: FakeRespUrl(
    200, "<html>请先登录</html>", "https://passport.weibo.com/sso/signin"))
uid_wall, _ = wc.get_self_uid(COOKIE)
check("落地到 passport → 判定为登录墙，返回空 uid", uid_wall == "", repr(uid_wall))

# _homepage_is_wall 的口径：URL 特征优先，正文只认人话
_r_ok = FakeResp(200, SHELL_HOME)
_r_ok.url = "https://weibo.com/"
check("_homepage_is_wall 不把外壳当登录墙", wc._homepage_is_wall(_r_ok) is False)
_r_bad = FakeResp(200, "<html></html>")
_r_bad.url = "https://passport.weibo.com/sso/signin"
check("_homepage_is_wall 认 URL 特征", wc._homepage_is_wall(_r_bad) is True)
_r_302 = FakeResp(302, "")
_r_302.url = "https://weibo.com/"
check("_homepage_is_wall 认 302", wc._homepage_is_wall(_r_302) is True)

print("\n=== D7 · warm_session 不得因误判登录墙而跳过续期（v1.6.15） ===")
# 旧实现：_looks_not_login 命中 "passport" → 直接 return cookie_expired，
# 连 merge_cookie_entries 都没执行 → Set-Cookie 里的续期结果全丢。
# 这正是「用着用着刚好 7 天掉线」的机制性原因。
_install(lambda self, url, *a, **kw: FakeResp(200, SHELL_HOME))
_w = wc.warm_session(COOKIE, "5189652182")
check("外壳页预热成功（不再误判失效）", _w.get("ok") is True, _w.get("status"))
check("状态为 ok", _w.get("status") == "ok", _w.get("status"))

_install(lambda self, url, *a, **kw: FakeRespUrl(
    200, "<html>请先登录</html>", "https://passport.weibo.com/sso/signin"))
_w2 = wc.warm_session(COOKIE, "5189652182")
check("真登录墙仍正确判失效", _w2.get("status") == "cookie_expired", _w2.get("status"))

print("\n" + "=" * 60)
if OK:
    print("ALL_D1D4_TESTS_PASS")
else:
    print("失败项：%s" % "、".join(FAILED))
    sys.exit(1)
