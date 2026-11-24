# -*- coding: utf-8 -*-
"""v1.6.18 · 内置浏览器取 Cookie 不再阻塞/异常 —— 可证伪验证。

覆盖三件事：
  A. `_read_login_cookies()` 走平台层直取，**完全不会**碰到 `Window.get_cookies()`
     （那个挂 `@_loaded_call`，登录页前 30 秒必抛 Main window failed to start）。
  B. 平台层抛异常时才回退，且回退失败给出人能看懂的提示、不白等 20 秒。
  C. `_skip_bridge_inject_for_login_window()` 只跳过「child_ 子窗 + 微博 URL」，
     主窗（master）与其它 URL 一律走原逻辑 —— 必须挂对照组，否则「跳过」不可解读。

用法：python tests/probe_login_cookie.py
退出码 0 = 全过。
"""
import os
import re
import sys
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "_src_v151k"))

SRC = open(os.path.join(BASE, "_src_v151k", "api_bridge.py"), encoding="utf-8").read()

PASS = FAIL = 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s%s" % (name, ("  " + extra) if extra else ""))
    else:
        FAIL += 1
        print("  FAIL  %s%s" % (name, ("  " + extra) if extra else ""))


# ---------------------------------------------------------------- A / B
print("[A] capture_login_cookies 不再走 @_loaded_call")


def _grab(src, name, limit=2600):
    """取某个方法的方法体（到下一个同缩进 def 为止）。"""
    m = re.search(r"\n    def %s\(self[^)]*\):(.*?)(?=\n    def )" % name, src, re.S)
    return m.group(1) if m else ""


_body = _grab(SRC, "capture_login_cookies")
# 只看真代码，注释里提到这个方法名是正常的（说明「为什么不再调它」）
_code_lines = [ln for ln in _body.splitlines() if not ln.strip().startswith("#")]
_code = "\n".join(_code_lines)
ok("capture_login_cookies 里不再直接调 login_window.get_cookies()",
   bool(_body) and "self.login_window.get_cookies()" not in _code)
ok("capture_login_cookies 改调 _read_login_cookies()",
   "_read_login_cookies()" in _code)
ok("读取结果留痕 WV_COOKIE_READ", "WV_COOKIE_READ" in _body)

_cand = re.search(r"cookies = self\.login_window\.get_cookies\(\)", SRC)
ok("全文件已无 self.login_window.get_cookies() 调用点", _cand is None)


# 用假窗口跑真方法：不走 __init__，只需要 self.login_window
import api_bridge  # noqa: E402


class FakeEvents(object):
    def __init__(self, loaded_set):
        self.loaded = types.SimpleNamespace(_set=loaded_set)


class FakeWindow(object):
    """模拟 pywebview 的 Window。

    `get_cookies()` 复刻官方实现的关键行为：`loaded` 未 set 就**等满 20 秒**再抛
    Main window failed to start。这里把 20 秒缩放成 0.05 秒只为让探针跑得快 ——
    判据不是「等了多久」，而是**有没有被调用**。
    """

    def __init__(self, gui_result=None, gui_exc=None, loaded_set=False, official_exc=None):
        self.uid = "child_abc123"
        self.calls = {"gui": 0, "official": 0}
        self._gui_result = gui_result
        self._gui_exc = gui_exc
        self._official_exc = official_exc
        self._loaded_set = loaded_set
        self.gui = types.SimpleNamespace(get_cookies=self._gui_get_cookies)

    def _gui_get_cookies(self, uid):
        self.calls["gui"] += 1
        if self._gui_exc:
            raise self._gui_exc
        return list(self._gui_result or [])

    def get_cookies(self):
        self.calls["official"] += 1
        if self._official_exc is not None:
            raise self._official_exc
        return ["official"]


class Cookie(object):
    pass


def mk_cookie(name, value):
    """伪造 pywebview 的 SimpleCookie 元素（本方法只做 len/遍历，结构够用即可）。"""
    m = types.SimpleNamespace(key=name, value=value, get=lambda k, d=None: {
        "domain": ".weibo.com", "path": "/", "expires": "", "secure": True,
        "httponly": True}.get(k, d))
    c = types.SimpleNamespace(values=lambda: [m])
    return c


bridge = object.__new__(api_bridge.Api)

print("[A2] 平台层直取：官方接口一次都不许被调")
w = FakeWindow(gui_result=[mk_cookie("SUB", "_2AkM")], loaded_set=False)
bridge.login_window = w
r = bridge._read_login_cookies()
ok("ok=True", r.get("ok") is True, str({k: v for k, v in r.items() if k != "cookies"}))
ok("path=direct", r.get("path") == "direct", r.get("path"))
ok("拿到 1 条 cookie", len(r.get("cookies") or []) == 1)
ok("官方 get_cookies() 调用次数 = 0（这是本次修复的核心）",
   w.calls["official"] == 0, "official=%d" % w.calls["official"])
ok("返回耗时 < 500ms（不再白等 20 秒）", r.get("cost_ms", 99999) < 500,
   "%sms" % r.get("cost_ms"))

print("[A3] 对照组：如果走官方接口，就会抛 Main window failed to start")
w2 = FakeWindow(gui_result=[], official_exc=RuntimeError("Main window failed to start"))
_trapped = None
try:
    w2.get_cookies()
except Exception as _e:  # noqa: BLE001
    _trapped = _e
ok("（对照组）走官方接口确实会抛出用户看到的那个异常",
   isinstance(_trapped, RuntimeError) and "Main window failed to start" in str(_trapped),
   repr(_trapped))

print("[B] 平台层失败才回退，且不炸")
w3 = FakeWindow(gui_result=None, gui_exc=AttributeError("'NoneType' has no attribute 'CoreWebView2'"),
                official_exc=RuntimeError("Main window failed to start"))
bridge.login_window = w3
r3 = bridge._read_login_cookies()
ok("回退也失败 → ok=False", r3.get("ok") is False)
ok("错误信息可读（含『稍等 2 秒再点一次』）",
   "稍等" in str(r3.get("error")), str(r3.get("error"))[:70])
ok("两跳都试过了", w3.calls["gui"] == 1 and w3.calls["official"] == 1)

w4 = FakeWindow(gui_result=None, gui_exc=AttributeError("boom"), loaded_set=True)
bridge.login_window = w4
r4 = bridge._read_login_cookies()
ok("平台层失败但官方接口可用 → path=fallback", r4.get("path") == "fallback", str(r4.get("path")))

print("[B2] 未登录（拿到 0 条）不阻塞、给明确提示")
w5 = FakeWindow(gui_result=[], loaded_set=True)
bridge.login_window = w5
r5 = bridge._read_login_cookies()
ok("0 条也是 ok=True，交给上层判空", r5.get("ok") is True and r5.get("cookies") == [])
ok("0 条时同样不碰官方接口", w5.calls["official"] == 0)

# ---------------------------------------------------------------- C
print("[C] 登录窗跳过桥注入：只跳过 child_ + 微博")


# 伪造 webview.platforms.edgechromium，避免真去加载 pythonnet/WinForms
class _FakeEdgeChrome(object):
    def __init__(self, uid, url):
        self.pywebview_window = types.SimpleNamespace(uid=uid)
        self.url = url
        self.called = 0

    def _schedule_inject(self):
        self.called += 1


pkgs = ["webview", "webview.platforms"]
for p in pkgs:
    if p not in sys.modules:
        m = types.ModuleType(p)
        m.__path__ = []
        sys.modules[p] = m
fake_ec = types.ModuleType("webview.platforms.edgechromium")
fake_ec.EdgeChrome = _FakeEdgeChrome
_seen = []
fake_ec._injlog = lambda msg: _seen.append(msg)
sys.modules["webview.platforms.edgechromium"] = fake_ec
setattr(sys.modules["webview.platforms"], "edgechromium", fake_ec)

bridge._skip_bridge_inject_for_login_window()
ok("打上补丁", getattr(_FakeEdgeChrome._schedule_inject, "_wbb_skip", False) is True)

bridge._skip_bridge_inject_for_login_window()  # 幂等
ok("重复调用不会层层包装",
   getattr(_FakeEdgeChrome._schedule_inject, "_wbb_skip", False) is True)

# 对照组：主窗（master）必须照旧注入 —— 否则「跳过」就是坏的
m = _FakeEdgeChrome("master", "http://127.0.0.1:5123/index.html")
m._schedule_inject()
ok("主窗 master + index.html → 仍注入（对照组）", m.called == 1, "called=%d" % m.called)

c = _FakeEdgeChrome("child_ab12", "https://weibo.com/login.php")
c._schedule_inject()
ok("子窗 + weibo.com/login.php → 跳过", c.called == 0, "called=%d" % c.called)

c2 = _FakeEdgeChrome("child_cd34", "https://passport.weibo.cn/sso/login")
c2._schedule_inject()
ok("子窗 + weibo.cn → 跳过", c2.called == 0)

c3 = _FakeEdgeChrome("child_ef56", "https://example.com/x")
c3._schedule_inject()
ok("子窗 + 非微博 → 仍注入（不误伤）", c3.called == 1, "called=%d" % c3.called)

print("[C2] _inj.log 降噪：只丢 evaluate_js OK，保留注入链路")
fake_ec._injlog("evaluate_js OK len=2048 result='None'")
fake_ec._injlog("INJECT_TRY attempt=0")
fake_ec._injlog("evaluate_js TIMEOUT len=99")
ok("噪点行被丢掉", not any("evaluate_js OK" in m for m in _seen), str(_seen))
ok("INJECT_TRY 保留", any("INJECT_TRY" in m for m in _seen))
ok("evaluate_js TIMEOUT 保留（这是要看的那种）",
   any("TIMEOUT" in m for m in _seen), str(_seen))

print("\n%d passed, %d failed" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
