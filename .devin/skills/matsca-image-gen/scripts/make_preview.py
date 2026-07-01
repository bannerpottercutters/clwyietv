#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把生成的大图压成回传用的小 JPEG（原图保留）。

后端优先 Pillow，回退 ImageMagick(convert/magick)；两者都没有则报错。
可作为库被 auto_deliver.py 导入（make_preview / target_size），也可直接 CLI 调用。
"""

import argparse
import os
import shutil
import subprocess
import sys


def target_size(w, h, max_px):
    m = max(w, h)
    if m <= max_px:
        return w, h
    s = max_px / float(m)
    return max(1, round(w * s)), max(1, round(h * s))


def _with_pillow(src, dst, max_px, quality):
    try:
        from PIL import Image
    except ImportError:
        return False
    im = Image.open(src).convert("RGB")
    w, h = target_size(im.size[0], im.size[1], max_px)
    if (w, h) != im.size:
        im = im.resize((w, h), Image.LANCZOS)
    im.save(dst, "JPEG", quality=quality, optimize=True)
    return True


def _with_convert(src, dst, max_px, quality):
    exe = shutil.which("convert") or shutil.which("magick")
    if not exe:
        return False
    cmd = [exe, src, "-resize", "%dx%d>" % (max_px, max_px),
           "-quality", str(quality), dst]
    return subprocess.run(cmd).returncode == 0


def make_preview(src, dst, max_px=900, quality=82):
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    if _with_pillow(src, dst, max_px, quality):
        return dst
    if _with_convert(src, dst, max_px, quality):
        return dst
    raise RuntimeError("需要 Pillow 或 ImageMagick(convert) 才能压缩预览；两者都没装")


IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")


def iter_images(paths):
    for p in paths:
        if os.path.isdir(p):
            for n in sorted(os.listdir(p)):
                if n.lower().endswith(IMG_EXT):
                    yield os.path.join(p, n)
        elif p.lower().endswith(IMG_EXT):
            yield p


def main():
    ap = argparse.ArgumentParser(description="生成回传用的压缩 JPEG 预览（原图保留）")
    ap.add_argument("paths", nargs="+", help="图片文件或目录（目录则取其中所有图）")
    ap.add_argument("--outdir", default=None, help="预览输出目录（默认各图同级 ./preview/）")
    ap.add_argument("--max-px", dest="max_px", type=int, default=900, help="最长边像素上限")
    ap.add_argument("--quality", type=int, default=82, help="JPEG 质量 1-95")
    a = ap.parse_args()
    n = 0
    for src in iter_images(a.paths):
        outdir = a.outdir or os.path.join(os.path.dirname(src) or ".", "preview")
        base = os.path.splitext(os.path.basename(src))[0] + ".jpg"
        dst = os.path.join(outdir, base)
        make_preview(src, dst, a.max_px, a.quality)
        print("%s -> %s (%dKB)" % (src, dst, os.path.getsize(dst) // 1024))
        n += 1
    if not n:
        sys.exit("没有可处理的图片")


if __name__ == "__main__":
    main()
