# -*- coding: utf-8 -*-
"""CA 证书包自愈（PyInstaller onefile + certifi）。

问题现象（2026-09-14 实测日志）：
    拉取微博失败 uid=... · 判定=network_error · 已尝试 3 种接口 · 原因：
    mymblog exc: Could not find a suitable TLS CA certificate bundle,
    invalid path: C:\\Users\\ADMINI~1\\AppData\\Local\\Temp\\_MEI000066942\\certifi\\cacert.pem
    —— 三种接口、相册、账户检测**同时**全部以 network_error 失败，重启程序后立刻恢复正常。

根因：PyInstaller onefile 会把 certifi 的 cacert.pem 解压到 `%TEMP%\\_MEIxxxxxx\\certifi\\`。
（已核实 cacert.pem 确实打进了 exe，不是打包缺失。）一旦该临时目录在程序运行期间
被 Windows 存储感知 / 磁盘清理 / 杀毒软件 / CCleaner 之类工具清掉，requests 就会对
**每一个** HTTPS 请求抛 OSError（requests/adapters.py 的 cert_verify 校验路径存在性），
表现为全站网络异常；重启会解压出新的 _MEI 目录，所以「重启即好」。

对策（本模块）：
  1) 启动正常时把 certifi 的证书包备份到 data/cacert.pem（只写一次，几 KB）；
  2) 运行时若发现 certifi 的证书包已失效，改用该备份，并写入
     REQUESTS_CA_BUNDLE / SSL_CERT_FILE / CURL_CA_BUNDLE
     （requests 每次请求都会读这些环境变量，因此无需改动业务代码）。
"""
import os
import shutil

_BACKUP_NAME = "cacert.pem"


def _backup_path():
    try:
        from paths import DATA_DIR
        return os.path.join(DATA_DIR, _BACKUP_NAME)
    except Exception:
        return ""


def ensure_ca_bundle():
    """确保 requests 能找到可用的 CA 证书包。

    返回 (状态, 证书路径)：
      ok                     —— certifi 自带证书包可用（顺带完成备份）
      已改用备份证书包        —— certifi 路径已失效，成功切换到备份
      证书包缺失且无备份      —— 无法自愈，HTTPS 仍会失败
    """
    try:
        import certifi
        live = certifi.where()
    except Exception as e:
        return f"certifi 不可用：{e}", ""

    backup = _backup_path()

    if live and os.path.exists(live):
        # 正常：顺手做一份备份，供后续自愈使用
        if backup and not os.path.exists(backup):
            try:
                shutil.copyfile(live, backup)
            except Exception:
                pass
        return "ok", live

    # certifi 的证书包不见了（临时目录被清理）→ 改用备份
    if backup and os.path.exists(backup):
        os.environ["REQUESTS_CA_BUNDLE"] = backup
        os.environ["SSL_CERT_FILE"] = backup
        os.environ["CURL_CA_BUNDLE"] = backup
        return f"已改用备份证书包（原路径失效：{live}）", backup

    return f"证书包缺失且无备份：{live}", ""
