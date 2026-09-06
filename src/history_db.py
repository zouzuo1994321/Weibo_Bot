# -*- coding: utf-8 -*-
"""历史记录数据库（SQLite）：被监控对象发过的微博、软件转发的记录、操作日志。"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta

from paths import DB_PATH

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
    status TEXT,            -- success / skipped / failed
    reason TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT,
    message TEXT,
    created_at TEXT
);
"""

_lock = threading.Lock()
_db = None


class HistoryDB:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init()

    def _init(self):
        with self.conn:
            self.conn.executescript(SCHEMA)
            # 兼容旧表：增加 avatar_local 字段
            try:
                self.conn.execute("ALTER TABLE monitored_posts ADD COLUMN avatar_local TEXT")
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
                f"original_content,forward_content,forward_type,status,reason,created_at "
                f"FROM forwards WHERE id IN ({placeholders})",
                tuple(ids))
            rows = cur.fetchall()
        return [{
            "id": r[0], "account_id": r[1], "account_name": r[2], "uid": r[3],
            "screen_name": r[4], "post_id": r[5], "original_content": r[6],
            "forward_content": r[7], "forward_type": r[8], "status": r[9],
            "reason": r[10], "created_at": r[11],
        } for r in rows]

    def add_forward(self, account_id, account_name, uid, screen_name, post_id,
                    original_content, forward_content, forward_type,
                    status, reason=""):
        with _lock:
            self.conn.execute(
                """INSERT INTO forwards
                   (account_id,account_name,uid,screen_name,post_id,
                    original_content,forward_content,forward_type,status,reason,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (account_id, account_name, uid, screen_name, post_id,
                 original_content, forward_content, forward_type, status,
                 reason, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            self.conn.commit()

    def get_forwards(self, limit=200, offset=0):
        with _lock:
            cur = self.conn.execute(
                "SELECT id,account_id,account_name,uid,screen_name,post_id,"
                "original_content,forward_content,forward_type,status,reason,created_at "
                "FROM forwards ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset))
            rows = cur.fetchall()
        return [{
            "id": r[0], "account_id": r[1], "account_name": r[2], "uid": r[3],
            "screen_name": r[4], "post_id": r[5], "original_content": r[6],
            "forward_content": r[7], "forward_type": r[8], "status": r[9],
            "reason": r[10], "created_at": r[11],
        } for r in rows]

    # ---------- 操作日志 ----------
    def add_log(self, level, message):
        with _lock:
            self.conn.execute(
                "INSERT INTO logs (level,message,created_at) VALUES (?,?,?)",
                (level, message, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            self.conn.commit()

    def get_logs(self, limit=500, offset=0):
        with _lock:
            cur = self.conn.execute(
                "SELECT id,level,message,created_at FROM logs ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset))
            rows = cur.fetchall()
        return [{"id": r[0], "level": r[1], "message": r[2], "created_at": r[3]}
                for r in rows]

    # ---------- 统计 ----------
    def stats(self):
        with _lock:
            c1 = self.conn.execute("SELECT COUNT(*) FROM monitored_posts").fetchone()[0]
            c2 = self.conn.execute("SELECT COUNT(*) FROM forwards").fetchone()[0]
            c3 = self.conn.execute("SELECT COUNT(*) FROM forwards WHERE status='success'").fetchone()[0]
            today = datetime.now().strftime("%Y-%m-%d")
            c4 = self.conn.execute(
                "SELECT COUNT(*) FROM monitored_posts WHERE fetched_at LIKE ?",
                (today + "%",)
            ).fetchone()[0]
            c5 = self.conn.execute(
                "SELECT COUNT(*) FROM forwards WHERE status='success' AND created_at LIKE ?",
                (today + "%",)
            ).fetchone()[0]
        return {"monitored_posts": c1, "forwards": c2, "forwards_success": c3,
                "monitored_posts_today": c4, "forwards_today": c5}

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

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def get_history_db() -> HistoryDB:
    global _db
    if _db is None:
        _db = HistoryDB()
    return _db
