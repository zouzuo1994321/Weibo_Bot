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
from forward_engine import (run_once, _download_avatar, _download_post_images,
                            manual_run_async, get_poll_progress)
from history_db import get_history_db
from logger import (info, warn, error, debug, read_file_lines,
                    set_verbose, is_verbose, CAT_CORE, CAT_MEDIA)
from monitor_manager import get_monitor_manager
from pathlib import Path
from paths import (APP_NAME, COPYRIGHT, COPYRIGHT_FULL, HISTORY_DIR,
                   MAJOR_VERSION, BASE_DIR, DATA_DIR, MEDIA_DIR)
from licensemgr import (get_status, generate_feature_code, activate_license,
                      deactivate_license, can_forward, forward_block_reason,
                      FEATURE_CODE_VALID_DAYS)
from scheduler import get_scheduler
from media_engine import (get_media_progress, start_media_download_async,
                          user_media_dir, maybe_run_scheduled_media_download)
from weibo_client import (get_user_info, get_latest_posts, diagnose_account,
                          repost, get_post_detail)
import model_manager


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
        # 软件运行时：若启用「本地大模型」路线且模型缺失，自动开始后台下载
        try:
            model_manager.maybe_autostart()
        except Exception:
            pass
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
            "ai_deploy": model_manager.get_state(),   # AI 部署进度（模型下载/加载状态）
        }

    def get_data_overview(self):
        """为「数据总览」页提供聚合统计与曲线序列。"""
        try:
            return {"ok": True, "data": get_history_db().data_overview()}
        except Exception as e:
            error(f"数据总览查询失败：{e}")
            return {"ok": False, "error": str(e)}

    def get_data_overview_by_date(self, date):
        """按指定日期（YYYY-MM-DD）查询当日抓取 / 转发趋势，供趋势曲线日期选择器使用。"""
        try:
            return {"ok": True, "data": get_history_db().data_overview_date(date)}
        except Exception as e:
            error(f"按日期查询数据总览失败：{e}")
            return {"ok": False, "error": str(e)}

    def export_data_overview(self, fmt="json"):
        """导出整理好的数据总览（JSON/CSV），方便用 AI 进一步分析。"""
        import webview
        try:
            data = get_history_db().data_overview()
            suffix = ".json" if fmt == "json" else ".csv"
            paths = webview.create_file_dialog(
                webview.SAVE_DIALOG,
                file_types=(fmt.upper() + " files", "*" + suffix),
                save_filename=f"data_overview{suffix}")
            if not paths:
                return {"ok": False, "error": "已取消保存"}
            dest = paths if isinstance(paths, str) else paths[0]
            if fmt == "json":
                with open(dest, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            else:
                rows = []
                for scope, vals in data["summary"].items():
                    rows.append({"范围": scope, "抓取": vals["monitored"], "转发": vals["forwarded"]})
                with open(dest, "w", newline="", encoding="utf-8-sig") as f:
                    import csv
                    w = csv.DictWriter(f, fieldnames=["范围", "抓取", "转发"])
                    w.writeheader()
                    w.writerows(rows)
            return {"ok": True, "path": dest}
        except Exception as e:
            error(f"导出数据总览失败：{e}")
            return {"ok": False, "error": str(e)}

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

    def manual_run_async(self):
        """后台异步执行一轮完整轮询，轮询完毕后立即转发一条（不保留抖动延迟）。
        前端通过 get_poll_progress 轮询进度与结果；转发内容经 run_once 去重（不重复）。"""
        return manual_run_async(force_immediate=True)

    def get_poll_progress(self):
        """返回当前轮询进度（用于「立即轮询一次」进度条）。"""
        try:
            return get_poll_progress()
        except Exception as e:
            return {"active": False, "error": str(e)}

    def forward_random_now(self):
        """立即转发：在监控列表中随机挑一个对象，转发其最新一条（非置顶）微博。
        手动触发，不依赖轮询增量状态；命中黑名单则跳过。
        （回退至 v2609080004 行为：直接随机挑选并转发，不先轮询。）"""
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
                           "skipped", reason=f"命中黑名单：{','.join(hit)}", persona=persona)
            return {"ok": True, "skipped": True, "uid": uid,
                    "screen_name": screen_name,
                    "detail": f"命中黑名单跳过：{','.join(hit)}"}

        r = repost(chosen["id"], comment, cookie, uid=uid)
        if r.get("ok"):
            db.add_forward(acc["id"], acc_name, uid, screen_name, chosen["id"],
                           chosen["text"], comment, ftype, "success", persona=persona)
            info(f"手动立即转发成功：UID={uid} persona={persona}")
            return {"ok": True, "uid": uid, "screen_name": screen_name,
                    "detail": f"已转发（{ftype}）：{comment or '纯转发'}"}
        db.add_forward(acc["id"], acc_name, uid, screen_name, chosen["id"],
                       chosen["text"], comment, ftype,
                       "failed", reason=r.get("error", "未知错误"), persona=persona)
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

    def save_custom_persona(self, name, desc):
        """保存自定义人格。

        desc 可为：
        - 字符串：新版「人设介绍」自然语言描述（如「你是猫娘，说话软萌」）
        - 列表：旧版模板列表（兼容历史数据，内部归一为文本存储）
        """
        try:
            desc = json.loads(desc) if isinstance(desc, str) else desc
        except Exception:
            desc = desc  # 当作纯文本处理
        if isinstance(desc, (list, tuple)):
            desc = "\n".join(str(t) for t in desc)
        desc = (desc or "").strip()
        if not desc:
            return {"ok": False, "error": "请填写人格介绍"}
        cfg = get_config()
        cp = cfg.get("custom_personas", {}) or {}
        cp[name] = desc
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

    # ---------------- AI 引擎路线（主：本地大模型 / 备：轻量规则） ----------------
    def get_ai_engine_status(self):
        cfg = get_config()
        return {
            "ok": True,
            "ai_engine": cfg.get("ai_engine", "rule"),
            "ai_enabled": cfg.get("ai_enabled", False),
            "deploy": model_manager.get_state(),
        }

    def set_ai_engine(self, engine):
        if engine not in ("model", "rule"):
            return {"ok": False, "error": "未知引擎路线"}
        get_config().set("ai_engine", engine)
        info(f"AI 引擎路线已切换为：{engine}")
        # 切换到本地大模型且模型缺失时，自动开始后台下载
        if engine == "model":
            try:
                model_manager.maybe_autostart()
            except Exception as e:
                warn(f"切换后触发模型下载失败：{e}")
        return {"ok": True, "ai_engine": engine}

    def start_model_download(self):
        return model_manager.start_download()

    def get_model_progress(self):
        return model_manager.get_state()

    def open_models_dir(self):
        path = model_manager.ensure_models_dir()
        try:
            os.startfile(path)  # Windows：打开资源管理器
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e), "path": path}
        return {"ok": True}

    # ---------------- 历史 / 日志 ----------------
    def get_monitored_posts(self, uid=None, limit=200, offset=0):
        return get_history_db().get_monitored_posts(uid, limit, offset)

    def repair_history_images(self, limit=100):
        """修复历史记录里被抓成表情包的微博图片。

        逐条按微博 ID 重新请求详情接口取 `pic_infos`（真正的推文配图），
        重新下载并覆盖同名文件后写回数据库；纯文本微博则清空其错误图片。
        """
        try:
            from account_manager import get_account_manager
            acc_mgr = get_account_manager()
            accounts = acc_mgr.list_accounts()
            if not accounts:
                return {"ok": False, "error": "没有可用账户，请先登录"}
            cookie = acc_mgr.get_cookie(accounts[0]["id"])

            db = get_history_db()
            try:
                limit = max(1, min(int(limit or 100), 1000))
            except Exception:
                limit = 100
            posts = db.get_monitored_posts(limit=limit)
            fixed = cleaned = failed = 0
            debug(f"开始修复历史图片 · 共 {len(posts)} 条", CAT_CORE)
            for p in posts:
                pid = str(p.get("post_id") or "")
                if not pid:
                    continue
                try:
                    d = get_post_detail(pid, cookie)
                except Exception as e:
                    failed += 1
                    debug(f"修复失败 {pid}：{e}", CAT_CORE)
                    time.sleep(0.5)
                    continue
                if not d.get("ok"):
                    failed += 1
                    time.sleep(0.5)
                    continue
                imgs = d.get("images") or []
                if not imgs:
                    # 详情接口明确无配图 → 清掉此前误存的表情包
                    if p.get("images"):
                        db.update_post_images(pid, [])
                        cleaned += 1
                    time.sleep(0.4)
                    continue
                saved = _download_post_images(imgs, p.get("uid") or "", pid, cookie)
                if saved:
                    db.update_post_images(pid, saved)
                    fixed += 1
                else:
                    failed += 1
                time.sleep(0.6)   # 控速，避免触发风控
            msg = (f"历史图片修复完成 · 重新下载 {fixed} 条 · 清除误存表情 {cleaned} 条"
                   f" · 失败/无权限 {failed} 条")
            info(msg, CAT_CORE)
            return {"ok": True, "fixed": fixed, "cleaned": cleaned,
                    "failed": failed, "total": len(posts), "message": msg}
        except Exception as e:
            error(f"历史图片修复失败：{e}", CAT_CORE)
            return {"ok": False, "error": str(e)}

    def get_forwards(self, limit=200, offset=0):
        return get_history_db().get_forwards(limit, offset)

    def get_logs(self, limit=500, offset=0, level="ALL", category="ALL", keyword=""):
        """查询日志，支持级别 / 模块分类筛选与关键字搜索。"""
        db = get_history_db()
        rows = db.get_logs(limit, offset, level, category, keyword)
        try:
            total = db.count_logs(level, category, keyword)
        except Exception:
            total = len(rows)
        return {"rows": rows, "total": total}

    def read_log_file(self, limit=500):
        return read_file_lines(limit)

    def clear_logs(self):
        """清空运行日志（数据库），并返回是否成功。"""
        try:
            get_history_db().clear_logs()
            info("运行日志已清空", CAT_CORE)
            return {"ok": True}
        except Exception as e:
            error(f"清空日志失败：{e}", CAT_CORE)
            return {"ok": False, "error": str(e)}

    def set_log_verbose(self, flag):
        """开启/关闭冗余调试日志（DEBUG 级，记录 HTTP 细节与每步决策）。"""
        try:
            set_verbose(bool(flag))
            try:
                cfg = get_config()
                cfg.set("log_verbose", bool(flag))
            except Exception:
                pass
            info(f"冗余调试日志已{'开启' if flag else '关闭'}", CAT_CORE)
            return {"ok": True, "verbose": bool(flag)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_log_verbose(self):
        return {"verbose": is_verbose()}

    def export_logs(self, fmt="txt"):
        """导出当前筛选后的日志到用户指定位置。"""
        import webview
        suffix = {"csv": ".csv", "json": ".json", "txt": ".txt"}.get(fmt, ".txt")
        try:
            win = getattr(self, "main_window", None)
            res = win.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=BASE_DIR,
                save_filename=f"weibobot_logs{suffix}",
                file_types=("日志文件 (*.txt;*.csv;*.json)",),
            )
            path = res[0] if isinstance(res, (list, tuple)) and res else (res or "")
            if not path:
                return {"ok": False, "error": "已取消", "path": ""}
            rows = get_history_db().get_logs(limit=100000, offset=0)
            if fmt == "json":
                import json as _json
                with open(path, "w", encoding="utf-8") as f:
                    _json.dump(rows, f, ensure_ascii=False, indent=2)
            elif fmt == "csv":
                import csv
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["id", "level", "message", "created_at"])
                    for r in rows:
                        w.writerow([r["id"], r["level"], r["message"], r["created_at"]])
            else:
                with open(path, "w", encoding="utf-8") as f:
                    for r in rows:
                        f.write(f"[{r['created_at']}][{r['level']}] {r['message']}\n")
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e), "path": ""}

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

    # ---------------- 视频 / 相册 ----------------
    def get_media_config(self):
        """获取视频/相册下载设置。"""
        return get_config().get("media_download", {}) or {}

    def save_media_config(self, patch):
        """保存视频/相册下载设置（局部更新）。"""
        try:
            cfg = get_config()
            md = dict(cfg.get("media_download", {}) or {})
            md.update(patch or {})
            cfg.set("media_download", md)
            info(f"视频/相册设置已更新：{list((patch or {}).keys())}")
            return {"ok": True, "config": md}
        except Exception as e:
            error(f"保存视频/相册设置失败：{e}")
            return {"ok": False, "error": str(e)}

    def start_media_download(self):
        """手动执行一次下载（后台线程，首次全量、后续增量）。"""
        return start_media_download_async()

    def get_media_progress(self):
        """下载进度快照，供前端轮询。"""
        return get_media_progress()

    def get_media_summary(self):
        """媒体库统计：对象数 / 视频数 / 图片数 / 占用空间。"""
        return get_history_db().get_media_summary()

    def clear_media_records(self):
        """清理软件内的下载记录（media_files 表），**不删除已下载的文件**。

        清理后增量判定重置：再次执行「立即执行下载」会重新拉取全部资源，
        并对文件名一致的文件直接覆盖。
        """
        try:
            get_history_db().clear_media_files()
            info("已清理下载记录（本地文件保留，增量判定已重置）", CAT_MEDIA)
            return {"ok": True}
        except Exception as e:
            error(f"清理下载记录失败：{e}", CAT_MEDIA)
            return {"ok": False, "error": str(e)}

    def list_media_objects(self):
        """列出媒体库中已有的监控对象。"""
        return get_history_db().list_media_objects()

    def search_media(self, keyword):
        """按 UID / 昵称 / 正文搜索媒体文件。"""
        return get_history_db().search_media(keyword or "")

    def get_media_by_uid(self, uid, media_type="all"):
        """取某个监控对象的媒体列表。"""
        return get_history_db().get_media_by_uid(uid, media_type)

    def load_media_base64(self, rel_path):
        """读取媒体文件并返回 base64 Data URL（图片预览用）。"""
        try:
            rel = os.path.normpath(str(rel_path).replace("/", os.sep))
            p = os.path.abspath(os.path.join(MEDIA_DIR, rel))
            if not os.path.isfile(p):
                return {"ok": False, "error": f"文件不存在：{rel_path}"}
            with open(p, "rb") as f:
                data = f.read()
            ext = os.path.splitext(p)[1].lower()
            mime_map = {
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
                ".mp4": "video/mp4", ".mov": "video/quicktime",
                ".m4v": "video/mp4", ".webm": "video/webm",
            }
            mime = mime_map.get(ext, "application/octet-stream")
            return {"ok": True,
                    "data_url": f"data:{mime};base64,{base64.b64encode(data).decode()}",
                    "size": len(data)}
        except Exception as e:
            return {"ok": False, "error": f"读取媒体失败：{e}"}

    def get_media_file_url(self, rel_path):
        """返回媒体文件的 file:// URL（视频预览用，避免 base64 过大卡界面）。"""
        try:
            rel = os.path.normpath(str(rel_path).replace("/", os.sep))
            p = os.path.abspath(os.path.join(MEDIA_DIR, rel))
            if not os.path.isfile(p):
                return {"ok": False, "error": "文件不存在"}
            return {"ok": True, "url": Path(p).as_uri(),
                    "path": p, "size": os.path.getsize(p)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------------- 监控对象 导入 / 导出 ----------------
    def export_monitors(self, fmt="json"):
        """导出监控列表（json / csv / txt）到用户指定位置。"""
        import webview
        suffix = {"csv": ".csv", "json": ".json", "txt": ".txt"}.get(fmt, ".txt")
        try:
            win = getattr(self, "main_window", None)
            res = win.create_file_dialog(
                webview.SAVE_DIALOG, directory=BASE_DIR,
                save_filename=f"monitors{suffix}",
                file_types=("监控列表 (*.json;*.csv;*.txt)",))
            path = res[0] if isinstance(res, (list, tuple)) and res else (res or "")
            if not path:
                return {"ok": False, "error": "已取消", "path": ""}
            items = get_monitor_manager().list()
            if fmt == "json":
                with open(path, "w", encoding="utf-8") as f:
                    json.dump([{"uid": it["uid"], "screen_name": it.get("screen_name", "")}
                               for it in items], f, ensure_ascii=False, indent=2)
            elif fmt == "csv":
                import csv as _csv
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    w = _csv.writer(f)
                    w.writerow(["uid", "screen_name"])
                    for it in items:
                        w.writerow([it["uid"], it.get("screen_name", "")])
            else:
                with open(path, "w", encoding="utf-8") as f:
                    for it in items:
                        f.write(f"{it['uid']}\n")
            info(f"导出监控对象 {len(items)} 个 → {path}")
            return {"ok": True, "path": path, "count": len(items)}
        except Exception as e:
            error(f"导出监控对象失败：{e}")
            return {"ok": False, "error": str(e), "path": ""}

    def import_monitors_from_file(self):
        """从文件导入监控列表（json / csv / txt）。"""
        import webview
        try:
            win = getattr(self, "main_window", None)
            res = win.create_file_dialog(
                webview.OPEN_DIALOG, directory=BASE_DIR,
                file_types=("监控列表 (*.json;*.csv;*.txt)", "所有文件 (*.*)"))
            path = res[0] if isinstance(res, (list, tuple)) and res else (res or "")
            if not path:
                return {"ok": False, "error": "已取消"}
            try:
                with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
                    text = f.read()
            except Exception as e:
                return {"ok": False, "error": f"读取文件失败：{e}"}
            return self.import_monitors_text(text)
        except Exception as e:
            error(f"导入监控对象失败：{e}")
            return {"ok": False, "error": str(e)}

    def import_monitors_text(self, text):
        """按文本导入监控对象。

        支持三种格式：
          1) JSON 数组：[{"uid":"123","screen_name":"昵称"}] 或 {"monitors":[...]}
          2) CSV / 文本：每行 `UID,昵称`（也兼容逗号、制表符、分号分隔）
          3) 纯 UID 或微博主页链接（https://weibo.com/u/1234567890），每行一个
        已存在的 UID 自动跳过，不会重复添加。
        """
        import re as _re
        try:
            mon = get_monitor_manager()
            text = (text or "").strip()
            if not text:
                return {"ok": False, "error": "内容为空"}

            rows = []
            if text.startswith("[") or text.startswith("{"):
                try:
                    data = json.loads(text)
                    if isinstance(data, dict):
                        data = (data.get("monitors") or data.get("list")
                                or data.get("data") or [])
                    for it in data:
                        if isinstance(it, dict):
                            rows.append((
                                str(it.get("uid") or it.get("id") or "").strip(),
                                str(it.get("screen_name") or it.get("name")
                                    or it.get("nickname") or "").strip()))
                        else:
                            rows.append((str(it).strip(), ""))
                except Exception as e:
                    return {"ok": False, "error": f"JSON 解析失败：{e}"}
            else:
                for line in text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = _re.split(r"[,，\t;]+", line, 1)
                    uid = parts[0].strip()
                    name = parts[1].strip() if len(parts) > 1 else ""
                    rows.append((uid, name))

            added, skipped, failed, msgs = 0, 0, 0, []
            for uid, name in rows:
                if not uid:
                    continue
                # 兼容微博主页链接，提取其中的 UID
                m = (_re.search(r"weibo\.(?:com|cn)/(?:u/)?(\d{5,})", uid)
                     or _re.match(r"^(\d{5,})$", uid))
                if m:
                    uid = m.group(1)
                if not uid.isdigit():
                    failed += 1
                    msgs.append(f"无效 UID：{uid}")
                    continue
                ok, msg = mon.add(uid, name)
                if ok:
                    added += 1
                elif "已在监控列表" in str(msg):
                    skipped += 1
                else:
                    failed += 1
                    msgs.append(f"{uid}：{msg}")

            info(f"导入监控对象完成 · 新增 {added} · 已存在跳过 {skipped} · 失败 {failed}")
            return {"ok": True, "added": added, "skipped": skipped,
                    "failed": failed, "messages": msgs[:10]}
        except Exception as e:
            error(f"导入监控对象异常：{e}")
            return {"ok": False, "error": str(e)}

    def open_media_dir(self, uid=""):
        """打开媒体目录（传入 uid 则打开该对象的目录）。"""
        try:
            if uid:
                items = {it["uid"]: it for it in get_monitor_manager().list()}
                it = items.get(str(uid)) or {}
                target = user_media_dir(str(uid), it.get("screen_name", ""))
            else:
                target = MEDIA_DIR
            os.makedirs(target, exist_ok=True)
            try:
                os.startfile(target)          # Windows 资源管理器
            except Exception:
                webbrowser.open(Path(target).as_uri())
            return {"ok": True, "path": target}
        except Exception as e:
            return {"ok": False, "error": str(e)}
