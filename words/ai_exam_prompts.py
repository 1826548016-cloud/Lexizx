"""考研写作工坊 - AI 提示词与词汇画像构建"""


def build_vocabulary_profile(max_words=80):
    """根据学习进度构建用户词汇画像（供 AI 生成作文时参考）。
    返回结构化字符串描述用户已掌握词汇水平。"""
    from .models import StudyProgress, Word

    mastered = StudyProgress.objects.filter(
        status='mastered', is_excluded=False
    ).select_related('word')[:max_words]

    words = [
        {'word': p.word.word, 'category': p.word.category, 'level': p.mastery_level}
        for p in mastered
    ]

    total = StudyProgress.objects.filter(status='mastered', is_excluded=False).count()
    total_in_db = Word.objects.count()

    # 统计词频分布
    from collections import Counter
    cat_counter = Counter(w['category'] for w in words)

    parts = [
        f'用户当前已掌握单词总数：约 {total} 个（词库总量 {total_in_db}）。',
    ]
    if cat_counter:
        desc = '、'.join(f'{name}词 {n} 个' for name, n in cat_counter.most_common())
        parts.append(f'掌握词汇类别分布：{desc}。')
    if words:
        sample = '、'.join(w['word'] for w in words[:60])
        parts.append(f'已掌握词汇取样（仅作参考，不代表全部）：{sample}。')
    parts.append(
        '注意：AI 生成的作文中，主体词汇必须控制在用户已掌握范围内，'
        '只可适量引入少量（3-6 个）略高于用户水平的高级表达并加粗标注，供用户学习。'
    )
    return '\n'.join(parts)


def essay_generation_prompt(question, user_level_desc, style='standard'):
    """生成作文的提示词。style: standard / easy / advanced"""
    if style == 'easy':
        level_hint = '使用更简单、更基础的词汇和句式，确保用户能轻松读懂。'
    elif style == 'advanced':
        level_hint = '使用较为高级的词汇和复杂句式（考研高分水平），供用户学习模仿。'
    else:
        level_hint = '使用用户当前已掌握的词汇为主体，适当引入少量高级表达。'

    return f"""你是一位专业的考研英语写作指导教师。请根据以下真题要求，为用户写一篇范文。

【用户的词汇水平画像】
{user_level_desc}

【作文要求】
{level_hint}
- 小作文控制在 100 词左右，大作文控制在 160-200 词。
- 结构完整，符合该体裁的格式规范（书信、通知等格式要正确）。
- 语言自然地道，避免中式英语。
- 如需引入高级表达，请用【】括号标出这些新词/新表达，方便用户学习。

【真题信息】
考试：{question.get_exam_type_display()}（{question.year}年）
题型：{question.get_question_type_display()} {('- ' + question.genre) if question.genre else ''}
题目：{question.title}
题目内容：{question.content}
写作要求：{question.prompt or '（无额外要求）'}

请直接输出范文正文，不要多余解释。"""


def essay_grading_prompt(question, user_essay):
    """批改用户作文的提示词，要求结构化 JSON 输出"""
    return f"""你是一位经验丰富的考研英语阅卷老师。请对以下学生作文进行批改，并以严格的 JSON 对象格式输出（不要输出任何其他文字）。

【批改维度】（每项 0-10 分）
1. vocabulary：词汇准确性与丰富度
2. grammar：语法正确性
3. coherence：篇章连贯性
4. relevance：切题度
5. richness：语言表现力

【输出格式】
{{
  "scores": {{"vocabulary": 8, "grammar": 7, "coherence": 8, "relevance": 9, "richness": 6}},
  "total": 38,
  "comments": "总评：一段话整体点评（中文）",
  "errors": [{{"original": "原句", "corrected": "修改后", "reason": "错误原因（中文）"}}],
  "suggestions": ["改进建议1（中文）", "改进建议2", "改进建议3"],
  "advanced_expressions": [{{"original": "简单表达", "advanced": "高级同义替换", "explain": "用法说明（中文）"}}],
  "model_essay": "修改后的完整范文（贴合学生水平、小幅拔高）"
}}

【真题信息】
考试：{question.get_exam_type_display()}（{question.year}年）
题型：{question.get_question_type_display()} {('- ' + question.genre) if question.genre else ''}
题目：{question.title}
题目内容：{question.content}

【学生作文】
{user_essay}"""


def translation_help_prompt(question, sentence, user_level_desc):
    """翻译真题句子解析提示词"""
    return f"""你是一位考研英语翻译辅导老师。请帮助用户解析并翻译以下英文句子。

【用户的词汇水平画像】
{user_level_desc}

【句子】
{sentence}

请从以下三个层面帮助用户（用中文回答，Markdown 格式）：
## 1. 语法结构拆解
把句子按意群拆开，标出主干、从句、修饰成分，说明各部分的逻辑关系。

## 2. 词汇点拨
标出句子中相对难或值得记的词汇/短语，给出释义和在此处的用法。可标注哪些词超出用户当前词汇量。

## 3. 参考译文 + 翻译技巧
给出通顺的参考译文，并说明翻译这类句式时需要注意的技巧（如长句拆分、语序调整、增词减词等）。

【真题背景】
{question.get_exam_type_display()} {question.year}年翻译真题
{question.title}"""


def translation_grading_prompt(question, user_translation, reference=None):
    """批改用户译文提示词，结构化 JSON 输出"""
    ref_part = f'\n【官方/参考译文】\n{reference}' if reference else ''
    return f"""你是一位考研英语翻译阅卷老师。请对用户的译文进行批改，并以严格的 JSON 对象格式输出（不要输出任何其他文字）。

【批改维度】（每项 0-10 分）
1. accuracy：忠实度（是否准确传达原意）
2. fluency：通顺度（中文表达是否自然）
3. completeness：完整度（是否漏译）

【输出格式】
{{
  "scores": {{"accuracy": 8, "fluency": 7, "completeness": 9}},
  "total": 24,
  "comments": "总评：一段话整体点评（中文）",
  "errors": [{{"original": "用户译句", "corrected": "改进后", "reason": "原因（中文）"}}],
  "suggestions": ["改进建议1（中文）", "改进建议2", "改进建议3"],
  "model_translation": "更佳的参考译文"
}}
{ref_part}
【用户译文】
{user_translation}"""


def trans_grading_prompt_v2(sentence_en, user_translation, reference=None):
    """长难句翻译五维批改提示词：每处问题必须回指原句英文片段，严格 JSON 输出"""
    ref_part = f'\n【参考译文】\n{reference}' if reference else '\n【参考译文】（无，请依据原句自行判断）'
    return f"""你是一位考研英语一长难句翻译阅卷老师。请对学生的译文严格批改，只输出一个 JSON 对象，不要输出任何其他文字或代码块标记。

【原句】
{sentence_en}
{ref_part}
【学生译文】
{user_translation}

【批改维度】（每项 0-10 分）
1. structure 主干识别：主谓宾/主系表等核心结构是否找准并译出，有无误译主干
2. clause 从句与非谓语：定语、状语、同位语从句及分词、不定式等非谓语的理解与处理是否正确
3. lexicon 关键词义：熟词僻义、指代、被动/否定等关键词是否译准、是否误译语境义
4. logic 逻辑关系：因果、转折、让步、条件、比较等逻辑是否译反或丢失
5. expression 中文表达：是否通顺、符合中文语序，有无翻译腔或漏译

【输出格式】
{{
  "scores": {{"structure": 8, "clause": 7, "lexicon": 8, "logic": 7, "expression": 8}},
  "total": 38,
  "comments": "总评：一段话整体点评（中文）",
  "errors": [
    {{"fragment_en": "逐字摘抄原句中出问题的英文片段", "issue_type": "主干|从句|词义|逻辑|中文表达 五选一", "suggestion": "问题说明与具体改法（中文）"}}
  ],
  "suggestions": ["改进建议1（中文）", "改进建议2", "改进建议3"],
  "model_translation": "更佳的参考译文",
  "syntax_notes": "本句语法结构一句话拆解（主干+从句/修饰成分标注，中文）"
}}

【硬性要求】
- errors 至少 1 条（译文完美时可给 1 条"锦上添花"级优化点），fragment_en 必须逐字摘自原句英文
- issue_type 只能取：主干、从句、词义、逻辑、中文表达
- 每条错误必须指出具体英文依据，禁止只写"译文不够自然"这类笼统评价
- 只输出 JSON 本身"""


def cet6_translation_help_prompt(question, paragraph, user_level_desc):
    """六级段落翻译（汉译英）解析提示词：分析中文段落，给出词汇、句式与参考译文"""
    return f"""你是一位大学英语六级翻译辅导老师。请帮助用户将以下中文段落翻译成英文。

【用户的词汇水平画像】
{user_level_desc}

【中文段落】
{paragraph}

请从以下四个层面帮助用户（用中文回答，Markdown 格式）：
## 1. 关键词与表达
列出段落中相对难翻译或值得记的中文词组/短语，给出对应的英文译法（优先使用落在用户当前词汇量内的词），并标注是否超纲。

## 2. 句式拆解与建议
按意群把中文段落拆成 4-6 个小句，逐句说明：
- 中文原句
- 推荐的英文句式（主谓宾 / 倒装 / 强调句 / 从句 / 分词作状语等）
- 翻译时需注意的语序调整、时态、单复数、冠词等

## 3. 参考译文
给出一份通顺、贴合用户词汇水平的完整英文参考译文（约 150-200 词）。

## 4. 翻译技巧点拨
总结汉译英这类段落翻译的 2-3 条通用技巧（如：化整为零拆短句、被动转主动、并列句合并、删冗余修饰等）。

【真题背景】
大学英语六级 {question.year}年段落翻译真题
{question.title}"""


def cet6_translation_grading_prompt(question, user_translation, reference=None):
    """批改用户英文译文（汉译英），结构化 JSON 输出"""
    ref_part = f'\n【参考译文】\n{reference}' if reference else ''
    return f"""你是一位大学英语六级翻译阅卷老师。请对用户的英文译文进行批改，并以严格的 JSON 对象格式输出（不要输出任何其他文字）。

【批改维度】（每项 0-10 分）
1. accuracy：忠实度（是否准确传达原文意思，有无漏译、误译）
2. grammar：语法（时态、单复数、冠词、从句结构是否正确）
3. vocabulary：用词（词汇是否恰当、是否回避了重复、有无中式英语）
4. fluency：通顺度（英文表达是否自然、地道）
5. coherence：连贯度（句间衔接、上下文逻辑是否清晰）

【输出格式】
{{
  "scores": {{"accuracy": 8, "grammar": 7, "vocabulary": 8, "fluency": 7, "coherence": 8}},
  "total": 38,
  "comments": "总评：一段话整体点评（中文）",
  "errors": [{{"original": "用户译句", "corrected": "改进后", "reason": "原因（中文）"}}],
  "suggestions": ["改进建议1（中文）", "改进建议2", "改进建议3"],
  "model_translation": "更佳的参考英文译文"
}}
{ref_part}
【用户译文】
{user_translation}"""


def themes_report_prompt(questions_text):
    """历年真题主题规律分析提示词"""
    return f"""你是一位研究考研英语命题规律多年的专家。以下是部分历年真题，请分析：

1. 高频主题归纳（按出现频次排序）
2. 常见题型分析（书信各类型、通知、图画、图表等）
3. 每种题型的高分写作要点（条数结构、常用句式）
4. 对备考的建议（词汇、模板、审题技巧）

用中文回答，Markdown 格式输出。

【真题数据】
{questions_text}"""


def personal_template_prompt(user_level_desc, practice_summary):
    """基于用户词汇画像 + 历史练习记录，生成专属作文模板的提示词"""
    return f"""你是一位资深的考研英语写作教师，擅长为每个学生量身定制作文模板。请根据用户的词汇水平和历史练习情况，生成一份【专属作文模板】，让用户能直接套用。

【用户的词汇水平画像】
{user_level_desc}

【用户的历史练习情况】
{practice_summary}

【输出要求——Markdown 排版规范，务必严格遵守】

1. 标题层级：
   - 全文只有一个 "# 你的专属考研英语写作模板"（一级标题）
   - 五个大板块使用 "## 一、我的常用高分句型" 格式（二级标题，必须带编号"一、二、三、四、五、"）
   - 板块内子项用 "### 句型 1：开头引入——图画/图表描述" 或 "#### 开头句（3个）" 格式（三/四级标题）
   - 禁止用空格 + 文字充当标题，必须用 # 号

2. 列表与项目：
   - 多个并列项目一律用有序列表（1. 2. 3.）或无序列表（- ）
   - 禁止用"句型 1：XXX  句型 2：XXX"这样的扁平文字罗列
   - 子项如果有分类，用嵌套列表或四级标题分组

3. 表格：
   - 「我的词汇升级卡」板块 MUST 使用标准 Markdown 表格，格式如下：
     | 原表达 | 升级为 | 例句 |
     |---|---|---|
     | use（使用） | adopt（采用） | We should adopt a new method. |
   - 分隔行（|---|---|---|）绝对不能省！否则无法正确渲染

4. 英文模板句展示：
   - 单独的英文模板句用引用块 "> 英文句子" 包裹（> 开头）
   - 英文例句前面加 "　例句：" 引导，英文部分用 *斜体* 包裹

5. 加粗标注：
   - 高级词汇、高分亮点、重点提示用 **加粗** 标注
   - 易错点、注意事项用 **注意：** 开头强调
   - 记忆口诀、要点提示用 "　　"（两个全角空格）缩进开头

6. 分段与空行：
   - 每个段落之间空一行，段首不要缩进
   - 列表项、引用块、表格之间务必留空行，避免粘连

【内容结构（五个板块，严格按下列框架输出）】

## 一、我的常用高分句型
> 基于你已掌握的词汇设计，全部经过批改验证，可直接套用。

### 句型 1：开头引入——图画/图表描述
**中文示意：** 如图所示，我们可以清楚地看到⋯⋯
英文模板：
> As is **vividly** shown in the picture, we can see clearly that ______.

　用 `As is shown` 开头比 `The picture shows` 更正式。
　例句：*As is vividly shown in the picture, a young man is addicted to his phone.*

### 句型 2：...（3-5 个，依次类推）

---

## 二、我的万能开头 / 衔接 / 结尾

#### 开头句（3个）
1. **As is vividly shown in the picture, ______.**
   　例：*As is vividly shown in the picture, a senior man is teaching a child to write.*
2. ...

#### 过渡衔接句（3个）
1. **In addition, ______.**
2. ...

#### 结尾升华句（3个）
1. **In a word, it is **high time** that we took **effective** actions.**
   　**注意：** `It is high time that` 后从句用过去时（虚拟语气），考研高频考点！
2. ...

---

## 三、我的常犯错误清单
> 根据你的历史批改记录整理，这些是你最容易丢分的地方，务必牢记！

### 错误 1：`like` 与 `such as` 混用
　错误：*Sketch patterns like blossoms on its surface.*
　正确：*Sketch patterns such as blossoms on its surface.*
　记忆口诀：书面语用 `such as`，口语才用 `like`。

### 错误 2：...（3-5 个）

---

## 四、我的词汇升级卡
> 每次写作前扫一眼，挑 2-3 个用进去，慢慢变成你自己的。

| 原表达（你已经会的） | 高级表达（加粗学习） | 例句（用你已掌握的词汇造句） |
|---|---|---|
| **use**（使用） | **adopt**（采用） | We should **adopt** a new method to solve this problem. |
| ...（5-8 行） | ... | ... |

---

## 五、我的写作流程建议
> 根据你的得分情况与薄弱环节，给你 4 条最实用的提分建议。

### 建议 1：先搭骨架，再填肉——5 分钟列提纲
拿到题目后，不要直接动笔。花 5 分钟在草稿纸上写下：
　开头（描述图画/图表/书信目的）→ 用模板句型 1 或书信开头
　中间（分析原因/举例）→ 用模板句型 3 或 4
　结尾（总结/建议）→ 用模板句型 5 + 结尾升华句

### 建议 2：...（3-4 条）

【其他硬性要求】
- 所有英文表达主体必须落在用户已掌握词汇范围内，只适量引入 1-2 个更高阶表达并标注 **加粗**
- 语气亲切、可操作，让用户真正能背下来、用得上
- 绝对不要输出 Markdown 代码块包裹（不要 ```markdown ... ```），直接输出纯 Markdown 文本
- 全文不要出现"下面的模板""以上建议"等指代语，保持独立成章"""


# ─── 长难句 AI 组卷 ───────────────────────────────────────────────

# 可选句法结构代码 → 中文说明（供 prompt 与展示共用）
TRANS_SYNTAX_TAGS = {
    'attributive_clause': '定语从句（which/that/who/where 等引导）',
    'noun_clause': '名词性从句（主语/宾语/表语/同位语从句）',
    'non_finite': '非谓语动词（分词、不定式、动名词短语）',
    'parenthesis': '插入语（双逗号/双破折号分隔的成分）',
    'inversion': '倒装结构（部分倒装/完全倒装/强调）',
    'passive': '被动语态',
    'comparison': '比较结构（as...as / more...than / 倍数表达）',
    'abstract_noun': '抽象名词作主语（名词化结构）',
}

TRANS_DIFFICULTY_NAMES = {2: '基础', 3: '标准', 4: '拔高', 5: '拔高'}
TRANS_LENGTH_RANGES = {
    'short': (20, 30, '短句（20-30 词）'),
    'standard': (30, 45, '标准句（30-45 词）'),
    'long': (45, 65, '长难句（45-65 词）'),
}


def trans_paper_generation_prompt(batch_size, length_key, difficulty, syntax_codes,
                                  target_words, topics, rewrite_sources=None,
                                  avoid_sentences=None, retry_feedback=None):
    """长难句 AI 组卷受控提示词：严格 JSON 输出，约束句长/句法/目标词/风格/去重。"""
    lo, hi, length_desc = TRANS_LENGTH_RANGES.get(length_key, TRANS_LENGTH_RANGES['standard'])
    diff_name = TRANS_DIFFICULTY_NAMES.get(difficulty, '标准')
    syntax_desc = '、'.join(
        TRANS_SYNTAX_TAGS.get(c, c) for c in syntax_codes) or '英语一常见长难句结构（定语从句、名词性从句、非谓语、被动等）'
    topics_desc = '、'.join(topics) if topics else '学术科普通用主题（社会、科技、教育、文化、环境、经济等）'
    words_desc = '、'.join(target_words) if target_words else '（本次无指定目标词，按学术写作高频词自然命题）'

    rewrite_block = ''
    if rewrite_sources:
        numbered = '\n'.join(f'{i + 1}. {s}' for i, s in enumerate(rewrite_sources))
        rewrite_block = f"""
【改写参考真题】（仅模仿其句式难度与结构复杂度，内容必须全新创作）
{numbered}
要求：保留同类句法难度，但替换主题、词汇和措辞，严禁照抄原句或仅替换个别词。
"""

    avoid_block = ''
    if avoid_sentences:
        numbered = '\n'.join(f'- {s[:160]}' for s in avoid_sentences[:12])
        avoid_block = f"""
【必须避开的近期题目】（不得与以下句子高度相似，主题与措辞均需明显不同）
{numbered}
"""

    retry_block = ''
    if retry_feedback:
        retry_block = f"""
【上一批未通过校验，必须针对性修正】
{retry_feedback}
请逐条改正后重新生成完整的 {batch_size} 句。
"""

    return f"""你是考研英语一长难句翻译训练命题助手。请命制 {batch_size} 个适合"英译汉"训练的英文句子。

【风格与内容】
- 学术说明文风格，话题贴近考研英语一翻译真题（学术著作、科普评论、社科论述）
- 每个句子必须能独立理解，不依赖上下文
- 难度等级：{diff_name}（难度 {difficulty}/5）
- 主题范围：{topics_desc}
- 句长：每句严格控制在 {lo}-{hi} 个英文单词（{length_desc}）

【句法要求】
本批必须重点覆盖以下结构：{syntax_desc}
- 每个句子至少自然包含其中 1 种结构，全批尽量覆盖全部指定结构
- syntax_tags 只可使用这些代码：{', '.join(TRANS_SYNTAX_TAGS.keys())}

【目标词】（来自用户本地词库，必须真实、自然地用到句子中，可使用其正确词形变化）
{words_desc}
- 全批句子对上述目标词的覆盖率不低于 80%；一个句子可包含多个目标词
- 用词必须符合该词的真实搭配与语境义，不得生硬堆砌

【禁止事项】
- 不得使用生僻人名地名、虚构机构、虚构统计数据
- 不得复制任何真实考研真题句子，也不得自称真题
- 不得出现需要上文才能理解的指代表达
- 不得输出 JSON 以外的任何文字、解释或代码块标记
{avoid_block}{rewrite_block}{retry_block}
【输出格式——只输出一个 JSON 对象】
{{
  "questions": [
    {{
      "sentence_en": "英文原句",
      "reference_zh": "准确、自然、完整的中文参考译文",
      "syntax_tags": ["non_finite", "attributive_clause"],
      "target_words": ["句中实际用到的目标词原形"],
      "difficulty": {difficulty},
      "difficulty_reason": "一句话说明本句难点",
      "syntax_tree": [
        {{"fragment": "主干英文片段", "role": "主干"}},
        {{"fragment": "修饰成分英文片段", "role": "定语从句/状语/插入语等"}}
      ]
    }}
  ]
}}
要求：questions 数组必须恰好包含 {batch_size} 项；reference_zh 不能为空；syntax_tree 每项 fragment 必须逐字摘自该句英文。"""


# ============================================================
# 六级汉译英：常量与提示词（训练单位为中文段落，与英语一逐句英译汉分开维护）
# ============================================================

CET6_SKILL_TAGS = {
    'tense_voice': '时态与语态（一般现在/过去/完成时、被动语态的正确选用）',
    'attributive_clause': '定语从句（which/that/whose 等引导的修饰）',
    'non_finite': '非谓语动词（分词、不定式作状语/定语）',
    'conversion': '词性转换（动词↔名词↔形容词的英译转换）',
    'collocation': '动词/名词搭配（固定搭配与动宾组合）',
    'culture': '文化表达（中国传统/民俗/历史概念的规范英译）',
    'cohesion': '衔接表达（连接词、指代、段落连贯）',
}
CET6_SKILL_TAG_NAMES = {k: v.split('（')[0] for k, v in CET6_SKILL_TAGS.items()}

CET6_TOPICS = ['文化', '社会', '教育', '科技', '环境', '经济', '传统节日', '乡村振兴']

CET6_ERROR_TAGS = {
    'omission': '遗漏', 'culture': '文化表达', 'grammar': '语法',
    'collocation': '搭配', 'logic': '逻辑', 'fluency': '自然度',
}

CET6_DIFFICULTY_NAMES = {2: '基础', 3: '标准', 4: '拔高', 5: '拔高'}
# 中文段落字数范围（含标点）：短段 / 标准段 / 长段
CET6_LENGTH_RANGES = {
    'short': (60, 90, '短段（60-90 字）'),
    'standard': (100, 140, '标准段（100-140 字）'),
    'long': (140, 200, '长段（140-200 字）'),
}
CET6_BATCH_SIZE = 2  # 每批生成段落数（段落较长，控制单次返回体量）


def cet6_grading_prompt_v2(passage_zh, user_translation, reference=None,
                           key_points=None, skill_tags=None):
    """六级汉译英批改提示词：五维评分 + 错因标签 + 替代表达，严格 JSON 输出。
    每条错误必须回指中文原文片段；key_points 逐点判断（表达出含义/用词/搭配）。"""
    ref_part = f'\n【参考译文】\n{reference}' if reference else \
        '\n【参考译文】（无，请依据中文原文自行判断后给出 model_translation）'
    skill_desc = '、'.join(CET6_SKILL_TAGS.get(c, c) for c in (skill_tags or []))
    kp_lines = ''
    if key_points:
        rows = []
        for kp in key_points:
            if isinstance(kp, dict) and kp.get('zh'):
                alt = '、'.join(kp.get('alternatives') or [])
                rows.append(f'- {kp["zh"]} → 推荐 {kp.get("recommended_en", "")}'
                            + (f'（可接受：{alt}）' if alt else ''))
        if rows:
            kp_lines = '【关键表达点】（逐点判断用户是否达成）\n' + '\n'.join(rows) + '\n'
    return f"""你是一位大学英语六级汉译英阅卷老师。请对学生的英文译文严格批改，只输出一个 JSON 对象，不要输出任何其他文字或代码块标记。

【中文原文】
{passage_zh}
{ref_part}
{kp_lines}【学生译文】
{user_translation}

【评分维度】（每项 0-10 分）
1. accuracy 信息完整：中文信息是否完整传达，有无漏译、误译、随意增删
2. culture 文化表达：专有文化表达（节日、民俗、历史、名胜等）是否使用规范英译
3. grammar 语法句法：时态、语态、主谓一致、从句与非谓语结构是否正确{('；重点关注：' + skill_desc) if skill_desc else ''}
4. collocation 搭配与词性：词汇搭配是否地道、词性使用是否正确、有无中式英语
5. fluency 衔接与自然度：句间衔接是否清晰、英文表达是否自然流畅

【关键表达点判定】若有【关键表达点】，在 key_point_results 中逐点输出：
- status 只能取 conveyed（表达了该含义且用词合理）/ rephrased（用合理替代表达达意）/ missed（漏译或误译）
- 用户使用替代表达且达意的必须判 conveyed 或 rephrased，不得仅因未用推荐词而判 missed

【输出格式】
{{
  "scores": {{"accuracy": 8, "culture": 7, "grammar": 8, "collocation": 7, "fluency": 8}},
  "total": 38,
  "comments": "总评：一段话整体点评（中文）",
  "errors": [
    {{"zh_fragment": "逐字摘抄中文原文中出问题的片段", "original": "对应的用户译文", "corrected": "改进后译文", "issue_type": "遗漏|文化表达|语法|搭配|逻辑|自然度 六选一", "suggestion": "问题说明与具体改法（中文）"}}
  ],
  "key_point_results": [
    {{"zh": "关键表达点中文", "user_en": "用户实际使用的英文", "status": "conveyed|rephrased|missed", "note": "一句话点评"}}
  ],
  "replacements": [
    {{"zh": "中文概念", "basic": "用户或直译的英文", "better": "更适合六级写作与翻译的替换表达", "note": "简短说明"}}
  ],
  "suggestions": ["改进建议1（中文）", "改进建议2", "改进建议3"],
  "model_translation": "更佳的完整参考英文译文",
  "error_tags": ["omission", "grammar"]
}}

【硬性要求】
- errors 至少 1 条（译文完美时可给 1 条"锦上添花"级优化点），zh_fragment 必须逐字摘自中文原文
- issue_type 只能取：遗漏、文化表达、语法、搭配、逻辑、自然度
- error_tags 从上述 issue_type 对应的英文代码中选取（遗漏=omission 文化表达=culture 语法=grammar 搭配=collocation 逻辑=logic 自然度=fluency），无错则给空数组
- 每条错误必须指出具体中文依据，禁止只写"表达不够自然"这类笼统评价
- 只输出 JSON 本身"""


def cet6_paper_generation_prompt(batch_size, length_key, difficulty, topics,
                                 skill_codes, target_words, rewrite_sources=None,
                                 avoid_texts=None, retry_feedback=None):
    """六级汉译英 AI 组卷受控提示词：命制可译成英文的中文材料 + 参考译文 + 关键表达点。
    严格 JSON 输出，约束段落字数/主题/专项/目标表达/去重。"""
    lo, hi, length_desc = CET6_LENGTH_RANGES.get(length_key, CET6_LENGTH_RANGES['standard'])
    diff_name = CET6_DIFFICULTY_NAMES.get(difficulty, '标准')
    topics_desc = '、'.join(topics) if topics else '、'.join(CET6_TOPICS)
    skill_desc = '、'.join(CET6_SKILL_TAGS.get(c, c) for c in skill_codes) or \
        '六级翻译常考点（时态语态、非谓语、词性转换、文化表达、衔接）'
    skill_codes_desc = ', '.join(CET6_SKILL_TAGS.keys())
    words_desc = '、'.join(target_words) if target_words else '（本次无指定目标词，按六级翻译高频词自然命题）'

    rewrite_block = ''
    if rewrite_sources:
        numbered = '\n'.join(f'{i + 1}. {s}' for i, s in enumerate(rewrite_sources))
        rewrite_block = f"""
【改写参考真题】（仅模仿其主题类型、篇幅与表达难度，内容必须全新创作）
{numbered}
要求：保留同类主题风格与难度，但中文材料与参考译文均须全新撰写，严禁照抄原段。
"""

    avoid_block = ''
    if avoid_texts:
        numbered = '\n'.join(f'- {s[:120]}' for s in avoid_texts[:10])
        avoid_block = f"""
【必须避开的近期材料】（中文主题与英文参考译文均不得与以下内容高度相似）
{numbered}
"""

    retry_block = ''
    if retry_feedback:
        retry_block = f"""
【上一批未通过校验，必须针对性修正】
{retry_feedback}
请逐条改正后重新生成完整的 {batch_size} 段。
"""

    return f"""你是大学英语六级汉译英训练命题助手。请命制 {batch_size} 个适合"读中文、用英文表达"训练的中文段落，并给出参考英文译文。

【风格与内容】
- 主题范围：{topics_desc}
- 题材贴近六级翻译真题（中国文化、社会发展、科技成就、教育民生等说明性段落）
- 难度等级：{diff_name}（难度 {difficulty}/5）
- 每段中文严格控制在 {lo}-{hi} 个汉字（{length_desc}，含标点）
- 中文段落需语义完整、可独立翻译，不依赖上下文

【语法与表达专项】
每段必须重点覆盖以下考点：{skill_desc}
- skill_tags 只可使用这些代码：{skill_codes_desc}
- 每段至少自然包含其中 1 个考点，全批尽量覆盖全部指定考点

【目标表达】（来自用户本地词库，参考译文中必须自然用到，可用其正确词形变化）
{words_desc}
- 全批参考译文对上述目标表达的覆盖率不低于 80%；一段可包含多个目标表达
- 每个目标表达必须在 key_points 中给出对应的中文概念与推荐搭配，并可给出可接受的替代表达
- 用词必须符合真实搭配与语境，不得生硬堆砌

【禁止事项】
- 不得使用无依据的具体人名、机构名、统计数字（如"增长 35%"），不得虚构百分比数据
- 不得复制任何真实六级翻译真题段落，也不得自称真题
- 不得出现需要上文才能理解的指代表达
- 不得输出 JSON 以外的任何文字、解释或代码块标记
{avoid_block}{rewrite_block}{retry_block}
【输出格式——只输出一个 JSON 对象】
{{
  "passages": [
    {{
      "source_zh": "中文翻译材料段落",
      "reference_en": "准确、自然、完整的英文参考译文",
      "topic_tags": ["文化"],
      "skill_tags": ["non_finite", "culture"],
      "target_words": ["参考译文中实际用到的目标词原形"],
      "difficulty": {difficulty},
      "difficulty_reason": "一句话说明本段难点",
      "key_points": [
        {{"zh": "文化遗产", "recommended_en": "cultural heritage", "alternatives": ["traditional legacy"]}}
      ]
    }}
  ]
}}
要求：passages 数组必须恰好包含 {batch_size} 项；source_zh 与 reference_en 均不能为空；key_points 至少 2 条且 recommended_en 必须出现在 reference_en 中。"""