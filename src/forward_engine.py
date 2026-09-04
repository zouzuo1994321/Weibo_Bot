# -*- coding: utf-8 -*-
"""
转发引擎：核心调度逻辑。
- 按监控列表逐个 UID 拉取最新微博
- 识别「相比上次检查」新增的微博
- 本轮只随机挑选一条新微博进行转发（普通 / AI 增强）
- 命中关键词黑名单则跳过该条
- 记录被监控对象发过的微博（文字 + 图片）与软件转发记录
"""
import os
import random

from ai_generator import generate as ai_generate
from config_manager import get_config
from history_db import get_history_db
from logger import info, warn, error
from monitor_manager import get_monitor_manager
from paths import BASE_DIR, IMAGE_DIR, DATA_DIR
from licensemgr import can_forward
from weibo_client import (download_image, get_latest_posts, get_user_info,
                          repost)


def _is_newer(post_id: str, last_id: str) -> bool:
    """判断 post_id 是否比 last_id 更新（数值大者更新）。"""
    if not last_id:
        return True
    try:
        return int(post_id) > int(last_id)
    except Exception:
        return post_id > last_id


def _now():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _download_post_images(images, uid, post_id, cookie):
    saved = []
    for i, url in enumerate(images[:9], 1):
        ext = os.path.splitext(url.split("?")[0])[1] or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            ext = ".jpg"
        fname = f"{uid}_{post_id}_{i}{ext}"
        path = os.path.join(IMAGE_DIR, fname)
        if download_image(url, path, cookie):
            saved.append(os.path.relpath(path, DATA_DIR))
    return saved


def _download_avatar(avatar_url: str, uid: str, cookie: str) -> str:
    """下载用户头像到本地 avatars 目录，返回相对 DATA_DIR 的路径。"""
    if not avatar_url or not uid:
        return ""
    try:
        avatar_dir = os.path.join(IMAGE_DIR, "avatars")
        os.makedirs(avatar_dir, exist_ok=True)
        ext = os.path.splitext(avatar_url.split("?")[0])[1] or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            ext = ".jpg"
        path = os.path.join(avatar_dir, f"{uid}{ext}")
        if download_image(avatar_url, path, cookie):
            return os.path.relpath(path, DATA_DIR)
    except Exception as e:
        warn(f"下载头像 UID={uid} 失败：{e}")
    return ""


def _record_monitored_post(post, uid, screen_name, cookie, avatar_local=""):
    db = get_history_db()
    # 避免重复写入
    existing = db.get_monitored_posts(uid=uid, limit=500)
    if any(p["post_id"] == post["id"] for p in existing):
        return False
    imgs = _download_post_images(post.get("images", []), uid, post["id"], cookie)
    db.add_monitored_post(
        uid=uid, screen_name=screen_name, post_id=post["id"],
        content=post["text"], images=imgs, created_at=post.get("created_at", ""),
        avatar_local=avatar_local,
    )
    return True


def fetch_uid(uid, cookie, item):
    """拉取单个 UID 的最新微博，记录到历史库，更新监控状态。

    返回 (posts, new_posts, screen_name)。
    """
    resp = get_latest_posts(uid, cookie, page=1, count=10)
    if not resp.get("ok"):
        get_monitor_manager().update(uid, {
            "status": "error", "last_check": _now()})
        return [], [], item.get("screen_name", "")

    posts = resp.get("posts", [])
    # 刷新昵称与头像
    avatar_local = item.get("avatar_local", "")
    if posts:
        info_resp = get_user_info(uid, cookie)
        if info_resp.get("ok"):
            item["screen_name"] = info_resp.get("screen_name", item.get("screen_name", ""))
            avatar_url = info_resp.get("avatar_url", "")
            if avatar_url:
                avatar_local = _download_avatar(avatar_url, uid, cookie) or avatar_local

    last_id = item.get("last_post_id", "")
    # 跳过置顶微博（置顶非真实新动态，且首次运行时可能被误判为新）
    new_posts = [p for p in posts if _is_newer(p["id"], last_id) and not p.get("is_top")]

    # 记录所有抓取到的微博（含图片）到历史库
    for p in posts:
        _record_monitored_post(p, uid, item.get("screen_name", ""), cookie, avatar_local)

    # 更新最新已知 id（始终指向最新，避免下次重复）
    newest_id = posts[0]["id"] if posts else last_id
    get_monitor_manager().update(uid, {
        "last_post_id": newest_id,
        "last_post_text": posts[0]["text"][:200] if posts else "",
        "status": "online",
        "last_check": _now(),
        "screen_name": item.get("screen_name", ""),
        "avatar_local": avatar_local,
    })
    return posts, new_posts, item.get("screen_name", "")


def _forward_one(acc_id, acc_name, cookie, uid, screen_name, chosen):
    """对指定微博执行一次转发（含 AI 生成、黑名单、repost、历史记录）。"""
    cfg = get_config()
    blacklist = cfg.get("blacklist", []) or []
    ai_on = cfg.get("ai_enabled", False)
    persona = cfg.get("persona", "幽默")
    custom = cfg.get("custom_personas", {}).get(persona)

    comment = ""
    ftype = "normal"
    if ai_on:
        try:
            comment = ai_generate(chosen["text"], persona, custom)
            ftype = "ai"
        except Exception as e:
            warn(f"[{uid}] AI 文案生成失败，降级为普通转发：{e}")
            comment = ""
            ftype = "normal"

    # 关键词黑名单检测（原始内容 + 转发内容）
    haystack = (chosen["text"] + " " + comment).lower()
    hit = [w for w in blacklist if w and w.lower() in haystack]
    db = get_history_db()
    if hit:
        db.add_forward(acc_id, acc_name, uid, screen_name, chosen["id"],
                       chosen["text"], comment, ftype,
                       "skipped", reason=f"命中黑名单：{','.join(hit)}")
        return {"action": "skipped", "detail": f"命中黑名单跳过：{','.join(hit)}"}

    # 执行转发
    r = repost(chosen["id"], comment, cookie, uid=uid)
    if r.get("ok"):
        db.add_forward(acc_id, acc_name, uid, screen_name, chosen["id"],
                       chosen["text"], comment, ftype, "success")
        return {"action": "forwarded", "detail": f"已转发（{ftype}）：{comment or '纯转发'}"}
    else:
        db.add_forward(acc_id, acc_name, uid, screen_name, chosen["id"],
                       chosen["text"], comment, ftype,
                       "failed", reason=r.get("error", "未知错误"))
        return {"action": "failed", "detail": f"转发失败：{r.get('error', '未知错误')}"}


def run_once():
    """执行一轮完整轮询：每个监控对象拉取并记录微博，
    若存在新微博则从中随机挑选一条进行转发。"""
    from account_manager import get_account_manager
    acc_mgr = get_account_manager()
    accounts = acc_mgr.list_accounts()
    if not accounts:
        return {"ok": False, "error": "没有可用的微博账户，请先登录", "results": []}
    # 默认使用第一个账户进行转发（如需多账户轮询可扩展）
    acc = accounts[0]
    cookie = acc_mgr.get_cookie(acc["id"])

    mon_mgr = get_monitor_manager()
    items = mon_mgr.list()
    if not items:
        return {"ok": True, "error": "", "results": [],
                "message": "监控列表为空"}

    results = {}
    candidates = []
    for it in items:
        uid = it["uid"]
        try:
            posts, new_posts, screen_name = fetch_uid(uid, cookie, it)
        except Exception as e:
            res = {"uid": uid, "screen_name": it.get("screen_name", ""),
                   "new_count": 0, "action": "error", "detail": str(e)}
            results[uid] = res
            error(f"处理 UID {uid} 异常：{e}")
            continue

        results[uid] = {"uid": uid, "screen_name": screen_name,
                        "new_count": len(new_posts), "action": "none",
                        "detail": "无新微博"}
        for p in new_posts:
            candidates.append({"uid": uid, "screen_name": screen_name, "post": p})

    forwarded = 0
    if not can_forward():
        # 试用结束且未授权：仅记录监控微博，不执行转发
        for uid in results:
            if results[uid]["action"] == "none":
                results[uid]["detail"] = "转发未授权（试用已结束或未激活授权）"
        return {"ok": True, "error": "", "results": list(results.values()),
                "forwarded": 0,
                "message": "转发功能未授权，已跳过转发（监控记录仍正常进行）"}

    if candidates:
        chosen_item = random.choice(candidates)
        uid = chosen_item["uid"]
        screen_name = chosen_item["screen_name"]
        post = chosen_item["post"]
        res = _forward_one(acc["id"], acc.get("display_name", ""), cookie,
                           uid, screen_name, post)
        results[uid]["action"] = res["action"]
        results[uid]["detail"] = res["detail"]
        if res["action"] == "forwarded":
            forwarded = 1
            info(f"本轮随机选中 UID={uid} 转发：{res['detail']}")
        # 其他有新增但未被选中的对象给出友好提示
        for c in candidates:
            if c["uid"] != uid and results[c["uid"]]["action"] == "none":
                results[c["uid"]]["detail"] = "本轮随机未选中"

    if not candidates:
        message = "本轮无新微博"
    elif forwarded:
        message = "本轮转发 1 条"
    else:
        message = "本轮选中 1 条但未转发"
    return {"ok": True, "error": "", "results": list(results.values()),
            "forwarded": forwarded, "message": message}
