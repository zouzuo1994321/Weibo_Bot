# -*- coding: utf-8 -*-
"""调度器：按设定间隔在后台线程轮询，支持工作时间限制。"""
import random
import threading
import time
import traceback
from datetime import datetime

from account_manager import get_account_manager
from config_manager import get_config
from forward_engine import run_once
from logger import info, warn, error, debug, CAT_SCHED
from media_engine import maybe_run_scheduled_media_download


def _in_working_hours() -> bool:
    cfg = get_config()
    wh = cfg.get("working_hours", {})
    if not wh.get("enabled"):
        return True
    try:
        start = datetime.strptime(wh.get("start", "00:00"), "%H:%M").time()
        end = datetime.strptime(wh.get("end", "23:59"), "%H:%M").time()
    except Exception:
        return True
    now = datetime.now().time()
    if start <= end:
        return start <= now <= end
    # 跨天（如 22:00 - 08:00）
    return now >= start or now <= end


class Scheduler:
    def __init__(self):
        self._running = False
        self._thread = None
        self.last_run = None
        self.next_run = None
        self.last_result = None
        self._lock = threading.Lock()
        self._last_keepalive_ts = 0.0
        self.last_keepalive = None
        # 【v1.6.5】独立保活线程：不依赖排程是否运行
        self._ka_stop = threading.Event()
        self._ka_thread = None
        # 【v1.4.20】本轮轮询排程出的实际转发执行时间（浮点秒，含抖动偏移）。
        # next_run 是「下一次轮询」；真正下一次「发出去」的时刻是这个字段。
        # 前端进度环用它对齐 100%，未排程时回退到 next_run。
        self.next_forward_run = None

    def is_running(self):
        """只有标记为运行中、且后台线程确实存活，才算真正在运行。

        避免调度线程因异常退出后 `self._running` 仍为 True，
        导致界面一直显示“运行中”却永远不再轮询（假死状态）。
        """
        return bool(self._running) and self._thread is not None and self._thread.is_alive()

    def start(self):
        # 线程还活着 → 已在运行，无需重复启动
        if self._running and self._thread is not None and self._thread.is_alive():
            return False
        # 自愈：标记为运行中但线程已死（异常退出），重建线程而不阻塞用户重启
        if self._running and self._thread is not None and not self._thread.is_alive():
            warn("检测到调度线程已异常退出，正在自动重启", CAT_SCHED)
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        info("调度器已启动", CAT_SCHED)
        return True

    def stop(self):
        self._running = False
        info("调度器已停止", CAT_SCHED)
        return True

    def _loop(self):
        while self._running:
            interval = None
            try:
                cfg = get_config()
                base = max(1, int(cfg.get("interval_minutes", 30)))
                # 轮询间隔抖动：在 ±poll_jitter_ratio 范围内随机，避免固定节奏被识别为机器人
                ratio = cfg.get("poll_jitter_ratio", 0.2)
                try:
                    ratio = float(ratio)
                except Exception:
                    ratio = 0.2
                jitter = base * ratio
                interval = max(1, int(round(base + random.uniform(-jitter, jitter))))
                with self._lock:
                    self.next_run = (datetime.now().timestamp() + interval * 60)
                    self.next_forward_run = None   # 【v1.4.20】新一轮开始，旧排程视为已消费
                next_run_str = datetime.fromtimestamp(self.next_run).strftime("%H:%M")
                in_wh = _in_working_hours()
                info(f"调度器心跳 · 下次轮询约 {next_run_str}（间隔 {interval} 分钟）"
                 + ("" if in_wh else " · 当前非工作时间，等待中"), CAT_SCHED)
                # 视频 / 相册：到达每日设定时间且当天未执行过，则触发一次下载
                try:
                    maybe_run_scheduled_media_download()
                except Exception as e:
                    error(f"检查媒体下载定时失败：{e}", CAT_SCHED)
                if in_wh:
                    try:
                        res = run_once()
                        self.last_result = res
                        self.last_run = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        # 【v1.4.20】本轮若排程了转发，把执行时间暴露给进度环
                        self._capture_scheduled_run_at(res)
                        try:
                            self._maybe_keepalive()
                        except Exception as e:
                            error(f"保活任务异常：{e}", CAT_SCHED)
                    except Exception as e:
                        error(f"调度执行异常：{e}", CAT_SCHED)
                        error(traceback.format_exc(), CAT_SCHED)
                else:
                    warn("当前不在工作时间，跳过本轮轮询", CAT_SCHED)
            except Exception as e:
                # 兜底：循环体内任何未预料的异常都必须留下痕迹。
                # 否则线程会静默退出，界面仍显示“运行中”却永不再轮询——
                # 这正是历史上 `random` 未导入导致“自动轮询无法启动”且日志无任何报错的原因。
                error(f"调度循环异常：{e}", CAT_SCHED)
                error(traceback.format_exc(), CAT_SCHED)
            # 按（抖动后的）间隔等待；若本轮异常导致 interval 未算出，退回到配置间隔
            if interval is None:
                try:
                    interval = max(1, int(get_config().get("interval_minutes", 30)))
                except Exception:
                    interval = 30
            waited = 0
            while self._running and waited < interval * 60:
                time.sleep(5)
                waited += 5

    def _capture_scheduled_run_at(self, res):
        """【v1.4.20】从本轮 run_once 结果里提取排程转发的实际执行时刻。

        run_once 的 results 是「按 UID 聚合」的字典列表（action/detail/run_at
        由 _forward_one 的返回值合并进对应 UID 项），任一项带
        action=="scheduled" 且有 run_at 即视为本轮已排程。
        【v1.6.1】一轮可能排程多条，进度环应对齐**最早**那一跳（真正的「下一次转发」）。
        """
        try:
            stamps = [r.get("run_at") for r in (res.get("results") or [])
                      if r.get("action") == "scheduled" and r.get("run_at")]
            if stamps:
                with self._lock:
                    self.next_forward_run = min(stamps)
        except Exception:
            pass

    def _maybe_keepalive(self, force=False):
        """按 keepalive_minutes 节流触发会话保活，避免每轮轮询都联网。

        force=True 时跳过节流（启动后首次、退出前兜底）。
        """
        try:
            cfg = get_config()
            km = max(5, int(cfg.get("keepalive_minutes", 30)))
        except Exception:
            km = 30
        now_ts = time.time()
        if not force and self._last_keepalive_ts and (now_ts - self._last_keepalive_ts) < km * 60:
            return None
        self._last_keepalive_ts = now_ts
        info("执行会话保活探针（双通道）", CAT_SCHED)
        return self.keepalive_all()

    # ---------------- 独立保活线程（v1.6.5） ----------------
    # 此前保活只挂在轮询循环里（run loop 内），软件一关 / 停止轮询就没人续期，
    # 微博 7 天硬期限一到必然失效 —— 这就是「系统浏览器能存一周以上、软件却刚好
    # 一周掉线」的直接原因。现在改为常驻线程 + 启动/退出各补一次。
    def start_keepalive_worker(self):
        """启动常驻保活线程（幂等）。每 60s 醒一次，按 keepalive_minutes 节流真正联网。"""
        if self._ka_thread is not None and self._ka_thread.is_alive():
            return False
        self._ka_stop.clear()
        t = threading.Thread(target=self._keepalive_loop, daemon=True,
                             name="cookie-keepalive")
        self._ka_thread = t
        t.start()
        info("会话保活常驻线程已启动（不依赖排程）", CAT_SCHED)
        return True

    def _keepalive_loop(self):
        # 先等 20s：让首屏、状态缓存、开屏自推进先跑完，别抢网络
        if self._ka_stop.wait(20):
            return
        try:
            self._maybe_keepalive(force=True)
        except Exception as e:
            warn(f"启动保活失败：{e}", CAT_SCHED)
        while not self._ka_stop.is_set():
            if self._ka_stop.wait(60):
                return
            try:
                self._maybe_keepalive()
            except Exception as e:
                warn(f"周期保活失败：{e}", CAT_SCHED)

    def stop_keepalive_worker(self):
        try:
            self._ka_stop.set()
        except Exception:
            pass

    def final_keepalive(self, timeout=6.0):
        """退出前补一次续期。**走节流**，有界等待，绝不拖慢退出。

        故意不 force：正常使用时启动/周期保活刚跑过，节流会直接跳过，退出零延迟；
        只有「开了十几秒就关」这种还没轮到首次保活的情况才会真的发一次请求。
        """
        try:
            if not get_account_manager().list_accounts():
                return False
        except Exception:
            return False
        done = threading.Event()

        def _run():
            try:
                self._maybe_keepalive()
            except Exception:
                pass
            finally:
                done.set()

        threading.Thread(target=_run, daemon=True, name="ka-final").start()
        return done.wait(timeout)

    def keepalive_all(self):
        """对所有账户执行双通道会话保活，并将续期结果回写。

        【v1.6.7】每轮额外跑一次登录态实测，把「此刻到底还有没有效」记成 last_verified_at。
        【v1.6.9 修正】实测改为**两级**，且口径必须与 check_account 一致：
          ① 同源 `probe_login_web`（weibo.com/ajax/profile/info）——唯一有资格判「失效」；
          ② 移动端 `probe_login`（m.weibo.cn/api/config）——**只敢确认有效，不敢否定**
             （跨域口径差异，网页端有效 Cookie 在移动端也常报未登录）。
        v1.6.7 只用②并据其 False 判失效，导致「一切正常却显示实测已失效」，
        所以这里把②降级为兜底确认。探测不到一律**不写库**。
        """
        from weibo_client import keepalive, probe_login, probe_login_web
        am = get_account_manager()
        results = []
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for acc in am.list_accounts():
            cookie = am.get_cookie(acc["id"])
            name = acc.get("display_name", acc.get("id", ""))
            if not cookie or "SUB=" not in cookie:
                results.append({"name": name, "ok": False, "status": "no_cookie",
                                "detail": "缺少登录凭证", "renewed": False})
                continue
            try:
                res = keepalive(cookie, entries=am.get_cookie_entries(acc["id"]))
                if res.get("ok") and (res.get("entries") or res.get("cookie")):
                    am.update_cookie(acc["id"], res.get("cookie") or "",
                                     "保活探针已自动续期 Cookie（会话保活）",
                                     entries=res.get("entries"))
                # 实测登录态（用保活后的最新 Cookie，比原 Cookie 更接近服务端认知）
                live_cookie = res.get("cookie") or cookie
                uid = acc.get("wb_uid") or ""
                web = probe_login_web(live_cookie, uid)
                probe = web
                # 同源测不出来才退回移动端；移动端只能把「有效」补上来，不能推翻
                if web.get("login") is None:
                    mob = probe_login(live_cookie)
                    if mob.get("login") is True:
                        probe = mob
                wrote = None
                if probe.get("login") is not None:
                    wrote = bool(probe["login"])
                    am.set_verified(acc["id"], wrote, source="会话保活实测",
                                    detail=probe.get("detail", ""))
                # 【v1.6.9】每次实测都留痕，便于事后从日志还原判定过程
                if wrote is None:
                    info(f"登录态实测跳过 {name} · 同源={web.get('status')}"
                         f"（{web.get('detail', '')}）· 移动端未给出有效结论，不写库",
                         CAT_SCHED)
                else:
                    info(f"登录态实测 {name} · 同源={web.get('status')} → 移动端="
                         f"{probe.get('status')} · 写入={'有效' if wrote else '失效'}"
                         f" · {probe.get('detail', '')}", CAT_SCHED)
                results.append({"name": name, "ok": res.get("ok"),
                                "status": res.get("status"), "detail": res.get("detail"),
                                "renewed": res.get("cookie") != cookie,
                                "verified": wrote})
            except Exception as e:
                results.append({"name": name, "ok": False, "status": "error",
                                "detail": str(e), "renewed": False})
        summary = {"time": now, "total": len(results),
                   "ok": sum(1 for r in results if r.get("ok")), "results": results}
        self.last_keepalive = summary
        info(f"会话保活完成 · 成功 {summary["ok"]}/{summary["total"]}", CAT_SCHED)
        return summary


    def status(self):
        cfg = get_config()
        return {
            "running": self.is_running(),
            "interval_minutes": cfg.get("interval_minutes", 30),
            "last_run": self.last_run,
            "next_run": self.next_run,
            "next_forward_run": self.next_forward_run,   # 【v1.4.20】下一次实际转发时刻
            "in_working_hours": _in_working_hours(),
            "last_result": self.last_result,
            "last_keepalive": self.last_keepalive,
        }


_scheduler = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler
