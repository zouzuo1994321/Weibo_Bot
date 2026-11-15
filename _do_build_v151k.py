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

# 【v1.5.8】构建期 JS 语法门禁。
# 事故：v1.5.7 往 CHANGELOG 里写文档字符串时带了一个未转义的双引号，
# app.js 整体解析失败 → 前端一行都不执行 → 永远停在「正在启动界面…」。
# Python 侧编译、导入、29 项基准测试全部照常通过（没人检查 .js），
# 于是错误一路进了 exe。这里在打包前强制 `node --check`。
NODE_CANDIDATES = [
    os.environ.get("NODE_EXE", ""),
    r"C:\Users\zouzu\.workbuddy\binaries\node\versions\22.22.2-6\node.exe",
    "node",
]


def _find_node():
    for p in NODE_CANDIDATES:
        if not p:
            continue
        if os.path.isabs(p):
            if os.path.exists(p):
                return p
        else:
            from shutil import which
            hit = which(p)
            if hit:
                return hit
    return None


def check_ui_syntax():
    """对 _ui_v151k 下所有 .js 做语法校验；失败直接中止构建。"""
    node = _find_node()
    if not node:
        print("UI_SYNTAX_SKIP 未找到 node.exe —— 前端语法未经校验，存在构建出错风险")
        return
    targets = []
    for root, _d, files in os.walk(os.path.join(ROOT, "_ui_v151k")):
        for fn in files:
            if fn.endswith(".js"):
                targets.append(os.path.join(root, fn))
    bad = []
    for p in targets:
        r = subprocess.run([node, "--check", p], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            bad.append((p, (r.stderr or "").strip().splitlines()[:6]))
    if bad:
        print("UI_SYNTAX_FAIL —— 前端语法错误，已中止打包：")
        for p, lines in bad:
            print("  · %s" % os.path.relpath(p, ROOT))
            for l in lines:
                print("      %s" % l)
        sys.exit(2)
    print("UI_SYNTAX_OK (%d 个 js 文件)" % len(targets))


SPEC_TEMPLATE = '''# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules

# 【v1.6.11】打包瘦身（依据：解包统计 124.4MB / 292 条目，逐项核实「谁在用」）
#   · jieba.lac_small  —— 词法分析大模型 10.2MB，本软件只用 cut/posseg，从未调用
#   · jieba.analyse    —— TF-IDF 的 idf.txt 5.9MB，从未调用 extract_tags/textrank
#   · PIL._avif        —— AVIF 编解码 7.5MB；软件只在托盘读 logo.png/ico，
#                         微博图片一律原样字节落盘（open(wb)），不过 Pillow
#   · PIL._imagingft   —— FreeType 文字栅格化 2.1MB，软件没有任何在图上绘字的代码
#   · llama_cpp/lib/*.lib —— 静态导入库，运行期不加载
# 保留不动的：cryptography（account_manager 用 Fernet 加密 Cookie）、numpy
# （llama_cpp 在 import 期就要它）、jieba.posseg（ai_generator 做词性标注）。
JIEBA_DROP_DATA = ['lac_small', 'lac_small/**', 'analyse', 'analyse/**']
JIEBA_DROP_MODS = ('jieba.lac_small', 'jieba.analyse')

datas = [('{ui}', 'ui'), ('{version_json}', '.')]
hiddenimports = ['version', 'ai_engine', 'storage', 'media_index', 'winctl', 'webview', 'webview.guilib', 'pystray', 'PIL', 'llama_cpp', 'llama_cpp.llama']
datas += collect_data_files('webview')
datas += collect_data_files('pystray')
datas += collect_data_files('PIL')
datas += collect_data_files('certifi')
datas += collect_data_files('jieba', excludes=JIEBA_DROP_DATA)
datas += collect_data_files('llama_cpp', excludes=['lib/*.lib'])
hiddenimports += collect_submodules('pystray')
hiddenimports += collect_submodules('PIL')
hiddenimports += [m for m in collect_submodules('jieba')
                  if not m.startswith(JIEBA_DROP_MODS)]
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
    excludes=['PIL._avif', 'PIL._imagingft'],
    noarchive=False,
    # optimize=2：剥掉 docstring / 断言（源码里无 assert），PYZ 更小
    optimize=2,
)

# 【v1.6.11】二次剔除：jieba 的官方 hook（_pyinstaller_hooks_contrib 里的
# hook-jieba.py）会无条件 `collect_data_files('jieba')`，把 lac_small / analyse
# 又整包加回来 —— 在 datas 参数里设 excludes 对它是无效的（上一轮实测残留 18MB）。
# 只能在 Analysis 之后按最终目标路径剪掉。
_DROP_PREFIX = ('jieba/lac_small/', 'jieba/analyse/')


def _keep_entry(entry):
    name = (entry[0] or '').replace('\\\\', '/')
    return not name.startswith(_DROP_PREFIX)


a.datas = [d for d in a.datas if _keep_entry(d)]
a.binaries = [b for b in a.binaries if _keep_entry(b)]
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
    os.makedirs(os.path.dirname(SPEC_PATH), exist_ok=True)
    with open(SPEC_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(spec)
    print("wrote %s" % SPEC_PATH)


def running_instances():
    """返回正在运行的「微博bot小助手.exe」进程 PID 列表。

    【v1.6.11 新增闸门】事故：v1.6.10 打包时用户还开着旧 exe，
    `shutil.copy2` 写根目录直接抛 PermissionError(WinError 32)，
    而这一步在 dist 构建之后、哈希断言之前 —— 结果是 dist 已是新版、
    根目录和历史归档还停在上一个版本，三处错位且脚本没给出明确结论。
    现在把「有没有旧进程」变成**构建前**的硬闸门，不合格直接退出。
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq 微博bot小助手.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="ignore")
    except Exception as e:
        print("PROC_CHECK_SKIP 无法执行 tasklist：%s" % e)
        return []
    pids = []
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower().endswith("微博bot小助手.exe".lower()):
            try:
                pids.append(int(parts[1]))
            except Exception:
                pass
    return pids


def main():
    internal = read_internal()

    # 【v1.6.11】旧进程闸门：旧 exe 还开着就会导致根目录覆盖失败、三交付位错位
    pids = running_instances()
    if pids:
        print("PROC_BLOCKED —— 检测到软件仍在运行，已中止打包：")
        print("  进程 PID: %s" % ", ".join(str(p) for p in pids))
        print("  请先完全退出软件（含托盘图标右键退出），再重新运行本脚本。")
        print("  （不先退出的话，dist 会是新的，但根目录/history 会覆盖失败，三处错位）")
        sys.exit(3)

    print("开始 v1.6.13 打包（「仅转发含图博文」归位到转发方式 + 「只看有图」默认开启）…")
    check_ui_syntax()
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
    try:
        os.makedirs(HISTORY_DIR, exist_ok=True)
        shutil.copy2(exe, dst_root)
        shutil.copy2(exe, dst_hist)
    except PermissionError as e:
        # 【v1.6.11】明确报错，不再留下「dist 新、根目录旧」的错位现场
        sys.exit("ARCHIVE_BLOCKED 文件被占用（多半是软件还在运行）：%s\n"
                 "  dist 里已是新版，但根目录/历史归档没更新 —— 请退出软件后重跑本脚本。"
                 % e)
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
