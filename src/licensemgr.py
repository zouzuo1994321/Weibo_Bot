# -*- coding: utf-8 -*-
"""
软件授权模块
============
职责：
  - 试用管理：首次启动记录时间，默认 7 天试用；过期后禁用转发功能。
  - 特征码：48 位，由（时间戳 + 机器识别码 + MAC）生成，可重新生成；
            自生成起 7 天内有效，供作者离线签发授权密钥。
  - 授权密钥：格式 0000-0000-0000-0000-0000（20 位十六进制 / 10 字节），
            由「授权机（license_gen.py）」基于特征码生成，
            本模块负责校验其归属本机、有效期与完整性。

密钥绑定本机：密钥内部包含"机器指纹"（由机器识别码派生），
只有生成特征码的那台机器才能激活，复制到其他机器无效。
"""
import hashlib
import hmac
import json
import os
import struct
import time

from paths import DATA_DIR

# 软件端与授权机共享的密钥（必须一致，否则无法互验）。
SECRET = b"WB_AE_LIC_2026_X9K2P4Q8"

TRIAL_DAYS = 7                      # 默认试用天数
FEATURE_CODE_VALID_DAYS = 7         # 特征码自生成起有效天数（供作者签发）
LICENSE_FILE = os.path.join(DATA_DIR, "license.json")
TRIAL_FILE = os.path.join(DATA_DIR, "trial.json")

# 授权类型：索引 -> 名称 / 时长（天，0 表示永久）
KEY_TYPES = {
    0: "试用",
    1: "1个月",
    2: "3个月",
    3: "6个月",
    4: "1年",
    5: "永久",
}
KEY_TYPE_DAYS = {0: 0, 1: 30, 2: 90, 3: 180, 4: 365, 5: 0}


# ------------------------- 机器识别 -------------------------
def _mac_address() -> str:
    """返回本机 MAC（12 位大写十六进制），失败时返回全 0。"""
    try:
        import uuid
        mac = uuid.getnode()
        return f"{mac:012X}"
    except Exception:
        return "000000000000"


def _machine_guid() -> str:
    """读取 Windows 机器 GUID（稳定且重启不变）。其他平台回退到主机名。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography") as k:
            return winreg.QueryValueEx(k, "MachineGuid")[0]
    except Exception:
        return os.environ.get("COMPUTERNAME", "unknown-host")


def machine_id() -> str:
    """稳定的 16 位十六进制机器识别码（MAC + 机器 GUID 派生）。"""
    raw = f"{_mac_address()}|{_machine_guid()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


# ------------------------- base36 工具 -------------------------
def _b36_encode(n: int) -> str:
    if n == 0:
        return "0"
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    s = ""
    while n > 0:
        s = chars[n % 36] + s
        n //= 36
    return s


def _b36_decode(s: str) -> int:
    return int(s, 36)


# ------------------------- 特征码 -------------------------
def generate_feature_code() -> str:
    """生成 48 位特征码：机器识别码(16) + MAC(12) + 时间戳(10) + 随机串(10)。

    自生成起 7 天内有效（见 FEATURE_CODE_VALID_DAYS），可重复调用以重新生成。
    """
    mid = machine_id()
    mac = _mac_address()
    ts = _b36_encode(int(time.time())).zfill(10)[:10]
    import os as _os
    serial = _os.urandom(5).hex()  # 10 位十六进制
    return (mid + mac + ts + serial)[:48].ljust(48, "0")


def parse_feature_code(code: str) -> dict:
    """解析特征码，返回 {mid, mac, ts}；非法返回 None。"""
    if not code:
        return None
    code = code.strip().replace("-", "").replace(" ", "")
    if len(code) != 48:
        return None
    try:
        mid = code[:16].lower()
        mac = code[16:28].upper()
        ts = _b36_decode(code[28:38])
    except Exception:
        return None
    return {"mid": mid, "mac": mac, "ts": ts}


# ------------------------- 密钥构造 / 解析 -------------------------
def build_license_key(mid: str, days: int, key_type_idx: int) -> str:
    """（授权机使用）根据机器识别码生成授权密钥。

    days=0 表示永久；key_type_idx 见 KEY_TYPES。
    """
    fp = hashlib.sha256(SECRET + mid.lower().encode("utf-8")).digest()[:4]
    exp = 0 if (days == 0) else (int(time.time()) + days * 86400)
    exp_b = struct.pack(">I", exp & 0xFFFFFFFF)
    type_b = bytes([int(key_type_idx) & 0xFF])
    body = fp + exp_b + type_b                       # 9 字节
    chk = hmac.new(SECRET, body, hashlib.sha256).digest()[:1]
    raw = body + chk                                 # 10 字节 = 20 位十六进制
    hexs = raw.hex().upper()
    return "-".join(hexs[i:i + 4] for i in range(0, 20, 4))


def parse_license_key(key: str) -> dict:
    """解析并校验授权密钥，返回 {type, exp, mid_ok}；失败返回 None。"""
    if not key:
        return None
    hexs = key.replace("-", "").replace(" ", "").upper()
    if len(hexs) != 20:
        return None
    try:
        raw = bytes.fromhex(hexs)
    except Exception:
        return None
    body, chk = raw[:9], raw[9:]
    expected_chk = hmac.new(SECRET, body, hashlib.sha256).digest()[:1]
    if not hmac.compare_digest(expected_chk, chk):
        return None
    fp, exp_b, type_b = raw[:4], raw[4:8], raw[8:9]
    expected_fp = hashlib.sha256(SECRET + machine_id().encode("utf-8")).digest()[:4]
    mid_ok = hmac.compare_digest(expected_fp, fp)
    exp = struct.unpack(">I", exp_b)[0]
    return {"type": type_b[0], "exp": exp, "mid_ok": mid_ok}


# ------------------------- 试用 / 授权状态 -------------------------
def _save_license(data: dict):
    with open(LICENSE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_license() -> dict:
    if os.path.exists(LICENSE_FILE):
        try:
            with open(LICENSE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _trial_first_run() -> int:
    if os.path.exists(TRIAL_FILE):
        try:
            with open(TRIAL_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("first_run", int(time.time()))
        except Exception:
            pass
    t = int(time.time())
    try:
        with open(TRIAL_FILE, "w", encoding="utf-8") as f:
            json.dump({"first_run": t}, f)
    except Exception:
        pass
    return t


def trial_info() -> dict:
    first = _trial_first_run()
    now = int(time.time())
    elapsed = now - first
    remaining = TRIAL_DAYS * 86400 - elapsed
    return {
        "first_run": first,
        "elapsed_days": elapsed // 86400,
        "remaining_seconds": max(0, remaining),
        "trial_days": TRIAL_DAYS,
    }


def get_status() -> dict:
    """返回授权状态：{licensed, type_name, exp, trial, ...}。"""
    lic = get_license()
    now = int(time.time())
    if lic:
        exp = lic.get("exp", 0)
        active = (exp == 0) or (exp > now)
        if active:
            return {
                "licensed": True,
                "trial": False,
                "type_name": lic.get("type_name", KEY_TYPES.get(lic.get("type"), "未知")),
                "type": lic.get("type"),
                "exp": exp,
                "exp_str": _fmt_exp(exp),
            }
    t = trial_info()
    return {
        "licensed": False,
        "trial": True,
        "type_name": "试用",
        "type": 0,
        "exp": 0,
        "exp_str": "",
        "first_run": t["first_run"],
        "elapsed_days": t["elapsed_days"],
        "remaining_seconds": t["remaining_seconds"],
        "trial_days": t["trial_days"],
    }


def can_forward() -> bool:
    """是否允许转发（试用期内或已授权未过期）。"""
    s = get_status()
    if s.get("licensed"):
        return True
    return s.get("remaining_seconds", 0) > 0


def forward_block_reason() -> str:
    s = get_status()
    if s.get("licensed"):
        return ""
    if s.get("remaining_seconds", 0) > 0:
        return ""
    return "试用已结束，请获取授权后继续使用转发功能（见「授权」页面）。"


def activate_license(key: str) -> dict:
    """校验并激活授权密钥。"""
    parsed = parse_license_key(key)
    if not parsed:
        return {"ok": False, "error": "授权密钥无效或格式错误"}
    if not parsed.get("mid_ok"):
        return {"ok": False, "error": "该授权密钥不属于本机（机器不匹配）"}
    now = int(time.time())
    if parsed["exp"] != 0 and parsed["exp"] < now:
        return {"ok": False, "error": "授权密钥已过期"}
    data = {
        "key": key,
        "type": parsed["type"],
        "type_name": KEY_TYPES.get(parsed["type"], "未知"),
        "exp": parsed["exp"],
        "activated_at": now,
    }
    _save_license(data)
    return {"ok": True, **data}


def deactivate_license() -> dict:
    try:
        if os.path.exists(LICENSE_FILE):
            os.remove(LICENSE_FILE)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _fmt_exp(exp: int) -> str:
    if not exp or exp == 0:
        return "永久"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp))


def feature_code_valid(ts: int) -> bool:
    """特征码是否在有效期内（供授权机判断）。"""
    return (int(time.time()) - ts) <= FEATURE_CODE_VALID_DAYS * 86400
