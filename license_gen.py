# -*- coding: utf-8 -*-
"""
授权机（作者专用离线密钥生成器）
================================
用途：用户把软件生成的「48 位特征码」发给你（作者），你据此生成
      授权密钥（格式 0000-0000-0000-0000-0000），用户回到软件「授权」页粘贴激活。

支持授权时长：1个月 / 3个月 / 6个月 / 1年 / 永久。

用法（交互）：
    python license_gen.py
用法（命令行）：
    python license_gen.py <特征码> <时长>
    时长取值：1m | 3m | 6m | 1y | permanent

说明：特征码自生成起 7 天内有效，过期需用户重新生成后再发给你。
      本脚本需与软件使用同一 SECRET（见 src/license.py），否则生成的密钥无法通过校验。
"""
import argparse
import os
import sys

# 允许从项目根目录直接运行（导入 src.license）
ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from licensemgr import (  # noqa: E402
    build_license_key, parse_feature_code, feature_code_valid, KEY_TYPES,
)

DURATIONS = {
    "1m": (30, 1),
    "3m": (90, 2),
    "6m": (180, 3),
    "1y": (365, 4),
    "permanent": (0, 5),
}


def run(feature_code: str, duration: str):
    code = feature_code.strip().replace("-", "").replace(" ", "").upper()
    parsed = parse_feature_code(code)
    if not parsed:
        print("✗ 特征码无效：应为 48 位（由 机器识别码+MAC+时间戳 组成）。")
        return
    if not feature_code_valid(parsed["ts"]):
        print("⚠ 该特征码已超出 7 天有效期，请让用户重新生成后再发送。")
        print("  （如确需签发，可在下方选择强制生成，但用户端可能视为异常。）")
        force = input("是否强制生成？(y/N): ").strip().lower()
        if force != "y":
            print("已取消。")
            return
    dur = duration.strip().lower()
    if dur not in DURATIONS:
        print("✗ 未知时长，可选：1m / 3m / 6m / 1y / permanent")
        return
    days, type_idx = DURATIONS[dur]
    key = build_license_key(parsed["mid"], days, type_idx)
    print("-" * 48)
    print(f"机器识别码 : {parsed['mid']}")
    print(f"MAC        : {parsed['mac']}")
    print(f"授权类型   : {KEY_TYPES[type_idx]}")
    print(f"生成密钥   : {key}")
    print("-" * 48)
    print("请将上方密钥发给用户，由其在「授权」页粘贴激活。")


def main():
    parser = argparse.ArgumentParser(description="微博bot小助手 授权机")
    parser.add_argument("feature_code", nargs="?", help="48 位特征码")
    parser.add_argument("duration", nargs="?", help="1m / 3m / 6m / 1y / permanent")
    args = parser.parse_args()

    if args.feature_code and args.duration:
        run(args.feature_code, args.duration)
        return

    print("=" * 48)
    print("微博bot小助手 · 授权机（离线密钥生成器）")
    print("=" * 48)
    while True:
        print()
        code = input("请输入用户发来的特征码（留空退出）：").strip()
        if not code:
            print("再见。")
            break
        print("可选时长：1m(1个月) 3m(3个月) 6m(6个月) 1y(1年) permanent(永久)")
        dur = input("请选择授权时长：").strip()
        run(code, dur)


if __name__ == "__main__":
    main()
