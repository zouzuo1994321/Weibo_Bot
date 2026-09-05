# -*- coding: utf-8 -*-
"""
微博 HTTP 客户端（基于移动端 m.weibo.cn 公开接口 + 网页端转发接口）。
说明：微博接口可能随官方策略调整，如遇失效请按源码注释位置更新对应 URL / 参数。
所有请求均使用用户自己登录后获取的 Cookie，仅用于个人自动化，请遵守微博平台规则。
"""
import html
import json
import os
import re
import time

import requests
from logger import warn

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
M_BASE = "https://m.weibo.cn"
WEB_BASE = "https://weibo.com"

TAG_RE = re.compile(r"<[^>]+>")
IMG_RE = re.compile(r"https?://[^\s\"'<>]+?(?:jpg|jpeg|png|gif|webp)", re.I)


def _session(cookie: str, referer: str = None) -> requests.Session:
    s = requests.Session()
    if referer is None:
        referer = f"{M_BASE}/"
    s.headers.update({
        "User-Agent": UA,
        "Cookie": cookie,
        "Referer": referer,
    })
    return s


def clean_text(raw: str) -> str:
    """去除微博正文 HTML 标签，还原实体。"""
    if not raw:
        return ""
    txt = TAG_RE.sub("", raw)
    txt = html.unescape(txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def _normalize_image_url(url: str) -> str:
    """补全微博图片 URL 协议头并清理参数。"""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    # 去除可能干扰下载的查询参数（保留原图尺寸参数意义不大，且微博图床常不带参数）
    url = url.split("?")[0]
    return url


def extract_images(raw_html: str, pics_field=None) -> list:
    """从微博内容或 pics 字段提取图片直链（优先 large 大图）。"""
    imgs = []
    if pics_field:
        for p in pics_field:
            if not isinstance(p, dict):
                continue
            # 优先取 large / bmiddle 大图，其次 thumbnail / 普通 url
            url = (p.get("large", {}).get("url")
                   or p.get("bmiddle", {}).get("url")
                   or p.get("url")
                   or p.get("middle", {}).get("url"))
            if url:
                imgs.append(_normalize_image_url(url))
    if not imgs and raw_html:
        imgs = [_normalize_image_url(u) for u in IMG_RE.findall(raw_html)]
    # 去重并过滤表情小图
    seen, out = set(), []
    for u in imgs:
        if not u or u in seen:
            continue
        seen.add(u)
        if "emoji" in u or "static" in u:
            continue
        out.append(u)
    return out


def _safe_json(r):
    """安全解析 JSON 响应，失败返回 None。"""
    try:
        return r.json()
    except Exception:
        return None


def _extract_feed(j):
    """从 weibo.com 多种返回结构中提取 mblog 列表（兼容 data 为 dict/list/字符串）。"""
    if not isinstance(j, dict):
        return None
    data = j.get("data")
    # weibo.com ajax 接口常把 data 序列化为 JSON 字符串
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("list", "data", "statuses", "cards"):
            v = data.get(key)
            if isinstance(v, list):
                return v
    return None


def _is_pinned(mb: dict) -> bool:
    """判断一条微博是否为置顶（不再计入「最新微博」与转发挑选）。"""
    if mb.get("isTop"):
        return True
    if mb.get("is_pinned"):
        return True
    title = mb.get("title")
    if isinstance(title, dict):
        txt = str(title.get("text", ""))
    elif isinstance(title, str):
        txt = title
    else:
        txt = ""
    if "置顶" in txt:
        return True
    # 部分接口用 mblog 的 card_type/显示标记
    if str(mb.get("card_type", "")) == "11" and "置顶" in str(mb.get("itemid", "")):
        return True
    return False


def _build_posts(items, count):
    """把微博对象列表统一转换为内部 post 结构（兼容 card 包裹与直接 mblog）。"""
    posts = []
    for it in items:
        if not isinstance(it, dict):
            continue
        mb = it.get("mblog", it)  # 兼容移动端 card 包裹结构
        if not isinstance(mb, dict):
            continue
        raw = mb.get("text", "")
        pics = mb.get("pics") or mb.get("pic_ids") or []
        posts.append({
            "id": str(mb.get("id") or mb.get("mid") or mb.get("mblogid")),
            "bid": mb.get("mblogid") or mb.get("bid"),
            "text": clean_text(raw),
            "text_raw": raw,
            "created_at": mb.get("created_at", ""),
            "images": extract_images(raw, pics if isinstance(pics, list) else None),
            "reposts_count": mb.get("reposts_count", 0),
            "comments_count": mb.get("comments_count", 0),
            "attitudes_count": mb.get("attitudes_count", 0),
            "is_top": _is_pinned(mb),
        })
        if len(posts) >= count:
            break
    return posts


def _find_user_obj(d):
    """从 profile/info 的 `data` 中取出『用户对象』。

    兼容微博当前两种返回结构：
      - 扁平：data 自身即含 name / screen_name / id
      - 嵌套：data.user / data.userInfo / data.data.user 含上述字段
    返回 dict（找不到时为空 dict）。
    """
    if isinstance(d, str):
        d = _safe_json(d) or {}
    if not isinstance(d, dict):
        return {}
    candidates = [d]
    for k in ("user", "userInfo", "data"):
        v = d.get(k)
        if isinstance(v, dict):
            candidates.append(v)
            vv = v.get("user")
            if isinstance(vv, dict):
                candidates.append(vv)
    for c in candidates:
        if isinstance(c, dict) and (c.get("screen_name") or c.get("name") or c.get("id")):
            return c
    return {}


def _pick_avatar(user_obj: dict) -> str:
    """从用户对象中挑选最佳头像 URL（优先高清）。"""
    if not isinstance(user_obj, dict):
        return ""
    for k in ("avatar_hd", "profile_image_url", "avatar_large", "avatar300", "avatar180", "avatar50"):
        v = user_obj.get(k)
        if v and isinstance(v, str):
            return v
    return ""


def get_user_info(uid: str, cookie: str) -> dict:
    """通过 UID 获取微博昵称等基本信息。

    优先使用网页端接口（Cookie 本就来自 weibo.com 登录，同源最稳），
    移动端接口作为兜底。
    """
    s = _session(cookie, referer=f"{WEB_BASE}/")
    s.headers.update({"X-Requested-With": "XMLHttpRequest"})
    try:
        r = s.get(f"{WEB_BASE}/ajax/profile/info", params={"uid": uid}, timeout=15)
        j = _safe_json(r)
        d = j.get("data") if isinstance(j, dict) else None
        u = _find_user_obj(d)
        if u:
            return {
                "ok": True,
                "uid": uid,
                "screen_name": u.get("name") or u.get("screen_name", ""),
                "avatar_url": _pick_avatar(u),
                "description": u.get("description", ""),
                "followers": u.get("followers_count", 0),
            }
    except Exception:
        pass
    # 兜底：移动端接口
    try:
        s2 = _session(cookie)
        r2 = s2.get(f"{M_BASE}/api/container/getIndex", params={"type": "uid", "value": uid}, timeout=15)
        data = r2.json().get("data", {})
        user = data.get("userInfo", {})
        if not user:
            for c in data.get("cards", []):
                if c.get("card_type") == "11" or "user" in c:
                    user = c.get("user", {})
                    break
        if user.get("screen_name"):
            return {
                "ok": True,
                "uid": uid,
                "screen_name": user.get("screen_name", ""),
                "avatar_url": _pick_avatar(user),
                "description": user.get("description", ""),
                "followers": user.get("followers_count", 0),
            }
    except Exception:
        pass
    return {"ok": False, "error": "未获取到用户信息（可能 Cookie 失效或无权限）"}


def get_latest_posts(uid: str, cookie: str, page: int = 1, count: int = 10) -> dict:
    """获取该 UID 最新微博列表。

    优先使用网页端 /ajax/statuses/mymblog（Cookie 同源），再尝试 getProfileFeed，
    最后以移动端 container 兜底。返回中附带 http_summary 用于前端/日志诊断。
    """
    err_parts = []

    # 策略 1：网页端 /ajax/statuses/mymblog
    try:
        s = _session(cookie, referer=f"{WEB_BASE}/u/{uid}")
        s.headers.update({
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        })
        r = s.get(f"{WEB_BASE}/ajax/statuses/mymblog", params={
            "uid": uid, "page": page, "feature": 0}, timeout=15)
        j = _safe_json(r)
        feed = _extract_feed(j)
        if feed:
            posts = _build_posts(feed, count)
            if posts:
                return {"ok": True, "posts": posts, "page": page}
        # 未拿到帖子，记录诊断信息
        ok_flag = j.get("ok") if isinstance(j, dict) else None
        err_parts.append(f"mymblog HTTP {r.status_code}, ok={ok_flag}, "
                         f"body={r.text[:120]!r}")
    except Exception as e:
        err_parts.append(f"mymblog exc: {e}")

    # 策略 2：网页端 /ajax/profile/getProfileFeed
    try:
        s = _session(cookie, referer=f"{WEB_BASE}/u/{uid}")
        s.headers.update({"X-Requested-With": "XMLHttpRequest"})
        r = s.get(f"{WEB_BASE}/ajax/profile/getProfileFeed", params={
            "uid": uid, "page": page, "feature": 0}, timeout=15)
        j = _safe_json(r)
        feed = _extract_feed(j)
        if feed:
            posts = _build_posts(feed, count)
            if posts:
                return {"ok": True, "posts": posts, "page": page}
        ok_flag = j.get("ok") if isinstance(j, dict) else None
        err_parts.append(f"getProfileFeed HTTP {r.status_code}, ok={ok_flag}, "
                         f"body={r.text[:120]!r}")
    except Exception as e:
        err_parts.append(f"getProfileFeed exc: {e}")

    # 兜底：移动端 container
    try:
        s2 = _session(cookie)
        r2 = s2.get(f"{M_BASE}/api/container/getIndex", params={
            "containerid": f"107603{uid}", "page": page}, timeout=15)
        j2 = _safe_json(r2)
        cards = (j2 or {}).get("data", {}).get("cards", []) if isinstance(j2, dict) else []
        items = [c.get("mblog") for c in cards if c.get("card_type") == "9" and c.get("mblog")]
        if items:
            posts = _build_posts(items, count)
            if posts:
                return {"ok": True, "posts": posts, "page": page}
        ok_flag = j2.get("ok") if isinstance(j2, dict) else None
        err_parts.append(f"m.container HTTP {r2.status_code}, ok={ok_flag}, "
                         f"body={r2.text[:120]!r}")
    except Exception as e:
        err_parts.append(f"m.container exc: {e}")

    return {"ok": False, "error": "; ".join(err_parts) or "拉取失败", "posts": []}


def get_st(cookie: str, uid: str = "") -> str:
    """获取转发所需的 st 令牌（CSRF 令牌）。

    优先顺序（实测最稳的放最前）：
      1) Cookie 中的 XSRF-TOKEN —— 网页端登录写入，是 ajax 转发接口的权威 CSRF 令牌；
      2) 抓取网页端页面（带 uid 的主页 / 首页），从内嵌 JS 配置提取 ST 令牌兜底。
    返回空串表示取不到，调用方应据此判定「无转发权限 / Cookie 失效」。
    """
    # 来源一：XSRF-TOKEN cookie（最可靠）
    m = re.search(r"XSRF-TOKEN=([^;\s]+)", cookie)
    if m and m.group(1):
        return m.group(1)
    # 来源二：抓取网页端页面兜底
    candidates = []
    if uid:
        candidates.append(f"{WEB_BASE}/u/{uid}")
    candidates.append(f"{WEB_BASE}/")
    candidates.append(f"{WEB_BASE}/home")
    for url in candidates:
        try:
            s = _session(cookie, referer=url)
            r = s.get(url, timeout=15)
            text = r.text or ""
            # 常见嵌入形式：ST:"xxxx" / "st":"xxxx" / st = 'xxxx'
            for pat in (
                r'''["']st["']?\s*[:=]\s*["']([a-zA-Z0-9]{8,})["']''',
                r"""ST["']?\s*[:=]\s*["']([^"']{8,})["']""",
            ):
                mm = re.search(pat, text, re.I)
                if mm and mm.group(1):
                    return mm.group(1)
        except Exception:
            continue
    return ""


def check_login(cookie: str) -> dict:
    """检测 Cookie 是否仍处于登录态，并尝试取回自身 UID 与昵称。

    本软件的 Cookie 取自 weibo.com 网页登录，因此优先用「网页端」接口校验，
    再辅以「移动端」接口与「XSRF-TOKEN 令牌存在性」兜底，避免跨域误判“失效”。
    """
    # 方式一：网页端个人信息接口（最权威，Cookie 本就来自 weibo.com）
    try:
        s = _session(cookie, referer=f"{WEB_BASE}/")
        s.headers.update({"X-Requested-With": "XMLHttpRequest"})
        r = s.get(f"{WEB_BASE}/ajax/profile/info", timeout=15)
        j = _safe_json(r)
        d = j.get("data") if isinstance(j, dict) else None
        u = _find_user_obj(d)
        if u:
            return {
                "ok": True,
                "uid": str(u.get("id", "")),
                "screen_name": u.get("name") or u.get("screen_name", ""),
                "login": True,
            }
    except Exception:
        pass

    # 方式二：移动端 /api/config（部分登录态下可用）
    try:
        s2 = _session(cookie)
        r2 = s2.get(f"{M_BASE}/api/config", timeout=15)
        data = r2.json().get("data", {})
        if data.get("login") or data.get("uid"):
            return {
                "ok": True,
                "uid": str(data.get("uid", "")),
                "screen_name": data.get("nick", ""),
                "login": True,
            }
    except Exception:
        pass

    # 方式三：XSRF-TOKEN 存在即视为已登录
    # （weibo.com 登录成功后写入，登出后被清除，是可靠的登录信号）
    if re.search(r"XSRF-TOKEN=", cookie):
        return {
            "ok": True,
            "uid": "",
            "screen_name": "",
            "login": True,
            "message": "检测到网页端登录令牌 XSRF-TOKEN",
        }

    return {"ok": False, "login": False, "error": "Cookie 已失效或未登录"}


def check_repost_capability(cookie: str) -> dict:
    """检测账户是否具备转发能力（能否获取到转发所需的 st 令牌）。

    st 令牌是网页端转发接口的必填参数，仅在登录态下可获取；拿不到即代表
    无法转发（Cookie 失效或账户无转发权限）。
    """
    st = get_st(cookie)
    if st:
        return {"ok": True, "st": st[:8] + "…"}
    return {"ok": False, "error": "无法获取转发令牌 st（可能 Cookie 已失效或无转发权限）"}


def diagnose_account(cookie: str) -> dict:
    """综合诊断账户：登录态 + 转发能力，返回统一状态与说明。

    status: ok / cookie_expired / no_repost_perm / unknown
    """
    login = check_login(cookie)
    result = {"login": login, "repost": None,
              "status": "unknown", "message": ""}
    if not login.get("ok"):
        result["status"] = "cookie_expired"
        result["message"] = login.get("error", "Cookie 已失效")
        return result
    repost = check_repost_capability(cookie)
    result["repost"] = repost
    if repost.get("ok"):
        result["status"] = "ok"
        result["message"] = "登录正常，已具备转发能力"
    else:
        result["status"] = "no_repost_perm"
        result["message"] = "可登录，但无法获取转发令牌（可能无转发权限或 Cookie 部分失效）"
    return result


def download_image(url: str, save_path: str, cookie: str = "", timeout: int = 15) -> bool:
    """下载图片到本地（用于历史存档）。

    微博图床存在防盗链（带过期 Cookie 反而 403、无 Referer 也 403）与多尺寸变体，
    单策略极易失败。这里做稳健重试：
      - 多尺寸变体：原图 / large / mw2000 / bmiddle / orj360，优先取大图；
      - 带 Cookie 与不带 Cookie 各试一遍（部分资源公开可访问，带失效 Cookie 反而被拒）；
      - 多 Referer（网页端 / 移动端 / 用户主页）；
      - 严格排除 text/html 登录墙/防盗链错误页，非图片类型按魔数兜底校验。
    """
    url = _normalize_image_url(url)
    if not url:
        return False

    os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)

    # 候选尺寸：原图优先，再依次尝试更稳/更大的变体
    sizes = ["", "large", "mw2000", "bmiddle", "orj360"]
    candidates = []
    for repl in sizes:
        if not repl:
            if url not in candidates:
                candidates.append(url)
        else:
            u = re.sub(r"/(thumb[a-z]+|bmiddle|large|mw\d+|orj\d+)/", f"/{repl}/", url, count=1)
            if u != url and u not in candidates:
                candidates.append(u)

    referers = (f"{WEB_BASE}/", f"{M_BASE}/", "https://weibo.com/u/")
    # 微博图片有时需 Cookie，有时公开（带失效 Cookie 反而 403）→ 两种都试
    cookie_variants = [cookie, ""] if cookie else [""]

    last_err = ""
    for u in candidates:
        for ck in cookie_variants:
            for ref in referers:
                try:
                    headers = {
                        "User-Agent": UA,
                        "Referer": ref,
                        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    }
                    if ck:
                        headers["Cookie"] = ck
                    r = requests.get(u, headers=headers, timeout=timeout)
                    if r.status_code != 200:
                        last_err = f"HTTP {r.status_code}"
                        continue
                    data = r.content
                    if len(data) < 200:
                        last_err = "数据过小(<200B)"
                        continue
                    ctype = (r.headers.get("Content-Type") or "").lower()
                    # 防盗链/登录墙常返回 text/html 错误页，必须排除
                    if "text/html" in ctype:
                        last_err = "返回 HTML(疑似防盗链/登录墙)"
                        continue
                    # 非图片类型且非二进制流 → 按魔数兜底判断
                    if not ("image" in ctype or ctype.startswith("application/octet-stream")):
                        if not _is_image_bytes(data):
                            last_err = f"非图片类型({ctype})且魔数不匹配"
                            continue
                    with open(save_path, "wb") as f:
                        f.write(data)
                    return True
                except Exception as e:
                    last_err = str(e)
                    continue
    if last_err:
        warn(f"图片下载失败（已尝试多种变体）{os.path.basename(save_path)}：{last_err}")
    return False


def _is_image_bytes(data: bytes) -> bool:
    """按文件魔数判断是否为常见图片格式。"""
    if len(data) < 8:
        return False
    # PNG / JPEG / GIF / WEBP / BMP
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:2] == b"\xff\xd8":
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    if data[:2] == b"BM":
        return True
    return False


def repost(post_id: str, comment: str, cookie: str, st: str = "", uid: str = "") -> dict:
    """转发一条微博（网页端 ajax 接口 normal_repost）。

    关键约束与要点（依据当前微博网页端实际行为）：
      - Cookie 取自 weibo.com 登录，m.weibo.cn 是另一域、跨域不可用，故转发必须走
        weibo.com 端点。
      - 端点为 `ajax/statuses/normal_repost`；CSRF 令牌须放在请求头 `X-Xsrf-Token`
        （取值即 Cookie 中的 XSRF-TOKEN），缺失/错误会被网关直接 403。
      - 表单字段与旧版不同：comment / pic_id / is_repost / comment_ori / is_comment /
        visible / share_id；id 用 base62 的 bid（非数字 mid）。
      - 成功响应 JSON 含 `msg == "转发成功"`（或 ok==1 / code==100000）。
    首次疑似「令牌/请求被拒」时自动重试：换 Referer 并重新取令牌。
    任意失败分支都返回带字符串 error 的 dict，便于上层记录可读日志。
    """
    xsrf = st or get_st(cookie, uid)
    if not xsrf:
        return {"ok": False,
                "error": "无法获取转发令牌 st（Cookie 可能已失效或缺少 XSRF-TOKEN，请重新登录）"}
    referer_a = f"{WEB_BASE}/u/{uid}" if uid else f"{WEB_BASE}/"
    referer_b = f"{WEB_BASE}/"
    last_err = "转发失败"
    for attempt, referer in enumerate((referer_a, referer_b), start=1):
        s = _session(cookie, referer=referer)
        s.headers.update({
            "Referer": referer,
            "Origin": WEB_BASE,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            # 关键：CSRF 令牌走请求头（缺失/错误 → 网关 403）
            "X-Xsrf-Token": xsrf,
        })
        try:
            r = s.post(f"{WEB_BASE}/ajax/statuses/normal_repost", data={
                "id": post_id,
                "comment": comment,
                "st": xsrf,
                "pic_id": "",
                "is_repost": 0,
                "comment_ori": 0,
                "is_comment": 0,
                "visible": 0,
                "share_id": "",
            }, timeout=15)
            try:
                j = r.json()
            except Exception:
                last_err = (f"转发接口返回非 JSON（HTTP {r.status_code}）："
                            f"{r.text[:160]}")
                if attempt == 1:
                    xsrf = get_st(cookie, uid)  # 重新取令牌再试
                    continue
                return {"ok": False, "error": last_err}
            msg = j.get("msg")
            code = j.get("code")
            ok_val = j.get("ok")
            is_ok = (msg == "转发成功"
                     or str(ok_val) == "1" or ok_val is True
                     or str(code) == "100000")
            if is_ok:
                return {"ok": True, "data": j.get("data")}
            # 诊断：把微博真实回包带出来，避免吞掉原因
            last_err = (msg or j.get("error")
                        or f"未知响应(ok={ok_val!r}, code={code!r})")
            # 疑似令牌 / 登录问题 → 重试一次（重新取令牌 + 换 Referer）
            if attempt == 1 and any(k in str(last_err) for k in
                                    ("st", "ST", "令牌", "登录", "login", "验证", "403", "Forbidden")):
                xsrf = get_st(cookie, uid)
                continue
            return {"ok": False,
                    "error": f"{last_err} ｜ 原始回包={r.text[:300]!r}",
                    "raw": j}
        except Exception as e:
            last_err = f"请求异常：{e}"
            if attempt == 1:
                xsrf = get_st(cookie, uid)
                continue
            return {"ok": False, "error": last_err}
    return {"ok": False, "error": last_err}
