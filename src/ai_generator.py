# -*- coding: utf-8 -*-
"""
轻量化「AI」转发内容生成器。
说明：本模块为规则 + 内容分析（关键词抽取 + 人格模板组合）的轻量方案，
不依赖任何外部大模型服务，可在单机离线运行。如需接入真实大模型，
可在 generate() 中替换为模型推理调用。
"""
import random
import re

# 常见停用词（过滤无意义词，提升关键词质量）
STOPWORDS = set(
    "的 了 和 是 在 我 有 也 就 不 人 都 一 一个 上 都 你 他 她 它 们 这 那 "
    "这个 那个 我们 你们 他们 自己 什么 怎么 这样 那样 因为 所以 但是 如果 "
    "可以 已经 还是 没有 不是 就是 这么 那么 一些 这种 那种 时候 现在 今天 "
    "会 要 说 看 去 来 到 把 被 让 给 等 与 及 或 啊 吧 呢 吗 哦 呀 啦 嘛 "
    "a an the of to and is are in on for with this that it its".split()
)

# 内置人格模板，{k1}{k2}{k3} 会被关键词替换，{em} 为语气词
PERSONAS = {
    "幽默": [
        "关于{k1}，我只想说：笑死，根本停不下来😂",
        "看完{k1}我悟了，原来{k2}还能这么玩，服了",
        "别人都在聊{k1}，只有我在{k2}里找不到北🤣",
        "{k1}？这波操作我给满分，扣一分怕你骄傲",
        "今天的快乐是{k1}给的，尤其是{k2}那段绝了",
    ],
    "文艺": [
        "风把{k1}的故事吹进夜里，{k2}便成了温柔的注脚。",
        "关于{k1}，或许沉默比言语更接近答案。",
        "{k1}落进眼底，{k2}在时光里慢慢发酵成诗。",
        "愿你所念的{k1}，终会如{k2}般如约而至。",
        "有些{k1}只能藏在心底，像{k2}一样安静生长。",
    ],
    "吃瓜": [
        "前排吃瓜！{k1}这事我盯很久了🍉",
        "蹲一个{k1}的后续，{k2}看起来有戏",
        "震惊！{k1}居然和{k2}有关，本瓜已裂开",
        "小小{k1}淡定吃瓜，{k2}的瓜真甜",
        "在线等挺急的，{k1}到底啥情况，{k2}求科普",
    ],
    "专业点评": [
        "从行业视角看，{k1}反映出{k2}的新趋势，值得关注。",
        "{k1}的核心在于{k2}的落地，执行层面仍有优化空间。",
        "客观分析：{k1}具备一定代表性，{k2}是关键变量。",
        "简报：{k1}事件指向{k2}的结构性变化，建议持续跟踪。",
        "专业角度，{k1}与{k2}的关联度较高，需理性看待。",
    ],
    "暖心": [
        "看到{k1}觉得世界还是很温柔的，{k2}也请照顾好自己。",
        "愿{k1}里的每份{k2}都被温柔以待，抱抱你。",
        "生活偶尔辛苦，但{k1}告诉我们{k2}值得期待。",
        "把{k1}的温暖收好，{k2}也要好好的呀。",
        "希望你也能被{k1}治愈，像{k2}一样慢慢发光。",
    ],
}


def _tokenize(text: str):
    """中文分词：优先 jieba，失败（如打包环境缺失词典）退化为正则分词。"""
    try:
        import jieba
        words = []
        for w in jieba.lcut(text):
            w = w.strip()
            if not w:
                continue
            if w in STOPWORDS:
                continue
            if re.fullmatch(r"[\s\W_]+", w):
                continue
            words.append(w)
        return words
    except Exception:
        # jieba 不可用时的兜底：直接按连续中英文字符切词
        return re.findall(r"[\u4e00-\u9fa5a-zA-Z0-9]{2,}", text)


def extract_keywords(text: str, top_n: int = 3) -> list:
    """基于词频的关键词抽取（jieba 分词 + 词频排序，优先长词）。"""
    from collections import Counter
    words = _tokenize(text)
    if not words:
        en = re.findall(r"[a-zA-Z0-9]{2,}", text)
        return en[:top_n] or ["这条微博"]
    cnt = Counter(words)
    # 按 词长降序、词频降序 排序，长词优先
    ranked = sorted(cnt.items(), key=lambda kv: (len(kv[0]), kv[1]), reverse=True)
    chosen = []
    for w, _ in ranked:
        if len(w) < 2:
            continue
        # 跳过与已选关键词存在子串包含的情况
        if any(w in c or c in w for c in chosen):
            continue
        chosen.append(w)
        if len(chosen) >= top_n * 3:
            break
    return (chosen[:top_n] or ["这条动态"])


def generate(text: str, persona: str = "幽默", custom_templates: list = None) -> str:
    """根据被转发内容与人设生成转发文案。

    全程容错：即使 jieba / 模板渲染异常，也返回一个可用文案（绝不抛异常），
    保证 AI 增强转发不会因为生成器内部错误而整体失败。
    """
    try:
        kws = extract_keywords(text, 3)
        while len(kws) < 3:
            kws.append(kws[-1] if kws else "这条动态")
        k1, k2, k3 = kws[0], kws[1], kws[2]

        templates = custom_templates
        if not templates:
            templates = PERSONAS.get(persona, PERSONAS["幽默"])
        if not templates:
            return f"转发：{k1} {k2}"
        tpl = random.choice(templates)
        try:
            out = tpl.format(k1=k1, k2=k2, k3=k3)
        except Exception:
            out = tpl.replace("{k1}", k1).replace("{k2}", k2).replace("{k3}", k3)
        return out.strip() or f"转发：{k1} {k2}"
    except Exception:
        return f"转发：{(text or '这条微博')[:40]}"


def list_personas() -> list:
    return list(PERSONAS.keys())
