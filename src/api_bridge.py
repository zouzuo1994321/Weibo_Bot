# -*- coding: utf-8 -*-
"""
API 桥接层：暴露给前端（JS）调用的接口。
前端通过 window.pywebview.api.<method>(...).then(...) 调用。
"""
import base64
import json
import os
import random
import time
import webbrowser
from datetime import datetime

from account_manager import get_account_manager
from ai_generator import generate as ai_generate, list_personas
from config_manager import get_config
from exporter import export, export_all, export_filtered, export_rows
from forward_engine import run_once, _download_avatar
from history_db import get_history_db
from logger import info, warn, error, read_file_lines
from monitor_manager import get_monitor_manager
from paths import (APP_NAME, COPYRIGHT, COPYRIGHT_FULL, HISTORY_DIR,
                   MAJOR_VERSION, BASE_DIR, DATA_DIR)
from licensemgr import (get_status, generate_feature_code, activate_license,
                      deactivate_license, can_forward, forward_block_reason,
                      FEATURE_CODE_VALID_DAYS)
from scheduler import get_scheduler
from weibo_client import get_user_info, get_latest_posts, diagnose_account, repost


def _ver():
    try:
        from version import get_version
        return get_version()
    except Exception:
        return {"major": MAJOR_VERSION, "internal": "2609040001"}


class Api:
    def __init__(self):
        self.login_window = None
        self.main_window = None
        self._window_maximized = False
        # 自动账户健康检测节流：距上次自动检测超过该秒数才再跑
        self._last_auto_check = 0
        self._auto_check_interval = 300  # 5 分钟

    # ---------------- 窗口控制（无边框模式） ----------------
    def window_minimize(self):
        if self.main_window:
            try:
                self.main_window.minimize()
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        return {"ok": False, "error": "窗口未就绪"}

    def window_toggle_maximize(self):
        if not self.main_window:
            return {"ok": False, "error": "窗口未就绪"}
        try:
            if self._window_maximized:
                self.main_window.restore()
                self._window_maximized = False
            else:
                self.main_window.maximize()
                self._window_maximized = True
            return {"ok": True, "maximized": self._window_maximized}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def window_close(self):
        if self.main_window:
            try:
                self.main_window.destroy()
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        return {"ok": False, "error": "窗口未就绪"}

    def get_window_pos(self):
        """读取当前窗口左上角坐标与尺寸（供前端标题栏拖动计算偏移）。"""
        if not self.main_window:
            return {"ok": False, "error": "窗口未就绪"}
        try:
            return {"ok": True,
                    "x": int(self.main_window.x),
                    "y": int(self.main_window.y),
                    "width": int(self.main_window.width),
                    "height": int(self.main_window.height)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def window_move(self, x, y):
        """将窗口移动到指定屏幕坐标（标题栏拖动的核心实现，取代不可靠的 CSS app-region）。"""
        if not self.main_window:
            return {"ok": False, "error": "窗口未就绪"}
        try:
            # 若处于最大化状态，拖动时先还原，否则无法移动
            if self._window_maximized:
                self.main_window.restore()
                self._window_maximized = False
            self.main_window.move(int(round(x)), int(round(y)))
            return {"ok": True,
                    "x": int(self.main_window.x),
                    "y": int(self.main_window.y),
                    "width": int(self.main_window.width),
                    "height": int(self.main_window.height)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------------- 状态 ----------------
    def get_state(self):
        # 触发节流式自动账户健康检测（后台线程，不阻塞）
        self._auto_check_accounts()
        cfg = get_config().to_dict()
        acc = get_account_manager().list_accounts()
        mon = get_monitor_manager().list()
        sch = get_scheduler().status()
        db = get_history_db()
        return {
            "app": APP_NAME,
            "copyright": COPYRIGHT,
            "copyright_full": COPYRIGHT_FULL,
            "major_version": MAJOR_VERSION,
            "version": _ver(),
            "config": cfg,
            "accounts": acc,
            "monitors": mon,
            "scheduler": sch,
            "stats": db.stats(),
            "personas": list_personas(),
            "license": get_status(),
        }

    def get_config(self):
        return get_config().to_dict()

    def save_config(self, patch):
        try:
            patch = json.loads(patch) if isinstance(patch, str) else patch
        except Exception:
            return {"ok": False, "error": "配置格式错误"}
        get_config().update(patch)
        info(f"配置已更新：{list(patch.keys())}")
        return {"ok": True}

    # ---------------- 账户 ----------------
    def list_accounts(self):
        return get_account_manager().list_accounts()

    def _auto_check_accounts(self):
        """节流式自动检测：仅在距上次检测超过间隔时执行，避免频繁联网。
        放在后台线程，不阻塞 get_state（即不拖慢界面初始化）。"""
        import threading, time
        now = time.time()
        if now - self._last_auto_check < self._auto_check_interval:
            return
        self._last_auto_check = now  # 立即标记，避免重复触发

        def _worker():
            try:
                self._run_check_all()
            except Exception as e:
                warn(f"自动账户检测异常：{e}")

        threading.Thread(target=_worker, daemon=True).start()

    def _run_check_all(self):
        am = get_account_manager()
        for acc in am.list_accounts():
            cookie = am.get_cookie(acc["id"])
            if not cookie:
                am.set_check_result(acc["id"], "unknown", "无 Cookie")
                continue
            diag = diagnose_account(cookie)
            wb = ""
            login = diag.get("login")
            if isinstance(login, dict):
                wb = login.get("screen_name", "")
            am.set_check_result(acc["id"], diag["status"], diag["message"], wb or None)

    def check_account(self, acc_id):
        """手动检测单个账户：登录态 + 转发能力，并回填微博昵称。"""
        am = get_account_manager()
        cookie = am.get_cookie(acc_id)
        if not cookie:
            return {"ok": False, "error": "该账户没有可用 Cookie"}
        diag = diagnose_account(cookie)
        wb = ""
        login = diag.get("login")
        if isinstance(login, dict):
            wb = login.get("screen_name", "")
        am.set_check_result(acc_id, diag["status"], diag["message"], wb or None)
        info(f"账户检测 {acc_id}：{diag['status']} - {diag['message']}")
        return {"ok": True, "status": diag["status"], "message": diag["message"],
                "login": diag["login"], "repost": diag["repost"]}

    def check_all_accounts(self):
        """手动检测全部账户。"""
        self._run_check_all()
        accs = get_account_manager().list_accounts()
        summary = {a["display_name"]: a["status"] for a in accs}
        info(f"已检测全部账户：{summary}")
        return {"ok": True, "accounts": summary}

    def add_account(self, remark, cookie):
        ok, msg = get_account_manager().add_account(remark, cookie)
        if ok:
            info(f"已添加账户：{remark}")
        else:
            warn(f"添加账户失败：{msg}")
        return {"ok": ok, "message": msg}

    def remove_account(self, acc_id):
        ok = get_account_manager().remove_account(acc_id)
        return {"ok": ok}

    def open_system_browser(self):
        webbrowser.open("https://weibo.com/login.php")
        return {"ok": True, "message": "已打开系统浏览器，请登录后复制 Cookie 粘贴到输入框"}

    def open_external(self, url):
        try:
            webbrowser.open(url)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_login_browser(self):
        """内置浏览器登录：打开独立窗口加载微博登录页。"""
        import webview
        if self.login_window is not None and self.login_window.gui is not None:
            try:
                self.login_window.show()
                return {"ok": True}
            except Exception:
                pass
        try:
            self.login_window = webview.create_window(
                "微博登录 - 请在窗口内登录",
                url="https://weibo.com/login.php",
                width=1000, height=720,
            )
            return {"ok": True, "message": "已打开内置浏览器，登录后点击『获取Cookie』"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def capture_login_cookies(self):
        """从内置登录窗口读取 Cookie。

        注意：pywebview 的 get_cookies() 返回的每个元素是 http.cookies.SimpleCookie
        （字典子类，键为 cookie 名，值为 Morsel）。Morsel 通过 .key / .value 属性
        取值，不能用 .get('name')（那只会去查名为 'name' 的 cookie，恒为 None）。
        下面兼容 SimpleCookie 与旧版 dict 两种返回结构。
        """
        import webview
        if self.login_window is None:
            return {"ok": False, "error": "请先打开内置浏览器并完成登录"}
        try:
            cookies = self.login_window.get_cookies()
        except Exception as e:
            return {"ok": False, "error": f"读取 Cookie 失败：{e}"}
        if not cookies:
            return {"ok": False, "error": "未读取到 Cookie，请确认已登录"}
        parts = []
        for c in cookies:
            name = value = None
            # 情况一：pywebview（edge/cef 等）返回的 SimpleCookie
            if hasattr(c, "values"):
                for morsel in c.values():
                    name = getattr(morsel, "key", None) or (
                        morsel.get("key") if hasattr(morsel, "get") else None)
                    value = getattr(morsel, "value", None) or (
                        morsel.get("value") if hasattr(morsel, "get") else None)
                    break
            # 情况二：旧版直接返回 dict（含 name/value 字段）
            elif isinstance(c, dict):
                name = c.get("name") or c.get("Name")
                value = c.get("value") or c.get("Value")
            if name and value:
                parts.append(f"{name}={value}")
        cookie_str = "; ".join(parts)
        if not cookie_str:
            return {"ok": False, "error": "Cookie 解析为空，请确认已成功登录"}
        # 判断是否真正登录（存在 SUB 等关键 cookie）
        logged = any(k in cookie_str for k in ("SUB=", "SUBP=", "SUHB="))
        return {"ok": True, "cookie": cookie_str, "logged_in": logged}

    def close_login_browser(self):
        if self.login_window is not None:
            try:
                self.login_window.destroy()
            except Exception:
                pass
        return {"ok": True}

    # ---------------- 监控 ----------------
    def add_monitor(self, uid):
        uid = str(uid).strip()
        if not uid:
            return {"ok": False, "error": "UID 不能为空"}
        # 尝试用首个账户 cookie 获取昵称与头像
        screen_name = ""
        avatar_local = ""
        accs = get_account_manager().list_accounts()
        if accs:
            from account_manager import get_account_manager as gam
            cookie = gam().get_cookie(accs[0]["id"])
            if cookie:
                info_resp = get_user_info(uid, cookie)
                if info_resp.get("ok"):
                    screen_name = info_resp.get("screen_name", "")
                    avatar_url = info_resp.get("avatar_url", "")
                    if avatar_url:
                        avatar_local = _download_avatar(avatar_url, uid, cookie)
        ok, msg = get_monitor_manager().add(uid, screen_name)
        if ok and avatar_local:
            get_monitor_manager().update(uid, {"avatar_local": avatar_local})
        if ok:
            info(f"已添加监控 UID={uid} 昵称={screen_name}")
        else:
            warn(f"添加监控失败：{msg}")
        return {"ok": ok, "message": msg, "screen_name": screen_name, "avatar_local": avatar_local}

    def remove_monitor(self, uid):
        ok = get_monitor_manager().remove(uid)
        return {"ok": ok}

    def set_monitor_pinned(self, uid, pinned):
        """置顶 / 取消置顶某个监控对象。pinned 为 bool。"""
        ok = get_monitor_manager().set_pinned(str(uid), bool(pinned))
        if ok:
            info(f"监控对象 UID={uid} 置顶状态已设为 {pinned}")
        else:
            warn(f"设置置顶失败：未找到 UID={uid}")
        return {"ok": ok}

    def refresh_monitors(self):
        """刷新并判定所有监控对象：用监控实际调用的接口拉取最新微博，
        成功置 online 并刷新昵称/最新微博/检测时间，失败置 error。"""
        accs = get_account_manager().list_accounts()
        if not accs:
            return {"ok": False, "error": "需要至少一个账户 Cookie 才能刷新"}
        from account_manager import get_account_manager as gam
        cookie = gam().get_cookie(accs[0]["id"])
        mon = get_monitor_manager()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        details = []
        ok_count = err_count = 0
        for it in mon.list():
            uid = it["uid"]
            resp = get_latest_posts(uid, cookie, page=1, count=5)
            if resp.get("ok") and resp.get("posts"):
                posts = resp["posts"]
                # 昵称优先从用户信息取，其次用首条微博
                name = it.get("screen_name", "")
                if not name:
                    info_resp = get_user_info(uid, cookie)
                    if info_resp.get("ok") and info_resp.get("screen_name"):
                        name = info_resp["screen_name"]
                # 「最新微博」展示跳过置顶（置顶通常排在最前，非真实新动态）
                non_top = [p for p in posts if not p.get("is_top")]
                display = non_top or posts
                # 刷新头像
                avatar_local = it.get("avatar_local", "")
                info_resp = get_user_info(uid, cookie)
                if info_resp.get("ok"):
                    name = info_resp.get("screen_name", name) or name
                    avatar_url = info_resp.get("avatar_url", "")
                    if avatar_url:
                        avatar_local = _download_avatar(avatar_url, uid, cookie) or avatar_local
                mon.update(uid, {
                    "screen_name": name,
                    "status": "online",
                    "last_post_id": posts[0]["id"],
                    "last_post_text": display[0]["text"][:200],
                    "last_check": now,
                    "avatar_local": avatar_local,
                })
                ok_count += 1
                details.append({"uid": uid, "ok": True,
                                "screen_name": name,
                                "detail": f"在线，最新微博：{display[0]['text'][:30]}"})
            else:
                mon.update(uid, {"status": "error", "last_check": now})
                err_count += 1
                details.append({"uid": uid, "ok": False,
                                "detail": resp.get("error", "拉取失败")})
        info(f"监控刷新完成：{ok_count} 在线 / {err_count} 异常")
        first_err = next((d for d in details if not d["ok"]), None)
        summary = (f"在线 {ok_count} / 异常 {err_count}" +
                   (f"；首个异常 {first_err['uid']}：{first_err['detail'][:60]}"
                    if first_err else ""))
        return {"ok": True, "updated": ok_count, "error_count": err_count,
                "details": details, "summary": summary}

    # ---------------- 转发 / 调度 ----------------
    def run_once(self):
        return run_once()

    def forward_random_now(self):
        """立即转发：在监控列表中随机挑一个对象，转发其最新一条（非置顶）微博。
        手动触发，不依赖轮询增量状态；命中黑名单则跳过。"""
        am = get_account_manager()
        accs = am.list_accounts()
        if not accs:
            return {"ok": False, "error": "没有可用账户，请先登录"}
        if not can_forward():
            return {"ok": False, "error": forward_block_reason() or "转发功能暂不可用"}
        acc = accs[0]
        cookie = am.get_cookie(acc["id"])
        mons = get_monitor_manager().list()
        if not mons:
            return {"ok": False, "error": "监控列表为空，请先添加监控对象"}

        target = random.choice(mons)
        uid = target["uid"]
        screen_name = target.get("screen_name", "")
        resp = get_latest_posts(uid, cookie, page=1, count=10)
        if not resp.get("ok") or not resp.get("posts"):
            return {"ok": False, "error": f"拉取 UID {uid} 失败：{resp.get('error', '空')}"}
        posts = [p for p in resp["posts"] if not p.get("is_top")]
        if not posts:
            return {"ok": False, "error": f"UID {uid} 无非置顶微博可转发"}
        chosen = random.choice(posts)

        cfg = get_config()
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
                warn(f"UID={uid} AI 文案生成失败，降级为普通转发：{e}")
                comment = ""
                ftype = "normal"
        acc_name = acc.get("display_name", "")

        # 关键词黑名单检测
        blacklist = cfg.get("blacklist", []) or []
        haystack = (chosen["text"] + " " + comment).lower()
        hit = [w for w in blacklist if w and w.lower() in haystack]
        db = get_history_db()
        if hit:
            db.add_forward(acc["id"], acc_name, uid, screen_name, chosen["id"],
                           chosen["text"], comment, ftype,
                           "skipped", reason=f"命中黑名单：{','.join(hit)}")
            return {"ok": True, "skipped": True, "uid": uid,
                    "screen_name": screen_name,
                    "detail": f"命中黑名单跳过：{','.join(hit)}"}

        r = repost(chosen["id"], comment, cookie, uid=uid)
        if r.get("ok"):
            db.add_forward(acc["id"], acc_name, uid, screen_name, chosen["id"],
                           chosen["text"], comment, ftype, "success")
            info(f"手动立即转发成功：UID={uid} persona={persona}")
            return {"ok": True, "uid": uid, "screen_name": screen_name,
                    "detail": f"已转发（{ftype}）：{comment or '纯转发'}"}
        db.add_forward(acc["id"], acc_name, uid, screen_name, chosen["id"],
                       chosen["text"], comment, ftype,
                       "failed", reason=r.get("error", "未知错误"))
        error(f"手动立即转发失败：UID={uid} {r.get('error')}")
        return {"ok": False, "error": f"转发失败：{r.get('error', '未知错误')}"}

    def start_scheduler(self):
        ok = get_scheduler().start()
        return {"ok": ok, "running": get_scheduler().is_running()}

    def stop_scheduler(self):
        get_scheduler().stop()
        return {"ok": True, "running": False}

    def scheduler_status(self):
        return get_scheduler().status()

    # ---------------- AI ----------------
    def list_personas(self):
        return list_personas()

    def preview_ai(self, text, persona, custom=None):
        try:
            custom = json.loads(custom) if isinstance(custom, str) and custom else custom
        except Exception:
            custom = None
        out = ai_generate(text or "这是一条测试微博，关于人工智能和开源软件的发展。",
                          persona or "幽默", custom)
        return {"ok": True, "text": out}

    def save_custom_persona(self, name, templates):
        try:
            templates = json.loads(templates) if isinstance(templates, str) else templates
        except Exception:
            return {"ok": False, "error": "模板格式错误"}
        cfg = get_config()
        cp = cfg.get("custom_personas", {}) or {}
        cp[name] = templates
        cfg.set("custom_personas", cp)
        return {"ok": True}

    # ---------------- 授权 ----------------
    def get_license_status(self):
        st = get_status()
        valid_until = int(time.time()) + FEATURE_CODE_VALID_DAYS * 86400
        return {"ok": True, "status": st,
                "feature_code": generate_feature_code(),
                "feature_code_valid_until": valid_until,
                "feature_code_valid_days": FEATURE_CODE_VALID_DAYS}

    def regenerate_feature_code(self):
        """重新生成特征码（时间戳 + 随机串变化）。"""
        return {"ok": True, "feature_code": generate_feature_code()}

    def activate_license(self, key):
        r = activate_license(key)
        if r.get("ok"):
            info(f"授权已激活：{r.get('type_name')}（到期 {r.get('exp_str')}）")
        else:
            warn(f"授权激活失败：{r.get('error')}")
        return r

    def deactivate_license(self):
        r = deactivate_license()
        return r

    # ---------------- 自定义人格 ----------------
    def delete_custom_persona(self, name):
        cfg = get_config()
        cp = cfg.get("custom_personas", {}) or {}
        if name not in cp:
            return {"ok": False, "error": "未找到该自定义人格"}
        del cp[name]
        cfg.set("custom_personas", cp)
        info(f"已删除自定义人格：{name}")
        return {"ok": True}

    # ---------------- 历史 / 日志 ----------------
    def get_monitored_posts(self, uid=None, limit=200, offset=0):
        return get_history_db().get_monitored_posts(uid, limit, offset)

    def get_forwards(self, limit=200, offset=0):
        return get_history_db().get_forwards(limit, offset)

    def get_logs(self, limit=500, offset=0):
        return get_history_db().get_logs(limit, offset)

    def read_log_file(self, limit=500):
        return read_file_lines(limit)

    # ---------------- 导出 ----------------
    def export_data(self, kind, fmt):
        import webview
        # 通过文件对话框选择保存位置
        suffix = {"csv": ".csv", "json": ".json", "xlsx": ".xlsx"}.get(fmt, ".txt")
        default_name = f"weibobot_{kind}{suffix}"
        try:
            paths = webview.windows[0].create_file_dialog(
                webview.SAVE_DIALOG, save_filename=default_name)
        except Exception:
            paths = None
        if not paths:
            return {"ok": False, "error": "已取消保存"}
        dest = paths if isinstance(paths, str) else paths[0]
        return export(kind, fmt, dest)

    def export_all_data(self):
        import webview
        try:
            folder = webview.windows[0].create_file_dialog(
                webview.FOLDER_DIALOG)
        except Exception:
            folder = None
        if not folder:
            return {"ok": False, "error": "已取消"}
        dest_dir = folder if isinstance(folder, str) else folder[0]
        return export_all(dest_dir)

    def export_filtered(self, kind, fmt, filters):
        """按前端筛选条件导出历史记录。"""
        import webview
        try:
            filters = json.loads(filters) if isinstance(filters, str) else filters
        except Exception:
            return {"ok": False, "error": "筛选条件格式错误"}
        suffix = {"csv": ".csv", "json": ".json", "xlsx": ".xlsx"}.get(fmt, ".txt")
        default_name = f"weibobot_{kind}_filtered{suffix}"
        try:
            paths = webview.windows[0].create_file_dialog(
                webview.SAVE_DIALOG, save_filename=default_name)
        except Exception:
            paths = None
        if not paths:
            return {"ok": False, "error": "已取消保存"}
        dest = paths if isinstance(paths, str) else paths[0]
        return export_filtered(kind, fmt, dest, filters)

    def export_selected(self, kind, ids_json, fmt):
        """导出选中的若干条历史记录（单选/多选）。"""
        import webview
        try:
            ids = json.loads(ids_json) if isinstance(ids_json, str) else ids_json
        except Exception:
            return {"ok": False, "error": "选择项格式错误"}
        if not ids:
            return {"ok": False, "error": "未选择任何记录"}
        db = get_history_db()
        if kind == "monitored":
            rows = db.get_monitored_posts_by_ids(ids)
        elif kind == "forwards":
            rows = db.get_forwards_by_ids(ids)
        else:
            return {"ok": False, "error": "未知类型"}
        if not rows:
            return {"ok": False, "error": "未找到对应记录"}
        suffix = {"csv": ".csv", "json": ".json", "xlsx": ".xlsx"}.get(fmt, ".txt")
        default_name = f"weibobot_{kind}_selected{suffix}"
        try:
            paths = webview.windows[0].create_file_dialog(
                webview.SAVE_DIALOG, save_filename=default_name)
        except Exception:
            paths = None
        if not paths:
            return {"ok": False, "error": "已取消保存"}
        dest = paths if isinstance(paths, str) else paths[0]
        return export_rows(kind, rows, fmt, dest)

    def load_image_base64(self, rel_path: str):
        """读取本地图片并返回 base64 Data URL，供前端预览。

        图片统一存储为相对 DATA_DIR 的 ``images\\...``，兼容旧版 ``data\\images\\...``。
        """
        try:
            rel = os.path.normpath(rel_path.replace("/", os.sep))
            # 兼容旧版：data\images\... → images\...
            if rel.lower().startswith("data" + os.sep):
                rel = rel[len("data" + os.sep):]
            candidates = [
                os.path.abspath(os.path.join(DATA_DIR, rel)),
                os.path.abspath(os.path.join(BASE_DIR, rel)),
            ]
            for p in candidates:
                if os.path.isfile(p):
                    with open(p, "rb") as f:
                        data = f.read()
                    ext = os.path.splitext(p)[1].lower()
                    mime_map = {
                        ".png": "image/png", ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg", ".gif": "image/gif",
                        ".webp": "image/webp",
                    }
                    mime = mime_map.get(ext, "image/jpeg")
                    return {"ok": True,
                            "data_url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}
            return {"ok": False, "error": f"图片文件不存在：{rel_path}"}
        except Exception as e:
            return {"ok": False, "error": f"读取图片失败：{e}"}
