# -*- coding: utf-8 -*-
"""验证「新版首页外壳含 passport/login.php → 被判登录墙」这一根因。

直接抓 https://weibo.com/ 的匿名首页（未登录也能拿到同一份 JS 外壳），
检查正文里是否含登录墙特征词、旧口径会不会误判。
"""
import sys
sys.path.insert(0, "_src_v151k")
import requests
from weibo_client import _looks_not_login, _homepage_is_wall, _SELF_UID_PATTERNS
import re

s = requests.Session()
s.trust_env = False
s.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Upgrade-Insecure-Requests": "1",
})
r = s.get("https://weibo.com/", timeout=15, allow_redirects=True)
t = r.text or ""
print("HTTP", r.status_code, "bytes", len(t), "final", r.url)
print("含 'passport' :", "passport" in t)
print("含 'login.php':", "login.php" in t)
print("含 '登录'      :", "登录" in t)
print("旧口径 _looks_not_login(200, text) =", _looks_not_login(200, t))
print("新口径 _homepage_is_wall(r)        =", _homepage_is_wall(r))
hits = []
for i, pat in enumerate(_SELF_UID_PATTERNS):
    m = re.search(pat, t)
    if m:
        hits.append((i + 1, m.group(1)))
print("命中的 uid 规则（匿名页本就不该有 uid）:", hits)
# 打印 passport 出现处的上下文（判断是不是在 JS 外壳里）
for m in re.finditer("passport|login\\.php", t):
    a = max(0, m.start() - 60)
    print("  …%s…" % t[a:m.end() + 40].replace("\n", " ")[:150])
    break
