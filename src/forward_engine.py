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
import threading
import time
from datetime import datetime, timedelta

from ai_generator import generate as ai_generate
from config_manager import get_config
from history_db import get_history_db
from logger import info, warn, error, debug, CAT_FWD, CAT_HTTP
from monitor_manager import get_monitor_manager
from paths import BASE_DIR, IMAGE_DIR, DATA_DIR
from licensemgr import can_forward
from weibo_client import (download_image, get_latest_posts, get_user_info,
                          repost)


# ---------------- 轮询进度追踪 ----------------
# 供前端「立即轮询一次」显示进度条：run_once 在遍历监控对象时实时更新该状态，
# manual_run_async 在后台线程执行，避免阻塞 pywebview 的 WebMessage 处理线程。
_poll_progress = {
    "active": False,
    "total": 0,
    "done": 0,
    "current": "",
    "phase": "",   # init / fetch / forward / done
    "result": None,
}


def get_poll_progress():
    """返回当前轮询进度（浅拷贝，避免外部修改内部状态）。"""
    return dict(_poll_progress)


# ---------------- 并发控制 ----------------
# 防止「启动自动轮询」与「立即轮询一次」同时进入 run_once 导致同一批新微博被重复转发。
# 采用非阻塞加锁：若已有轮询在进行，本次直接跳过（返回跳过说明），而非排队等待。
_run_lock = threading.Lock()


def manual_run_async(force_immediate=False):
    """后台异步执行一轮完整轮询，并实时更新 _poll_progress 供 frontend 轮询。
    返回 {ok, started} 立即返回，不阻塞调用线程。
    force_immediate=True 时本轮转发不延迟（忽略抖动），用于「立即转发」按钮。
    """
    if _poll_progress["active"]:
        return {"ok": False, "started": False, "error": "已有轮询正在进行"}
    _poll_progress["active"] = True
    _poll_progress["total"] = 0
    _poll_progress["done"] = 0
    _poll_progress["current"] = "准备中…"
    _poll_progress["phase"] = "init"
    _poll_progress["result"] = None

    def _worker():
        try:
            res = run_once(force_immediate=force_immediate)
        except Exception as e:  # 兜底，避免进度条卡在 active=True
            res = {"ok": False, "error": str(e), "results": []}
        _poll_progress["active"] = False
        _poll_progress["phase"] = "done"
        _poll_progress["result"] = res
        # 同步到调度器，使概览页「最近一次轮询结果」可见
        try:
            from scheduler import get_scheduler
            sch = get_scheduler()
            sch.last_result = res
            sch.last_run = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return {"ok": True, "started": True}


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


def _compute_forward_offset(cfg):
    """计算转发时间随机偏移量（秒）。

    目标：转发不在轮询触发时刻「齐步走」，而是在「设定间隔 ±poll_jitter_ratio」
    范围内随机散开，降低被平台识别为机器人批量操作的风险；同时把延后上限收紧到
    设定间隔的 ±15%，保证转发在合理时间内可见地完成（不再延后到 ~27 分钟才执行）。

    例如：间隔 30 分钟、ratio=0.15 → 实际延后 0 ~ 4.5 分钟随机触发。
    """
    interval = max(1, int(cfg.get("interval_minutes", 30))) * 60
    ratio = cfg.get("poll_jitter_ratio", 0.15)
    try:
        ratio = float(ratio)
    except Exception:
        ratio = 0.15
    cap = interval * ratio  # 延后上限 = 设定间隔 × ±ratio（默认 ±15%）
    return random.uniform(0, max(0, cap))


def _schedule_forward(delay, fn):
    """延迟 delay 秒后在后台线程执行转发（用于工作时段内随机偏移）。"""
    def _run():
        time.sleep(delay)
        try:
            fn()
        except Exception as e:
            error(f"延迟转发执行失败：{e}")
    threading.Thread(target=_run, daemon=True).start()


def _repost_now(acc_id, acc_name, cookie, uid, screen_name, post, comment, ftype, persona=""):
    """实际执行转发 + 入库（供立即转发或延迟转发调用）。含重复转发兜底。"""
    db = get_history_db()
    if db.is_post_forwarded(post["id"]):
        return {"action": "skipped", "detail": "该微博已被转发，跳过（排程重复）"}
    r = repost(post["id"], comment, cookie, uid=uid)
    if r.get("ok"):
        db.add_forward(acc_id, acc_name, uid, screen_name, post["id"],
                       post["text"], comment, ftype, "success", persona=persona)
        return {"action": "forwarded", "detail": f"已转发（{ftype}）：{comment or '纯转发'}"}
    else:
        db.add_forward(acc_id, acc_name, uid, screen_name, post["id"],
                       post["text"], comment, ftype,
                       "failed", reason=r.get("error", "未知错误"), persona=persona)
        return {"action": "failed", "detail": f"转发失败：{r.get('error', '未知错误')}"}


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

    # 候选判定：以「该微博是否已有任意转发处理记录（成功/跳过/失败）」为准，
    # 而非依赖 last_post_id 游标。原因：run_once 每轮只随机转发 1 条新微博，
    # 若用游标，转发 1 条后游标会被抬到最新一条，把同批其余未转发的新微博永久
    # 排除在候选之外（漏抓）。改为按 is_post_processed 判定后，每条未处理的新微博
    # 都会在后续轮询里持续进入候选，逐条被转发（保留每轮只转 1 条的防检测设计），
    # 同时已处理（含黑名单跳过 / 失败）的微博不再反复进入候选。
    db = get_history_db()
    new_posts = [p for p in posts
                 if (not p.get("is_top"))
                 and (not db.is_post_processed(p["id"]))]

    # 记录所有抓取到的微博（含图片）到历史库
    for p in posts:
        _record_monitored_post(p, uid, item.get("screen_name", ""), cookie, avatar_local)

    # 更新最新已知 id（仅用于监控状态展示，不再参与候选判定）
    newest_id = posts[0]["id"] if posts else item.get("last_post_id", "")
    get_monitor_manager().update(uid, {
        "last_post_id": newest_id,
        "last_post_text": posts[0]["text"][:200] if posts else "",
        "status": "online",
        "last_check": _now(),
        "screen_name": item.get("screen_name", ""),
        "avatar_local": avatar_local,
    })
    return posts, new_posts, item.get("screen_name", "")


def _forward_one(acc_id, acc_name, cookie, uid, screen_name, chosen, force_immediate=False):
    """对指定微博执行一次转发（含 AI 生成、黑名单、repost、历史记录）。

    转发时间策略：若开启「转发时间随机偏移」且非强制立即（force_immediate），
    则把实际 repost 延迟到「设定间隔 ±poll_jitter_ratio」范围内的随机时刻执行
    （后台线程，默认至多延后 4.5 分钟 / 30 分钟间隔），避免多账号/多对象在轮询
    触发时刻「齐步走」；force_immediate=True 时（如用户手动「立即转发」、
    「立即轮询一次」）则立即转发，给用户即时反馈。
    """
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
                       "skipped", reason=f"命中黑名单：{','.join(hit)}", persona=persona)
        return {"action": "skipped", "detail": f"命中黑名单跳过：{','.join(hit)}"}

    # 转发时间随机偏移：延迟到工作时段内随机时刻执行
    if (not force_immediate) and cfg.get("forward_time_jitter", True):
        offset = _compute_forward_offset(cfg)
        if offset > 0:
            run_at = datetime.now() + timedelta(seconds=offset)
            post = dict(chosen)  # 快照，避免后续被复用覆盖
            _schedule_forward(offset, lambda: _repost_now(
                acc_id, acc_name, cookie, uid, screen_name, post, comment, ftype, persona))
            return {"action": "scheduled",
                    "detail": f"已排程转发（预计 {run_at.strftime('%H:%M:%S')} 执行）：{comment or '纯转发'}"}

    return _repost_now(acc_id, acc_name, cookie, uid, screen_name, chosen, comment, ftype, persona)


def run_once(force_immediate=False):
    """执行一轮完整轮询：每个监控对象拉取并记录微博，
    若存在新微博则从中随机挑选一条进行转发。

    force_immediate=True 时忽略「转发时间随机偏移」，转发立即执行（用于用户手动
    「立即转发」「立即轮询一次」，给用户即时反馈）；自动轮询（force_immediate=False）
    则会在「设定间隔 ±15%」范围内随机延后转发，兼顾防检测与可见性。

    并发保护：若已有轮询（手动或自动）在执行，本次直接跳过，避免重复转发。
    """
    # 非阻塞加锁：已有轮询进行中则跳过，防止「启动轮询」与「立即轮询一次」重复转发同一批新微博
    if not _run_lock.acquire(blocking=False):
        return {"ok": True, "error": "", "results": [],
                "forwarded": 0, "skipped": True,
                "message": "已有轮询进行中，本次已跳过以避免重复转发"}
    try:
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
        _poll_progress["active"] = True
        _poll_progress["total"] = len(items)
        _poll_progress["done"] = 0
        _poll_progress["current"] = ""
        _poll_progress["phase"] = "fetch"
        if not items:
            _poll_progress["active"] = False
            warn("本轮轮询跳过：监控列表为空", CAT_FWD)
            return {"ok": True, "error": "", "results": [],
                    "message": "监控列表为空"}
        info(f"本轮轮询开始 · 监控对象 {len(items)} 个 · 转发账户「{acc.get('display_name') or acc.get('id', '')}」"
             f" · 立即转发={force_immediate}", CAT_FWD)
        debug(f"本轮监控对象：{[(it['uid'], it.get('screen_name') or '') for it in items]}", CAT_FWD)

        db = get_history_db()
        results = {}
        candidates = []
        for it in items:
            uid = it["uid"]
            _poll_progress["current"] = it.get("screen_name") or uid
            try:
                posts, new_posts, screen_name = fetch_uid(uid, cookie, it)
            except Exception as e:
                results[uid] = {"uid": uid, "screen_name": it.get("screen_name", ""),
                                "new_count": 0, "action": "error", "detail": str(e)}
                error(f"处理 UID {uid} 异常：{e}")
                _poll_progress["done"] += 1
                continue

            # 仅把「未成功转发过」的新微博纳入候选，避免重复转发同一推文
            new_count = 0
            for p in new_posts:
                if db.is_post_forwarded(p["id"]):
                    continue
                candidates.append({"uid": uid, "screen_name": screen_name, "post": p})
                new_count += 1
            results[uid] = {"uid": uid, "screen_name": screen_name,
                            "new_count": new_count, "action": "none",
                            "detail": "无新微博" if new_count == 0 else "本轮未选中"}
            _poll_progress["done"] += 1
            debug(f"抓取 {screen_name or uid}（{uid}）：本页 {len(posts)} 条 · 新增 {len(new_posts)} 条"
                  f" · 进入候选 {new_count} 条", CAT_FWD)

        _poll_progress["phase"] = "forward"
        forwarded = 0
        info(f"本轮抓取完成 · 候选池 {len(candidates)} 条（已剔除历史已转发）· 覆盖 {len(results)} 个对象", CAT_FWD)
        if not can_forward():
            warn("本轮不执行转发：授权未生效（试用已结束或未激活）· 监控记录仍正常写入", CAT_FWD)
            # 试用结束且未授权：仅记录监控微博，不执行转发
            for uid in results:
                if results[uid]["action"] == "none":
                    results[uid]["detail"] = "转发未授权（试用已结束或未激活授权）"
            _poll_progress["active"] = False
            return {"ok": True, "error": "", "results": list(results.values()),
                    "forwarded": 0,
                    "message": "转发功能未授权，已跳过转发（监控记录仍正常进行）"}

        if candidates:
            chosen_item = random.choice(candidates)
            uid = chosen_item["uid"]
            screen_name = chosen_item["screen_name"]
            post = chosen_item["post"]
            res = _forward_one(acc["id"], acc.get("display_name", ""), cookie,
                               uid, screen_name, post, force_immediate=force_immediate)
            results[uid]["action"] = res["action"]
            results[uid]["detail"] = res["detail"]
            debug(f"候选池 {len(candidates)} 条 → 随机选中 UID={uid}（{screen_name}）"
                  f" 微博ID={post.get('id', '')} 内容「{(post.get('text') or '')[:40]}」", CAT_FWD)
            if res["action"] in ("forwarded", "scheduled"):
                forwarded = 1
                info(f"本轮随机选中 UID={uid} 转发：{res['detail']}", CAT_FWD)
            else:
                warn(f"选中微博但未完成转发 UID={uid} · 动作={res['action']} · 详情={res['detail']}", CAT_FWD)
            # 其他有新增但未被选中的对象给出友好提示
            for c in candidates:
                if c["uid"] != uid and results[c["uid"]]["action"] == "none":
                    results[c["uid"]]["detail"] = "本轮随机未选中"

        if not candidates:
            message = "本轮无新微博"
        elif results[uid]["action"] == "scheduled" if candidates else False:
            message = "本轮排程转发 1 条"
        elif forwarded:
            message = "本轮转发 1 条"
        else:
            message = "本轮选中 1 条但未转发"
        _poll_progress["active"] = False
        return {"ok": True, "error": "", "results": list(results.values()),
                "forwarded": forwarded, "message": message}
    finally:
        _run_lock.release()

