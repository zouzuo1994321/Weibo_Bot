# -*- coding: utf-8 -*-
"""
统一的 AI 引擎抽象层（v1.2.0 新增，参考「标书检查工具箱」app/ai_engine.py）。

设计目标：
  1. 三类可切换的 AI 来源，均用于「生成转发文案」这一件事：
       · 内置离线AI（builtin）—— 软件自带的确定性离线规则引擎（jieba 分词 +
         人格模板 + 意图识别），不联网、不依赖大模型运行时，始终可用；
       · 本地离线AI（local）—— 本机 Ollama（默认 127.0.0.1:11434），纯本地；
         另外**保留** v1.1.x 的 llama.cpp + Qwen2.5-1.5B GGUF 路线（引擎标识
         ``local_gguf``），因为它随软件自动下载模型、无需用户装 Ollama；
       · 云端在线AI（cloud）—— 兼容 OpenAI /chat/completions 的在线大模型 API，
         需用户自备 endpoint 与 key，仅在联网时可用。
  2. 只读增强、绝不静默降级：任一外部 AI 不可用或调用失败时，
     **明确回落**到内置引擎并在返回值/日志里说明原因，绝不假装成功。
  3. 配置与密钥保存在独立 JSON（DATA_DIR/ai_settings.json），不与 config.json /
     sqlite 混存，避免导出备份时泄露 key。
  4. 主 / 备双路线：``primary`` 优先，失败自动切``fallback``，两者都失败才用builtin。

对外关键接口：
  load_config() / save_config(cfg)
  get_active_engine() -> "builtin" | "local" | "local_gguf" | "cloud"
  ai_status(cfg=None) -> dict         探测引擎可用性（支持草稿配置探测）
  ask(prompt, **kw) -> dict           统一大模型调用，返回 {ok, engine, text, error}
  generate_forward_text(...) -> dict  转发文案生成的**唯一**入口（含主备路由）
  list_local_models(host) / probe_local(host) / launch_local()
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

from paths import DATA_DIR
from logger import info, warn, error, debug

# ----------------------------------------------------------------- 引擎类型
ENGINE_LABELS = {
    "builtin": "内置离线AI",
    "local": "本地离线AI (Ollama)",
    "local_gguf": "本地离线AI (llama.cpp)",
    "cloud": "云端在线AI",
}
VALID_ENGINES = ("builtin", "local", "local_gguf", "cloud")
# 需要外部大模型运行时 / 网络的引擎
EXTERNAL_ENGINES = ("local", "local_gguf", "cloud")

LOCAL_DEFAULT_HOST = "http://127.0.0.1:11434"
CLOUD_DEFAULT_BASE = "https://api.openai.com/v1"

AI_SETTINGS_NAME = "ai_settings.json"


def _settings_path() -> str:
    return os.path.join(DATA_DIR, AI_SETTINGS_NAME)


DEFAULT_CONFIG = {
    # 主路线：默认 builtin（零依赖、始终可用），用户可改
    "active_engine": "builtin",
    # 备路线：主路线失败时自动切到备路线
    "fallback_engine": "builtin",
    "local": {
        "host": LOCAL_DEFAULT_HOST,
        "model": "",              # 留空 = 自动取本机第一个模型
        "auto_start": False,      # 探测不到时尝试拉起 Ollama
        "timeout": 30,
    },
    "local_gguf": {
        "model_file": "",         # 留空 = 用model_manager 默认的 Qwen2.5-1.5B
        "auto_download": True,    # 缺失时自动后台下载（约 1GB）
        "timeout": 120,
    },
    "cloud": {
        "base_url": CLOUD_DEFAULT_BASE,
        "api_key": "",
        "model": "gpt-4o-mini",
        "timeout": 60,
    },
}


# ----------------------------------------------------------------- 配置读写
def _deep_merge(base: dict, over: dict) -> None:
    for k, v in (over or {}).items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def load_config() -> dict:
    """读配置。任何异常都退回全默认（保证 AI 永远有一个可用的 builtin）。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))   # 深拷贝
    try:
        p = _settings_path()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                _deep_merge(cfg, raw)
    except Exception as e:
        warn(f"AI 配置读取失败，使用默认配置：{e}")
    for k in ("active_engine", "fallback_engine"):
        if cfg.get(k) not in VALID_ENGINES:
            cfg[k] = "builtin"
    return cfg


def save_config(cfg: dict) -> bool:
    """原子替换写配置。返回是否成功。"""
    p = _settings_path()
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        for k in ("active_engine", "fallback_engine"):
            if cfg.get(k) not in VALID_ENGINES:
                cfg[k] = "builtin"
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
        return True
    except Exception as e:
        error(f"AI 配置保存失败：{e}")
        return False


def get_config() -> dict:
    return load_config()


def get_active_engine() -> str:
    return load_config().get("active_engine", "builtin")


# ----------------------------------------------------------------- 本地 Ollama
def _no_proxy_urlopen(req, timeout):
    """对本地回环地址（如 127.0.0.1:11434）的请求必须绕过系统代理。

    否则开着 Clash / v2ray 等代理时，本机 Ollama 请求会被发往代理网关而失败，
    即便 Ollama 正常也会误报「未检测到本机 Ollama」。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, timeout=timeout)


def probe_local(host: str = "") -> bool:
    host = (host or LOCAL_DEFAULT_HOST).rstrip("/")
    try:
        req = urllib.request.Request(host + "/api/tags", method="GET")
        with _no_proxy_urlopen(req, 2.0) as r:
            return r.status == 200
    except Exception:
        return False


def list_local_models(host: str = "") -> list:
    host = (host or LOCAL_DEFAULT_HOST).rstrip("/")
    try:
        req = urllib.request.Request(host + "/api/tags", method="GET")
        with _no_proxy_urlopen(req, 5.0) as r:
            data = json.loads(r.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


def launch_local() -> (bool, str):
    """用 powershell 后台拉起本机 Ollama（= `ollama serve`），四重静默。"""
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0        # SW_HIDE
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command",
             "Start-Process ollama -ArgumentList 'serve' -WindowStyle Hidden"],
            startupinfo=si, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            shell=False,
        )
        for _ in range(16):
            time.sleep(0.5)
            if probe_local(LOCAL_DEFAULT_HOST):
                return True, "已启动本机 Ollama"
        return True, "已尝试拉起 Ollama，请稍候点「检测本地 AI 引擎」确认"
    except Exception as e:
        return False, "拉起失败：%s" % e


# ----------------------------------------------------------------- 云端探测
def _probe_cloud(base_url: str, api_key: str, model: str) -> (bool, str):
    """探测云端服务可达性。

    401/403 视为「服务可达」——某些厂商 /models 端点需特定权限，
    不能据此判死；只有连不上 / 5xx / 参数缺失才算不可用。
    """
    base = (base_url or "").rstrip("/")
    try:
        req = urllib.request.Request(base + "/models", method="GET")
        req.add_header("Authorization", "Bearer " + api_key)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=8.0) as r:
            return r.status == 200, "ok"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return True, "鉴权端点受限（%d），但服务可达" % e.code
        return False, "HTTP %d" % e.code
    except Exception as e:
        return False, str(e)[:120]


# ----------------------------------------------------------------- llama.cpp
def _gguf_state() -> dict:
    """取model_manager 的 llama.cpp 状态（延迟导入，避免无谓加载）。"""
    try:
        import model_manager
        return model_manager.get_state()
    except Exception as e:
        debug(f"model_manager 状态读取失败：{e}")
        return {"exists": False, "downloading": False, "progress": 0.0,
                "loaded": False, "error": str(e)[:120], "model_path": ""}


def _gguf_available() -> bool:
    try:
        import model_manager
        return bool(model_manager.model_available())
    except Exception:
        return False


# ----------------------------------------------------------------- 状态探测
def ai_status(cfg: dict = None) -> dict:
    """探测引擎可用性。

    传入 cfg 即「草稿配置探测」——探测设置页当前控件的即时取值，
    避免「下拉选了 Ollama、状态却显示 builtin」的错位；缺省则探测已保存配置。
    """
    cfg = cfg or load_config()
    engine = cfg.get("active_engine", "builtin")

    if engine == "builtin":
        return {"engine": "builtin", "label": ENGINE_LABELS["builtin"],
                "available": True, "models": [],
                # 这段文案会同时显示在「主路线」「备路线」两处状态行，
                # 故保持简短——原来 50+ 字会把AI 部署卡拉得过高、
                # 进而把同一网格里的「轮询间隔」卡顶出大片空白。
                "detail": "零依赖、始终可用，无需联网或模型运行时。"}

    if engine == "local":
        host = (cfg.get("local", {}) or {}).get("host") or LOCAL_DEFAULT_HOST
        ok = probe_local(host)
        if not ok and (cfg.get("local", {}) or {}).get("auto_start"):
            launch_local()
            ok = probe_local(host)
        models = list_local_models(host) if ok else []
        return {"engine": "local", "label": ENGINE_LABELS["local"],
                "available": ok, "models": models,
                "detail": ("本机 Ollama 可达（%s），已安装 %d 个模型。" % (host, len(models)))
                          if ok else
                          ("未检测到本机 Ollama（%s）。请先安装并启动 Ollama，"
                           "或点击「启动 Ollama」。" % host)}

    if engine == "local_gguf":
        st = _gguf_state()
        exists = bool(st.get("exists"))
        detail = "已就绪：%s" % st.get("model_path", "")
        if not exists:
            if st.get("downloading"):
                detail = ("模型下载中…%d%%" % round((st.get("progress") or 0) * 100))
            else:
                detail = ("模型尚未下载（%s）。点击「下载 / 重试模型」后台拉取，"
                          "约 1GB，全离线运行。" % st.get("model_path", "data/models/"))
        return {"engine": "local_gguf", "label": ENGINE_LABELS["local_gguf"],
                "available": exists, "models": [st.get("engine", "")] if exists else [],
                "detail": detail, "deploy": st}

    # cloud
    c = cfg.get("cloud", {}) or {}
    base = (c.get("base_url") or "").strip().rstrip("/")
    key = (c.get("api_key") or "").strip()
    model = (c.get("model") or "").strip()
    if not base or not key or not model:
        return {"engine": "cloud", "label": ENGINE_LABELS["cloud"],
                "available": False, "models": [],
                "detail": "尚未配置完整的云端 AI 参数（base_url / api_key / model）。"}
    ok, msg = _probe_cloud(base, key, model)
    return {"engine": "cloud", "label": ENGINE_LABELS["cloud"],
            "available": ok, "models": [model] if ok else [],
            "detail": ("云端 AI 连接正常（%s，模型 %s）。" % (base, model)) if ok
                      else ("云端 AI 连接失败：%s" % msg)}


def all_status(cfg: dict = None) -> dict:
    """一次性给出三类引擎 + 当前主/备路线的完整状态，供前端渲染。"""
    cfg = cfg or load_config()
    out = {"ok": True, "labels": ENGINE_LABELS, "valid_engines": list(VALID_ENGINES),
           "primary": cfg.get("active_engine", "builtin"),
           "fallback": cfg.get("fallback_engine", "builtin"),
           "engines": {}}
    for e in VALID_ENGINES:
        sub = dict(cfg)
        sub["active_engine"] = e
        try:
            out["engines"][e] = ai_status(sub)
        except Exception as ex:
            out["engines"][e] = {"engine": e, "label": ENGINE_LABELS[e],
                                 "available": False, "models": [],
                                 "detail": "探测异常：%s" % ex}
    out["deploy"] = _gguf_state()
    return out


# ----------------------------------------------------------------- 统一调用
def ask(prompt: str, system: str = "", temperature: float = 0.95,
        max_tokens: int = 160, timeout: int = None,
        engine: str = "", model: str = "") -> dict:
    """统一的大模型调用。返回 {ok, engine, text, error}。

    - engine 缺省取全局 active_engine；
    - builtin：无外部大模型，返回 ok=False 并说明原因（调用方应改用规则引擎）；
    - local      ：POST 到 Ollama /api/generate；
    - local_gguf ：走model_manager（llama.cpp + GGUF）；
    - cloud      ：POST 到 OpenAI 兼容 /chat/completions。
    """
    cfg = load_config()
    effective = engine or cfg.get("active_engine", "builtin")
    if effective == "builtin":
        return {"ok": False, "engine": "builtin", "text": "",
                "error": "内置离线AI 为确定型规则引擎，无需联网调用。"}
    if effective == "local":
        return _ask_ollama(prompt, system, temperature, max_tokens,
                           timeout or (cfg.get("local", {}) or {}).get("timeout", 30),
                           model=model)
    if effective == "local_gguf":
        return _ask_gguf(prompt, system, temperature, max_tokens,
                         timeout or (cfg.get("local_gguf", {}) or {}).get("timeout", 120))
    if effective == "cloud":
        return _ask_cloud(prompt, system, temperature, max_tokens,
                          timeout or (cfg.get("cloud", {}) or {}).get("timeout", 60),
                          model=model)
    return {"ok": False, "engine": effective, "text": "",
            "error": "未知的 AI 引擎：%s" % effective}


def _ask_ollama(prompt, system, temperature, max_tokens, timeout, model="") -> dict:
    cfg = load_config()["local"]
    host = (cfg.get("host") or LOCAL_DEFAULT_HOST).rstrip("/")
    model = (model or cfg.get("model") or "").strip()
    if not model:
        models = list_local_models(host)
        if not models:
            return {"ok": False, "engine": "local", "text": "",
                    "error": "本机 Ollama 未安装任何模型，请先 `ollama pull` 一个模型。"}
        model = models[0]
    payload = {"model": model, "prompt": prompt, "stream": False,
               "options": {"temperature": temperature, "num_predict": max_tokens}}
    if system:
        payload["system"] = system
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(host + "/api/generate", data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with _no_proxy_urlopen(req, timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
        text = (resp.get("response") or "").strip()
        return {"ok": True, "engine": "local", "text": text,
                "error": None if text else "Ollama 返回空内容"}
    except Exception as e:
        return {"ok": False, "engine": "local", "text": "", "error": str(e)[:200]}


def _ask_gguf(prompt, system, temperature, max_tokens, timeout) -> dict:
    """llama.cpp + GGUF 路线。复用 model_manager 的懒加载单例。"""
    try:
        import model_manager
    except Exception as e:
        return {"ok": False, "engine": "local_gguf", "text": "",
                "error": "本地模型模块不可用：%s" % e}
    if not model_manager.model_available():
        return {"ok": False, "engine": "local_gguf", "text": "",
                "error": "本地模型文件尚未下载完成，请等待后台下载结束或检查网络。"}
    full = (system + "\n\n" + prompt) if system else prompt
    try:
        out = model_manager.generate(
            full, max_tokens=max_tokens, temperature=temperature)
    except Exception as e:
        return {"ok": False, "engine": "local_gguf", "text": "",
                "error": str(e)[:200]}
    if not out:
        return {"ok": False, "engine": "local_gguf", "text": "",
                "error": "本地模型推理失败或返回空内容"}
    return {"ok": True, "engine": "local_gguf", "text": out, "error": None}


def _ask_cloud(prompt, system, temperature, max_tokens, timeout, model="") -> dict:
    cfg = load_config()["cloud"]
    base = (cfg.get("base_url") or CLOUD_DEFAULT_BASE).rstrip("/")
    key = (cfg.get("api_key") or "").strip()
    model = (model or cfg.get("model") or "").strip()
    if not base or not key or not model:
        return {"ok": False, "engine": "cloud", "text": "",
                "error": "云端 AI 参数不完整（base_url / api_key / model）。"}
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    payload = {"model": model, "messages": msgs, "temperature": temperature,
               "max_tokens": max_tokens, "stream": False}
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(base + "/chat/completions", data=data, method="POST")
        req.add_header("Authorization", "Bearer " + key)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
        text = (resp["choices"][0]["message"]["content"] or "").strip()
        return {"ok": True, "engine": "cloud", "text": text,
                "error": None if text else "云端返回空内容"}
    except Exception as e:
        return {"ok": False, "engine": "cloud", "text": "", "error": str(e)[:200]}


# ----------------------------------------------------------------- 主备路由
def generate_forward_text(text: str, persona: str = "幽默",
                          persona_desc: str = "", k1: str = "", k2: str = "",
                          custom_templates=None) -> dict:
    """转发文案生成的**唯一**入口（主 / 备双路线）。

    路由逻辑：
      主路线（external）→ 成功即返回
                → 失败则记日志 + 切备路线（fallback，也可能是 builtin）
                → 备路线成功即返回（result.engine 标明实际来源）
                → 都失败才落回 builtin 规则引擎（永不返回空）

    返回 {ok, engine, text, error, chain}：
      chain 是实际尝试过的引擎序列，便于前端/日志展示「为什么降级了」。
    """
    cfg = load_config()
    primary = cfg.get("active_engine", "builtin")
    fallback = cfg.get("fallback_engine", "builtin")
    chain = []

    def _rule(reason: str = "") -> dict:
        """builtin 规则引擎兜底（始终可用）。"""
        try:
            import ai_generator
            out = ai_generator.generate_by_rule(
                text, persona=persona, custom_templates=custom_templates)
            return {"ok": bool(out), "engine": "builtin", "text": out or "",
                    "error": reason, "chain": chain + ["builtin"]}
        except Exception as e:
            error(f"内置规则引擎异常：{e}")
            return {"ok": False, "engine": "builtin", "text": "",
                    "error": "内置规则引擎异常：%s" % e, "chain": chain}

    # ---- builtin 直接出结果 ----
    if primary == "builtin":
        r = _rule()
        if fallback != "builtin" and not r["ok"]:
            pass# builtin 理论上不会失败；真失败就再试备路线
        return r

    # ---- 外部引擎：构造与大模型路线一致的 prompt ----
    prompt = _build_prompt(text, persona_desc, k1, k2)
    for eng in (primary, fallback):
        if eng == "builtin":
            continue
        if eng in chain:            # 主备同一个引擎，别重复试
            continue
        chain.append(eng)
        r = _ask_engine(eng, text, prompt, persona_desc, persona, k1, k2)
        if r.get("ok") and r.get("text"):
            try:
                import ai_generator
                cleaned = ai_generator.clean_model_output(r["text"])
            except Exception:
                cleaned = (r["text"] or "").strip()
            if cleaned:
                return {"ok": True, "engine": eng, "text": cleaned,
                        "error": None, "chain": chain}
            r = {"ok": False, "text": "", "error": "返回内容清洗后为空"}
        warn(f"AI 引擎 {eng} 生成失败：{r.get('error')}"
             + ("，切备路线" if eng == primary else "，落回内置规则引擎"))

    return _rule("所有外部 AI 引擎均不可用，已落回内置规则引擎")


def _ask_engine(engine: str, text: str, prompt: str, persona_desc: str,
                persona: str, k1: str, k2: str) -> dict:
    """按引擎分派。

    local_gguf 走 ai_generator.model_generate ——它带三层去重
    （跨条历史注入 + 结构级随机化 + 安全条款），是v1.1.x 积累的成果，
    不能被这里的裸 prompt 调用绕过。
    """
    if engine == "local_gguf":
        try:
            import ai_generator
            out = ai_generator.model_generate(
                text, persona_desc, k1, k2, persona_key=persona)
            if out:
                return {"ok": True, "engine": engine, "text": out, "error": None}
            return {"ok": False, "engine": engine, "text": "",
                    "error": "本地模型未就绪或返回空"}
        except Exception as e:
            return {"ok": False, "engine": engine, "text": "", "error": str(e)[:200]}
    return ask(prompt, system=_build_system(persona_desc), engine=engine,
               max_tokens=160)


def _build_system(persona_desc: str) -> str:
    """大模型系统提示：人格层 + 硬性规则（安全条款）。"""
    d = persona_desc or "幽默风趣、爱玩梗的中文网民"
    return (
        "你是微博转发文案助手。「说什么」必须严格来自用户给的原文，"
        "「怎么说」由你的人格决定。\n"
        "【人格】%s\n"
        "【硬性规则】\n"
        "1. 只输出一句中文转发文案，不要解释、不要引号、不要话题标签、不要换行。\n"
        "2. 长度 10~40 字，禁止空泛（不得出现「这条微博」「这个动态」等弱指代）。\n"
        "3. 不得涉及政治敏感、灾难、攻击性内容。\n"
        "4. 不得复读原文整句。" % d
    )


def _build_prompt(text: str, persona_desc: str, k1: str, k2: str) -> str:
    """用户提示：原文 + 已抽取的主话题（供模型锚定）。"""
    topic = "、".join([x for x in (k1, k2) if x])
    return ("原文：%s\n已抽取主话题：%s\n请按人格生成一句转发文案。"
            % ((text or "")[:300], topic or "（无）"))


# ----------------------------------------------------------------- 自检
def self_check() -> dict:
    """AI 子系统自检（供启动日志/测试用）。"""
    out = {"ok": True, "checks": []}

    def _add(name, cond, detail=""):
        out["checks"].append({"name": name, "ok": bool(cond), "detail": detail})
        if not cond:
            out["ok"] = False

    try:
        cfg = load_config()
        _add("配置可读", isinstance(cfg, dict))
        _add("主路线合法", cfg.get("active_engine") in VALID_ENGINES,
             str(cfg.get("active_engine")))
        _add("备路线合法", cfg.get("fallback_engine") in VALID_ENGINES,
             str(cfg.get("fallback_engine")))
    except Exception as e:
        _add("配置可读", False, str(e))
    try:
        st = ai_status()
        _add("主路线探测", True, st.get("detail", "")[:100])
    except Exception as e:
        _add("主路线探测", False, str(e))
    try:
        r = generate_forward_text("今天天气真不错适合出门散步", persona="幽默")
        _add("文案生成链路", bool(r.get("text")),
             "engine=%s chain=%s" % (r.get("engine"), r.get("chain")))
    except Exception as e:
        _add("文案生成链路", False, str(e))
    return out


if __name__ == "__main__":
    print(json.dumps(self_check(), ensure_ascii=False, indent=2))