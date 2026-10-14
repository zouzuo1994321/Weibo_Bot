# -*- coding: utf-8 -*-
"""微博bot小助手 v1.5.0 构建 + 三处归档脚本。

行为（与 build_v119k_2610140002.log 记录一致）：
  1. 读 _src_v151k/version.json → 内部版本号 internal = date + %04d seq
  2. CHANGELOG_SYNC：把 internal 同步进 _ui_v151k/app.js 的 CHANGELOG[0]（幂等，CRLF 安全）
  3. 写 spec_v151k_m/微博bot小助手.spec
  4. PyInstaller 全量构建（--clean）→ dist_v151k_m/微博bot小助手.exe
  5. ARCHIVE：复制到根目录与 history/微博bot小助手_<internal>.exe，三处 sha256 一致断言
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
VERSION_JSON = os.path.join(ROOT, "_src_v151k", "version.json")
APP_JS = os.path.join(ROOT, "_ui_v151k", "app.js")
SPEC_PATH = os.path.join(ROOT, "spec_v151k_m", "微博bot小助手.spec")
DIST_DIR = os.path.join(ROOT, "dist_v151k_m")
WORK_DIR = os.path.join(ROOT, "work_v151k_m")
HISTORY_DIR = os.path.join(ROOT, "history")
MANIFEST = os.path.join(ROOT, "_manifest_v151j.xml")
ICON = os.path.join(ROOT, "logo.ico")
PY = r"C:\Users\zouzu\.workbuddy\binaries\python\envs\weibobot\Scripts\python.exe"

SPEC_TEMPLATE = '''# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules

datas = [('{ui}', 'ui'), ('{version_json}', '.')]
hiddenimports = ['version', 'ai_engine', 'storage', 'media_index', 'winctl', 'webview', 'webview.guilib', 'pystray', 'PIL', 'llama_cpp', 'llama_cpp.llama']
datas += collect_data_files('webview')
datas += collect_data_files('pystray')
datas += collect_data_files('PIL')
datas += collect_data_files('certifi')
datas += collect_data_files('jieba')
datas += collect_data_files('llama_cpp')
hiddenimports += collect_submodules('pystray')
hiddenimports += collect_submodules('PIL')
hiddenimports += collect_submodules('jieba')
hiddenimports += collect_submodules('llama_cpp')


a = Analysis(
    ['{entry}'],
    pathex=['{src}', '{root}'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='微博bot小助手',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['{icon}'],
    manifest='{manifest}',
)
'''


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_internal():
    with open(VERSION_JSON, encoding="utf-8") as f:
        v = json.load(f)
    return "%s%04d" % (v["date"], v["seq"])


def sync_changelog(internal):
    """把 internal 同步进 app.js CHANGELOG[0].internal（幂等；newline='' 保住 CRLF）。"""
    with open(APP_JS, encoding="utf-8", newline="") as f:
        src = f.read()
    m = re.search(r'(CHANGELOG\s*=\s*\[\s*\{\s*ver:\s*"[^"]*",\s*internal:\s*")(\d+)(")', src)
    if not m:
        print("CHANGELOG_SYNC pattern-not-found")
        return
    if m.group(2) == internal:
        print("CHANGELOG_SYNC no-op (internal=%s)" % internal)
        return
    src = src[: m.start(2)] + internal + src[m.end(2):]
    with open(APP_JS, "w", encoding="utf-8", newline="") as f:
        f.write(src)
    print("CHANGELOG_SYNC %s -> %s" % (m.group(2), internal))


def write_spec():
    spec = SPEC_TEMPLATE.format(
        ui=os.path.join(ROOT, "_ui_v151k").replace("\\", "/"),
        version_json=VERSION_JSON.replace("\\", "/"),
        entry=os.path.join(ROOT, "_src_v151k", "_app_v117.py").replace("\\", "/"),
        src=os.path.join(ROOT, "_src_v151k").replace("\\", "/"),
        root=ROOT.replace("\\", "/"),
        icon=ICON.replace("\\", "/"),
        manifest=MANIFEST.replace("\\", "/"),
    )
    # 幂等：内容一致则跳过写入（安全中心可能对 spec 路径有写限制）
    try:
        with open(SPEC_PATH, encoding="utf-8") as f:
            if f.read() == spec:
                print("spec unchanged, skip write (%s)" % SPEC_PATH)
                return
    except FileNotFoundError:
        pass
    with open(SPEC_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(spec)
    print("wrote %s" % SPEC_PATH)


def main():
    internal = read_internal()
    print("开始 v1.5.1 打包（UI=%s，修复两个偶发 BUG：进度环生长期高光 + 轮询进度条间歇不显示）…"
          % os.path.join(ROOT, "_ui_v151k"))
    sync_changelog(internal)
    write_spec()
    cmd = [PY, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--workpath", WORK_DIR, "--distpath", DIST_DIR, SPEC_PATH]
    print("RUN: %s" % " ".join(cmd))
    rc = subprocess.run(cmd, cwd=ROOT).returncode
    if rc != 0:
        sys.exit("BUILD_FAILED rc=%d" % rc)
    exe = os.path.join(DIST_DIR, "微博bot小助手.exe")
    size = os.path.getsize(exe)
    print("BUILD_DONE exe=%s size=%d" % (exe, size))
    print("ARCHIVE internal=%s" % internal)
    dst_root = os.path.join(ROOT, "微博bot小助手.exe")
    dst_hist = os.path.join(HISTORY_DIR, "微博bot小助手_%s.exe" % internal)
    shutil.copy2(exe, dst_root)
    shutil.copy2(exe, dst_hist)
    hashes = {}
    for tag, p in (("dist", exe), ("root", dst_root), ("history", dst_hist)):
        h = sha256(p)
        hashes[tag] = h
        print("  %-7s: %s %d %s" % (tag, p, os.path.getsize(p), h))
    if len(set(hashes.values())) != 1:
        sys.exit("ARCHIVE_HASH_MISMATCH")
    print("ARCHIVE_OK")


if __name__ == "__main__":
    main()
