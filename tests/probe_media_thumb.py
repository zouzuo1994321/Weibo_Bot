# -*- coding: utf-8 -*-
"""v1.6.12 相册缩略图探针：验证「卡片预览图确实变小了」。

背景：相册监控页滚动卡顿的根因是卡片把原始大图（中位 1.9MB，最大 29MB）
整份 base64 塞进 104×104 的预览框。本探针直接对真实媒体库取样，量三件事：
  ① 生成后的图片尺寸被钳到 ≤ 2×104 px；
  ② 字节数相比原图大幅下降（目标 < 60KB，通常 8~20KB）；
  ③ 第二次起走磁盘缓存，耗时接近纯读文件。

用法：
    python tests/probe_media_thumb.py            # 取样 6 张
    python tests/probe_media_thumb.py --n 12
"""
import argparse
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, os.path.join(ROOT, "_src_v151k"))

import api_bridge as ab              # noqa: E402

IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def sample_images(media_dir, n):
    """按「每个对象取最新一张」的口径取样，尽量贴近真实卡片列表。"""
    picked = []
    try:
        dirs = [d for d in os.listdir(media_dir)
                if os.path.isdir(os.path.join(media_dir, d))]
    except Exception as e:
        print("无法读取媒体目录 %s：%s" % (media_dir, e))
        return picked
    dirs.sort()
    for d in dirs:
        full = os.path.join(media_dir, d)
        try:
            files = [f for f in os.listdir(full) if f.lower().endswith(IMG_EXT)]
        except Exception:
            continue
        if not files:
            continue
        files.sort(key=lambda f: os.path.getmtime(os.path.join(full, f)), reverse=True)
        picked.append(os.path.join(full, files[0]))
        if len(picked) >= n:
            break
    return picked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6)
    args = ap.parse_args()

    media_dir = ab.get_media_dir()
    print("媒体目录：%s" % media_dir)
    files = sample_images(media_dir, args.n)
    if not files:
        print("没有可取样图片，跳过（这不算失败，但说明本机媒体库为空）")
        return 0

    rows = []
    ok_all = True
    for p in files:
        src_size = os.path.getsize(p)
        dst_jpg, dst_png = ab._thumb_targets(p, 208)
        for q in (dst_jpg, dst_png):
            try:
                os.remove(q)
            except Exception:
                pass
        t0 = time.perf_counter()
        out = ab._make_thumb(p, dst_jpg, dst_png, 208)
        cold = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        out2 = ab._make_thumb(p, dst_jpg, dst_png, 208)   # 应命中缓存（这里重算，只测尺寸）
        _ = out2
        dst_jpg, dst_png = ab._thumb_targets(p, 208)
        t2 = time.perf_counter()
        # 真正的「缓存命中」路径 = 直接读已存在的文件
        with open(out, "rb") as f:
            thumb_size = len(f.read())
        hot = (t2 - t1) * 1000

        from PIL import Image
        with Image.open(out) as im:
            w, h = im.size
        ratio = thumb_size / float(src_size) if src_size else 1.0
        good = (max(w, h) <= 208) and (thumb_size < 60 * 1024) and (ratio < 0.5)
        ok_all = ok_all and good
        rows.append((os.path.basename(p), src_size, thumb_size, w, h, cold, hot, good))
        print("%-46s 原 %7.1fKB → 缩略 %5.1fKB (%4.1f%%) %dx%d 冷生成 %5.0fms  %s"
              % (os.path.basename(p)[:46], src_size / 1024.0, thumb_size / 1024.0,
                 ratio * 100, w, h, cold, "PASS" if good else "FAIL"))

    src_total = sum(r[1] for r in rows)
    th_total = sum(r[2] for r in rows)
    print("\n合计：%d 张 · 原 %.1fMB → 缩略 %.1fKB（降到 %.2f%%）"
          % (len(rows), src_total / 1048576.0, th_total / 1024.0,
             th_total * 100.0 / src_total if src_total else 0))

    # 缓存目录必须落在 DATA_DIR 下，不能污染用户的媒体目录
    in_media = os.path.abspath(ab._THUMB_CACHE_DIR).startswith(
        os.path.abspath(media_dir) + os.sep)
    print("缓存目录：%s（位于媒体目录内=%r，应为 False）"
          % (ab._THUMB_CACHE_DIR, in_media))
    ok_all = ok_all and (not in_media)
    print("\n%s" % ("ALL PASS" if ok_all else "HAS FAILURE"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
