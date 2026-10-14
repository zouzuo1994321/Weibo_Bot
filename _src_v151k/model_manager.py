# -*- coding: utf-8 -*-
"""
本地大模型管理：自动建目录、下载 Qwen2.5-1.5B-Instruct GGUF、懒加载推理。

设计要点：
- 全离线：模型文件放在 data/models/，绝不打进 exe。
- 首次运行（用户机器、正常联网）时软件自动建目录并尝试下载；
  下载在后台线程进行，进度可经 get_state() 暴露给前端「转发设置」页展示。
- 推理通过 llama-cpp-python 调用本地 GGUF，CPU 可跑（n_gpu_layers=0）。
- 多镜像回退：HuggingFace -> ModelScope -> hf-mirror，任一可用即可。
"""
import os
import threading
import time

from paths import DATA_DIR
from config_manager import get_config
from logger import info, warn, error

# 【v1.2.2】模型目录可自定义。仍保留 MODELS_DIR / MODEL_PATH 两个模块常量
# 以兼容既有引用（导出、api_bridge 等），但**所有真正使用的地方一律走
# models_dir() / model_path() 函数** —— 因为用户可在运行中改路径，
# 静态常量拿不到改动后的值。
MODELS_DIR = os.path.join(DATA_DIR, "models")
MODEL_FILENAME = "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf"
MODEL_PATH = os.path.join(MODELS_DIR, MODEL_FILENAME)


def models_dir():
    """模型目录的当前生效路径（可被用户在存储管理里改）。"""
    try:
        from paths import get_models_dir
        d = get_models_dir()
    except Exception:
        d = MODELS_DIR
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def model_path():
    """模型文件的当前生效绝对路径。"""
    return os.path.join(models_dir(), MODEL_FILENAME)

# 多镜像下载源（按顺序回退）。文件名随 MODEL_FILENAME 同步。
DOWNLOAD_SOURCES = [
    f"https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/{MODEL_FILENAME}",
    f"https://modelscope.cn/models/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/master/{MODEL_FILENAME}",
    f"https://hf-mirror.com/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/{MODEL_FILENAME}",
]

_state = {
    "exists": False,
    "downloading": False,
    "progress": 0.0,          # 0..1
    "downloaded_bytes": 0,
    "total_bytes": 0,
    "speed": 0.0,             # bytes/s
    "error": "",
    "loaded": False,
    "engine": "llama.cpp · Qwen2.5-1.5B-Instruct (Q4_K_M)",
}
_state_lock = threading.Lock()
_model = None
_model_lock = threading.Lock()
_download_thread = None


def ensure_models_dir():
    return models_dir()


def model_available():
    return os.path.exists(model_path())


def get_state():
    with _state_lock:
        s = dict(_state)
    s["model_path"] = model_path()
    s["models_dir"] = models_dir()
    s["exists"] = os.path.exists(model_path())
    return s


def _set(**kw):
    with _state_lock:
        _state.update(kw)


def start_download():
    """后台启动模型下载（幂等）。返回 {ok, started/already/running}。"""
    global _download_thread
    if model_available():
        return {"ok": True, "already": True}
    with _state_lock:
        if _state["downloading"]:
            return {"ok": True, "running": True}
    ensure_models_dir()
    _download_thread = threading.Thread(target=_download, daemon=True)
    _download_thread.start()
    return {"ok": True, "started": True}


def _download():
    import urllib.request
    _mp = model_path()
    tmp_path = _mp + ".part"
    # 清理可能的残留
    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    except Exception:
        pass
    _set(downloading=True, progress=0.0, error="", downloaded_bytes=0, total_bytes=0)
    success = False
    try:
        for url in DOWNLOAD_SOURCES:
            try:
                info(f"尝试下载模型：{url}")
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    total = int(resp.headers.get("Content-Length", 0) or 0)
                    _set(total_bytes=total)
                    with open(tmp_path, "wb") as f:
                        dl = 0
                        t0 = time.time()
                        chunk = 1024 * 256
                        while True:
                            buf = resp.read(chunk)
                            if not buf:
                                break
                            f.write(buf)
                            dl += len(buf)
                            now = time.time()
                            _set(downloaded_bytes=dl,
                                 progress=(dl / total) if total else 0.0,
                                 speed=dl / (now - t0 + 1e-6))
                success = True
                break
            except Exception as e:
                warn(f"模型下载源失败 {url}：{e}")
                continue
        if success:
            os.replace(tmp_path, _mp)
            _set(progress=1.0, exists=True, downloading=False)
            info("模型下载完成")
        else:
            _set(error="所有下载源均失败（请检查网络，或手动将模型放入 data/models/）",
                 downloading=False)
    except Exception as e:
        error(f"模型下载异常：{e}")
        _set(error=str(e), downloading=False)
    finally:
        _download_thread = None


def load_model(n_ctx=2048, n_threads=None):
    """懒加载并缓存本地模型单例。失败返回 None。"""
    global _model
    with _model_lock:
        if _model is not None:
            return _model
        if not model_available():
            return None
        try:
            from llama_cpp import Llama
            if n_threads is None:
                n_threads = max(1, (os.cpu_count() or 4) // 2)
            _model = Llama(
                model_path=_mp,
                n_ctx=n_ctx,
                n_threads=n_threads,
                n_gpu_layers=0,      # CPU 推理；若有 GPU 可调高
                verbose=False,
                chat_handler=None,
            )
            _set(loaded=True, error="")
            info("本地大模型已加载（llama.cpp）")
            return _model
        except Exception as e:
            error(f"本地大模型加载失败：{e}")
            _set(error=f"模型加载失败：{e}", loaded=False)
            return None


# 默认采样参数（高多样性 + 稳健去复现）。
# 注意：llama-cpp-python 0.3.35 不支持 DRY（dry_multiplier/dry_base 等）参数，
# 因此「单条内部重复」防护改由更强 repeat_penalty + ai_generator 的首句截断实现。
DEFAULT_TEMPERATURE = 0.95   # 高温增加多样性
DEFAULT_TOP_P = 0.92
DEFAULT_MIN_P = 0.05         # 比 top-p 更稳健的现代默认
DEFAULT_REPEAT_PENALTY = 1.15


def generate(prompt, max_tokens=160, temperature=DEFAULT_TEMPERATURE,
             top_p=DEFAULT_TOP_P, min_p=DEFAULT_MIN_P,
             repeat_penalty=DEFAULT_REPEAT_PENALTY):
    """用本地模型对 prompt 做一次生成，返回纯文本或 None。"""
    llm = load_model()
    if llm is None:
        return None
    try:
        out = llm(
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            min_p=min_p,
            repeat_penalty=repeat_penalty,
            stop=["</s>", "\n\n"],
            echo=False,
        )
        text = out["choices"][0]["text"].strip()
        return text or None
    except Exception as e:
        error(f"本地模型推理失败：{e}")
        return None


def maybe_autostart():
    """软件启动/打开 AI 设置时：若 llama.cpp 路线已启用且模型缺失，自动开始下载。

    【v1.2.0】引擎判断改为读三引擎配置（data/ai_settings.json 的 active_engine）
    —— 只有主路线或备路线里含 local_gguf 时才自动下载；
    同时尊重 local_gguf.auto_download 开关（默认开，保持 v1.1.x 的自动行为）。
    老配置项 config.json的 ai_engine="model" 仍作为兜底判据。
    """
    try:
        want = False
        try:
            import ai_engine
            cfg = ai_engine.load_config()
            if cfg["local_gguf"].get("auto_download", True):
                want = (cfg.get("active_engine") == "local_gguf"
                        or cfg.get("fallback_engine") == "local_gguf")
        except Exception as e:
            debug(f"读取 AI 引擎配置失败，回退到老配置项判断：{e}")
            want = False

        cfg = get_config()
        if (not want) and cfg.get("ai_engine") == "model":
            want = True
        if (not want) or not cfg.get("ai_enabled"):
            return
        if not model_available() and not _state["downloading"]:
            info("已启用本地离线AI（llama.cpp）路线，开始后台下载模型")
            start_download()
    except Exception as e:
        warn(f"自动触发模型下载失败：{e}")
