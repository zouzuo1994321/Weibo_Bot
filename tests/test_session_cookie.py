# -*- coding: utf-8 -*-
"""验证：Cookie 既通过请求头发出，又灌入 CookieJar 以便接收 Set-Cookie 续期。"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))

COOKIE = ("SUB=_2A25KabcSUB; SUBP=0033WrSXsubp; XSRF-TOKEN=Vj8QkLmNoPqRsTuV; ALF=1780000000")

received = {}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        received["cookie"] = self.headers.get("Cookie", "")
        # 模拟微博下发续期 Cookie
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "SUB=_2A25KxyzRENEWED; Path=/")
        self.send_header("Set-Cookie", "Ugrow-G0=tracking; Path=/")
        self.end_headers()
        self.wfile.write(b'{"ok":1}')

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

import weibo_client as wc  # noqa: E402

s = wc._session(COOKIE, referer=f"http://127.0.0.1:{port}/")
r = s.get(f"http://127.0.0.1:{port}/x", timeout=5)
sent = received.get("cookie", "")

ok = True


def check(name, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' · ' + extra) if extra else ''}")


print("\n=== Session Cookie 传输与续期 ===")
check("SUB 已发出", "SUB=_2A25KabcSUB" in sent, sent[:80])
check("XSRF-TOKEN 已发出", "XSRF-TOKEN=Vj8QkLmNoPqRsTuV" in sent)
check("ALF 已发出", "ALF=1780000000" in sent)
merged = wc._merge_cookies(COOKIE, s)
check("Set-Cookie 续期被捕获", "_2A25KxyzRENEWED" in merged, merged)
check("无关追踪 Cookie 未混入", "Ugrow-G0" not in merged)

srv.shutdown()
print("\n结果：" + ("全部通过 ✔" if ok else "存在失败 ✘"))
sys.exit(0 if ok else 1)
