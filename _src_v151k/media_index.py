# -*- coding: utf-8 -*-
"""媒体库索引文件（v1.4.16 引入，v1.4.17 降级为「备份 / 导入」用途）。

需求演变
--------
- **v1.4.16**：曾让「相册监控 → 媒体检索」优先读索引文件、避免每次查整表。
- **v1.4.17**：用户实测体验不如旧版 → 前台读取**全部回退为直接查库**；
  索引文件不再参与展示，只保留两个用途：

  1. **备份索引**：按当前台账生成一份 JSON 快照，另存到用户指定路径；
  2. **导入索引**：读取索引 JSON，把其中的台账行回灌回数据库
     （换机器 / 重装 / 恢复时用）。

索引内容
--------
{
  "version": 1,
  "generated_at": "2026-10-13 12:40:00",
  "media_dir": "C:\\\\...\\\\data\\\\media",
  "summary": {"total": N, "videos": N, "images": N, "size": N, "objects": N},
  "objects": [{"uid", "screen_name", "videos", "images", "size"}],
  "files":   [{"uid","screen_name","post_id","media_type","url","file_path",
               "file_name","size","post_text","post_created_at","downloaded_at"}]
}

说明
----
- `<media_dir>/media_index.json` 仍会在下载完成 / 对账完成时后台自动刷新，
  作为「媒体目录自带的台账快照」，方便整体拷走媒体库时连台账一起带走。
- 所有读写都不阻塞 UI：自动刷新走后台线程 + 合并抖动；备份/导入由用户显式触发。
"""
import json
import os
import threading
import time

from history_db import get_history_db
from logger import info, warn, debug, CAT_MEDIA
from paths import get_media_dir

INDEX_NAME = "media_index.json"
INDEX_VERSION = 1

# 后台重建的「合并抖动」：多次调用只保留最后一次
_rebuild_lock = threading.Lock()
_rebuild_pending = threading.Event()
_rebuild_thread = None
_last_rebuild = {"at": 0.0, "ok": False, "files": 0, "error": ""}


def index_path():
    """索引文件绝对路径。"""
    return os.path.join(get_media_dir(), INDEX_NAME)


# ---------------- 读取 ----------------
def load(use_cache=True):
    """读取媒体目录下的索引文件。返回 dict 或 None（不存在 / 损坏 / 版本不符）。"""
    return read_index_file(index_path())


def read_index_file(path):
    """读取任意路径的索引 JSON。返回 dict 或 None（不抛异常）。

    用于「导入索引」——用户可能从别的机器 / 备份目录选一个 json 进来。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        warn(f"媒体索引文件不可读或损坏：{path} · {e}", CAT_MEDIA)
        return None
    if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
        debug(f"媒体索引版本不符：{path}", CAT_MEDIA)
        return None
    if not isinstance(data.get("files"), list):
        warn(f"媒体索引缺少 files 字段：{path}", CAT_MEDIA)
        return None
    return data


def is_fresh():
    """媒体目录下的索引文件是否存在且可读。"""
    return load() is not None


# ---------------- 构建 / 写入 ----------------
def build_index_data():
    """采集当前台账，返回索引数据结构（同步，允许慢）。

    供「备份索引」（用户点按钮时）与「自动刷新」（后台线程）共用。
    """
    db = get_history_db()
    return {
        "version": INDEX_VERSION,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "media_dir": get_media_dir(),
        "summary": db.get_media_summary(),
        "objects": db.list_media_objects_with_size(),
        "files": db.all_media_files(),
    }


def write_now():
    """同步构建并写入媒体目录下的索引。返回 {"ok", "files", "error"}。仅后台线程用。"""
    global _last_rebuild
    try:
        data = build_index_data()
        path = index_path()
        tmp = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except Exception:
            pass
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
        n = len(data.get("files") or [])
        _last_rebuild = {"at": time.time(), "ok": True, "files": n, "error": ""}
        info(f"媒体索引已更新 · {n} 条 · {path}", CAT_MEDIA)
        return {"ok": True, "files": n, "error": ""}
    except Exception as e:
        _last_rebuild = {"at": time.time(), "ok": False, "files": 0, "error": str(e)}
        warn(f"媒体索引写入失败：{e}", CAT_MEDIA)
        return {"ok": False, "files": 0, "error": str(e)}


def _worker():
    """后台刷新线程：等待抖动窗口后执行一次写入。"""
    global _rebuild_thread
    try:
        while _rebuild_pending.is_set():
            _rebuild_pending.clear()
            time.sleep(1.5)
        write_now()
    except Exception as e:
        warn(f"媒体索引后台刷新异常：{e}", CAT_MEDIA)
    finally:
        with _rebuild_lock:
            _rebuild_thread = None


def schedule_rebuild():
    """请求（异步、合并）刷新媒体目录下的索引。**绝不阻塞调用方**。

    下载完成 / 对账完成 / 启动校验时调用；用户点「备份索引」走的是
    `build_index_data()` 另存，不经过这里。
    """
    global _rebuild_thread
    with _rebuild_lock:
        _rebuild_pending.set()
        if _rebuild_thread is None or not _rebuild_thread.is_alive():
            _rebuild_thread = threading.Thread(target=_worker, daemon=True)
            _rebuild_thread.start()
    return {"ok": True}


def last_rebuild_info():
    return dict(_last_rebuild)

