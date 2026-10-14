# -*- coding: utf-8 -*-
"""
路径与全局常量定义。

【v1.1.9j 关键修复】运行时数据目录不再放在 exe 同级（可能位于 NAS / 网络映射盘），
改为放在「本机用户数据目录」（%LOCALAPPDATA%\\微博bot小助手），原因：

  - 实测 Z: 为 REMOTE 网络映射盘（GetDriveTypeW=4），其上：
      · history.db 有 38.6 万行 logs，`COUNT(*)` / 全表扫描在 SMB 上要 0.05~1.3s；
      · app.log 已达 55MB，读取一次要 1s+；
      · 更致命的是**权限被 DENY（只读 ACL）**：所有已存在文件都不可写
        （`icacls data` 显示 `(DENY)(M,DC)`），写入必然失败并抛 PermissionError。
  - 而 pywebview 的 js_bridge_call 会在 WebView2 的 **UI 线程**上同步执行 Python
    方法（get_state / get_logs 等），上述每次慢 I/O 都会把 UI 线程卡住 →
    Windows 判定「未响应」→ 标题栏 / 按钮全部失灵。

把 data 放到本地固态盘后，同样操作耗时降到毫秒级，UI 线程不再被拖住。

兼容策略：
  - 若 exe 同级已有 `data/`（老用户的历史数据），**首次启动自动迁移**到本地目录
    （逐文件复制 + 失败兜底），迁移完成后在本地目录写 `.migrated` 标记；
  - 迁移不动原目录（NAS 只读也动不了），只是不再使用它；
  - 可用环境变量 `WEIBOBOT_DATA_DIR` 覆盖数据目录位置（便于调试 / 便携部署）。
"""
import os
import shutil
import sqlite3
import sys

# 判断是否为 PyInstaller 打包后的运行环境
FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    # exe 所在目录（用于定位随包资源与老数据目录）
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    # 开发环境：项目根目录
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _local_appdata_root():
    """本机用户数据根目录：优先 %LOCALAPPDATA%，回退 %APPDATA%，再回退用户目录。"""
    for var in ("LOCALAPPDATA", "APPDATA"):
        p = os.environ.get(var)
        if p and os.path.isdir(p):
            return p
    return os.path.expanduser("~")


def _resolve_data_dir():
    """决定运行时数据目录。

    优先级：
      1. 环境变量 WEIBOBOT_DATA_DIR（显式指定，最高优先级）
      2. 本机 %LOCALAPPDATA%\\微博bot小助手\\data （默认，本地盘、可写、快）
    """
    env = os.environ.get("WEIBOBOT_DATA_DIR")
    if env:
        return os.path.abspath(env)
    return os.path.join(_local_appdata_root(), "微博bot小助手", "data")


# 运行时数据目录（本地盘）
DATA_DIR = _resolve_data_dir()

# 老数据目录（exe 同级 data/）：仅用于一次性迁移，位于 NAS 时只读也无妨
LEGACY_DATA_DIR = os.path.join(BASE_DIR, "data")

IMAGE_DIR = os.path.join(DATA_DIR, "images")
# 视频 / 相册下载根目录：其下按「微博ID + 昵称」建子目录，再分 video/ 与 album/
MEDIA_DIR = os.path.join(DATA_DIR, "media")
# exe 同级的版本留档目录（放历史版本 exe，与用户数据无关，不可自定义）
# 【v1.2.2 重命名】原先叫 HISTORY_DIR，容易和用户的「历史记录数据目录」混淆 ——
# 后者现在由 get_history_dir() 管理。两者是完全不同的东西。
VERSION_ARCHIVE_DIR = os.path.join(BASE_DIR, "history")
DB_PATH = os.path.join(DATA_DIR, "history.db")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
ACCOUNTS_PATH = os.path.join(DATA_DIR, "accounts.json")
LOG_PATH = os.path.join(DATA_DIR, "app.log")


# ---------------------------------------------------------------------------
# 【v1.2.2】四个「可自定义的大目录」+ 首次启动向导
#
# 用户需求（2026-10-12）：
#   「因为要扫描 /data 和下级的位置，所以会卡。改变逻辑，软件首次启动设置
#    /images 和 /media 和 /models 和 /history 的保存位置。data 中除这三个
#    文件夹外，其他默认保存在 %LOCALAPPDATA%\微博bot小助手\。之后启动
#    /data 就自动匹配这个位置，四个目录随着启动之后在系统概览里展示
#    数据载入进度，在启动后再慢慢读取。」
#
# 落地方式：
#   · DATA_DIR **不动**（授权相关基线，见上方注释），仍是
#     %LOCALAPPDATA%\微博bot小助手\data —— 授权、配置、数据库之外的
#     小文件都在这里；
#   · 把四个「大目录」抽出来独立配置，默认值全部落在 DATA_DIR 下，
#     因此**老用户零迁移**（现有 images/ media/ 原地不动）；
#   · ensure_dirs() 不再预建这四个目录（建目录本身不慢，但会诱使后续
#     代码顺手去遍历它们）——改为**懒创建**：谁要用谁建。
# ---------------------------------------------------------------------------

DEFAULT_IMAGE_DIR = IMAGE_DIR
DEFAULT_MEDIA_DIR = MEDIA_DIR
#历史记录目录：默认与 data 平级，避免动到已有的 history.db（见下方迁移说明）
DEFAULT_HISTORY_DIR = os.path.join(DATA_DIR, "history")
DEFAULT_MODELS_DIR = os.path.join(DATA_DIR, "models")

# 自定义路径的配置文件（独立于 config.json，避免用户清空配置时丢路径）
PATH_CONFIG_NAME = "data_paths.json"

# 四个可配置目录的键名 → 展示名 / 默认值 / 说明
DIR_KEYS = ("image_dir", "media_dir", "models_dir", "history_dir")
DIR_META = {
    "image_dir":   {"name": "监控列表头像", "en": "images",
                    "desc": "监控对象头像缓存"},
    "media_dir":   {"name": "相册 / 视频", "en": "media",
                    "desc": "相册图片与视频下载目录"},
    "models_dir":  {"name": "本地AI 模型", "en": "models",
                    "desc": "llama.cpp 的 GGUF 模型文件"},
    "history_dir": {"name": "历史记录", "en": "history",
                    "desc": "history.db 数据库与导出产物"},
}
DEFAULT_DIR_BY_KEY = {
    "image_dir": DEFAULT_IMAGE_DIR,
    "media_dir": DEFAULT_MEDIA_DIR,
    "models_dir": DEFAULT_MODELS_DIR,
    "history_dir": DEFAULT_HISTORY_DIR,
}


def _paths_cfg_path():
    return os.path.join(DATA_DIR, PATH_CONFIG_NAME)


def _load_path_overrides():
    """读自定义路径覆盖配置。任何异常都退回{} （即全部用默认）。"""
    import json
    p = _paths_cfg_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_path_overrides(d):
    import json
    p = _paths_cfg_path()
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)          # 原子替换，避免半截文件
        return True
    except Exception:
        return False


def _norm_dir(v):
    """规范化用户输入的目录：空/ None → 返回""（表示用默认）。"""
    if not v:
        return ""
    try:
        v = os.path.expandvars(os.path.expanduser(str(v).strip().strip('"')))
        if not v:
            return ""
        return os.path.abspath(v)
    except Exception:
        return ""


def _cur(key):
    """某个目录键的当前生效路径（自定义优先，否则默认）。"""
    v = _norm_dir(_load_path_overrides().get(key))
    return v or DEFAULT_DIR_BY_KEY[key]


def get_image_dir():
    """监控列表头像目录（当前生效值，可被用户自定义）。"""
    return _cur("image_dir")


def get_media_dir():
    """相册 / 视频目录（当前生效值，可被用户自定义）。"""
    return _cur("media_dir")


def get_models_dir():
    """本地AI 模型目录（llama.cpp 的 GGUF 存放处）。"""
    return _cur("models_dir")


def get_history_dir():
    """历史记录目录（history.db 与导出产物所在处）。"""
    return _cur("history_dir")


def get_db_path():
    """history.db 的当前生效绝对路径。

    【v1.2.2】从模块常量改为函数——因为历史记录目录变成可自定义，
    DB 路径必须跟着变；而 history_db 里是 `from paths import DB_PATH`
    的静态导入，拿不到运行期改动。这里返回动态值供调用方使用。
    """
    return os.path.join(get_history_dir(), "history.db")


def rel_to_data(path):
    """把图片等媒体文件的路径规范成「入库用」的形式。

    【v1.2.3 修复 · 跨盘符 ValueError】
    历史记录里的图片一向存**相对 DATA_DIR 的路径**（`images\\xx.jpg`），
    好处是整台机器的 data 目录搬家后数据库照样能读。但 v1.2.2 起
    images / media 可以自定义到**另一个盘符**，此时
    `os.path.relpath(path, DATA_DIR)` 会直接抛

        ValueError: path is on mount 'D:', start on mount 'C:'

    于是用户把 images 指到 D 盘后，每下载一张头像 / 配图就报一次错、
    整条转发记录写不进去（用户实测反馈）。

    本函数的规则：
      · 同一盘符 → 仍返回相对路径（保持原有数据库语义与向后兼容）；
      · 跨盘符 → 返回**绝对路径**。读取侧全都走
        `os.path.join(DATA_DIR, rel)`，而 Windows 的 os.path.join
        在第二个参数为绝对路径时会直接采用它，所以绝对路径天然可读，
        不需要改任何读取端代码。

    注意：这只是「不抛异常、数据不丢」的兜底。真正跨盘可搬性是本
    项目早就接受的不变量（修改路径不自动搬迁已有文件）。
    """
    try:
        p = os.path.abspath(path)
        base = os.path.abspath(DATA_DIR)
        if os.path.splitdrive(p)[0].lower() == os.path.splitdrive(base)[0].lower():
            return os.path.relpath(p, base)
        return p
    except Exception:
        return os.path.abspath(path) if path else ""


# 【v1.2.2】历史记录目录迁移：把老DATA_DIR/history.db 挪到新的 history 目录。
# 只在「用户把 history 目录改了、且老库还在、目标还没有」时执行一次，
# 成功后写 .migrated 标记；失败则保留老库并在返回里说明（不阻断启动）。
def migrate_history_db():
    """把 history.db 迁移到当前生效的历史记录目录。返回 (是否动了, 说明)。"""
    target_dir = get_history_dir()
    target = os.path.join(target_dir, "history.db")
    old = os.path.join(DATA_DIR, "history.db")
    if os.path.abspath(target_dir) == os.path.abspath(DATA_DIR):
        return False, "历史记录目录未改动"
    if not os.path.exists(old):
        return False, "老库不存在，无需迁移"
    if os.path.exists(target):
        return False, "目标已存在 history.db，保持不动"
    moved = []
    try:
        os.makedirs(target_dir, exist_ok=True)
        for name in ("history.db", "history.db-wal", "history.db-shm"):
            src = os.path.join(DATA_DIR, name)
            if os.path.exists(src):
                shutil.move(src, os.path.join(target_dir, name))
                moved.append(name)
        # 老库遗留的导出目录一并挪走
        for name in ("exports", "export"):
            src = os.path.join(DATA_DIR, name)
            if os.path.isdir(src):
                dst = os.path.join(target_dir, name)
                if not os.path.exists(dst):
                    shutil.move(src, dst)
                    moved.append(name + "/")
    except Exception as e:
        # 回滚已挪走的文件，避免库被切成两半
        for name in moved:
            try:
                src = os.path.join(target_dir, name.rstrip("/"))
                if os.path.exists(src):
                    shutil.move(src, os.path.join(DATA_DIR, name.rstrip("/")))
            except Exception:
                pass
        return False, "迁移失败已回滚：%s" % e
    return True, "已迁移：%s" % "、".join(moved)


def wizard_state():
    """首次启动向导状态。

    完成条件：data_paths.json 里存在 wizard_done 标记。
    【v1.2.2】v1.2.1 的老用户已经手动配过image/media，直接视为已完成，
    不再弹向导打扰他们。
    """
    ov = _load_path_overrides()
    done = bool(ov.get("wizard_done"))
    legacy_ok = bool(ov.get("image_dir") or ov.get("media_dir"))
    return {
        "done": bool(done or legacy_ok),
        "legacy": legacy_ok,
        "data_dir": DATA_DIR,
        "app_root": os.path.dirname(DATA_DIR),
        "dirs": [
            {"key": k,
             "name": DIR_META[k]["name"],
             "en": DIR_META[k]["en"],
             "desc": DIR_META[k]["desc"],
             "value": _cur(k),
             "default": DEFAULT_DIR_BY_KEY[k],
             "custom": bool(_norm_dir(ov.get(k)))}
            for k in DIR_KEYS
        ],
    }


def get_path_settings():
    """给前端「存储管理」用的完整状态（v1.2.2 扩到四个目录）。"""
    ov = _load_path_overrides()
    out = {
        "data_dir": DATA_DIR,
        "app_root": os.path.dirname(DATA_DIR),
        "data_dir_locked": True,          # 授权相关信息固定，不可改
        "locked_files": ["license.json", "trial.json", ".acc_key",
                         "config.json", "ai_settings.json", "app.log"],
        "locked_reason": ("软件授权与运行配置固定存放于本目录，用于保证授权校验"
                          "基线稳定、不被随意搬动；只有下方四个大目录可自定义。"),
        "wizard_done": bool(ov.get("wizard_done") or ov.get("image_dir")
                            or ov.get("media_dir")),
    }
    for k in DIR_KEYS:
        out[k] = _cur(k)
        out[k + "_default"] = DEFAULT_DIR_BY_KEY[k]
        out[k + "_custom"] = bool(_norm_dir(ov.get(k)))
    # 兼容 v1.2.1 前端字段
    out["image_dir"] = get_image_dir()
    out["media_dir"] = get_media_dir()
    return out


def _is_inside(child, parent):
    """child 是否在 parent 之内（含相等）。用于阻止四个目录互相嵌套。"""
    try:
        c = os.path.abspath(child).rstrip("\\/").lower()
        p = os.path.abspath(parent).rstrip("\\/").lower()
        return c == p or c.startswith(p + os.sep)
    except Exception:
        return False


def _probe_dir(path, need_bytes=8 * 1024 * 1024):
    """探测目录是否可用：能否创建、能否写入 8KB、剩余空间是否够用。"""
    res = {"ok": False, "reason": "", "free_bytes": -1, "exists": False}
    if not path:
        res["reason"] = "路径为空"
        return res
    try:
        exists = os.path.isdir(path)
        res["exists"] = exists
        if not exists:
            os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".wb_write_test")
        with open(probe, "wb") as f:
            f.write(b"0" * need_bytes)
        try:
            os.remove(probe)
        except Exception:
            pass
        try:
            import shutil
            res["free_bytes"] = shutil.disk_usage(path).free
        except Exception:
            res["free_bytes"] = -1
        res["ok"] = True
        res["reason"] = "可写"
    except Exception as e:
        res["reason"] = f"不可用：{e}"
    return res


def set_data_paths(image_dir=None, media_dir=None, models_dir=None,
                   history_dir=None, wizard_done=None):
    """设置自定义路径。返回 {ok, error, settings}。

    语义（沿用 v1.2.0）：
      · 传 None 表示「该项不改」；
      · 传 "" 表示「该项恢复默认」（从配置里删掉该项）。

    【v1.2.2】扩到四个目录，并新增 wizard_done 标记。改动 history_dir 时
    会自动迁移已有的 history.db（见 migrate_history_db）。
    """
    ov = dict(_load_path_overrides())
    changed = []
    errors = []

    pairs = (("image_dir", image_dir), ("media_dir", media_dir),
             ("models_dir", models_dir), ("history_dir", history_dir))
    for key, val in pairs:
        if val is None:
            continue
        meta = DIR_META[key]
        if val == "":
            if ov.get(key):
                ov.pop(key, None)
                changed.append("%s 已恢复默认" % meta["name"])
            continue
        norm = _norm_dir(val)
        if not norm:
            errors.append("%s 路径无效" % meta["name"])
            continue
        # 四个目录不能互相嵌套（否则清理 A 时会连带删掉 B 的内容）
        clash = ""
        for other in DIR_KEYS:
            if other == key:
                continue
            o = _norm_dir(ov.get(other)) or DEFAULT_DIR_BY_KEY[other]
            if _is_inside(norm, o) or _is_inside(o, norm):
                other_name = DIR_META[other]["name"]
                clash = ("%s 与%s（%s）路径互相包含，"
                         "清理其中一个会误删另一个的内容"
                         % (meta["name"], other_name, o))
                break
        if clash:
            errors.append(clash)
            continue
        # 落盘前先探测，避免用户填了个不可写的路径后程序静默丢数据
        probe = _probe_dir(norm)
        if not probe["ok"]:
            errors.append("%s → %s %s" % (meta["name"], norm, probe["reason"]))
            continue
        if probe["free_bytes"] >= 0 and probe["free_bytes"] < 512 * 1024 * 1024:
            errors.append("%s → %s 所在磁盘剩余空间仅 %dMB，建议至少留 512MB"
                          % (meta["name"], norm, probe["free_bytes"] // 1048576))
            continue
        if ov.get(key) != norm:
            ov[key] = norm
            changed.append("%s → %s" % (meta["name"], norm))

    if wizard_done:
        ov["wizard_done"] = True

    if errors and not changed and not wizard_done:
        # 全部失败，不写文件
        return {"ok": False, "error": "；".join(errors), "settings": get_path_settings()}

    if changed or wizard_done:
        if not _save_path_overrides(ov):
            return {"ok": False, "error": "写入 data_paths.json 失败",
                    "settings": get_path_settings()}

    # 立即建目录，保证新数据马上有落点
    for k in DIR_KEYS:
        try:
            os.makedirs(_cur(k), exist_ok=True)
        except Exception:
            pass

    # 历史记录目录变了 → 把老库挪过去
    note = ""
    if any(c.startswith("历史记录") for c in changed):
        try:
            moved, why = migrate_history_db()
            note = why
        except Exception as e:
            note = "历史库迁移异常：%s" % e

    return {
        "ok": not errors,
        "error": "；".join(errors) if errors else "",
        "changed": changed,
        "note": note,
        "restart_required": bool(note.startswith("已迁移")),
        "settings": get_path_settings(),
    }


# 大版本号
# 【v1.2.1 修正】此前一直停在 "v1.1.9k"，而 version.py 的 MAJOR 早已迭代到 v1.2.x，
# 导致 api_bridge 暴露的 major_version（界面顶部显示）与 version.get_version() 不一致。
# 单一事实源以 version.py 为准，这里与其保持同步。
MAJOR_VERSION = "v1.3.2"

# 应用元信息
APP_NAME = "微博bot小助手"
COPYRIGHT = "Copyright  2026 肆月Aperture"
COPYRIGHT_FULL = (
    "Copyright  2026 肆月Aperture\n"
    "本软件为开源软件，未经授权禁止用于任何商业用途。"
)


def _copy_file(src, dst):
    """流式复制单文件（1MB 块）+ **校验字节数**，避免「复制了一半当成功」。

    之前踩过的坑：NAS 上 69MB history.db 复制中途失败/被截断，但代码没校验，
    于是本地留下一个 4KB 的残缺库，后续 SQLite 打开就报错。
    现在复制完比对大小，不一致则删除目标并抛错。
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    want = os.path.getsize(src)
    tmp = dst + ".part"
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                chunk = fi.read(1024 * 1024)
                if not chunk:
                    break
                fo.write(chunk)
            fo.flush()
            os.fsync(fo.fileno())
        got = os.path.getsize(tmp)
        if got != want:
            raise IOError("size mismatch src=%d got=%d" % (want, got))
        os.replace(tmp, dst)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise


def _copy_tree(src, dst):
    """递归复制目录（不用 shutil.copytree，前者对部分失败更宽容）。"""
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s):
            _copy_tree(s, d)
        else:
            if os.path.exists(d):
                continue
            _copy_file(s, d)


# 迁移中的文件清单：只迁移这些关键数据文件，避免把巨大的 images/media 目录
# 也拖进来（那会让首次启动等上几分钟）。图片/媒体可按需后续补迁。
_MIGRATE_FILES = (
    "config.json", "accounts.json", "monitor.json",
    "license.json", "trial.json", ".acc_key",
    "cacert.pem",     # 【v1.1.9k0011】CA 证书包备份：漏了它会导致 HTTPS 全挂（见 ca_bundle.py）
    "history.db",
    "history",     # 目录（若存在）
)


def _copy_db_online(src, dst):
    """用 SQLite 在线备份 API 把 src 整库复制为**新文件** dst。

    注意：此函数仅在**目标文件未被占用**时可用（例如迁移早于 Api() 初始化）。
    运行时 Api() 已持有 DATA_DIR/history.db 连接的情况下，请改用
    `history_db.get_history_db().import_from_file(src)` —— 它以当前连接为目标，
    不碰文件名，故不受占用影响。
    """
    tmp = dst + ".btmp"
    for p in (tmp, tmp + "-wal", tmp + "-shm"):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
    src_con = sqlite3.connect("file:%s?mode=ro" % src.replace("\\", "/"), uri=True,
                              timeout=30)
    dst_con = sqlite3.connect(tmp, timeout=30)
    try:
        src_con.backup(dst_con)          # 一致快照写入新文件
        dst_con.commit()
    finally:
        try:
            src_con.close()
        except Exception:
            pass
        try:
            dst_con.close()
        except Exception:
            pass
    os.replace(tmp, dst)
    return os.path.getsize(dst)


def _count_rows(path):
    """读某个 SQLite 文件的某表行数（只读，失败返回 -1）。"""
    try:
        c = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                            uri=True, timeout=10)
        try:
            n = c.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        finally:
            c.close()
        return n
    except Exception:
        return -1


def import_legacy_db_via(importer):
    """把老库导入到**调用方提供的活跃连接**上（importer(src_path) → dict）。

    这是运行时唯一可靠的迁移方式：本地 history.db 已被 Api() 的连接独占，
    文件级操作（remove/replace）必然失败，只能由目标连接自己做在线备份。

    返回 (成功?, 摘要或错误字符串)。
    """
    marker = os.path.join(DATA_DIR, ".migrated_db")
    if os.path.exists(marker):
        return True, "already"
    src = os.path.join(LEGACY_DATA_DIR, "history.db")
    if not os.path.exists(src):
        return True, "no-legacy-db"
    if os.path.abspath(LEGACY_DATA_DIR) == os.path.abspath(DATA_DIR):
        return True, "same-dir"
    try:
        summary = importer(src)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write("db imported from: %s\nrows=%r\n" % (src, summary))
    except Exception:
        pass
    return True, summary


def _migrate_legacy_data():
    """把 exe 同级老 data/ 里的关键数据迁移到本地 DATA_DIR（仅首次，逐文件兜底）。

    设计要点：
      - 老目录不可写（NAS DENY ACL）也没关系：只读复制即可；
      - **复制后校验字节数**，失败不留下残缺文件；
      - 已存在的同名文件**不覆盖**（本地更新优先）；
      - 单个文件失败不中断整体；
      - 全部成功后才写 `.migrated` 标记；有失败则下次启动重试。
    """
    marker = os.path.join(DATA_DIR, ".migrated")
    if os.path.exists(marker):
        return []
    if os.path.abspath(LEGACY_DATA_DIR) == os.path.abspath(DATA_DIR):
        return []
    if not os.path.isdir(LEGACY_DATA_DIR):
        return []

    failed = []
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        return ["<makedirs>"]

    for name in _MIGRATE_FILES:
        src = os.path.join(LEGACY_DATA_DIR, name)
        dst = os.path.join(DATA_DIR, name)
        if not os.path.exists(src):
            continue
        # history.db 不做文件级迁移：本地库被 Api() 的连接独占，文件级
        # remove/replace 必定失败（WinError 32/5）。改由启动流程调用
        # `import_legacy_db_via()` → HistoryDB.import_from_file() 在线备份导入。
        if name == "history.db":
            continue
        if os.path.exists(dst):
            continue
        try:
            if os.path.isdir(src):
                _copy_tree(src, dst)
            else:
                _copy_file(src, dst)
        except Exception as e:
            failed.append(name + "(%s: %s)" % (type(e).__name__, e))

    if not failed:
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write("migrated from: %s\n" % LEGACY_DATA_DIR)
        except Exception:
            pass
    return failed


def ensure_dirs():
    """确保运行时所需目录存在（本地盘）。

    【v1.2.2 关键改动 —— 启动未响应的根因】
    旧版在这里 makedirs 了 images / media / history 三个大目录。它们本身
    建目录很快，但会带来两个真实问题：
      1. 首次运行时这三个目录是空的，随后各处代码（数据路径占用统计、
         相册对象枚举、模型探测…）会立刻 os.walk 遍历它们；
      2. 更要命的是——用户如果把它们指到NAS / 网络盘 / 机械盘上，
         任何一次遍历都是秒级阻塞，而pywebview 的 js_bridge_call 是在
         WebView2 的 **UI 线程**上同步执行的 → Windows 判定「未响应」。

    现在改为：
      · 这里只建DATA_DIR（小目录，纯本地，几乎零成本）；
      · 四个大目录一律**懒创建**（谁写谁建，见 ensure_dir(key)）；
      · 占用统计一律走 storage.py 的后台线程，绝不在 UI 线程上遍历。

    注意：老数据迁移**不在这里做**（69MB history.db 在 SMB 上复制要十几秒，
    放在启动路径会阻塞首屏）。改为由入口在窗口显示后起后台线程执行，
    见 start_legacy_migration()。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        os.makedirs(VERSION_ARCHIVE_DIR, exist_ok=True)
    except Exception:
        # 版本留档目录不可写（只读介质）不应阻断启动
        pass


def ensure_dir(key):
    """懒创建某个大目录。key ∈ DIR_KEYS。由真正要写入的调用点调用。"""
    if key not in DIR_KEYS:
        return ""
    try:
        d = _cur(key)
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return _cur(key)


def ensure_all_user_dirs():
    """后台线程里把四个大目录都建好（供启动后的预热线程调用）。"""
    made = []
    for k in DIR_KEYS:
        d = ensure_dir(k)
        if d:
            made.append(d)
    return made


def start_legacy_migration():
    """在后台线程执行老数据迁移（不阻塞 UI 启动）。返回线程对象。

    两步：
      1. 文件级迁移（json / .acc_key / history 目录）—— 走 `_migrate_legacy_data`；
      2. 数据库迁移 —— 因本地 history.db 已被 Api() 的连接独占，文件级操作不可行，
         改调 `history_db.get_history_db().import_from_file(老库)` 做在线备份导入。

    每步结果写进 DATA_DIR/.migrate_report.txt，便于排查「迁移没生效」。
    """
    import threading
    import traceback

    def _report(text):
        try:
            with open(os.path.join(DATA_DIR, ".migrate_report.txt"),
                      "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass

    def _work():
        import time as _t
        _report("== start %s ==" % _t.strftime("%H:%M:%S"))
        _report("DATA_DIR   = %s" % DATA_DIR)
        _report("LEGACY_DIR = %s" % LEGACY_DATA_DIR)
        _report("legacy exists = %s" % os.path.isdir(LEGACY_DATA_DIR))
        # 1) 文件级
        try:
            failed = _migrate_legacy_data()
            _report("files failed = %r" % (failed,))
        except Exception:
            _report("FILES EXC:\n" + traceback.format_exc())
        # 2) 数据库（在线备份导入）
        try:
            def _importer(src):
                from history_db import get_history_db
                return get_history_db().import_from_file(src)
            ok, info = import_legacy_db_via(_importer)
            _report("db import ok=%s info=%r" % (ok, info))
        except Exception:
            _report("DB EXC:\n" + traceback.format_exc())
        try:
            rows = _count_rows(os.path.join(DATA_DIR, "history.db"))
            _report("local logs rows = %d" % rows)
        except Exception:
            pass

    t = threading.Thread(target=_work, daemon=True, name="legacy-migrate")
    t.start()
    return t
