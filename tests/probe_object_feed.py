# -*- coding: utf-8 -*-
"""按对象核查「拉取微博是否真的为空」。

背景（2026-11-22）：真机连续 25 小时日志里，`uid=6872830640`（一色雨）每一轮
都返回 `判定=empty`，累计 71 次告警，占全部 HTTP 告警的 98%。这有两种可能：

  A. 该账号确实没有公开微博（正常，只是日志太吵）
  B. 我们的拉取链路对**某些账号**失效（真 BUG）

两者在日志里长得完全一样（`ok=1` + 空 list），必须换证据：直接拿着真实 Cookie
去打同一个接口，把「该账号」与「已知正常的账号」并排比一次。

判读方式：
  - 若正常账号有数据、异常账号仍为空 → 判 A，压日志即可；
  - 若**两个都空** → 说明 Cookie 或接口整体变了，与账号无关 → 判 B。

用法：
    python tests/probe_object_feed.py
    python tests/probe_object_feed.py 6872830640 8004547585
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "_src_v151k")
sys.path.insert(0, SRC)

import weibo_client as wc          # noqa: E402
from account_manager import AccountManager  # noqa: E402

# 默认：异常对象 + 一个今天确实转发成功的对象（对照组）
DEFAULT_UIDS = [
    ("6872830640", "一色雨", "连续 71 轮判空"),
    ("8004547585", "杏仁布丁仮装", "对照组（10-23 转发成功）"),
]


def pick_cookie():
    m = AccountManager()
    accs = m.list_accounts() if hasattr(m, "list_accounts") else m.accounts
    if not accs:
        raise SystemExit("accounts.json 里没有账户")
    # 取最近使用过的那个
    acc = sorted(accs, key=lambda a: a.get("last_used") or "",
                 reverse=True)[0]
    ck = m.get_cookie(acc.get("id"))
    if not ck:
        raise SystemExit("取不到 Cookie（账户 %s）" % acc.get("name"))
    return acc.get("name") or acc.get("id"), acc.get("wb_uid") or "", ck


def main():
    args = sys.argv[1:]
    if args:
        cases = [(u, "", "命令行指定") for u in args]
    else:
        cases = DEFAULT_UIDS

    name, self_uid, cookie = pick_cookie()
    print("账户=%s wb_uid=%s cookie_len=%d" % (name, self_uid or "(空)", len(cookie)))
    print("=" * 78)

    bad = 0
    for uid, screen, note in cases:
        try:
            res, errs, flags = wc._fetch_once(uid, cookie, 1, 20)
        except Exception as e:
            print("%-12s %-14s EXC %r" % (uid, screen or "-", e))
            bad += 1
            continue
        posts = (res or {}).get("posts") or []
        ok = bool((res or {}).get("ok"))
        print("%-12s %-14s ok=%-5s 条数=%-3d flags=%s  %s"
              % (uid, screen or "-", ok, len(posts), sorted(flags) or "-", note))
        if posts:
            for p in posts[:3]:
                t = (p.get("text") or "").replace("\n", " ")[:46]
                print("      · %s  %s" % (p.get("id"), t))
        else:
            # 空的时候必须给出接口原始回执，否则无法判断是「真空」还是「被墙」
            for e in (errs or [])[:2]:
                print("      ! %s" % str(e)[:150])
        if not ok:
            bad += 1

    print("=" * 78)
    if bad:
        print("有 %d 个对象拉取失败（需人工判读上面的原因串）" % bad)
        return 1
    print("全部对象均返回 ok=True")
    return 0


if __name__ == "__main__":
    sys.exit(main())
