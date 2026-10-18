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
    RISK_CHOICES = [
        (0, '低风险'),
        (1, '中风险'),
        (2, '高风险'),
    ]
    risk_override = models.IntegerField(
        choices=RISK_CHOICES, null=True, blank=True, db_index=True,
        verbose_name='手动风险覆盖', help_text='手动覆盖风险档位：0=低 1=中 2=高；为空时采用自动风险计算')
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


class PickRecord(models.Model):
    """选词记录：把一次今日自选清单保存为可回看、可重新载入续作的记录。"""
    name = models.CharField(max_length=100, verbose_name='记录名')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        ordering = ['-created_at']
        verbose_name = '选词记录'
        verbose_name_plural = '选词记录'

    def __str__(self):
        return self.name


class PickRecordWord(models.Model):
    """选词记录内的单词，按 position 保持勾选顺序。"""
    record = models.ForeignKey(PickRecord, on_delete=models.CASCADE,
        related_name='words', verbose_name='记录')
    word = models.ForeignKey(Word, on_delete=models.CASCADE,
        related_name='pick_record_words', verbose_name='单词')
    position = models.PositiveIntegerField(default=0, verbose_name='顺序')

    class Meta:
        unique_together = [('record', 'word')]
        ordering = ['position', 'id']
        verbose_name = '选词记录单词'
        verbose_name_plural = '选词记录单词'

    def __str__(self):
        return f'{self.record_id} - {self.word.word}'


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
        ('exam', '考试'),
        ('pos', '词性练习'),
    ]
    MISTAKE_CHOICES = [
        ('spelling', '拼写'),
        ('meaning', '释义'),
        ('meaning_confusion', '义项混淆'),
        ('pos', '词性'),
        ('collocation', '搭配'),
        ('other', '其他'),
    ]

    word = models.ForeignKey(Word, on_delete=models.CASCADE,
        related_name='study_records', verbose_name='单词')
    action = models.CharField(max_length=20, choices=ACTION_CHOICES,
        verbose_name='动作', db_index=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES,
        default='learn', verbose_name='来源', db_index=True)
    mode = models.CharField(max_length=30, blank=True, default='',
        verbose_name='模式')  # sequential/random/gates/spelling/meaning 等
    mistake_type = models.CharField(
        max_length=30, choices=MISTAKE_CHOICES, blank=True, default='',
        db_index=True, verbose_name='错误类型',
        help_text='错在哪儿：拼写/释义/义项混淆/词性/搭配/其他；答对或不会时可为空')
    error_detail = models.JSONField(
        default=dict, blank=True, verbose_name='错误详情',
        help_text='沉淀真实错误样本：{"user_answer": 你的答案, "correct": 标准答案}')
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
    cet6_translation_model = models.ForeignKey('AIModel', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+', verbose_name='六级汉译英模型')
    pos_learning_model = models.ForeignKey('AIModel', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+', verbose_name='词性学习模型',
        help_text='未设置时回退复审模型，再回退第一个已启用模型')

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


class TranslationSentence(models.Model):
    """英语一翻译真题逐句训练单位：把整道翻译真题拆成单句"""
    SOURCE_TYPE_CHOICES = [
        ('past_paper', '真题'),
        ('ai_generated', 'AI 训练题'),
        ('user_error', '用户错句'),
    ]
    question = models.ForeignKey(ExamQuestion, on_delete=models.CASCADE, null=True, blank=True,
        related_name='translation_sentences', verbose_name='所属真题',
        help_text='真题句必填；AI 训练题为空')
    order = models.IntegerField(verbose_name='句序', help_text='真题 1-5 对应标号 (46)-(50)；AI 题为卷内序号')
    sentence_en = models.TextField(verbose_name='英文原句')
    sentence_zh = models.TextField(blank=True, default='', verbose_name='参考译文')
    difficulty = models.IntegerField(default=3, verbose_name='难度(1-5)')
    syntax_tags = models.JSONField(default=list, blank=True, verbose_name='句法标签',
        help_text='真题为中文标签；AI 题为句法代码，如 ["non_finite","attributive_clause"]')
    source_type = models.CharField(max_length=20, choices=SOURCE_TYPE_CHOICES,
        default='past_paper', verbose_name='来源类型')
    source_note = models.CharField(max_length=200, blank=True, default='',
        verbose_name='来源说明')
    verified = models.BooleanField(default=False, verbose_name='参考译文已核验')

    class Meta:
        ordering = ['question__year', 'order']
        unique_together = [('question', 'order')]
        verbose_name = '翻译训练句'
        verbose_name_plural = '翻译训练句'
        indexes = [
            models.Index(fields=['source_type']),
        ]

    def __str__(self):
        if self.question_id:
            return f'{self.question} 第{self.order + 45}句'
        return f'AI训练句 {self.id} 第{self.order}句'


class TranslationPractice(models.Model):
    """长难句翻译练习记录：用户译文 + AI 五维批改 + 复习排期"""
    user = models.ForeignKey('auth.User', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='translation_practices', verbose_name='用户')
    sentence = models.ForeignKey(TranslationSentence, on_delete=models.CASCADE,
        related_name='practices', verbose_name='练习句子')
    user_translation = models.TextField(blank=True, default='', verbose_name='用户译文')
    score_json = models.TextField(blank=True, default='{}', verbose_name='五维评分数据')
    feedback = models.TextField(blank=True, default='', verbose_name='批改总评')
    duration_seconds = models.IntegerField(default=0, verbose_name='耗时(秒)')
    error_tags = models.JSONField(default=list, blank=True, verbose_name='错因标签',
        help_text='main/subordinate/meaning/logic/expression')
    next_review_date = models.DateField(null=True, blank=True, verbose_name='下次复习日期')
    consecutive_correct = models.IntegerField(default=0, verbose_name='连续正确次数')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='练习时间')

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user', 'sentence'])]
        verbose_name = '翻译练习'
        verbose_name_plural = '翻译练习'

    def __str__(self):
        return f'{self.sentence} - {self.created_at:%Y-%m-%d %H:%M}'

    def get_score(self):
        try:
            return json.loads(self.score_json)
        except (json.JSONDecodeError, TypeError):
            return {}


class TranslationFavorite(models.Model):
    """长难句收藏：用户标记需要反复练的句子"""
    user = models.ForeignKey('auth.User', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='translation_favorites', verbose_name='用户')
    sentence = models.ForeignKey(TranslationSentence, on_delete=models.CASCADE,
        related_name='favorites', verbose_name='收藏句子')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='收藏时间')

    class Meta:
        ordering = ['-created_at']
        unique_together = [('user', 'sentence')]
        verbose_name = '长难句收藏'
        verbose_name_plural = '长难句收藏'

    def __str__(self):
        return f'{self.sentence}'


class GeneratedPaper(models.Model):
    """AI 专项组卷：一次组卷任务及其配置（首期 GenerationJob 合并进本表）"""
    MODE_CHOICES = [
        ('weakness', '按薄弱点推荐'),
        ('syntax', '指定句法专项'),
        ('words', '按词库单词组卷'),
        ('rewrite', '真题同难度改写'),
    ]
    STATUS_CHOICES = [
        ('pending', '待生成'),
        ('generating', '生成中'),
        ('ready', '已完成'),
        ('failed', '失败'),
    ]
    title = models.CharField(max_length=200, verbose_name='试卷标题')
    mode = models.CharField(max_length=20, choices=MODE_CHOICES,
        default='weakness', verbose_name='组卷模式')
    source = models.CharField(max_length=20, default='ai', verbose_name='来源')
    config_json = models.JSONField(default=dict, blank=True, verbose_name='组卷配置')
    target_word_ids = models.JSONField(default=list, blank=True, verbose_name='目标词ID列表')
    syntax_tags = models.JSONField(default=list, blank=True, verbose_name='句法标签')
    difficulty = models.IntegerField(default=3, verbose_name='难度(1-5)')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES,
        default='pending', db_index=True, verbose_name='状态')
    error_message = models.TextField(blank=True, default='', verbose_name='失败原因')
    retry_count = models.IntegerField(default=0, verbose_name='已重试次数')
    model_id = models.CharField(max_length=120, blank=True, default='', verbose_name='使用的AI模型')
    prompt_hash = models.CharField(max_length=64, blank=True, default='', verbose_name='提示词指纹')
    raw_response = models.TextField(blank=True, default='', verbose_name='AI原始返回')
    duration_ms = models.IntegerField(null=True, blank=True, verbose_name='生成耗时(毫秒)')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'AI组卷'
        verbose_name_plural = 'AI组卷'

    def __str__(self):
        return f'{self.title}（{self.get_status_display()}）'


class GeneratedQuestion(models.Model):
    """AI 卷中的单题：扩展 TranslationSentence(source_type=ai_generated) 的组卷元数据"""
    paper = models.ForeignKey(GeneratedPaper, on_delete=models.CASCADE,
        related_name='questions', verbose_name='所属试卷')
    sentence = models.OneToOneField(TranslationSentence, on_delete=models.CASCADE,
        related_name='generated_question', verbose_name='训练句')
    order = models.IntegerField(verbose_name='卷内句序')
    syntax_tree_json = models.JSONField(default=list, blank=True, verbose_name='句法层级')
    target_word_ids = models.JSONField(default=list, blank=True, verbose_name='目标词ID列表')
    difficulty = models.IntegerField(default=3, verbose_name='难度(1-5)')
    difficulty_reason = models.CharField(max_length=300, blank=True, default='',
        verbose_name='难度说明')
    quality_score = models.FloatField(null=True, blank=True, verbose_name='本地质量分')
    quality_notes = models.TextField(blank=True, default='', verbose_name='校验备注')
    is_approved = models.BooleanField(default=True, verbose_name='校验通过')

    class Meta:
        ordering = ['order']
        unique_together = [('paper', 'order')]
        verbose_name = 'AI组卷题目'
        verbose_name_plural = 'AI组卷题目'

    def __str__(self):
        return f'{self.paper.title} 第{self.order}句'


class Cet6TranslationPassage(models.Model):
    """六级汉译英段落：真题 / AI 生成 / 用户错段，与英语一 TranslationSentence 分开维护"""
    SOURCE_CHOICES = [
        ('past_paper', '真题'),
        ('ai_generated', 'AI 生成'),
        ('user_error', '用户错段'),
    ]
    question = models.ForeignKey(ExamQuestion, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='cet6_passages', verbose_name='关联真题')
    source_type = models.CharField(max_length=15, choices=SOURCE_CHOICES,
        verbose_name='来源')
    source_note = models.CharField(max_length=200, blank=True, default='',
        verbose_name='来源备注')
    source_zh = models.TextField(verbose_name='中文原文')
    reference_en = models.TextField(blank=True, default='',
        verbose_name='参考英文译文')
    topic_tags = models.JSONField(default=list, blank=True, verbose_name='主题标签')
    skill_tags = models.JSONField(default=list, blank=True,
        verbose_name='语法/表达标签')
    key_points = models.JSONField(default=list, blank=True,
        verbose_name='关键表达点',
        help_text='[{zh, recommended_en, alternatives}] 中文概念到英文表达的映射')
    difficulty = models.IntegerField(default=3, verbose_name='难度(1-5)')
    verified = models.BooleanField(default=False, verbose_name='参考译文已人工核验')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        ordering = ['-created_at']
        verbose_name = '六级汉译英段落'
        verbose_name_plural = '六级汉译英段落'

    def __str__(self):
        head = (self.source_zh or '')[:18]
        return f'{self.get_source_type_display()}：{head}'


class Cet6TranslationPractice(models.Model):
    """六级翻译练习记录：用户英文译文 + AI 批改评分 + SRS 复习排期"""
    user = models.ForeignKey('auth.User', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='cet6_translation_practices',
        verbose_name='用户')
    passage = models.ForeignKey(Cet6TranslationPassage, on_delete=models.CASCADE,
        related_name='practices', verbose_name='翻译段落')
    user_translation = models.TextField(blank=True, default='', verbose_name='用户英文译文')
    score_json = models.TextField(blank=True, default='{}', verbose_name='分维评分数据')
    feedback = models.TextField(blank=True, default='', verbose_name='批改总评')
    error_tags = models.JSONField(default=list, blank=True, verbose_name='错因标签',
        help_text='omission/culture/grammar/collocation/logic/fluency')
    next_review_date = models.DateField(null=True, blank=True, verbose_name='下次复习日期')
    consecutive_correct = models.IntegerField(default=0, verbose_name='连续达标次数')
    duration_seconds = models.IntegerField(default=0, verbose_name='耗时(秒)')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        ordering = ['-created_at']
        verbose_name = '六级翻译练习'
        verbose_name_plural = '六级翻译练习'

    def __str__(self):
        return f'{self.passage} - {self.created_at:%m-%d %H:%M}'

    def get_score(self):
        try:
            return json.loads(self.score_json)
        except (json.JSONDecodeError, TypeError):
            return {}


class GeneratedCet6Paper(models.Model):
    """六级 AI 专项组卷：一次组卷任务及其配置"""
    MODE_CHOICES = [
        ('weakness', '按薄弱点推荐'),
        ('topic', '按主题组卷'),
        ('expressions', '按表达词库组卷'),
        ('rewrite', '真题同难度改写'),
    ]
    STATUS_CHOICES = [
        ('generating', '生成中'),
        ('ready', '已完成'),
        ('failed', '失败'),
    ]
    title = models.CharField(max_length=100, verbose_name='试卷标题')
    mode = models.CharField(max_length=15, choices=MODE_CHOICES, verbose_name='组卷模式')
    config_json = models.JSONField(default=dict, blank=True, verbose_name='组卷配置')
    target_word_ids = models.JSONField(default=list, blank=True,
        verbose_name='目标表达词ID列表')
    status = models.CharField(max_length=12, choices=STATUS_CHOICES,
        default='generating', verbose_name='状态')
    error_message = models.TextField(blank=True, default='', verbose_name='失败原因')
    retry_count = models.IntegerField(default=0, verbose_name='自动重试次数')
    duration_ms = models.IntegerField(default=0, verbose_name='生成耗时(毫秒)')
    raw_response = models.TextField(blank=True, default='', verbose_name='原始返回')
    model_id = models.CharField(max_length=100, blank=True, default='',
        verbose_name='使用的模型')
    prompt_hash = models.CharField(max_length=64, blank=True, default='',
        verbose_name='配置哈希')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        ordering = ['-created_at']
        verbose_name = '六级AI组卷'
        verbose_name_plural = '六级AI组卷'

    def __str__(self):
        return self.title


class GeneratedCet6Question(models.Model):
    """六级 AI 卷中的单题：扩展 Cet6TranslationPassage 的组卷元数据"""
    paper = models.ForeignKey(GeneratedCet6Paper, on_delete=models.CASCADE,
        related_name='questions', verbose_name='所属试卷')
    passage = models.OneToOneField(Cet6TranslationPassage, on_delete=models.CASCADE,
        related_name='generated_question', verbose_name='翻译段落')
    order = models.IntegerField(verbose_name='卷内序号')
    quality_score = models.FloatField(null=True, blank=True, verbose_name='本地质量分')
    quality_notes = models.TextField(blank=True, default='', verbose_name='校验备注')
    is_approved = models.BooleanField(default=True, verbose_name='校验通过')

    class Meta:
        ordering = ['order']
        unique_together = [('paper', 'order')]
        verbose_name = '六级AI组卷题目'
        verbose_name_plural = '六级AI组卷题目'

    def __str__(self):
        return f'{self.paper.title} 第{self.order}段'


class PosLearningAttempt(models.Model):
    """词性学习作答记录：词性错误的可审计来源。
    判错后由服务端额外写一条 StudyRecord(mistake_type='pos')。"""
    QUESTION_TYPES = [
        ('pos_choice', '词性识别'),
        ('pos_match', '词性释义匹配'),
        ('form_convert', '词形转换'),
        ('open', '开放题'),
    ]
    RESULT_CHOICES = [
        ('correct', '正确'),
        ('partial', '部分正确'),
        ('wrong', '错误'),
    ]
    JUDGED_BY_CHOICES = [('local', '本地判定'), ('ai', 'AI判定')]

    word = models.ForeignKey(Word, on_delete=models.CASCADE,
        related_name='pos_attempts', verbose_name='单词')
    target_pos = models.CharField(max_length=20, blank=True, default='',
        verbose_name='目标词性')
    question_type = models.CharField(max_length=20, choices=QUESTION_TYPES,
        verbose_name='题型')
    question = models.JSONField(default=dict, verbose_name='题目',
        help_text='{prompt, options?, answer, reference?}')
    user_answer = models.TextField(blank=True, default='', verbose_name='用户答案')
    result = models.CharField(max_length=10, choices=RESULT_CHOICES,
        blank=True, default='', verbose_name='判定结果')
    judged_by = models.CharField(max_length=10, choices=JUDGED_BY_CHOICES,
        blank=True, default='', verbose_name='判定来源')
    feedback = models.TextField(blank=True, default='', verbose_name='点评')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='作答时间')

    class Meta:
        ordering = ['-created_at']
        verbose_name = '词性学习作答'
        verbose_name_plural = '词性学习作答'

    def __str__(self):
        return f'{self.word.word} - {self.get_question_type_display()}'


class PosLessonCache(models.Model):
    """AI 词性讲解缓存：同一词 + 同一提示词版本直接读取，避免重复收费与判定漂移。
    （本表为方案「内容版本 + 本地缓存」要求的落地载体。）"""
    word = models.OneToOneField(Word, on_delete=models.CASCADE,
        related_name='pos_lesson_cache', verbose_name='单词')
    content = models.JSONField(default=dict, verbose_name='讲解内容')
    model_id = models.CharField(max_length=120, blank=True, default='', verbose_name='生成模型')
    prompt_version = models.CharField(max_length=20, default='1', verbose_name='提示词版本')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '词性讲解缓存'
        verbose_name_plural = '词性讲解缓存'

    def __str__(self):
        return f'{self.word.word} - 词性讲解缓存'


class WordFamily(models.Model):
    """词族：一组可教学的同根词。AI 生成的一律 source='ai_candidate'，
    经人工确认（confirmed_at 非空）后才可用于正式推荐。"""
    SOURCE_CHOICES = [('manual', '人工录入'), ('ai_candidate', 'AI候选')]

    key = models.CharField(max_length=60, unique=True, verbose_name='词族标识',
        help_text='如 econom-；用于稳定键映射')
    title = models.CharField(max_length=120, verbose_name='词族名称')
    description = models.TextField(blank=True, default='', verbose_name='中文说明')
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES,
        default='manual', verbose_name='来源', db_index=True)
    confirmed_at = models.DateTimeField(null=True, blank=True,
        verbose_name='人工确认时间', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        ordering = ['key']
        verbose_name = '词族'
        verbose_name_plural = '词族'

    def __str__(self):
        return self.title

    @property
    def is_confirmed(self):
        return self.confirmed_at is not None


class WordFamilyMember(models.Model):
    """词族成员：一个词可加入多个词族；词库外示例 word 为空、用 outside_word 文本。"""
    RELATION_CHOICES = [
        ('core', '核心词'),
        ('derived', '派生词'),
        ('inflection', '屈折变化'),
        ('compound', '复合词'),
    ]
    SOURCE_CHOICES = [('manual', '人工录入'), ('ai_candidate', 'AI候选')]

    family = models.ForeignKey(WordFamily, on_delete=models.CASCADE,
        related_name='members', verbose_name='所属词族')
    word = models.ForeignKey(Word, on_delete=models.CASCADE, null=True, blank=True,
        related_name='family_memberships', verbose_name='词库单词')
    outside_word = models.CharField(max_length=100, blank=True, default='',
        verbose_name='词库外示例', help_text='词库中不存在时仅作展示文本')
    pos = models.CharField(max_length=20, blank=True, default='', verbose_name='词性')
    meaning = models.CharField(max_length=200, blank=True, default='',
        verbose_name='释义', help_text='简短中文释义；库内词展示时优先取词库释义')
    relation = models.CharField(max_length=20, choices=RELATION_CHOICES,
        default='derived', verbose_name='关系')
    note = models.CharField(max_length=200, blank=True, default='', verbose_name='构词说明')
    sort_order = models.IntegerField(default=0, verbose_name='排序')
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES,
        default='manual', verbose_name='来源')
    confirmed_at = models.DateTimeField(null=True, blank=True, verbose_name='人工确认时间')

    class Meta:
        ordering = ['family', 'sort_order']
        verbose_name = '词族成员'
        verbose_name_plural = '词族成员'

    def __str__(self):
        return f'{self.family.key}: {self.word or self.outside_word}'


class WordFormationPattern(models.Model):
    """构词法规则：前缀/后缀/词根与词性转换规律。AI 候选需人工确认。"""
    KIND_CHOICES = [('prefix', '前缀'), ('suffix', '后缀'), ('root', '词根')]
    SOURCE_CHOICES = [('manual', '人工录入'), ('ai_candidate', 'AI候选')]

    family = models.ForeignKey(WordFamily, on_delete=models.CASCADE,
        null=True, blank=True, related_name='patterns',
        verbose_name='候选批次', help_text='AI 候选时关联所属词族，便于整批确认/拒绝')
    name = models.CharField(max_length=40, verbose_name='构词元素',
        help_text='如 -tion / -ment / un-')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES,
        default='suffix', verbose_name='类型')
    from_pos = models.CharField(max_length=20, blank=True, default='', verbose_name='输入词性')
    to_pos = models.CharField(max_length=20, blank=True, default='', verbose_name='输出词性')
    rule = models.TextField(blank=True, default='', verbose_name='规则说明')
    exception = models.TextField(blank=True, default='', verbose_name='例外说明')
    confidence = models.FloatField(default=1.0, verbose_name='置信度')
    is_confirmed = models.BooleanField(default=True, verbose_name='已人工确认')
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES,
        default='manual', verbose_name='来源')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        verbose_name = '构词法规则'
        verbose_name_plural = '构词法规则'

    def __str__(self):
        return self.name


class WordRelation(models.Model):
    """词与词之间的关系：易混/搭配/近义/反义/派生。AI 候选需人工确认。"""
    REL_CHOICES = [
        ('confusing', '易混词'),
        ('collocation', '搭配'),
        ('synonym', '近义'),
        ('antonym', '反义'),
        ('derived', '派生'),
    ]
    SOURCE_CHOICES = [('manual', '人工录入'), ('ai_candidate', 'AI候选')]

    family = models.ForeignKey(WordFamily, on_delete=models.CASCADE,
        null=True, blank=True, related_name='relations',
        verbose_name='候选批次', help_text='AI 候选时关联所属词族，便于整批确认/拒绝')
    word = models.ForeignKey(Word, on_delete=models.CASCADE,
        related_name='relations_from', verbose_name='起点词')
    related_word = models.ForeignKey(Word, on_delete=models.CASCADE,
        null=True, blank=True, related_name='relations_to', verbose_name='终点词')
    outside_word = models.CharField(max_length=100, blank=True, default='',
        verbose_name='词库外终点词')
    rel_type = models.CharField(max_length=20, choices=REL_CHOICES,
        default='confusing', verbose_name='关系类型')
    note = models.CharField(max_length=200, blank=True, default='', verbose_name='说明')
    confidence = models.FloatField(default=1.0, verbose_name='置信度')
    is_confirmed = models.BooleanField(default=True, verbose_name='已人工确认')
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES,
        default='manual', verbose_name='来源')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        verbose_name = '词关系'
        verbose_name_plural = '词关系'

    def __str__(self):
        return f'{self.word} ↔ {self.related_word or self.outside_word} ({self.rel_type})'


class KnowledgeFeedback(models.Model):
    """用户对知识推荐的反馈：重点/已掌握/不相关/暂停。反馈优先于自动关联。"""
    TARGET_CHOICES = [
        ('word', '单词'),
        ('family', '词族'),
        ('member', '词族成员'),
        ('pattern', '构词法'),
        ('relation', '词关系'),
    ]
    ACTION_CHOICES = [
        ('focus', '重点'),
        ('mastered', '已掌握'),
        ('irrelevant', '不相关'),
        ('paused', '暂停推荐'),
    ]

    target_type = models.CharField(max_length=20, choices=TARGET_CHOICES,
        verbose_name='对象类型', db_index=True)
    target_id = models.IntegerField(verbose_name='对象ID')
    action = models.CharField(max_length=20, choices=ACTION_CHOICES,
        verbose_name='反馈', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='反馈时间')

    class Meta:
        ordering = ['-created_at']
        verbose_name = '知识反馈'
        verbose_name_plural = '知识反馈'

    def __str__(self):
        return f'{self.target_type}#{self.target_id} - {self.action}'


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
        ('trans_grade', '长难句批改'),
        ('trans_hint', '长难句解析'),
        ('trans_paper_generate', '长难句AI组卷'),
        ('cet6_grade_passage', '六级段落批改'),
        ('cet6_paper_generate', '六级AI组卷'),
        ('pos_lesson', '词性讲解'),
        ('pos_grade', '词性开放题判定'),
        ('word_family_candidate', '词族候选生成'),
        ('pos_expand', '词性扩展'),
    ]
    user = models.ForeignKey('auth.User', null=True, blank=True,
        on_delete=models.SET_NULL, verbose_name='用户')
    practice = models.ForeignKey(WritingPractice, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='ai_calls', verbose_name='关联练习')
    translation_practice = models.ForeignKey(
        'TranslationPractice', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='ai_calls', verbose_name='关联长难句练习')
    cet6_practice = models.ForeignKey(
        'Cet6TranslationPractice', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='ai_calls', verbose_name='关联六级翻译练习')
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
