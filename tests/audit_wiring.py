#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""接线完整性审计（复盘用）：找出「调了但不存在」的接口与事件处理器。

三个方向：
  A. 前端 `apiCall("x")` / `window.pywebview.api.x` → 后端 `Api.x` 是否存在
  B. HTML + JS 模板里的 `onclick= / onchange= / oninput=` 引用的函数是否定义过
  C. 后端暴露给前端的每个 `Api.*` 是否至少有一处前端调用（反向，仅提示）

A/B 任一不通即 = 该功能必然报错或静默失效，属于必须修的缺陷。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "_ui_v151k")
SRC = os.path.join(ROOT, "_src_v151k")

# 前端会调用但由 pywebview 自身提供 / 不存在的桥方法白名单
API_WHITELIST = set()
# onclick 里允许出现的内置函数
JS_BUILTINS = {"event", "this", "return", "if", "void"}


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def backend_methods():
    """Api 类里所有公开方法名。

    注意：`class Api:` 之后到文件末尾全是缩进体（后面没有其它顶层块），
    所以结束锚点必须是 `\Z`，不能只写 `(?=^\S)`。
    """
    src = read(os.path.join(SRC, "api_bridge.py"))
    m = re.search(r"^class Api\b.*?(?=^class |\Z)", src, re.S | re.M)
    if not m:
        raise SystemExit("未找到 class Api")
    body = m.group(0)
    names = set(re.findall(r"^    def ([A-Za-z_]\w*)\s*\(", body, re.M))
    return {n for n in names if not n.startswith("_")}


def frontend_api_calls():
    """前端所有 apiCall("x") 与 `.api.x(` 调用。"""
    calls = {}
    for fn in ("app.js", "index.html"):
        text = read(os.path.join(UI, fn))
        for m in re.finditer(r"""apiCall\(\s*["']([A-Za-z_]\w*)["']""", text):
            calls.setdefault(m.group(1), set()).add(fn)
        for m in re.finditer(r"\.api\.([A-Za-z_]\w*)\s*\(", text):
            calls.setdefault(m.group(1), set()).add(fn)
    return calls


def js_defined():
    js = read(os.path.join(UI, "app.js"))
    names = set(re.findall(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", js, re.M))
    names |= set(re.findall(
        r"^\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()",
        js, re.M))
    return names


def inline_handlers():
    """收集 HTML 与 JS 模板字符串里 on*="func(...)" 引用的函数名。"""
    refs = {}
    for fn in ("index.html", "app.js"):
        text = read(os.path.join(UI, fn))
        for m in re.finditer(r"""\bon(?:click|change|input|blur|keydown|submit)\s*=\s*["']([^"']+)["']""",
                             text):
            body = m.group(1)
            # 只认「顶层函数调用」：`document.getElementById(...)`、`event.stopPropagation()`
            # 这类方法调用前面带点，不该算成函数引用（否则全是误报）。
            for name in re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(", body):
                refs.setdefault(name, set()).add(fn)
    return refs


def main():
    bad = 0
    print("=" * 76)
    print("接线完整性审计")
    print("=" * 76)

    be = backend_methods()
    fe = frontend_api_calls()
    print("后端 Api 公开方法: %d 个 · 前端调用到的桥方法: %d 个" % (len(be), len(fe)))

    missing = []
    for name in sorted(fe):
        if name in be or name in API_WHITELIST:
            continue
        missing.append((name, sorted(fe[name])))
    print("\n【A】前端调用了、但后端没有的桥方法：%d 个" % len(missing))
    for name, where in missing:
        print("   ✗ %-32s  ← %s" % (name, ", ".join(where)))
    bad += len(missing)

    defined = js_defined()
    handlers = inline_handlers()
    print("\n【B】on* 事件里引用、但 JS 未定义的函数：")
    missing_js = []
    for name in sorted(handlers):
        if name in JS_BUILTINS or name in defined:
            continue
        missing_js.append((name, sorted(handlers[name])))
    print("   共 %d 个" % len(missing_js))
    for name, where in missing_js:
        print("   ✗ %-32s  ← %s" % (name, ", ".join(where)))
    bad += len(missing_js)

    # 反向：后端有、前端没调（可能只在内部用，仅提示）
    orphan = sorted(be - set(fe))
    print("\n【C】后端暴露但前端从未调用的方法（仅提示，可能内部复用）：%d 个" % len(orphan))
    for name in orphan:
        print("   · %s" % name)

    print("\n" + "=" * 76)
    print("必须修的接线缺陷：%d 个" % bad)
    print("=" * 76)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
