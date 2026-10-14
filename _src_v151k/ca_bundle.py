# -*- coding: utf-8 -*-
"""CA 证书包自愈（PyInstaller onefile + certifi）。

问题现象（2026-09-14 / 2026-10-12 实测日志）：
    拉取微博失败 uid=... · 判定=network_error · 已尝试 3 种接口 · 原因：
    mymblog exc: Could not find a suitable TLS CA certificate bundle,
    invalid path: C:\\...\\AppData\\Local\\Temp\\_MEI00000c5c2\\certifi\\cacert.pem
    —— 三种接口、相册、账户检测**同时**全部以 network_error 失败，重启程序后立刻恢复正常。

根因：PyInstaller onefile 会把 certifi 的 cacert.pem 解压到 `%TEMP%\\_MEIxxxxxx\\certifi\\`。
（已核实 cacert.pem 确实打进了 exe，不是打包缺失。）一旦该临时目录在程序运行期间
被 Windows 存储感知 / 磁盘清理 / 杀毒软件 / CCleaner 之类工具清掉，requests 就会对
**每一个** HTTPS 请求抛 OSError，表现为全站网络异常；重启会解压出新的_MEI 目录，所以「重启即好」。

【v1.1.9k 0011 重写 —— 旧版有三个致命缺陷，导致自愈 100% 失效】
  ① **备份从未被创建**。旧逻辑是「certifi 的包存在 → 顺手备份」，即备份动作发生在
     「路径正常」时。可一旦 _MEI 被清理，第一次进入 ensure_ca_bundle() 时备份必然为空，
     于是返回「证书包缺失且无备份」→ 永远无法自愈。**备份必须无条件预置，不能等失效。**
  ② **备份没跟着数据目录迁移**。程序把 DATA_DIR 从 Z 盘迁到 %LOCALAPPDATA% 后，
     迁移清单里没有 cacert.pem，旧备份仍留在老目录，新目录里压根没有这个文件。
  ③ **调用方 `_ca_healed` 是假性成功**：它在**尝试之前**就置 True，自愈失败后所有后续
     请求都被 `if _ca_healed: return` 挡掉，**再也不会重试**。实测表现为：日志里 5621 条
     CA 错误，却一条「自愈」记录都没有——自愈代码每次都在第 3 行就 return 了。

本版对策：
  A. `ensure_ca_bundle()` 一进来就**先做备份预置**（无论 certifi 当前是否可用），
     保证「随时都有一个可用的本地证书包」；
  B. 多级兜底源，逐级尝试直到拿到一份能用的 cacert.pem：
       ① DATA_DIR/cacert.pem（本地持久副本，首选，不再依赖 _MEI）
       ② _MEIPASS/certifi/cacert.pem（本次运行的解压产物）
       ③ sys.base_prefix 下真实 certifi 包的cacert.pem
       ④ 老数据目录（LEGACY_DATA_DIR）里的历史备份
       ⑤ Windows 自带的 schannel.pem / curl-ca-bundle.crt
  C. `force_retry` 参数 + 调用方只在**真正拿到可用路径**后才置标志，失败允许下次重试；
  D. 成功时把路径写入 REQUESTS_CA_BUNDLE / SSL_CERT_FILE / CURL_CA_BUNDLE，
     requests 每次请求都通过 merge_environment_settings 重读这三个环境变量，无需改业务代码。
"""
import os
import shutil
import sys

_BACKUP_NAME = "cacert.pem"

# 供调用方判断「是否真的拿到了可用证书包」
OK = "ok"
HEALED = "已改用本地备份证书包"


def _backup_path():
    """本地持久副本的目标路径（DATA_DIR/cacert.pem）。"""
    try:
        from paths import DATA_DIR
        return os.path.join(DATA_DIR, _BACKUP_NAME)
    except Exception:
        return ""


def _legacy_backup_path():
    """老数据目录（exe 同级 data/）里的历史备份，作为兜底源之一。"""
    try:
        from paths import LEGACY_DATA_DIR, DATA_DIR
        p = os.path.join(LEGACY_DATA_DIR, _BACKUP_NAME)
        if os.path.abspath(p) != os.path.abspath(os.path.join(DATA_DIR, _BACKUP_NAME)):
            return p
    except Exception:
        pass
    return ""


def _meipass_certifi():
    """本次运行的 _MEIPASS 解压产物（onefile 正常运行时的原始来源）。"""
    meipass = getattr(sys, "_MEIPASS", "")
    if not meipass:
        return ""
    return os.path.join(meipass, "certifi", _BACKUP_NAME)


def _real_certifi():
    """构建/开发环境里真实安装的 certifi 包（非 frozen 时 sys.prefix 下）。"""
    try:
        import certifi
        p = certifi.where()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    return os.path.join(sys.base_prefix, "certifi", _BACKUP_NAME)


def _windows_roots():
    """Windows 自带 / curl 的 CA 包，最后的兜底。"""
    for p in (os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                           "System32", "schannel.pem"),
              os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                           "System32", "curl-ca-bundle.crt")):
        if os.path.exists(p):
            yield p


def _valid(path):
    """证书包是否「看起来完整」：存在、非空、且含 PEM 头。"""
    if not path or not os.path.exists(path):
        return False
    try:
        if os.path.getsize(path) < 1024:      # 正常 cacert.pem ≈ 220~250KB
            return False
        with open(path, "rb") as f:
            return b"-----BEGIN CERTIFICATE-----" in f.read(4096)
    except Exception:
        return False


def _iter_sources():
    """按优先级产出候选证书包路径。"""
    for p in (_backup_path(),
              _meipass_certifi(),
              _real_certifi(),
              _legacy_backup_path()):
        if p:
            yield p
    for p in _windows_roots():
        yield p


def _copy(src, dst):
    try:
        d = os.path.dirname(dst)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        shutil.copyfile(src, dst)
        return _valid(dst)
    except Exception:
        return False


def ensure_ca_bundle(force_retry=False):
    """确保 requests 能找到可用的 CA 证书包。

    返回 (状态, 证书路径)：
      ok / 已改用本地备份证书包  —— path非空，**已写入环境变量，HTTPS 可用**
      证书包完全不可用·<原因>    —— path 为空，HTTPS 仍会失败

    参数 force_retry：调用方上次尝试失败时传 True，允许再试一次
                    （旧版用 _ca_healed 一刀切，导致失败后永不重试）
    """
    backup = _backup_path()

    #---- A. 备份预置：无论 certifi 当前是否可用，先保证本地有一份 ----
    # 这一步必须在「判断 live 是否有效」**之前**，否则就是旧版的死循环：
    # 等到live 失效才想备份，而那时已无源可备份。
    if not _valid(backup):
        for src in _iter_sources():
            if src and os.path.abspath(src) != os.path.abspath(backup or "") and _valid(src):
                if _copy(src, backup):
                    break

    # ---- B. 找一个当前可用的证书包 ----
    chosen = ""
    for src in _iter_sources():
        if _valid(src):
            chosen = src
            break

    if not chosen:
        return (f"证书包完全不可用（已尝试 {len(list(_iter_sources()))} 个来源，"
                f"本地备份 {backup or '<无法确定>'} 也不可用）"), ""

    # ---- C. 把结果固化到本地备份 + 写入环境变量 ----
    if backup and not _valid(backup) and _valid(chosen):
        _copy(chosen, backup)

    if backup and _valid(backup) and os.path.abspath(chosen) != os.path.abspath(backup):
        chosen = backup

    os.environ["REQUESTS_CA_BUNDLE"] = chosen
    os.environ["SSL_CERT_FILE"] = chosen
    os.environ["CURL_CA_BUNDLE"] = chosen

    if chosen == backup:
        return f"{HEALED}：{chosen}", chosen
    return f"{OK}：{chosen}", chosen


def ca_status():
    """给「系统概览」等处展示用的只读诊断信息。"""
    backup = _backup_path()
    live = ""
    try:
        import certifi
        live = certifi.where() or ""
    except Exception:
        pass
    return {
        "live": live,
        "live_valid": _valid(live),
        "backup": backup,
        "backup_valid": _valid(backup),
        "env_bundle": os.environ.get("REQUESTS_CA_BUNDLE", ""),
    }
