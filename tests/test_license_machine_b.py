# -*- coding: utf-8 -*-
"""验证授权一机一码：同机激活有效；复制 license.json 到异机后失效。"""
import os
import sys
import json
import tempfile

BASE = r"Z:\【01】自研软件\【26-11】微博bot小助手（正式版）"
sys.path.insert(0, os.path.join(BASE, "_src_v117"))

import paths

TMP = tempfile.mkdtemp(prefix="lic_test_")
paths.DATA_DIR = TMP
paths.LICENSE_FILE = os.path.join(TMP, "license.json")
paths.TRIAL_FILE = os.path.join(TMP, "trial.json")

import licensemgr

# 清空可能存在的授权/试用文件
for f in (paths.LICENSE_FILE, paths.TRIAL_FILE):
    if os.path.exists(f):
        os.remove(f)

failures = []


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {extra}")
    if not cond:
        failures.append(name)


# 1) 同机：生成密钥 → 激活 → 状态应为已授权
mid = licensemgr.machine_id()
key = licensemgr.build_license_key(mid, 365, 5)
r = licensemgr.activate_license(key)
check("同机激活成功", r.get("ok"), str(r))

st = licensemgr.get_status()
check("同机 get_status 已授权", st.get("licensed") is True, str(st.get("type_name")))
check("同机 can_forward 允许", licensemgr.can_forward() is True)

# 2) 模拟 license.json 被复制到另一台机器（机器指纹变化）
real_mid = licensemgr.machine_id
licensemgr.machine_id = lambda: "DEADBEEFDEADBEEF"
try:
    st2 = licensemgr.get_status()
    check("异机 get_status 未授权", st2.get("licensed") is False, str(st2.get("type_name")))
    check("异机 标记 machine_mismatch", st2.get("machine_mismatch") is True, str(st2.get("machine_mismatch")))
    # 异机时：授权本身不被承认（即便 exp 有效）。转发是否允许取决于试用，
    # 新机器试用是全新 7 天属正常设计，故只校验"授权未被复制"这一核心点。
    check("异机 授权未被复制(exp仍有效但licensed=False)",
          st2.get("licensed") is False and st2.get("machine_mismatch") is True)
finally:
    licensemgr.machine_id = real_mid

# 3) 过期密钥仍应失效（即便机器匹配）
licensemgr.machine_id = real_mid
expired_key = licensemgr.build_license_key(mid, 0, 4)  # 永久？用 type 4 但 days=0→永久；改用 days=1 已过
# 构造一个已过期的密钥：days=1 但 exp 设为过去
import time
old = licensemgr.build_license_key(mid, 1, 4)
# 直接写一个过期 license 文件验证
with open(paths.LICENSE_FILE, "w", encoding="utf-8") as f:
    json.dump({"key": old, "type": 4, "type_name": "1年", "exp": int(time.time()) - 100,
               "activated_at": int(time.time()) - 86400}, f)
st3 = licensemgr.get_status()
check("过期密钥 get_status 未授权", st3.get("licensed") is False, str(st3.get("exp_str")))

# 4) 试用也绑定本机：换机器后不再沿用旧的起算时间
licensemgr.machine_id = real_mid
if os.path.exists(paths.TRIAL_FILE):
    os.remove(paths.TRIAL_FILE)
# 模拟 TRIAL_FILE 来自另一台机器（mid 不匹配、first_run 被人为置 0）
with open(paths.TRIAL_FILE, "w", encoding="utf-8") as f:
    json.dump({"first_run": 0, "mid": "ANOTHERMAC000001"}, f)
# 当前机器读取试用 → mid 不匹配 → 应重新起算（first_run 不再是 0）
fr = licensemgr._trial_first_run()
check("试用绑定本机(异机 first_run 被重置)", fr != 0, f"first_run={fr}")
# 再次读取（仍是当前机器）→ mid 匹配 → 沿用刚写入的起算时间
fr2 = licensemgr._trial_first_run()
check("试用绑定本机(同机沿用起算时间)", fr2 == fr, f"{fr2} vs {fr}")

print("\n结果:", "ALL_LICENSE_BINDING_TESTS_PASS" if not failures else f"FAILURES={failures}")
