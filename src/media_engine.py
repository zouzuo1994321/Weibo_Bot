# -*- coding: utf-8 -*-
"""相册监控：自动下载监控对象的相册图片。

按监控对象逐个拉取其相册（对应微博 `?tabtype=album`），图片下载到：

    data/media/{微博ID}_{昵称}/

- 首次执行为**全量下载**；
- 后续执行通过 media_files 表的 (url, media_type) 唯一索引实现**增量**：
  已下载过的资源自动跳过，不再重复请求网络；
- 执行「清理下载记录」只清空上述记录、不动本地文件，增量判定随之重置；
  再次下载时**同名文件直接覆盖**（文件名 = pid，无 pid 时取 URL 摘要）；
- 执行过程实时写入进度（对象 N/M、当前昵称、文件计数、阶段），供前端轮询显示；
- 支持「立即执行」与按「监控间隔」周期执行。
"""
import hashlib
import os
import re
import threading
import time

from config_manager import get_config
from history_db import get_history_db
from logger import info, warn, error, debug, exception, CAT_MEDIA
from monitor_manager import get_monitor_manager
from paths import MEDIA_DIR
from weibo_client import download_image, get_album_photos

# ---------------- 下载进度（供前端轮询） ----------------
_progress = {
    "active": False,
    "phase": "",          # fetch / download / done / error
    "total": 0,           # 监控对象总数
    "done": 0,            # 已完成对象数
    "current": "",        # 当前对象昵称
    "current_uid": "",
    "posts": 0,           # 已扫描微博数
    "files_done": 0,      # 新下载文件数
    "files_skipped": 0,   # 增量跳过数
    "files_failed": 0,
    "started_at": 0,
    "finished_at": 0,
    "message": "",
}
_lock = threading.Lock()
_run_lock = threading.Lock()


def get_media_progress():
    """返回当前下载进度快照。"""
    with _lock:
        return dict(_progress)


def _reset(total):
    with _lock:
        _progress.update({
            "active": True, "phase": "fetch", "total": total, "done": 0,
            "current": "", "current_uid": "", "posts": 0,
            "files_done": 0, "files_skipped": 0, "files_failed": 0,
            "started_at": time.time(), "finished_at": 0, "message": "",
        })


def _bump(**kw):
    with _lock:
        _progress.update(kw)


def _add(key, delta):
    with _lock:
        _progress[key] = _progress.get(key, 0) + delta


# ---------------- 目录与文件 ----------------
def _safe_name(name: str, default: str = "noname") -> str:
    """把昵称清洗为可安全用于文件系统的目录名。"""
    if not name:
        return default
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "", str(name)).strip()
    s = s.strip(". ")
    return (s or default)[:40]


def user_media_dir(uid: str, screen_name: str = "") -> str:
    """「微博ID + 微博名称」对应的用户目录。"""
    return os.path.join(MEDIA_DIR, f"{uid}_{_safe_name(screen_name, uid)}")


def _ext_from_url(url: str, default: str = "jpg") -> str:
    """从 URL 推断图片扩展名。"""
    m = re.search(r"\.(jpg|jpeg|png|gif|webp|bmp)(?:\?|$)", str(url), re.I)
    return (m.group(1).lower() if m else default)


# ---------------- 单个监控对象 ----------------
def _download_for_monitor(uid, screen_name, cookie, cfg):
    """下载一个监控对象的相册图片，返回统计字典。"""
    db = get_history_db()
    max_pages = max(1, int(cfg.get("max_pages", 5) or 5))

    udir = user_media_dir(uid, screen_name)   # 图片直接放「ID_昵称」目录，不再分子文件夹
    os.makedirs(udir, exist_ok=True)

    st = {"posts": 0, "done": 0, "skipped": 0, "failed": 0}

    def _file_name(base, ext):
        """固定文件名（同名直接覆盖）。

        需求：执行「清理下载记录」后，增量判定重置，再次下载遇到文件名一致的文件
        应直接覆盖，而不是生成 _1 / _2 后缀。因此这里不再做重名规避。
        """
        return f"{base}.{ext}"

    # ---- 相册：走「相册 tab」专属接口（?tabtype=album）----
    resp = get_album_photos(uid, cookie, max_pages)
    if resp.get("ok"):
        photos = resp["photos"]
        st["posts"] = len(photos)
        _add("posts", len(photos))
        info(f"{screen_name} 相册源：{resp.get('source')} · 获取 {len(photos)} 张", CAT_MEDIA)
        for i, ph in enumerate(photos, 1):
            iurl = ph.get("url") or ""
            if not iurl:
                continue
            if db.has_media_url(iurl, "image"):
                st["skipped"] += 1
                _add("files_skipped", 1)
                continue
            ext = _ext_from_url(iurl)
            # 文件名以 pid 为准；无 pid 时用 URL 摘要，保证「同一张图 = 同一个文件名」，
            # 清理下载记录后重新下载才能准确覆盖同一个文件。
            base = (ph.get("pid") or "").strip()
            if not base:
                base = hashlib.md5(iurl.encode("utf-8")).hexdigest()[:16]
            fname = _file_name(base, ext)
            fpath = os.path.join(udir, fname)
            if download_image(iurl, fpath, cookie):
                size = os.path.getsize(fpath) if os.path.exists(fpath) else 0
                rel = os.path.relpath(fpath, MEDIA_DIR).replace("\\", "/")
                db.add_media_file(uid, screen_name, base, "image", iurl,
                                  rel, fname, size, ph.get("caption", ""), "")
                st["done"] += 1
                _add("files_done", 1)
                debug(f"图片已保存 {rel}", CAT_MEDIA)
            else:
                st["failed"] += 1
                _add("files_failed", 1)
    else:
        warn(f"{screen_name}（{uid}）未获取到相册图片", CAT_MEDIA)

    time.sleep(1)   # 对象之间留出间隔，避免触发风控
    return st


# ---------------- 主流程 ----------------
def run_media_download():
    """执行一次媒体下载（同步、阻塞）。首次全量，后续增量。"""
    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "error": "已有下载任务正在执行中"}
    try:
        from account_manager import get_account_manager
        acc_mgr = get_account_manager()
        accounts = acc_mgr.list_accounts()
        if not accounts:
            _bump(active=False, phase="error", message="没有可用账户，请先登录")
            warn("媒体下载取消：没有可用账户", CAT_MEDIA)
            return {"ok": False, "error": "没有可用的微博账户，请先登录"}

        cookie = acc_mgr.get_cookie(accounts[0]["id"])
        items = get_monitor_manager().list()
        if not items:
            _bump(active=False, phase="error", message="监控列表为空")
            warn("媒体下载取消：监控列表为空", CAT_MEDIA)
            return {"ok": False, "error": "监控列表为空"}

        cfg = get_config().get("media_download", {}) or {}
        _reset(len(items))
        info(f"相册监控下载开始 · 监控对象 {len(items)} 个"
             f" · 每对象最多 {cfg.get('max_pages', 5)} 页", CAT_MEDIA)

        total = {"posts": 0, "done": 0, "skipped": 0, "failed": 0}
        for idx, it in enumerate(items, 1):
            uid = it["uid"]
            name = it.get("screen_name") or uid
            _bump(current=name, current_uid=uid, done=idx - 1, phase="download")
            try:
                st = _download_for_monitor(uid, name, cookie, cfg)
            except Exception as e:
                exception(f"下载对象 {name}（{uid}）时异常：{e}", CAT_MEDIA)
                continue
            for k in total:
                total[k] += st.get(k, 0)
            info(f"[{idx}/{len(items)}] {name}（{uid}）完成 · 扫描 {st['posts']} 条"
                 f" · 新下载 {st['done']} · 增量跳过 {st['skipped']} · 失败 {st['failed']}",
                 CAT_MEDIA)

        _bump(active=False, phase="done", done=len(items),
              finished_at=time.time(),
              message=f"完成：新下载 {total['done']} 个文件，跳过 {total['skipped']} 个（已存在），失败 {total['failed']} 个")
        info(f"媒体下载完成 · 扫描微博 {total['posts']} 条 · 新下载 {total['done']}"
             f" · 增量跳过 {total['skipped']} · 失败 {total['failed']}", CAT_MEDIA)
        return {"ok": True, **total}
    finally:
        _run_lock.release()


def start_media_download_async():
    """后台线程启动媒体下载，避免阻塞界面。"""
    with _lock:
        if _progress.get("active"):
            return {"ok": False, "error": "已有下载任务正在执行中"}
    threading.Thread(target=run_media_download, daemon=True).start()
    return {"ok": True}


def maybe_run_scheduled_media_download():
    """由调度器定期调用：距上次执行超过「监控间隔」则自动触发一次。"""
    try:
        cfg = get_config()
        md = cfg.get("media_download", {}) or {}
        if not md.get("enabled"):
            return
        try:
            interval = max(1, int(md.get("interval_minutes", 30) or 30))
        except Exception:
            interval = 30
        now = time.time()
        if now - float(md.get("last_run_ts", 0) or 0) < interval * 60:
            return
        md["last_run_ts"] = now
        try:
            cfg.set("media_download", md)
        except Exception:
            pass
        info(f"到达相册监控间隔（{interval} 分钟），自动开始下载", CAT_MEDIA)
        start_media_download_async()
    except Exception as e:
        error(f"检查相册监控定时失败：{e}", CAT_MEDIA)
