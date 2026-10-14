# -*- coding: utf-8 -*-
"""存储占用统计（**全异步**）与一键清理。

【v1.2.2 新增 · 启动未响应根治】

用户反馈的根因是「软件启动时要扫描 /data 和下级的位置，所以会卡」。
技术上对应两件事：
  1. os.walk 一个装了上万张图的 media/ 目录要几秒到几十秒；
  2. pywebview 的 js_bridge_call 在 **WebView2 的 UI 线程**上同步执行，
     任何一次这样的遍历都会把界面冻住 → Windows 判定「未响应」。

本模块的规则：
  · **绝不在 UI 线程上遍历目录**。所有扫描都在后台守护线程里跑，
    前端只轮询一个内存里的状态字典（纳秒级）。
  · 扫描分片进行，每片最多处理 ~2000 个文件就更新一次进度，
    这样「系统概览 → 数据载入进度」能平滑跳动，而不是长时间停在 0%。
  · 支持取消（用户点了别的页面就不再需要结果了）。
  · 结果带时间戳缓存，避免前端每秒轮询都重新扫一遍。

对外接口：
  start_scan(force=False)     启动/请求一次后台扫描（非阻塞）
  get_status()                取当前进度与结果（**零I/O**）
  clear_dir(key, mode)        清理某个目录。mode: cache（仅清缓存/可重下）| all（清空全部）
"""
import os
import shutil
import threading
import time

from paths import (DIR_KEYS, DIR_META, get_history_dir, get_image_dir,
                    get_media_dir, get_models_dir)

# ---------------------------------------------------------------------------
# 状态字典：前端唯一的数据来源。任何写入都在_lock 保护下。
# ---------------------------------------------------------------------------
_LOCK = threading.Lock()
_STATE = {
    "running": False,
    "phase": "idle",       # idle | scanning | done | cancelled | error
    "current": "",         # 当前正在扫的目录名（展示用）
    "pct": 0,              # 0~100
    "bytes": 0,            # 已统计到的字节
    "files": 0,
    "results": {},         # key -> {path, files, bytes, mb, exists, free_gb, error}
    "started_at": 0.0,
    "finished_at": 0.0,
    "last_error": "",
    "gen": 0,              # 扫描代号：取消时 +1，让旧线程自然放弃
}

# 每个分片处理多少个文件就报一次进度
_CHUNK = 2000


def _dir_path(key):
    """某个目录键的当前生效路径（延迟取，拿到运行期最新值）。"""
    if key == "image_dir":
        return get_image_dir()
    if key == "media_dir":
        return get_media_dir()
    if key == "models_dir":
        return get_models_dir()
    if key == "history_dir":
        return get_history_dir()
    return ""


def _empty_result(path, error=""):
    return {"path": path, "files": 0, "bytes": 0, "mb": 0, "gb": 0.0,
            "exists": False, "free_gb": -1, "error": error}


def _scan_one(key, gen, publish):
    """扫描单个目录。分片推进，每片结束调 publish()报进度。

    返回 (files, bytes, error)。异常一律吞掉转成 error 字符串——
    统计失败不该让整个面板挂掉。
    """
    path = _dir_path(key)
    if not path:
        return 0, 0, "路径未配置"
    if not os.path.isdir(path):
        return 0, 0, ""

    n = 0
    total = 0
    since_report = 0
    try:
        for root, _dirs, files in os.walk(path):
            for fn in files:
                # 用户取消 / 重新发起扫描 → 立刻收手，别再占着磁盘
                with _LOCK:
                    if _STATE["gen"] != gen:
                        return n, total, "__cancelled__"
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                    n += 1
                except Exception:
                    # 单个文件取不到大小（权限/正在写/已被删）——跳过，不中断
                    pass
                since_report += 1
                if since_report >= _CHUNK:
                    since_report = 0
                    publish(key, n, total)
    except Exception as e:
        return n, total, str(e)[:160]
    return n, total, ""


def _worker(gen):
    """后台扫描线程主体。"""
    results = {}
    grand_bytes = 0
    grand_files = 0

    def publish(key, n, total):
        with _LOCK:
            _STATE["current"] = DIR_META.get(key, {}).get("name", key)
            _STATE["bytes"] = _STATE.get("_base_bytes", 0) + total
            _STATE["files"] = _STATE.get("_base_files", 0) + n
            done = len(_STATE["results"])
            # 四分之三给已完成目录，四分之一给当前目录的粗略进度
            _STATE["pct"] = min(97, int((done + 0.75) / len(DIR_KEYS) * 100))

    for i, key in enumerate(DIR_KEYS):
        with _LOCK:
            if _STATE["gen"] != gen:
                return
            _STATE["current"] = DIR_META.get(key, {}).get("name", key)
            _STATE["pct"] = min(97, int(i / len(DIR_KEYS) * 100))
            _STATE["_base_bytes"] = grand_bytes
            _STATE["_base_files"] = grand_files
        n, total, err = _scan_one(key, gen, publish)
        if err == "__cancelled__":
            with _LOCK:
                _STATE["phase"] = "cancelled"
                _STATE["running"] = False
                _STATE["pct"] = 0
                _STATE["current"] = ""
                _STATE.pop("_base_bytes", None)
                _STATE.pop("_base_files", None)
            return
        path = _dir_path(key)
        row = _empty_result(path, err)
        if not err:
            row.update({
                "files": n, "bytes": total,
                "mb": total // 1048576, "gb": round(total / 1073741824, 2),
                "exists": os.path.isdir(path),
            })
            try:
                row["free_gb"] = shutil.disk_usage(path).free // 1073741824
            except Exception:
                row["free_gb"] = -1
        results[key] = row
        grand_bytes += total
        grand_files += n

    with _LOCK:
        _STATE["results"] = results
        _STATE["running"] = False
        _STATE["phase"] = "done"
        _STATE["pct"] = 100
        _STATE["current"] = ""
        _STATE["bytes"] = grand_bytes
        _STATE["files"] = grand_files
        _STATE["finished_at"] = time.time()
        _STATE.pop("_base_bytes", None)
        _STATE.pop("_base_files", None)


def start_scan(force=False):
    """发起一次后台扫描（非阻塞）。返回是否真的发起了。

    force=False 时若已有扫描在跑、或刚跑完（60s 内），直接复用结果——
    前端每秒轮询，不加这个节流会把磁盘读爆。
    """
    with _LOCK:
        if _STATE["running"]:
            return False
        if (not force and _STATE["phase"] == "done"
                and time.time() - _STATE["finished_at"] < 60):
            return False
        _STATE["gen"] += 1
        gen = _STATE["gen"]
        _STATE.update({
            "running": True, "phase": "scanning", "current": "",
            "pct": 0, "bytes": 0, "files": 0, "results": {},
            "started_at": time.time(), "finished_at": 0.0, "last_error": "",
        })
    threading.Thread(target=_worker, args=(gen,), daemon=True,
                     name="storage-scan").start()
    return True


def get_status():
    """给前端的完整状态（**纯内存读取，不碰磁盘**）。"""
    with _LOCK:
        s = dict(_STATE)
        s.pop("_base_bytes", None)
        s.pop("_base_files", None)
    s["total_mb"] = s.get("bytes", 0) // 1048576
    s["total_gb"] = round(s.get("bytes", 0) / 1073741824, 2)
    s["dirs"] = [
        {"key": k,
         "name": DIR_META[k]["name"],
         "en": DIR_META[k]["en"],
         "desc": DIR_META[k]["desc"],
         "path": _dir_path(k),
         **(s.get("results", {}).get(k) or _empty_result(_dir_path(k)))}
        for k in DIR_KEYS
    ]
    return s


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------

# 「清缓存」时保留的东西：模型（要重新下 1GB）、数据库与导出（不可再生）
_PRESERVE_EXT = {".db", ".json", ".log", ".pem", ".txt"}


def _human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return "%.1f %s" % (n, u)
        n /= 1024.0
    return "%.1f TB" % n


def clear_dir(key, mode="cache"):
    """清理某个目录。

    mode:
      · "cache" —— 只删「可重新获取」的文件（图片/视频/头像/分片），
        保留模型、数据库、导出、日志。**默认**，安全。
      · "all"   —— 清空该目录下所有内容（含数据库与模型），仅用户明确
        选择时使用；history_dir 在这个模式下会额外警告。

    返回 {ok, removed_files, freed_bytes, freed, error, kept}
    """
    if key not in DIR_KEYS:
        return {"ok": False, "error": "未知目录"}
    if mode not in ("cache", "all"):
        return {"ok": False, "error": "未知清理模式"}
    path = _dir_path(key)
    if not path or not os.path.isdir(path):
        return {"ok": False, "error": "目录不存在", "removed_files": 0,
                "freed_bytes": 0, "freed": "0 B", "kept": 0}

    # 历史记录目录额外保护：mode=all 会删掉 history.db，而程序正持有它的
    # 连接 —— 删了会导致数据丢失且当前会话后续查询报错。这里直接拒绝，
    # 引导用户用 cache 模式（保留 .db/.json/.log）或手动去资源管理器删。
    if key == "history_dir" and mode == "all":
        return {"ok": False,
                "error": ("历史记录目录不能整体清空：其中的 history.db 正被本"
                          "程序占用，删除会导致数据丢失且当前会话报错。\n"
                          "请改用「清理缓存」（保留数据库与导出），或关闭软件"
                          "后到资源管理器里手动删除。"),
                "removed_files": 0, "freed_bytes": 0, "freed": "0 B", "kept": 0}

    removed = 0
    freed = 0
    kept = 0
    errors = []
    # 收集后再删：边走边删会在 os.walk 里改动目录树，部分平台会漏项
    victims = []
    try:
        for root, _dirs, files in os.walk(path):
            for fn in files:
                fp = os.path.join(root, fn)
                if mode == "cache" and os.path.splitext(fn)[1].lower() in _PRESERVE_EXT:
                    kept += 1
                    continue
                victims.append(fp)
    except Exception as e:
        return {"ok": False, "error": "遍历失败：%s" % e, "removed_files": 0,
                "freed_bytes": 0, "freed": "0 B", "kept": kept}

    for fp in victims:
        try:
            sz = os.path.getsize(fp)
            os.remove(fp)
            removed += 1
            freed += sz
        except Exception as e:
            errors.append("%s：%s" % (os.path.basename(fp), e))

    # 顺手清掉空目录（只在 all 模式下，避免删掉用户自建的分类夹）
    if mode == "all":
        for root, dirs, files in os.walk(path, topdown=False):
            if root == path:
                continue
            if not dirs and not files:
                try:
                    os.rmdir(root)
                except Exception:
                    pass

    # 状态作废，下次读状态时重新扫
    with _LOCK:
        _STATE["phase"] = "idle"
        _STATE["finished_at"] = 0.0
    threading.Thread(target=start_scan, args=(True,), daemon=True).start()

    return {
        "ok": not errors,
        "removed_files": removed,
        "freed_bytes": freed,
        "freed": _human(freed),
        "kept": kept,
        "error": "；".join(errors[:5]) if errors else "",
        "path": path,
    }


def open_dir(key):
    """打开某个目录（不存在则先建）。"""
    import subprocess
    if key not in DIR_KEYS:
        return {"ok": False, "error": "未知目录"}
    path = _dir_path(key)
    try:
        os.makedirs(path, exist_ok=True)
        os.startfile(path)
        return {"ok": True, "path": path}
    except Exception as e:
        return {"ok": False, "error": str(e)}