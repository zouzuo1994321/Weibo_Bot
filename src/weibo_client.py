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
from logger import warn, debug, CAT_HTTP

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
# 移动端 UA：m.weibo.cn 接口对 UA 敏感，用桌面 UA 极易被判定为风控（HTTP 432）
M_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1")
M_BASE = "https://m.weibo.cn"
WEB_BASE = "https://weibo.com"

TAG_RE = re.compile(r"<[^>]+>")
IMG_RE = re.compile(r"https?://[^\s\"'<>]+?(?:jpg|jpeg|png|gif|webp)", re.I)

# 表情包 / 贴纸等非正文配图的图床特征。
# 微博正文里的表情形如 <img src="https://h5.sinaimg.cn/upload/.../hotface/0019.gif" alt="[微笑]">，
# 若直接从正文 HTML 正则取图，抓到的会全是表情而非推文配图——必须剔除。
_EMOJI_URL_RE = re.compile(
    r"(?:h5\.sinaimg\.cn/(?:upload|m/emoticon)/"
    r"|(?:face|img)\.t\.sinajs\.cn"
    r"|/emoticon/"
    r"|/(?:hotface|dface|faceicon|face|sticker)/)",
    re.I,
)

# 微博正文配图图床：按尺寸分目录，例如
#   https://wx1.sinaimg.cn/large/xxxx.jpg
#   https://wx3.sinaimg.cn/orj960/xxxx.jpg
_PHOTO_URL_RE = re.compile(
    r"/(?:original|woriginal|large|mw\d{3,4}|orj\d{3,4}|bmiddle"
    r"|thumb(?:nail|\d{3,4})|square|wap\d{2,3}|w\d{3,4})/",
    re.I,
)


def _is_emoji_url(url: str) -> bool:
    """是否为表情包 / 贴纸 / 装饰小图（非推文配图）。"""
    return bool(url) and bool(_EMOJI_URL_RE.search(url))


def _looks_like_photo(url: str) -> bool:
    """是否像微博正文配图（图床域名 + 尺寸目录），用于正文 HTML 兜底时筛除表情。"""
    return bool(url) and bool(_PHOTO_URL_RE.search(url))


# ---------------------------------------------------------------------------
# 登录态 / 风控 判定（v1.1.1 新增：修复「检测正常但抓取全挂」）
#
# 背景：微博登录态由 SUB（及 SUBP/ALF）决定，而 XSRF-TOKEN 只是 CSRF 令牌。
# 服务端 session 过期后 SUB 失效，所有数据接口返回 ok=-100 并跳转 login.php；
# 但 XSRF-TOKEN 仍残留在客户端 Cookie 里。旧版 check_login 把「存在 XSRF-TOKEN」
# 当作已登录的兜底证据，于是检测显示「正常登录、可转发」，实际抓取全部失败。
# 这里把所有判定收敛到下面几个纯函数，杜绝同类误判。
# ---------------------------------------------------------------------------
# 微博风控 / 反爬状态码：432 为 m.weibo.cn 限流或要求验证，418/429 为频控
_RISK_STATUS = {403, 418, 429, 432}
# ajax 接口明确的「未登录」返回码
_NOT_LOGIN_OK = {"-100"}
# 登录墙特征（含通行证 HTML，注意其编码为 GBK，requests 会按 latin-1 解码成乱码，
# 因此这里同时保留可读与乱码两种形态的关键词）
_LOGIN_WALL_MARKS = (
    "login.php", "passport", "login?", "sso/login",
    "dÐÂÀËÍ¨ÐÐÖ¤", "Í¨ÐÐÖ¤", "通行证", "安全验证", "请登录", "重新登录",
)


def _looks_not_login(status: int, text: str = "", j=None) -> bool:
    """响应是否为「未登录 / 登录墙」。"""
    if isinstance(j, dict):
        if str(j.get("ok")) in _NOT_LOGIN_OK:
            return True
        u = j.get("url")
        if isinstance(u, str) and "login" in u.lower():
            return True
        # 注意：这里不能用宽松的 "login" in v —— 正常响应里常有 "login":false 这类字段，
        # 会被误判成登录墙。只认明确表述（中文「登录/未登录」或英文固定短语）。
        for key in ("msg", "message", "error"):
            v = str(j.get(key) or "")
            if not v:
                continue
            low = v.lower()
            if ("登录" in v or "not login" in low or "please login" in low
                    or "unlogin" in low or "need login" in low):
                return True
    if status in (302, 401):
        return True
    t = (text or "")[:3000]
    if not t:
        return False
    low = t.lower()
    for m in _LOGIN_WALL_MARKS:
        if m.lower() in low:
            return True
    return False


def _looks_risk_control(status: int, text: str = "") -> bool:
    """响应是否为微博风控拦截（而非普通失败）。"""
    if status in _RISK_STATUS:
        return True
    t = (text or "")[:800]
    return ("验证码" in t) or ("访问过于频繁" in t) or ("请求过于频繁" in t)


# ---------------------------------------------------------------------------
# Cookie 工具：解析 / 合并 / 续期
# ---------------------------------------------------------------------------
# 微博每次网页请求都可能通过 Set-Cookie 续期这些键，实现「滑动续期」。
_COOKIE_RENEW_KEYS = ("SUB", "SUBP", "SUBP_0033", "XSRF-TOKEN",
                      "ALF", "ALF_SSOINFO", "SSOLoginState", "WBPSESS")


def _cookie_dict(cookie: str) -> dict:
    """把 Cookie 字符串解析为 dict。"""
    d = {}
    for part in (cookie or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        d[k.strip()] = v.strip()
    return d


def _cookie_str(d: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in d.items() if v != "" and v is not None)


def _has_login_cookie(cookie: str) -> bool:
    """是否持有微博登录凭证 SUB。没有 SUB 必然未登录（XSRF-TOKEN 无意义）。"""
    return bool(_cookie_dict(cookie).get("SUB"))


def _merge_cookies(cookie: str, session) -> str:
    """把 Session 中服务端下发的（已续期的）Cookie 合并回原始 Cookie 串。

    微博在网页请求响应里常通过 Set-Cookie 续期 SUB / SUBP / XSRF-TOKEN / ALF，
    把续期结果回写即等价于「会话保活」，可显著延长 Cookie 寿命，减少必须
    重新登录的次数。只合并与登录相关的键，避免把无关追踪 Cookie 塞进来。
    """
    try:
        jar = session.cookies.get_dict()
    except Exception:
        return cookie
    if not jar:
        return cookie
    base = _cookie_dict(cookie)
    changed = False
    for k in _COOKIE_RENEW_KEYS:
        v = jar.get(k)
        if v and base.get(k) != v:
            base[k] = v
            changed = True
    return _cookie_str(base) if changed else cookie


def _session(cookie: str, referer: str = None, mobile: bool = False) -> requests.Session:
    s = requests.Session()
    if referer is None:
        referer = f"{M_BASE}/" if mobile else f"{WEB_BASE}/"
    s.headers.update({
        "User-Agent": M_UA if mobile else UA,
        "Cookie": cookie,
        "Referer": referer,
    })
    # 关键：同时把 Cookie 灌进 Session 的 CookieJar。
    # 只用请求头传 Cookie 时，服务端下发的 Set-Cookie（SUB/SUBP/XSRF-TOKEN 续期）
    # 无法被 Session 接收，也就拿不到续期结果；灌入 jar 后才能实现会话保活。
    d = _cookie_dict(cookie)
    if d:
        try:
            s.cookies.update(requests.utils.cookiejar_from_dict(d))
        except Exception:
            pass
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
    """从微博内容或 pics / pic_infos 字段提取图片直链（优先原图 / large）。

    微博改版后图片主要放在 `pic_infos`（dict：{pid: {"original"/"large": {"url": ...}}}），
    旧版在 `pics`（list）。两者都兼容；注意 pic_infos 中的 "video" 键是视频，不作为图片。
    """
    imgs = []
    if isinstance(pics_field, dict):
        # 新版 pic_infos：{pid: {"original": {"url":...}, "large": {...}, "video": "..."}}
        for item in pics_field.values():
            if not isinstance(item, dict):
                continue
            got = False
            for key in ("original", "largest", "mw2000", "large", "bmiddle"):
                v = item.get(key)
                if isinstance(v, dict) and v.get("url"):
                    imgs.append(_normalize_image_url(v["url"]))
                    got = True
                    break
            if not got and item.get("url"):
                imgs.append(_normalize_image_url(item["url"]))
    elif pics_field:
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
    from_html = False
    if not imgs and raw_html:
        # 兜底：从正文 HTML 里找图（此时极易混入表情，下面会严格过滤）
        imgs = [_normalize_image_url(u) for u in IMG_RE.findall(raw_html)]
        from_html = True
    # 去重并过滤表情小图
    seen, out = set(), []
    for u in imgs:
        if not u or u in seen:
            continue
        seen.add(u)
        # 1) 表情包 / 贴纸：一律剔除
        if _is_emoji_url(u):
            debug(f"忽略表情/贴纸图片：{u}", CAT_HTTP)
            continue
        # 2) 正文兜底得到的图，必须是图床尺寸目录，否则视为装饰图丢弃
        if from_html and not _looks_like_photo(u):
            debug(f"忽略非正文配图：{u}", CAT_HTTP)
            continue
        out.append(u)
    return out


# 视频清晰度键（按从高到低尝试），覆盖新旧两版字段命名
_VIDEO_KEYS = ("mp4_1080p_mp4", "hevc_mp4_720p", "mp4_720p_mp4",
               "inch_5_5_mp4_hd", "mp4_hd_mp4", "stream_url_hd",
               "stream_url", "mp4_480p_mp4", "mp4_ld_mp4", "mp4_sd_mp4")
# 图片尺寸键（按从大到小尝试）
_PIC_SIZE_KEYS = ("original", "largest", "mw2000", "large", "bmiddle")


def extract_videos(mb: dict) -> list:
    """从微博对象中提取视频直链（多结构兼容，按清晰度从高到低）。

    微博视频主要藏在 mblog 的 `page_info` 中，不同接口结构差异较大，这里覆盖：
      1) page_info.urls.{mp4_1080p_mp4 / mp4_720p_mp4 / mp4_hd_mp4 / mp4_ld_mp4 ...}
      2) page_info.media_info.playback_list[].play_info.url（移动端常见）
      3) page_info 顶层的 stream_url / mp4_sd_url / mp4_hd_url
    返回去重后的候选列表，下载时按顺序尝试（前者失败自动降级到下一档）。
    """
    if not isinstance(mb, dict):
        return []
    pi = mb.get("page_info")
    if not isinstance(pi, dict):
        pi = {}
    # 新版可能是 mix_media_info 而无 page_info，需一并考虑
    _mmi0 = mb.get("mix_media_info")
    has_mix = isinstance(_mmi0, dict) and bool(_mmi0.get("items"))
    # 无媒体信息的卡片（如普通图文、文章链接）直接跳过
    if not has_mix and not (pi.get("media_info") or pi.get("urls") or pi.get("stream_url")):
        return []

    urls = []

    def _add(v):
        if not v:
            return
        u = _normalize_image_url(str(v))
        if u and u not in urls:
            urls.append(u)

    # 0) 新版混合媒体：mix_media_info.items[].data（图文 + 视频混合卡片）
    mmi = mb.get("mix_media_info")
    if isinstance(mmi, dict):
        for it in (mmi.get("items") or []):
            if not isinstance(it, dict):
                continue
            d = it.get("data") or {}
            _mi = d.get("media_info") or {}
            for key in _VIDEO_KEYS:
                _add(_mi.get(key))
                _add(d.get(key))

    # 1) 各清晰度键（高 → 低）
    u = pi.get("urls")
    if isinstance(u, dict):
        for key in _VIDEO_KEYS:
            _add(u.get(key))

    # 2) playback_list 多档清晰度
    mi = pi.get("media_info")
    if isinstance(mi, dict):
        for item in (mi.get("playback_list") or []):
            if isinstance(item, dict):
                pf = item.get("play_info")
                if isinstance(pf, dict):
                    _add(pf.get("url"))
        for key in ("stream_url", "mp4_sd_url", "mp4_hd_url", "playback_url"):
            _add(mi.get(key))
        for key in _VIDEO_KEYS:
            _add(mi.get(key))

    # 3) page_info 顶层
    for key in ("stream_url", "mp4_sd_url", "mp4_hd_url"):
        _add(pi.get(key))

    return urls


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
        # 新版结构优先 pic_infos（dict），旧版为 pics / pic_ids（list）
        # 注意：新版 pic_infos 是 dict，必须原样传入；此前只传 list 导致 dict 被丢弃，
        # 回退到正文 HTML 正则取图，结果抓到的全是表情包。
        pics = mb.get("pic_infos") or mb.get("pics") or mb.get("pic_ids") or []
        posts.append({
            "id": str(mb.get("id") or mb.get("mid") or mb.get("mblogid")),
            "bid": mb.get("mblogid") or mb.get("bid"),
            "text": clean_text(raw),
            "text_raw": raw,
            "created_at": mb.get("created_at", ""),
            "images": extract_images(raw, pics),
            "videos": extract_videos(mb),
            "reposts_count": mb.get("reposts_count", 0),
            "comments_count": mb.get("comments_count", 0),
            "attitudes_count": mb.get("attitudes_count", 0),
            "is_top": _is_pinned(mb),
        })
        if len(posts) >= count:
            break
    return posts


def get_post_detail(post_id: str, cookie: str) -> dict:
    """按微博 ID 拉取单条详情（含 pic_infos），用于修复历史记录中缺失/错误的图片。

    返回 {"ok": True, "images": [...]} 或 {"ok": False, "error": ...}。
    """
    if not post_id:
        return {"ok": False, "error": "缺少微博 ID"}
    mb = None
    # 策略 1：网页端 statuses/show
    try:
        s = _session(cookie, referer=f"{WEB_BASE}/")
        s.headers.update({
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        })
        r = s.get(f"{WEB_BASE}/ajax/statuses/show", params={"id": post_id}, timeout=15)
        j = _safe_json(r)
        if isinstance(j, dict):
            if j.get("pic_infos") or j.get("pics") or j.get("text"):
                mb = j
            elif isinstance(j.get("data"), dict):
                mb = j["data"]
        debug(f"HTTP {r.status_code} statuses/show id={post_id} · "
              f"pic_infos={'有' if (mb or {}).get('pic_infos') else '无'}", CAT_HTTP)
    except Exception as e:
        debug(f"statuses/show 异常 id={post_id}：{e}", CAT_HTTP)

    # 策略 2：移动端 statuses/show
    if not mb:
        try:
            r = _session(cookie).get(f"{M_BASE}/statuses/show", params={"id": post_id}, timeout=15)
            j = _safe_json(r)
            d = (j or {}).get("data") if isinstance(j, dict) else None
            if isinstance(d, dict):
                mb = d
        except Exception as e:
            debug(f"m.statuses/show 异常 id={post_id}：{e}", CAT_HTTP)

    if not mb:
        return {"ok": False, "error": "未获取到微博详情（可能 Cookie 失效或微博已删除）"}

    raw = mb.get("text", "")
    pics = mb.get("pic_infos") or mb.get("pics") or []
    images = extract_images(raw, pics)
    return {"ok": True, "images": images}


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
        # v1.1.1：显式识别未登录 / 风控，便于上层区分「Cookie 失效」与「该用户不可见」
        if _looks_not_login(r.status_code, r.text, j):
            return {"ok": False, "status": "cookie_expired",
                    "error": "Cookie 已失效（微博要求重新登录）"}
        if _looks_risk_control(r.status_code, r.text):
            return {"ok": False, "status": "risk_control",
                    "error": f"被微博风控拦截（HTTP {r.status_code}）"}
        d = j.get("data") if isinstance(j, dict) else None
        u = _find_user_obj(d)
        if u:
            return {
                "ok": True, "status": "ok",
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
                "ok": True, "status": "ok",
                "uid": uid,
                "screen_name": user.get("screen_name", ""),
                "avatar_url": _pick_avatar(user),
                "description": user.get("description", ""),
                "followers": user.get("followers_count", 0),
            }
    except Exception:
        pass
    return {"ok": False, "status": "unknown",
            "error": "未获取到用户信息（可能 Cookie 失效或无权限）"}


def warm_session(cookie: str, uid: str = "") -> dict:
    """网页端会话预热 + Cookie 续期（会话保活）。

    两个作用：
      1) 访问一次真实页面，让微博通过 Set-Cookie 续期 SUB / SUBP / XSRF-TOKEN / ALF，
         把续期结果合并回 Cookie 串 —— 相当于滑动续期，可显著延长 Cookie 寿命；
      2) 顺带判定当前 Cookie 是否已被登出（页面落到登录墙即失效）。

    返回 {"ok":bool, "cookie":续期后的 cookie, "status":ok/cookie_expired/risk_control/network_error}
    """
    if not cookie:
        return {"ok": False, "cookie": cookie, "status": "cookie_expired",
                "error": "Cookie 为空"}
    url = f"{WEB_BASE}/u/{uid}" if uid else f"{WEB_BASE}/"
    try:
        s = _session(cookie, referer=url)
        s.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Upgrade-Insecure-Requests": "1",
        })
        r = s.get(url, timeout=15, allow_redirects=True)
        if _looks_risk_control(r.status_code, r.text):
            return {"ok": False, "cookie": cookie, "status": "risk_control",
                    "error": f"会话预热被风控拦截（HTTP {r.status_code}）"}
        if _looks_not_login(r.status_code, r.text):
            return {"ok": False, "cookie": cookie, "status": "cookie_expired",
                    "error": "会话预热落到登录页，Cookie 已失效"}
        renewed = _merge_cookies(cookie, s)
        if renewed != cookie:
            debug(f"会话预热完成 · Cookie 已续期（{url}）", CAT_HTTP)
        return {"ok": True, "cookie": renewed, "status": "ok",
                "renewed": renewed != cookie}
    except Exception as e:
        return {"ok": False, "cookie": cookie, "status": "network_error",
                "error": f"会话预热异常：{e}"}


def _fetch_once(uid: str, cookie: str, page: int, count: int):
    """按「网页端 mymblog → getProfileFeed → 移动端 container」顺序尝试一次。

    返回 (result|None, err_parts, flags)
      - result: 成功时的 {"ok":True, "posts":[...], "page":page, "cookie":...}
      - flags : {"not_login" / "risk" / "network"} 用于上层区分失败性质
    """
    err_parts = []
    flags = set()

    def _mark(status, text, j):
        """按响应特征打标记，返回 True 表示已明确判定为未登录。"""
        if _looks_risk_control(status, text):
            flags.add("risk")
        if _looks_not_login(status, text, j):
            flags.add("not_login")
            return True
        return False

    # 策略 1：网页端 /ajax/statuses/mymblog
    try:
        s = _session(cookie, referer=f"{WEB_BASE}/u/{uid}")
        s.headers.update({
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        })
        _t0 = time.time()
        r = s.get(f"{WEB_BASE}/ajax/statuses/mymblog", params={
            "uid": uid, "page": page, "feature": 0}, timeout=15)
        _cost = int((time.time() - _t0) * 1000)
        j = _safe_json(r)
        feed = _extract_feed(j)
        debug(f"HTTP {r.status_code} mymblog uid={uid} page={page} · {_cost}ms · "
              f"原始 {len(feed or [])} 条", CAT_HTTP)
        if feed:
            posts = _build_posts(feed, count)
            if posts:
                debug(f"mymblog 解析成功 uid={uid} → {len(posts)} 条", CAT_HTTP)
                return ({"ok": True, "posts": posts, "page": page,
                         "cookie": _merge_cookies(cookie, s)}, err_parts, flags)
        if _mark(r.status_code, r.text, j):
            err_parts.append(f"mymblog HTTP {r.status_code} · 未登录（ok={j.get('ok') if isinstance(j, dict) else None}）")
        else:
            ok_flag = j.get("ok") if isinstance(j, dict) else None
            err_parts.append(f"mymblog HTTP {r.status_code}, ok={ok_flag}, "
                             f"body={r.text[:120]!r}")
    except Exception as e:
        flags.add("network")
        err_parts.append(f"mymblog exc: {e}")

    # 策略 2：网页端 /ajax/profile/getProfileFeed
    try:
        s = _session(cookie, referer=f"{WEB_BASE}/u/{uid}")
        s.headers.update({
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        })
        r = s.get(f"{WEB_BASE}/ajax/profile/getProfileFeed", params={
            "uid": uid, "page": page, "feature": 0}, timeout=15)
        j = _safe_json(r)
        feed = _extract_feed(j)
        if feed:
            posts = _build_posts(feed, count)
            if posts:
                return ({"ok": True, "posts": posts, "page": page,
                         "cookie": _merge_cookies(cookie, s)}, err_parts, flags)
        if _mark(r.status_code, r.text, j):
            err_parts.append(f"getProfileFeed HTTP {r.status_code} · 未登录/登录墙")
        else:
            ok_flag = j.get("ok") if isinstance(j, dict) else None
            err_parts.append(f"getProfileFeed HTTP {r.status_code}, ok={ok_flag}, "
                             f"body={r.text[:120]!r}")
    except Exception as e:
        flags.add("network")
        err_parts.append(f"getProfileFeed exc: {e}")

    # 策略 3：移动端 container（先预热移动端首页拿 MLOGIN，否则极易 HTTP 432）
    try:
        ms = _session(cookie, referer=f"{M_BASE}/u/{uid}", mobile=True)
        try:
            ms.get(f"{M_BASE}/", timeout=10)   # 预热：换取移动端会话 Cookie
        except Exception:
            pass
        m_cookie = _merge_cookies(cookie, ms)
        ms.headers.update({
            "Cookie": m_cookie,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        })
        r2 = ms.get(f"{M_BASE}/api/container/getIndex", params={
            "containerid": f"107603{uid}", "page": page}, timeout=15)
        j2 = _safe_json(r2)
        cards = (j2 or {}).get("data", {}).get("cards", []) if isinstance(j2, dict) else []
        items = [c.get("mblog") for c in cards if c.get("card_type") == "9" and c.get("mblog")]
        if items:
            posts = _build_posts(items, count)
            if posts:
                return ({"ok": True, "posts": posts, "page": page,
                         "cookie": m_cookie}, err_parts, flags)
        # 关键：移动端与 Cookie 不同源，其「未登录」是常态，**不作为失效依据**
        # （否则网页端只要有一次空响应，就会被跨域信号误判成 Cookie 失效）。
        # 这里只采纳它的风控状态码（432/418/429/403），那是真实的反爬信号。
        if _looks_risk_control(r2.status_code, r2.text):
            flags.add("risk")
            err_parts.append(f"m.container HTTP {r2.status_code} · 风控拦截")
        else:
            ok_flag = j2.get("ok") if isinstance(j2, dict) else None
            err_parts.append(f"m.container HTTP {r2.status_code}, ok={ok_flag}, "
                             f"body={r2.text[:120]!r}")
    except Exception as e:
        flags.add("network")
        err_parts.append(f"m.container exc: {e}")

    return (None, err_parts, flags)


def get_latest_posts(uid: str, cookie: str, page: int = 1, count: int = 10) -> dict:
    """获取该 UID 最新微博列表。

    在 v1.1.1 中重写了失败处理：
      - 三种接口任一命中「未登录 / 风控 / 网络异常」都会被打上 flag；
      - 首次全败且 flag 非空时，先做一次**会话预热**（页面访问触发 Set-Cookie 续期
        SUB/SUBP/XSRF-TOKEN），拿到续期 Cookie 后**重试一轮**；
      - 仍失败则按 flag 分类返回结构化 status：
        cookie_expired（须重新登录）/ risk_control（被风控）/ network_error / empty，
        并附带可直接展示给用户的中文原因。

    成功时返回值可能含 `cookie` / `renewed_cookie` 键为续期后的 Cookie，
    调用方应回写账户，从而实现「越用越不容易掉线」的会话保活。
    """
    res, errs, flags = _fetch_once(uid, cookie, page, count)
    if res:
        return res

    # 首次全败：若疑似会话过期 / 风控，先预热续期再重试一次
    if flags:
        w = warm_session(cookie, uid)
        if w.get("ok") and w.get("cookie") and w["cookie"] != cookie:
            debug(f"会话预热后重试抓取 uid={uid} · 失败标记={sorted(flags)}", CAT_HTTP)
            res2, errs2, flags2 = _fetch_once(uid, w["cookie"], page, count)
            if res2:
                res2["renewed_cookie"] = w["cookie"]
                res2["cookie"] = w["cookie"]
                info(f"会话续期后抓取恢复 uid={uid}（此前标记：{sorted(flags)}）", CAT_HTTP)
                return res2
            errs = errs + errs2
            flags |= flags2

    # 分类失败原因，给出可操作结论
    if "not_login" in flags:
        status = "cookie_expired"
        reason = "Cookie 登录态已失效（微博要求重新登录）"
    elif "risk" in flags:
        status = "risk_control"
        reason = "被微博风控拦截（HTTP 432/418/403），请降低轮询频率后重试"
    elif "network" in flags:
        status = "network_error"
        reason = "网络异常或接口超时"
    else:
        status = "empty"
        reason = "未获取到微博数据（该用户可能暂无公开微博）"

    _err = "; ".join(errs) or "拉取失败"
    warn(f"拉取微博失败 uid={uid} page={page} · 判定={status} · 已尝试 3 种接口 · "
         f"原因：{_err}", CAT_HTTP)
    return {"ok": False, "status": status, "reason": reason,
            "error": f"{reason}（{_err}）", "posts": []}


# ---------------- 相册 / 视频 专属接口 ----------------
def _iter_dicts(obj):
    """递归遍历 JSON 中的所有 dict（用于宽容解析，适配微博字段频繁变动）。"""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_dicts(v)


def _dedup(urls):
    out, seen = [], set()
    for u in urls:
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def extract_media_from_json(obj):
    """从任意微博 JSON 结构中宽容地提取图片与视频直链。

    微博字段层级经常变动（pics → pic_infos、page_info → mix_media_info），
    这里不依赖固定层级，而是递归遍历所有 dict 按特征键取值，
    对接口改版具有更好的适应性。
    返回 (images, videos)。
    """
    imgs, vids = [], []
    for d in _iter_dicts(obj):
        # 图片：pic_infos / pics 的尺寸对象（每张只取最高优先级，避免同图多尺寸重复下载）
        for k in _PIC_SIZE_KEYS:
            v = d.get(k)
            if isinstance(v, dict) and isinstance(v.get("url"), str):
                imgs.append(_normalize_image_url(v["url"]))
                break
        # 图片：photo.weibo.com 的 pic_host + pic_name
        ph, pn = d.get("pic_host"), d.get("pic_name")
        if isinstance(ph, str) and isinstance(pn, str) and ph.startswith("http"):
            imgs.append(f"{ph.rstrip('/')}/large/{pn}")
        # 视频：各清晰度键（只取最高清晰度一档）
        for k in _VIDEO_KEYS:
            v = d.get(k)
            if isinstance(v, str) and v.startswith("http"):
                vids.append(_normalize_image_url(v))
                break
        # 视频：playback_list
        pl = d.get("playback_list")
        if isinstance(pl, list):
            for it in pl:
                if isinstance(it, dict):
                    pinfo = it.get("play_info")
                    if isinstance(pinfo, dict) and isinstance(pinfo.get("url"), str):
                        vids.append(_normalize_image_url(pinfo["url"]))
    return _dedup(imgs), _dedup(vids)


def get_album_photos(uid: str, cookie: str, max_pages: int = 5) -> dict:
    """获取用户**相册**图片（对应 https://weibo.com/u/{uid}?tabtype=album）。

    相册与微博流是两套内容，仅抓微博流是拿不到相册的。这里多策略尝试：
      1) weibo.com/ajax/profile/getImageWall —— 新版相册墙；
      2) photo.weibo.com/albums/get_all + photos/get_all —— 经典相册接口；
      3) 兜底：微博流中的图片（pic_infos）。
    返回 {"ok":bool, "photos":[{"url","pid","caption"}], "source":str}
    """
    photos, src, errs = [], "", []

    # 策略 1：新版相册墙
    try:
        s = _session(cookie, referer=f"{WEB_BASE}/u/{uid}")
        s.headers.update({"X-Requested-With": "XMLHttpRequest",
                          "Accept": "application/json, text/plain, */*"})
        r = s.get(f"{WEB_BASE}/ajax/profile/getImageWall",
                  params={"uid": uid, "sinceid": 0, "has_album": "true"}, timeout=15)
        j = _safe_json(r) or {}
        imgs, _ = extract_media_from_json(j)
        if imgs:
            photos = [{"url": u, "pid": "", "caption": ""} for u in imgs]
            src = "getImageWall"
        else:
            errs.append(f"getImageWall HTTP {r.status_code} 无图片")
    except Exception as e:
        errs.append(f"getImageWall exc: {e}")

    # 策略 2：经典相册接口 photo.weibo.com
    if not photos:
        try:
            s = _session(cookie, referer=f"https://photo.weibo.com/{uid}/talbum/index")
            r = s.get("https://photo.weibo.com/albums/get_all", params={
                "uid": uid, "page": 1, "count": 100,
                "__rnd": int(time.time() * 1000)}, timeout=15)
            j = _safe_json(r) or {}
            albums = ((j.get("data") or {}).get("album_list") or [])
            if not albums:
                errs.append(f"albums/get_all 无相册（HTTP {r.status_code}）")
            for al in albums[:max(1, max_pages)]:
                aid, atype = al.get("album_id"), al.get("type")
                cap = al.get("caption") or ""
                if not aid:
                    continue
                for pg in range(1, max(1, max_pages) + 1):
                    r2 = s.get("https://photo.weibo.com/photos/get_all", params={
                        "uid": uid, "album_id": aid, "count": 100,
                        "page": pg, "type": atype}, timeout=15)
                    j2 = _safe_json(r2) or {}
                    plist = ((j2.get("data") or {}).get("photo_list") or [])
                    if not plist:
                        break
                    for ph in plist:
                        host, name = ph.get("pic_host"), ph.get("pic_name")
                        if host and name:
                            photos.append({
                                "url": f"{str(host).rstrip('/')}/large/{name}",
                                "pid": str(ph.get("pid") or name),
                                "caption": cap,
                            })
                    if len(plist) < 100:
                        break
            if photos:
                src = "photo.weibo.com"
        except Exception as e:
            errs.append(f"photo.weibo.com exc: {e}")

    # 策略 3：兜底 —— 微博流图片
    if not photos:
        for pg in range(1, max(1, max_pages) + 1):
            resp = get_latest_posts(uid, cookie, page=pg, count=10)
            if not resp.get("ok"):
                errs.append(f"mymblog p{pg}: {resp.get('error', '')}")
                break
            posts = resp.get("posts") or []
            for p in posts:
                for u in (p.get("images") or []):
                    photos.append({"url": u, "pid": str(p.get("id") or ""),
                                   "caption": (p.get("text") or "")[:60]})
            if len(posts) < 10:
                break
        if photos:
            src = "微博流(兜底)"

    if not photos:
        warn(f"相册拉取失败 uid={uid} · 已尝试 3 种方式 · {'; '.join(errs) or '无数据'}", CAT_HTTP)
    return {"ok": bool(photos), "photos": photos, "source": src}


def get_user_videos(uid: str, cookie: str, max_pages: int = 5) -> dict:
    """获取用户**视频**（对应 https://weibo.com/u/{uid}?tabtype=newVideo）。

    主接口为瀑布流 getWaterFallContent（cursor 翻页），失败则回退微博流。
    返回 {"ok":bool, "videos":[{"url","pid","text"}], "source":str}
    """
    vids, src, errs = [], "", []

    # 策略 1：瀑布流（视频 tab 使用的接口）
    try:
        s = _session(cookie, referer=f"{WEB_BASE}/u/{uid}")
        s.headers.update({"X-Requested-With": "XMLHttpRequest",
                          "Accept": "application/json, text/plain, */*"})
        cursor = "0"
        for _ in range(max(1, max_pages)):
            r = s.get(f"{WEB_BASE}/ajax/profile/getWaterFallContent",
                      params={"uid": uid, "cursor": cursor}, timeout=15)
            j = _safe_json(r) or {}
            _, v = extract_media_from_json(j)
            for u in v:
                vids.append({"url": u, "pid": "", "text": ""})
            nxt = (j.get("data") or {}).get("next_cursor")
            if not nxt or str(nxt) == str(cursor):
                break
            cursor = nxt
        if vids:
            src = "getWaterFallContent"
        else:
            errs.append(f"getWaterFallContent 无视频（HTTP {r.status_code}）")
    except Exception as e:
        errs.append(f"getWaterFallContent exc: {e}")

    # 策略 2：兜底 —— 微博流中的视频
    if not vids:
        for pg in range(1, max(1, max_pages) + 1):
            resp = get_latest_posts(uid, cookie, page=pg, count=10)
            if not resp.get("ok"):
                errs.append(f"mymblog p{pg}: {resp.get('error', '')}")
                break
            posts = resp.get("posts") or []
            for p in posts:
                for u in (p.get("videos") or []):
                    vids.append({"url": u, "pid": str(p.get("id") or ""),
                                 "text": (p.get("text") or "")[:60]})
            if len(posts) < 10:
                break
        if vids:
            src = "微博流(兜底)"

    if not vids:
        warn(f"视频拉取失败 uid={uid} · 已尝试 2 种方式 · {'; '.join(errs) or '无数据'}", CAT_HTTP)
    return {"ok": bool(vids), "videos": vids, "source": src}


def get_st(cookie: str, uid: str = "") -> str:
    """获取转发所需的 st 令牌（CSRF 令牌）。

    优先顺序（实测最稳的放最前）：
      1) Cookie 中的 XSRF-TOKEN —— 网页端登录写入，是 ajax 转发接口的权威 CSRF 令牌；
      2) 抓取网页端页面（带 uid 的主页 / 首页），从内嵌 JS 配置提取 ST 令牌兜底。
    返回空串表示取不到，调用方应据此判定「无转发权限 / Cookie 失效」。
    """
    # v1.1.1：XSRF-TOKEN 只是 CSRF 令牌，登录态由 SUB 决定。
    # 缺少 SUB 时即便存在 XSRF-TOKEN 也必然未登录，直接返回空，
    # 杜绝「有令牌 → 判定可转发」的假阳性（旧版 get_st 无此校验）。
    if not _has_login_cookie(cookie):
        return ""
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


def check_login(cookie: str, uid: str = "") -> dict:
    """检测 Cookie 是否仍处于**真实**登录态，并尝试取回自身 UID 与昵称。

    【v1.1.1 修复 · 消除「检测正常但抓取全挂」的假阳性】
    旧实现把「Cookie 中存在 XSRF-TOKEN」当作已登录的兜底证据，但：
      - 微博登录态实际由 **SUB** 决定，XSRF-TOKEN 只是 CSRF 令牌；
      - 服务端 session 过期后 SUB 失效，所有数据接口返回 ok=-100 并跳转 login.php，
        而 XSRF-TOKEN **仍残留在客户端 Cookie 里** → 旧逻辑据此判定「已登录」，
        界面显示正常，实际抓取三种接口全败（与用户反馈现象完全一致）。

    现在的判定顺序（任一环节拿到确定结论即返回）：
      1) 结构性检查：Cookie 里没有 SUB → 必然未登录（直接 cookie_expired）；
      2) 网页端 /ajax/profile/info：**与 Cookie 同源**，是唯一有权判定「已失效」的接口
         （显式识别 ok=-100 / login.php 登录墙 / 风控）；
      3) 移动端 /api/config：**只能做正向确认，绝不能否定登录态**
         —— m.weibo.cn 与 weibo.com 不同源，Cookie 不跨域共享，它对一份全新有效的
         网页端 Cookie 同样会回答「未登录」，若据以判失效必然误报；
      4) 确有 SUB 但以上都判不出 → 返回 **unknown**（由上层用真实抓取探测裁决），
         既不冒充「已登录」，也不冤枉成「已失效」。

    返回统一带 `status`：ok / cookie_expired / risk_control / network_error / unknown，
    并附 `steps` 逐步诊断信息（HTTP 状态码 + 响应片段），便于从运行日志定位问题。
    """
    steps = []

    def _fail(status, error):
        return {"ok": False, "login": False, "status": status, "error": error,
                "steps": steps}

    if not (cookie or "").strip():
        return _fail("cookie_expired", "Cookie 为空，请先登录")

    # 1) 结构性检查：登录凭证 SUB 缺失即未登录
    if not _has_login_cookie(cookie):
        return _fail("cookie_expired",
                     "Cookie 缺少登录凭证 SUB（未登录或已失效），请重新登录")

    # 2) 网页端个人信息接口（与 Cookie 同源，唯一有权判定失效）
    net_err = ""
    try:
        s = _session(cookie, referer=f"{WEB_BASE}/")
        s.headers.update({
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        })
        params = {"uid": uid} if uid else None
        r = s.get(f"{WEB_BASE}/ajax/profile/info", params=params, timeout=15)
        steps.append(f"web.ajax/profile/info → HTTP {r.status_code} · "
                     f"{(r.text or '')[:100]!r}")
        if _looks_risk_control(r.status_code, r.text):
            return _fail("risk_control", f"被微博风控拦截（HTTP {r.status_code}）")
        j = _safe_json(r)
        if _looks_not_login(r.status_code, r.text, j):
            return _fail("cookie_expired",
                         "微博网页端接口返回未登录（ok=-100 / 登录页），Cookie 已失效，请重新登录")
        u = _find_user_obj((j or {}).get("data"))
        if u:
            steps.append("web.profile/info → 解析到用户对象")
            return {
                "ok": True, "login": True, "status": "ok",
                "uid": str(u.get("id", "")),
                "screen_name": u.get("name") or u.get("screen_name", ""),
                "cookie": _merge_cookies(cookie, s),
                "steps": steps,
            }
        # 正向兜底：接口明确返回成功（ok=1 且 data 非空）即视为已登录，
        # 即便昵称字段结构变化导致解析不出用户对象，也不该判成失效。
        if isinstance(j, dict) and str(j.get("ok")) == "1" and j.get("data"):
            steps.append("web.profile/info → ok=1 且 data 非空（昵称未解析出）")
            return {
                "ok": True, "login": True, "status": "ok",
                "uid": uid or "", "screen_name": "",
                "cookie": _merge_cookies(cookie, s),
                "steps": steps,
            }
        steps.append("web.profile/info → 未解析到用户对象，转移动端确认")
    except Exception as e:
        net_err = str(e)
        steps.append(f"web.profile/info 异常：{e}")

    # 3) 移动端 /api/config —— 仅作正向确认
    #    重要：m.weibo.cn 与 weibo.com 不同源，网页端 Cookie 不会自动带过去，
    #    因此该接口的「未登录」是常态，**不能**用来否定网页端登录态。
    try:
        s2 = _session(cookie, mobile=True)
        r2 = s2.get(f"{M_BASE}/api/config", timeout=15)
        j2 = _safe_json(r2) or {}
        data = j2.get("data") or {}
        steps.append(f"m.api/config → HTTP {r2.status_code} · {(r2.text or '')[:100]!r}")
        if data.get("login") or data.get("uid"):
            steps.append("m.api/config → 确认已登录")
            return {
                "ok": True, "login": True, "status": "ok",
                "uid": str(data.get("uid", "")),
                "screen_name": data.get("nick", ""),
                "cookie": _merge_cookies(cookie, s2),
                "steps": steps,
            }
        steps.append("m.api/config → 未确认登录（跨域常态，不作失效依据）")
    except Exception as e:
        net_err = net_err or str(e)
        steps.append(f"m.api/config 异常：{e}")

    # 4) 网络异常优先于「未知」，避免把超时误报成 Cookie 失效
    if net_err:
        return _fail("network_error", f"登录检测网络异常：{net_err}")

    # 5) 持有 SUB 却无法确认 → unknown，交给上层用真实抓取探测裁决
    steps.append("所有接口均未确认登录态，但 Cookie 含 SUB 且无登录墙特征")
    warn("登录态无法确认（未出现登录墙特征）· " + " ｜ ".join(steps), CAT_HTTP)
    return {"ok": False, "login": False, "status": "unknown",
            "uid": "", "screen_name": "",
            "message": "无法确认登录态（接口未返回登录墙，但也未确认登录）",
            "error": "无法确认登录态（接口未返回登录墙，但也未确认登录）",
            "steps": steps}


def check_repost_capability(cookie: str) -> dict:
    """检测账户是否具备转发能力（能否获取到转发所需的 st 令牌）。

    st 令牌是网页端转发接口的必填参数，仅在登录态下可获取；拿不到即代表
    无法转发（Cookie 失效或账户无转发权限）。
    """
    st = get_st(cookie)
    if st:
        return {"ok": True, "st": st[:8] + "…"}
    # 明确区分「未登录」与「无转发权限」，便于上层给出不同处置建议
    if not _has_login_cookie(cookie):
        return {"ok": False, "status": "cookie_expired",
                "error": "Cookie 缺少登录凭证 SUB，无法获取转发令牌，请重新登录"}
    return {"ok": False, "status": "no_repost_perm",
            "error": "无法获取转发令牌 st（Cookie 可能已失效或无转发权限）"}


def diagnose_account(cookie: str, probe_uid: str = "") -> dict:
    """综合诊断账户：登录态 + **真实抓取能力** + 转发能力。

    v1.1.1 关键修复：此前只校验「能否登录」「能否拿到 st 令牌」，而二者都可能因
    XSRF-TOKEN 残留而假阳性；用户遇到的是「检测全部正常，但一个微博都抓不到」。
    现在在登录校验通过后**追加一次真实抓取探测**（与轮询走完全相同的代码路径），
    只有真正能取到数据才算 status=ok。

    status: ok / cookie_expired / no_repost_perm / risk_control / network_error / unknown
    """
    login = check_login(cookie)
    result = {"login": login, "repost": None, "probe": None,
              "status": login.get("status") or "unknown", "message": ""}

    if not login.get("ok"):
        st = login.get("status") or "cookie_expired"
        result["status"] = st
        result["message"] = login.get("error") or "Cookie 已失效"
        # 只有「确证失效 / 风控 / 网络异常」才在此收口；
        # unknown 交由下面的真实抓取探测裁决 —— 能不能抓到数据才是最终标准，
        # 避免接口形态变化导致误报「请重新登录」。
        if st == "cookie_expired":
            result["message"] += " · 请在「账户管理」删除该账户后重新登录"
            return result
        if st in ("risk_control", "network_error"):
            return result

    # 登录校验通过（或无法确认）→ 做一次与轮询同路径的真实抓取探测
    uid = probe_uid or login.get("uid") or ""
    if uid:
        pr = get_latest_posts(uid, cookie, page=1, count=1)
        result["probe"] = {"ok": pr.get("ok"), "status": pr.get("status", "ok"),
                           "reason": pr.get("reason", "")}
        if not pr.get("ok"):
            st = pr.get("status") or "unknown"
            # 「该用户暂无微博」不属于账户问题，仍按正常账户处理
            if st != "empty":
                result["status"] = st
                if st == "cookie_expired":
                    result["message"] = ("抓取被要求重新登录 · Cookie 已失效，"
                                         "请在「账户管理」删除该账户后重新登录")
                elif st == "risk_control":
                    result["message"] = "抓取被微博风控拦截，请降低轮询频率或稍后重试"
                elif st == "network_error":
                    result["message"] = "抓取网络异常，请检查网络后重试"
                else:
                    result["message"] = pr.get("reason") or "抓取失败"
                return result
        # 探测成功：把续期后的 Cookie 带回，供上层回写保活
        if pr.get("renewed_cookie"):
            result["renewed_cookie"] = pr["renewed_cookie"]
        if not login.get("ok") and pr.get("ok"):
            # 登录接口未能确认，但真实抓取成功 → 以抓取结果为准判为正常
            result["status"] = "ok"
            result["message"] = "登录接口未能确认，但真实抓取正常，账户可用"
            result["login"] = {"ok": True, "login": True, "status": "ok",
                               "uid": login.get("uid", ""),
                               "screen_name": login.get("screen_name", ""),
                               "note": "由抓取探测确认"}
            repost = check_repost_capability(cookie)
            result["repost"] = repost
            if not repost.get("ok"):
                result["status"] = repost.get("status") or "no_repost_perm"
                result["message"] = repost.get("error") or "可抓取，但无法获取转发令牌"
            return result

    repost = check_repost_capability(cookie)
    result["repost"] = repost
    if repost.get("ok"):
        result["status"] = "ok"
        result["message"] = "登录正常，抓取与转发能力均正常"
    else:
        result["status"] = repost.get("status") or "no_repost_perm"
        result["message"] = repost.get("error") or "可登录，但无法获取转发令牌"
        if result["status"] == "cookie_expired":
            result["message"] += " · 请在「账户管理」删除该账户后重新登录"
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
            # v1.1.1：先判定「未登录 / 风控」，把失败性质结构化，避免一律报成「转发失败」
            if _looks_not_login(r.status_code, r.text, j):
                return {"ok": False, "status": "cookie_expired",
                        "error": "转发接口要求重新登录：Cookie 已失效，请在「账户管理」重新登录",
                        "raw": j}
            if _looks_risk_control(r.status_code, r.text):
                return {"ok": False, "status": "risk_control",
                        "error": f"转发被微博风控拦截（HTTP {r.status_code}），请降低频率后重试",
                        "raw": j}
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
