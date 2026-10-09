import json
from django.db import models
from django.utils import timezone


class Unit(models.Model):
    number = models.IntegerField(unique=True, verbose_name='单元编号')
    name = models.CharField(max_length=100, verbose_name='单元名称')
    category = models.CharField(max_length=20, verbose_name='词汇类别',
        choices=[('required', '必考词'), ('basic', '基础词'), ('advanced', '超纲词')])
    word_count = models.IntegerField(default=0, verbose_name='单词数量')

    class Meta:
        ordering = ['number']
        verbose_name = '单元'
        verbose_name_plural = '单元'

    def __str__(self):
        return f'List {self.number} - {self.name}'

    def mastered_count(self):
        return StudyProgress.objects.filter(
            word__unit=self, status='mastered'
        ).count()

    def progress_percent(self):
        if self.word_count == 0:
            return 0
        return round(self.mastered_count() / self.word_count * 100, 1)


class Word(models.Model):
    CATEGORY_CHOICES = [
        ('required', '必考词'),
        ('basic', '基础词'),
        ('advanced', '超纲词'),
    ]

    word = models.CharField(max_length=100, unique=True, verbose_name='单词')
    phonetic_us = models.CharField(max_length=100, blank=True, verbose_name='美式音标')
    phonetic_uk = models.CharField(max_length=100, blank=True, verbose_name='英式音标')
    pos = models.CharField(max_length=50, blank=True, verbose_name='词性')
    meanings = models.TextField(verbose_name='释义', default='[]',
        help_text='JSON格式: ["释义1", "释义2"]')
    meanings_by_pos = models.TextField(blank=True, verbose_name='按词性释义', default='{}',
        help_text='JSON格式: {"v.": ["释义"], "n.": ["释义"]}')
    uncommon_meanings = models.TextField(blank=True, verbose_name='熟词僻义',
        default='[]')
    collocations = models.TextField(blank=True, verbose_name='搭配',
        default='[]')
    word_forms = models.TextField(blank=True, verbose_name='词形变化',
        default='{}')
    example_en = models.TextField(blank=True, verbose_name='英文例句')
    example_zh = models.TextField(blank=True, verbose_name='中文翻译')
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES,
        verbose_name='词汇类别', db_index=True)
    unit = models.ForeignKey(Unit, on_delete=models.CASCADE,
        related_name='words', verbose_name='所属单元')
    list_number = models.IntegerField(default=1, verbose_name='List内序号')

    class Meta:
        ordering = ['unit__number', 'list_number']
        verbose_name = '单词'
        verbose_name_plural = '单词'
        indexes = [
            models.Index(fields=['unit', 'list_number']),
            models.Index(fields=['category']),
        ]

    def __str__(self):
        return self.word

    def get_meanings(self):
        try:
            return json.loads(self.meanings)
        except (json.JSONDecodeError, TypeError):
            return []

    def get_uncommon_meanings(self):
        try:
            return json.loads(self.uncommon_meanings)
        except (json.JSONDecodeError, TypeError):
            return []

    def get_meanings_by_pos(self):
        """按词性分组的释义：{"v.": ["释义"], "n.": ["释义"]}；未生成时返回 {}"""
        try:
            data = json.loads(self.meanings_by_pos or '{}')
        except (json.JSONDecodeError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def get_collocations(self):
        try:
            return json.loads(self.collocations)
        except (json.JSONDecodeError, TypeError):
            return []

    def get_word_forms(self):
        try:
            return json.loads(self.word_forms)
        except (json.JSONDecodeError, TypeError):
            return {}

    def meanings_display(self):
        by_pos = self.get_meanings_by_pos()
        if by_pos:
            parts = ['%s %s' % (p, '；'.join(ms)) for p, ms in by_pos.items() if ms]
            if parts:
                return '；'.join(parts)
        return '; '.join(self.get_meanings())


class StudyProgress(models.Model):
    STATUS_CHOICES = [
        ('new', '未学'),
        ('learning', '学习中'),
        ('reviewing', '复习中'),
        ('mastered', '已掌握'),
    ]

    word = models.OneToOneField(Word, on_delete=models.CASCADE,
        related_name='progress', verbose_name='单词')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES,
        default='new', verbose_name='状态', db_index=True)
    mastery_level = models.IntegerField(default=0, verbose_name='掌握等级', db_index=True)
    review_count = models.IntegerField(default=0, verbose_name='复习次数')
    error_count = models.IntegerField(default=0, verbose_name='错误次数')
    last_review = models.DateTimeField(null=True, blank=True,
        verbose_name='上次复习')
    next_review = models.DateField(null=True, blank=True,
        verbose_name='下次复习', db_index=True)
    is_today_new = models.BooleanField(default=False, verbose_name='今日新学')
    learned_date = models.DateField(null=True, blank=True,
        verbose_name='首次学习日期', db_index=True)
    uncommon_pos = models.TextField(blank=True, default='[]',
        verbose_name='陌生词性', help_text='JSON格式: ["n.", "adj."]')
    spelling_attempts = models.IntegerField(default=0, verbose_name='拼写检测次数')
    spelling_correct = models.IntegerField(default=0, verbose_name='拼写正确次数')
    meaning_attempts = models.IntegerField(default=0, verbose_name='释义默写次数')
    meaning_correct = models.IntegerField(default=0, verbose_name='释义默写正确次数')
    consecutive_correct = models.IntegerField(default=0, verbose_name='连续答对次数',
        help_text='背诵/复习中连续答对计数，达到阈值自动标记为永不忘记')
    is_excluded = models.BooleanField(default=False, db_index=True,
        verbose_name='永不忘记（永久排除）',
        help_text='标记为永不忘记的词不再出现在背诵/复习中')
    manual_level = models.IntegerField(
        choices=[(1, '熟练'), (2, '次于熟练'), (3, '生词')], default=3, db_index=True,
        verbose_name='手动等级', help_text='1=熟练 2=次于熟练 3=生词；与自动「会/不会」双轨并存，由用户手动划分')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '学习进度'
        verbose_name_plural = '学习进度'
        indexes = [
            models.Index(fields=['status', 'next_review']),
            models.Index(fields=['learned_date', 'is_today_new']),
        ]

    def __str__(self):
        return f'{self.word.word} - {self.status} (L{self.mastery_level})'


class StudyPlan(models.Model):
    name = models.CharField(max_length=100, verbose_name='计划名称')
    daily_new_words = models.IntegerField(default=30, verbose_name='每日新词数')
    daily_review_count = models.IntegerField(default=50, verbose_name='每日复习量')
    target_date = models.DateField(null=True, blank=True,
        verbose_name='目标完成日期')
    unit_range = models.TextField(default='[]', verbose_name='单元范围')
    is_active = models.BooleanField(default=False, verbose_name='是否激活')
    start_date = models.DateField(auto_now_add=True, verbose_name='开始日期')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = '学习计划'
        verbose_name_plural = '学习计划'

    def __str__(self):
        return self.name

    def get_unit_range(self):
        try:
            return json.loads(self.unit_range)
        except (json.JSONDecodeError, TypeError):
            return []

    def remaining_words(self):
        unit_nums = self.get_unit_range()
        if not unit_nums:
            total = Word.objects.count()
        else:
            total = Word.objects.filter(unit__number__in=unit_nums).count()
        learned = StudyProgress.objects.exclude(status='new').count()
        return max(0, total - learned)

    def estimated_days(self):
        remaining = self.remaining_words()
        if self.daily_new_words <= 0:
            return 0
        return (remaining + self.daily_new_words - 1) // self.daily_new_words


class DailyCheckIn(models.Model):
    date = models.DateField(unique=True, verbose_name='日期')
    new_words_learned = models.IntegerField(default=0, verbose_name='新学词数')
    words_reviewed = models.IntegerField(default=0, verbose_name='复习词数')
    study_duration = models.IntegerField(default=0, verbose_name='学习时长(秒)')
    correct_rate = models.FloatField(default=0, verbose_name='正确率')
    today_correct = models.IntegerField(default=0, verbose_name='今日正确数')
    today_wrong = models.IntegerField(default=0, verbose_name='今日错误数')
    is_checked = models.BooleanField(default=False, verbose_name='是否打卡')

    class Meta:
        ordering = ['-date']
        verbose_name = '每日打卡'
        verbose_name_plural = '每日打卡'

    def __str__(self):
        return f'{self.date} - 新{self.new_words_learned} 复习{self.words_reviewed}'


class Favorite(models.Model):
    word = models.OneToOneField(Word, on_delete=models.CASCADE,
        related_name='favorite', verbose_name='单词')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='收藏时间')

    class Meta:
        ordering = ['-created_at']
        verbose_name = '收藏'
        verbose_name_plural = '收藏'

    def __str__(self):
        return self.word.word


class Note(models.Model):
    word = models.OneToOneField(Word, on_delete=models.CASCADE,
        related_name='note', verbose_name='单词')
    content = models.TextField(blank=True, verbose_name='笔记内容')
    page_number = models.IntegerField(null=True, blank=True,
        verbose_name='书页码')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '笔记'
        verbose_name_plural = '笔记'

    def __str__(self):
        return f'{self.word.word} - 笔记'


class QuickMemory(models.Model):
    """速记：AI 生成的单词快速记忆内容，缓存到数据库，下次直接读取不再调用 AI"""
    word = models.OneToOneField(Word, on_delete=models.CASCADE,
        related_name='quick_memory', verbose_name='单词')
    content = models.TextField(blank=True, verbose_name='速记内容')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '速记'
        verbose_name_plural = '速记'

    def __str__(self):
        return f'{self.word.word} - 速记'


class DailyPick(models.Model):
    """今日自选背诵清单：用户手动勾选、按日期保存的单词。

    跨天规则（rollover_daily_picks）：历史日期未掌握的词自动顺延到当天，
    已掌握 / 永不忘记的词视为背完，自动移除。
    """
    date = models.DateField(verbose_name='日期', db_index=True)
    word = models.ForeignKey(Word, on_delete=models.CASCADE,
        related_name='daily_picks', verbose_name='单词')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='加入时间')

    class Meta:
        unique_together = [('date', 'word')]
        ordering = ['date', 'id']
        verbose_name = '今日自选'
        verbose_name_plural = '今日自选'

    def __str__(self):
        return f'{self.date} - {self.word.word}'


class StudySession(models.Model):
    MODE_CHOICES = [
        ('sequential', '顺序背诵'),
        ('random', '乱序背诵'),
        ('cover_en', '遮英文'),
        ('cover_zh', '遮中文'),
        ('review', '复习'),
        ('favorite', '收藏复习'),
        ('exam', '模拟考试'),
    ]

    date = models.DateField(verbose_name='日期')
    mode = models.CharField(max_length=20, choices=MODE_CHOICES,
        verbose_name='学习模式')
    start_time = models.DateTimeField(auto_now_add=True, verbose_name='开始时间')
    end_time = models.DateTimeField(null=True, blank=True, verbose_name='结束时间')
    words_count = models.IntegerField(default=0, verbose_name='单词数')
    correct_count = models.IntegerField(default=0, verbose_name='正确数')

    class Meta:
        ordering = ['-start_time']
        verbose_name = '学习会话'
        verbose_name_plural = '学习会话'

    @property
    def correct_rate(self):
        if self.words_count == 0:
            return 0
        return round(self.correct_count / self.words_count * 100, 1)


class StudyRecord(models.Model):
    """背词历史明细：每次 会/不会、拼写/释义检测、跳过 等操作各记一条"""
    ACTION_CHOICES = [
        ('known', '会'),
        ('unknown', '不会'),
        ('spelling_ok', '拼写正确'),
        ('spelling_wrong', '拼写错误'),
        ('meaning_ok', '释义正确'),
        ('meaning_wrong', '释义错误'),
        ('skip', '永不忘记'),
    ]
    SOURCE_CHOICES = [
        ('learn', '背诵'),
        ('review', '复习'),
        ('favorite', '收藏复习'),
    ]

    word = models.ForeignKey(Word, on_delete=models.CASCADE,
        related_name='study_records', verbose_name='单词')
    action = models.CharField(max_length=20, choices=ACTION_CHOICES,
        verbose_name='动作', db_index=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES,
        default='learn', verbose_name='来源', db_index=True)
    mode = models.CharField(max_length=30, blank=True, default='',
        verbose_name='模式')  # sequential/random/gates/spelling/meaning 等
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='时间', db_index=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = '背词记录'
        verbose_name_plural = '背词记录'

    def __str__(self):
        return '%s %s' % (self.word_id, self.get_action_display())


class UserSettings(models.Model):
    FONT_CHOICES = [
        ('small', '小'),
        ('medium', '中'),
        ('large', '大'),
    ]
    VOICE_CHOICES = [
        ('us', '美音'),
        ('uk', '英音'),
    ]
    THEME_CHOICES = [
        ('light', '暖纸浅色'),
        ('blue', '清爽蓝白'),
        ('pureblue', '纯蓝白'),
        ('sepia', '复古牛皮'),
        ('dark', '深夜书桌'),
        ('nightblue', '星空午夜'),
        ('custom', '自定义壁纸'),
    ]

    font_size = models.CharField(max_length=10, choices=FONT_CHOICES,
        default='medium', verbose_name='字体大小')
    dark_mode = models.BooleanField(default=False, verbose_name='深色模式')
    theme = models.CharField(max_length=15, choices=THEME_CHOICES,
        default='light', verbose_name='主题')
    pronunciation_on = models.BooleanField(default=True, verbose_name='发音开关')
    auto_read = models.BooleanField(default=True, verbose_name='自动朗读')
    speech_rate = models.FloatField(default=1.0, verbose_name='语速')
    voice_type = models.CharField(max_length=5, choices=VOICE_CHOICES,
        default='us', verbose_name='发音类型')
    daily_new_target = models.IntegerField(default=30, verbose_name='每日新词目标')
    daily_review_target = models.IntegerField(default=50, verbose_name='每日复习目标')
    batch_size = models.IntegerField(default=10, verbose_name='每批背诵数量')
    gate_answer_show = models.BooleanField(default=True, verbose_name='高效模式答错后显示答案')
    use_ai_meaning_check = models.BooleanField(default=True, verbose_name='释义默写使用 AI 判定')
    assistant_model = models.ForeignKey('AIModel', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+', verbose_name='小助手模型')
    recognize_model = models.ForeignKey('AIModel', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+', verbose_name='导入识别模型')
    review_model = models.ForeignKey('AIModel', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+', verbose_name='复审模型')
    quick_memory_model = models.ForeignKey('AIModel', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+', verbose_name='速记生成模型')
    meaning_check_model = models.ForeignKey('AIModel', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+', verbose_name='释义默写判定模型')
    exam_model = models.ForeignKey('AIModel', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+', verbose_name='考研写作工坊模型')

    # 密钥保险箱：salt 与校验串落库，密码本身与派生密钥不落库
    vault_salt = models.CharField(max_length=64, blank=True, default='', verbose_name='保险箱密码 salt')
    vault_verifier = models.TextField(blank=True, default='', verbose_name='保险箱密码校验串')

    # 隐私与内容声明：记录用户已同意的声明版本（PRIVACY_VERSION），空串=未同意
    # hash 为同意时正文（弹窗渲染后的可见文本）的 SHA-256，用于事后举证"同意的是哪一份文本"：
    # 正文措辞一旦修改，hash 随之变化，可与归档在 docs/legal/ 下的各版本正文相互印证
    privacy_agreed_version = models.CharField(max_length=20, blank=True, default='',
        verbose_name='已同意的隐私声明版本')
    privacy_agreed_hash = models.CharField(max_length=64, blank=True, default='',
        verbose_name='已同意正文的 SHA-256')

    class Meta:
        verbose_name = '用户设置'
        verbose_name_plural = '用户设置'

    def __str__(self):
        return '用户设置'

    @classmethod
    def get_settings(cls):
        settings = cls.objects.first()
        if not settings:
            settings = cls.objects.create()
        return settings


class StudyPreset(models.Model):
    """用户保存的背诵/复习自定义模式预设"""
    PRESET_TYPE_CHOICES = [
        ('learn', '背诵'),
        ('review', '复习'),
    ]
    name = models.CharField(max_length=50, verbose_name='预设名称')
    preset_type = models.CharField(max_length=10, choices=PRESET_TYPE_CHOICES, verbose_name='类型')
    params = models.JSONField(default=dict, verbose_name='参数')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        ordering = ['-created_at']
        verbose_name = '学习预设'
        verbose_name_plural = '学习预设'

    def __str__(self):
        return self.name


class Conversation(models.Model):
    """AI 智能助手会话：每条对话独立，标题自动取首条消息"""
    title = models.CharField(max_length=100, blank=True, default='新对话', verbose_name='会话标题')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'AI 会话'
        verbose_name_plural = 'AI 会话'

    def __str__(self):
        return self.title


class ChatMessage(models.Model):
    """小助手对话记录：背诵/复习两个模式共用同一条对话，历史持久化保存"""
    ROLE_CHOICES = [
        ('user', '用户'),
        ('assistant', '小助手'),
    ]
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, verbose_name='角色')
    content = models.TextField(verbose_name='内容')
    word_id = models.IntegerField(null=True, blank=True, verbose_name='关联单词 ID')
    conversation_id = models.IntegerField(null=True, blank=True, verbose_name='会话 ID')
    attachments = models.JSONField(default=list, blank=True, verbose_name='附件信息')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        ordering = ['id']
        verbose_name = '小助手对话'
        verbose_name_plural = '小助手对话'

    def __str__(self):
        return f'{self.role}: {self.content[:30]}'


class AIModel(models.Model):
    """AI 模型配置：集中存储在数据库，由设置页统一管理"""
    provider = models.CharField(max_length=50, default='openai', verbose_name='服务商')
    model_id = models.CharField(max_length=100, verbose_name='模型 ID')
    display_name = models.CharField(max_length=100, blank=True, verbose_name='展示名称')
    base_url = models.CharField(max_length=300, default='https://api.openai.com/v1', verbose_name='接口基础地址')
    endpoint = models.CharField(max_length=400, blank=True, verbose_name='完整请求地址')
    api_key = models.CharField(max_length=300, blank=True, verbose_name='API 密钥（旧版明文，新数据使用加密字段）')
    api_key_encrypted = models.TextField(blank=True, default='', verbose_name='加密后的 API 密钥')
    context = models.CharField(max_length=10, blank=True, default='128K', verbose_name='上下文')
    vision = models.BooleanField(default=True, verbose_name='支持图片识别')
    enabled = models.BooleanField(default=True, verbose_name='启用')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = 'AI 模型'
        verbose_name_plural = 'AI 模型'

    def __str__(self):
        return self.display_name or self.model_id


class ImportLog(models.Model):
    """导入记录：记录每次单词导入的历史，便于回溯查看"""
    SOURCE_CHOICES = [
        ('image', '图片识别'),
        ('text', '纯文本'),
        ('file', '文件导入'),
        ('manual', '手动添加'),
        ('command', '命令行导入'),
    ]
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='导入时间')
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES,
        default='text', verbose_name='导入来源')
    unit_number = models.IntegerField(default=99, verbose_name='目标单元编号')
    unit_name = models.CharField(max_length=100, blank=True, verbose_name='单元名称')
    imported_count = models.IntegerField(default=0, verbose_name='成功导入数')
    skipped_count = models.IntegerField(default=0, verbose_name='跳过重复数')
    words_list = models.TextField(default='[]', verbose_name='导入的单词列表',
        help_text='JSON格式: ["word1", "word2"]')

    class Meta:
        ordering = ['-created_at']
        verbose_name = '导入记录'
        verbose_name_plural = '导入记录'

    def __str__(self):
        return f'{self.created_at:%Y-%m-%d %H:%M} - {self.get_source_display()} - 成功{self.imported_count}'

    def get_words(self):
        try:
            return json.loads(self.words_list)
        except (json.JSONDecodeError, TypeError):
            return []


class LearningReport(models.Model):
    """周学习报告：每周日生成一次，保存周期统计数据快照与 AI 评语"""
    week_start = models.DateField(verbose_name='周起始日（周一）')
    week_end = models.DateField(verbose_name='周结束日（周日）')
    summary_json = models.TextField(default='{}', verbose_name='统计数据快照',
        help_text='JSON格式，保存生成时的周期内核心指标')
    ai_comment = models.TextField(blank=True, default='', verbose_name='AI 评语')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='生成时间')

    class Meta:
        ordering = ['-week_start']
        unique_together = [('week_start',)]
        verbose_name = '学习周报'
        verbose_name_plural = '学习周报'

    def __str__(self):
        return f'{self.week_start} ~ {self.week_end} 周报'

    def get_summary(self):
        try:
            return json.loads(self.summary_json)
        except (json.JSONDecodeError, TypeError):
            return {}

class ExamQuestion(models.Model):
    """考研英语真题库：作文 / 翻译题目"""
    EXAM_TYPE_CHOICES = [
        ('english1', '英语一'),
        ('english2', '英语二'),
        ('cet6', '六级'),
    ]
    QUESTION_TYPE_CHOICES = [
        ('small_essay', '小作文'),
        ('big_essay', '大作文'),
        ('translation', '翻译'),
    ]
    year = models.IntegerField(verbose_name='年份', db_index=True)
    exam_type = models.CharField(max_length=10, choices=EXAM_TYPE_CHOICES,
        verbose_name='考试类型')
    question_type = models.CharField(max_length=20, choices=QUESTION_TYPE_CHOICES,
        verbose_name='题型')
    genre = models.CharField(max_length=50, blank=True, default='', verbose_name='体裁',
        help_text='小作文：书信/通知/道歉/邀请…；大作文：图画/图表')
    title = models.CharField(max_length=200, blank=True, default='', verbose_name='标题')
    content = models.TextField(blank=True, default='', verbose_name='题目内容')
    prompt = models.TextField(blank=True, default='', verbose_name='写作要求/题干')
    model_answer = models.TextField(blank=True, default='', verbose_name='参考范文/参考译文')
    tags = models.JSONField(default=list, blank=True, verbose_name='主题标签')
    difficulty = models.IntegerField(default=3, verbose_name='难度(1-5)')
    is_imported = models.BooleanField(default=True, verbose_name='已入库')

    class Meta:
        ordering = ['exam_type', '-year', 'question_type']
        unique_together = [('exam_type', 'year', 'question_type', 'genre')]
        verbose_name = '考研真题'
        verbose_name_plural = '考研真题'

    def __str__(self):
        return f'{self.get_exam_type_display()} {self.year}年 {self.get_question_type_display()}'


class WritingPractice(models.Model):
    """写作/翻译练习记录：用户作答 + AI 生成 + AI 批改结果"""
    MODE_CHOICES = [
        ('essay', '作文'),
        ('translation', '翻译'),
    ]
    user = models.ForeignKey('auth.User', null=True, blank=True,
        on_delete=models.SET_NULL, verbose_name='用户')
    question = models.ForeignKey(ExamQuestion, null=True, blank=True,
        on_delete=models.CASCADE, related_name='practices', verbose_name='真题')
    mode = models.CharField(max_length=20, choices=MODE_CHOICES, default='essay',
        verbose_name='练习类型')
    source = models.CharField(max_length=20, default='ai', verbose_name='来源',
        help_text='ai=AI代写, manual=手动写作, translation=翻译练习')
    user_input = models.TextField(blank=True, default='', verbose_name='用户作答')
    ai_output = models.TextField(blank=True, default='', verbose_name='AI 范文/译文')
    score_json = models.TextField(blank=True, default='{}', verbose_name='批改评分数据',
        help_text='JSON 格式，各维度评分与建议')
    feedback = models.TextField(blank=True, default='', verbose_name='AI 批改意见')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        ordering = ['-created_at']
        verbose_name = '写作练习'
        verbose_name_plural = '写作练习'

    def __str__(self):
        return f'{self.question} - {self.get_mode_display()}'

    def get_score(self):
        try:
            return json.loads(self.score_json)
        except (json.JSONDecodeError, TypeError):
            return {}


class WritingPhrase(models.Model):
    """写作好句/错题沉淀：从练习中收集的高级表达与错误点"""
    user = models.ForeignKey('auth.User', null=True, blank=True,
        on_delete=models.SET_NULL, verbose_name='用户')
    practice = models.ForeignKey(WritingPractice, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='phrases', verbose_name='来源练习')
    phrase = models.TextField(verbose_name='好句/错误句子')
    meaning = models.TextField(blank=True, default='', verbose_name='释义/修改建议')
    phrase_type = models.CharField(max_length=20, default='nice',
        choices=[('nice', '好句'), ('error', '错误点'), ('replace', '替换表达')],
        verbose_name='类型')
    is_collected = models.BooleanField(default=True, verbose_name='已收藏')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        ordering = ['-created_at']
        verbose_name = '写作好句'
        verbose_name_plural = '写作好句'

    def __str__(self):
        return self.phrase[:40]


class AICallLog(models.Model):
    """AI 调用日志：记录每次调用详情，便于追溯与排查"""
    ACTION_CHOICES = [
        ('exam_generate', '作文生成'),
        ('exam_grade', '作文批改'),
        ('exam_grade_translation', '译文批改'),
        ('exam_translate_analyze', '翻译拆解'),
        ('exam_themes_report', '命题规律分析'),
        ('exam_template', '专属模板'),
        ('cet6_translate_analyze', '六级翻译解析'),
        ('cet6_grade_translation', '六级译文批改'),
    ]
    user = models.ForeignKey('auth.User', null=True, blank=True,
        on_delete=models.SET_NULL, verbose_name='用户')
    practice = models.ForeignKey(WritingPractice, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='ai_calls', verbose_name='关联练习')
    action = models.CharField(max_length=40, default='exam_generate',
        verbose_name='调用功能')
    model_id = models.CharField(max_length=120, blank=True, default='',
        verbose_name='模型')
    success = models.BooleanField(default=True, verbose_name='是否成功')
    error = models.TextField(blank=True, default='', verbose_name='错误信息')
    duration_ms = models.IntegerField(default=0, verbose_name='耗时(毫秒)')
    prompt_preview = models.TextField(blank=True, default='', verbose_name='提示词预览',
        help_text='记录提示词前 500 字，便于追溯')
    response_preview = models.TextField(blank=True, default='', verbose_name='响应预览',
        help_text='记录响应前 500 字')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='调用时间')

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'AI 调用日志'
        verbose_name_plural = 'AI 调用日志'

    def __str__(self):
        return f'{self.get_action_display()} - {self.success}'
