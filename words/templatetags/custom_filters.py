import json
from django import template

register = template.Library()


@register.filter
def get_item(dictionary, key):
    """获取字典中的值"""
    if isinstance(dictionary, dict):
        return dictionary.get(key, '')
    return ''


@register.filter
def json_loads(value):
    """将 JSON 字符串转为 Python 对象"""
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


@register.filter
def status_badge_class(status):
    """返回状态对应的 badge CSS 类名"""
    mapping = {
        'new': 'badge-new',
        'learning': 'badge-learning',
        'reviewing': 'badge-reviewing',
        'mastered': 'badge-mastered',
    }
    return mapping.get(status, 'badge-new')


@register.filter
def status_display(status):
    """返回状态中文名"""
    mapping = {
        'new': '未学',
        'learning': '学习中',
        'reviewing': '复习中',
        'mastered': '已掌握',
    }
    return mapping.get(status, status)


@register.filter
def category_display(code):
    """返回类别中文名"""
    mapping = {
        'required': '必考词',
        'basic': '基础词',
        'advanced': '超纲词',
    }
    return mapping.get(code, code)


@register.filter
def dict_get(d, key):
    """从字典中获取值"""
    try:
        return d.get(key)
    except (AttributeError, TypeError):
        return ''


# 批改结果里每个维度为 0-10 分，满分 = 维度数 × 10：
#   作文（词汇/语法/连贯/切题/丰富度）= 50；考研翻译（忠实/通顺/完整）= 30；
#   六级翻译（忠实/语法/用词/通顺/连贯）= 50。
# 维度数随题型变化，故直接由结果字段推导，避免硬编码满分与 AI 实际返回不一致。
SCORE_PER_DIMENSION = 10


@register.filter
def score_max(score):
    """批改结果的满分：维度数 × 10；无法推导时返回 0（调用方按未知处理）"""
    try:
        scores = (score or {}).get('scores')
    except AttributeError:
        return 0
    if not isinstance(scores, dict) or not scores:
        return 0
    return len(scores) * SCORE_PER_DIMENSION


@register.filter
def score_tone(score):
    """得分档次 → CSS 修饰类：good(≥80%) / ok(≥60%) / weak / none(未批改)"""
    try:
        total = (score or {}).get('total')
    except AttributeError:
        total = None
    if not total:
        return 'none'
    try:
        total = float(total)
    except (TypeError, ValueError):
        return 'none'
    full = score_max(score)
    if not full:
        return 'none'
    pct = total / full * 100
    if pct >= 80:
        return 'good'
    if pct >= 60:
        return 'ok'
    return 'weak'
