"""AI 提示词集中管理模块。

所有发给模型的 prompt / system 提示都从这里维护，页面接口与管理命令共用。
注意：带 _cli 后缀的是命令行工具使用的版本，与页面版措辞略有差异，
如需统一措辞请在这里一并调整。
"""


# ─── 速记（AI 速记生成）──────────────────────────────────────────

def quick_memory_prompt(word_text, pos, meanings, phonetic_us, phonetic_uk):
    """为单个单词生成速记内容。

    结构：词根拆解 → 一词串义 → 联想 → 同族词 → 例句 → 一句话总记。
    内容在纯文本 textarea 中展示（不做 Markdown 渲染），故禁用表格/星号等
    Markdown 符号，统一用 ①-⑥ 分节、· 列点，并附 deposit 完整范例做 few-shot。
    """
    def _wrap(p):
        # 数据库里音标可能已带 / /，避免包成 //
        p = (p or '').strip()
        return p if not p or p.startswith('/') else f'/{p}/'

    head_bits = [f'{word_text} 速记']
    us, uk = _wrap(phonetic_us), _wrap(phonetic_uk)
    if us or uk:
        head_bits.append(f'英 {uk or us} 美 {us or uk}'.strip())
    if pos or meanings:
        head_bits.append(f'{pos} {meanings}'.strip())
    head = '\n'.join(head_bits)

    return (
        '你是考研英语词汇速记专家，擅长用词根词源和谐音联想让学生 30 秒记住一个单词。\n'
        f'请为单词「{word_text}」创作一份速记，严格按下面示范的纯文本格式输出，'
        '不要使用 Markdown 表格、#、**、--- 等符号，不要输出格式以外的任何文字。\n\n'
        '各节要求：\n'
        '① 拆解记：优先用词根词缀，每段写成「部分(含义)」用 + 连接，'
        '末尾给「= 字面义 → 引申义」；若该词没有常见词根词缀或你拿不准词源，'
        '改按音节拆分并给每段中文谐音——禁止编造词源。\n'
        '② 一词串义：先一行点出核心动作/意象，再用「· 场景 → 含义」把给定的'
        '每个释义各串一行，覆盖全部义项。\n'
        '③ 联想：谐音或画面联想，1-2 句生动小故事，谐音贴近真实发音。\n'
        '④ 同族词：3-5 个同根/同缀的考研常见词，每行「· 单词：拆解 = 含义」；'
        '没有常见同族词时整节省略，不要硬凑。\n'
        '⑤ 例句：2-3 个简单地道的英文例句（8-20 词，覆盖不同义项与高频搭配），'
        '每句下一行缩进 2 格给中文翻译。\n'
        '⑥ 一句话：一句话收束词根或核心意象与全部含义。\n\n'
        '其他要求：\n'
        '- 释义只能使用给定释义，不新增生僻义项；全部用中文（例句与单词除外）\n'
        '- 每行不超过约 30 个汉字，全文不超过 32 行，适合窄屏阅读\n'
        '- 3 个字母以内的短词，①可直接给谐音，不必硬拆\n'
        '- 下面是格式示范，请学习其结构与排版，再为目标单词创作全新内容\n\n'
        '──── 格式示范 ────\n'
        'deposit 速记\n'
        '英 /dɪˈpɒzɪt/ 美 /dɪˈpɑːzɪt/\n'
        'v.存放；储蓄；使沉淀 n.存款；押金；沉积物\n\n'
        '① 拆解记\n'
        'de-(向下) + posit(放) = 往下放 → 存放、沉积\n\n'
        '② 一词串义\n'
        '核心意象：往下放\n'
        '· 钱往下放进银行 → 存钱；存款\n'
        '· 东西往下放某处 → 存放\n'
        '· 往下放一笔保证金 → 付押金\n'
        '· 泥沙往下沉到底 → 沉积物\n\n'
        '③ 联想\n'
        'de 谐音「得」，posit 谐音「破石头」：得把破石头往下放，沉到水底 → 沉积、存放。\n\n'
        '④ 同族词\n'
        '· position：posit(放)+ion(名词尾) = 位置\n'
        '· compose：com(一起)+pose(放) = 组成\n'
        '· expose：ex(出)+pose(放) = 暴露\n'
        '· impose：im(在上)+pose(放) = 强加\n'
        '· propose：pro(向前)+pose(放) = 提议\n\n'
        '⑤ 例句\n'
        '· You need to deposit a hundred dollars to rent the car.\n'
        '  租这辆车你需要存一百美元押金。\n'
        '· She deposited the money in the bank.\n'
        '  她把钱存进了银行。\n\n'
        '⑥ 一句话\n'
        'de(下)+posit(放)=往下放：存钱、存放、押金、沉淀，一个动作全串起。\n'
        '──── 示范结束 ────\n\n'
        f'现在请输出「{word_text}」的速记，开头为：\n{head}'
    )


# ─── 小助手（自由对话）───────────────────────────────────────────

ASSISTANT_SYSTEM_PROMPT = (
    '你是「考研单词」学习 App 中的智能助手，支持自由聊天，用户问什么你就答什么。\n'
    '要求：\n'
    '- 用中文回答，准确、简洁、有条理\n'
    '- 不设主题限制：单词学习、日常闲聊、生活建议、编程问题、百科知识、写作等都可以回答\n'
    '- 如果附带当前单词信息（见下文），优先结合该单词讲解，其余问题正常自由回答\n'
    '- 结合上下文保持对话连贯；不确定的内容要如实说明，不要编造\n'
    '- 排版要求：用清晰的 Markdown 格式组织回答——用 ### 小标题分节、**加粗** 关键词、\n'
    '  用 - 或 1. 列表、用 --- 分隔线、善用表格，让答案层次分明、便于阅读\n'
)


def assistant_word_context(word):
    """当前单词信息，作为小助手的补充上下文。"""
    return (
        f'当前单词：{word.word}\n'
        f'音标：英 /{word.phonetic_uk or "-"}/ 美 /{word.phonetic_us or "-"}/\n'
        f'词性：{word.pos or "-"}\n'
        f'释义：{"；".join(word.get_meanings()[:5]) or "-"}\n'
        f'搭配：{"；".join(word.get_collocations()[:5]) or "-"}\n'
        f'词形变化：{word.get_word_forms() or "-"}\n'
        f'例句：{word.example_en or ""} {word.example_zh or ""}'
    )


# ─── 按词性归类释义 / 生成例句 ──────────────────────────────────

def _pos_grouping_prompt(lines, cli=False):
    """把每个单词的释义按词性重新归类，输出严格 JSON 对象。"""
    if cli:
        return (
            '你是英语词典编辑。下面每行是一个单词：单词 | 词性 | 全部释义（用；分隔，未按词性区分）。\n'
            '请把每个单词的释义按词性重新归类，只输出一个严格的 JSON 对象，不要输出任何其他文字或代码块标记。\n'
            '格式：{"单词": {"词性缩写": ["释义"], "词性缩写": ["释义"]}, ...}\n'
            '要求：\n'
            '- 词性缩写沿用输入中的标准缩写（如 v. n. adj. adv. prep. pron. conj. num. art. aux. 等），顺序与输入词性一致\n'
            '- 释义保持原文措辞，按语义归入最合适的词性；同一释义在不同词性均有用法时可放入多个词性\n'
            '- 若某个词性没有对应释义，其值为空数组 []\n'
            '- 所有单词都要出现在 JSON 中，键名严格等于原单词\n\n'
            '单词列表：\n' + '\n'.join(lines)
        )
    return (
        '你是英语词典编辑。下面每行是一个单词：单词 | 词性 | 释义。\n'
        '请为每个单词按词性重新归类其释义，只输出一个严格的 JSON 对象：'
        '{"单词": {"v.": ["释义"], "n.": ["释义"], ...}, ...}\n'
        '要求：\n'
        '- 词性键使用 v./n./adj./adv./prep./conj./pron./num./art./aux./abbr. 等标准缩写\n'
        '- 释义归入对应词性下，保留原有中文释义，不要新增义项\n'
        '- 所有单词都要出现在 JSON 中，键名严格等于原单词\n\n'
        '单词列表：\n' + '\n'.join(lines)
    )


def _examples_prompt(lines, cli=False):
    """为每个单词生成 1 个英文例句 + 中文翻译，输出严格 JSON 对象。"""
    if cli:
        return (
            '你是英语词典编辑。下面每行是一个单词：单词 | 词性 | 释义。\n'
            '请为每个单词生成 1 个简单、地道的英文例句（尽量体现该词的常见用法），并给出对应的中文翻译。\n'
            '只输出一个严格的 JSON 对象，不要输出任何其他文字或代码块标记。\n'
            '格式：{"单词": {"en": "英文例句", "zh": "中文翻译"}, ...}\n'
            '要求：\n'
            '- 例句长度 8-20 个词，语法正确，避免生僻词，适合考研词汇学习场景\n'
            '- 中文翻译自然通顺，符合例句原意\n'
            '- 所有单词都要出现在 JSON 中，键名严格等于原单词\n\n'
            '单词列表：\n' + '\n'.join(lines)
        )
    return (
        '你是英语词典编辑。下面每行是一个单词：单词 | 词性 | 释义。\n'
        '请为每个单词生成 1 个简单、地道的英文例句，并给出对应的中文翻译。\n'
        '只输出一个严格的 JSON 对象，格式：{"单词": {"en": "英文例句", "zh": "中文翻译"}, ...}\n'
        '要求：\n'
        '- 例句 8-20 个词，语法正确，适合考研词汇学习场景\n'
        '- 所有单词都要出现在 JSON 中，键名严格等于原单词\n\n'
        '单词列表：\n' + '\n'.join(lines)
    )


def pos_grouping_prompt(lines):
    """页面接口（ai_complete_words）使用的按词性归类提示词。"""
    return _pos_grouping_prompt(lines, cli=False)


def phonetics_prompt(lines):
    """页面接口（ai_complete_words）使用的批量补音标提示词。"""
    return (
        '请为下列英语单词标注标准 IPA 国际音标。\n'
        '返回一个 JSON 对象：键名严格等于原单词（注意大小写），'
        '值为 {"us": "美式音标", "uk": "英式音标"}，音标必须用 / 包裹，'
        '如 {"abandon": {"us": "/əˈbændən/", "uk": "/əˈbændən/"}}。\n'
        '要求：\n'
        '- 美式、英式音标都要给出；若英美读音相同则填一样的\n'
        '- 重音符号 ˈ 与次重音 ˌ 必须准确\n'
        '- 词组按正常连读标注即可\n'
        '- 所有单词都要出现在 JSON 中，不要遗漏\n'
        '- 只输出 JSON 本身，不要输出任何多余文字、不要用代码块包裹\n\n'
        '单词列表：\n' + '\n'.join(lines)
    )


def pos_grouping_cli_prompt(lines):
    """命令行（optimize_pos_meanings）使用的按词性归类提示词。"""
    return _pos_grouping_prompt(lines, cli=True)


def examples_prompt(lines):
    """页面接口（ai_complete_words）使用的生成例句提示词。"""
    return _examples_prompt(lines, cli=False)


def examples_cli_prompt(lines):
    """命令行（generate_examples）使用的生成例句提示词。"""
    return _examples_prompt(lines, cli=True)


# ─── AI 导入识别（图片 / 文本 / 文件）────────────────────────────

RECOGNIZE_JSON_SCHEMA = (
    '[{"word": "单词拼写", "phonetic_us": "美式音标如/ˈdʒenəreɪt/", '
    '"phonetic_uk": "英式音标(可选)", "pos": "词性如v./n./adj.", '
    '"meanings": ["中文释义1", "中文释义2"], '
    '"example_en": "英文例句(可选)", "example_zh": "中文翻译(可选)"}]\n'
)

RECOGNIZE_COMMON_RULES = (
    '要求：\n'
    '1. 完整整理所有出现的单词，不要遗漏任何一个\n'
    '2. 尽量给出每个单词的美式音标（phonetic_us），用 / 包裹，如 /əˈbændən/；'
    '若原文已标注音标则直接使用，若无法确定请根据单词拼写推断常见读音\n'
    '3. 释义使用中文，多个义项放入 meanings 数组\n'
    '4. 若包含词组/搭配，把短语作为单词输出\n'
    '5. 只输出 JSON 本身，不要输出任何多余文字、不要用代码块包裹'
)


def recognize_image_prompt():
    """图片识别：识别图中所有英语单词并按 JSON 数组输出。"""
    return (
        '你是一个专业的英语词汇整理助手。请识别图片中的所有英语单词，'
        '并严格按以下 JSON 数组格式输出：\n' + RECOGNIZE_JSON_SCHEMA +
        '要求：\n'
        '1. 完整识别图中所有单词，不要遗漏任何一个\n'
        '2. 音标若无法准确识别可以留空\n'
        '3. 释义使用中文，多个义项放入 meanings 数组\n'
        '4. 按图片中从左到右、从上到下的顺序输出\n'
        '5. 若图片包含词组/搭配，把短语作为单词输出\n'
        '6. 只输出 JSON 本身'
    )


def recognize_text_prompt(text_content):
    """纯文本提取：从文本中提取所有英语单词/词组。"""
    return (
        '你是一个专业的英语词汇整理助手。请从下面的文本中提取所有英语单词/词组'
        '（若文本本身是词表，则整理其中每一行），并严格按以下 JSON 数组格式输出：\n' + RECOGNIZE_JSON_SCHEMA +
        RECOGNIZE_COMMON_RULES + '\n\n文本内容如下：\n\n' + text_content
    )


def recognize_file_prompt(file_name, file_content):
    """文件导入：从上传文件的内容中提取所有英语单词/词组。"""
    return (
        '你是一个专业的英语词汇整理助手。请从文件「%s」的内容中提取所有英语单词/词组'
        '（若内容本身是词表，则整理其中每一行），并严格按以下 JSON 数组格式输出：\n' % (file_name or '未命名') + RECOGNIZE_JSON_SCHEMA +
        RECOGNIZE_COMMON_RULES + '\n\n文件内容如下：\n\n' + file_content
    )


# ─── AI 复审 ────────────────────────────────────────────────────

def ai_review_prompt(words_json):
    """对 AI 识别出的单词列表做二次校验，输出问题与修正建议。"""
    return (
        '你是一个英语词汇质检专家。以下是 AI 从图片/文本中识别出的单词列表，'
        '请逐条校验拼写、词性、中文释义是否正确合理。\n'
        '输出要求：\n'
        '- 严格输出 JSON 数组，数组长度与输入单词数量一致，顺序一致，不要遗漏\n'
        '- 每项格式：{"word": "原单词", "correct": true/false, '
        '"issues": ["问题描述1", "问题描述2"], "suggested": "简短修改建议(可选)", '
        '"fix": {"word": "修正后单词(仅拼写错误时)", "pos": "修正后词性(仅错误时)", "meanings": ["修正后释义1"]}}\n'
        '- correct 为 true 时 issues 为空数组，fix 可为 null\n'
        '- 只输出 JSON 本身，不要输出任何多余文字、不要用代码块包裹\n\n'
        '待复审的单词列表：\n' + words_json
    )


def weekly_report_prompt(summary):
    """生成周学习报告的 AI 评语。

    summary 为统计快照字典，包含：
      - period_label / new_words / reviewed_words / study_duration / correct_rate / streak
      - total_words / mastered_words / learning_words / new_not_learned / excluded_words
      - mastery_rate / days_to_finish / finish_date_label
      - weak_words（易错词 Top） / weak_pos（陌生词性 Top）
      - best_day（学习最多的一天） / active_days
    """
    return (
        '你是一位经验丰富的考研英语学习规划师，正在给学生写一份周学习报告评语。\n'
        '要求：\n'
        '1. 用中文输出，语气亲切、鼓励为主，但评价要客观具体\n'
        '2. 3-5 个自然段：先总结本周表现，再指出亮点，然后点出薄弱环节，最后给出下周建议\n'
        '3. 薄弱环节要结合「易错词」和「陌生词性」数据具体指出，不要泛泛而谈\n'
        '4. 如数据异常少（如本周没怎么学），要温和提醒并给出可执行的恢复建议\n'
        '5. 总字数控制在 250 字左右\n'
        '6. 直接输出评语正文，不要标题、不要列表符号、不要任何多余格式\n\n'
        '本周学习数据：\n' + summary
    )
