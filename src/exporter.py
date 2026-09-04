# -*- coding: utf-8 -*-
"""数据导出：将历史记录（被监控微博、转发记录、日志）导出为 CSV / JSON / XLSX。"""
import csv
import json
import os
import shutil
from datetime import datetime

from history_db import get_history_db
from paths import IMAGE_DIR


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


def export(kind: str, fmt: str, dest_path: str) -> dict:
    """
    kind: monitored | forwards | logs
    fmt:  csv | json | xlsx
    """
    db = get_history_db()
    if kind == "monitored":
        rows = db.get_monitored_posts(limit=5000)
        cols = ["id", "uid", "screen_name", "post_id", "content",
                "images", "created_at", "fetched_at"]
    elif kind == "forwards":
        rows = db.get_forwards(limit=5000)
        cols = ["id", "account_name", "uid", "screen_name", "post_id",
                "original_content", "forward_content", "forward_type",
                "status", "reason", "created_at"]
    elif kind == "logs":
        rows = db.get_logs(limit=5000)
        cols = ["id", "level", "message", "created_at"]
    else:
        return {"ok": False, "error": "未知导出类型"}

    # 图片字段转为可读字符串
    for r in rows:
        if "images" in r and isinstance(r["images"], list):
            r["images"] = ";".join(r["images"])

    if fmt == "json":
        with open(dest_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
    elif fmt == "csv":
        _write_csv(dest_path, rows, cols)
    elif fmt == "xlsx":
        if not _write_xlsx(dest_path, rows, cols):
            return {"ok": False, "error": "未安装 openpyxl，无法导出 XLSX"}
    else:
        return {"ok": False, "error": "不支持的格式"}

    return {"ok": True, "path": dest_path, "count": len(rows)}


def export_filtered(kind: str, fmt: str, dest_path: str, filters: dict) -> dict:
    """按筛选条件导出历史记录（keyword/date/status/ftype/has_images）。"""
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
        cols = ["id", "uid", "screen_name", "post_id", "content",
                "images", "created_at", "fetched_at"]
        if keyword:
            rows = [r for r in rows if keyword in " ".join([
                str(r.get("uid", "")), str(r.get("screen_name", "")),
                str(r.get("content", ""))]).lower()]
        if has_img:
            rows = [r for r in rows if (r.get("images") or [])]
        if date_from or date_to:
            rows = [r for r in rows if _match_date(
                r.get("fetched_at") or r.get("created_at", ""), date_from, date_to)]
    elif kind == "forwards":
        rows = db.get_forwards(limit=5000)
        cols = ["id", "account_name", "uid", "screen_name", "post_id",
                "original_content", "forward_content", "forward_type",
                "status", "reason", "created_at"]
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
    else:
        return {"ok": False, "error": "未知导出类型"}

    for r in rows:
        if "images" in r and isinstance(r["images"], list):
            r["images"] = ";".join(r["images"])

    if fmt == "json":
        with open(dest_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
    elif fmt == "csv":
        _write_csv(dest_path, rows, cols)
    elif fmt == "xlsx":
        if not _write_xlsx(dest_path, rows, cols):
            return {"ok": False, "error": "未安装 openpyxl，无法导出 XLSX"}
    else:
        return {"ok": False, "error": "不支持的格式"}

    return {"ok": True, "path": dest_path, "count": len(rows)}


def export_rows(kind: str, rows: list, fmt: str, dest_path: str) -> dict:
    """导出给定的若干行（由调用方筛选后的结果）。"""
    if kind == "monitored":
        cols = ["id", "uid", "screen_name", "post_id", "content",
                "images", "created_at", "fetched_at"]
    elif kind == "forwards":
        cols = ["id", "account_name", "uid", "screen_name", "post_id",
                "original_content", "forward_content", "forward_type",
                "status", "reason", "created_at"]
    else:
        return {"ok": False, "error": "未知导出类型"}

    for r in rows:
        if "images" in r and isinstance(r["images"], list):
            r["images"] = ";".join(r["images"])

    if fmt == "json":
        with open(dest_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
    elif fmt == "csv":
        _write_csv(dest_path, rows, cols)
    elif fmt == "xlsx":
        if not _write_xlsx(dest_path, rows, cols):
            return {"ok": False, "error": "未安装 openpyxl，无法导出 XLSX"}
    else:
        return {"ok": False, "error": "不支持的格式"}

    return {"ok": True, "path": dest_path, "count": len(rows)}


def export_all(dest_dir: str) -> dict:
    """导出全部数据为独立文件夹：JSON + CSV + 图片副本。"""
    os.makedirs(dest_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.join(dest_dir, f"微博bot导出_{ts}")
    os.makedirs(base, exist_ok=True)

    summary = {}
    for kind in ("monitored", "forwards", "logs"):
        rj = export(kind, "json", os.path.join(base, f"{kind}.json"))
        rc = export(kind, "csv", os.path.join(base, f"{kind}.csv"))
        summary[kind] = {"json": rj.get("count", 0), "csv": rc.get("count", 0)}

    # 复制图片
    img_dest = os.path.join(base, "images")
    copied = 0
    if os.path.isdir(IMAGE_DIR):
        os.makedirs(img_dest, exist_ok=True)
        for fn in os.listdir(IMAGE_DIR):
            try:
                shutil.copy2(os.path.join(IMAGE_DIR, fn), os.path.join(img_dest, fn))
                copied += 1
            except Exception:
                pass

    # 汇总说明
    with open(os.path.join(base, "README.txt"), "w", encoding="utf-8") as f:
        f.write("微博bot小助手 数据导出包\n")
        f.write(f"导出时间：{datetime.now()}\n\n")
        f.write(f"被监控微博记录：{summary['monitored']['json']} 条\n")
        f.write(f"软件转发记录：{summary['forwards']['json']} 条\n")
        f.write(f"操作日志：{summary['logs']['json']} 条\n")
        f.write(f"图片文件：{copied} 张\n")

    return {"ok": True, "path": base, "summary": summary, "images": copied}
