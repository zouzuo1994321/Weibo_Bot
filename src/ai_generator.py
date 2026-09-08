# -*- coding: utf-8 -*-
"""
轻量化「AI」转发内容生成器（v2：主题 / 意图感知）。

设计原则（参考 AI 人格（role prompting）研究结论）：
- 人格（persona）只决定「语气 / 风格 / 视角」，即怎么说；不决定「说什么」。
- 「说什么」必须来自原文：用实体 / 名词抽取出真正的主话题，再套用与人格匹配、且
  与该话题语义相称的模板，从而避免「文不对题」的胡言乱语。
- 轻量意图识别（吐槽 / 分享 / 提问 / 炫耀 / 新闻 / 日常）用于挑选更贴合语境的模板，
  这对应研究中「动态角色调整应随上下文切换」的做法。

全程容错：即使分词 / 抽取 / 模板渲染异常，也返回一个可用文案，绝不抛异常。
"""
import random
import re

from config_manager import get_config
import model_manager

# 内置人格 -> 供本地大模型使用的「人格层」自然语言描述。
# 当 ai_engine="model" 时，内置人格也会被送往本地大模型，这里给出贴合的中文人设描述。
BUILTIN_PERSONA_DESC = {
    "幽默": "幽默风趣、爱玩梗、会整活的中文网民，说话轻松搞笑",
    "文艺": "文艺诗意、温柔治愈、喜欢用意象与短句表达情绪",
    "吃瓜": "爱围观八卦、爱凑热闹、说话逗趣的吃瓜群众",
    "专业点评": "理性客观、专业严谨、擅长用行业视角做点评的分析者",
    "暖心": "温暖贴心、乐于鼓励他人、充满正能量的暖心朋友",
}

# 常见停用词（过滤无意义词，提升关键词质量）
STOPWORDS = set(
    "的 了 和 是 在 我 有 也 就 不 人 都 一个 上 你 他 她 它 们 这 那 "
    "这个 那个 我们 你们 他们 自己 什么 怎么 这样 那样 因为 所以 但是 如果 "
    "可以 已经 还是 没有 不是 就是 这么 那么 一些 这种 那种 时候 现在 今天 "
    "会 要 说 看 去 来 到 把 被 让 给 等 与 及 或 啊 吧 呢 吗 哦 呀 啦 嘛 "
    "这条 微博 动态 一个 种 个 些 这 那 哪 该 该 该 该".split()
)

# 情感 / 评价词（用于 k3）。命中原文时优先采用，使评价贴合语境。
SENTIMENT_WORDS = [
    "离谱", "上头", "绝了", "绝绝子", "香", "坑", "神", "尴尬", "无语", "服了",
    "硬核", "治愈", "破防", "下头", "拉胯", "顶", "炸裂", "拿捏", "整活", "带感",
    "有料", "封神", "出圈", "塌房", "翻车", "真香", "emo", "社死", "上钟", "蚌埠",
    "麻了", "寄了", "上分", "封顶", "好家伙", "绝", "妙", "绝妙",
]

# 意图识别关键词（顺序即优先级）
INTENT_RULES = [
    ("提问", ["？", "?", "怎么", "为什么", "如何", "咋", "吗？", "吗?", "啥情况",
             "求科普", "在线等", "谁知道", "怎么看", "为什么", "请教", "求解"]),
    ("吐槽", ["垃圾", "差评", "踩雷", "坑", "无语", "离谱", "劝退", "气死", "退了",
             "拉胯", "下头", "翻车", "塌房", "破防", "服了", "麻了", "寄了", "蚌埠",
             "避雷", "烂", "崩", "糟", "忍不了", "受不了", "血压"]),
    ("炫耀", ["喜提", "拿下", "获奖", "终于", "上岸", "脱单", "暴富", "达成", "冲上",
             "满分", "封神", "上分", "秀", "财富", "加薪", "升职", "中标", "解锁",
             "通关", "搞定", "上岸", "晒", "报喜"]),
    ("分享", ["推荐", "安利", "分享", "宝藏", "教程", "测评", "种草", "整理", "合集",
             "盘点", "干货", "攻略", "好物", "经验", "安利", "记录", "整理", "总结"]),
    ("新闻", ["宣布", "发布", "曝光", "突发", "速报", "通报", "公示", "回应", "致歉",
             "声明", "重磅", "官宣", "上线", "开售", "召回", "停售", "整改", "立案"]),
]

# 内置人格模板：每个为一 dict，key 为意图分组，value 为模板列表。
# 占位符语义固定：{k1}=主话题（原文抽出的名词/实体），{k2}=相关词（次话题），{k3}=情感/评价词。
# 每个模板都把 {k1} 置于「主语 / 话题」位置，确保「文对题」。
PERSONAS = {
    "幽默": {
        "general": [
            "关于{k1}，我只想说：好家伙，这波属实给我整乐了😂",
            "看完{k1}我悟了，原来{k2}还能这么玩，服了",
            "今天的快乐是{k1}给的，尤其是{k2}那段绝了",
            "好家伙，{k1}直接把{k2}整成大型真香现场了🤣",
        ],
        "吐槽": [
            "{k1}？就这？我直接一个无语😅",
            "{k1}这事整的，属实{k3}，我人都麻了",
            "别人都在夸{k1}，只有我在{k2}里找不到北🤣",
            "{k1}这操作，{k2}直接给我看沉默了",
        ],
        "分享": [
            "刚看到{k1}，{k2}这块确实有点东西，安利给大伙",
            "关于{k1}，我的评价是：{k3}，建议码住",
            "分享个{k1}的{k2}，亲测有用，拿走不谢😎",
        ],
        "提问": [
            "所以{k1}到底啥情况？{k2}有没有懂哥科普一下",
            "{k1}这事我有点懵，{k2}怎么理解？在线等挺急的",
        ],
        "炫耀": [
            "{k1}拿下了，{k2}也跟着沾光，今天必须得瑟一下😎",
            "必须晒一下{k1}，{k2}直接封神，开心🥳",
        ],
        "新闻": [
            "{k1}有新动静了，{k2}相关的朋友可以关注下",
            "速报：{k1}刚刚更新，{k2}迎来变化",
        ],
    },
    "文艺": {
        "general": [
            "风把{k1}的故事吹进夜里，{k2}便成了温柔的注脚。",
            "关于{k1}，或许沉默比言语更接近答案。",
            "{k1}落进眼底，{k2}在时光里慢慢发酵成诗。",
        ],
        "吐槽": [
            "{k1}这场热闹，终究只剩{k3}的余温。",
            "本以为{k1}是惊喜，却只换来{k2}的一声叹息。",
        ],
        "分享": [
            "把{k1}的故事收好，{k2}也请温柔以待。",
            "愿你所念的{k1}，终会如{k2}般如约而至。",
        ],
        "提问": [
            "关于{k1}，谁又能给一个确凿的回答？{k2}不过是个问号。",
        ],
        "炫耀": [
            "{k1}如愿而至，{k2}在灯火里熠熠生辉。",
        ],
        "新闻": [
            "{k1}的消息掠过窗前，{k2}便有了新的注脚。",
        ],
    },
    "吃瓜": {
        "general": [
            "前排吃瓜！{k1}这事我盯很久了🍉",
            "蹲一个{k1}的后续，{k2}看起来有戏",
        ],
        "吐槽": [
            "震惊！{k1}居然整出{k3}这波操作，本瓜已裂开",
            "{k1}这瓜，吃得我{k2}都凉了",
        ],
        "分享": [
            "小小{k1}淡定吃瓜，{k2}的瓜真甜，安排",
        ],
        "提问": [
            "在线等挺急的，{k1}到底啥情况，{k2}求科普",
        ],
        "炫耀": [
            "{k1}我拿下了，{k2}都来沾沾喜气🍉",
        ],
        "新闻": [
            "速报！{k1}有新瓜，{k2}相关剧情更新中",
        ],
    },
    "专业点评": {
        "general": [
            "从行业视角看，{k1}反映出{k2}的新趋势，值得关注。",
            "{k1}的核心在于{k2}的落地，执行层面仍有优化空间。",
            "客观分析：{k1}具备一定代表性，{k2}是关键变量。",
        ],
        "吐槽": [
            "{k1}的表现难言理想，{k2}的短板暴露得相当明显。",
        ],
        "分享": [
            "关于{k1}，值得梳理的是{k2}背后的方法论。",
        ],
        "提问": [
            "围绕{k1}，仍需厘清{k2}的边界与前提。",
        ],
        "炫耀": [
            "{k1}的达成，验证了{k2}路径的可行性。",
        ],
        "新闻": [
            "简报：{k1}事件指向{k2}的结构性变化，建议持续跟踪。",
        ],
    },
    "暖心": {
        "general": [
            "看到{k1}觉得世界还是很温柔的，{k2}也请照顾好自己。",
            "生活偶尔辛苦，但{k1}告诉我们{k2}值得期待。",
            "把{k1}的温暖收好，{k2}也要好好的呀。",
        ],
        "吐槽": [
            "纵使{k1}让人{k3}，也别忘了{k2}里仍有微光。",
        ],
        "分享": [
            "愿{k1}里的每份{k2}都被温柔以待，抱抱你。",
        ],
        "提问": [
            "关于{k1}，如果你也困惑{k2}，别怕，慢慢来。",
        ],
        "炫耀": [
            "为你实现{k1}开心，{k2}也要继续闪闪发光呀✨",
        ],
        "新闻": [
            "{k1}传来好消息，{k2}也跟着被点亮了。",
        ],
    },
}

# 主话题抽取偏弱时的兜底模板（不依赖具体名词也能读通）
UNIVERSAL_SAFE = [
    "这条动态有点意思，码住慢慢看。",
    "刷到一条不错的微博，顺手转发一下。",
    "内容挺有价值，分享给同样感兴趣的人。",
]

# 主话题偏弱时的判定集合
WEAK_TOPIC = {"这条动态", "这个", "那个", "一个", "我们", "你们", "他们",
              "自己", "什么", "怎么", "这样", "那样", "这", "那"}

# 自定义人格「人设介绍」离线分析所用的声音预设库。
# 每个预设由若干关键词触发，决定开场白(op)、口头禅(catch)、表情(emoji)等风格要素。
VOICE_PRESETS = {
    "cute": {  # 猫娘 / 可爱
        "keywords": ["猫娘", "猫", "喵", "可爱", "萌", "软萌", "萝莉", "少女",
                     "甜", "宝宝", "主人", "小可爱", "黏人"],
        "op": "喵~ ", "catch": "喵~", "emoji": ["🐱", "✨", "💕"],
    },
    "anime": {  # 动漫 / 二次元
        "keywords": ["动漫", "二次元", "番", "动画", "中二", "御宅", "宅", "cos",
                     "acg", "漫画", "gal", "手游", "游戏", "纸片人"],
        "op": "", "catch": "呐", "emoji": ["✨", "🌟", "💫"],
    },
    "sharp": {  # 毒舌 / 犀利
        "keywords": ["毒舌", "犀利", "怼", "吐槽", "直", "刻薄", "阴阳", "嘴毒",
                     "毒", "犀利哥", "怼人", "不留情"],
        "op": "讲真，", "catch": "（摊手）", "emoji": ["😅", "🤷"],
    },
    "poetic": {  # 文艺 / 诗意
        "keywords": ["文艺", "诗意", "温柔", "治愈", "散文", "古风", "浪漫",
                     "唯美", "清冷", "感性"],
        "op": "", "catch": "。", "emoji": ["🌿", "🍃"],
    },
    "pro": {  # 专业 / 理性
        "keywords": ["专业", "严肃", "理性", "财经", "科技", "硬核", "客观",
                     "行业", "数据", "分析", "深度", "严谨"],
        "op": "", "catch": "。", "emoji": ["📊", "💡"],
    },
    "warm": {  # 暖心 / 治愈
        "keywords": ["暖心", "温暖", "治愈", "温柔", "鼓励", "抱抱", "贴心",
                     "阳光", "正能量"],
        "op": "", "catch": "呀", "emoji": ["💗", "🤗"],
    },
    "melon": {  # 吃瓜 / 围观
        "keywords": ["吃瓜", "八卦", "围观", "瓜", "看戏", "群众", "乐子", "吃瓜群众"],
        "op": "", "catch": "（吃瓜）", "emoji": ["🍉", "👀"],
    },
}

# 通用骨架：{k1}=主话题 {k2}=相关词 {k3}=评价词 {op}=开场 {catch}=口头禅 {emoji}=表情。
# 骨架保持「文对题」（k1 始终为主语/话题），风格由 voice 注入。
VOICE_SKELETONS = [
    "{op}关于{k1}，{k2}这块{k3}{catch}{emoji}",
    "{op}{k1}？{k2}确实有点东西{catch}{emoji}",
    "{op}看完{k1}，{k2}给我整得{k3}了{catch}{emoji}",
    "{op}聊到{k1}，{k2}还真挺{k3}的{catch}{emoji}",
    "{op}{k1}这事，{k2}的部分{k3}{catch}{emoji}",
]


def analyze_persona(desc: str) -> dict:
    """对自然语言「人设介绍」做轻量离线分析，产出声音要素（op/catch/emoji）。"""
    desc_l = (desc or "").lower()
    matched = []
    for key, p in VOICE_PRESETS.items():
        score = sum(1 for kw in p["keywords"] if kw.lower() in desc_l)
        if score:
            matched.append((score, key))
    matched.sort(reverse=True)
    voice = {"op": "", "catch": "", "emoji": "✨", "labels": []}
    if matched:
        primary = VOICE_PRESETS[matched[0][1]]
        voice["op"] = primary["op"]
        voice["catch"] = primary["catch"]
        voice["emoji"] = primary["emoji"][0]
        voice["labels"] = [k for _, k in matched]
    # 显式口头禅提取：如「口头禅：喵~」「喜欢说 绝了」
    m = re.search(r"(?:口头禅|喜欢说|常说|结尾[用使]|说[：:]\s*[\"']?)([^\"',，。\n]{1,12})",
                  desc or "")
    if m:
        catch = m.group(1).strip().strip("\"'：: ")
        if catch:
            voice["catch"] = catch
    return voice


def _fill_voice(voice: dict, k1: str, k2: str, k3: str) -> str:
    skel = random.choice(VOICE_SKELETONS)
    try:
        out = skel.format(op=voice.get("op", ""), k1=k1, k2=k2, k3=k3,
                          catch=voice.get("catch", ""), emoji=voice.get("emoji", ""))
    except Exception:
        out = f"转发：{k1} {k2}"
    return out.strip() or f"转发：{k1}"


def _tokenize_pos(text: str):
    """中文分词 + 词性标注：优先 jieba.posseg，失败退化为正则分词（flag='x'）。"""
    try:
        import jieba.posseg as pseg
        out = []
        for w, flag in pseg.cut(text):
            w = w.strip()
            if not w:
                continue
            if w in STOPWORDS:
                continue
            if re.fullmatch(r"[\s\W_]+", w):
                continue
            out.append((w, flag.lower()))
        return out
    except Exception:
        return [(w, "x") for w in re.findall(r"[\u4e00-\u9fa5a-zA-Z0-9]{2,}", text)]


def _extract_entities(text: str):
    """抽取显式实体：引号内、@昵称、#话题#、英文品牌词。这些最可能是真实话题。"""
    ents = []
    for m in re.findall(r"[《「『\"']([^》」』\"']+)[》」』\"']", text):
        if len(m) >= 2:
            ents.append(m)
    for m in re.findall(r"@([A-Za-z0-9_\u4e00-\u9fa5]+)", text):
        ents.append(m)
    for m in re.findall(r"#([^#]+)#", text):
        if len(m) >= 2:
            ents.append(m)
    # 英文 / 数字字母混合品牌词（如 cosplay、ELS、nikeeONLY）
    for m in re.findall(r"[A-Za-z][A-Za-z0-9]{1,20}", text):
        if len(m) >= 2:
            ents.append(m)
    # 去重保序
    seen, uniq = set(), []
    for e in ents:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    return uniq


def extract_topic(text: str) -> dict:
    """从原文抽取 {k1,k2,k3}。

    k1 = 主话题（最像「主语 / 主题」的名词或实体）
    k2 = 相关词（次话题，与主话题不同）
    k3 = 情感 / 评价词（命中原文中的情感词，否则中性兜底）
    """
    text = text or ""
    ents = _extract_entities(text)
    words = _tokenize_pos(text)

    noun_flags = ("n", "nz", "nr", "ns", "nt", "nl", "ng", "an", "vn")
    scored = []
    for w, flag in words:
        if len(w) < 2:
            continue
        if w in STOPWORDS:
            continue
        if re.fullmatch(r"\d+", w):
            continue
        score = 0
        if w in ents:
            score += 100          # 显式实体优先
        if flag in ("nz", "nr", "ns", "nt"):
            score += 60           # 专有名词
        elif flag.startswith("n"):
            score += 30           # 普通名词（最适合作话题）
        elif flag == "eng":
            score += 20           # 英文词（多为专有 / 品牌）
        elif flag.startswith("v"):
            score += 3            # 动词仅作兜底
        elif flag.startswith(("a", "m", "r", "d", "c", "u", "p", "b")):
            score += 0            # 形容词/数词/代词/副词等不作为话题
        else:
            score += 1
        score += len(w)           # 长词优先（更可能是核心概念）
        scored.append((score, w))

    # 按分数降序，并去重、剔除子串包含关系
    scored.sort(key=lambda x: -x[0])
    seen, ranked = set(), []
    for s, w in scored:
        if w in seen:
            continue
        if any(w in c or c in w for c in ranked):
            continue
        seen.add(w)
        ranked.append(w)

    k1 = ranked[0] if ranked else (ents[0] if ents else "")
    k2 = ranked[1] if len(ranked) > 1 else (ents[1] if len(ents) > 1 else k1)
    if not k2 or k2 == k1:
        k2 = k1

    # 情感 / 评价词
    k3 = ""
    for sw in SENTIMENT_WORDS:
        if sw in text:
            k3 = sw
            break
    if not k3:
        # 原文无显式情感词时，用中性评价词兜底，避免「我的评价是：这波操作」这类生硬表达
        k3 = random.choice(["值得一说", "有点意思", "有点东西", "挺上头", "可圈可点"])

    if not k1:
        k1 = "这条动态"
    return {"k1": k1, "k2": k2, "k3": k3}


def detect_intent(text: str) -> str:
    """轻量意图识别：返回 提问 / 吐槽 / 炫耀 / 分享 / 新闻 / general。"""
    for name, kws in INTENT_RULES:
        for kw in kws:
            if kw in text:
                return name
    return "general"


def _fill(tpl: str, k1: str, k2: str, k3: str) -> str:
    """安全填充模板占位符，保证不残留 {k1} 等未替换标记。"""
    try:
        out = tpl.format(k1=k1, k2=k2, k3=k3)
    except Exception:
        out = tpl.replace("{k1}", k1).replace("{k2}", k2).replace("{k3}", k3)
    # 兜底：若仍有未替换占位符，用主话题覆盖，避免半成品文案
    out = out.replace("{k1}", k1).replace("{k2}", k2).replace("{k3}", k3)
    return out.strip() or f"转发：{k1} {k2}"


def generate(text: str, persona: str = "幽默", custom_templates: list = None) -> str:
    """根据被转发内容与人设生成转发文案。

    引擎路由（配置项 ai_engine）：
    - "model"：优先使用本地大模型（llama.cpp + Qwen2.5-1.5B GGUF）做分层生成；
               未就绪 / 失败 / 无模型时自动回退到下方规则引擎，保证转发不中断。
    - "rule"（默认）：轻量规则引擎（jieba + 模板）。
    全程容错：即使抽取 / 渲染 / 推理异常，也返回一个可用文案。
    """
    try:
        topic = extract_topic(text)
        k1, k2, k3 = topic["k1"], topic["k2"], topic["k3"]

        # ---- 主路线：本地大模型 ----
        cfg = get_config()
        if cfg.get("ai_engine", "rule") == "model":
            if isinstance(custom_templates, str) and custom_templates.strip():
                persona_desc = custom_templates.strip()
            else:
                persona_desc = BUILTIN_PERSONA_DESC.get(persona, persona)
            try:
                out = model_generate(text, persona_desc, k1, k2, persona_key=persona)
                if out:
                    return out
            except Exception as e:
                warn(f"本地大模型生成失败，回退规则引擎：{e}")

        # ---- 备路线：轻量规则引擎（始终可用） ----
        if custom_templates:
            # 新版：人设介绍（自然语言描述），本地离线分析后生成贴合人设的回复
            if isinstance(custom_templates, str):
                if k1 in WEAK_TOPIC:
                    return random.choice(UNIVERSAL_SAFE)
                voice = analyze_persona(custom_templates)
                return _fill_voice(voice, k1, k2, k3)
            # 兼容旧版：模板列表
            templates = [t for t in custom_templates if t and str(t).strip()]
            if not templates:
                templates = UNIVERSAL_SAFE
            return _fill(random.choice(templates), k1, k2, k3)

        persona_def = PERSONAS.get(persona) or PERSONAS["幽默"]
        intent = detect_intent(text)
        group = persona_def.get(intent) or persona_def.get("general") or []
        if not group:
            group = [t for ts in persona_def.values() for t in ts]
        # 主话题偏弱时退回通用安全模板，避免「关于{这/那}」式空洞
        if k1 in WEAK_TOPIC:
            group = UNIVERSAL_SAFE
        return _fill(random.choice(group), k1, k2, k3)
    except Exception:
        return f"转发：{(text or '这条微博')[:40]}"


def model_generate(text: str, persona_desc: str, k1: str, k2: str = "",
                  persona_key: str = "") -> str:
    """用本地大模型生成转发文案（Character.AI 分层 Prompt + 三层去重 + 安全条款）。

    三层去重：
      ① 单条内部重复 —— 高温度多样采样 + 首句截断（见 model_manager 默认采样参数）；
      ② 跨条重复 —— 生成前把该人格最近 N 条历史文案 + 同话题已发文案注入 prompt；
      ③ 结构级随机化 —— 每次随机指定句长区间 / 开头方式 / 是否带 emoji / 是否用口语插入语。

    安全条款写进 [System - 人格层] 下的「硬性规则」，约束政治/灾难/攻击性等内容。

    返回清洗后的文案；失败 / 无效返回空串，交由规则引擎兜底。
    """
    if not model_manager.model_available():
        return ""

    # ---------- 层级 ②：跨条去重 —— 注入历史已发文案 ----------
    dedup_block = ""
    try:
        from history_db import get_history_db
        db = get_history_db()
        # 同话题关键词：优先主话题，避免弱话题（如「这条动态」）造成误匹配
        topic_for_dedup = k1 if k1 not in WEAK_TOPIC else (
            k2 if k2 not in WEAK_TOPIC else "")
        recent = db.get_recent_ai_texts(persona_key, limit=6)
        same_topic = db.get_recent_same_topic(topic_for_dedup, limit=6)
        seen, past = set(), []
        for t in recent + same_topic:
            if t and t not in seen:
                seen.add(t)
                past.append(t)
        if past:
            lines = "\n".join(f"- {t}" for t in past[:10])
            dedup_block = (
                "[Dedup - 已发过的内容]\n"
                "以下内容你已经说过，绝对不要再用相同句式、相同开头或相同表达：\n"
                f"{lines}\n"
            )
    except Exception as e:
        warn(f"跨条去重上下文查询失败（忽略）：{e}")

    # ---------- 层级 ③：结构级随机化 ----------
    # 句长区间：10-15 字 / 20-40 字
    lo, hi = random.choice([(10, 15), (20, 40)])
    n = random.randint(lo, hi)

    # 开头方式
    openings = [
        ("疑问", "用疑问句式开头（结尾带问号）"),
        ("感叹", "用感叹句式开头（带感叹语气）"),
        ("陈述", "用平实陈述句开头，不绕弯子"),
        ("省略主语", "省略主语，直接切入内容本身"),
        ("接口头禅", "以你的口头禅或标志性口头语直接开头"),
    ]
    _, opening_inst = random.choice(openings)

    # 是否带 emoji / 带几个
    use_emoji = random.random() < 0.5
    emoji_count = random.randint(1, 2) if use_emoji else 0

    # 是否使用口语插入语（如「说真的」「讲道理」）
    use_interj = random.random() < 0.4
    interj = random.choice(["说真的", "讲道理", "说句实话", "不夸张地讲"]) if use_interj else ""

    # 从人设描述提取「口头禅」（与规则引擎 analyze_persona 对齐）
    catch = ""
    m = re.search(r"(?:口头禅|喜欢说|常说|结尾[用使]|说[：:]\s*[\"']?)([^\"',，。\n]{1,12})",
                  persona_desc or "")
    if m:
        catch = m.group(1).strip().strip("\"'：: ")

    persona_line = f"你是{persona_desc}。说话风格自然贴合你的人设。"
    if catch:
        persona_line += f"你习惯在文案里自然地带上口头禅「{catch}」。"
    persona_line += "绝不出戏，绝不提及自己是AI或机器人。"

    # 结构指令拼接
    struct = [f"- 长度约 {n} 字"]
    struct.append(f"- 开头方式：{opening_inst}")
    if use_interj:
        struct.append(f"- 可在句中自然插入口语化插入语「{interj}」")
    if use_emoji:
        struct.append(f"- 文末可带 {emoji_count} 个 emoji，自然不堆砌")
    else:
        struct.append("- 不要使用 emoji")

    prompt = (
        "[System - 人格层]\n"
        f"{persona_line}\n\n"
        "[硬性规则]\n"
        "- 绝不评论政治、灾难、命案、民族宗教、性别对立话题\n"
        "- 遇到上述内容：输出中性转发或直接说\"转了\"\n"
        "- 绝不辱骂、嘲讽、引战、站队；不使用\"恶心\"\"垃圾\"\"跪了\"等攻击性词汇\n"
        "- 不 @ 任何人、不发起任何互动号召（如\"快去看\"\"扩散\"）\n"
        "- 不编造原文没有的事实\n\n"
        "[Context - 内容层]\n"
        f"要转发的微博原文：「{text}」\n\n"
        f"{dedup_block}\n"
        "[Task - 任务层]\n"
        "用一句话写一条转发文案，要求：\n"
        "- 紧扣原文主题，复述原文核心信息（不要编造原文没有的事实）\n"
        "- 符合你的人设语气\n"
        + "\n".join(struct) + "\n"
        "- 直接输出文案本身，不要任何解释、不要带引号、不要多行\n"
    )
    # 采样参数走 model_manager 默认（高温度多样 + 强 repeat_penalty，见层级①）
    raw = model_manager.generate(prompt, max_tokens=96)
    if not raw:
        return ""
    return _clean_model_output(raw)


def _clean_model_output(raw: str) -> str:
    """清洗模型输出：

    - 取首个非空行；去掉常见引导前缀（「转发：」「文案：」等）；
    - 只保留第一句话（遇到句末标点即截断），避免模型啰嗦/重复刷屏，也顺手去掉模型自作主张加的 #话题#；
    - 去引号、控长度（2~80 字）；无效则返空交由规则引擎兜底。
    """
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return ""
    out = lines[0]
    for pre in ("转发文案：", "文案：", "转发：", "回复：", "AI：", "答案："):
        if out.startswith(pre):
            out = out[len(pre):].strip()
    # 仅保留第一句话（含句末标点）
    m = re.search(r"^[^。！？!?…]+[。！？!?…]", out)
    if m:
        out = m.group(0)
    out = out.strip("\"'「」『』“”")
    out = out.replace("\n", " ").strip()
    if not (2 <= len(out) <= 80):
        return ""
    return out


def list_personas() -> list:
    return list(PERSONAS.keys())
