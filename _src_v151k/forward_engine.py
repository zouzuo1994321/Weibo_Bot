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
# 【v1.2.0】IMAGE_DIR → get_image_dir()，支持自定义头像目录
from paths import BASE_DIR, DATA_DIR, get_image_dir, rel_to_data
from licensemgr import can_forward
from account_manager import get_account_manager
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

# 【v1.6.17】本轮「判空」的对象集合 —— 接口正常返回、只是该账号近半年没有公开微博。
# 判空不是故障：一色雨的账号资料页有 297 条微博，但最新一条停在 2025-08-19，
# mymblog 按可见窗口返回空 list。旧版逐对象记 WARN「拉取失败」，两天 142 行、
# 占全部 HTTP 告警 98%，把真正的故障埋了。现在改由 run_once 每轮汇总一行。
# 用集合而非计数：同一对象本轮被重复拉取不会重复算。
_round_empty_uids = set()


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


def _compute_forward_offset(cfg, slot=None):
    """计算转发时间随机偏移量（秒）。

    目标：转发不在轮询触发时刻「齐步走」，而是在「设定间隔 ±poll_jitter_ratio」
    范围内随机散开，降低被平台识别为机器人批量操作的风险；同时把延后上限收紧到
    设定间隔的 ±15%，保证转发在合理时间内可见地完成（不再延后到 ~27 分钟才执行）。

    例如：间隔 30 分钟、ratio=0.15 → 实际延后 0 ~ 4.5 分钟随机触发。

    【v1.6.17】`slot=(序号, 总数)` 时改用**分层抽样**。
    旧版对每条转发各自独立取 `uniform(0, cap)`，N 次独立抽样必然扎堆：
    真机两天实测 179 个圈，相邻间隔**最短 0 秒**、`<10 秒` 占 15.1%、中位仅 30 秒。
    后果有三：① 进度环「一圈=一条」时，0~2 秒的圈看上去仍是一闪；
    ② 两条落到同一秒，`forward_runs` 去重把 2 圈折叠成 1 圈；
    ③ 5 条挤在 2 分半内发出，本身就是「批量转发」特征。

    改成把窗口均分成 N 段、每段只取中间 60%（0.2~0.8 段长）后：
    - 相邻两条的最小间隔 = 段长 × 0.4（15 分钟/5 条 → 27×0.4 ≈ 10.8 秒）；
    - 平均间隔 = 段长，整体仍铺满原窗口，既散开又不改变「多久内发完」的上限。
    """
    interval = max(1, int(cfg.get("interval_minutes", 30))) * 60
    ratio = cfg.get("poll_jitter_ratio", 0.15)
    try:
        ratio = float(ratio)
    except Exception:
        ratio = 0.15
    cap = interval * ratio  # 延后上限 = 设定间隔 × ±ratio（默认 ±15%）
    cap = max(0.0, cap)
    if slot:
        try:
            idx, total = int(slot[0]), max(1, int(slot[1]))
        except Exception:
            idx, total = 0, 1
        idx = max(0, min(total - 1, idx))
        step = cap / total
        return random.uniform(idx * step + step * 0.2,
                              idx * step + step * 0.8)
    return random.uniform(0, cap)


def _schedule_forward(delay, fn, meta=None):
    """延迟 delay 秒后在后台线程执行转发（用于工作时段内随机偏移）。

    【v1.6.17】到期执行**必须留痕**。旧版只在异常时 `error()`，成功路径一个字不写，
    于是排程点之后日志一片空白 —— 排查时极易误判成「排程了但根本没执行」。
    现在无论成功、跳过还是失败都记一行，带上对象与耗时。
    """
    meta = meta or {}

    def _run():
        time.sleep(delay)
        t0 = time.time()
        tag = "UID=%s（%s）" % (meta.get("uid", "?"), meta.get("screen_name") or "-")
        try:
            r = fn() or {}
            cost = int((time.time() - t0) * 1000)
            act = r.get("action") or "unknown"
            det = r.get("detail") or ""
            line = (f"排程到期执行 · {tag} · 动作={act} · 耗时 {cost}ms · {det}")
            if act in ("forwarded", "skipped"):
                info(line, CAT_FWD)
            else:
                warn(line, CAT_FWD)
        except Exception as e:
            error(f"排程到期执行失败 · {tag} · {e}", CAT_FWD)

    threading.Thread(target=_run, daemon=True, name="fwd-delayed").start()


# ---------------- 每轮排程数量（v1.6.1） ----------------
# 用户可设置「每轮轮询后要排程发送的微博条数」。默认 1（历史行为），上限 10 条
# —— 再多容易在短时间内形成批量转发特征，反而提高被风控的概率。
FORWARD_COUNT_KEY = "forward_count"
FORWARD_COUNT_MIN = 1
FORWARD_COUNT_MAX = 10
# 【v1.6.12】两个可选策略开关
FORWARD_JITTER_KEY = "forward_count_jitter"      # 排程波动
FORWARD_IMAGES_ONLY_KEY = "forward_images_only"  # 仅转发含图博文


def get_forward_count(cfg=None) -> int:
    """读取并钳制「每轮排程数量」（1~10，缺省/非法值一律回退到 1）。"""
    cfg = cfg or get_config()
    try:
        n = int(cfg.get(FORWARD_COUNT_KEY, 1))
    except Exception:
        n = 1
    return max(FORWARD_COUNT_MIN, min(FORWARD_COUNT_MAX, n))


def get_forward_count_for_round(cfg=None) -> int:
    """【v1.6.12】本轮实际要转发几条 —— 排程波动开关的落点。

    「排程数量」在用户心里是**上限**而不是固定值：设为 5 且开启波动时，
    每轮在 1~5 之间随机取一个数。这样长时间看下来每轮条数忽多忽少，
    不会呈现「每隔 N 分钟必发 K 条」的机械节律（那是风控最容易识别的特征）。

    关闭波动时行为与 v1.6.1 完全一致（固定 N 条）；N=1 时波动无意义，直接返回 1，
    避免把「我只想每轮 1 条」的用户随机成 0 条。
    """
    cfg = cfg or get_config()
    n = get_forward_count(cfg)
    if n <= 1:
        return n
    try:
        jitter = bool(cfg.get(FORWARD_JITTER_KEY, False))
    except Exception:
        jitter = False
    return random.randint(1, n) if jitter else n


def _filter_images_only(candidates):
    """【v1.6.12】只保留「含图」博文（`post.images` 非空）。

    图片直链来自 `weibo_client.extract_images()`（兼容 pics / pic_infos），
    为空即纯文字 / 纯视频 —— 纯视频不计入「含图」，因为转发视频博文时
    微博客户端只会渲染出链接卡片，观感与用户预期（配图）不符。
    """
    out = []
    for c in candidates:
        try:
            if (c.get("post") or {}).get("images"):
                out.append(c)
        except Exception:
            continue
    return out


def _pick_candidates(candidates, n):
    """从候选池中随机挑 n 条微博。

    优先「一条一个监控对象」（先把不同 UID 各取一条），不足 n 条时才回到同一
    对象里补 —— 这样排程出的多条转发天然分散在不同账号 / 话题上，不会在同一条
    微博下堆叠多条转发记录，也避免了同一对象连发被判定为刷屏。
    """
    pool = list(candidates)
    random.shuffle(pool)
    n = max(0, int(n))
    if n <= 0 or not pool:
        return []
    if n == 1:
        return pool[:1]
    picked, seen = [], set()
    for c in pool:
        if len(picked) >= n:
            break
        uid = c.get("uid")
        if uid in seen:
            continue
        seen.add(uid)
        picked.append(c)
    if len(picked) < n:
        for c in pool:
            if len(picked) >= n:
                break
            if any(c is p for p in picked):
                continue
            picked.append(c)
    return picked


# 同一监控对象被选中多条时的状态聚合优先级（取「更值得关注」的那个）
_ACTION_RANK = {"scheduled": 4, "forwarded": 3, "failed": 2, "skipped": 1, "none": 0}


def _merge_action(a, b):
    return a if _ACTION_RANK.get(a, 0) >= _ACTION_RANK.get(b, 0) else b


def _writeback_cookie(acc_id, old_cookie, new_cookie):
    """把续期后的 Cookie 回写到账户（会话保活）。仅在确有变化时落盘。"""
    if not new_cookie or new_cookie == old_cookie:
        return
    try:
        get_account_manager().update_cookie(
            acc_id, new_cookie, "转发/抓取时已自动续期 Cookie（会话保活）")
    except Exception as e:
        debug(f"Cookie 续期回写失败：{e}", CAT_HTTP)


def _is_no_permission(msg):
    """该失败是否属于「内容不可转发」（而非我们的链路上出问题）。

    【v1.6.17】微博对「作者设置了可见性」的微博回 `ok=0` +
    `由于作者设置了可见性，你没有权限转发此微博`。这既不是登录态失效、也不是网络
    故障，我们做什么都转发不了 —— 归到 failed 会让历史页的失败数虚高，也容易让人
    以为账号出问题。单独按「跳过」记，理由写清楚。
    """
    s = str(msg or "")
    return any(k in s for k in (
        "没有权限转发", "设置了可见性", "无权转发", "不能转发此微博",
        # 【v1.6.18】11-23 实测：同一个作者把可见性改成「对方设置」口径后，
        # 回包是 `由于对方设置，你无法进行操作`，旧关键词一个都没命中，
        # 于是又被记成 failed（两天 3 条全栽在这句上）。
        "由于对方设置", "无法进行操作", "无法进行此操作",
        "该微博已被删除", "微博不存在"))


def _repost_now(acc_id, acc_name, cookie, uid, screen_name, post, comment, ftype, persona=""):
    """实际执行转发 + 入库（供立即转发或延迟转发调用）。含重复转发兜底。"""
    db = get_history_db()
    if db.is_post_forwarded(post["id"]):
        return {"action": "skipped", "detail": "该微博已被转发，跳过（排程重复）"}
    r = repost(post["id"], comment, cookie, uid=uid)
    if r.get("ok"):
        db.add_forward(acc_id, acc_name, uid, screen_name, post["id"],
                       post["text"], comment, ftype, "success", persona=persona)
        # v1.1.7 强化回写：转发响应里的续期 Cookie 立即回写（会话保活）
        _writeback_cookie(acc_id, cookie, r.get("cookie"))
        return {"action": "forwarded", "detail": f"已转发（{ftype}）：{comment or '纯转发'}"}
    else:
        # 【v1.6.17】「作者设置了可见性」这类不可转发内容按「跳过」记，不记 failed：
        # 两天真机 3 条失败全是它，记成失败会让失败率虚高、也误导排查方向。
        raw = r.get("error", "未知错误")
        if _is_no_permission(raw):
            db.add_forward(acc_id, acc_name, uid, screen_name, post["id"],
                           post["text"], comment, ftype,
                           "skipped", reason=f"内容不可转发：{raw}", persona=persona)
            return {"action": "skipped",
                    "detail": f"内容不可转发（作者设置了可见性，已跳过）：{raw}"}
        db.add_forward(acc_id, acc_name, uid, screen_name, post["id"],
                       post["text"], comment, ftype,
                       "failed", reason=raw, persona=persona)
        return {"action": "failed", "detail": f"转发失败：{raw}"}


def _download_post_images(images, uid, post_id, cookie):
    saved = []
    for i, url in enumerate(images[:9], 1):
        ext = os.path.splitext(url.split("?")[0])[1] or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            ext = ".jpg"
        fname = f"{uid}_{post_id}_{i}{ext}"
        # 【v1.2.2】懒建目录：ensure_dirs() 不再预建大目录，谁写谁建
        _img_dir = get_image_dir()
        try:
            os.makedirs(_img_dir, exist_ok=True)
        except Exception:
            pass
        path = os.path.join(_img_dir, fname)
        if download_image(url, path, cookie):
            # 【v1.2.3】跨盘符时 relpath 会抛 ValueError（见 paths.rel_to_data）
            saved.append(rel_to_data(path))
    return saved


def _download_avatar(avatar_url: str, uid: str, cookie: str) -> str:
    """下载用户头像到本地 avatars 目录，返回相对 DATA_DIR 的路径。"""
    if not avatar_url or not uid:
        return ""
    try:
        avatar_dir = os.path.join(get_image_dir(), "avatars")
        os.makedirs(avatar_dir, exist_ok=True)
        ext = os.path.splitext(avatar_url.split("?")[0])[1] or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            ext = ".jpg"
        path = os.path.join(avatar_dir, f"{uid}{ext}")
        if download_image(avatar_url, path, cookie):
            # 【v1.2.3】同上：images 目录可能被用户指到别的盘符
            return rel_to_data(path)
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

    返回 (posts, new_posts, screen_name, renewed_cookie, ok)。
    ok 表示「这次拉取真的成功了」（v1.6.6）——账户表的「最近使用」要靠它判定
    「最近一次成功轮询」，不能靠 posts 是否为空（无新微博也是成功）。
    renewed_cookie 为微博 Set-Cookie 续期后的新 Cookie（无续期时为空串），
    调用方应回写账户实现「会话保活」，显著延长 Cookie 寿命（v1.1.1）。
    """
    resp = get_latest_posts(uid, cookie, page=1, count=10)
    if not resp.get("ok"):
        st = resp.get("status") or "error"
        # 【v1.6.17】判空单独处理：接口是通的，只是这个账号近半年没有公开微博。
        # 监控状态不该亮红（那会变成「功能正常却显示故障」的翻版），
        # 告警也不该逐对象刷屏，统一交给 run_once 汇总。
        if st == "empty":
            _round_empty_uids.add(uid)
            # 接口是通的（HTTP 200 / ok=1），只是这个账号近半年没有公开微博。
            # 状态记「在线」并推进「最近检查」，否则监控列表会一直挂着旧的检查时间，
            # 看上去像「已经不监控了」；也不该标红 —— 那是「功能正常却显示故障」。
            try:
                get_monitor_manager().update(uid, {
                    "status": "online", "last_check": _now()})
            except Exception:
                pass
            debug(f"抓取无近期内容 UID={uid} · 近半年没有可转发的公开微博（非故障）",
                  CAT_HTTP)
            return [], [], item.get("screen_name", ""), "", False
        get_monitor_manager().update(uid, {
            "status": "error", "last_check": _now()})
        # v1.1.1：按失败性质给出可操作日志，而不是笼统一句「拉取失败」
        reason = resp.get("reason") or resp.get("error") or "未知原因"
        if st == "cookie_expired":
            error(f"抓取失败 UID={uid} · 原因：Cookie 已失效，"
                  f"请在「账户管理」删除该账户后重新登录", CAT_HTTP)
        elif st == "risk_control":
            warn(f"抓取失败 UID={uid} · 原因：被微博风控拦截，建议调大轮询间隔后重试",
                 CAT_HTTP)
        elif st == "network_error":
            warn(f"抓取失败 UID={uid} · 原因：网络异常（{reason}）", CAT_HTTP)
        else:
            warn(f"抓取失败 UID={uid} · 原因：{reason}", CAT_HTTP)
        return [], [], item.get("screen_name", ""), "", False

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
    return (posts, new_posts, item.get("screen_name", ""),
            resp.get("renewed_cookie", ""), True)


def _forward_one(acc_id, acc_name, cookie, uid, screen_name, chosen, force_immediate=False,
                 slot=None):
    """对指定微博执行一次转发（含 AI 生成、黑名单、repost、历史记录）。

    转发时间策略：若开启「转发时间随机偏移」且非强制立即（force_immediate），
    则把实际 repost 延迟到「设定间隔 ±poll_jitter_ratio」范围内的随机时刻执行
    （后台线程，默认至多延后 4.5 分钟 / 30 分钟间隔），避免多账号/多对象在轮询
    触发时刻「齐步走」；force_immediate=True 时（如用户手动「立即转发」、
    「立即轮询一次」）则立即转发，给用户即时反馈。

    【v1.6.17】`slot=(本轮第几条, 本轮共几条)` 透传给 `_compute_forward_offset`
    做分层抽样 —— 否则 N 条转发各自独立取随机偏移会扎堆（真机实测最短间隔 0 秒）。
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
        offset = _compute_forward_offset(cfg, slot=slot)
        if offset > 0:
            run_at = datetime.now() + timedelta(seconds=offset)
            post = dict(chosen)  # 快照，避免后续被复用覆盖
            _schedule_forward(offset, lambda: _repost_now(
                acc_id, acc_name, cookie, uid, screen_name, post, comment, ftype, persona),
                meta={"uid": uid, "screen_name": screen_name,
                      "post_id": post.get("id", ""),
                      "run_at": run_at.strftime("%H:%M:%S")})
            # 【v1.4.20】带回实际转发执行时间（浮点秒），调度器据此暴露 next_forward_run，
            # 供前端「本日转发次数」进度环把 100% 对齐到真正的转发时刻。
            return {"action": "scheduled",
                    "detail": f"已排程转发（预计 {run_at.strftime('%H:%M:%S')} 执行）：{comment or '纯转发'}",
                    "run_at": run_at.timestamp()}

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
        ok_fetches = 0
        _round_empty_uids.clear()   # 【v1.6.17】每轮重新统计判空对象
        for it in items:
            uid = it["uid"]
            _poll_progress["current"] = it.get("screen_name") or uid
            try:
                posts, new_posts, screen_name, renewed, fok = fetch_uid(uid, cookie, it)
                if fok:
                    ok_fetches += 1
                # 会话保活：微博下发的续期 Cookie 立即回写并沿用（v1.1.1）
                if renewed and renewed != cookie:
                    if acc_mgr.update_cookie(acc["id"], renewed,
                                             "轮询时已自动续期 Cookie（会话保活）"):
                        debug(f"账户 Cookie 已自动续期（保活），后续对象沿用新 Cookie",
                              CAT_HTTP)
                    cookie = renewed
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

        # 【v1.6.6】「最近使用」= 最近一次成功轮询的时间。
        # 判定口径是「本轮至少有一个对象真的拉取成功」——Cookie 若已失效，所有对象都会
        # 失败，此时不更新，免得把一次废轮询记成有效使用。mark_used 同时会滑动 7 天窗口。
        if ok_fetches:
            try:
                acc_mgr.mark_used(acc["id"])
            except Exception as e:
                warn(f"记录最近使用时间失败：{e}", CAT_FWD)
            # 【v1.6.9】真实抓取成功 = 登录态有效的**直接证据**（比任何探针都硬）。
            # 顺手把实测结论纠正为 valid：这样一旦哪一路探针误报了「已失效」，
            # 下一轮正常轮询就会自动复原，不必等用户手点「检测」。
            try:
                rv = acc_mgr.set_verified(acc["id"], True, source="轮询抓取成功",
                                          detail=f"本轮 {ok_fetches}/{len(items)} 个对象拉取成功")
                if rv.get("changed") and rv.get("prev") == "expired":
                    warn("本轮抓取成功，已自动纠正此前误标的「实测已失效」", CAT_FWD)
            except Exception as e:
                debug(f"记录抓取成功的登录态证据失败：{e}", CAT_FWD)
            try:
                from api_bridge import _state_dirty as _sd
                _sd()          # 账户表立即刷新，不用等下一次轮询
            except Exception:
                pass

        _poll_progress["phase"] = "forward"
        # 【v1.6.17】判空对象汇总成一行：既不掩盖问题（看得到有几个对象没内容），
        # 又不会像旧版那样每个对象两条 WARN 把真故障埋掉。
        if _round_empty_uids:
            _names = []
            for _it in items:
                if _it.get("uid") in _round_empty_uids:
                    _names.append(_it.get("screen_name") or _it.get("uid", ""))
            _head = "、".join(_names[:5]) + ("…" if len(_names) > 5 else "")
            info(f"本轮 {len(_round_empty_uids)} 个对象近半年无公开微博（接口正常，非故障）：{_head}",
                 CAT_FWD)
        info(f"本轮抓取完成 · 候选池 {len(candidates)} 条（已剔除已处理：成功/跳过/失败）"
             f"· 覆盖 {len(results)} 个对象", CAT_FWD)
        # 【v1.6.12】仅转发含图博文：必须在「排程挑选」之前收窄候选池，
        # 否则随机挑到无图微博后再丢弃，会把本轮的条数悄悄做少。
        _cfg_round = get_config()
        try:
            _img_only = bool(_cfg_round.get(FORWARD_IMAGES_ONLY_KEY, False))
        except Exception:
            _img_only = False
        _img_dropped = 0
        _dropped_uids = set()
        if _img_only:
            _before = len(candidates)
            _keep = _filter_images_only(candidates)
            _dropped_uids = {c.get("uid") for c in candidates} - {c.get("uid") for c in _keep}
            candidates = _keep
            _img_dropped = _before - len(candidates)
            if _before:
                info(f"仅转发含图博文：候选池 {_before} → {len(candidates)} 条"
                     f"（滤除 {_img_dropped} 条无图微博）", CAT_FWD)
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

        chosen_items = []
        if candidates:
            # 【v1.6.12】开启「排程波动」时，把排程数量当作**上限**：
            # 设 5 则每轮在 1~5 之间随机，避免固定节律。
            _want = get_forward_count_for_round(_cfg_round)
            chosen_items = _pick_candidates(candidates, _want)
            debug(f"本轮排程数量上限 {_want} 条 → 候选池 {len(candidates)} 条 "
                  f"→ 实际选中 {len(chosen_items)} 条", CAT_FWD)
        sched_n = direct_n = skip_n = fail_n = 0
        picked_uids = set()
        # 【v1.6.17】把「第几条 / 共几条」传下去做分层抽样，保证 N 条排程在偏移窗口
        # 里各占一段、不会扎堆到同一秒（进度环才能一圈圈地走）。
        _n_chosen = len(chosen_items)
        for _idx, item in enumerate(chosen_items):
            uid = item["uid"]
            screen_name = item["screen_name"]
            post = item["post"]
            try:
                res = _forward_one(acc["id"], acc.get("display_name", ""), cookie,
                                   uid, screen_name, post, force_immediate=force_immediate,
                                   slot=(_idx, _n_chosen))
            except Exception as e:
                error(f"转发 UID={uid} 异常：{e}", CAT_FWD)
                res = {"action": "failed", "detail": f"转发异常：{e}"}
            act = res.get("action") or "failed"
            det = res.get("detail") or ""
            picked_uids.add(uid)
            row = results.get(uid)
            if row is not None:
                if row.get("action") == "none":
                    row["action"] = act
                    row["detail"] = det
                else:
                    # 同一对象被选中多条：状态取更重要者，明细用「；」串接
                    row["action"] = _merge_action(row.get("action"), act)
                    row["detail"] = (row.get("detail") or "") + "；" + det
                # 【v1.4.21】排程转发的实际执行时刻必须透传——scheduler 靠它设置
                # next_forward_run 供前端进度环对齐 100%；v1.4.20 只合并了
                # action/detail，run_at 被丢弃，导致进度环恒回退到 next_run 终点。
                if res.get("run_at"):
                    row["run_at"] = res["run_at"]
            debug(f"候选池 {len(candidates)} 条 → 选中 UID={uid}（{screen_name}）"
                  f" 微博ID={post.get('id', '')} 内容「{(post.get('text') or '')[:40]}」", CAT_FWD)
            if act == "scheduled":
                sched_n += 1
                info(f"本轮随机选中 UID={uid} 排程转发：{det}", CAT_FWD)
            elif act == "forwarded":
                direct_n += 1
                info(f"本轮随机选中 UID={uid} 转发：{det}", CAT_FWD)
            elif act == "skipped":
                skip_n += 1
                warn(f"选中微博但已跳过 UID={uid} · {det}", CAT_FWD)
            else:
                fail_n += 1
                warn(f"选中微博但未完成转发 UID={uid} · 动作={act} · 详情={det}", CAT_FWD)
        # 其他有新增但未被选中的对象给出友好提示
        for c in candidates:
            if c["uid"] in picked_uids:
                continue
            if results.get(c["uid"], {}).get("action") == "none":
                results[c["uid"]]["detail"] = "本轮随机未选中"
        # 【v1.6.12】被「仅转发含图博文」整轮滤掉的对象，明细要写清原因 ——
        # 若仍显示「本轮随机未选中」，用户会以为只是运气不好，而真相是候选里没图。
        for _uid in _dropped_uids:
            _row = results.get(_uid)
            if _row and _row.get("action") == "none":
                _row["detail"] = "本轮未选中（无图博文已按设置滤除）"

        if not candidates:
            if _img_dropped:
                message = f"本轮无含图新微博（已滤除 {_img_dropped} 条无图微博）"
            else:
                message = "本轮无新微博"
        elif sched_n and direct_n:
            message = f"本轮排程转发 {sched_n} 条 · 立即转发 {direct_n} 条"
        elif sched_n:
            message = f"本轮排程转发 {sched_n} 条"
        elif direct_n:
            message = f"本轮转发 {direct_n} 条"
        elif skip_n:
            message = f"本轮选中 {skip_n} 条但被跳过"
        else:
            message = f"本轮选中 {len(chosen_items)} 条但未转发"
        if fail_n:
            warn(f"本轮有 {fail_n} 条转发失败", CAT_FWD)
        _poll_progress["active"] = False
        return {"ok": True, "error": "", "results": list(results.values()),
                "forwarded": sched_n + direct_n, "scheduled": sched_n,
                "message": message}
    finally:
        _run_lock.release()

