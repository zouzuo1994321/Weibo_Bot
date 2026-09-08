# 微博bot小助手 · AI 人格模块技术说明

> 适用版本：v1.0.1（内部 2609080001）
> 目的：独立说明「AI 转发人格」模块的实现原理，便于后续迭代研究。
> 阅读对象：需要对生成逻辑做二次开发 / 替换推理引擎的开发者。

---

## 0. 一句话结论（先说清楚当前引擎真身）

**当前版本的 AI 人格模块并没有使用任何神经网络推理引擎。**

界面上标注的「本地离线轻量引擎」，实际由两部分构成：

1. **jieba 0.42.1** —— 中文分词 + 词性标注（`jieba.posseg`），用来从原文里抽出主话题 / 次话题 / 情感词；
2. **纯 Python 规则** —— 关键词匹配 + 正则表达式 + 模板字符串填充（`re` / `random`）。

已核实：`requirements.txt` 中只有 `jieba` 一个推理相关依赖；`src/` 全目录未引用 `onnxruntime` / `tokenizers` / `numpy` / `torch` / `transformers`；打包 venv（`weibobot`）里推理相关包也仅有 `jieba`。

所以「离线、无需联网、不吃 GPU、内存极小」是真的，但它**不是大模型**，而是一套**启发式（规则）生成器**。这一点对后续迭代路线选择至关重要（详见第 4 章）。

---

## 1. 当前引擎详解

### 1.1 技术栈

| 组件 | 版本 / 形式 | 作用 |
|------|------------|------|
| `jieba` | 0.42.1（pip，唯一推理依赖） | 中文分词、词性标注、关键词抽取 |
| `re` | Python 标准库 | 口头禅正则提取、实体识别 |
| `random` | Python 标准库 | 多模板随机选取，避免每条文案雷同 |
| 内置词典 / 规则表 | 代码常量 | `SENTIMENT_WORDS` / `INTENT_RULES` / `PERSONAS` / `VOICE_PRESETS` 等 |

**无**：`onnxruntime`、`tokenizers`、`numpy`、`torch`、`transformers`、`sentence-transformers`、`llama` 等。

### 1.2 生成管线（pipeline）

```
被转发微博文本
   │
   ▼
extract_topic(text)  ──►  {k1, k2, k3}
   │   jieba.posseg 词性标注 + 实体打分
   │   k1=主话题(核心名词/实体)  k2=次话题  k3=情感/评价词
   │
   ▼
detect_intent(text)  ──►  意图 ∈ {提问,吐槽,炫耀,分享,新闻,general}
   │   关键词规则匹配（INTENT_RULES，按优先级）
   │
   ▼
选模板
   ├─ 内置人格：PERSONAS[persona][intent]   （意图分组模板列表）
   ├─ 自定义人格(字符串人设描述)：analyze_persona(desc) → _fill_voice()
   └─ 自定义人格(旧版模板列表)：随机选模板 → _fill()
   │
   ▼
渲染：模板.format(k1,k2,k3[,op,catch,emoji]) + 占位符兜底替换
   │
   ▼
返回文案（全程 try/except，异常降级为普通转发或安全文案）
```

**主话题偏弱保护**：若 `k1` 落入 `WEAK_TOPIC` 集合（如「这个」「那个」「我们」），或整段无有效名词，则直接返回 `UNIVERSAL_SAFE` 兜底模板，避免生成「关于这个，我只想说……」这类空洞文案。

### 1.3 占位符语义（文对题的关键）

所有模板统一使用三个占位符：

- `{k1}` = **主话题**：原文抽出的核心名词 / 实体，**始终处于主语/话题位** → 保证「文对题」；
- `{k2}` = **相关词**：次话题 / 关联对象；
- `{k3}` = **情感/评价词**：命中 `SENTIMENT_WORDS`（离谱/绝了/治愈/翻车……）时采用，否则用中性兜底词。

设计原则（参考 role prompting 研究结论）：**人格只决定「语气 / 风格 / 视角」（怎么说），「说什么」必须来自原文**。`k1/k2/k3` 永远来自被转发的微博，人格只负责修饰。

### 1.4 内置人格一览

| 人格 | 意图分组 | 风格 |
|------|---------|------|
| 幽默 | general/吐槽/分享/提问/炫耀/新闻 | 玩梗、真香、整活 |
| 文艺 | 同上 | 诗意、温柔、治愈 |
| 吃瓜 | 同上 | 围观、八卦、前排 |
| 专业点评 | 同上 | 行业视角、客观分析 |
| 暖心 | 同上 | 鼓励、抱抱、正能量 |

### 1.5 代码位置速查表

**核心生成 `src/ai_generator.py`**

| 函数 / 常量 | 行号 | 说明 |
|------------|------|------|
| `WEAK_TOPIC` | 185 | 主话题偏弱判定集合 |
| `UNIVERSAL_SAFE` | 178 | 兜底模板 |
| `SENTIMENT_WORDS` | 27 | 情感/评价词典（k3 来源） |
| `INTENT_RULES` | 35 | 意图识别关键词（优先级顺序） |
| `PERSONAS` | 53 | 5 个内置人格的意图分组模板 |
| `VOICE_PRESETS` | 190 | 自定义人格 7 类声音预设（cute/anime/sharp/poetic/pro/warm/melon） |
| `VOICE_SKELETONS` | 229 | 通用骨架（k1 恒为主语） |
| `analyze_persona()` | 238 | 解析人设描述 → 声音要素 |
| `_fill_voice()` | 264 | 用骨架+声音要素渲染 |
| `_tokenize_pos()` | 274 | jieba 词性标注（带退化兜底） |
| `_extract_entities()` | 293 | 引号/@/#/英文品牌词实体抽取 |
| `extract_topic()` | 317 | 产出 {k1,k2,k3} |
| `detect_intent()` | 386 | 意图分类 |
| `_fill()` | 395 | 安全填充模板占位符 |
| `generate()` | 406 | 总入口（含自定义人格分支） |

**桥接 `src/api_bridge.py`**

| 函数 | 行号 | 说明 |
|------|------|------|
| `preview_ai()` | 534 | 前端「生成预览」后端入口 |
| `save_custom_persona()` | 543 | 保存自定义人格（存自然语言描述） |
| `delete_custom_persona()` | 591 | 删除自定义人格 |
| 转发调用点 | ~485 | `ai_generate(chosen["text"], persona, custom)` |

**前端 `ui/app.js`**

| 函数 | 行号 | 说明 |
|------|------|------|
| `renderAI()` | 525 | 渲染人格列表 + 标题右侧模型状态徽章 |
| `selectPersona()` | 570 | 选中人格（自定义则载入编辑区） |
| `savePersona()` | 602 | 保存当前生效人格 |
| `previewAI()` | 608 | 调用 preview_ai 生成预览 |
| `saveCustom()` | 619 | 保存自定义人格 |
| `deleteCustom()` | 636 | 删除自定义人格 |

**前端结构 `ui/index.html`**

- `#view-ai`：AI 人格整页（约 200–248 行）
- `#aiModelStatus`：标题右侧模型/状态徽章
- `#personaList` / `#currentPersonaLabel`：人格列表 / 当前已选
- `#aiTest` / `#aiPreview`：原文输入 / 生成预览
- `#custName` / `#custDesc` / `#custEditHint`：自定义人格的名称 / 人设介绍 / 编辑提示

---

## 2. 自定义人格如何运行（端到端链路）

### 2.1 数据流全景

```
用户界面                         后端                                  持久化 / 运行时
─────────                       ──────                                ───────────────
#custName + #custDesc
      │ 点「保存自定义人格」
      ▼
saveCustom()  ──apiCall("save_custom_persona", name, JSON.stringify(desc))──►
                                                              save_custom_persona()
                                                              │  list→join("\n") 归一为文本
                                                              │  custom_personas[name] = desc(字符串)
                                                              ▼
                                                        cfg.set("custom_personas", ...)  ──► config.json
      │ 在人格列表点选该自定义人格
      ▼
selectPersona(name)  ── 载入 #custName/#custDesc（可再次编辑覆盖）
      │ 点「保存当前选择」
      ▼
savePersona()  ──apiCall("save_config",{persona:name})──►  cfg.set("persona", name)  ──► config.json（当前生效人格）
      │ 轮询 / 立即转发触发
      ▼
run_once()  ──ai_generate(text, persona, custom)──►  generate(text, persona, custom)
                                                      │  custom 为 str → analyze_persona(custom)
                                                      │                         ↓
                                                      │                   _fill_voice(voice,k1,k2,k3)
                                                      ▼
                                                  返回贴合人设的转发文案
```

### 2.2 存储形态

- 自定义人格以 **字符串** 形式存于 `config.json`：`custom_personas = { "猫娘": "你是猫娘，说话软萌…口头禅：喵呜~", ... }`。
- 兼容旧版：若传入的是模板列表（`list`），`save_custom_persona` 会 `join("\n")` 归一为文本再存储（见第 543 行），保证历史数据不崩。

### 2.3 离线分析逻辑 `analyze_persona(desc)`

1. **风格匹配**：遍历 `VOICE_PRESETS` 的 7 类，对每类 `keywords` 计数命中；取命中最高的一类作为主风格（primary），并收集所有命中标签。
   - cute（猫娘/可爱/软萌）、anime（动漫/二次元/番）、sharp（毒舌/怼/吐槽）、poetic（文艺/诗意/治愈）、pro（专业/理性/财经）、warm（暖心/温暖/正能量）、melon（吃瓜/八卦/围观）。
2. **开场 / 口头禅 / 表情**取自该预设的 `op` / `catch` / `emoji`。
3. **显式口头禅覆盖**：正则 `口头禅|喜欢说|常说|结尾[用使]|说[：:]` 提取用户写的口头禅，覆盖预设默认 `catch`。

返回 `voice = {op, catch, emoji, labels}`。若没有任何关键词命中，`voice` 退化为「无风格修饰」的中性渲染。

### 2.4 渲染逻辑 `_fill_voice(voice, k1, k2, k3)`

- 随机选 `VOICE_SKELETONS` 中一个通用骨架（5 个，保证 `k1` 恒为主语）：
  - `{op}关于{k1}，{k2}这块{k3}{catch}{emoji}`
  - `{op}{k1}？{k2}确实有点东西{catch}{emoji}`
  - ……
- `skel.format(op=…, k1=k1, k2=k2, k3=k3, catch=…, emoji=…)`。

### 2.5 工作示例

- 原文：「周杰伦演唱会门票又抢不到，气死我了，这就离谱。」
- 人设描述：「你是猫娘，说话软萌可爱，喜欢用喵~结尾，对二次元动漫很熟悉。口头禅：喵呜~」
- 抽取：`k1=周杰伦` `k2=演唱会` `k3=离谱`（命中情感词典）
- 分析：命中 `cute`（猫娘/可爱）→ `op="喵~ "` `catch="喵呜~"` `emoji="🐱"`
- 输出：`喵~ 周杰伦这事，演唱会的部分离谱喵呜~🐱`

---

## 3. 提示词（人设描述）如何编写与运行

### 3.1 当前「提示词」的本质

这里没有给大模型的 prompt，而是一段**自然语言「人设介绍」文本**，被 `analyze_persona` 做关键词 + 正则解析。解析维度只有两个：

1. **风格关键词** —— 命中 7 类预设之一；
2. **口头禅** —— 正则提取。

> 也就是说：写得再长，只要没命中关键词、没写「口头禅：」，引擎也只会做中性渲染。

### 3.2 编写规范（提升命中率）

| 维度 | 写法 | 效果 |
|------|------|------|
| 风格关键词 | 从 7 类 `keywords` 里挑词：「猫娘/喵/可爱」「动漫/二次元/番」「毒舌/怼/吐槽」「文艺/诗意/治愈」「专业/理性/财经」「暖心/治愈/正能量」「吃瓜/八卦/围观」 | 决定整体语气 |
| 口头禅 | 显式写「口头禅：喵呜~」「喜欢说 绝了」 | 覆盖默认 catch，出现在句尾 |
| 开场白 | 命中预设的 `op`（如毒舌的「讲真，」） | 出现在句首 |
| emoji 倾向 | 当前 emoji 由预设决定，**自定义 emoji 暂不被解析** | —— |
| 内容题材 | 人设里的「对二次元很熟」等，**不改变文案说什么**（k1/k2/k3 仍来自原文） | 仅影响语气 |

编写模板示例：

```
你是猫娘，说话软萌可爱，喜欢用喵~结尾，对二次元动漫很熟悉。口头禅：喵呜~
你是毒舌财经老哥，说话犀利直接，爱怼人。口头禅：（摊手）
你是文艺青年，喜欢诗意温柔的表达，治愈系。
```

### 3.3 运行与调试

- **实时预览**：`ui/index.html` 的「实时预览」区粘贴原文 → 点「生成预览」 → `previewAI()` → `api_bridge.preview_ai()` → `ai_generate()`。改完人设即时看效果，无需真转发。
- **后端降级日志**：若生成异常，转发流程会 `warn("UID=... AI 文案生成失败，降级为普通转发")` 并退回纯转发，不会崩。
- **单测**：`python -c "from ai_generator import generate; print(generate(text, '自定义', desc))"` 直接在 venv 里跑。

### 3.4 迭代时可研究的点（现状短板）

1. `analyze_persona` 只看关键词+正则，**无法理解复杂/混合人设**（如「表面高冷内心柔软的学姐」）。
2. `VOICE_SKELETONS` 是**固定骨架**，"说什么"完全被原文 `k1/k2/k3` 锁死，人设无法创造新信息。
3. 想要「人设真正参与创作内容」→ **必须引入生成式模型**（见第 4 章）。
4. 情感词 `SENTIMENT_WORDS` 是静态词典，可改为 jieba 情感分析或本地小模型打分。

---

## 4. 可安装的本地离线轻量引擎（迭代升级路线）

> 运行环境：Windows 11 + Python 3.13（managed venv `weibobot`）。以下引擎**全部支持纯本地离线推理，无需联网调用云端 API**。

### 4.1 选型总览

| 引擎 | 类型 | 是否需要下载模型 | 典型硬件 | 适合解决 |
|------|------|----------------|---------|---------|
| **llama-cpp-python (GGUF)** | 本地小模型推理 | 是（GGUF 文件，几百 MB～几 GB） | CPU 可跑，支持 GPU offload | 「真正人格生成」（风格+内容都由模型决定） |
| **onnxruntime + ONNX 模型** | 推理运行时 | 是（ONNX 文件） | CPU/GPU | 语义向量 / 导出式小生成模型，保留「无 PyTorch」约束 |
| **tokenizers** | 分词器 | 否（仅库） | 任意 | 配合 onnxruntime 做 tokenizer |
| **sentence-transformers / FlagEmbedding** | 句向量 | 是（如 MiniLM） | CPU 可跑 | 人设语义匹配、聚类 |
| **transformers** | 模型库 | 是 | 需 torch 较重 | 研究期快速试错 |
| **OpenVINO** | Intel 推理加速 | 是 | Intel CPU/GPU | 加速 onnx/小模型 |
| **chatglm.cpp / fastllm** | 国产小模型本地推理 | 是（GGML/GGUF） | CPU/GPU | 国产小模型离线生成 |
| **规则增强（不换引擎）** | 纯代码 | 否 | 任意 | 扩展 VOICE_PRESETS / 增加意图 / 更细情感分析 |

### 4.2 详细清单与安装

#### ① llama-cpp-python（GGUF）—— 推荐做「真·人格生成」
```bash
pip install llama-cpp-python
# 模型（自行下载，放 data/models/，不进 exe）：
#   Qwen2.5-0.5B-Instruct-Q4_K_M.gguf   （极轻，CPU 流畅）
#   Qwen2.5-1.5B-Instruct-Q4_K_M.gguf   （效果/体积平衡，推荐）
#   Qwen2.5-3B-Instruct-Q4_K_M.gguf     （更好，略吃资源）
```
- 把人设描述 + 原文拼成 prompt 交给本地模型，可同时决定「语气」和「说什么」。
- 集成点：在 `generate()` 内，当 `custom_templates` 为字符串时，改为调用本地模型而非 `analyze_persona` + `_fill_voice`。
- 优势：全离线、CPU 可跑、体积小、中文社区模型丰富。

#### ② onnxruntime + ONNX 模型（贴合原计划「onnxruntime + tokenizers + numpy，不引入 PyTorch」）
```bash
pip install onnxruntime tokenizers numpy
```
用途二选一：
- **文本向量**：MiniLM INT8 量化 ONNX → 人设语义匹配、相似度聚类（替代关键词命中，理解混合人设）。
- **小生成模型**：把 Qwen/TinyLlama 导出为 ONNX → 离线生成文案，继续保留「轻量、无 PyTorch」约束。

#### ③ sentence-transformers / FlagEmbedding
```bash
pip install sentence-transformers   # 需 torch 后端，较重
# 或更轻的 FlagEmbedding（BGE 系列）
```
用于「人设描述 → 向量 → 最近邻匹配最贴预设」，比关键词更鲁棒。

#### ④ transformers（研究期试错）
```bash
pip install transformers torch
```
快速验证想法，但 torch 体积大、PyInstaller 打包麻烦，不建议进最终 exe。

#### ⑤ OpenVINO（Intel 平台加速）
```bash
pip install openvino
```
把 onnx/小模型在 Intel CPU/GPU 上加速推理；适合低功耗机器。

#### ⑥ chatglm.cpp / fastllm
国产小模型本地推理方案，适合对国产模型有偏好的场景。

#### ⑦ 规则增强（零成本，不换引擎）
- 扩充 `VOICE_PRESETS`（增加更多风格类、更细 keywords）；
- 增加 `INTENT_RULES` 意图类别；
- 用 jieba 做更细情感分析替代静态 `SENTIMENT_WORDS`；
- 这部分**无需安装任何新依赖**，改代码即可，适合作为升级模型的过渡。

### 4.3 选型建议（按目标）

| 你的目标 | 推荐方案 |
|---------|---------|
| 最小改动、保留离线轻量、只优化风格匹配 | 规则增强 +（可选）onnxruntime+MiniLM 做语义匹配 |
| 想要「人设真正参与创作内容」 | llama-cpp-python + Qwen2.5-1.5B GGUF |
| 严格遵守「无 PyTorch、纯轻量」约束 | onnxruntime + ONNX 导出小生成模型 |
| Intel 低功耗机器提速度 | OpenVINO 加速 onnx |
| 先快速验证想法 | transformers 本地试错（不进生产） |

### 4.4 打包注意事项（PyInstaller）

- **大模型文件（GGUF/ONNX）不要打进 exe**：体积大且有版权/分发问题，放 `data/models/` 运行时按需加载（类似现在 `ui/`、`version.json` 用 `--add-data` 的方式）。
- `build.py` 需补充对应 `hidden-import` / `--collect-data`：
  - llama-cpp-python：一般无需额外收集，`llama_cpp` 会自带库；
  - onnxruntime：`--collect-data onnxruntime`（若用到）；
  - jieba：已有 `--collect-submodules jieba --collect-data jieba`。
- 模型路径在 `frozen` 模式下走 `_MEIPASS` 或相对 `DATA_DIR`，需和现有 `version.py` 的 `_base_dir()` 逻辑一致。

---

## 5. 快速上手（给后续迭代者的 5 条）

1. 改风格 → 编辑 `VOICE_PRESETS` / `VOICE_SKELETONS`（`ai_generator.py`）。
2. 改内置人格模板 → 编辑 `PERSONAS`（`ai_generator.py`）。
3. 改「说什么」抽取逻辑 → 编辑 `extract_topic` / `_extract_entities`（`ai_generator.py`）。
4. 想换引擎 → 在 `generate()` 的 `custom_templates is str` 分支替换为模型调用（参考第 4 章）。
5. 验证：`python -m py_compile src/ai_generator.py` + 用 `preview_ai` 或单测跑 5 种人设看输出是否文对题。

---
*文档生成于 2026-09-08，对应内部版本 2609080001。如需同步到 README，请在版本迭代记录表追加对应说明。*
