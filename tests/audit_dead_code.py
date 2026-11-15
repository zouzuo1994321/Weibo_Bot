#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""死代码审计：找出「定义了但没人用」的 CSS 类 与 JS 函数。

只做**候选报告**，不自动删。判定口径：
  · CSS 类：类名在 style.css 里出现于选择器，但在 index.html / app.js 里
    也找不到这个字符串（含 classList.add/remove/toggle、模板字符串、class="..."）。
  · JS 函数：名字在 app.js 里只出现于定义处，别处没有调用/引用。
  · JS 变量/常量：同上，但只报只在单处出现的顶层 const/let。

注意：动态拼接出来的类名（如 `fwd-${state}`）会被误报为死代码，删之前必须人工确认。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "_ui_v151k")

# 生成过程中由 JS 拼接、不会以完整字面量出现在源码里的前缀/后缀 —— 人工白名单
CSS_WHITELIST = set()


def read(name):
    with open(os.path.join(UI, name), encoding="utf-8") as f:
        return f.read()


def css_classes(css):
    """从样式表里抽出所有 .class 选择器名（忽略注释）。"""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = {}
    for m in re.finditer(r"\.([A-Za-z_][-\w]*)", css):
        name = m.group(1)
        # 纯数字或像小数（如 .5rem）跳过
        if name[0].isdigit():
            continue
        out.setdefault(name, 0)
        out[name] += 1
    return out


def js_functions(js):
    out = {}
    for m in re.finditer(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", js, re.M):
        out[m.group(1)] = m.start()
    for m in re.finditer(r"^\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()", js, re.M):
        out.setdefault(m.group(1), m.start())
    return out


def count_refs(text, name):
    return len(re.findall(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", text))


def main():
    css = read("style.css")
    html = read("index.html")
    js = read("app.js")
    all_text = html + "\n" + js

    print("=" * 74)
    print("死代码审计（仅候选，删前必须人工确认）")
    print("=" * 74)

    # ---------- CSS ----------
    classes = css_classes(css)
    dead_css = []
    for name, cnt in sorted(classes.items()):
        if name in CSS_WHITELIST:
            continue
        if count_refs(all_text, name) == 0:
            dead_css.append((name, cnt))
    print("\n【CSS】样式表里定义、HTML/JS 里完全没出现的类：%d 个" % len(dead_css))
    for name, cnt in dead_css:
        print("   .%-34s （%d 条规则引用）" % (name, cnt))

    # ---------- JS 函数 ----------
    funcs = js_functions(js)
    dead_js = []
    for name, pos in sorted(funcs.items()):
        # 在 app.js 里的总出现次数：定义 1 次 + 调用 N 次
        n_js = count_refs(js, name)
        n_html = count_refs(html, name)
        if n_js <= 1 and n_html == 0:
            dead_js.append(name)
    print("\n【JS】定义了但从未被调用的函数：%d 个" % len(dead_js))
    for name in dead_js:
        print("   %-40s" % name)

    # ---------- 体积 ----------
    print("\n【体积】")
    for f in sorted(os.listdir(UI)):
        p = os.path.join(UI, f)
        if os.path.isfile(p):
            print("   %-22s %9d 字节" % (f, os.path.getsize(p)))
    print("   %-22s %9d 字节" % ("UI 合计", sum(
        os.path.getsize(os.path.join(UI, f)) for f in os.listdir(UI)
        if os.path.isfile(os.path.join(UI, f)))))
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
