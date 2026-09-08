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
        _t0 = time.time()
        r = s.get(f"{WEB_BASE}/ajax/statuses/mymblog", params={
            "uid": uid, "page": page, "feature": 0}, timeout=15)
        _cost = int((time.time() - _t0) * 1000)
        j = _safe_json(r)
        feed = _extract_feed(j)
        debug(f"HTTP {r.status_code} mymblog uid={uid} page={page} · {_cost}ms · 原始 {len(feed or [])} 条",
              CAT_HTTP)
        if feed:
            posts = _build_posts(feed, count)
            if posts:
                debug(f"mymblog 解析成功 uid={uid} → {len(posts)} 条", CAT_HTTP)
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

    _err = "; ".join(err_parts) or "拉取失败"
    warn(f"拉取微博失败 uid={uid} page={page} · 已尝试 3 种接口 · 原因：{_err}", CAT_HTTP)
    return {"ok": False, "error": _err, "posts": []}


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
