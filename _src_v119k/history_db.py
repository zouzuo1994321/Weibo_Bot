# -*- coding: utf-8 -*-
"""历史记录数据库（SQLite）：被监控对象发过的微博、软件转发的记录、操作日志。"""
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta

from paths import DB_PATH, get_db_path


def _copy_file_stream(src, dst):
    """流式复制（1MB 块），用于把 NAS 上的老库弄到本地临时目录。"""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            chunk = fi.read(1024 * 1024)
            if not chunk:
                break
            fo.write(chunk)
        fo.flush()
        os.fsync(fo.fileno())

SCHEMA = """
CREATE TABLE IF NOT EXISTS monitored_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL,
    screen_name TEXT,
    avatar_local TEXT,      -- 头像本地相对路径
    post_id TEXT UNIQUE,
    content TEXT,
    images TEXT,            -- JSON 数组，图片相对路径
    created_at TEXT,        -- 微博发布时间
    fetched_at TEXT         -- 抓取时间
);
CREATE TABLE IF NOT EXISTS forwards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT,
    account_name TEXT,
    uid TEXT,
    screen_name TEXT,
    post_id TEXT,
    original_content TEXT,
    forward_content TEXT,
    forward_type TEXT,      -- normal / ai
    persona TEXT,           -- 转发时使用的人格（用于跨条去重）
    status TEXT,            -- success / skipped / failed
    reason TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT,
    message TEXT,
    category TEXT,          -- 模块分类（CORE/SCHED/FWD/HTTP/MEDIA...），用于筛选排查
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL,
    screen_name TEXT,
    post_id TEXT,
    media_type TEXT,        -- video / image
    url TEXT,
    file_path TEXT,         -- 相对 MEDIA_DIR 的相对路径
    file_name TEXT,
    size INTEGER DEFAULT 0,
    post_text TEXT,         -- 微博正文（便于检索预览）
    post_created_at TEXT,
    downloaded_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_media_url ON media_files(url, media_type);
-- v1.1.9j 性能索引：logs 表曾达 38.6 万行，`ORDER BY id DESC LIMIT 500` 走全表 SCAN，
-- 且按级别 / 分类筛选也无索引。补索引后可走索引扫描，查询从数百 ms 降到 ms 级。
CREATE INDEX IF NOT EXISTS idx_logs_id_desc ON logs(id DESC);
CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level);
CREATE INDEX IF NOT EXISTS idx_logs_category ON logs(category);
CREATE INDEX IF NOT EXISTS idx_monitored_fetched ON monitored_posts(fetched_at);
CREATE INDEX IF NOT EXISTS idx_forwards_status_created ON forwards(status, created_at);
"""

# v1.1.9j：logs 表保留的最大行数（异步裁剪，避免无限膨胀拖慢查询与统计）
LOGS_KEEP_ROWS = 120000
# v1.1.9j：logs 总行数缓存（COUNT(*) 在 38 万行上要 45ms，且前端每次翻页都调用）
_COUNT_CACHE = {"ts": 0.0, "n": 0}
_COUNT_CACHE_TTL = 10.0
_count_lock = threading.Lock()

_lock = threading.Lock()
_stats_lock = threading.Lock()   # v1.1.9g：stats() TTL 缓存专用锁（避免与 DB 写锁互等）
_db = None


def _pct(s):
    """对路径做最小必要的百分号编码（保留斜杠与盘符冒号）。"""
    import urllib.parse
    return urllib.parse.quote(s, safe="/:()[]{}!$&'*+,;=@~")


def sqlite_ro_target(path):
    """构造「只读打开」SQLite 的 URI，仅适用于**本地盘**路径。

    为什么不处理 UNC：SQLite 的 URI 解析只接受空 authority 或 `localhost`
    （实测 `file://Nas-1/...` 报 `invalid uri authority: Nas-1`）。
    因此 UNC / 网络盘路径请直接用 `sqlite_connect_ro()`，它会绕开 URI。
    """
    p = str(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file://" + _pct(p) + "?mode=ro"


def sqlite_connect_ro(path, timeout=30):
    """（尽量）以只读方式打开 SQLite 库，兼容 UNC / 网络映射盘。

    策略：
      1. 本地盘 → `file:///C:/...?mode=ro`（严格只读，不产生 -wal）；
      2. UNC / 其他 → 普通 connect（避开 URI authority 限制）。
         老库位于 NAS 且通常只读 ACL，普通打开也只会读；不会写坏它。
    """
    p = str(path)
    looks_unc = p.replace("\\", "/").startswith("//")
    if not looks_unc:
        try:
            return sqlite3.connect(sqlite_ro_target(p), uri=True, timeout=timeout)
        except Exception:
            pass
    return sqlite3.connect(p, timeout=timeout)


class HistoryDB:
    def __init__(self, path: str = ""):
        # 【v1.2.2】历史记录目录可自定义，DB 路径必须运行期解析。
        # 用None 默认值而不是 DB_PATH 常量，是因为默认参数在 import 时求值，
        # 那时用户可能还没设过路径；写成 path=None 时每次调用都重新解析。
        self.path = path or get_db_path()
        self._stats_cache = None   # (datetime, dict) — stats() TTL 缓存
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=15)
        # v1.1.9j：调优 PRAGMA——
        #   WAL 保持不变（本地盘上很快）；
        #   synchronous=NORMAL 在 WAL 下已足够安全，写入更快；
        #   busy_timeout 避免偶发锁竞争直接抛错；
        #   temp_store 放内存，避免排序 / 临时表落盘。
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=15000")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.execute("PRAGMA cache_size=-16000")
        self._init()

    def _init(self):
        with self.conn:
            self.conn.executescript(SCHEMA)
            # 兼容旧表：增加 avatar_local 字段
            try:
                self.conn.execute("ALTER TABLE monitored_posts ADD COLUMN avatar_local TEXT")
            except Exception:
                pass
            # 兼容旧表：增加 persona 字段（用于 AI 跨条去重）
            try:
                self.conn.execute("ALTER TABLE forwards ADD COLUMN persona TEXT")
            except Exception:
                pass
            # 兼容旧表：logs 增加 category 字段（日志模块分类）
            try:
                self.conn.execute("ALTER TABLE logs ADD COLUMN category TEXT")
            except Exception:
                pass
        # v1.1.9j：异步裁剪超大 logs 表（老库曾达 38.6 万行，拖慢查询与统计）。
        # 放到后台线程做，避免阻塞启动；只保留最近 LOGS_KEEP_ROWS 行。
        try:
            threading.Thread(target=self._prune_logs_if_needed, daemon=True).start()
        except Exception:
            pass

    def _prune_logs_if_needed(self):
        """logs 表行数超过上限时，删掉最旧的部分（后台执行）。"""
        try:
            with _lock:
                n = self.conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
                if n <= LOGS_KEEP_ROWS:
                    return
                cut = n - LOGS_KEEP_ROWS
                # 删除最旧的 cut 行（按 id 升序）
                self.conn.execute(
                    "DELETE FROM logs WHERE id IN "
                    "(SELECT id FROM logs ORDER BY id ASC LIMIT ?)", (cut,))
                self.conn.commit()
            with _count_lock:
                _COUNT_CACHE["ts"] = 0.0   # 失效计数缓存
        except Exception:
            pass

    # ---------- 被监控对象发过的微博 ----------
    def add_monitored_post(self, uid, screen_name, post_id, content,
                           images, created_at, avatar_local=""):
        with _lock:
            try:
                self.conn.execute(
                    """INSERT OR IGNORE INTO monitored_posts
                       (uid, screen_name, avatar_local, post_id, content, images, created_at, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (uid, screen_name, avatar_local or "", post_id, content,
                     json.dumps(images or [], ensure_ascii=False),
                     created_at, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
                self.conn.commit()
                return True
            except Exception:
                return False

    def update_post_images(self, post_id, images):
        """更新某条微博的图片列表（用于修复历史记录中抓成表情包的图片）。"""
        with _lock:
            try:
                cur = self.conn.execute(
                    "UPDATE monitored_posts SET images=? WHERE post_id=?",
                    (json.dumps(images or [], ensure_ascii=False), str(post_id)),
                )
                self.conn.commit()
                return cur.rowcount > 0
            except Exception:
                return False

    def get_monitored_posts(self, uid=None, limit=200, offset=0):
        with _lock:
            if uid:
                cur = self.conn.execute(
                    "SELECT id,uid,screen_name,avatar_local,post_id,content,images,created_at,fetched_at "
                    "FROM monitored_posts WHERE uid=? ORDER BY id DESC LIMIT ? OFFSET ?",
                    (uid, limit, offset))
            else:
                cur = self.conn.execute(
                    "SELECT id,uid,screen_name,avatar_local,post_id,content,images,created_at,fetched_at "
                    "FROM monitored_posts ORDER BY id DESC LIMIT ? OFFSET ?",
                    (limit, offset))
            rows = cur.fetchall()
        return [self._row_monitored(r) for r in rows]

    @staticmethod
    def _row_monitored(r):
        imgs = json.loads(r[6] or "[]")
        normalized = []
        for p in imgs:
            p = p.replace("/", os.sep)
            # 【v1.2.3】绝对路径原样保留：v1.2.2 起 images 可自定义到别的盘符，
            # 跨盘时写入端（paths.rel_to_data）只能存绝对路径。这里若再按老
            # 规则压成 images\basename，读端就会回到 DATA_DIR 下找不到文件。
            # 读端 os.path.join(DATA_DIR, 绝对路径) 在 Windows 上等于该绝对路径。
            if os.path.isabs(p):
                normalized.append(p)
                continue
            # 规范为相对 DATA_DIR 的 images/...（兼容旧版 data\images\...）
            if p.lower().startswith("data" + os.sep):
                p = p[len("data" + os.sep):]
            if not p.lower().startswith("images" + os.sep):
                p = os.path.join("images", os.path.basename(p))
            normalized.append(p)
        return {
            "id": r[0], "uid": r[1], "screen_name": r[2], "avatar_local": r[3] or "",
            "post_id": r[4], "content": r[5], "images": normalized,
            "created_at": r[7], "fetched_at": r[8],
        }

    # ---------- 软件转发的记录 ----------
    def get_monitored_posts_by_ids(self, ids):
        with _lock:
            rows = []
            for pid in ids:
                cur = self.conn.execute(
                    "SELECT id,uid,screen_name,avatar_local,post_id,content,images,created_at,fetched_at "
                    "FROM monitored_posts WHERE id=?", (pid,))
                r = cur.fetchone()
                if r:
                    rows.append(self._row_monitored(r))
            return rows

    def get_forwards_by_ids(self, ids):
        with _lock:
            placeholders = ",".join("?" * len(ids)) if ids else "?"
            cur = self.conn.execute(
                f"SELECT id,account_id,account_name,uid,screen_name,post_id,"
                f"original_content,forward_content,forward_type,persona,status,reason,created_at "
                f"FROM forwards WHERE id IN ({placeholders})",
                tuple(ids))
            rows = cur.fetchall()
        return [{
            "id": r[0], "account_id": r[1], "account_name": r[2], "uid": r[3],
            "screen_name": r[4], "post_id": r[5], "original_content": r[6],
            "forward_content": r[7], "forward_type": r[8], "persona": r[9],
            "status": r[10], "reason": r[11], "created_at": r[12],
        } for r in rows]

    def add_forward(self, account_id, account_name, uid, screen_name, post_id,
                    original_content, forward_content, forward_type,
                    status, reason="", persona=""):
        with _lock:
            self.conn.execute(
                """INSERT INTO forwards
                   (account_id,account_name,uid,screen_name,post_id,
                    original_content,forward_content,forward_type,persona,
                    status,reason,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (account_id, account_name, uid, screen_name, post_id,
                 original_content, forward_content, forward_type, persona or "",
                 status, reason, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            self.conn.commit()

    def get_forwards(self, limit=200, offset=0):
        with _lock:
            cur = self.conn.execute(
                "SELECT id,account_id,account_name,uid,screen_name,post_id,"
                "original_content,forward_content,forward_type,persona,status,reason,created_at "
                "FROM forwards ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset))
            rows = cur.fetchall()
        return [{
            "id": r[0], "account_id": r[1], "account_name": r[2], "uid": r[3],
            "screen_name": r[4], "post_id": r[5], "original_content": r[6],
            "forward_content": r[7], "forward_type": r[8], "persona": r[9],
            "status": r[10], "reason": r[11], "created_at": r[12],
        } for r in rows]

    def is_post_forwarded(self, post_id):
        """该微博（post_id）是否已成功转发过，用于避免重复转发。"""
        if not post_id:
            return False
        with _lock:
            cur = self.conn.execute(
                "SELECT 1 FROM forwards WHERE post_id=? AND status='success' LIMIT 1",
                (post_id,))
            return cur.fetchone() is not None

    def is_post_processed(self, post_id):
        """该微博是否已有任意转发处理记录（成功/跳过/失败）。

        用于「抓取候选」去重：已处理过（含命中黑名单跳过、转发失败）的微博不再
        进入候选池，避免每轮轮询反复拉取同一条已决策过的微博；同时避免因
        last_post_id 游标跳变而把尚未转发的新微博永久吞掉（漏抓）。
        """
        if not post_id:
            return False
        with _lock:
            cur = self.conn.execute(
                "SELECT 1 FROM forwards WHERE post_id=? LIMIT 1",
                (post_id,))
            return cur.fetchone() is not None

    # ---------- AI 三层去重辅助查询 ----------
    def get_recent_ai_texts(self, persona, limit=8):
        """该人格最近 N 条 AI 转发文案（成功转发），用于「跨条去重」上下文注入。

        返回纯文本列表（最新在前），供 ai_generator 在生成前拼进 prompt，
        提示模型「以下内容你已经说过，不要再用相同句式或开头」。
        """
        with _lock:
            cur = self.conn.execute(
                "SELECT forward_content FROM forwards "
                "WHERE forward_type='ai' AND persona=? AND status='success' "
                "ORDER BY id DESC LIMIT ?",
                (persona or "", limit))
            rows = [r[0] for r in cur.fetchall() if r[0]]
        return rows

    def get_recent_same_topic(self, topic, limit=8):
        """平台已发的同话题文案（按关键词模糊匹配原文或转发文案），用于「跨条去重」。

        topic 通常为抽取出的主话题（k1）；过短（<2 字）无意义则直接返回空。
        返回纯文本列表（最新在前）。
        """
        if not topic or len(topic) < 2:
            return []
        like = "%" + topic + "%"
        with _lock:
            cur = self.conn.execute(
                "SELECT forward_content FROM forwards "
                "WHERE status='success' AND "
                "(original_content LIKE ? OR forward_content LIKE ?) "
                "ORDER BY id DESC LIMIT ?",
                (like, like, limit))
            rows = [r[0] for r in cur.fetchall() if r[0]]
        return rows

    # ---------- 操作日志 ----------
    def add_log(self, level, message, category="CORE"):
        """写入一条日志。category 为模块分类，前端可按其筛选。"""
        with _lock:
            try:
                self.conn.execute(
                    "INSERT INTO logs (level,message,category,created_at) VALUES (?,?,?,?)",
                    (level, message, category or "CORE",
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            except Exception:
                # 兼容尚未迁移出 category 列的旧库
                self.conn.execute(
                    "INSERT INTO logs (level,message,created_at) VALUES (?,?,?)",
                    (level, message, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            self.conn.commit()

    def get_logs(self, limit=500, offset=0, level=None, category=None, keyword=None):
        """查询日志，支持按级别 / 分类筛选与关键字搜索。"""
        sql = "SELECT id,level,message,created_at FROM logs"
        where, args = [], []
        if level and level != "ALL":
            where.append("level = ?")
            args.append(level)
        if category and category != "ALL":
            # 兼容旧库无 category 列的情况
            try:
                self.conn.execute("SELECT category FROM logs LIMIT 1")
                where.append("category = ?")
                args.append(category)
            except Exception:
                pass
        if keyword:
            where.append("message LIKE ?")
            args.append(f"%{keyword}%")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        args.extend([limit, offset])
        with _lock:
            try:
                cur = self.conn.execute(sql, args)
                rows = cur.fetchall()
            except Exception:
                cur = self.conn.execute(
                    "SELECT id,level,message,created_at FROM logs ORDER BY id DESC LIMIT ? OFFSET ?",
                    (limit, offset))
                rows = cur.fetchall()
        return [{"id": r[0], "level": r[1], "message": r[2], "created_at": r[3]}
                for r in rows]

    def count_logs(self, level=None, category=None, keyword=None):
        """统计符合条件的日志条数（供分页使用）。

        v1.1.9j：加 TTL 缓存。老库 logs 曾达 38.6 万行，`COUNT(*)` 在磁盘上要 45ms，
        而前端「运行日志」页每次翻页 / 改筛选都会调用它；命中缓存后几乎零开销。
        仅当**无筛选条件**（全量计数）时使用缓存，带条件时仍实时查询以保证准确。
        """
        no_filter = (not level or level == "ALL") and \
                    (not category or category == "ALL") and not keyword
        if no_filter:
            now = time.time()
            with _count_lock:
                if now - _COUNT_CACHE["ts"] < _COUNT_CACHE_TTL:
                    return _COUNT_CACHE["n"]
        sql = "SELECT COUNT(*) FROM logs"
        where, args = [], []
        if level and level != "ALL":
            where.append("level = ?")
            args.append(level)
        if category and category != "ALL":
            try:
                self.conn.execute("SELECT category FROM logs LIMIT 1")
                where.append("category = ?")
                args.append(category)
            except Exception:
                pass
        if keyword:
            where.append("message LIKE ?")
            args.append(f"%{keyword}%")
        if where:
            sql += " WHERE " + " AND ".join(where)
        with _lock:
            try:
                n = self.conn.execute(sql, args).fetchone()[0]
            except Exception:
                return 0
        if no_filter:
            with _count_lock:
                _COUNT_CACHE["ts"] = time.time()
                _COUNT_CACHE["n"] = n
        return n

    def clear_logs(self):
        """清空全部日志。"""
        with _lock:
            self.conn.execute("DELETE FROM logs")
            self.conn.commit()
            try:
                self.conn.execute("DELETE FROM sqlite_sequence WHERE name='logs'")
                self.conn.commit()
            except Exception:
                pass
        with _count_lock:
            _COUNT_CACHE["ts"] = time.time()
            _COUNT_CACHE["n"] = 0

    # ---------- 视频 / 相册（媒体文件） ----------
    _MEDIA_COLS = ["id", "uid", "screen_name", "post_id", "media_type", "url",
                   "file_path", "file_name", "size", "post_text",
                   "post_created_at", "downloaded_at"]

    def add_media_file(self, uid, screen_name, post_id, media_type, url,
                       file_path, file_name, size=0, post_text="", post_created_at=""):
        """登记一个已下载的媒体文件。

        依赖 (url, media_type) 唯一索引实现增量：
        已存在 → 返回 False（增量跳过）；新下载 → 返回 True。
        """
        with _lock:
            try:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO media_files "
                    "(uid,screen_name,post_id,media_type,url,file_path,file_name,"
                    " size,post_text,post_created_at,downloaded_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (uid, screen_name, post_id, media_type, url, file_path, file_name,
                     size or 0, post_text or "", post_created_at or "",
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                self.conn.commit()
                return cur.rowcount > 0
            except Exception:
                return False

    def has_media_url(self, url, media_type):
        """该媒体 URL 是否已下载过（增量判定用）。"""
        with _lock:
            try:
                cur = self.conn.execute(
                    "SELECT 1 FROM media_files WHERE url=? AND media_type=? LIMIT 1",
                    (url, media_type))
                return cur.fetchone() is not None
            except Exception:
                return False

    def get_media_by_uid(self, uid, media_type="all", limit=1000):
        """取某个监控对象的全部媒体（可按 video/image 过滤）。"""
        with _lock:
            try:
                sql = ("SELECT id,uid,screen_name,post_id,media_type,url,file_path,"
                       "file_name,size,post_text,post_created_at,downloaded_at "
                       "FROM media_files WHERE uid=?")
                args = [uid]
                if media_type and media_type != "all":
                    sql += " AND media_type=?"
                    args.append(media_type)
                sql += " ORDER BY id DESC LIMIT ?"
                args.append(limit)
                cur = self.conn.execute(sql, args)
                return [dict(zip(self._MEDIA_COLS, r)) for r in cur.fetchall()]
            except Exception:
                return []

    def search_media(self, keyword, limit=500):
        """按 UID / 昵称 / 微博正文搜索媒体文件。"""
        if not keyword:
            return []
        with _lock:
            try:
                cur = self.conn.execute(
                    "SELECT id,uid,screen_name,post_id,media_type,url,file_path,"
                    "file_name,size,post_text,post_created_at,downloaded_at "
                    "FROM media_files WHERE uid LIKE ? OR screen_name LIKE ? "
                    "OR post_text LIKE ? ORDER BY uid, id DESC LIMIT ?",
                    (f"%{keyword}%", f"%{keyword}%", f"%{keyword}%", limit))
                return [dict(zip(self._MEDIA_COLS, r)) for r in cur.fetchall()]
            except Exception:
                return []

    def list_media_objects(self):
        """列出媒体库中已有的监控对象（UID + 昵称 + 视频/图片数量）。"""
        with _lock:
            try:
                cur = self.conn.execute(
                    "SELECT uid, screen_name, "
                    "SUM(CASE WHEN media_type='video' THEN 1 ELSE 0 END), "
                    "SUM(CASE WHEN media_type='image' THEN 1 ELSE 0 END) "
                    "FROM media_files GROUP BY uid ORDER BY uid")
                return [{"uid": r[0], "screen_name": r[1] or "",
                         "videos": r[2] or 0, "images": r[3] or 0}
                        for r in cur.fetchall()]
            except Exception:
                return []

    def clear_media_files(self):
        """清空媒体下载记录（不删除本地文件），重置增量判定。

        清理后所有 URL 视为未下载：再次执行下载会重新拉取全部资源，
        并对同名文件直接覆盖。
        """
        with _lock:
            self.conn.execute("DELETE FROM media_files")
            self.conn.commit()
            try:
                self.conn.execute("DELETE FROM sqlite_sequence WHERE name='media_files'")
                self.conn.commit()
            except Exception:
                pass

    def get_media_summary(self):
        """媒体库统计：对象数 / 视频数 / 图片数 / 占用空间。"""
        with _lock:
            try:
                total = self.conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]
                v = self.conn.execute(
                    "SELECT COUNT(*) FROM media_files WHERE media_type='video'").fetchone()[0]
                i = self.conn.execute(
                    "SELECT COUNT(*) FROM media_files WHERE media_type='image'").fetchone()[0]
                sz = self.conn.execute(
                    "SELECT COALESCE(SUM(size),0) FROM media_files").fetchone()[0]
                objs = self.conn.execute(
                    "SELECT COUNT(DISTINCT uid) FROM media_files").fetchone()[0]
                return {"total": total, "videos": v, "images": i,
                        "size": sz or 0, "objects": objs}
            except Exception:
                return {"total": 0, "videos": 0, "images": 0, "size": 0, "objects": 0}

    # ---------- 统计 ----------
    def stats(self, ttl: float = 8.0):
        """统计概览。

        【性能】v1.1.9g：加 TTL 缓存。本方法含 5 个 COUNT(*)，当 history.db 位于
        NAS/网络盘（SMB）时，每次执行都会产生多次网络往返 + 文件锁，而前端「系统
        概览」每秒轮询一次 get_state → 每次都触发这里，足以把 UI 线程拖到「未响应」。
        缓存后同一窗口内几乎瞬时返回，把 DB 压力降到 ~1/8。
        """
        now = datetime.now()
        with _stats_lock:
            cached = getattr(self, "_stats_cache", None)
            if cached is not None and (now - cached[0]).total_seconds() < ttl:
                return dict(cached[1])
        with _lock:
            c1 = self.conn.execute("SELECT COUNT(*) FROM monitored_posts").fetchone()[0]
            c2 = self.conn.execute("SELECT COUNT(*) FROM forwards").fetchone()[0]
            c3 = self.conn.execute("SELECT COUNT(*) FROM forwards WHERE status='success'").fetchone()[0]
            today = now.strftime("%Y-%m-%d")
            c4 = self.conn.execute(
                "SELECT COUNT(*) FROM monitored_posts WHERE fetched_at LIKE ?",
                (today + "%",)
            ).fetchone()[0]
            c5 = self.conn.execute(
                "SELECT COUNT(*) FROM forwards WHERE status='success' AND created_at LIKE ?",
                (today + "%",)
            ).fetchone()[0]
        result = {"monitored_posts": c1, "forwards": c2, "forwards_success": c3,
                  "monitored_posts_today": c4, "forwards_today": c5}
        with _stats_lock:
            self._stats_cache = (now, dict(result))
        return result

    def data_overview(self):
        """为「数据总览」页聚合多维度统计数据与曲线序列。"""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        month_start = now.strftime("%Y-%m-01")
        year_start = now.strftime("%Y-01-01")

        def _count_monitored(where, params=()):
            sql = "SELECT COUNT(*) FROM monitored_posts"
            if where:
                sql += " WHERE " + where
            return self.conn.execute(sql, params).fetchone()[0]

        def _count_forwarded(where, params=()):
            sql = "SELECT COUNT(*) FROM forwards WHERE status='success'"
            if where:
                sql += " AND " + where
            return self.conn.execute(sql, params).fetchone()[0]

        def _series(sql, params=()):
            rows = self.conn.execute(sql, params).fetchall()
            return {r[0]: r[1] for r in rows if r[0]}

        with _lock:
            summary = {
                "today": {"monitored": _count_monitored("fetched_at LIKE ?", (today + "%",)),
                          "forwarded": _count_forwarded("created_at LIKE ?", (today + "%",))},
                "month": {"monitored": _count_monitored("fetched_at >= ?", (month_start,)),
                          "forwarded": _count_forwarded("created_at >= ?", (month_start,))},
                "year": {"monitored": _count_monitored("fetched_at >= ?", (year_start,)),
                         "forwarded": _count_forwarded("created_at >= ?", (year_start,))},
                "all": {"monitored": _count_monitored(""),
                        "forwarded": _count_forwarded("")},
            }

            # 高频被转发对象（只统计成功转发）
            top = self.conn.execute(
                """SELECT uid, screen_name, COUNT(*) as cnt
                     FROM forwards
                    WHERE status='success'
                    GROUP BY uid
                    ORDER BY cnt DESC
                    LIMIT 10"""
            ).fetchall()
            top_forwarded = [{"uid": r[0], "screen_name": r[1] or r[0], "count": r[2]} for r in top]

            # 今日小时级
            mon_h = _series(
                "SELECT SUBSTR(fetched_at,12,2) as h, COUNT(*) FROM monitored_posts WHERE fetched_at LIKE ? GROUP BY h",
                (today + "%",))
            fwd_h = _series(
                "SELECT SUBSTR(created_at,12,2) as h, COUNT(*) FROM forwards WHERE status='success' AND created_at LIKE ? GROUP BY h",
                (today + "%",))
            hours = [f"{i:02d}" for i in range(24)]
            today_hourly = [{"label": h, "monitored": mon_h.get(h, 0), "forwarded": fwd_h.get(h, 0)} for h in hours]

            # 本月天级
            mon_d = _series(
                "SELECT SUBSTR(fetched_at,1,10) as d, COUNT(*) FROM monitored_posts WHERE fetched_at >= ? GROUP BY d",
                (month_start,))
            fwd_d = _series(
                "SELECT SUBSTR(created_at,1,10) as d, COUNT(*) FROM forwards WHERE status='success' AND created_at >= ? GROUP BY d",
                (month_start,))
            days = []
            d = datetime.strptime(month_start, "%Y-%m-%d")
            while d <= now:
                days.append(d.strftime("%Y-%m-%d"))
                d += timedelta(days=1)
            month_daily = [{"label": d[-5:], "monitored": mon_d.get(d, 0), "forwarded": fwd_d.get(d, 0)} for d in days]

            # 本年月级
            mon_m = _series(
                "SELECT SUBSTR(fetched_at,1,7) as m, COUNT(*) FROM monitored_posts WHERE fetched_at >= ? GROUP BY m",
                (year_start,))
            fwd_m = _series(
                "SELECT SUBSTR(created_at,1,7) as m, COUNT(*) FROM forwards WHERE status='success' AND created_at >= ? GROUP BY m",
                (year_start,))
            months = []
            y, mo = int(year_start[:4]), 1
            while (y, mo) <= (now.year, now.month):
                months.append(f"{y}-{mo:02d}")
                mo += 1
                if mo > 12:
                    mo = 1
                    y += 1
            year_monthly = [{"label": m[-2:] + "月", "monitored": mon_m.get(m, 0), "forwarded": fwd_m.get(m, 0)} for m in months]

            # 全部年月级
            mon_all = _series(
                "SELECT SUBSTR(fetched_at,1,7) as m, COUNT(*) FROM monitored_posts GROUP BY m ORDER BY m")
            fwd_all = _series(
                "SELECT SUBSTR(created_at,1,7) as m, COUNT(*) FROM forwards WHERE status='success' GROUP BY m ORDER BY m")
            all_keys = sorted(set(mon_all.keys()) | set(fwd_all.keys()))
            all_monthly = [{"label": k, "monitored": mon_all.get(k, 0), "forwarded": fwd_all.get(k, 0)} for k in all_keys]

        return {
            "summary": summary,
            "top_forwarded": top_forwarded,
            "series": {
                "today_hourly": today_hourly,
                "month_daily": month_daily,
                "year_monthly": year_monthly,
                "all_monthly": all_monthly,
            }
        }

    def data_overview_date(self, date: str):
        """指定某天（YYYY-MM-DD）的抓取 / 转发统计与 24 小时级曲线。

        供「数据总览」趋势曲线的日期选择器使用，便于回看前几天的记录。
        """
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except Exception:
            return {"date": date, "error": "日期格式应为 YYYY-MM-DD"}
        with _lock:
            mon = self.conn.execute(
                "SELECT COUNT(*) FROM monitored_posts WHERE fetched_at LIKE ?",
                (date + "%",)).fetchone()[0]
            fwd = self.conn.execute(
                "SELECT COUNT(*) FROM forwards WHERE status='success' AND created_at LIKE ?",
                (date + "%",)).fetchone()[0]
            mon_h = {r[0]: r[1] for r in self.conn.execute(
                "SELECT SUBSTR(fetched_at,12,2), COUNT(*) FROM monitored_posts "
                "WHERE fetched_at LIKE ? GROUP BY 1", (date + "%",)).fetchall() if r[0]}
            fwd_h = {r[0]: r[1] for r in self.conn.execute(
                "SELECT SUBSTR(created_at,12,2), COUNT(*) FROM forwards "
                "WHERE status='success' AND created_at LIKE ? GROUP BY 1",
                (date + "%",)).fetchall() if r[0]}
            top = self.conn.execute(
                """SELECT uid, screen_name, COUNT(*) as cnt FROM forwards
                   WHERE status='success' AND created_at LIKE ?
                   GROUP BY uid ORDER BY cnt DESC LIMIT 10""",
                (date + "%",)).fetchall()
        hours = [f"{i:02d}" for i in range(24)]
        hourly = [{"label": h, "monitored": mon_h.get(h, 0), "forwarded": fwd_h.get(h, 0)} for h in hours]
        top_forwarded = [{"uid": r[0], "screen_name": r[1] or r[0], "count": r[2]} for r in top]
        return {
            "date": date,
            "summary": {"monitored": mon, "forwarded": fwd},
            "series": {"hourly": hourly},
            "top_forwarded": top_forwarded,
        }

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def import_from_file(self, src_path):
        """把另一个 SQLite 库整体导入到当前库（在线备份，用于老数据迁移）。

        为什么必须这样做（v1.1.9k 的关键修复）：
          exe 启动时 Api() 会先 connect 本地 history.db 并长期持有连接（WAL）。
          此时那个文件被**独占**，`os.remove` / `os.replace` 一律失败
          （WinError 32/5），所以「复制文件覆盖」这条路在运行时根本走不通。

          改用 SQLite 官方在线备份 API：**以当前已打开的连接作为目标**，
          从老库（只读打开）整库复制进来。全程不碰本地文件名，
          也不受占用影响；老库的 WAL 会被自动合并成一致快照。

        返回导入后的表行数摘要 dict；失败抛异常。
        """
        # 老库可能位于 NAS（DENY ACL 只读，甚至无法生成 -wal/-shm）。
        # 直接 sqlite3.connect 打开会因「写不进去」报
        # `attempt to write a readonly database`（v1.1.9k 实测踩到）。
        # 所以先把主库 + 可能存在的 -wal/-shm **流式复制**到本地临时目录，
        # 再从本地临时副本做在线备份 —— 全程不写 NAS。
        tmpdir = None
        try:
            tmpdir = tempfile.mkdtemp(prefix="wbbot_legacy_")
            local_copy = os.path.join(tmpdir, "legacy.db")
            _copy_file_stream(src_path, local_copy)
            for suffix in ("-wal", "-shm"):
                side = str(src_path) + suffix
                if os.path.exists(side):
                    try:
                        _copy_file_stream(side, local_copy + suffix)
                    except Exception:
                        pass
            src_con = sqlite3.connect(local_copy, timeout=30)
        except Exception:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
            raise
        try:
            with _lock:
                src_con.backup(self.conn)
                self.conn.commit()
        finally:
            try:
                src_con.close()
            except Exception:
                pass
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
        summary = {}
        try:
            for t in ("monitored_posts", "forwards", "logs", "media_files"):
                try:
                    summary[t] = self.conn.execute(
                        "SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                except Exception:
                    summary[t] = -1
        except Exception:
            pass
        return summary


def get_history_db() -> HistoryDB:
    global _db
    if _db is None:
        _db = HistoryDB()
    return _db


def reset_history_db():
    """关闭并丢弃当前单例，让下次 get_history_db() 按**新路径**重开。

    【v1.2.2】用户改了「历史记录」目录后必须调这个，否则单例还攥着
    老库文件的连接，界面会继续读写旧位置。
    """
    global _db
    if _db is not None:
        try:
            _db.close()
        except Exception:
            pass
        _db = None
    return True
