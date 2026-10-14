# -*- coding: utf-8 -*-
"""数据导出：将历史记录（被监控微博、转发记录、日志）导出为 CSV / JSON / XLSX。

导出形态统一为**导出文件夹**，内部包含：

    微博bot导出_xxx_时间戳/
        monitored.csv     # 数据文件（csv / json / xlsx 由用户选择）
        images/           # 数据里引用到的图片副本（保持相对结构）
        README.txt        # 导出说明与统计

即「导出选中 / 导出当前筛选 / 导出全部」三种方式**都包含图片**，不再只有
「导出全部」单独带图。图片只复制本次导出记录实际引用的那部分，避免整库拷贝。
"""
import csv
import json
import os
import shutil
from datetime import datetime

from history_db import get_history_db
# 【v1.2.0】IMAGE_DIR → get_image_dir()
from paths import BASE_DIR, DATA_DIR, get_image_dir

_SUFFIX = {"csv": ".csv", "json": ".json", "xlsx": ".xlsx"}

COLS = {
    "monitored": ["id", "uid", "screen_name", "post_id", "content",
                  "images", "created_at", "fetched_at"],
    "forwards": ["id", "account_name", "uid", "screen_name", "post_id",
                 "original_content", "forward_content", "forward_type",
                 "status", "reason", "created_at"],
    "logs": ["id", "level", "message", "created_at"],
}


# ---------------- 基础写文件 ----------------
def _write_csv(path, rows, columns):
    with open(path, "w", encoding="utf-8-sig", newline="", errors="ignore") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _write_xlsx(path, rows, columns):
    try:
        from openpyxl import Workbook
    except Exception:
        return False
    wb = Workbook()
    ws = wb.active
    ws.append(columns)
    for r in rows:
        ws.append([r.get(c, "") for c in columns])
    wb.save(path)
    return True


def _write_payload(rows, columns, fmt, dest_path):
    """按格式写出数据文件。

    JSON 保留 images 数组（结构化更好用）；CSV / XLSX 以分号连接为字符串。
    """
    if fmt == "json":
        with open(dest_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        return {"ok": True}
    flat = []
    for r in rows:
        r = dict(r)
        if isinstance(r.get("images"), list):
            r["images"] = ";".join(r["images"])
        flat.append(r)
    if fmt == "csv":
        _write_csv(dest_path, flat, columns)
        return {"ok": True}
    if fmt == "xlsx":
        if not _write_xlsx(dest_path, flat, columns):
            return {"ok": False, "error": "未安装 openpyxl，无法导出 XLSX"}
        return {"ok": True}
    return {"ok": False, "error": "不支持的格式"}


# ---------------- 图片 ----------------
def _resolve_image_abs(rel):
    """把图片相对路径解析为本机绝对路径（兼容旧版 data\\images\\...）。"""
    if not rel:
        return ""
    rel = os.path.normpath(str(rel).replace("/", os.sep))
    # 【v1.2.3】跨盘写入的绝对路径：直接采用（os.path.join 本来也会这么做，
    # 这里显式写出来是为了让兜底逻辑不被误触发）
    if os.path.isabs(rel):
        return rel if os.path.isfile(rel) else ""
    if rel.lower().startswith("data" + os.sep):
        rel = rel[len("data" + os.sep):]
    for base in (DATA_DIR, BASE_DIR):
        p = os.path.join(base, rel)
        if os.path.isfile(p):
            return p
    # 兜底：按文件名直接在图片目录找
    p = os.path.join(get_image_dir(), os.path.basename(rel))
    return p if os.path.isfile(p) else ""


def _collect_image_rels(rows):
    """收集记录里实际引用的图片相对路径（配图 + 头像），去重且保持顺序。"""
    out, seen = [], set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        for p in (r.get("images") or []):
            if p and p not in seen:
                seen.add(p)
                out.append(p)
        av = r.get("avatar_local")
        if av and av not in seen:
            seen.add(av)
            out.append(av)
    return out


def _copy_images(rels, dest_dir):
    """按原相对结构把图片复制到 dest_dir，返回 (成功数, 缺失数)。"""
    if not rels:
        return 0, 0
    os.makedirs(dest_dir, exist_ok=True)
    copied = missing = 0
    for rel in rels:
        src = _resolve_image_abs(rel)
        if not src:
            missing += 1
            continue
        rel_norm = os.path.normpath(str(rel).replace("/", os.sep))
        if os.path.isabs(rel_norm):
            # 【v1.2.3】跨盘写入的绝对路径：只取文件名，避免把整个 D:\
            # 层级搬进导出目录
            rel_norm = os.path.basename(rel_norm)
        else:
            if rel_norm.lower().startswith("data" + os.sep):
                rel_norm = rel_norm[len("data" + os.sep):]
            if rel_norm.lower().startswith("images" + os.sep):
                rel_norm = rel_norm[len("images") + 1:]
        dst = os.path.join(dest_dir, rel_norm)
        try:
            os.makedirs(os.path.dirname(dst) or dest_dir, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        except Exception:
            missing += 1
    return copied, missing


def _write_readme(base, title, count, images, missing, files):
    lines = [
        "微博bot小助手 数据导出包",
        f"导出内容：{title}",
        f"导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"记录条数：{count} 条",
        f"附带图片：{images} 张" + (f"（{missing} 张源文件缺失已跳过）" if missing else ""),
        "",
        "目录说明：",
    ]
    for fn in files:
        lines.append(f"  - {fn}")
    if images:
        lines.append("  - images/  本次导出记录所引用的图片副本（按相对路径结构存放）")
    with open(os.path.join(base, "README.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _bundle(kind, rows, fmt, dest_dir, folder_name, title):
    """生成「数据文件 + 图片副本 + 说明」的导出文件夹。"""
    base = os.path.join(dest_dir, folder_name)
    os.makedirs(base, exist_ok=True)
    suffix = _SUFFIX.get(fmt, ".txt")
    data_path = os.path.join(base, f"{kind}{suffix}")
    res = _write_payload(rows, COLS[kind], fmt, data_path)
    if not res.get("ok"):
        return res
    copied = missing = 0
    if kind == "monitored":
        copied, missing = _copy_images(_collect_image_rels(rows),
                                       os.path.join(base, "images"))
    _write_readme(base, title, len(rows), copied, missing,
                  [os.path.basename(data_path)])
    return {"ok": True, "path": base, "file": data_path, "count": len(rows),
            "images": copied, "missing": missing}


# ---------------- 对外接口 ----------------
def export(kind: str, fmt: str, dest_dir: str) -> dict:
    """导出某一类全部记录（文件夹形态，含图片）。"""
    if kind not in COLS:
        return {"ok": False, "error": "未知导出类型"}
    db = get_history_db()
    if kind == "monitored":
        rows = db.get_monitored_posts(limit=5000)
    elif kind == "forwards":
        rows = db.get_forwards(limit=5000)
    else:
        rows = db.get_logs(limit=5000)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    title = {"monitored": "被监控微博", "forwards": "转发记录", "logs": "运行日志"}[kind]
    return _bundle(kind, rows, fmt, dest_dir, f"微博bot导出_{title}_{ts}", title)


def export_filtered(kind: str, fmt: str, dest_dir: str, filters: dict) -> dict:
    """按筛选条件导出历史记录（keyword/date/status/ftype/has_images）。"""
    if kind not in ("monitored", "forwards"):
        return {"ok": False, "error": "未知导出类型"}
    db = get_history_db()
    keyword = (filters.get("keyword") or "").strip().lower()
    date_from = filters.get("dateFrom") or ""
    date_to = filters.get("dateTo") or ""
    status = filters.get("status") or ""
    ftype = filters.get("ftype") or ""
    has_img = filters.get("hasImg", False)

    def _match_date(date_str, from_str, to_str):
        if not date_str:
            return True
        ds = date_str[:10]
        if from_str and ds < from_str:
            return False
        if to_str and ds > to_str:
            return False
        return True

    if kind == "monitored":
        rows = db.get_monitored_posts(limit=5000)
        if keyword:
            rows = [r for r in rows if keyword in " ".join([
                str(r.get("uid", "")), str(r.get("screen_name", "")),
                str(r.get("content", ""))]).lower()]
        if has_img:
            rows = [r for r in rows if (r.get("images") or [])]
        if date_from or date_to:
            rows = [r for r in rows if _match_date(
                r.get("fetched_at") or r.get("created_at", ""), date_from, date_to)]
    else:
        rows = db.get_forwards(limit=5000)
        if keyword:
            rows = [r for r in rows if keyword in " ".join([
                str(r.get("account_name", "")), str(r.get("screen_name", "")),
                str(r.get("uid", "")), str(r.get("original_content", "")),
                str(r.get("forward_content", ""))]).lower()]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        if ftype:
            rows = [r for r in rows if r.get("forward_type") == ftype]
        if date_from or date_to:
            rows = [r for r in rows if _match_date(
                r.get("created_at", ""), date_from, date_to)]

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    kind_cn = "被监控微博" if kind == "monitored" else "转发记录"
    return _bundle(kind, rows, fmt, dest_dir, f"微博bot导出_筛选_{ts}",
                  f"当前筛选结果（{kind_cn}）")


def export_rows(kind: str, rows: list, fmt: str, dest_dir: str) -> dict:
    """导出给定的若干行（导出选中）。"""
    if kind not in ("monitored", "forwards"):
        return {"ok": False, "error": "未知导出类型"}
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    kind_cn = "被监控微博" if kind == "monitored" else "转发记录"
    return _bundle(kind, rows, fmt, dest_dir, f"微博bot导出_选中_{ts}",
                  f"选中记录（{kind_cn}）")


def export_all(dest_dir: str) -> dict:
    """导出全部数据为独立文件夹：JSON + CSV + 图片副本。"""
    os.makedirs(dest_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.join(dest_dir, f"微博bot导出_全部_{ts}")
    os.makedirs(base, exist_ok=True)

    db = get_history_db()
    summary = {}
    files = []
    monitored_rows = []
    for kind in ("monitored", "forwards", "logs"):
        if kind == "monitored":
            rows = db.get_monitored_posts(limit=5000)
            monitored_rows = rows
        elif kind == "forwards":
            rows = db.get_forwards(limit=5000)
        else:
            rows = db.get_logs(limit=5000)
        rj = _write_payload(rows, COLS[kind], "json", os.path.join(base, f"{kind}.json"))
        rc = _write_payload(rows, COLS[kind], "csv", os.path.join(base, f"{kind}.csv"))
        summary[kind] = {"json": len(rows) if rj.get("ok") else 0,
                         "csv": len(rows) if rc.get("ok") else 0}
        files += [f"{kind}.json", f"{kind}.csv"]

    # 复制本次导出记录实际引用的图片
    copied, missing = _copy_images(_collect_image_rels(monitored_rows),
                                   os.path.join(base, "images"))

    total = summary["monitored"]["json"] + summary["forwards"]["json"] + summary["logs"]["json"]
    with open(os.path.join(base, "README.txt"), "w", encoding="utf-8") as f:
        f.write("微博bot小助手 数据导出包\n")
        f.write(f"导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(f"被监控微博记录：{summary['monitored']['json']} 条\n")
        f.write(f"软件转发记录：{summary['forwards']['json']} 条\n")
        f.write(f"操作日志：{summary['logs']['json']} 条\n")
        f.write(f"图片文件：{copied} 张"
                + (f"（{missing} 张源文件缺失已跳过）" if missing else "") + "\n")
        f.write("\n目录说明：\n")
        for fn in files:
            f.write(f"  - {fn}\n")
        if copied:
            f.write("  - images/  被监控微博所引用的图片副本（按相对路径结构存放）\n")

    return {"ok": True, "path": base, "summary": summary,
            "images": copied, "missing": missing, "count": total}
