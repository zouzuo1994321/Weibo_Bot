# -*- coding: utf-8 -*-
"""v1.1.1 回归测试：验证「检测正常但抓取全挂」场景已被修复。

用 mock 构造用户日志里的真实失败签名：
  mymblog HTTP 200 ok=-100 跳转 login.php
  getProfileFeed HTTP 200 返回「新浪通行证」登录页
  m.container HTTP 432 空 body（风控）
旧逻辑：check_login 见 XSRF-TOKEN 即判「已登录」→ diagnose 输出 status=ok（假阳性）
新逻辑：应判 cookie_expired，并给出「请重新登录」的可操作提示。
"""
import os
import sys
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "src")
sys.path.insert(0, SRC)

import requests  # noqa: E402

# ---------- 构造用户遇到的真实响应 ----------
COOKIE = ("SUB=_2A25KabcSUB; SUBP=0033WrSXsubp; XSRF-TOKEN=Vj8QkLmNoPqRsTuV; "
          "ALF=1780000000; WBPSESS=abc")

NOT_LOGIN_JSON = '{"ok":-100,"url":"https://weibo.com/login.php?url=https%3A%2F%2Fweibo.com%2Fu%2F6996654711"}'
PASSPORT_HTML = ('\n\t\t<html>\n\t\t<head>\n\t\t<title>ÐÂÀËÍ¨ÐÐÖ¤</title>\n'
                 '\t\t<meta http-equiv="refresh" content="0; url=&#39;https://weibo.com/ajax/')


class FakeResp:
    def __init__(self, status=200, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def make_fake_get():
    """按 URL 返回对应的失败响应（模拟 Cookie 已失效的场景）。"""
    def _get(self, url, params=None, timeout=None, **kw):
        if "mymblog" in url:
            return FakeResp(200, NOT_LOGIN_JSON, {"ok": -100, "url": "https://weibo.com/login.php"})
        if "getProfileFeed" in url:
            return FakeResp(200, PASSPORT_HTML, None)
        if "getIndex" in url:
            return FakeResp(432, "", None)            # 风控
        if "profile/info" in url:
            return FakeResp(200, NOT_LOGIN_JSON, {"ok": -100, "url": "https://weibo.com/login.php"})
        if "api/config" in url:
            return FakeResp(200, '{"ok":0,"data":{"login":false}}', {"ok": 0, "data": {"login": False}})
        return FakeResp(200, "", None)
    return _get


requests.Session.get = make_fake_get()

import weibo_client as wc  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' · ' + extra) if extra else ''}")


print("\n=== 1. 登录墙 / 风控 判定 ===")
check("mymblog ok=-100 判定为未登录",
      wc._looks_not_login(200, NOT_LOGIN_JSON, {"ok": -100, "url": "https://weibo.com/login.php"}))
check("通行证 HTML 判定为未登录", wc._looks_not_login(200, PASSPORT_HTML))
check("HTTP 432 判定为风控", wc._looks_risk_control(432, ""))
check("HTTP 418 判定为风控", wc._looks_risk_control(418, ""))
check("普通 200 不误判为未登录",
      not wc._looks_not_login(200, '{"ok":1,"data":{"list":[]}}', {"ok": 1}))

print("\n=== 2. Cookie 结构检查（修复核心） ===")
check("含 SUB 视为持有登录凭证", wc._has_login_cookie(COOKIE))
check("只有 XSRF-TOKEN 不算已登录", not wc._has_login_cookie("XSRF-TOKEN=Vj8Q; other=1"))
check("get_st 在无 SUB 时返回空（不再假阳性）",
      wc.get_st("XSRF-TOKEN=Vj8QkLmNoPqRsTuV") == "")
check("get_st 在有 SUB 时返回令牌",
      wc.get_st(COOKIE) == "Vj8QkLmNoPqRsTuV")

print("\n=== 3. check_login 不再误判 ===")
r = wc.check_login(COOKIE)
check("失效 Cookie → ok=False", not r.get("ok"), f"status={r.get('status')}")
check("失效 Cookie → status=cookie_expired", r.get("status") == "cookie_expired")

print("\n=== 4. 抓取失败分类（用户日志场景） ===")
res = wc.get_latest_posts("6996654711", COOKIE, page=1, count=10)
check("抓取失败 ok=False", not res.get("ok"))
check("判定为 cookie_expired 或 risk_control",
      res.get("status") in ("cookie_expired", "risk_control"), f"status={res.get('status')}")
check("给出可读中文原因", bool(res.get("reason")), res.get("reason", ""))

print("\n=== 5. diagnose_account 与真实抓取一致 ===")
d = wc.diagnose_account(COOKIE, probe_uid="6996654711")
check("诊断不再是 ok（消除假阳性）", d.get("status") != "ok", f"status={d.get('status')}")
check("诊断给出重新登录指引", "重新登录" in (d.get("message") or ""), d.get("message", ""))

print("\n=== 5b. 跨域误判回归（用户二次反馈场景） ===")
# 场景：用户刚用内置浏览器登录拿到全新 Cookie。
# 网页端（同源）正常，但 m.weibo.cn 因 Cookie 不跨域而回答「未登录」。
# 旧版据此直接判 cookie_expired → 误报「请重新登录」。


def make_scene_get(web_body, web_json, mobile_body, mobile_json,
                   mymblog_body=None, mymblog_json=None):
    def _get(self, url, params=None, timeout=None, **kw):
        if "mymblog" in url:
            return FakeResp(200, mymblog_body or '{"ok":1}',
                            mymblog_json if mymblog_json is not None else
                            {"ok": 1, "data": {"list": [
                                {"mblog": {"id": "1", "text": "x"}}]}})
        if "profile/info" in url:
            return FakeResp(200, web_body, web_json)
        if "api/config" in url:
            return FakeResp(200, mobile_body, mobile_json)
        if "getIndex" in url:
            return FakeResp(200, '{"ok":0,"data":{"cards":[]}}', {"ok": 0, "data": {"cards": []}})
        return FakeResp(200, "", {})
    return _get


MOBILE_NOT_LOGIN = '{"ok":0,"data":{"login":false,"uid":null}}'

# 5b-1：网页端返回完整用户对象 → 必须判为已登录（移动端说什么都不影响）
requests.Session.get = make_scene_get(
    '{"ok":1,"data":{"user":{"id":"6996654711","name":"测试用户"}}}',
    {"ok": 1, "data": {"user": {"id": "6996654711", "name": "测试用户"}}},
    MOBILE_NOT_LOGIN, {"ok": 0, "data": {"login": False, "uid": None}})
r = wc.check_login(COOKIE)
check("网页端确认登录 → 不受移动端跨域影响", r.get("ok") and r.get("status") == "ok",
      f"status={r.get('status')}")

# 5b-2：网页端 ok=1 但昵称结构变了（解析不出用户对象）→ 仍应判为已登录
requests.Session.get = make_scene_get(
    '{"ok":1,"data":{"userInfo":{"screenName":"测试"}}}',
    {"ok": 1, "data": {"userInfo": {"screenName": "测试"}}},
    MOBILE_NOT_LOGIN, {"ok": 0, "data": {"login": False, "uid": None}})
r = wc.check_login(COOKIE)
check("网页端 ok=1 且 data 非空 → 判为已登录", r.get("ok"), f"status={r.get('status')}")

# 5b-3：网页端无明确结论 + 移动端「未登录」→ 只能是 unknown，绝不能是 cookie_expired
requests.Session.get = make_scene_get(
    '{"ok":0,"data":{}}', {"ok": 0, "data": {}},
    MOBILE_NOT_LOGIN, {"ok": 0, "data": {"login": False, "uid": None}})
r = wc.check_login(COOKIE)
check("跨域未登录不再误判 cookie_expired", r.get("status") != "cookie_expired",
      f"status={r.get('status')}")
check("降级为 unknown 交由抓取裁决", r.get("status") == "unknown")

# 5b-4：真正失效（网页端登录墙）仍必须判 cookie_expired
requests.Session.get = make_scene_get(
    NOT_LOGIN_JSON, {"ok": -100, "url": "https://weibo.com/login.php"},
    MOBILE_NOT_LOGIN, {"ok": 0, "data": {"login": False, "uid": None}})
r = wc.check_login(COOKIE)
check("真实失效仍正确判为 cookie_expired", r.get("status") == "cookie_expired")

# 5b-5：{"login":false} 不应被当成登录墙文本
check("login:false 不触发登录墙判定",
      not wc._looks_not_login(200, MOBILE_NOT_LOGIN,
                              {"ok": 0, "data": {"login": False}}))

# 5b-6：unknown + 抓取成功 → 最终诊断应为 ok（以实际抓取为准）
requests.Session.get = make_scene_get(
    '{"ok":0,"data":{}}', {"ok": 0, "data": {}},
    MOBILE_NOT_LOGIN, {"ok": 0, "data": {"login": False, "uid": None}})
d = wc.diagnose_account(COOKIE, probe_uid="6996654711")
check("unknown 时以真实抓取裁决 → ok", d.get("status") == "ok",
      f"status={d.get('status')} msg={d.get('message','')}")

print("\n=== 5c. profile/info 强制要求 uid（2026-09-14 日志场景） ===")
# 日志实测：web.ajax/profile/info → HTTP 400 · '{"ok":0,"message":"缺少必要参数"}'
HOME_HTML = ('<html><script>var $CONFIG = {};'
             "$CONFIG['uid']='6996654711';$CONFIG['nick']='茶涧三沫';</script></html>")
NEED_UID = '{"ok":0,"message":"缺少必要参数"}'


def make_uid_scene(self_uid_in_home=True, with_uid_ok=True):
    def _get(self, url, params=None, timeout=None, **kw):
        if url.rstrip("/") == "https://weibo.com":
            return FakeResp(200, HOME_HTML if self_uid_in_home else "<html></html>", None)
        if "profile/info" in url:
            has_uid = bool(params and params.get("uid"))
            if has_uid and with_uid_ok:
                return FakeResp(200, '{"ok":1}', {
                    "ok": 1, "data": {"user": {"id": "6996654711", "name": "茶涧三沫"}}})
            return FakeResp(400, NEED_UID, {"ok": 0, "message": "缺少必要参数"})
        if "api/config" in url:
            return FakeResp(200, MOBILE_NOT_LOGIN, {"ok": 0, "data": {"login": False}})
        if "mymblog" in url:
            return FakeResp(200, '{"ok":1}', {"ok": 1, "data": {"list": [
                {"mblog": {"id": "1", "text": "x"}}]}})
        return FakeResp(200, "", {})
    return _get


requests.Session.get = make_uid_scene()
r = wc.check_login(COOKIE)
check("首页解析出自身 UID 后检测通过", r.get("status") == "ok", f"status={r.get('status')}")
check("正确回填账户昵称", r.get("screen_name") == "茶涧三沫", f"name={r.get('screen_name')}")
check("正确回填账户 UID", r.get("uid") == "6996654711")
check("自愈步骤已记录", any("首页解析到账户自身" in s for s in r.get("steps", [])))

# 不带 uid 时接口报 400；若只有监控 UID 兜底 → 能确认登录，但不能把对方昵称当账户昵称
requests.Session.get = make_uid_scene(self_uid_in_home=False)
r2 = wc.check_login(COOKIE, fallback_uid="1281071463")
check("仅用监控 UID 也能确认登录", r2.get("status") == "ok", f"status={r2.get('status')}")
check("探测模式不回填昵称", r2.get("screen_name") == "", f"name={r2.get('screen_name')!r}")
check("探测模式标记 probe_only", r2.get("probe_only") is True)

print("\n=== 5d. CA 证书包自愈 ===")
import ca_bundle  # noqa: E402

status_ok, path_ok = ca_bundle.ensure_ca_bundle()
check("正常环境证书包可用", status_ok == "ok" and bool(path_ok), f"{status_ok} {path_ok}")

# 模拟 certifi 路径失效（临时目录被清理）：应切到备份
import tempfile  # noqa: E402
tmpdir = tempfile.mkdtemp()
backup = os.path.join(tmpdir, "cacert.pem")
with open(os.path.join(tmpdir, "cacert.pem"), "w", encoding="utf-8") as f:
    f.write("fake-ca")
_orig_where = wc.__dict__.get("_orig_where")


class _FakeCertifi:
    @staticmethod
    def where():
        return os.path.join(tmpdir, "__missing__", "cacert.pem")


import certifi as _real_certifi  # noqa: E402
_bak = _real_certifi.where
_real_certifi.where = _FakeCertifi.where
try:
    ca_bundle._backup_path = lambda: backup
    st, p = ca_bundle.ensure_ca_bundle()
    check("证书包失效时切到备份", st.startswith("已改用备份证书包"), st)
    check("备份路径已写入环境变量",
          os.environ.get("REQUESTS_CA_BUNDLE") == backup)
finally:
    _real_certifi.where = _bak
    os.environ.pop("REQUESTS_CA_BUNDLE", None)
    os.environ.pop("SSL_CERT_FILE", None)
    os.environ.pop("CURL_CA_BUNDLE", None)

print("\n=== 6b. Cookie 续期（会话保活） ===")


class FakeJarSession:
    """模拟 Session.cookies：服务端通过 Set-Cookie 续期了 SUB / ALF。"""

    def __init__(self, d):
        self._d = d

    @property
    def cookies(self):
        outer = self

        class _Jar:
            @staticmethod
            def get_dict():
                return outer._d
        return _Jar()


new_sub = "SUB=_2A25KxyzRENEWED"
merged = wc._merge_cookies(COOKIE, FakeJarSession({"SUB": "_2A25KxyzRENEWED",
                                                   "ALF": "1799999999",
                                                   "Ugrow-G0": "tracking"}))
check("续期后 SUB 被更新", "_2A25KxyzRENEWED" in merged)
check("续期后 ALF 被更新", "ALF=1799999999" in merged)
check("无关追踪 Cookie 不被写入", "Ugrow-G0" not in merged)
check("原有 XSRF-TOKEN 被保留", "XSRF-TOKEN=Vj8QkLmNoPqRsTuV" in merged)
check("无变化时不改写 Cookie",
      wc._merge_cookies(COOKIE, FakeJarSession({"SUB": "_2A25KabcSUB"})) == COOKIE)
check("Cookie 解析/序列化可逆",
      wc._cookie_dict(wc._cookie_str(wc._cookie_dict(COOKIE))) == wc._cookie_dict(COOKIE))

print("\n=== 7. 成功路径仍然正常 ===")
def fake_ok_get(self, url, params=None, timeout=None, **kw):
    if "mymblog" in url:
        body = {"ok": 1, "data": {"list": [
            {"mblog": {"id": "5100000000000001", "mblogid": "AbcD123",
                       "text": "测试微博正文", "pic_infos": {}}}]}}
        return FakeResp(200, '{"ok":1}', body)
    if "profile/info" in url:
        return FakeResp(200, '{"ok":1}', {"ok": 1, "data": {"user": {"id": "1", "name": "测试用户"}}})
    return FakeResp(200, "", {})


requests.Session.get = fake_ok_get
res2 = wc.get_latest_posts("123", COOKIE, page=1, count=5)
check("正常接口解析成功", res2.get("ok") and len(res2.get("posts", [])) == 1,
      f"posts={len(res2.get('posts', []))}")
d2 = wc.diagnose_account(COOKIE, probe_uid="123")
check("正常账户诊断 status=ok", d2.get("status") == "ok", f"status={d2.get('status')}")

print("\n结果：" + ("全部通过 ✔" if ok else "存在失败 ✘"))
sys.exit(0 if ok else 1)
