import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
import difflib
import hashlib
import urllib.request
import urllib.error
import base64
import math
import socket
from datetime import date, timedelta, datetime
from io import BytesIO

from django.conf import settings
from django.core.paginator import Paginator
from django.db import close_old_connections, transaction
from django.db.models import Q, Count, Sum, Avg, F, Max
from django.http import JsonResponse, HttpResponse, FileResponse, Http404
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.db import IntegrityError
from django.views.decorators.http import require_http_methods

from .models import (Unit, Word, StudyProgress, StudyPlan,
                     DailyCheckIn, Favorite, Note, QuickMemory, DailyPick, PickRecord, PickRecordWord, AIModel, AIModelHealth, PendingJudgment, StudySession, UserSettings, ChatMessage, ImportLog, Conversation, LearningReport, StudyRecord, StudyPreset, ExamQuestion, WritingPractice, WritingPhrase, AICallLog, TranslationSentence, TranslationPractice, TranslationFavorite, GeneratedPaper, GeneratedQuestion,
                     Cet6TranslationPassage, Cet6TranslationPractice,
                     GeneratedCet6Paper, GeneratedCet6Question,
                     PosLearningAttempt, PosLessonCache, WordFamily, WordFamilyMember,
                     WordFormationPattern, WordRelation, KnowledgeFeedback)
from .ai_prompts import (
    quick_memory_prompt, ASSISTANT_SYSTEM_PROMPT, assistant_word_context,
    pos_grouping_prompt, examples_prompt, phonetics_prompt,
    RECOGNIZE_JSON_SCHEMA, RECOGNIZE_COMMON_RULES,
    recognize_image_prompt, recognize_text_prompt, recognize_file_prompt,
    ai_review_prompt, weekly_report_prompt,
    pos_lesson_prompt, pos_open_grade_prompt, word_family_candidate_prompt,
    POS_LESSON_PROMPT_VERSION,
)
from .ai_exam_prompts import (
    build_vocabulary_profile, essay_generation_prompt, essay_grading_prompt,
    translation_help_prompt, translation_grading_prompt, themes_report_prompt,
    personal_template_prompt, cet6_translation_help_prompt, cet6_translation_grading_prompt,
    trans_grading_prompt_v2,
    trans_paper_generation_prompt, TRANS_SYNTAX_TAGS,
    TRANS_DIFFICULTY_NAMES, TRANS_LENGTH_RANGES,
    cet6_grading_prompt_v2, cet6_paper_generation_prompt,
    CET6_SKILL_TAGS, CET6_SKILL_TAG_NAMES, CET6_TOPICS, CET6_ERROR_TAGS,
    CET6_DIFFICULTY_NAMES, CET6_LENGTH_RANGES, CET6_BATCH_SIZE,
)


def parse_uncommon_pos(value):
    """解析 StudyProgress.uncommon_pos 字段为 list，容错脏数据。"""
    try:
        data = json.loads(value or '[]')
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


# ─── 帮助函数 ───────────────────────────────────────────────

CHECKIN_DAILY_WORDS = 30  # 每日学习满 30 词自动打卡

def update_daily_checkin():
    """更新今日打卡统计数据；今日背诵满 30 词自动打卡，返回是否刚刚自动打卡"""
    today = timezone.localdate()
    checkin, _ = DailyCheckIn.objects.get_or_create(date=today)
    # 新学：learned_date 是今天
    checkin.new_words_learned = StudyProgress.objects.filter(
        learned_date=today).count()
    # 复习：今天有复习记录，且 learned_date 早于今天（不是今天第一次学的词）
    checkin.words_reviewed = StudyProgress.objects.filter(
        last_review__date=today, review_count__gt=0
    ).exclude(learned_date=today).count()
    total = checkin.today_correct + checkin.today_wrong
    if total > 0:
        checkin.correct_rate = round(checkin.today_correct / total * 100, 1)
    else:
        checkin.correct_rate = 0
    auto_checked = False
    if not checkin.is_checked and (checkin.new_words_learned + checkin.words_reviewed) >= CHECKIN_DAILY_WORDS:
        checkin.is_checked = True
        auto_checked = True
    checkin.save()
    return auto_checked

def get_streak():
    """计算连续打卡天数"""
    today = timezone.localdate()
    streak = 0
    for i in range(365):
        d = today - timedelta(days=i)
        if DailyCheckIn.objects.filter(date=d, is_checked=True).exists():
            streak += 1
        else:
            break
    return streak

# ─── 页面视图 ───────────────────────────────────────────────

def dashboard(request):
    today = timezone.localdate()
    settings_obj = UserSettings.get_settings()
    active_plan = StudyPlan.objects.filter(is_active=True).first()

    today_new = StudyProgress.objects.filter(
        learned_date=today).count()
    today_review = StudyProgress.objects.filter(
        last_review__date=today, review_count__gt=0
    ).exclude(learned_date=today).count()
    total_words = Word.objects.count()
    mastered_words = StudyProgress.objects.filter(status='mastered').count()
    # also count learning+reviewing
    learning_words = StudyProgress.objects.exclude(status='new').exclude(status='mastered').count()
    # GitHub 语言条式占比（一位小数，供总体进度卡分段条/图例使用）
    if total_words > 0:
        master_percent = round(mastered_words / total_words * 100, 1)
        learning_percent = round(learning_words / total_words * 100, 1)
    else:
        master_percent = learning_percent = 0

    # by category
    cats = [('required', '必考词'), ('basic', '基础词'), ('advanced', '超纲词')]
    category_stats = {}
    for code, label in cats:
        total = Word.objects.filter(category=code).count()
        mastered = StudyProgress.objects.filter(word__category=code, status='mastered').count()
        category_stats[code] = {'label': label, 'total': total, 'mastered': mastered}

    streak = get_streak()
    today_checkin = DailyCheckIn.objects.filter(date=today).first()
    today_duration = today_checkin.study_duration if today_checkin else 0

    checkin_words = today_new + today_review
    checkin_threshold = CHECKIN_DAILY_WORDS
    checkin_percent = min(100, round(checkin_words / checkin_threshold * 100)) if checkin_threshold else 0

    # 刷题打卡热力图：近一年每日做题量（新学 + 复习），实时统计保证数据完整
    heat_start = today - timedelta(days=364)
    heat_new = StudyProgress.objects.filter(
        learned_date__gte=heat_start
    ).values('learned_date').annotate(cnt=Count('id'))
    heat_review = StudyProgress.objects.filter(
        last_review__date__gte=heat_start, review_count__gt=0
    ).exclude(learned_date__gte=F('last_review__date')) \
     .values('last_review__date').annotate(cnt=Count('id'))
    heat_map = {}
    for x in heat_new:
        heat_map[x['learned_date']] = heat_map.get(x['learned_date'], 0) + x['cnt']
    for x in heat_review:
        d = x['last_review__date']
        heat_map[d] = heat_map.get(d, 0) + x['cnt']
    heatmap_days = {}
    d = heat_start
    while d <= today:
        heatmap_days[d.isoformat()] = heat_map.get(d, 0)
        d += timedelta(days=1)
    total_study_days = sum(1 for v in heat_map.values() if v > 0)

    # 计划驱动的今日学习数据
    plan_today = None
    if active_plan:
        plan_today = _get_plan_today_data(active_plan, today)

    # 统一今日任务：顶部「今日新学/复习」的目标与完成数按计划（无计划则全局）
    task = _get_today_task(today)

    context = {
        'today_new': today_new,
        'today_review': today_review,
        'daily_new_done': task['new_done'],
        'daily_new_target': task['new_target'],
        'daily_review_done': task['review_done'],
        'daily_review_target': task['review_target'],
        'settings': settings_obj,
        'plan': active_plan,
        'plan_today': plan_today,
        'total_words': total_words,
        'mastered_words': mastered_words,
        'learning_words': learning_words,
        'master_percent': master_percent,
        'learning_percent': learning_percent,
        'category_stats': category_stats,
        'streak': streak,
        'today_checkin': today_checkin,
        'checkin_words': checkin_words,
        'checkin_threshold': checkin_threshold,
        'checkin_percent': checkin_percent,
        'heatmap_days': heatmap_days,
        'total_study_days': total_study_days,
        'today_duration': today_duration,
    }
    return render(request, 'dashboard.html', context)


def word_list(request):
    units = Unit.objects.annotate(
        mastered=Count('words__progress', filter=Q(words__progress__status='mastered'))
    ).all()
    return render(request, 'word_list.html', {'units': units})


def ai_import(request):
    """AI 单词导入独立页面"""
    units = Unit.objects.annotate(
        mastered=Count('words__progress', filter=Q(words__progress__status='mastered'))
    ).all()
    return render(request, 'ai_import.html', {'units': units})


def word_detail(request, word_id):
    word = get_object_or_404(Word, id=word_id)
    progress, _ = StudyProgress.objects.get_or_create(word=word)
    is_fav = hasattr(word, 'favorite')
    note = getattr(word, 'note', None)
    return render(request, 'word_detail.html', {
        'word': word,
        'progress': progress,
        'is_favorite': is_fav,
        'note': note,
    })


def rollover_daily_picks(today=None):
    """今日自选清单跨天顺延：

    历史日期的自选词——已掌握或被标记「永不忘记」的视为背完，自动移除；
    其余顺延到当天（当天已存在的不重复添加），保持原有加入顺序。
    """
    today = today or timezone.localdate()
    old_ids = list(
        DailyPick.objects.filter(date__lt=today)
        .order_by('id').values_list('word_id', flat=True)
    )
    if not old_ids:
        return
    finished_ids = set(
        StudyProgress.objects.filter(word_id__in=old_ids)
        .filter(Q(status='mastered') | Q(is_excluded=True))
        .values_list('word_id', flat=True)
    )
    carry = [wid for wid in dict.fromkeys(old_ids) if wid not in finished_ids]
    if carry:
        existing = set(
            DailyPick.objects.filter(date=today).values_list('word_id', flat=True)
        )
        new_picks = [
            DailyPick(date=today, word_id=wid)
            for wid in carry if wid not in existing
        ]
        if new_picks:
            DailyPick.objects.bulk_create(new_picks)
    DailyPick.objects.filter(date__lt=today).delete()


def _today_practiced_word_ids(today=None):
    """今日已背单词 ID 集合（用于「今日已背 · 重复练习」）。

    口径：当天背词明细（StudyRecord，忽略单独的「永不忘记/skip」记录）
    ∪ 当天新学（StudyProgress.learned_date = today，补无明细的新学词）。
    按本地日期划分，跨天后集合自然更新。
    """
    today = today or timezone.localdate()
    record_ids = StudyRecord.objects.filter(
        created_at__date=today,
    ).exclude(action='skip').values_list('word_id', flat=True)
    learned_ids = StudyProgress.objects.filter(
        learned_date=today,
    ).values_list('word_id', flat=True)
    return set(record_ids) | set(learned_ids)


def _learned_word_ids():
    """全部已学词 ID 集合（status != new），用于「复习自选」页与临时练习校验。"""
    return set(StudyProgress.objects.exclude(status='new').values_list('word_id', flat=True))


def learn_start(request):
    units = Unit.objects.all()
    today = timezone.localdate()
    rollover_daily_picks(today)
    daily_pick_count = DailyPick.objects.filter(date=today).count()
    plan_today = None
    active_plan = StudyPlan.objects.filter(is_active=True).first()
    if active_plan:
        plan_today = _get_plan_today_data(active_plan, today)
    settings_obj = UserSettings.get_settings()
    presets = StudyPreset.objects.filter(preset_type='learn')
    for p in presets:
        p.params_json = json.dumps(p.params or {}, ensure_ascii=False)
    return render(request, 'learn_start.html', {
        'units': units, 'plan_today': plan_today, 'batch_size': settings_obj.batch_size,
        'batch_options': [10, 20, 30, 50], 'presets': presets,
        'daily_pick_count': daily_pick_count,
        'review_pick_count': len(_learned_word_ids()),
    })


def today_words(request):
    """复习自选列表页：展示全部已学词（含背诵记录与状态），按会/不会筛选，勾选后进入临时练习。"""
    ids = _learned_word_ids()
    progress_map = {
        p.word_id: p for p in StudyProgress.objects.filter(word_id__in=ids)
    }
    # 每个词最近一条练习记录：一次查询 + python 分组取首条，避免 N+1
    latest_map = {}
    for r in (StudyRecord.objects.filter(word_id__in=ids)
              .order_by('word_id', '-created_at')
              .only('word_id', 'action', 'created_at')):
        if r.word_id not in latest_map:
            latest_map[r.word_id] = r
    ACTION_NAMES = {
        'known': '会', 'unknown': '不会',
        'spelling_ok': '拼写正确', 'spelling_wrong': '拼写错误',
        'meaning_ok': '释义正确', 'meaning_wrong': '释义错误',
        'skip': '永不忘记',
    }
    word_items = []
    for w in Word.objects.filter(id__in=ids).select_related('unit').only(
            'id', 'word', 'phonetic_us', 'meanings', 'unit__number', 'list_number'):
        p = progress_map.get(w.id)
        meaning = w.meanings_display()
        if len(meaning) > 40:
            meaning = meaning[:40] + '…'
        r = latest_map.get(w.id)
        status = p.status if p else 'new'
        word_items.append({
            'id': w.id,
            'w': w.word,
            'ph': w.phonetic_us,
            'm': meaning,
            'u': w.unit.number if w.unit else 0,
            'ln': w.list_number or 0,
            's': status,
            'sn': '会' if status == 'mastered' else '不会',
            'rc': p.review_count if p else 0,
            'lt': r.created_at.strftime('%Y-%m-%d') if r else '',
            'la': ACTION_NAMES.get(r.action, '') if r else '',
        })
    # 按单元号 + 单元内序号排序，与词书顺序一致（不依赖数据库主键，必要时以词文本兜底）
    word_items.sort(key=lambda x: (x['u'], x['ln'], x['w']))
    settings_obj = UserSettings.get_settings()
    return render(request, 'today_words.html', {
        'words_json': json.dumps(word_items, ensure_ascii=False),
        'word_count': len(word_items),
        'batch_size': settings_obj.batch_size,
        'today': timezone.localdate(),
    })


@require_http_methods(['POST'])
def api_today_practice_start(request):
    """从今日已背列表启动重复练习。

    服务端校验所选 ID 必须属于今日已背集合（不信任浏览器传入），
    校验通过后暂存 session，由背诵页 practice=1 读取。
    """
    try:
        data = json.loads(request.body or '{}')
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'error': '请求数据格式错误'}, status=400)
    raw_ids = data.get('word_ids') or []
    if not isinstance(raw_ids, list):
        return JsonResponse({'success': False, 'error': '参数格式错误'}, status=400)
    try:
        selected = {int(x) for x in raw_ids}
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': '单词 ID 格式错误'}, status=400)
    valid_ids = _learned_word_ids() & selected
    if not valid_ids:
        return JsonResponse({
            'success': False,
            'error': '所选单词不在复习自选列表中，请刷新后重选',
        }, status=400)
    # 保留用户勾选顺序（列表页按词书顺序勾选），去重后落 session
    ordered = [w.id for w in Word.objects.filter(id__in=valid_ids)
               .select_related('unit').order_by('unit__number', 'list_number')]
    request.session['practice_word_ids'] = ordered
    request.session['practice_date'] = timezone.localdate().isoformat()
    return JsonResponse({
        'success': True,
        'count': len(ordered),
        'url': reverse('words:learn_session') + '?practice=1',
    })


def learn_session(request):
    mode = request.GET.get('mode', 'sequential')
    check = request.GET.get('check', 'none')
    unit_ids = [u for u in request.GET.getlist('units') if str(u).strip().isdigit()]
    scope = request.GET.get('scope', 'unknown')
    # 状态选择（今日自选开始背诵时用）：'' 全部 / 'mastered' 会 / 'unmastered' 不会
    state = request.GET.get('state', '')
    if state not in ('mastered', 'unmastered'):
        state = ''

    # 「按今日计划背新词」：有激活计划时限定计划单元范围与新学剩余量；
    # 无计划时范围=全部单元、数量沿用默认（与既有行为一致）。
    plan_mode = request.GET.get('plan') == '1'
    plan_task = _get_today_task() if plan_mode else None
    if plan_task:
        unit_ids = plan_task['unit_nums']

    # 重复练习（今日已背 · 临时自测）：词表来自 session 中经服务端校验的 ID，
    # 二次限制在今日已背集合内；强制纯背诵（不写任何正式学习数据）。
    practice_only = request.GET.get('practice') == '1'
    practice_ids = None
    if practice_only:
        check = 'none'
        raw = request.session.get('practice_word_ids') or []
        try:
            session_ids = [int(x) for x in raw]
        except (TypeError, ValueError):
            session_ids = []
        valid = _learned_word_ids() & set(session_ids)
        if not valid:
            return redirect('words:today_words')
        # 保持勾选时的词书顺序
        practice_ids = [wid for wid in session_ids if wid in valid]

    settings_obj = UserSettings.get_settings()
    try:
        batch_size = max(1, min(int(request.GET.get('batch') or settings_obj.batch_size), 1000))
    except (TypeError, ValueError):
        batch_size = settings_obj.batch_size

    # 高效背诵 v3 参数：每个词连续背几遍 + 完成后随机穿插重现几次
    def _gate_int(name, default, lo, hi):
        try:
            v = int(request.GET.get(name, default))
        except (TypeError, ValueError):
            return default
        return max(lo, min(v, hi))

    gate_repeats = _gate_int('repeats', 3, 1, 10)
    gate_interleave = _gate_int('interleave', 1, 0, 5)

    # 本轮背诵总数量（留空或非法 = 范围内全部），按每批 batch_size 分批进行
    count_limit = None
    if request.GET.get('count'):
        try:
            v = int(request.GET.get('count'))
            if v >= 1:
                count_limit = min(v, 5000)
        except (TypeError, ValueError):
            count_limit = None
    elif plan_task and plan_task['new_remaining'] > 0:
        # 按今日计划：新学剩余量作为本轮数量上限
        count_limit = plan_task['new_remaining']

    query = Word.objects.all()
    daily_pick_ids = None
    fav_ids = None
    if practice_only:
        # 重复练习：词表完全由 session 中校验过的 ID 决定，忽略 scope/units 等参数
        query = query.filter(id__in=practice_ids)
    elif scope == 'daily':
        # 今日自选：以当天勾选清单为准（跨天未背完的自动顺延），忽略单元参数
        rollover_daily_picks()
        daily_pick_ids = list(
            DailyPick.objects.filter(date=timezone.localdate())
            .order_by('id').values_list('word_id', flat=True)
        )
        query = query.filter(id__in=daily_pick_ids)
        # 状态筛选：今日自选开始背诵时可选「全部 / 会 / 不会」（与复习自选两态一致）
        if state == 'mastered':
            query = query.filter(progress__status='mastered')
        elif state == 'unmastered':
            query = query.exclude(progress__status='mastered')
    elif scope == 'favorites':
        # 收藏词汇：以收藏表为准，忽略单元参数；顺序与收藏列表一致（最新收藏在前）
        fav_ids = list(
            Favorite.objects.order_by('-created_at').values_list('word_id', flat=True)
        )
        query = query.filter(id__in=fav_ids)
    elif unit_ids:
        query = query.filter(unit__number__in=unit_ids)

    # 今日自选 / 收藏词汇均为「选了就背」，不做掌握状态/永不忘记过滤；其余范围沿用旧规则
    if not practice_only and scope not in ('daily', 'favorites'):
        # 永不忘记的词（is_excluded）不再出现
        query = query.exclude(progress__is_excluded=True)

        if scope == 'mastered':
            # 会的词：已掌握
            query = query.filter(progress__status='mastered')
        elif scope == 'all':
            # 所有的词：不会 + 会全部出现（永不忘记的词已在上面排除）
            pass
        else:
            # 不会的词：未掌握（默认）
            query = query.exclude(progress__status='mastered')

    words = list(query)

    if mode in ('random', 'cover_en', 'cover_zh'):
        random.shuffle(words)
    elif practice_only and practice_ids:
        # 顺序模式按勾选顺序排列
        practice_order = {wid: i for i, wid in enumerate(practice_ids)}
        words.sort(key=lambda w: practice_order.get(w.id, 1 << 30))
    elif scope == 'daily' and daily_pick_ids:
        # 顺序模式下按用户勾选的先后排列
        pick_order = {wid: i for i, wid in enumerate(daily_pick_ids)}
        words.sort(key=lambda w: pick_order.get(w.id, 1 << 30))
    elif scope == 'favorites' and fav_ids:
        # 顺序模式与收藏列表一致（最新收藏在前）
        fav_order = {wid: i for i, wid in enumerate(fav_ids)}
        words.sort(key=lambda w: fav_order.get(w.id, 1 << 30))
    else:
        words = sorted(words, key=lambda w: (w.unit.number, w.list_number))

    # 乱序模式下先洗牌再截取（相当于随机抽 N 个）；顺序模式取前 N 个
    if count_limit:
        words = words[:count_limit]

    # 批量预取进度，避免 N+1
    word_ids = [w.id for w in words]
    progress_map = {
        p.word_id: p for p in StudyProgress.objects.filter(word_id__in=word_ids)
    }
    favorite_ids = set(
        Favorite.objects.filter(word_id__in=word_ids).values_list('word_id', flat=True)
    )

    word_data = []
    for w in words:
        p = progress_map.get(w.id)
        word_data.append({
            'id': w.id,
            'word': w.word,
            'phonetic_us': w.phonetic_us,
            'pos': w.pos,
            'meanings': w.get_meanings(),
            'meanings_by_pos': w.get_meanings_by_pos(),
            'example_en': w.example_en,
            'example_zh': w.example_zh,
            'unit_number': w.unit.number if w.unit else None,
            'uncommon_pos': parse_uncommon_pos(p.uncommon_pos if p else None),
            'is_favorite': w.id in favorite_ids,
        })

    # 用 session 记录词 ID 列表（供复习页用）；重复练习不覆盖，避免污染正式背诵的断点
    if not practice_only:
        request.session['learn_words'] = [w['id'] for w in word_data]
        request.session['learn_mode'] = mode

    return render(request, 'learn_session.html', {
        'word_count': len(word_data),
        'mode': mode,
        'check': check,
        'batch_size': batch_size,
        'gate_answer_show': settings_obj.gate_answer_show,
        'gate_repeats': gate_repeats,
        'gate_interleave': gate_interleave,
        'words_json': json.dumps(word_data, ensure_ascii=False),
        'practice_only': practice_only,
    })


def daily_pick_page(request):
    """今日自选选词页：按单元浏览 + 搜索勾选，选完直接进入背诵。"""
    today = timezone.localdate()
    rollover_daily_picks(today)
    units = Unit.objects.all().order_by('number')
    picked_ids = list(
        DailyPick.objects.filter(date=today).order_by('id').values_list('word_id', flat=True)
    )
    progress_map = {
        p.word_id: p
        for p in StudyProgress.objects.only('word_id', 'status', 'is_excluded')
    }
    word_items = []
    for w in Word.objects.select_related('unit').only(
            'id', 'word', 'phonetic_us', 'meanings', 'meanings_by_pos',
            'unit__number', 'list_number'):
        p = progress_map.get(w.id)
        meaning = w.meanings_display()
        if len(meaning) > 40:
            meaning = meaning[:40] + '…'
        word_items.append({
            'id': w.id,
            'w': w.word,
            'ph': w.phonetic_us,
            'm': meaning,
            'u': w.unit.number if w.unit else 0,
            'n': w.list_number,
            's': p.status if p else 'new',
            'x': bool(p and p.is_excluded),
        })
    settings_obj = UserSettings.get_settings()
    pick_records = [{
        'id': r.id,
        'name': r.name,
        'count': len(r.words.all()),
        'created_at': timezone.localtime(r.created_at).strftime('%Y-%m-%d %H:%M'),
    } for r in PickRecord.objects.prefetch_related('words')]
    return render(request, 'daily_pick.html', {
        'units': units,
        'picked_ids_json': json.dumps(picked_ids, ensure_ascii=False),
        'words_json': json.dumps(word_items, ensure_ascii=False),
        'picked_count': len(picked_ids),
        'batch_size': settings_obj.batch_size,
        'pick_records': pick_records,
        'pick_records_json': json.dumps(pick_records, ensure_ascii=False),
    })


@require_http_methods(["POST"])
def api_pick_record_create(request):
    """把当前今日自选清单快照保存为一条选词记录（保留勾选顺序）。"""
    try:
        data = json.loads(request.body or '{}')
    except (ValueError, TypeError):
        data = {}
    today = timezone.localdate()
    rollover_daily_picks(today)
    picked_ids = list(
        DailyPick.objects.filter(date=today).order_by('id').values_list('word_id', flat=True)
    )
    if not picked_ids:
        return JsonResponse({'success': False, 'error': '清单为空，先勾选要背的词'}, status=400)
    name = (data.get('name') or '').strip()
    if not name:
        name = '选词记录 ' + timezone.localtime().strftime('%Y-%m-%d %H:%M')
    name = name[:100]
    record = PickRecord.objects.create(name=name)
    PickRecordWord.objects.bulk_create([
        PickRecordWord(record=record, word_id=wid, position=i)
        for i, wid in enumerate(picked_ids)
    ])
    return JsonResponse({
        'success': True,
        'record': {
            'id': record.id,
            'name': record.name,
            'count': len(picked_ids),
            'created_at': timezone.localtime(record.created_at).strftime('%Y-%m-%d %H:%M'),
        },
    })


@require_http_methods(["POST"])
def api_pick_record_load(request, record_id):
    """载入选词记录到今日清单（替换当前清单，保留记录顺序）。"""
    record = get_object_or_404(PickRecord, id=record_id)
    try:
        data = json.loads(request.body or '{}')
    except (ValueError, TypeError):
        data = {}
    today = timezone.localdate()
    # 先滚动旧日期未完成的自选，再清空当天并按快照写入，确保当天清单严格等于该记录
    rollover_daily_picks(today)
    ordered_ids = list(
        PickRecordWord.objects.filter(record=record).order_by('position', 'id')
        .values_list('word_id', flat=True)
    )
    DailyPick.objects.filter(date=today).delete()
    DailyPick.objects.bulk_create([
        DailyPick(date=today, word_id=wid) for wid in ordered_ids
    ], ignore_conflicts=True)
    return JsonResponse({'success': True, 'count': len(ordered_ids),
                         'picked_ids': ordered_ids})


@require_http_methods(["POST"])
def api_pick_record_rename(request, record_id):
    """重命名选词记录。"""
    record = get_object_or_404(PickRecord, id=record_id)
    try:
        data = json.loads(request.body or '{}')
    except (ValueError, TypeError):
        data = {}
    name = (data.get('name') or '').strip()
    if not name:
        return JsonResponse({'success': False, 'error': '名称不能为空'}, status=400)
    record.name = name[:100]
    record.save(update_fields=['name'])
    return JsonResponse({'success': True, 'name': record.name})


@require_http_methods(["POST"])
def api_pick_record_delete(request, record_id):
    """删除选词记录。"""
    record = get_object_or_404(PickRecord, id=record_id)
    record.delete()
    return JsonResponse({'success': True})


@require_http_methods(["POST"])
def api_daily_pick_toggle(request):
    """加入 / 移出今日自选清单。"""
    try:
        data = json.loads(request.body or '{}')
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'error': '请求数据格式错误'}, status=400)
    word = get_object_or_404(Word, id=data.get('word_id'))
    today = timezone.localdate()
    rollover_daily_picks(today)
    existing = DailyPick.objects.filter(date=today, word=word).first()
    if existing:
        existing.delete()
        picked = False
    else:
        try:
            DailyPick.objects.create(date=today, word=word)
        except IntegrityError:
            pass
        picked = True
    total = DailyPick.objects.filter(date=today).count()
    return JsonResponse({'success': True, 'picked': picked, 'total': total})


@require_http_methods(["POST"])
def api_daily_pick_clear(request):
    """清空今日自选清单。"""
    DailyPick.objects.filter(date=timezone.localdate()).delete()
    return JsonResponse({'success': True, 'total': 0})


@require_http_methods(["POST"])
def api_daily_pick_random(request):
    """随机抽取：从「没背过 / 背过」的词里各抽指定数量，加入今日清单。

    已会按 StudyProgress.status == 'mastered' 判定，其余算没背过；
    永不忘记（is_excluded）与已在今日清单中的词不参与抽取。
    """
    try:
        data = json.loads(request.body or '{}')
    except (ValueError, TypeError):
        return JsonResponse({'success': False, 'error': '请求数据格式错误'}, status=400)
    try:
        unknown_count = max(0, min(int(data.get('unknown_count') or 0), 1000))
        mastered_count = max(0, min(int(data.get('mastered_count') or 0), 1000))
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': '数量格式错误'}, status=400)
    if unknown_count == 0 and mastered_count == 0:
        return JsonResponse({'success': False, 'error': '请至少设置一个抽取数量'}, status=400)

    today = timezone.localdate()
    rollover_daily_picks(today)
    picked_ids = list(DailyPick.objects.filter(date=today).values_list('word_id', flat=True))

    base = Word.objects.exclude(progress__is_excluded=True)
    pools = []
    if mastered_count > 0:
        pools.append((base.filter(progress__status='mastered'), mastered_count))
    if unknown_count > 0:
        pools.append((base.exclude(progress__status='mastered'), unknown_count))

    added_ids = []
    for qs, n in pools:
        picked = list(qs.exclude(id__in=picked_ids).order_by('?')[:n])
        DailyPick.objects.bulk_create(
            [DailyPick(date=today, word_id=w.id) for w in picked],
            ignore_conflicts=True,
        )
        added_ids.extend(w.id for w in picked)

    total = DailyPick.objects.filter(date=today).count()
    return JsonResponse({
        'success': True,
        'added_ids': added_ids,
        'added': len(added_ids),
        'total': total,
    })


def review_start(request):
    """复习：选择单元与范围（与背诵入口一致）"""
    units = Unit.objects.annotate(
        mastered=Count('words__progress', filter=Q(words__progress__status='mastered'))
    ).all()
    settings_obj = UserSettings.get_settings()
    presets = StudyPreset.objects.filter(preset_type='review')
    for p in presets:
        p.params_json = json.dumps(p.params or {}, ensure_ascii=False)
    # 今日复习数据：目标与到期数/昨日新学/今日新学均按计划范围（无计划则全部单元）
    today = timezone.localdate()
    yesterday = today - timedelta(days=1)
    task = _get_today_task(today)
    base_progress = StudyProgress.objects.exclude(status='new').exclude(is_excluded=True)
    if task['unit_nums']:
        base_progress = base_progress.filter(word__unit__number__in=task['unit_nums'])
    due_today = base_progress.filter(
        Q(next_review__isnull=True) | Q(next_review__lte=today)).count()
    # 每日新学记录：昨天新学的词今天可专项复习；今天新学的明天可复习
    new_yesterday = base_progress.filter(learned_date=yesterday).count()
    new_today = base_progress.filter(learned_date=today).count()
    return render(request, 'review_start.html', {
        'units': units, 'batch_size': settings_obj.batch_size,
        'batch_options': [10, 20, 30, 50],
        'daily_review_target': task['review_target'],
        'plan': task['plan'],
        'due_today': due_today,
        'new_yesterday': new_yesterday,
        'new_today': new_today,
        'presets': presets,
    })


def review_session(request):
    """复习：从选定范围（单元/掌握状态）的已学词汇中抽取。
    抽取优先级（每天固定复习，直到完全掌握）：
    - ① 昨天背错的词：最优先重练；
    - ② 昨天新学的词：第二天必须巩固；
    - ③ 今日到期词：昨天学会/复习过的词今天自动进入队列；历史数据无
      next_review 的视为今天到期，保证存量已学词不丢失；
    - ④ 其余已学词：按学习日期最久优先补足（今天刚学的词排在最后）；
    - 高优先级组内乱序，保证每次复习顺序不同；
    - mode=random 开始新一轮随机复习（清空已复习记录）
    - random=1 继续上一轮随机复习（跳过已复习过的词）
    - count=N 指定本轮抽取数量（默认使用每日复习目标）
    """
    today = timezone.localdate()
    settings_obj = UserSettings.get_settings()

    # 范围参数：units 多选单元编号；scope 掌握状态
    unit_ids = [u for u in request.GET.getlist('units') if str(u).strip().isdigit()]
    scope = request.GET.get('scope', 'all')

    # 「按今日计划复习」：有激活计划时以计划单元范围为唯一范围、计划目标为复习量；
    # 无计划时回退为全部单元 + 设置里的每日复习目标（与既有行为一致）。
    plan_mode = request.GET.get('plan') == '1'
    if plan_mode:
        task = _get_today_task(today)
        unit_ids = task['unit_nums']
        review_target = task['review_target']
    else:
        review_target = settings_obj.daily_review_target

    # 随机复习模式
    random_mode = request.GET.get('mode') == 'random' or request.GET.get('random') == '1'
    if request.GET.get('mode') == 'random':
        # 新一轮随机复习：清空历史记录
        request.session['random_review_done'] = []

    # 本轮抽取数量
    target = max(1, review_target)
    if request.GET.get('count'):
        try:
            target = max(1, min(int(request.GET.get('count')), 500))
        except (TypeError, ValueError):
            pass

    # 每批数量
    try:
        batch_size = max(1, min(int(request.GET.get('batch') or settings_obj.batch_size), 100))
    except (TypeError, ValueError):
        batch_size = settings_obj.batch_size

    query = StudyProgress.objects.exclude(status='new').select_related('word')
    # 永不忘记的词（is_excluded）不再出现
    query = query.exclude(is_excluded=True)
    if unit_ids:
        query = query.filter(word__unit__number__in=unit_ids)
    if scope == 'unmastered':
        query = query.exclude(status='mastered')
    elif scope == 'mastered':
        query = query.filter(status='mastered')
    elif scope == 'yesterday':
        # 专项复习：只复习昨天新学的词（第二天巩固）
        query = query.filter(learned_date=today - timedelta(days=1))

    all_progress = list(query)

    # —— 自动复习调度（每天固定复习，直到完全掌握）——
    # 优先级：① 昨天背错 → ② 昨天新学 → ③ 今日到期 → ④ 更早已学按最久优先。
    # 今天刚学会的词安排在明天，不混入今天的复习队列。
    def _is_due(p):
        return p.next_review is None or p.next_review <= today

    yesterday = today - timedelta(days=1)

    def _priority(p):
        if p.learned_date == yesterday and p.error_count > 0:
            return 0
        if p.learned_date == yesterday:
            return 1
        if _is_due(p):
            return 2
        return 3

    # 今日真正到期的词数量（不参与补足统计）
    due_total = sum(1 for p in all_progress if _is_due(p))

    # 随机复习：跳过本轮已复习过的词（session 记录）
    done_ids = request.session.get('random_review_done', []) or []
    if random_mode and done_ids:
        done_set = set(done_ids)
        all_progress = [p for p in all_progress if p.id not in done_set]

    # 分组调度：高优先级在前；①②③组内乱序，④按学习日期最久优先
    buckets = ([], [], [], [])
    for p in all_progress:
        buckets[_priority(p)].append(p)
    for i in (0, 1, 2):
        random.shuffle(buckets[i])
    buckets[3].sort(key=lambda p: (p.learned_date or today))
    ordered = buckets[0] + buckets[1] + buckets[2] + buckets[3]

    total_candidate = len(ordered)  # 本次能复习的全部词
    batch = ordered[:target]

    # 记录本轮已复习的词（随机模式）
    if random_mode and batch:
        done_ids = request.session.get('random_review_done', []) or []
        done_ids.extend(p.id for p in batch)
        request.session['random_review_done'] = done_ids[-2000:]

    favorite_ids = set(
        Favorite.objects.filter(word_id__in=[p.word_id for p in batch])
        .values_list('word_id', flat=True)
    )

    word_data = []
    for p in batch:
        w = p.word
        word_data.append({
            'id': w.id,
            'word': w.word,
            'phonetic_us': w.phonetic_us,
            'pos': w.pos,
            'meanings': w.get_meanings(),
            'meanings_by_pos': w.get_meanings_by_pos(),
            'example_en': w.example_en,
            'example_zh': w.example_zh,
            'error_count': p.error_count,
            'review_count': p.review_count,
            'status': p.status,
            'uncommon_pos': parse_uncommon_pos(p.uncommon_pos),
            'yesterday': p.learned_date == yesterday,
            'is_favorite': w.id in favorite_ids,
        })

    mastered_count = len([p for p in batch if p.status == 'mastered'])
    unmastered_count = len(batch) - mastered_count

    # 范围描述（复习页顶部提示用）
    scope_label = {
        'all': '全部已学词',
        'unmastered': '未掌握词',
        'mastered': '已掌握词',
    }.get(scope, '全部已学词')
    if unit_ids:
        unit_names = Unit.objects.filter(number__in=unit_ids).values_list('number', flat=True)
        unit_label = '、'.join('Unit %s' % n for n in sorted(unit_names))
    else:
        unit_label = '全部单元'

    # 继续随机复习链接参数
    units_param = '&'.join('units=%s' % u for u in unit_ids)

    return render(request, 'review_session.html', {
        'review_count': len(word_data),
        'words_json': json.dumps(word_data, ensure_ascii=False),
        'total_due': due_total,  # 范围内「今日到期」的已学词数（昨天学的今天自动到期）
        'remaining_due': max(0, total_candidate - len(batch)),
        'daily_review_target': settings_obj.daily_review_target,
        'batch_size': batch_size,
        'batch_date': today.isoformat(),
        'unmastered_in_batch': unmastered_count,
        'mastered_in_batch': mastered_count,
        'scope_label': scope_label,
        'unit_label': unit_label,
        'random_mode': random_mode,
        'random_count': target,
        'scope': scope,
        'units_param': units_param,
        'gate_answer_show': settings_obj.gate_answer_show,
    })


def statistics(request):
    return render(request, 'stats.html')


def exam(request):
    """模拟考试：配置页"""
    units = Unit.objects.all()
    return render(request, 'exam.html', {'units': units})


def study_plan(request):
    plans = StudyPlan.objects.all().order_by('-is_active', '-created_at')
    today = timezone.localdate()
    # 为激活计划计算今日学习数据
    plan_today = None
    active_plan = StudyPlan.objects.filter(is_active=True).first()
    if active_plan:
        plan_today = _get_plan_today_data(active_plan, today)
    return render(request, 'plan.html', {'plans': plans, 'plan_today': plan_today})


def plan_create(request):
    """创建/编辑学习计划的独立页面"""
    units = Unit.objects.all()
    plan = None
    plan_id = request.GET.get('plan_id')
    if plan_id:
        plan = get_object_or_404(StudyPlan, id=plan_id)
    return render(request, 'plan_form.html', {'units': units, 'plan': plan})


def settings_page(request):
    settings_obj = UserSettings.get_settings()
    ai_models = AIModel.objects.all().order_by('-enabled', 'id')
    return render(request, 'settings.html', {
        'settings': settings_obj,
        'ai_models': ai_models,
        'app_version': 'v' + settings.APP_VERSION,
    })


def favorites_list(request):
    favs = Favorite.objects.select_related('word__unit').all()
    return render(request, 'favorites.html', {'favorites': favs})


def weak_words(request):
    """薄弱词（错误知识库）：从全部薄弱词中精选最需要巩固的 50 个。
    使用统一风险引擎 compute_risk_score 计算风险档位：
      - 真实错误事件时间衰减 + 错误率修正 + 未掌握/逾期修正 - 连续答对修正
      - 手动风险覆盖(risk_override)永远优先于自动计算
    筛选必须纳入所有 risk_override 非空的词（即使已掌握）。
    """
    today = timezone.localdate()
    progress_qs = list(
        StudyProgress.objects
        .filter(Q(error_count__gt=0) | Q(status__in=['learning', 'reviewing'])
                | Q(risk_override__isnull=False))
        .select_related('word__unit')
        .prefetch_related('word__favorite')
    )

    now = timezone.now()
    # 批量预取真实错误事件，避免逐词查询（列表大时是明显 N+1）
    events_map = _batch_error_events([p.word_id for p in progress_qs])
    pool = []
    total_errors = 0
    reviewed = 0
    high_cnt = 0
    med_cnt = 0
    for p in progress_qs:
        total_errors += p.error_count
        if p.review_count > 0:
            reviewed += 1
        rs = _progress_risk_summary(p, today, events=events_map.get(p.word_id, []))
        if rs['risk_tier'] == 2:
            high_cnt += 1
        elif rs['risk_tier'] == 1:
            med_cnt += 1
        # 排序主键：手动覆盖档位 > 自动风险分 > 错误次数 > 复习次数
        override_rank = -1 if p.risk_override is None else p.risk_override
        pool.append((rs, p, override_rank))

    def _sort_key(x):
        rs, p, override_rank = x
        score = rs['risk_score'] if rs['risk_score'] is not None else 0.0
        # 手动覆盖(1/0)在该档位内排最前，再按自动分高低，再按错误/复习次数
        return (-rs['risk_tier'], -override_rank, -score,
                -p.error_count, -p.review_count)

    pool.sort(key=_sort_key)
    selected = pool[:50]

    items = []
    for rs, p, _ in selected:
        w = p.word
        item = {
            'id': w.id,
            'word': w.word,
            'phonetic_us': w.phonetic_us,
            'phonetic_uk': w.phonetic_uk,
            'pos': w.pos,
            'meanings': w.get_meanings(),
            'meanings_by_pos': w.get_meanings_by_pos(),
            'status': p.status,
            'status_label': p.get_status_display(),
            'mastery_level': p.mastery_level,
            'error_count': p.error_count,
            'review_count': p.review_count,
            'unit_number': w.unit.number,
            'unit_name': w.unit.name,
            'is_favorite': hasattr(w, 'favorite'),
        }
        item.update(rs)
        item['score'] = rs['risk_score'] if rs['risk_score'] is not None else rs['risk_tier']
        items.append(item)

    stats = {
        'total': len(pool),
        'unmastered': sum(1 for _, p, _ in pool if p.status != 'mastered'),
        'total_errors': total_errors,
        'reviewed': reviewed,
        'high': high_cnt,
        'medium': med_cnt,
    }
    return render(request, 'weak_words.html', {
        'items': items,
        'stats': stats,
        'selected_count': len(items),
        'pool_count': len(pool),
    })


# ─── API 视图 ───────────────────────────────────────────────

def api_words(request):
    search = request.GET.get('search', '')
    category = request.GET.get('category', '')
    unit = request.GET.get('unit', '')
    status = request.GET.get('status', '')
    sort = request.GET.get('sort', '')

    # 分页参数校验：非法值回退到默认值，per_page 限制上限防止内存耗尽
    try:
        page = int(request.GET.get('page', 1))
    except (TypeError, ValueError):
        page = 1
    if page < 1:
        page = 1
    try:
        per_page = int(request.GET.get('per_page', 50))
    except (TypeError, ValueError):
        per_page = 50
    if per_page < 1:
        per_page = 50
    elif per_page > 200:
        per_page = 200

    qs = Word.objects.select_related('unit', 'progress').all()
    if search:
        qs = qs.filter(Q(word__icontains=search) | Q(meanings__icontains=search))
    if category:
        qs = qs.filter(category=category)
    if unit:
        try:
            unit_num = int(unit)
        except (TypeError, ValueError):
            return JsonResponse({'words': [], 'total': 0, 'page': page,
                                 'total_excluded': 0})
        qs = qs.filter(unit__number=unit_num)
    if status:
        if status == 'unknown':
            qs = qs.filter(
                Q(progress__isnull=True) |
                Q(progress__status__in=['new', 'learning', 'reviewing'])
            )
        elif status == 'excluded':
            qs = qs.filter(progress__is_excluded=True)
        else:
            qs = qs.filter(progress__status=status)

    # 排序：alpha=按单词字母序，空=默认(单元+序号)
    if sort == 'alpha':
        qs = qs.order_by('word')
    elif sort == 'alpha_desc':
        qs = qs.order_by('-word')

    total = qs.count()
    start = (page - 1) * per_page
    words_qs = qs[start:start + per_page]

    words_data = []
    for w in words_qs:
        has_fav = hasattr(w, 'favorite')
        p = getattr(w, 'progress', None)
        words_data.append({
            'id': w.id,
            'word': w.word,
            'phonetic_us': w.phonetic_us,
            'phonetic_uk': w.phonetic_uk,
            'pos': w.pos,
            'meanings': w.get_meanings(),
            'meanings_by_pos': w.get_meanings_by_pos(),
            'uncommon_meanings': w.get_uncommon_meanings(),
            'collocations': w.get_collocations(),
            'example_en': w.example_en,
            'example_zh': w.example_zh,
            'category': w.category,
            'unit_number': w.unit.number,
            'unit_name': w.unit.name,
            'status': p.status if p else 'new',
            'mastery_level': p.mastery_level if p else 0,
            'is_excluded': p.is_excluded if p else False,
            'is_favorite': has_fav,
        })

    total_excluded = Word.objects.filter(progress__is_excluded=True).count()

    return JsonResponse({
        'words': words_data,
        'total': total,
        'page': page,
        'total_excluded': total_excluded,
    })


def api_word_detail(request, word_id):
    word = get_object_or_404(Word.objects.select_related('unit', 'progress'), id=word_id)
    progress, _ = StudyProgress.objects.get_or_create(word=word)
    is_fav = hasattr(word, 'favorite')
    note_obj = getattr(word, 'note', None)

    data = {
        'id': word.id,
        'word': word.word,
        'phonetic_us': word.phonetic_us,
        'phonetic_uk': word.phonetic_uk,
        'pos': word.pos,
        'meanings': word.get_meanings(),
        'meanings_by_pos': word.get_meanings_by_pos(),
        'uncommon_meanings': word.get_uncommon_meanings(),
        'collocations': word.get_collocations(),
        'word_forms': word.get_word_forms(),
        'example_en': word.example_en,
        'example_zh': word.example_zh,
        'category': word.category,
        'unit_number': word.unit.number,
        'unit_name': word.unit.name,
        'list_number': word.list_number,
        'progress': {
            'status': progress.status,
            'mastery_level': progress.mastery_level,
            'review_count': progress.review_count,
            'error_count': progress.error_count,
            'last_review': progress.last_review,
            'next_review': progress.next_review,
        },
        'is_favorite': is_fav,
        'note': {'content': note_obj.content, 'page_number': note_obj.page_number} if note_obj else None,
    }
    return JsonResponse(data)


def _record_study(word, action, data=None, mode='', mistake_type='', error_detail=None):
    """写一条背词历史记录；失败不影响主流程。
    mistake_type: 错误类型(spelling/meaning/meaning_confusion/pos/collocation/other)
    error_detail: {"user_answer": 你的答案, "correct": 标准答案}
    来源优先取 data['source']；未传或非法时可用 mode 覆盖（如 'exam'）；
    mode 与 payload['mode'] 同时存在时拼接，仍可区分大方向与细分模式。
    """
    try:
        payload = data or {}
        valid_sources = {c[0] for c in StudyRecord.SOURCE_CHOICES}
        src = payload.get('source', '')
        if src not in valid_sources:
            # mode 形如 'exam_choice' / 'exam_spelling' / 'pos_identify' 时，
            # 允许用其前缀覆盖 source，保证考试、词性练习来源真实可查。
            prefix = (mode or '').split('_', 1)[0]
            src = prefix if prefix in valid_sources else 'learn'
        sub_mode = payload.get('mode') or ''
        parts = [p for p in ((mode or ''), sub_mode) if p]
        mode_str = '/'.join(parts)[:30]
        valid_types = {c[0] for c in StudyRecord.MISTAKE_CHOICES}
        mtype = mistake_type if mistake_type in valid_types else ''
        detail = error_detail if isinstance(error_detail, dict) else {}
        StudyRecord.objects.create(
            word=word,
            action=action,
            source=src,
            mode=mode_str,
            mistake_type=mtype,
            error_detail=detail,
        )
    except Exception:
        pass


def _exam_mistake_type(qtype, direction):
    """根据考试题型与方向推断错误事件类型。
    - spelling/meaning 默写：spelling / meaning
    - 选择：英选汉=义项混淆；汉选英=拼写/词形辨识不清（视为其他）
    """
    if qtype == 'spelling':
        return 'spelling'
    if qtype == 'meaning':
        return 'meaning'
    if qtype == 'choice':
        return 'meaning_confusion' if direction == 'en2zh' else 'other'
    return ''


def _exam_error_detail(qtype, word, ans, sq):
    """构造考试错误的错误详情（你的答案 vs 标准答案）"""
    if qtype in ('spelling', 'meaning'):
        return {'user_answer': ans.get('answer') or '',
                'correct': word.word if qtype == 'spelling' else word.get_meanings()}
    selected = ans.get('selected')
    options = sq.get('options') or []
    ci = sq.get('correct_index')
    return {
        'user_answer': options[selected] if isinstance(selected, int) and 0 <= selected < len(options) else selected,
        'correct': options[ci] if isinstance(ci, int) and 0 <= ci < len(options) else '',
    }


CONSECUTIVE_THRESHOLD = 20

# ─── 错误风险引擎（只依据真实错误事件）────────────────────
DECAY_HALFLIFE_DAYS = 21      # 错误事件衰减半衰期（天）
MISTAKE_WEIGHTS = {
    'spelling': 1.2,          # 拼写
    'meaning': 1.0,           # 释义
    'meaning_confusion': 1.5, # 义项混淆
    'pos': 1.5,               # 词性
    'collocation': 1.8,       # 搭配
    'other': 0.8,             # 其他
    'unknown': 0.8,           # 普通「不会」（无具体错因）
}
RISK_ERR_RATE_W = 1.2        # 错误率平滑修正权重
RISK_UNMASTERED_W = 0.5      # 未掌握修正
RISK_OVERDUE_W = 0.3         # 逾期修正
RISK_CORRECT_W = 0.2         # 连续答对修正（每项扣减）
RISK_SCORE_CAP = 10.0        # 风险分封顶，避免高频无限抬分
RISK_TIER_LOW = 1.5
RISK_TIER_MEDIUM = 4.0
# 错误动作全集：真实答错才会写这些 action
RISK_ERROR_ACTIONS = ('spelling_wrong', 'meaning_wrong', 'unknown')


def _mistake_weight_for_record(rec):
    """从背词记录解析错误类型与权重；旧记录无 mistake_type 时按 action 兜底。"""
    mtype = rec.mistake_type
    if not mtype:
        mtype = 'spelling' if rec.action == 'spelling_wrong' else (
            'meaning' if rec.action == 'meaning_wrong' else 'unknown')
    return MISTAKE_WEIGHTS.get(mtype, MISTAKE_WEIGHTS['other'])


def compute_risk_score(progress, today=None, events=None):
    """计算某词的风险分并定档。只依据真实错误事件的时间衰减做证据，
    不使用 last_review 作为「近期仍错」的证据；答对表现只通过连续答对降分。
    返回 (score, tier, reasons, overridden)：
      tier: 0低 1中 2高
      reasons: 供展示的推荐理由列表
      overridden: 是否命中了手动覆盖
    events: 可选，传入该词预先批量查出的错误记录列表，避免逐词查询造成 N+1。
    """
    today = today or timezone.localdate()
    if progress.risk_override is not None:
        tier = min(max(int(progress.risk_override), 0), 2)
        label = {0: '手动低风险', 1: '手动中风险', 2: '手动高风险'}[tier]
        return (None, tier, [label], True)

    reasons = []
    # 1) 真实错误事件时间衰减
    if events is None:
        events = StudyRecord.objects.filter(
            word=progress.word, action__in=RISK_ERROR_ACTIONS).order_by('created_at')
    decay_score = 0.0
    for rec in events:
        days = (today - timezone.localdate(rec.created_at)).days
        if days < 0:
            days = 0
        weight = _mistake_weight_for_record(rec)
        decay_score += weight * math.exp(-days / DECAY_HALFLIFE_DAYS)
    if decay_score > 0:
        reasons.append(f'近期错误事件衰减分 {decay_score:.2f}')

    # 2) 错误率平滑修正（遇到 history sparse / attempt 少时避免异常放大）
    attempts = (progress.spelling_attempts + progress.meaning_attempts
                + progress.review_count)
    err_rate = (progress.error_count + 1) / (attempts + 4) if attempts is not None else 0
    rate_penalty = RISK_ERR_RATE_W * err_rate
    # 3) 未掌握修正
    unmastered = RISK_UNMASTERED_W if progress.status != 'mastered' else 0.0
    # 4) 逾期修正
    overdue = 0.0
    if progress.next_review is None or progress.next_review <= today:
        overdue = RISK_OVERDUE_W
    # 5) 连续答对修正（只降分）
    correct_credit = RISK_CORRECT_W * max(progress.consecutive_correct, 0)

    score = (decay_score + rate_penalty + unmastered + overdue - correct_credit)
    score = max(0.0, min(score, RISK_SCORE_CAP))

    if rate_penalty > 0.05:
        reasons.append(f'错误率 {progress.error_count} 次/回答 {attempts} 次')
    if unmastered:
        reasons.append('尚未掌握')
    if overdue:
        reasons.append('复习已到期')

    if score >= RISK_TIER_MEDIUM:
        tier = 2
    elif score >= RISK_TIER_LOW:
        tier = 1
    else:
        tier = 0
    return (round(score, 3), tier, reasons, False)


def _progress_risk_summary(p, today=None, events=None):
    """返回含风险档位与原因的业务结构，供视图/排序复用。
    events: 批量预取的错误记录列表，避免每张词表触发一次查询。"""
    score, tier, reasons, overridden = compute_risk_score(p, today, events=events)
    return {
        'risk_score': score,
        'risk_tier': tier,
        'risk_label': {0: '低', 1: '中', 2: '高'}[tier],
        'risk_reasons': reasons,
        'risk_overridden': overridden,
        'risk_override': p.risk_override,
    }


def _batch_error_events(word_ids):
    """按词批量取出真实错误事件（RISK_ERROR_ACTIONS），按 word_id 分桶返回。
    供 weak_words、_get_today_task 等列表视图一次性拉取，避免 N+1。"""
    if not word_ids:
        return {}
    events = (StudyRecord.objects
              .filter(word_id__in=word_ids, action__in=RISK_ERROR_ACTIONS)
              .only('word_id', 'action', 'mistake_type', 'created_at')
              .order_by('created_at'))
    buckets = {}
    for rec in events:
        buckets.setdefault(rec.word_id, []).append(rec)
    return buckets


def _sort_bucket_with_risk_jitter(bucket, key_fn):
    """桶内排序 + 同优先级区间随机。
    先按 key_fn 升序，再按「相邻 key 相同」切分成组打乱；
    避免早期按固定位置每3个一切、把不同优先级的词混进同一随机组。
    """
    ordered = sorted(bucket, key=key_fn)
    if len(ordered) <= 1:
        return ordered
    groups = []
    start = 0
    prev_key = key_fn(ordered[0])
    for idx in range(1, len(ordered) + 1):
        cur = key_fn(ordered[idx]) if idx < len(ordered) else None
        if cur != prev_key:
            groups.append(ordered[start:idx])
            start = idx
            prev_key = cur
    result = []
    for g in groups:
        if len(g) > 1:
            random.shuffle(g)
        result.extend(g)
    return result


def _track_consecutive(progress, correct):
    """维护连续答对计数；连续答对达到阈值时自动标记为永不忘记（is_excluded）。

    背诵/复习共用：答对 +1，答错清零；返回是否触发自动排除。
    """
    if correct:
        progress.consecutive_correct += 1
        if progress.consecutive_correct >= CONSECUTIVE_THRESHOLD:
            progress.is_excluded = True
            progress.status = 'mastered'
            return True
    else:
        progress.consecutive_correct = 0
    return False


def _schedule_next_review(progress, today):
    """按「每天固定复习，直到完全掌握」自动安排下次复习时间：
    - 已标记「永不忘记/完全掌握」(is_excluded) 的词：不再自动排期（置空），
      它们不会出现在任何复习队列中；
    - 其余单词（无论是今天首次学会，还是今天已复习/默写过）：下次复习都安排在
      「明天」→ 今天学的单词明天就会自动出现在复习栏目里。
    """
    if progress.is_excluded:
        progress.next_review = None
    else:
        progress.next_review = today + timedelta(days=1)


def _apply_wrong_progress(word, today=None, now=None):
    """把一次已确认的真实答错应用到 StudyProgress，与既有考试错题口径一致：
    增加错误计数、记录最近复习时间、安排明天复习；已掌握词保持「会」不降级，
    仅清零连续答对计数。供词性学习等入口复用，保证错误知识库/风险推荐不漏词。
    返回更新后的 StudyProgress（失败时不抛出，返回 None）。"""
    try:
        today = today or timezone.localdate()
        now = now or timezone.now()
        progress, created = StudyProgress.objects.get_or_create(word=word)
        if created or progress.status == 'new':
            progress.learned_date = today
            progress.is_today_new = True
        progress.error_count += 1
        if progress.status == 'mastered':
            # 已掌握词一次失误不清零降级，仅清零连续答对并安排明天巩固
            progress.consecutive_correct = 0
        else:
            progress.mastery_level = 0
            progress.status = 'learning'
            _track_consecutive(progress, False)
        progress.review_count += 1
        progress.last_review = now
        _schedule_next_review(progress, today)
        progress.save()
        return progress
    except Exception:
        return None


@require_http_methods(['POST'])
def api_mark_word(request, action, word_id):
    try:
        word = get_object_or_404(Word, id=word_id)
        progress, created = StudyProgress.objects.get_or_create(word=word)
        today = timezone.localdate()
        now = timezone.now()

        if created or progress.status == 'new':
            progress.learned_date = today
            progress.is_today_new = True

        if action == 'mastered':
            progress.mastery_level = 5
            progress.status = 'mastered'
            auto_excluded = _track_consecutive(progress, True)
        elif action in ('fuzzy', 'unknown'):
            progress.mastery_level = 0
            progress.status = 'learning'
            progress.error_count += 1
            _track_consecutive(progress, False)
            auto_excluded = False
        else:
            return JsonResponse({'error': '无效操作'}, status=400)

        progress.review_count += 1
        progress.last_review = now
        # 自动复习调度：今天学会/复习过的词 → 明天自动进入复习栏目
        _schedule_next_review(progress, today)
        progress.save()

        # 更新今日正确率统计
        checkin, _ = DailyCheckIn.objects.get_or_create(date=today)
        if action == 'mastered':
            checkin.today_correct += 1
        else:
            checkin.today_wrong += 1
        checkin.save()

        # 记录背词历史
        try:
            mark_data = json.loads(request.body or '{}')
        except Exception:
            mark_data = {}
        _record_study(word, 'known' if action == 'mastered' else 'unknown', mark_data)

        auto_checked = update_daily_checkin()

        return JsonResponse({
            'success': True,
            'status': progress.status,
            'mastery_level': progress.mastery_level,
            'auto_checked': auto_checked,
            'auto_excluded': auto_excluded,
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@require_http_methods(['POST'])
def api_mark_skip(request, word_id):
    """标记单词为「永不忘记」：之后不再出现在背诵/复习中。"""
    try:
        word = get_object_or_404(Word, id=word_id)
        progress, _ = StudyProgress.objects.get_or_create(word=word)
        progress.is_excluded = True
        progress.next_review = None  # 永不忘记的词不再自动排期
        if progress.status == 'new':
            progress.status = 'mastered'  # 视为已掌握，保证统计口径一致
        progress.save()
        try:
            skip_data = json.loads(request.body or '{}')
        except Exception:
            skip_data = {}
        _record_study(word, 'skip', skip_data)
        return JsonResponse({
            'success': True,
            'is_excluded': True,
            'status': progress.status,
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@require_http_methods(['POST'])
def api_mark_unskip(request, word_id):
    """取消「永不忘记」标记：恢复出现在背诵/复习中。"""
    try:
        word = get_object_or_404(Word, id=word_id)
        progress = StudyProgress.objects.filter(word=word).first()
        if not progress:
            return JsonResponse({'success': True, 'is_excluded': False})
        progress.is_excluded = False
        progress.save()
        return JsonResponse({
            'success': True,
            'is_excluded': False,
            'status': progress.status,
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@require_http_methods(['POST'])
def api_risk_override(request, word_id):
    """手动覆盖风险档位：0=低 1=中 2=高，传 -1 清除覆盖恢复自动计算。"""
    try:
        word = get_object_or_404(Word, id=word_id)
        progress, _ = StudyProgress.objects.get_or_create(word=word)
        try:
            payload = json.loads(request.body or '{}')
        except Exception:
            payload = {}
        try:
            level = int(payload.get('level', request.POST.get('level', 0)) or 0)
        except (TypeError, ValueError):
            return JsonResponse({'error': 'level 必须是整数'}, status=400)

        if level == -1:
            progress.risk_override = None
        elif level in (0, 1, 2):
            progress.risk_override = level
        else:
            return JsonResponse({'error': 'level 需为 -1/0/1/2'}, status=400)
        progress.save()

        score, tier, reasons, overridden = compute_risk_score(progress)
        return JsonResponse({
            'success': True,
            'risk_override': progress.risk_override,
            'risk_tier': tier,
            'risk_score': score,
            'risk_reasons': reasons,
            'risk_overridden': overridden,
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


MEANING_JUDGE_PROMPT = '''你是考研英语词汇老师，负责判定学生默写的单词中文释义是否合格。

单词：{word}
标准释义：{meanings}

学生的答案：{answer}

请判定学生答案是否表达了该单词的核心含义：
1. 只要学生答案大致表达了核心含义（同义词、近义表达、简洁概括、个别用词不准均可，但不能有明显错误），就算「correct」，本模式要求宽松，大致对即可判对。
2. 义项一致性检查（重点）：标准释义有几个义项，学生答案就要与这些义项一一对应。即使大部分义项都大致正确，只要学生答案中出现「与原词含义严重不符、明显是另一个意思」的义项（例如把“苹果”写成“香蕉”这类方向性错误），就必须判「wrong」，绝不能因整体大致对而放行。
3. 只有沾边但完全没写到点子上、或明显答非所问的，才算「partial」或「wrong」。
4. 完全不对、明显错误、或与单词无关的，算「wrong」。

只输出 JSON，不要输出其他内容，格式：
{{"verdict": "correct 或 partial 或 wrong", "comment": "一句话中文点评，指出对在哪或错在哪，并给出参考释义。若因某个义项严重不符而判 wrong，必须明确指出学生写错的这一处内容、正确的参考义项、以及判错理由"}}
'''


# 模拟考试交卷残留题的批量判定：同一次请求内统一标准，消除逐题独立调用的判定漂移
MEANING_BATCH_JUDGE_PROMPT = '''你是考研英语词汇老师，需要一次性判定 {n} 个单词的中文释义默写。
重要：所有题目必须使用**完全相同、宽松一致**的标准——同质量的答案在任何一题下结论都必须相同，不得前松后严。

判定标准：
1. 答案大致表达核心含义（同义词、近义表达、简洁概括、个别用词不准）即「correct」，不要苛求字面一致。
2. 义项一致性：若答案中出现与该单词严重不符、明显是另一个意思的义项，必须判「wrong」。
3. 沾边但没写到核心含义的判「partial」。
4. 完全不对、答非所问、空答案判「wrong」。

待判定题目（JSON 数组）：
{items}

只输出 JSON 数组，不要输出任何其他内容，元素与输入一一对应、id 必须原样返回：
[{{"id": 题号, "verdict": "correct 或 partial 或 wrong", "comment": "一句话中文点评"}}]
'''


def _normalize_spelling(text):
    """拼写判定归一化：小写、去首尾空格、去掉非字母数字字符（容错连字符/撇号/空格）"""
    return re.sub(r'[^a-z0-9]', '', (text or '').lower().strip())


def _local_meaning_match(word, answer):
    """本地释义比对：去标点空白后，答案与任一标准释义互相包含即命中"""
    meanings = word.get_meanings()
    by_pos = word.get_meanings_by_pos()
    meaning_parts = list(meanings)
    for pos, ms in by_pos.items():
        if ms:
            meaning_parts.append('%s：%s' % (pos, '；'.join(ms)))
    norm_answer = re.sub(r'[\s，。、；：,.!?；、（）()\[\]【】""\'\']', '', answer or '')
    if not norm_answer:
        return False, meaning_parts
    for m in meaning_parts:
        norm_m = re.sub(r'[\s，。、；：,.!?；、（）()\[\]【】""\'\']', '', m)
        if not norm_m:
            continue
        if norm_m in norm_answer or norm_answer in norm_m:
            return True, meaning_parts
    return False, meaning_parts


def _policy_local(verdict, comment, source='local_exact', confidence='high'):
    """构造本地确定性结论（is_final=True）。"""
    return {'verdict': verdict, 'source': source, 'is_final': True,
            'reason_code': '', 'confidence': confidence, 'comment': comment}


def _policy_pending(reason_code, comment=None):
    """构造待复核结论（未定论：不得据此判错、不得改掌握状态）。
    comment 以解读原因，但不泄露任何内部异常细节。"""
    return {'verdict': 'pending_review', 'source': 'pending', 'is_final': False,
            'reason_code': reason_code or 'ai_unavailable', 'confidence': 'low',
            'comment': comment or '已保存，待 AI 可用后复核'}


def judge_with_policy(word, answer, data, kind='meaning'):
    """统一判定服务（HANDOVER 第八节）：
    空答/拼写等确定性题 → 本地立即判定；释义等语义题 → 本地高置信规则先筛，
    其余交 AI 仲裁；AI 超时/失败/输出非法时**绝不默认判错**，一律 pending_review。
    只判定、不写库。返回 policy dict：
    verdict ∈ correct/partial/wrong/pending_review；
    source ∈ local_exact/local_confident/ai/ai_consensus/manual/pending。"""
    answer = (answer or '').strip()
    if not answer:
        return _policy_local('wrong', '未作答')

    settings_obj = UserSettings.get_settings()
    # 本地强匹配先筛：明显一致的直接判对，省一次 AI 调用（仅作「高置信正确」）
    hit, meaning_parts = _local_meaning_match(word, answer)
    if hit:
        return _policy_local('correct', '本地判定：与标准释义一致')

    # 未开启 AI 判定：本地不能确认，交给待复核而不是默认判错
    if not settings_obj.use_ai_meaning_check:
        return _policy_pending('ai_disabled')

    if settings_obj.meaning_check_model and not data.get('model_id'):
        data['model_id'] = settings_obj.meaning_check_model_id

    try:
        cfg = resolve_ai_model(data)
    except ValueError:
        return _policy_pending('ai_no_model', '已保存，待配置可用 AI 模型后复核')

    prompt = MEANING_JUDGE_PROMPT.format(
        word=word.word, meanings='；'.join(meaning_parts), answer=answer)
    content, _error, reason = _ai_judge_call(cfg, prompt, max_tokens=500, temperature=0.1)
    if not content:
        return _policy_pending(reason)
    parsed = _extract_json_object(content) or {}
    v = parsed.get('verdict')
    if v not in ('correct', 'partial', 'wrong'):
        return _policy_pending('ai_invalid_output')
    return {'verdict': v, 'source': 'ai', 'is_final': True,
            'reason_code': '', 'confidence': 'medium',
            'comment': (parsed.get('comment') or '').strip()}


def _judge_meaning_core(word, answer, data):
    """释义判定统一入口（返回 policy dict，见 judge_with_policy）。"""
    return judge_with_policy(word, answer, data)


def _judge_meaning_answer(word, answer, data):
    """释义默写判定（返回 policy dict）。"""
    return judge_with_policy(word, answer, data)


def judge_meaning_batch_with_policy(pairs, data, chunk_size=10):
    """批量释义判定，pairs=[(word, answer), ...]；返回 {word_id: policy dict}。
    本地确定层先筛 → 其余按 chunk_size 分组，每组一次 AI 调用统一标准。
    整组失败 / 缺项 / 组内同一 id 出现互相冲突的 verdict → 该组所有未定论题
    一律 pending_review（不再逐题兜底判错）。"""
    results = {}
    pending = []
    for word, answer in pairs:
        answer = (answer or '').strip()
        if not answer:
            results[word.id] = _policy_local('wrong', '未作答')
            continue
        hit, _ = _local_meaning_match(word, answer)
        if hit:
            results[word.id] = _policy_local('correct', '本地判定：与标准释义一致')
        else:
            pending.append((word, answer))

    if not pending:
        return results

    settings_obj = UserSettings.get_settings()
    use_ai = settings_obj.use_ai_meaning_check

    for start in range(0, len(pending), chunk_size):
        group = pending[start:start + chunk_size]
        if use_ai and settings_obj.meaning_check_model and not data.get('model_id'):
            data['model_id'] = settings_obj.meaning_check_model_id
        got, reason = {}, None
        if use_ai:
            cfg = None
            try:
                cfg = resolve_ai_model(data)
            except ValueError:
                reason = 'ai_no_model'
            if cfg is not None:
                items = [
                    {'id': w.id, 'word': w.word,
                     'meanings': '；'.join(_local_meaning_match(w, a)[1]),
                     'answer': a}
                    for w, a in group
                ]
                prompt = MEANING_BATCH_JUDGE_PROMPT.format(
                    n=len(items), items=json.dumps(items, ensure_ascii=False))
                content, _err, reason = _ai_judge_call(
                    cfg, prompt, max_tokens=400 * len(items), temperature=0.1)
                if content:
                    conflict = False
                    for el in _extract_json_array(content):
                        if not isinstance(el, dict):
                            continue
                        vid, v = el.get('id'), el.get('verdict')
                        if vid is None or v not in ('correct', 'partial', 'wrong'):
                            continue
                        key = int(vid)
                        if key in got and got[key][0] != v:
                            conflict = True
                        got.setdefault(key, (v, (el.get('comment') or '').strip()))
                    if conflict:
                        got, reason = {}, 'ai_conflict'
                    elif not all(w.id in got for w, _ in group):
                        got, reason = {}, 'ai_invalid_output'
                else:
                    got = {}
        else:
            reason = 'ai_disabled'

        for w, _a in group:
            if w.id in got:
                results[w.id] = {
                    'verdict': got[w.id][0], 'source': 'ai', 'is_final': True,
                    'reason_code': '', 'confidence': 'medium',
                    'comment': got[w.id][1]}
            else:
                results[w.id] = _policy_pending(reason or 'ai_unavailable')
    return results


def _judge_meaning_batch(pairs, data, chunk_size=10):
    """兼容包装：批量释义判定转调 judge_meaning_batch_with_policy。"""
    return judge_meaning_batch_with_policy(pairs, data, chunk_size)


def _ensure_session_key(request):
    """确保当前浏览器会话有稳定 session_key（用于待复核去重），返回该 key。"""
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key or ''


def _pending_dedupe_key(kind, source, mode, word_id, session_key):
    """待复核去重键：同一次浏览器会话里的同一题（词 + 题型 + 来源 + 模式）只保留一条未复核。
    刻意不用答案文本做键——同一答案在不同学习轮次应保留独立审计记录；
    会话标识做哈希，避免把 session key 原文落库。"""
    digest = hashlib.sha256((session_key or '').encode('utf-8')).hexdigest()[:16]
    return ('%s:%s:%s:%s:%s' % (kind, source or 'learn', mode or '-', word_id, digest))[:64]


def _get_or_create_pending_judgment(word, kind, answer, *, question=None, mode='',
                                    source='learn', reason_code='', attempt=None,
                                    dedupe_key=''):
    """建立或复用待复核记录（幂等入口）。
    dedupe_key 为稳定去重标识；同一键最多保留一条**未复核**记录，
    并发双击 / 断网重发由数据库部分唯一约束兜住；重复提交只更新答案与题面。"""
    answer = (answer or '').strip()
    defaults = {
        'user_answer': answer[:2000],
        'question': question or {},
        'mode': (mode or '')[:30],
        'source': (source or 'learn')[:20],
        'reason_code': reason_code or '',
        'attempt': attempt,
    }
    obj, created = PendingJudgment.objects.get_or_create(
        word=word, kind=kind, dedupe_key=dedupe_key or '', resolved=False,
        defaults=defaults)
    if not created:
        fields = []
        if obj.user_answer != defaults['user_answer']:
            obj.user_answer = defaults['user_answer']
            fields.append('user_answer')
        if defaults['question'] and obj.question != defaults['question']:
            obj.question = defaults['question']
            fields.append('question')
        if reason_code and obj.reason_code != reason_code:
            obj.reason_code = reason_code
            fields.append('reason_code')
        if attempt is not None and obj.attempt_id != attempt.id:
            obj.attempt = attempt
            fields.append('attempt')
        if fields:
            obj.save(update_fields=fields)
    return obj


def _apply_meaning_verdict(word, verdict, *, data=None, mode='meaning', source='learn',
                           record=True, mistake_type='meaning', error_detail=None,
                           attempt=None):
    """把一个**已定论**的释义结果落到学习进度与统计（correct/partial/wrong）。
    pending_review 不走这里；本函数与 `_apply_wrong_progress` 口径一致，
    是最终结论的唯一写库入口，保证补判/人工改判后只写一次。"""
    today = timezone.localdate()
    now = timezone.now()
    checkin, _ = DailyCheckIn.objects.get_or_create(date=today)

    if verdict in ('correct', 'partial'):
        progress, created = StudyProgress.objects.get_or_create(word=word)
        if created or progress.status == 'new':
            progress.learned_date = today
            progress.is_today_new = True
        progress.meaning_attempts += 1
        progress.meaning_correct += 1
        progress.mastery_level = 5
        progress.status = 'mastered'
        _track_consecutive(progress, True)
        progress.review_count += 1
        progress.last_review = now
        _schedule_next_review(progress, today)
        progress.save()
        checkin.today_correct += 1
        checkin.save()
        if record:
            _record_study(word, 'meaning_ok', data or {})
        return progress, checkin

    # 已确认的真实答错：与既有考试错题口径一致（mastered 不降级，仅 error_count+1）
    progress = _apply_wrong_progress(word, today, now)
    if progress is not None:
        progress.meaning_attempts += 1
        progress.save(update_fields=['meaning_attempts'])
    checkin.today_wrong += 1
    checkin.save()
    if record:
        _record_study(word, 'meaning_wrong', data or {}, mode=mode,
                      mistake_type=mistake_type, error_detail=error_detail or {})
    return progress, checkin


def _finalize_pending_judgment(pj, verdict, comment, source, by, data=None):
    """把待复核记录定论并落库；幂等——resolved/applied 后直接返回，不重复写学习记录。
    verdict ∈ correct/partial/wrong；by ∈ ai/manual。返回 (ok, message)。"""
    if verdict not in ('correct', 'partial', 'wrong'):
        return False, '无效的判定结论'
    with transaction.atomic():
        fresh = PendingJudgment.objects.select_for_update().get(pk=pj.pk)
        if fresh.resolved or fresh.applied:
            return True, '已复核'
        word = fresh.word
        if fresh.kind == 'pos_open':
            if verdict == 'wrong':
                _apply_wrong_progress(word)
                _record_study(
                    word, 'meaning_wrong', {'source': 'pos'},
                    mode='pos_open', mistake_type='pos',
                    error_detail={'user_answer': fresh.user_answer[:200],
                                  'judged_by': by})
            if fresh.attempt_id:
                PosLearningAttempt.objects.filter(pk=fresh.attempt_id).update(
                    result=verdict, judged_by=by, pending_review=False,
                    reason_code='', feedback=(comment or '')[:200])
        else:
            _apply_meaning_verdict(
                word, verdict, data=dict(data or {}),
                mode=(fresh.mode or fresh.kind), source=(fresh.source or 'learn'),
                mistake_type='meaning',
                error_detail={'user_answer': fresh.user_answer,
                              'correct': word.get_meanings(),
                              'judged_by': by})
        fresh.resolved = True
        fresh.final_verdict = verdict
        fresh.resolved_by = by
        fresh.resolved_at = timezone.now()
        fresh.applied = True
        fresh.save(update_fields=['resolved', 'final_verdict', 'resolved_by',
                                  'resolved_at', 'applied'])
    return True, ''


@require_http_methods(['POST'])
def api_spelling_check(request, word_id):
    """拼写检测：看中文默写英文，本地判定是否与标准拼写一致"""
    try:
        data = json.loads(request.body)
        answer = (data.get('answer') or '').strip()
        word = get_object_or_404(Word, id=word_id)
        progress, created = StudyProgress.objects.get_or_create(word=word)
        today = timezone.localdate()
        now = timezone.now()

        if created or progress.status == 'new':
            progress.learned_date = today
            progress.is_today_new = True

        correct = bool(answer) and _normalize_spelling(answer) == _normalize_spelling(word.word)

        progress.spelling_attempts += 1
        if correct:
            progress.spelling_correct += 1
            progress.mastery_level = 5
            progress.status = 'mastered'
        else:
            progress.error_count += 1
            progress.mastery_level = 0
            progress.status = 'learning'
        auto_excluded = _track_consecutive(progress, correct)
        progress.review_count += 1
        progress.last_review = now
        # 自动复习调度：今天默写过的词 → 明天自动进入复习栏目
        _schedule_next_review(progress, today)
        progress.save()

        checkin, _ = DailyCheckIn.objects.get_or_create(date=today)
        if correct:
            checkin.today_correct += 1
        else:
            checkin.today_wrong += 1
        checkin.save()

        # 记录背词历史
        if correct:
            _record_study(word, 'spelling_ok', data)
        else:
            _record_study(word, 'spelling_wrong', data,
                          mistake_type='spelling',
                          error_detail={'user_answer': answer, 'correct': word.word})

        auto_checked = update_daily_checkin()

        return JsonResponse({
            'success': True,
            'correct': correct,
            'status': progress.status,
            'auto_checked': auto_checked,
            'auto_excluded': auto_excluded,
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@require_http_methods(['POST'])
def api_meaning_check(request, word_id):
    """释义默写：看英文写中文意思。本地强匹配即时判定，其余交 AI 仲裁；
    AI 不可用/超时/输出非法时落「待复核」，不判错、不改掌握状态。"""
    try:
        data = json.loads(request.body)
        answer = (data.get('answer') or '').strip()
        word = get_object_or_404(Word, id=word_id)

        policy = _judge_meaning_answer(word, answer, data)
        verdict = policy['verdict']
        comment = policy['comment']

        if verdict == 'pending_review':
            # 待复核：只留「尝试过」的痕迹，绝不写错误事件、不改掌握状态与今日对错
            today = timezone.localdate()
            progress, created = StudyProgress.objects.get_or_create(word=word)
            if created or progress.status == 'new':
                progress.learned_date = today
                progress.is_today_new = True
            progress.meaning_attempts += 1
            progress.last_review = timezone.now()
            progress.save()
            pending = _get_or_create_pending_judgment(
                word, 'meaning', answer, mode=(data.get('mode') or ''),
                source=(data.get('source') or 'learn'),
                reason_code=policy['reason_code'],
                dedupe_key=_pending_dedupe_key(
                    'meaning', data.get('source') or 'learn',
                    data.get('mode') or '-', word.id, _ensure_session_key(request)))
            auto_checked = update_daily_checkin()
            return JsonResponse({
                'success': True, 'verdict': 'pending_review', 'comment': comment,
                'reason_code': policy['reason_code'], 'pending_id': pending.id,
                'used_ai': False, 'status': progress.status,
                'auto_checked': auto_checked, 'auto_excluded': False,
            })

        progress, _checkin = _apply_meaning_verdict(
            word, verdict, data=data, mode='',
            source=(data.get('source') or 'learn'), mistake_type='meaning',
            error_detail={'user_answer': answer, 'correct': word.get_meanings()})
        auto_checked = update_daily_checkin()

        return JsonResponse({
            'success': True,
            'verdict': verdict,
            'comment': comment,
            'source': policy['source'],
            'used_ai': policy['source'] == 'ai',
            'status': progress.status if progress else 'learning',
            'auto_checked': auto_checked,
            'auto_excluded': bool(progress and progress.is_excluded),
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@require_http_methods(['POST'])
def api_toggle_mastery(request, word_id):
    """在 会(mastered) 与 不会(未掌握) 之间切换"""
    try:
        word = get_object_or_404(Word, id=word_id)
        progress, _ = StudyProgress.objects.get_or_create(word=word)
        today = timezone.localdate()

        checkin, _ = DailyCheckIn.objects.get_or_create(date=today)
        if progress.status == 'mastered':
            # → 不会
            progress.status = 'learning'
            progress.mastery_level = 0
            # 标记为不会 → 明天自动回到复习队列再巩固
            _schedule_next_review(progress, today)
            progress.save()
            checkin.today_wrong += 1
            checkin.save()
            auto_checked = update_daily_checkin()
            return JsonResponse({
                'success': True,
                'is_mastered': False,
                'status_label': '不会',
                'auto_checked': auto_checked,
            })
        else:
            # → 会
            progress.status = 'mastered'
            progress.mastery_level = 5
            progress.review_count += 1
            progress.last_review = timezone.now()
            if progress.learned_date is None:
                progress.learned_date = today
            # 标记为会 → 明天自动进入复习队列巩固
            _schedule_next_review(progress, today)
            progress.save()
            checkin.today_correct += 1
            checkin.save()
            auto_checked = update_daily_checkin()
            return JsonResponse({
                'success': True,
                'is_mastered': True,
                'status_label': '会',
                'auto_checked': auto_checked,
            })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@require_http_methods(['POST'])
def api_mark_uncommon_pos(request, word_id):
    """标记/取消标记某个词性为陌生（add/remove）。"""
    try:
        data = json.loads(request.body or '{}')
        pos = str(data.get('pos', '')).strip()
        action = data.get('action', 'add')
        if not pos:
            return JsonResponse({'error': '缺少词性'}, status=400)
        if action not in ('add', 'remove'):
            return JsonResponse({'error': 'action 参数无效'}, status=400)
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({'error': '参数格式错误'}, status=400)

    # 处理并发 get_or_create 的竞争条件
    try:
        progress, _ = StudyProgress.objects.get_or_create(word_id=word_id)
    except IntegrityError:
        progress = StudyProgress.objects.get(word_id=word_id)

    uncommon = parse_uncommon_pos(progress.uncommon_pos)

    if action == 'add' and pos not in uncommon:
        uncommon.append(pos)
    elif action == 'remove' and pos in uncommon:
        uncommon = [p for p in uncommon if p != pos]

    progress.uncommon_pos = json.dumps(uncommon, ensure_ascii=False)
    progress.save(update_fields=['uncommon_pos'])
    return JsonResponse({'success': True, 'uncommon_pos': uncommon})


def api_today(request):
    today = timezone.localdate()
    task = _get_today_task(today)

    # 今日新词：范围内未学词，取今日剩余量（不足则有多少取多少）
    new_words = task['new_candidates'][:task['new_remaining']]
    new_list = [{
        'id': w.id,
        'word': w.word,
        'phonetic_us': w.phonetic_us,
        'pos': w.pos,
        'meanings': w.get_meanings(),
        'meanings_by_pos': w.get_meanings_by_pos(),
    } for w in new_words]

    # 今日需复习：按统一优先级（昨天答错→昨天新学→今日到期→较早补足）取今日剩余量
    review_batch = task['review_ordered'][:task['review_remaining']]

    review_list = [{
        'id': p.word.id,
        'word': p.word.word,
        'phonetic_us': p.word.phonetic_us,
        'pos': p.word.pos,
        'meanings': p.word.get_meanings(),
        'meanings_by_pos': p.word.get_meanings_by_pos(),
        'mastery_level': p.mastery_level,
        'uncommon_pos': parse_uncommon_pos(p.uncommon_pos),
    } for p in review_batch]

    # 今日到期数（范围内：昨天学会/复习过的词今天自动到期）
    due_count = sum(1 for p in task['review_ordered']
                    if p.next_review is None or p.next_review <= today)

    return JsonResponse({
        'new_words': new_list,
        'review_words': review_list,
        'new_count': len(new_list),
        'review_count': len(review_list),
        'due_count': due_count,
        'daily_new_target': task['new_target'],
        'daily_review_target': task['review_target'],
    })


@require_http_methods(['POST'])
def api_reset_progress(request):
    try:
        data = json.loads(request.body)
        scope = data.get('scope', 'all')
        if scope == 'all':
            StudyProgress.objects.all().delete()
            DailyCheckIn.objects.all().delete()
            message = '已清空全部学习进度'
        elif scope == 'unit':
            unit_number = data.get('unit_number')
            StudyProgress.objects.filter(word__unit__number=unit_number).delete()
            message = f'已清空 List {unit_number} 的学习进度'
        else:
            return JsonResponse({'error': '无效 scope'}, status=400)

        return JsonResponse({'success': True, 'message': message})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@require_http_methods(['POST'])
def api_plan_create(request):
    try:
        data = json.loads(request.body)
        # 停用其他计划
        StudyPlan.objects.filter(is_active=True).update(is_active=False)
        plan = StudyPlan.objects.create(
            name=data.get('name', '我的学习计划'),
            daily_new_words=int(data.get('daily_new_words', 30)),
            daily_review_count=int(data.get('daily_review_count', 50)),
            target_date=datetime.strptime(data['target_date'], '%Y-%m-%d').date() if data.get('target_date') else None,
            unit_range=json.dumps(data.get('unit_range', [])),
            is_active=True,
        )
        return JsonResponse({'success': True, 'plan_id': plan.id})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@require_http_methods(['POST'])
def api_plan_update(request, plan_id):
    try:
        data = json.loads(request.body)
        plan = get_object_or_404(StudyPlan, id=plan_id)
        if 'name' in data:
            plan.name = data['name']
        if 'daily_new_words' in data:
            plan.daily_new_words = int(data['daily_new_words'])
        if 'daily_review_count' in data:
            plan.daily_review_count = int(data['daily_review_count'])
        if 'target_date' in data and data['target_date']:
            plan.target_date = datetime.strptime(data['target_date'], '%Y-%m-%d').date()
        if 'unit_range' in data:
            plan.unit_range = json.dumps(data['unit_range'])
        if 'is_active' in data:
            if data['is_active']:
                StudyPlan.objects.filter(is_active=True).exclude(id=plan.id).update(is_active=False)
            plan.is_active = data['is_active']
        plan.save()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@require_http_methods(['POST', 'DELETE'])
def api_plan_delete(request, plan_id):
    """删除学习计划"""
    try:
        plan = get_object_or_404(StudyPlan, id=plan_id)
        plan_name = plan.name
        plan.delete()
        return JsonResponse({
            'success': True,
            'message': f'已删除计划 "{plan_name}"',
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@require_http_methods(['POST'])
def api_preset_create(request):
    """保存当前自定义配置为快速开始预设"""
    try:
        data = json.loads(request.body or '{}')
        name = (data.get('name') or '').strip()
        preset_type = data.get('preset_type')
        params = data.get('params') or {}
        if not name:
            return JsonResponse({'success': False, 'error': '请输入预设名称'}, status=400)
        if preset_type not in ('learn', 'review'):
            return JsonResponse({'success': False, 'error': '无效预设类型'}, status=400)
        preset = StudyPreset.objects.create(
            name=name, preset_type=preset_type, params=params
        )
        return JsonResponse({
            'success': True,
            'id': preset.id,
            'name': preset.name,
            'message': f'已保存预设 "{preset.name}"',
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@require_http_methods(['POST'])
def api_preset_update(request, preset_id):
    """更新预设（重命名 / 修改参数）"""
    try:
        preset = get_object_or_404(StudyPreset, id=preset_id)
        data = json.loads(request.body or '{}')
        name = (data.get('name') or '').strip()
        if not name:
            return JsonResponse({'success': False, 'error': '请输入预设名称'}, status=400)
        params = data.get('params')
        if params is not None:
            if not isinstance(params, dict):
                return JsonResponse({'success': False, 'error': '参数格式错误'}, status=400)
            preset.params = params
        preset.name = name
        preset.save()
        return JsonResponse({
            'success': True,
            'id': preset.id,
            'name': preset.name,
            'message': f'已更新预设 "{preset.name}"',
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


@require_http_methods(['POST'])
def api_preset_delete(request, preset_id):
    """删除预设"""
    try:
        preset = get_object_or_404(StudyPreset, id=preset_id)
        preset_name = preset.name
        preset.delete()
        return JsonResponse({
            'success': True,
            'message': f'已删除预设 "{preset_name}"',
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)


def _get_today_task(today=None):
    """统一今日学习任务：供仪表盘、背诵/复习首页与会话、api_today 共用。

    有激活计划时，以 StudyPlan 为唯一来源（新学目标、复习目标、单元范围）；
    无计划时回退到 UserSettings 的每日目标，范围为全部单元。
    返回结构：
      plan / unit_nums / new_target / review_target /
      new_done / review_done / new_remaining / review_remaining /
      new_candidates(queryset) / review_ordered(list[StudyProgress])
    """
    today = today or timezone.localdate()
    settings_obj = UserSettings.get_settings()
    plan = StudyPlan.objects.filter(is_active=True).first()
    unit_nums = plan.get_unit_range() if plan else []

    new_target = plan.daily_new_words if plan else settings_obj.daily_new_target
    review_target = plan.daily_review_count if plan else settings_obj.daily_review_target

    def _in_range(qs, via_word=False):
        if not unit_nums:
            return qs
        field = 'unit__number__in' if via_word else 'word__unit__number__in'
        return qs.filter(**{field: unit_nums})

    # 今日完成数（排除今日新学词被重复计入复习）
    new_done = _in_range(StudyProgress.objects.filter(learned_date=today)).count()
    review_done = _in_range(
        StudyProgress.objects.filter(last_review__date=today, review_count__gt=0)
    ).exclude(learned_date=today).count()

    # 新词候选：范围内尚无学习进度的词
    new_candidates = _in_range(Word.objects.all(), via_word=True).filter(progress__isnull=True)

    # 复习候选：范围内已学、未排除的词，按固定优先级排序
    # ① 昨天答错 → ② 昨天新学 → ③ 今日到期 → ④ 较早已学补足(学习日期最久优先)
    all_progress = list(_in_range(
        StudyProgress.objects.exclude(status='new').exclude(is_excluded=True)
    ).select_related('word'))
    yesterday = today - timedelta(days=1)

    def _is_due(p):
        return p.next_review is None or p.next_review <= today

    def _priority(p):
        if p.learned_date == yesterday and p.error_count > 0:
            return 0
        if p.learned_date == yesterday:
            return 1
        if _is_due(p):
            return 2
        return 3

    buckets = [[], [], [], []]
    for p in all_progress:
        buckets[_priority(p)].append(p)
    # 批量预取错误事件，避免桶内排序时逐词查库（列表大时是明显 N+1）
    events_map = _batch_error_events([p.word_id for p in all_progress])
    # 桶内排序：人工重点(手动等级1=熟练优先) > 手动风险档 > 自动风险分 > 到期紧迫度，再有限随机
    def _bucket_key(p):
        rs = _progress_risk_summary(p, today, events=events_map.get(p.word_id, []))
        score = rs['risk_score'] if rs['risk_score'] is not None else 0.0
        manual_rank = {1: 0, 2: 1, 3: 2}.get(p.manual_level, 2)
        override = -1 if p.risk_override is None else p.risk_override
        over = (today - (p.next_review or today)).days
        # key 越小优先级越高；_sort_bucket_with_risk_jitter 按升序取词
        return (manual_rank, -override, -score, over)
    for i in range(4):
        buckets[i] = _sort_bucket_with_risk_jitter(buckets[i], _bucket_key)
    review_ordered = buckets[0] + buckets[1] + buckets[2] + buckets[3]

    return {
        'plan': plan,
        'unit_nums': unit_nums,
        'new_target': new_target,
        'review_target': review_target,
        'new_done': new_done,
        'review_done': review_done,
        'new_remaining': max(0, new_target - new_done),
        'review_remaining': max(0, review_target - review_done),
        'new_candidates': new_candidates,
        'review_ordered': review_ordered,
    }


def _get_plan_today_data(plan, today):
    """计算激活计划的今日学习数据（供页面渲染和 API 共用）"""
    unit_nums = plan.get_unit_range()

    # 计划范围内的总词数 / 已掌握词数
    if unit_nums:
        total_words = Word.objects.filter(unit__number__in=unit_nums).count()
        mastered = StudyProgress.objects.filter(
            word__unit__number__in=unit_nums, status='mastered').count()
    else:
        total_words = Word.objects.count()
        mastered = StudyProgress.objects.filter(status='mastered').count()

    # 今日已学新词数
    today_new_done = StudyProgress.objects.filter(
        learned_date=today,
        **({'word__unit__number__in': unit_nums} if unit_nums else {})
    ).count()

    # 今日已复习数
    today_review_done = StudyProgress.objects.filter(
        last_review__date=today, review_count__gt=0,
        **({'word__unit__number__in': unit_nums} if unit_nums else {})
    ).exclude(learned_date=today).count()

    # 剩余单词
    remaining = max(0, total_words - mastered)

    # 预计天数
    est_days = (remaining + plan.daily_new_words - 1) // plan.daily_new_words if plan.daily_new_words > 0 else 0

    # 倒计时
    days_left = (plan.target_date - today).days if plan.target_date else None

    # 整体进度百分比
    overall_pct = round(mastered / total_words * 100, 1) if total_words > 0 else 0

    # 本周每日完成情况（周一到周日）
    weekday = today.weekday()  # 0=周一
    week_start = today - timedelta(days=weekday)
    weekly = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        new_cnt = StudyProgress.objects.filter(
            learned_date=d,
            **({'word__unit__number__in': unit_nums} if unit_nums else {})
        ).count()
        rev_cnt = StudyProgress.objects.filter(
            last_review__date=d, review_count__gt=0,
            **({'word__unit__number__in': unit_nums} if unit_nums else {})
        ).exclude(learned_date=d).count()
        total_cnt = new_cnt + rev_cnt
        if d > today:
            status = 'future'
        elif total_cnt == 0:
            status = 'missed'
        elif d == today:
            status = 'today'
        else:
            status = 'done'
        weekly.append({
            'date': d.isoformat(),
            'weekday': ['一', '二', '三', '四', '五', '六', '日'][i],
            'new_count': new_cnt,
            'review_count': rev_cnt,
            'total': total_cnt,
            'status': status,
        })

    return {
        'plan_id': plan.id,
        'plan_name': plan.name,
        'daily_new_target': plan.daily_new_words,
        'daily_review_target': plan.daily_review_count,
        'today_new_done': today_new_done,
        'today_review_done': today_review_done,
        'total_words': total_words,
        'mastered': mastered,
        'remaining': remaining,
        'estimated_days': est_days,
        'days_left': days_left,
        'overall_percent': overall_pct,
        'target_date': plan.target_date.isoformat() if plan.target_date else None,
        'weekly': weekly,
    }


@require_http_methods(['GET'])
def api_plan_today(request):
    """返回当前激活计划的今日学习任务与进度"""
    plan = StudyPlan.objects.filter(is_active=True).first()
    if not plan:
        return JsonResponse({'success': False, 'error': '没有激活的学习计划'}, status=404)
    today = timezone.localdate()
    data = _get_plan_today_data(plan, today)
    data['success'] = True
    return JsonResponse(data)


def api_stats(request, period):
    today = timezone.localdate()
    if period == '7':
        start_date = today - timedelta(days=7)
    elif period == '30':
        start_date = today - timedelta(days=30)
    else:
        start_date = today - timedelta(days=365)

    checkins = DailyCheckIn.objects.filter(
        date__gte=start_date, date__lte=today
    )
    checkin_map = {c.date: c for c in checkins}

    # 每日新学/复习数据按进度实时统计（不依赖打卡快照），保证近几天数据完整
    # 新学：learned_date 在范围内的所有词
    new_by_day = StudyProgress.objects.filter(
        learned_date__gte=start_date
    ).values('learned_date').annotate(cnt=Count('id'))
    new_map = {x['learned_date']: x['cnt'] for x in new_by_day}

    # 复习：last_review 在范围内，且 learned_date 早于该日期（排除当天新学的词）
    review_by_day = StudyProgress.objects.filter(
        last_review__date__gte=start_date, review_count__gt=0
    ).exclude(learned_date__gte=F('last_review__date')) \
     .values('last_review__date').annotate(cnt=Count('id'))
    review_map = {x['last_review__date']: x['cnt'] for x in review_by_day}

    # 连续日期序列：无学习记录的日期补 0，保证趋势图覆盖整个周期（含今天）
    days_data = []
    d = start_date
    while d <= today:
        c = checkin_map.get(d)
        days_data.append({
            'date': d.isoformat(),
            'new_words': new_map.get(d, 0),
            'reviewed': review_map.get(d, 0),
            'duration': c.study_duration if c else 0,
            'correct_rate': c.correct_rate if c else 0,
        })
        d += timedelta(days=1)

    summary = checkins.aggregate(
        avg_correct=Avg('correct_rate'),
        total_duration=Sum('study_duration'),
    )
    summary['total_new'] = sum(x['new_words'] for x in days_data)
    summary['total_reviewed'] = sum(x['reviewed'] for x in days_data)

    # 易错词
    error_words = StudyProgress.objects.select_related('word').filter(
        error_count__gt=0
    ).order_by('-error_count')[:20]

    error_list = [{
        'word': e.word.word,
        'error_count': e.error_count,
        'review_count': e.review_count,
        'status': e.status,
    } for e in error_words]

    # 分类进度
    cats = [('required', '必考词'), ('basic', '基础词'), ('advanced', '超纲词')]
    cat_progress = {}
    for code, label in cats:
        total = Word.objects.filter(category=code).count()
        mastered = StudyProgress.objects.filter(word__category=code, status='mastered').count()
        learning = StudyProgress.objects.filter(word__category=code).exclude(
            status='new').exclude(status='mastered').count()
        cat_progress[code] = {
            'label': label, 'total': total, 'mastered': mastered, 'learning': learning,
        }

    streak = get_streak()

    # 状态分布
    status_dist = []
    for status_code, status_label in StudyProgress.STATUS_CHOICES:
        cnt = StudyProgress.objects.filter(status=status_code).count()
        status_dist.append({'status': status_code, 'label': status_label, 'count': cnt})

    return JsonResponse({
        'days': days_data,
        'summary': {
            'total_new': summary['total_new'] or 0,
            'total_reviewed': summary['total_reviewed'] or 0,
            'avg_correct_rate': round(summary['avg_correct'] or 0, 1),
            'total_duration': summary['total_duration'] or 0,
            'streak': streak,
        },
        'error_words': error_list,
        'category_progress': cat_progress,
        'level_distribution': status_dist,
    })


def _get_week_bounds():
    """返回本周一 ~ 本周日（自然周）。"""
    today = timezone.localdate()
    week_start = today - timedelta(days=today.weekday())  # 周一是 0
    week_end = week_start + timedelta(days=6)
    return week_start, week_end


def _collect_report_summary(week_start, week_end):
    """收集自然周内的统计快照数据（不含 AI 评语）。"""
    today = timezone.localdate()
    actual_end = min(week_end, today)  # 未到周日时统计到今天

    # 周期内新学/复习（与 api_stats 同口径）
    new_words = StudyProgress.objects.filter(
        learned_date__gte=week_start, learned_date__lte=actual_end).count()
    reviewed_words = StudyProgress.objects.filter(
        last_review__date__gte=week_start, last_review__date__lte=actual_end,
        review_count__gt=0
    ).exclude(learned_date__gte=F('last_review__date')).count()

    # 打卡数据
    checkins = list(DailyCheckIn.objects.filter(
        date__gte=week_start, date__lte=actual_end))
    total_duration = sum(c.study_duration for c in checkins)
    today_total = sum(c.today_correct + c.today_wrong for c in checkins)
    today_correct = sum(c.today_correct for c in checkins)
    correct_rate = round(today_correct / today_total * 100, 1) if today_total else 0
    active_days = sum(1 for c in checkins if c.new_words_learned > 0 or c.words_reviewed > 0 or c.study_duration > 0)

    # 学习最多的一天
    best_day = None
    if checkins:
        best = max(checkins, key=lambda c: c.new_words_learned + c.words_reviewed)
        if best.new_words_learned + best.words_reviewed > 0:
            best_day = best.date.isoformat()

    # 词库总体掌握情况
    total_words = Word.objects.count()
    mastered_words = StudyProgress.objects.filter(status='mastered').count()
    learning_words = StudyProgress.objects.exclude(status='new').exclude(status='mastered').count()
    excluded_words = StudyProgress.objects.filter(is_excluded=True).count()
    not_learned = max(0, total_words - mastered_words - learning_words - excluded_words)
    mastery_rate = round(mastered_words / total_words * 100, 1) if total_words else 0

    # 完成度预测：按每日新词目标
    settings_obj = UserSettings.get_settings()
    daily_new_target = max(1, settings_obj.daily_new_target)
    days_to_finish = (not_learned + daily_new_target - 1) // daily_new_target
    finish_date = today + timedelta(days=days_to_finish)

    # 易错词 Top10
    weak_words = [{
        'word': e.word.word,
        'error_count': e.error_count,
    } for e in StudyProgress.objects.select_related('word')
        .filter(error_count__gt=0).order_by('-error_count')[:10]]

    # 陌生词性统计（uncommon_pos JSON）
    pos_counter = {}
    for p in StudyProgress.objects.exclude(uncommon_pos='[]').exclude(uncommon_pos=''):
        for pos in parse_uncommon_pos(p.uncommon_pos):
            pos_counter[pos] = pos_counter.get(pos, 0) + 1
    weak_pos = [{'pos': k, 'count': v} for k, v in
                sorted(pos_counter.items(), key=lambda x: -x[1])[:5]]

    return {
        'period_label': f'{week_start.isoformat()} ~ {week_end.isoformat()}',
        'week_start': week_start.isoformat(),
        'week_end': week_end.isoformat(),
        'actual_end': actual_end.isoformat(),
        'new_words': new_words,
        'reviewed_words': reviewed_words,
        'study_duration': total_duration,
        'correct_rate': correct_rate,
        'streak': get_streak(),
        'active_days': active_days,
        'best_day': best_day,
        'total_words': total_words,
        'mastered_words': mastered_words,
        'learning_words': learning_words,
        'not_learned': not_learned,
        'excluded_words': excluded_words,
        'mastery_rate': mastery_rate,
        'days_to_finish': days_to_finish,
        'finish_date': finish_date.isoformat(),
        'daily_new_target': daily_new_target,
        'weak_words': weak_words,
        'weak_pos': weak_pos,
    }


@require_http_methods(['GET', 'POST'])
def api_learning_report(request):
    """周学习报告：GET 查当前周报告与历史 / POST 生成（或重新生成）本周报告"""
    week_start, week_end = _get_week_bounds()

    if request.method == 'GET':
        current = LearningReport.objects.filter(week_start=week_start).first()
        history = [{
            'id': r.id,
            'week_start': r.week_start.isoformat(),
            'week_end': r.week_end.isoformat(),
            'summary': r.get_summary(),
            'ai_comment': r.ai_comment,
            'created_at': r.created_at.strftime('%Y-%m-%d %H:%M'),
        } for r in LearningReport.objects.all()[:20]]
        return JsonResponse({
            'success': True,
            'today': timezone.localdate().isoformat(),
            'is_sunday': timezone.localdate().weekday() == 6,
            'current': {
                'id': current.id,
                'week_start': current.week_start.isoformat(),
                'week_end': current.week_end.isoformat(),
                'summary': current.get_summary(),
                'ai_comment': current.ai_comment,
                'created_at': current.created_at.strftime('%Y-%m-%d %H:%M'),
            } if current else None,
            'history': history,
        })

    # POST：生成（或重新生成）本周报告
    try:
        settings_obj = UserSettings.get_settings()
        cfg = resolve_ai_model({'model_id': settings_obj.assistant_model_id} if settings_obj.assistant_model_id else {})
        summary = _collect_report_summary(week_start, week_end)
        prompt = weekly_report_prompt(json.dumps(summary, ensure_ascii=False))
        comment = _ai_chat_once(cfg, prompt, max_tokens=1000)
        report, _ = LearningReport.objects.update_or_create(
            week_start=week_start,
            defaults={
                'week_end': week_end,
                'summary_json': json.dumps(summary, ensure_ascii=False),
                'ai_comment': comment,
            },
        )
        return JsonResponse({
            'success': True,
            'id': report.id,
            'summary': summary,
            'ai_comment': comment,
            'created_at': report.created_at.strftime('%Y-%m-%d %H:%M'),
        })
    except ValueError as e:
        return JsonResponse({'error': str(e)}, status=400)
    except Exception as e:
        return JsonResponse({'error': f'生成失败：{e}'}, status=500)


@require_http_methods(['POST'])
def api_study_duration(request):
    """累加当前学习页的有效前台时长（秒）。"""
    try:
        data = json.loads(request.body or '{}')
        seconds = int(data.get('seconds', 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        return JsonResponse({'error': '时长格式错误'}, status=400)

    # 客户端每 20 秒同步一次；限制单次上报，避免异常请求造成统计失真。
    seconds = max(0, min(seconds, 90))
    if seconds == 0:
        return JsonResponse({'success': True, 'added': 0})

    checkin, _ = DailyCheckIn.objects.get_or_create(date=timezone.localdate())
    DailyCheckIn.objects.filter(pk=checkin.pk).update(study_duration=F('study_duration') + seconds)
    return JsonResponse({'success': True, 'added': seconds})


def api_export_pdf(request):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))

    export_type = request.GET.get('type', 'all')
    dictation_mode = request.GET.get('dictation', '')  # 'en2zh' or 'zh2en'
    unit_num = request.GET.get('unit')
    search = request.GET.get('search', '')
    category = request.GET.get('category', '')
    status = request.GET.get('status', '')

    # Build query based on filters
    if export_type == 'filtered':
        query = Word.objects.select_related('unit').all()
        if search:
            query = query.filter(Q(word__icontains=search) | Q(meanings__icontains=search))
        if category:
            query = query.filter(category=category)
        if unit_num:
            query = query.filter(unit__number=int(unit_num))
        if status:
            # 与列表筛选保持一致：unknown=未会(excluded之外的)，excluded=永不忘记
            if status == 'unknown':
                query = query.filter(
                    Q(progress__isnull=True) |
                    Q(progress__status__in=['new', 'learning', 'reviewing'])
                )
            elif status == 'excluded':
                query = query.filter(progress__is_excluded=True)
            else:
                query = query.filter(progress__status=status)
        words = query.order_by('unit__number', 'list_number')
        title_text = '考研单词表（筛选结果）'
    elif export_type == 'unit' and unit_num:
        words = Word.objects.filter(unit__number=int(unit_num)).order_by('list_number')
        unit_obj = Unit.objects.filter(number=int(unit_num)).first()
        title_text = f'考研单词表 - {unit_obj.name if unit_obj else "Unit "+unit_num}'
    elif export_type == 'favorites':
        words = Word.objects.filter(favorite__isnull=False).order_by('unit__number')
        title_text = '考研单词表（收藏单词）'
    elif export_type == 'pick':
        today = timezone.localdate()
        rollover_daily_picks(today)
        words = Word.objects.filter(daily_picks__date=today).order_by('daily_picks__id')
        title_text = '今日自选背诵清单'
    elif export_type == 'errors':
        words = Word.objects.filter(progress__error_count__gt=0).order_by('-progress__error_count')
        title_text = '考研单词表（易错词）'
    else:
        words = Word.objects.all().order_by('unit__number', 'list_number')
        title_text = '考研单词表（全部）'

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            topMargin=20 * mm, bottomMargin=20 * mm,
                            leftMargin=15 * mm, rightMargin=15 * mm)
    styles = getSampleStyleSheet()

    # 默写纸模式
    if dictation_mode:
        if dictation_mode == 'en2zh':
            title_text = f'{title_text} - 默写纸（看英文写中文）'
        else:
            title_text = f'{title_text} - 默写纸（看中文写英文）'

        title_style = ParagraphStyle('title', fontName='STSong-Light', fontSize=14,
                                     leading=20, spaceAfter=10, alignment=1)

        elements = [Paragraph(title_text, title_style),
                    Spacer(1, 6 * mm)]

        table_data = []
        if dictation_mode == 'en2zh':
            # 看英文写中文：显示英文，中文留空
            table_data.append(['序号', '单词', '词性', '中文释义（默写）'])
            for i, w in enumerate(words, 1):
                table_data.append([
                    str(i),
                    w.word,
                    w.pos or '',
                    '',  # 留空供默写
                ])
        else:
            # 看中文写英文：显示中文，英文留空
            table_data.append(['序号', '中文释义', '词性', '单词（默写）'])
            for i, w in enumerate(words, 1):
                meanings_str = '; '.join(w.get_meanings()[:3])
                table_data.append([
                    str(i),
                    meanings_str,
                    w.pos or '',
                    '',  # 留空供默写
                ])

        if len(table_data) > 1:
            # 根据模式调整列宽
            if dictation_mode == 'en2zh':
                col_widths = [14 * mm, 50 * mm, 18 * mm, 78 * mm]
            else:
                col_widths = [14 * mm, 68 * mm, 18 * mm, 60 * mm]

            t = Table(table_data, colWidths=col_widths, repeatRows=1)
            t.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (-1, -1), 'STSong-Light'),  # 全部使用STSong-Light以支持音标
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('FONTSIZE', (0, 0), (-1, 0), 9),
                ('GRID', (0, 0), (-1, -1), 0.3, colors.grey),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('BACKGROUND', (0, 0), (-1, 0), colors.Color(0.9, 0.9, 0.9)),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.Color(0.95, 0.95, 0.95)]),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('LEFTPADDING', (0, 0), (-1, -1), 4),
                ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ]))
            elements.append(t)
        else:
            cn_style = ParagraphStyle('cn', fontName='STSong-Light', fontSize=10,
                                      leading=16, spaceAfter=4)
            elements.append(Paragraph('暂无单词数据', cn_style))

        doc.build(elements)
        buf.seek(0)

        filename = f'hongbaoshu_{export_type}_dictation.pdf'
        return FileResponse(buf, content_type='application/pdf',
                            filename=filename, as_attachment=True)

    # 正常导出模式（包含音标）
    title_style = ParagraphStyle('title', fontName='STSong-Light', fontSize=14,
                                 leading=20, spaceAfter=10, alignment=1)

    elements = [Paragraph(title_text, title_style),
                Spacer(1, 6 * mm)]

    total_rows = len(words)
    table_data = []
    table_data.append(['序号', '单词', '词性', '释义'])
    for i, w in enumerate(words, 1):
        meanings_str = '; '.join(w.get_meanings()[:3])
        table_data.append([
            str(i),
            w.word,
            w.pos or '',
            meanings_str,
        ])

    if len(table_data) > 1:
        col_widths = [14 * mm, 50 * mm, 18 * mm, 78 * mm]
        t = Table(table_data, colWidths=col_widths, repeatRows=1)
        t.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (-1, -1), 'STSong-Light'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('FONTSIZE', (0, 0), (-1, 0), 9),
            ('GRID', (0, 0), (-1, -1), 0.3, colors.grey),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BACKGROUND', (0, 0), (-1, 0), colors.Color(0.9, 0.9, 0.9)),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.Color(0.95, 0.95, 0.95)]),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ]))
        elements.append(t)
    else:
        cn_style = ParagraphStyle('cn', fontName='STSong-Light', fontSize=10,
                                  leading=16, spaceAfter=4)
        elements.append(Paragraph('暂无单词数据', cn_style))

    doc.build(elements)
    buf.seek(0)

    filename = f'hongbaoshu_{export_type}.pdf'
    return FileResponse(buf, content_type='application/pdf',
                        filename=filename, as_attachment=True)


def api_backup_dir(request):
    """返回服务器端备份保存目录（每次备份都会在此目录留一份副本），供前端提示用户。"""
    backup_dir = settings.BACKUP_DIR
    try:
        os.makedirs(backup_dir, exist_ok=True)
    except Exception:
        pass
    return JsonResponse({'success': True, 'path': str(backup_dir)})


def api_privacy_agree(request):
    """记录用户同意《用户许可与使用协议》。仅写本机数据库，不发起任何网络请求。

    同时保存正文字本号与正文 SHA-256（由前端对弹窗可见文本计算后回传），
    用于事后举证"用户同意的是哪一份文本"；hash 无法解析时留空，不影响同意本身生效。
    """
    if request.method != 'POST':
        return JsonResponse({'error': '仅支持 POST 请求'}, status=405)
    try:
        body = json.loads(request.body or b'{}')
    except (json.JSONDecodeError, TypeError, ValueError):
        body = {}
    text_hash = str(body.get('text_hash') or '').strip().lower()
    # 只接受合法的 SHA-256 十六进制串，避免脏数据落库
    if not re.fullmatch(r'[0-9a-f]{64}', text_hash):
        text_hash = ''

    user_settings = UserSettings.get_settings()
    user_settings.privacy_agreed_version = settings.PRIVACY_VERSION
    user_settings.privacy_agreed_hash = text_hash
    user_settings.save(update_fields=['privacy_agreed_version', 'privacy_agreed_hash'])
    return JsonResponse({
        'success': True,
        'version': settings.PRIVACY_VERSION,
        'text_hash': text_hash,
    })


GITHUB_LATEST_RELEASE_API = (
    'https://api.github.com/repos/'
    '1826548016-cloud/Lexizx/releases/latest'
)
RELEASE_PAGE = ('https://github.com/1826548016-cloud/'
                'Lexizx/releases/latest')


def _parse_version(text):
    """'v3.4.0' / '3.4.0-beta' -> (3, 4, 0)；解析失败返回 None"""
    m = re.search(r'(\d+)\.(\d+)\.(\d+)', str(text or ''))
    return tuple(int(x) for x in m.groups()) if m else None


def _http_get_json(url, timeout=6):
    req = urllib.request.Request(
        url,
        headers={
            'Accept': 'application/json',
            'User-Agent': 'Postgraduate-Vocabulary-App',
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def api_check_update(request):
    """检查是否有新版本：读取 GitHub 最新 Release 信息。
    不可达时友好返回，不抛错。"""
    current = settings.APP_VERSION
    cur_tuple = _parse_version(current)

    # GitHub API 国内访问偶发被重置，失败时重试最多 3 次再报错
    data = None
    for timeout in (7, 10, 15):
        try:
            data = _http_get_json(GITHUB_LATEST_RELEASE_API, timeout=timeout)
            break
        except Exception:
            data = None
    if not data:
        return JsonResponse({
            'success': False,
            'current': 'v' + current,
            'error': '无法连接更新服务器（GitHub），请检查网络后重试',
        })
    try:
        latest_tag = data.get('tag_name') or ''
        if not _parse_version(latest_tag):
            raise ValueError('bad tag')
        payload = {
            'latest': latest_tag,
            'release_title': data.get('name') or '',
            'release_notes': data.get('body') or '',
            'html_url': data.get('html_url') or RELEASE_PAGE,
            'published_at': (data.get('published_at') or '')[:10],
        }
    except Exception:
        return JsonResponse({
            'success': False,
            'current': 'v' + current,
            'error': '更新服务器返回数据异常，请稍后重试',
        })

    new_tuple = _parse_version(payload['latest'])
    payload.update({
        'success': True,
        'current': 'v' + current,
        'has_update': bool(new_tuple and cur_tuple and new_tuple > cur_tuple),
    })
    return JsonResponse(payload)


def _knowledge_feedback_ref(fb):
    """为知识反馈导出稳定的自然引用（不使用数据库主键，跨库可解析）。
    - word：词条文本；- family：词族 key；
    - member：词族 key + 词条/词库外文本 + 词性 + 成员关系；
    - pattern：词族 key + 规范化构词元素（name+kind）；
    - relation：词族 key + 起点词 + 终点词/词库外文本 + 关系类型。
    """
    tt = fb.target_type
    if tt == 'word':
        w = Word.objects.filter(id=fb.target_id).values_list('word', flat=True).first()
        return {'word': w} if w else {}
    if tt == 'family':
        key = WordFamily.objects.filter(id=fb.target_id).values_list('key', flat=True).first()
        return {'key': key} if key else {}
    if tt == 'member':
        m = (WordFamilyMember.objects.filter(id=fb.target_id)
             .select_related('word', 'family').first())
        if not m:
            return {}
        return {
            'family_key': m.family.key,
            'word': m.word.word if m.word else None,
            'outside_word': m.outside_word,
            'pos': m.pos, 'relation': m.relation,
        }
    if tt == 'pattern':
        p = (WordFormationPattern.objects.filter(id=fb.target_id)
             .select_related('family').first())
        if not p:
            return {}
        return {
            'family_key': p.family.key if p.family else None,
            'name': p.name, 'kind': p.kind,
        }
    if tt == 'relation':
        r = (WordRelation.objects.filter(id=fb.target_id)
             .select_related('word', 'related_word', 'family').first())
        if not r:
            return {}
        return {
            'family_key': r.family.key if r.family else None,
            'word': r.word.word if r.word else None,
            'related_word': r.related_word.word if r.related_word else None,
            'outside_word': r.outside_word, 'rel_type': r.rel_type,
        }
    return {}


def _build_backup_payload(settings_obj):
    """全量备份数据结构：词库（单元+单词）、设置、学习进度、背词记录、打卡、收藏、笔记、
    速记、计划、预设、AI 模型、会话与消息、写作练习、写作好句、导入记录、学习周报。"""
    from django.core.serializers.json import DjangoJSONEncoder

    data = {
        'version': '2.1',
        'exported_at': timezone.localtime().isoformat(),
        'settings': _backup_settings(settings_obj),
        # 词库本体（v2.1 起携带，用于跨端恢复时自动补齐缺失的单元/词条）
        'units': [
            {'number': u.number, 'name': u.name,
             'category': u.category, 'word_count': u.word_count}
            for u in Unit.objects.all()
        ],
        'words': [
            {
                'word': w.word, 'phonetic_us': w.phonetic_us, 'phonetic_uk': w.phonetic_uk,
                'pos': w.pos, 'meanings': w.meanings, 'meanings_by_pos': w.meanings_by_pos,
                'uncommon_meanings': w.uncommon_meanings, 'collocations': w.collocations,
                'word_forms': w.word_forms, 'example_en': w.example_en, 'example_zh': w.example_zh,
                'category': w.category, 'unit_number': w.unit.number,
                'list_number': w.list_number,
            }
            for w in Word.objects.select_related('unit').all()
        ],
        'progress': [_backup_progress(p) for p in StudyProgress.objects.all()],
        'study_records': [
            {
                'word_id': r.word_id,
                'word': r.word.word if r.word else None,
                'action': r.action, 'source': r.source,
                'mode': r.mode, 'created_at': r.created_at.isoformat(),
                'mistake_type': r.mistake_type, 'error_detail': r.error_detail,
            }
            for r in StudyRecord.objects.all()
        ],
        'plans': [
            {
                'name': p.name, 'daily_new_words': p.daily_new_words,
                'daily_review_count': p.daily_review_count,
                'target_date': p.target_date.isoformat() if p.target_date else None,
                'unit_range': p.unit_range, 'is_active': p.is_active,
                'start_date': p.start_date.isoformat() if p.start_date else None,
            }
            for p in StudyPlan.objects.all()
        ],
        'checkins': [
            {
                'date': c.date.isoformat(), 'new_words_learned': c.new_words_learned,
                'words_reviewed': c.words_reviewed, 'study_duration': c.study_duration,
                'correct_rate': c.correct_rate, 'today_correct': c.today_correct,
                'today_wrong': c.today_wrong, 'is_checked': c.is_checked,
            }
            for c in DailyCheckIn.objects.all()
        ],
        'favorites': [
            {'word_id': f.word_id,
             'word': f.word.word if f.word else None,
             'created_at': timezone.localtime(f.created_at).isoformat()}
            for f in Favorite.objects.all()
        ],
        'notes': [
            {'word_id': n.word_id,
             'word': n.word.word if n.word else None,
             'content': n.content, 'page_number': n.page_number}
            for n in Note.objects.all()
        ],
        'quick_memories': [
            {'word_id': qm.word_id,
             'word': qm.word.word if qm.word else None,
             'content': qm.content,
             'created_at': qm.created_at.isoformat()}
            for qm in QuickMemory.objects.all()
        ],
        'daily_picks': [
            {'word_id': dp.word_id,
             'word': dp.word.word if dp.word else None,
             'date': dp.date.isoformat()}
            for dp in DailyPick.objects.all()
        ],
        # 选词记录（第十八节）：记录名 / 创建时间 / 成员单词稳定引用 + position
        'pick_records': [
            {
                'name': r.name,
                'created_at': r.created_at.isoformat(),
                'words': [
                    {'word': m.word.word, 'position': m.position}
                    for m in r.words.select_related('word').order_by('position', 'id')
                    if m.word
                ],
            }
            for r in PickRecord.objects.all()
        ],
        'presets': [
            {'name': p.name, 'preset_type': p.preset_type, 'params': p.params,
             'created_at': p.created_at.isoformat()}
            for p in StudyPreset.objects.all()
        ],
        'ai_models': [
            {
                'id': m.id, 'provider': m.provider, 'model_id': m.model_id,
                'display_name': m.display_name, 'base_url': m.base_url,
                'endpoint': m.endpoint,
                'api_key': m.api_key,
                'context': m.context,
                'vision': m.vision, 'enabled': m.enabled,
            }
            for m in AIModel.objects.all()
        ],
        'conversations': [
            {'id': c.id, 'title': c.title, 'created_at': c.created_at.isoformat()}
            for c in Conversation.objects.all()
        ],
        'chat_messages': [
            {
                'id': m.id, 'role': m.role, 'content': m.content,
                'word_id': m.word_id,
                'word': Word.objects.filter(id=m.word_id).values_list('word', flat=True).first() if m.word_id else None,
                'conversation_id': m.conversation_id,
                'attachments': m.attachments, 'created_at': m.created_at.isoformat(),
            }
            for m in ChatMessage.objects.all()
        ],
        'writing_practices': [
            {
                'id': w.id, 'question_id': w.question_id, 'mode': w.mode,
                'source': w.source, 'user_input': w.user_input,
                'ai_output': w.ai_output, 'score_json': w.score_json,
                'feedback': w.feedback, 'created_at': w.created_at.isoformat(),
            }
            for w in WritingPractice.objects.all()
        ],
        'writing_phrases': [
            {
                'id': p.id, 'practice_id': p.practice_id, 'phrase': p.phrase,
                'meaning': p.meaning, 'created_at': p.created_at.isoformat(),
            }
            for p in WritingPhrase.objects.all()
        ],
        'import_logs': [
            {
                'source': lg.source, 'unit_number': lg.unit_number,
                'unit_name': lg.unit_name, 'imported_count': lg.imported_count,
                'skipped_count': lg.skipped_count, 'words_list': lg.words_list,
                'created_at': lg.created_at.isoformat(),
            }
            for lg in ImportLog.objects.all()
        ],
        'learning_reports': [
            {
                'week_start': lr.week_start.isoformat(), 'week_end': lr.week_end.isoformat(),
                'summary_json': lr.summary_json, 'ai_comment': lr.ai_comment,
                'created_at': lr.created_at.isoformat(),
            }
            for lr in LearningReport.objects.all()
        ],
        # 词性学习 / 词族与构词法 / 知识反馈（HANDOVER 第十二节）
        'pos_attempts': [
            {
                'word': pa.word.word if pa.word else None,
                'target_pos': pa.target_pos, 'question_type': pa.question_type,
                'question': pa.question, 'user_answer': pa.user_answer,
                'result': pa.result, 'judged_by': pa.judged_by,
                'feedback': pa.feedback,
                'created_at': pa.created_at.isoformat(),
            }
            for pa in PosLearningAttempt.objects.select_related('word').all()
        ],
        # 待复核判定（第八节）：AI 暂不可用时的作答，必须随备份导出/恢复。
        # 注意：AIModelHealth 是运行时健康数据，刻意不写入备份。
        'pending_judgments': [
            {
                'word': pj.word.word if pj.word else None,
                'kind': pj.kind, 'mode': pj.mode, 'source': pj.source,
                'reason_code': pj.reason_code,
                'user_answer': pj.user_answer, 'question': pj.question,
                'dedupe_key': pj.dedupe_key,
                'resolved': pj.resolved, 'final_verdict': pj.final_verdict,
                'resolved_by': pj.resolved_by,
                'resolved_at': pj.resolved_at.isoformat() if pj.resolved_at else None,
                'applied': pj.applied,
                'created_at': pj.created_at.isoformat(),
            }
            for pj in PendingJudgment.objects.select_related('word').all()
        ],
        'word_families': [
            {
                'key': f.key, 'title': f.title, 'description': f.description,
                'source': f.source,
                'confirmed_at': f.confirmed_at.isoformat() if f.confirmed_at else None,
                'members': [
                    {
                        'word': m.word.word if m.word else None,
                        'outside_word': m.outside_word,
                        'pos': m.pos, 'meaning': m.meaning,
                        'relation': m.relation, 'note': m.note,
                        'sort_order': m.sort_order, 'source': m.source,
                        'confirmed_at': m.confirmed_at.isoformat() if m.confirmed_at else None,
                    }
                    for m in f.members.select_related('word').all()
                ],
                'patterns': [
                    {
                        'name': p.name, 'kind': p.kind,
                        'from_pos': p.from_pos, 'to_pos': p.to_pos,
                        'rule': p.rule, 'exception': p.exception,
                        'confidence': p.confidence, 'is_confirmed': p.is_confirmed,
                        'source': p.source,
                    }
                    for p in f.patterns.all()
                ],
                'relations': [
                    {
                        'word': r.word.word if r.word else None,
                        'related_word': r.related_word.word if r.related_word else None,
                        'outside_word': r.outside_word,
                        'rel_type': r.rel_type, 'note': r.note,
                        'confidence': r.confidence, 'is_confirmed': r.is_confirmed,
                        'source': r.source,
                    }
                    for r in f.relations.select_related('word', 'related_word').all()
                ],
            }
            for f in WordFamily.objects.all()
        ],
        'knowledge_feedback': [
            {
                'target_type': fb.target_type,
                'ref': _knowledge_feedback_ref(fb),
                'action': fb.action,
                'created_at': fb.created_at.isoformat(),
            }
            for fb in KnowledgeFeedback.objects.all()
        ],
    }

    return data


def _write_backup_copy(data):
    """在 data/backups 写一份本机副本，返回 (文件名, 绝对路径)。

    文件名带毫秒，避免同一秒内连续备份互相覆盖。
    """
    from django.core.serializers.json import DjangoJSONEncoder
    os.makedirs(settings.BACKUP_DIR, exist_ok=True)
    now = timezone.now()
    filename = f'backup_{now.strftime("%Y%m%d_%H%M%S")}_{now.microsecond // 1000:03d}.json'
    filepath = os.path.join(settings.BACKUP_DIR, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, cls=DjangoJSONEncoder)
    return filename, filepath


# 本机副本目录中最多保留的备份份数
_BACKUP_COPY_KEEP = 10


def _rotate_backup_copies():
    """清理 data/backups 中的本机副本，只保留最新的 _BACKUP_COPY_KEEP 份。

    只处理本目录下的 backup_*.json（文件名自带毫秒级时间戳，字典序即时间序），
    绝不触碰其他文件。
    """
    backup_dir = str(settings.BACKUP_DIR)
    try:
        candidates = sorted(
            (name for name in os.listdir(backup_dir)
             if name.startswith('backup_') and name.endswith('.json')),
            reverse=True,
        )
        for name in candidates[_BACKUP_COPY_KEEP:]:
            try:
                os.remove(os.path.join(backup_dir, name))
            except OSError:
                pass
    except OSError:
        pass


def api_backup(request):
    """浏览器模式：生成备份并直接以附件形式返回（走浏览器自身下载流程）。"""
    from django.core.serializers.json import DjangoJSONEncoder
    settings_obj = UserSettings.get_settings()
    data = _build_backup_payload(settings_obj)
    filename = f'backup_{timezone.now().strftime("%Y%m%d_%H%M%S")}.json'
    response = HttpResponse(
        json.dumps(data, ensure_ascii=False, cls=DjangoJSONEncoder),
        content_type='application/json; charset=utf-8',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def api_backup_prepare(request):
    """桌面端备份第一步：生成备份并写一份本机副本。

    随后前端通过 pywebview 桥接弹出系统「另存为」对话框，由宿主把该副本
    复制到用户选择的位置。
    """
    if request.method != 'POST':
        return JsonResponse({'error': '仅支持 POST 请求'}, status=405)
    settings_obj = UserSettings.get_settings()
    data = _build_backup_payload(settings_obj)
    filename, filepath = _write_backup_copy(data)
    _rotate_backup_copies()
    return JsonResponse({
        'success': True,
        'filename': filename,
        'size': os.path.getsize(filepath),
        'copy_path': filepath,
    })


def _backup_settings(s):
    """备份 UserSettings 全字段（含所有 FK id）"""
    return {
        'font_size': s.font_size, 'dark_mode': s.dark_mode, 'theme': s.theme,
        'pronunciation_on': s.pronunciation_on, 'auto_read': s.auto_read,
        'speech_rate': s.speech_rate, 'voice_type': s.voice_type,
        'daily_new_target': s.daily_new_target,
        'daily_review_target': s.daily_review_target,
        'batch_size': s.batch_size, 'gate_answer_show': s.gate_answer_show,
        'use_ai_meaning_check': s.use_ai_meaning_check,
        'assistant_model_id': s.assistant_model_id,
        'recognize_model_id': s.recognize_model_id,
        'review_model_id': s.review_model_id,
        'quick_memory_model_id': s.quick_memory_model_id,
        'meaning_check_model_id': s.meaning_check_model_id,
        'exam_model_id': s.exam_model_id,
        'cet6_translation_model_id': s.cet6_translation_model_id,
        'pos_learning_model_id': s.pos_learning_model_id,
    }


def _backup_progress(p):
    """备份 StudyProgress 全字段（含 word 字符串用于跨库 id 映射）"""
    return {
        'word_id': p.word_id, 'word': p.word.word if p.word else None,  # word 字符串用于跨库恢复
        'status': p.status, 'mastery_level': p.mastery_level,
        'review_count': p.review_count, 'error_count': p.error_count,
        'last_review': p.last_review.isoformat() if p.last_review else None,
        'next_review': p.next_review.isoformat() if p.next_review else None,
        'is_today_new': p.is_today_new,
        'learned_date': p.learned_date.isoformat() if p.learned_date else None,
        'uncommon_pos': p.uncommon_pos, 'spelling_attempts': p.spelling_attempts,
        'spelling_correct': p.spelling_correct, 'meaning_attempts': p.meaning_attempts,
        'meaning_correct': p.meaning_correct,
        'consecutive_correct': p.consecutive_correct, 'is_excluded': p.is_excluded,
        'manual_level': p.manual_level,
        'risk_override': p.risk_override,
    }


@require_http_methods(['POST'])
def api_restore(request):
    """全量恢复：兼容 v1.0 / v2.0 / v2.1 备份。
    - v2.1 备份自带 units+words 词库本体，恢复前自动补齐缺失单元与词条（只增不改）
    - v2.0 备份自带 word 字符串，可跨不同数据库的 Word.id 正常恢复
    - v1.0 旧备份只有 word_id，若两边词库不一致会跳过引用 Word 的表
    - AIModel / Conversation / WritingPractice 内部 id 做映射
    - 事务级关闭 FK 约束，恢复完恢复
    """
    from django.db import connection, transaction
    try:
        if request.FILES.get('file'):
            content = request.FILES['file'].read().decode('utf-8')
        else:
            content = request.body.decode('utf-8')
        data = json.loads(content)

        skipped = {}  # 跳过计数（{表名: 条数}）
        time_missing = 0  # 无可靠记录时间的背词记录条数（不伪装成恢复当天）

        def _resolve_word_id(row, table):
            """严格按 word 字符串映射当前库的 word_id；查不到返回 None（跳过）。
            不回退使用原 word_id，避免 id 撞车错挂到别的单词上。"""
            w = row.get('word')
            if w and w in exe_word_map:
                return exe_word_map[w]
            skipped[table] = skipped.get(table, 0) + 1
            return None

        def _resolve_fb_target(tt, ref):
            """把知识反馈的自然引用解析为目标库对象 id；解析不到返回 None（跳过并计数）。"""
            if tt == 'word':
                return exe_word_map.get(ref.get('word', ''))
            if tt == 'family':
                key = ref.get('key')
                if not key:
                    return None
                return WordFamily.objects.filter(key=key).values_list('id', flat=True).first()
            family = WordFamily.objects.filter(key=ref.get('family_key') or '').first()
            if family is None:
                return None
            if tt == 'member':
                wordtext = ref.get('word')
                wid = exe_word_map.get(wordtext) if wordtext else None
                qs = family.members.all()
                qs = (qs.filter(word_id=wid) if wid is not None
                      else qs.filter(outside_word=ref.get('outside_word') or ''))
                if ref.get('relation'):
                    qs = qs.filter(relation=ref['relation'])
                if ref.get('pos'):
                    qs = qs.filter(pos=ref['pos'])
                return qs.values_list('id', flat=True).first()
            if tt == 'pattern':
                return (family.patterns
                        .filter(name=ref.get('name') or '', kind=ref.get('kind') or '')
                        .values_list('id', flat=True).first())
            if tt == 'relation':
                wordtext = ref.get('word')
                wid = exe_word_map.get(wordtext) if wordtext else None
                if wid is None:
                    return None
                qs = family.relations.filter(word_id=wid)
                if ref.get('rel_type'):
                    qs = qs.filter(rel_type=ref['rel_type'])
                related = ref.get('related_word')
                if related:
                    qs = qs.filter(related_word_id=exe_word_map.get(related))
                else:
                    qs = qs.filter(outside_word=ref.get('outside_word') or '')
                return qs.values_list('id', flat=True).first()
            return None

        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute('PRAGMA foreign_keys = OFF')
            try:
                # --- 0) 补齐词库：v2.1 备份携带 units+words，只增不改 ---
                units_added = 0
                words_added = 0
                existing_numbers = set(Unit.objects.values_list('number', flat=True))
                for u in data.get('units', []):
                    num = u.get('number')
                    if num is None or num in existing_numbers:
                        continue
                    Unit.objects.create(
                        number=num, name=u.get('name') or f'List {num}',
                        category=u.get('category', 'required'),
                        word_count=u.get('word_count', 0))
                    existing_numbers.add(num)
                    units_added += 1

                existing_words = set(Word.objects.values_list('word', flat=True))
                unit_id_by_number = dict(Unit.objects.values_list('number', 'id'))
                next_unit_number = (max(existing_numbers) if existing_numbers else 0) + 1
                fallback_unit_id = None
                for wd in data.get('words', []):
                    wtext = (wd.get('word') or '').strip()
                    if not wtext or wtext in existing_words:
                        continue  # 目标库已有：绝不覆盖用户编辑过的内容
                    unit_id = unit_id_by_number.get(wd.get('unit_number'))
                    if unit_id is None:
                        # 备份里单元信息缺失：统一挂到兜底单元
                        if fallback_unit_id is None:
                            fu = Unit.objects.create(
                                number=next_unit_number, name='导入词汇',
                                category='advanced', word_count=0)
                            unit_id_by_number[next_unit_number] = fu.id
                            fallback_unit_id = fu.id
                            units_added += 1
                        unit_id = fallback_unit_id
                    Word.objects.create(
                        word=wtext,
                        phonetic_us=wd.get('phonetic_us', '') or '',
                        phonetic_uk=wd.get('phonetic_uk', '') or '',
                        pos=wd.get('pos', '') or '',
                        meanings=wd.get('meanings', '[]') or '[]',
                        meanings_by_pos=wd.get('meanings_by_pos', '{}') or '{}',
                        uncommon_meanings=wd.get('uncommon_meanings', '[]') or '[]',
                        collocations=wd.get('collocations', '[]') or '[]',
                        word_forms=wd.get('word_forms', '{}') or '{}',
                        example_en=wd.get('example_en', '') or '',
                        example_zh=wd.get('example_zh', '') or '',
                        category=wd.get('category', 'advanced'),
                        unit_id=unit_id,
                        list_number=wd.get('list_number', 1) or 1)
                    existing_words.add(wtext)
                    words_added += 1

                # 补齐后重建映射，供后续所有表带 word 的记录使用
                exe_word_map = dict(Word.objects.values_list('word', 'id'))

                # --- 1) AI 模型（必须先恢复，其他表 FK 引用模型 id）---
                id_map = {}
                if data.get('ai_models'):
                    AIModel.objects.all().delete()
                    for m in data['ai_models']:
                        old_id = m.get('id')
                        plain = m.get('api_key', '') or ''
                        m2 = AIModel.objects.create(
                            provider=m.get('provider', 'openai'),
                            model_id=m['model_id'],
                            display_name=m.get('display_name', ''),
                            base_url=m.get('base_url', 'https://api.openai.com/v1'),
                            endpoint=m.get('endpoint', ''),
                            api_key=plain,
                            context=m.get('context', '128K'),
                            vision=m.get('vision', True),
                            enabled=m.get('enabled', True),
                        )
                        if old_id:
                            id_map[old_id] = m2.id

                # --- 2) 用户设置（含模型 FK 映射）---
                if 'settings' in data:
                    s = data['settings']
                    settings_obj = UserSettings.get_settings()
                    settings_obj.font_size = s.get('font_size', 'medium')
                    settings_obj.dark_mode = s.get('dark_mode', False)
                    settings_obj.theme = s.get('theme', 'light')
                    settings_obj.pronunciation_on = s.get('pronunciation_on', True)
                    settings_obj.auto_read = s.get('auto_read', True)
                    settings_obj.speech_rate = s.get('speech_rate', 1.0)
                    settings_obj.voice_type = s.get('voice_type', 'us')
                    settings_obj.daily_new_target = s.get('daily_new_target', 30)
                    settings_obj.daily_review_target = s.get('daily_review_target', 50)
                    settings_obj.batch_size = s.get('batch_size', 10)
                    settings_obj.gate_answer_show = s.get('gate_answer_show', True)
                    settings_obj.use_ai_meaning_check = s.get('use_ai_meaning_check', True)
                    def _map_fk(key):
                        old = s.get(key)
                        return id_map.get(old) if old else None
                    settings_obj.assistant_model_id = _map_fk('assistant_model_id')
                    settings_obj.recognize_model_id = _map_fk('recognize_model_id')
                    settings_obj.review_model_id = _map_fk('review_model_id')
                    settings_obj.quick_memory_model_id = _map_fk('quick_memory_model_id')
                    settings_obj.meaning_check_model_id = _map_fk('meaning_check_model_id')
                    settings_obj.exam_model_id = _map_fk('exam_model_id')
                    settings_obj.cet6_translation_model_id = _map_fk('cet6_translation_model_id')
                    # 备份无此键时明确置空（None），不悄然保留目标库旧值
                    settings_obj.pos_learning_model_id = _map_fk('pos_learning_model_id')
                    settings_obj.save()

                # --- 3) 学习进度（用 word 字符串跨库映射 word_id）---
                if 'progress' in data:
                    StudyProgress.objects.all().delete()
                    for p in data['progress']:
                        new_wid = _resolve_word_id(p, 'progress')
                        if new_wid is None:
                            continue
                        StudyProgress.objects.create(
                            word_id=new_wid,
                            status=p.get('status', 'new'),
                            mastery_level=p.get('mastery_level', 0),
                            review_count=p.get('review_count', 0),
                            error_count=p.get('error_count', 0),
                            last_review=p.get('last_review'),
                            next_review=p.get('next_review'),
                            is_today_new=p.get('is_today_new', False),
                            learned_date=p.get('learned_date'),
                            uncommon_pos=p.get('uncommon_pos', '[]'),
                            spelling_attempts=p.get('spelling_attempts', 0),
                            spelling_correct=p.get('spelling_correct', 0),
                            meaning_attempts=p.get('meaning_attempts', 0),
                            meaning_correct=p.get('meaning_correct', 0),
                            consecutive_correct=p.get('consecutive_correct', 0),
                            is_excluded=p.get('is_excluded', False),
                            manual_level=p.get('manual_level', 3),
                            risk_override=p.get('risk_override'),
                        )

                # --- 4) 背词记录 ---
                if data.get('study_records'):
                    StudyRecord.objects.all().delete()
                    for r in data['study_records']:
                        new_wid = _resolve_word_id(r, 'study_records')
                        if new_wid is None:
                            continue
                        raw_ca = r.get('created_at')
                        parsed = parse_datetime(raw_ca) if raw_ca else None
                        if parsed is None:
                            # 旧备份无可靠记录时间：跳过，不把历史词伪装成恢复当天
                            time_missing += 1
                            continue
                        rec = StudyRecord.objects.create(
                            word_id=new_wid, action=r['action'],
                            source=r.get('source', 'learn'), mode=r.get('mode', ''),
                            mistake_type=r.get('mistake_type', ''),
                            error_detail=r.get('error_detail') or {},
                        )
                        # created_at 为 auto_now_add，先创建再用 update 写回原始时间
                        StudyRecord.objects.filter(pk=rec.pk).update(created_at=parsed)

                # --- 5) 打卡 ---
                if 'checkins' in data:
                    DailyCheckIn.objects.all().delete()
                    for c in data['checkins']:
                        DailyCheckIn.objects.create(
                            date=c['date'],
                            new_words_learned=c.get('new_words_learned', 0),
                            words_reviewed=c.get('words_reviewed', 0),
                            study_duration=c.get('study_duration', 0),
                            correct_rate=c.get('correct_rate', 0),
                            today_correct=c.get('today_correct', 0),
                            today_wrong=c.get('today_wrong', 0),
                            is_checked=c.get('is_checked', False),
                        )

                # --- 6) 收藏 ---
                if 'favorites' in data:
                    Favorite.objects.all().delete()
                    for f in data['favorites']:
                        new_wid = _resolve_word_id(f, 'favorites')
                        if new_wid is None:
                            continue
                        Favorite.objects.create(word_id=new_wid)

                # --- 7) 笔记 ---
                if 'notes' in data:
                    Note.objects.all().delete()
                    for n in data['notes']:
                        new_wid = _resolve_word_id(n, 'notes')
                        if new_wid is None:
                            continue
                        Note.objects.create(
                            word_id=new_wid, content=n.get('content', ''),
                            page_number=n.get('page_number'),
                        )

                # --- 8) 速记 ---
                if data.get('quick_memories'):
                    QuickMemory.objects.all().delete()
                    for qm in data['quick_memories']:
                        new_wid = _resolve_word_id(qm, 'quick_memories')
                        if new_wid is None:
                            continue
                        QuickMemory.objects.create(
                            word_id=new_wid, content=qm.get('content', ''),
                        )

                # --- 8.5) 今日自选（按单词字符串映射，查不到跳过）---
                if data.get('daily_picks'):
                    DailyPick.objects.all().delete()
                    for dp in data['daily_picks']:
                        new_wid = _resolve_word_id(dp, 'daily_picks')
                        if new_wid is None:
                            continue
                        try:
                            pick_date = dp.get('date')
                            if pick_date:
                                pick_date = datetime.strptime(pick_date[:10], '%Y-%m-%d').date()
                            else:
                                pick_date = timezone.localdate()
                            DailyPick.objects.get_or_create(date=pick_date, word_id=new_wid)
                        except (ValueError, TypeError):
                            continue

                # --- 8.6) 选词记录（第十八节，按单词字符串映射，保留顺序）---
                if data.get('pick_records'):
                    PickRecord.objects.all().delete()
                    for pr in data['pick_records']:
                        rec = PickRecord.objects.create(name=pr.get('name', '')[:100])
                        # created_at 为 auto_now_add，先创建再用 update 写回原始时间
                        raw_ca = pr.get('created_at')
                        parsed_ca = parse_datetime(raw_ca) if raw_ca else None
                        if parsed_ca:
                            PickRecord.objects.filter(pk=rec.pk).update(created_at=parsed_ca)
                        members = []
                        for m in pr.get('words', []):
                            new_wid = _resolve_word_id(m, 'pick_records')
                            if new_wid is None:
                                continue
                            members.append(PickRecordWord(
                                record=rec, word_id=new_wid,
                                position=m.get('position', 0)))
                        PickRecordWord.objects.bulk_create(members, ignore_conflicts=True)

                # --- 9) 计划（无 Word 引用）---
                if 'plans' in data:
                    StudyPlan.objects.all().delete()
                    for p in data['plans']:
                        StudyPlan.objects.create(
                            name=p['name'],
                            daily_new_words=p.get('daily_new_words', 30),
                            daily_review_count=p.get('daily_review_count', 50),
                            target_date=p.get('target_date'),
                            unit_range=p.get('unit_range', '[]'),
                            is_active=p.get('is_active', False),
                        )

                # --- 10) 预设 ---
                if data.get('presets'):
                    StudyPreset.objects.all().delete()
                    for p in data['presets']:
                        StudyPreset.objects.create(
                            name=p['name'], preset_type=p['preset_type'],
                            params=p.get('params', {}),
                        )

                # --- 11) AI 会话与消息 ---
                conv_id_map = {}
                if data.get('conversations'):
                    Conversation.objects.all().delete()
                    for c in data['conversations']:
                        old_id = c.get('id')
                        new_c = Conversation.objects.create(title=c.get('title', '新对话'))
                        if old_id:
                            conv_id_map[old_id] = new_c.id
                # 消息：conversation_id 映射 + word_id 跨库映射
                msg_list = data.get('chat_messages')
                if msg_list:
                    ChatMessage.objects.all().delete()
                    for m in msg_list:
                        new_cid = conv_id_map.get(m.get('conversation_id')) if conv_id_map else m.get('conversation_id')
                        new_wid = _resolve_word_id(m, 'chat_messages')
                        ChatMessage.objects.create(
                            role=m['role'], content=m['content'],
                            word_id=new_wid,  # None 也 OK（word_id 可空）
                            conversation_id=new_cid,
                            attachments=m.get('attachments', []),
                        )

                # --- 12) 写作练习与好句 ---
                wp_id_map = {}
                if data.get('writing_practices'):
                    WritingPractice.objects.all().delete()
                    for w in data['writing_practices']:
                        old_id = w.get('id')
                        new_w = WritingPractice.objects.create(
                            question_id=w.get('question_id'),
                            mode=w.get('mode', 'essay'),
                            source=w.get('source', 'ai'),
                            user_input=w.get('user_input', ''),
                            ai_output=w.get('ai_output', ''),
                            score_json=w.get('score_json', '{}'),
                            feedback=w.get('feedback', ''),
                        )
                        if old_id:
                            wp_id_map[old_id] = new_w.id
                    if data.get('writing_phrases'):
                        WritingPhrase.objects.all().delete()
                        for p in data['writing_phrases']:
                            old_pid = p.get('practice_id')
                            new_pid = wp_id_map.get(old_pid) if old_pid else None
                            WritingPhrase.objects.create(
                                practice_id=new_pid,
                                phrase=p['phrase'],
                                meaning=p.get('meaning', ''),
                            )

                # --- 13) 导入记录 ---
                if data.get('import_logs'):
                    ImportLog.objects.all().delete()
                    for lg in data['import_logs']:
                        ImportLog.objects.create(
                            source=lg.get('source', 'text'),
                            unit_number=lg.get('unit_number', 99),
                            unit_name=lg.get('unit_name', ''),
                            imported_count=lg.get('imported_count', 0),
                            skipped_count=lg.get('skipped_count', 0),
                            words_list=lg.get('words_list', '[]'),
                        )

                # --- 14) 学习周报 ---
                if data.get('learning_reports'):
                    LearningReport.objects.all().delete()
                    for lr in data['learning_reports']:
                        LearningReport.objects.create(
                            week_start=lr['week_start'],
                            week_end=lr['week_end'],
                            summary_json=lr.get('summary_json', '{}'),
                            ai_comment=lr.get('ai_comment', ''),
                        )

                # --- 15) 词性学习作答 / 词族与构词法 / 知识反馈 ---
                if data.get('pos_attempts'):
                    PosLearningAttempt.objects.all().delete()
                    for pa in data['pos_attempts']:
                        new_wid = _resolve_word_id(pa, 'pos_attempts')
                        if new_wid is None:
                            continue
                        rec = PosLearningAttempt.objects.create(
                            word_id=new_wid,
                            target_pos=pa.get('target_pos', ''),
                            question_type=pa.get('question_type', 'pos_choice'),
                            question=pa.get('question', {}),
                            user_answer=pa.get('user_answer', ''),
                            result=pa.get('result', ''),
                            judged_by=pa.get('judged_by', ''),
                            feedback=pa.get('feedback', ''),
                        )
                        # created_at 为 auto_now_add，先创建再用 update 写回原始时间
                        raw_ca = pa.get('created_at')
                        parsed_ca = parse_datetime(raw_ca) if raw_ca else None
                        if parsed_ca:
                            PosLearningAttempt.objects.filter(pk=rec.pk).update(created_at=parsed_ca)

                # --- 15b) 待复核判定（第八节）：随备份恢复，缺 word 字符串一律跳过 ---
                if data.get('pending_judgments'):
                    PendingJudgment.objects.all().delete()
                    for pj in data['pending_judgments']:
                        new_wid = _resolve_word_id(pj, 'pending_judgments')
                        if new_wid is None:
                            continue
                        rec = PendingJudgment.objects.create(
                            word_id=new_wid,
                            kind=pj.get('kind', 'meaning'),
                            mode=pj.get('mode', ''),
                            source=pj.get('source', 'learn'),
                            reason_code=pj.get('reason_code', ''),
                            user_answer=pj.get('user_answer', ''),
                            dedupe_key=pj.get('dedupe_key', ''),
                            question=pj.get('question') or {},
                            resolved=pj.get('resolved', False),
                            final_verdict=pj.get('final_verdict', ''),
                            resolved_by=pj.get('resolved_by', ''),
                            resolved_at=(parse_datetime(pj['resolved_at'])
                                         if pj.get('resolved_at') else None),
                            applied=pj.get('applied', False),
                        )
                        raw_ca = pj.get('created_at')
                        parsed_ca = parse_datetime(raw_ca) if raw_ca else None
                        if parsed_ca:
                            PendingJudgment.objects.filter(pk=rec.pk).update(created_at=parsed_ca)

                if data.get('word_families'):
                    # 词族整组恢复（成员/构词法/关系内联在 family 下）
                    WordFamily.objects.all().delete()
                    WordFormationPattern.objects.filter(family__isnull=False).delete()
                    WordRelation.objects.filter(family__isnull=False).delete()
                    for fam in data['word_families']:
                        f = WordFamily.objects.create(
                            key=fam.get('key', ''), title=fam.get('title', ''),
                            description=fam.get('description', ''),
                            source=fam.get('source', 'manual'),
                            confirmed_at=parse_datetime(fam['confirmed_at']) if fam.get('confirmed_at') else None,
                        )
                        for i, m in enumerate(fam.get('members', [])):
                            mw = m.get('word')
                            wid = exe_word_map.get(mw) if mw else None
                            if mw and wid is None:
                                skipped['word_family_members'] = skipped.get('word_family_members', 0) + 1
                            WordFamilyMember.objects.create(
                                family=f, word_id=wid,
                                outside_word='' if wid else (m.get('outside_word') or (mw or '')),
                                pos=m.get('pos', ''), meaning=m.get('meaning', ''),
                                relation=m.get('relation', 'derived'),
                                note=m.get('note', ''), sort_order=m.get('sort_order', i),
                                source=m.get('source', 'manual'),
                                confirmed_at=parse_datetime(m['confirmed_at']) if m.get('confirmed_at') else None,
                            )
                        for p in fam.get('patterns', []):
                            WordFormationPattern.objects.create(
                                family=f, name=p.get('name', ''),
                                kind=p.get('kind', 'suffix'),
                                from_pos=p.get('from_pos', ''), to_pos=p.get('to_pos', ''),
                                rule=p.get('rule', ''), exception=p.get('exception', ''),
                                confidence=p.get('confidence', 1.0),
                                is_confirmed=p.get('is_confirmed', True),
                                source=p.get('source', 'manual'),
                            )
                        for r in fam.get('relations', []):
                            from_w = r.get('word')
                            from_id = exe_word_map.get(from_w) if from_w else None
                            if not from_id:
                                skipped['word_relations'] = skipped.get('word_relations', 0) + 1
                                continue
                            rel_w = r.get('related_word')
                            rel_id = exe_word_map.get(rel_w) if rel_w else None
                            if rel_w and rel_id is None:
                                skipped['word_relations'] = skipped.get('word_relations', 0) + 1
                            WordRelation.objects.create(
                                family=f, word_id=from_id, related_word_id=rel_id,
                                outside_word='' if rel_id else (r.get('outside_word') or (rel_w or '')),
                                rel_type=r.get('rel_type', 'confusing'),
                                note=r.get('note', ''),
                                confidence=r.get('confidence', 1.0),
                                is_confirmed=r.get('is_confirmed', True),
                                source=r.get('source', 'manual'),
                            )

                if data.get('knowledge_feedback'):
                    KnowledgeFeedback.objects.all().delete()
                    for fb in data['knowledge_feedback']:
                        tt = fb.get('target_type')
                        ref = fb.get('ref') or {}
                        # 按稳定自然引用解析（member/pattern/relation 在词族恢复后再解析）
                        tid = _resolve_fb_target(tt, ref)
                        if tid is None:
                            skipped['knowledge_feedback'] = skipped.get('knowledge_feedback', 0) + 1
                            continue
                        KnowledgeFeedback.objects.create(
                            target_type=tt, target_id=tid, action=fb.get('action', 'focus'))
            finally:
                with connection.cursor() as cur2:
                    cur2.execute('PRAGMA foreign_keys = ON')

        msg = '恢复完成'
        if words_added or units_added:
            msg += f'（已随备份补齐 {units_added} 个单元、{words_added} 个词条）'
        if time_missing:
            msg += f'（另有 {time_missing} 条背词记录因缺少时间被跳过，未计入今日已背）'
        if skipped:
            total_skipped = sum(skipped.values())
            msg += f'（跳过 {total_skipped} 条词库中不存在的单词数据：'
            msg += ', '.join(f'{k} {v}条' for k, v in skipped.items()) + '）'
            if not data.get('words'):
                msg += ('。该备份由旧版本生成，不包含词库本体，新增的单元/单词无法随备份恢复；'
                        '请在新版程序中重新备份，或先在本机导入对应词库后再恢复')
        return JsonResponse({'success': True, 'message': msg})
    except Exception as e:
        import traceback
        return JsonResponse({'error': str(e), 'trace': traceback.format_exc()[:2000]}, status=400)


@require_http_methods(['GET', 'POST'])
def api_settings(request):
    if request.method == 'GET':
        settings_obj = UserSettings.get_settings()
        return JsonResponse({
            'success': True,
            'assistant_model_id': settings_obj.assistant_model_id,
            'recognize_model_id': settings_obj.recognize_model_id,
            'review_model_id': settings_obj.review_model_id,
            'quick_memory_model_id': settings_obj.quick_memory_model_id,
            'meaning_check_model_id': settings_obj.meaning_check_model_id,
            'exam_model_id': settings_obj.exam_model_id,
            'cet6_translation_model_id': settings_obj.cet6_translation_model_id,
            'pos_learning_model_id': settings_obj.pos_learning_model_id,
            'use_ai_meaning_check': settings_obj.use_ai_meaning_check,
        })
    try:
        data = json.loads(request.body)
        settings_obj = UserSettings.get_settings()
        for k, v in data.items():
            if not hasattr(settings_obj, k):
                continue
            field = settings_obj._meta.get_field(k)
            if field.is_relation and field.many_to_one:
                if v is None or v == '':
                    setattr(settings_obj, k, None)
                else:
                    try:
                        setattr(settings_obj, k, field.related_model.objects.get(pk=int(v)))
                    except (field.related_model.DoesNotExist, ValueError, TypeError):
                        return JsonResponse({'error': '指定的模型不存在'}, status=400)
            else:
                setattr(settings_obj, k, v)
        settings_obj.save()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@require_http_methods(['POST'])
def api_word_example(request, word_id):
    """例句增删改查（仅背诵页使用）：POST {action: 'save'|'delete', en, zh}"""
    word = get_object_or_404(Word, id=word_id)
    try:
        data = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, TypeError):
        data = {}
    if data.get('action') == 'delete':
        word.example_en = ''
        word.example_zh = ''
    else:
        en = (data.get('en') or '').strip()
        zh = (data.get('zh') or '').strip()
        if not en:
            return JsonResponse({'success': False, 'error': '英文例句不能为空'})
        word.example_en = en
        word.example_zh = zh
    word.save(update_fields=['example_en', 'example_zh'])
    return JsonResponse({'success': True, 'example_en': word.example_en, 'example_zh': word.example_zh})


@require_http_methods(['GET', 'POST', 'DELETE'])
def api_note(request, word_id):
    word = get_object_or_404(Word, id=word_id)
    if request.method == 'GET':
        note = Note.objects.filter(word=word).first()
        return JsonResponse({
            'success': True,
            'content': note.content if note else '',
            'page_number': note.page_number if note else None,
            'updated_at': timezone.localtime(note.updated_at).strftime('%Y-%m-%d %H:%M') if note and note.updated_at else None,
        })
    if request.method == 'DELETE':
        Note.objects.filter(word=word).delete()
        return JsonResponse({'success': True, 'deleted': True})
    try:
        data = json.loads(request.body)
        note, created = Note.objects.get_or_create(word=word)
        note.content = data.get('content', '')
        note.page_number = data.get('page_number')
        if note.page_number == '':
            note.page_number = None
        note.save()
        return JsonResponse({'success': True, 'note_id': note.id})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@require_http_methods(['GET', 'POST', 'DELETE'])
def api_quick_memory(request, word_id):
    """速记：增删改查（GET 查询 / POST 保存编辑 / DELETE 删除），内容存数据库"""
    word = get_object_or_404(Word, id=word_id)
    if request.method == 'GET':
        qm = QuickMemory.objects.filter(word=word).first()
        return JsonResponse({
            'success': True,
            'content': qm.content if qm else '',
            'updated_at': timezone.localtime(qm.updated_at).strftime('%Y-%m-%d %H:%M') if qm and qm.updated_at else None,
        })
    if request.method == 'DELETE':
        QuickMemory.objects.filter(word=word).delete()
        return JsonResponse({'success': True, 'deleted': True})
    try:
        data = json.loads(request.body)
        qm, _ = QuickMemory.objects.get_or_create(word=word)
        qm.content = data.get('content', '')
        qm.save()
        return JsonResponse({'success': True, 'quick_memory_id': qm.id})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@require_http_methods(['POST'])
def api_quick_memory_generate(request, word_id):
    """AI 生成速记：数据库已有缓存则直接返回（不再调用 AI）；force=true 时强制重新生成"""
    word = get_object_or_404(Word, id=word_id)
    try:
        data = json.loads(request.body)
    except Exception:
        data = {}

    existing = QuickMemory.objects.filter(word=word).first()
    if existing and existing.content and not data.get('force'):
        return JsonResponse({'success': True, 'content': existing.content, 'cached': True})

    settings_obj = UserSettings.get_settings()
    if settings_obj.quick_memory_model and not data.get('model_id'):
        data['model_id'] = settings_obj.quick_memory_model_id

    try:
        cfg = resolve_ai_model(data)
    except ValueError as e:
        return JsonResponse({'error': str(e)}, status=400)
    api_key = cfg['api_key']
    base_url = cfg['base_url']
    endpoint = cfg['endpoint']
    model = cfg['model_id']

    word_text = word.word
    pos = word.pos or ''
    meanings = '；'.join(word.get_meanings()[:3]) or ''
    phonetic_us = word.phonetic_us or ''
    phonetic_uk = word.phonetic_uk or ''

    prompt = quick_memory_prompt(word_text, pos, meanings, phonetic_us, phonetic_uk)

    payload = {
        'model': model,
        'temperature': 0.4,
        # 六段式速记（拆解/串义/联想/同族词/例句/总记），500 会被截断
        'max_tokens': 1200,
        'messages': [{'role': 'user', 'content': prompt}],
    }

    req = urllib.request.Request(
        resolve_ai_endpoint(base_url, endpoint),
        data=json.dumps(payload).encode('utf-8'),
        headers=build_ai_headers(api_key),
        method='POST',
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'ignore')
        return JsonResponse({'error': 'AI 接口错误 (HTTP %s): %s' % (e.code, body[:400])}, status=502)
    except urllib.error.URLError as e:
        return JsonResponse({'error': '无法连接 %s: %s' % (resolve_ai_endpoint(base_url, endpoint), e.reason)}, status=502)

    # 畸形 2xx（HTML / 非法 JSON / choices 异常 / 空内容）统一按「未返回有效内容」处理，不裸奔 500
    try:
        content = _ai_extract_content(_ai_parse_json_body(raw))
    except _AIRequestError:
        return JsonResponse({'error': 'AI 未返回有效内容'}, status=502)

    qm, _ = QuickMemory.objects.get_or_create(word=word)
    qm.content = content
    qm.save()
    return JsonResponse({'success': True, 'content': content, 'cached': False})


@require_http_methods(['GET', 'POST', 'DELETE'])
def api_assistant(request):
    """小助手：GET 获取对话历史 / POST 提问（AI 回答，历史互通） / DELETE 清空历史"""
    if request.method == 'GET':
        msgs = ChatMessage.objects.all().order_by('id')
        return JsonResponse({
            'success': True,
            'messages': [
                {
                    'id': m.id,
                    'role': m.role,
                    'content': m.content,
                    'word_id': m.word_id,
                    'created_at': timezone.localtime(m.created_at).strftime('%Y-%m-%d %H:%M'),
                }
                for m in msgs
            ],
        })

    if request.method == 'DELETE':
        ChatMessage.objects.all().delete()
        return JsonResponse({'success': True})

    # ---- POST：问答 ----
    try:
        data = json.loads(request.body)
    except Exception:
        data = {}
    user_text = (data.get('message') or '').strip()
    if not user_text:
        return JsonResponse({'error': '消息不能为空'}, status=400)

    word = None
    word_id = data.get('word_id')
    if word_id:
        try:
            word = Word.objects.get(id=int(word_id))
        except (Word.DoesNotExist, ValueError, TypeError):
            word = None

    # 校验 AI 配置（未配置时直接提示，不写入历史）
    # 优先使用设置页指定的小助手模型；未指定时自动使用第一个启用的模型
    settings_obj = UserSettings.get_settings()
    if settings_obj.assistant_model:
        data = dict(data)
        data['model_id'] = settings_obj.assistant_model_id
    try:
        cfg = resolve_ai_model(data)
    except ValueError as e:
        return JsonResponse({'error': str(e)}, status=400)

    # 构造上下文：系统提示 + 当前单词信息 + 最近对话历史
    system_prompt = ASSISTANT_SYSTEM_PROMPT
    messages = [{'role': 'system', 'content': system_prompt}]

    if word:
        word_ctx = assistant_word_context(word)
        messages.append({'role': 'system', 'content': word_ctx})

    # 最近 20 条历史作为对话上下文（记忆互通）
    for m in ChatMessage.objects.all().order_by('-id')[:20][::-1]:
        messages.append({'role': m.role, 'content': m.content[:2000]})

    messages.append({'role': 'user', 'content': user_text})

    payload = {
        'model': cfg['model_id'],
        'temperature': 0.7,
        'max_tokens': 1000,
        'messages': messages,
    }

    req = urllib.request.Request(
        resolve_ai_endpoint(cfg['base_url'], cfg['endpoint']),
        data=json.dumps(payload).encode('utf-8'),
        headers=build_ai_headers(cfg['api_key']),
        method='POST',
    )

    # 先保存用户消息；AI 调用失败时回滚，保证历史里只有成功的问答
    user_msg = ChatMessage.objects.create(
        role='user', content=user_text, word_id=word.id if word else None)

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'ignore')
        user_msg.delete()
        return JsonResponse({'error': 'AI 接口错误 (HTTP %s): %s' % (e.code, body[:400])}, status=502)
    except urllib.error.URLError as e:
        user_msg.delete()
        return JsonResponse({'error': '无法连接 %s: %s' % (resolve_ai_endpoint(cfg['base_url'], cfg['endpoint']), e.reason)}, status=502)

    try:
        content = _ai_extract_content(_ai_parse_json_body(raw))
    except _AIRequestError:
        user_msg.delete()
        return JsonResponse({'error': 'AI 未返回有效内容'}, status=502)

    ai_msg = ChatMessage.objects.create(
        role='assistant', content=content, word_id=word.id if word else None)

    def _ser(m):
        return {
            'id': m.id,
            'role': m.role,
            'content': m.content,
            'word_id': m.word_id,
            'created_at': timezone.localtime(m.created_at).strftime('%Y-%m-%d %H:%M'),
        }

    return JsonResponse({'success': True, 'user_message': _ser(user_msg), 'assistant_message': _ser(ai_msg)})


def ai_chat_page(request):
    """AI 智能助手独立页面：全屏对话 + 文件上传 + 模型选择"""
    models = AIModel.objects.filter(enabled=True).order_by('id')
    settings_obj = UserSettings.get_settings()
    current_model = settings_obj.assistant_model or models.first()
    return render(request, 'ai_chat.html', {
        'models': models,
        'current_model': current_model,
    })


def _ser_chat_msg(m):
    """ChatMessage → 前端 JSON"""
    return {
        'id': m.id,
        'role': m.role,
        'content': m.content,
        'attachments': m.attachments or [],
        'created_at': timezone.localtime(m.created_at).strftime('%Y-%m-%d %H:%M'),
    }


def _ser_conversation(c):
    """Conversation → 前端 JSON"""
    return {
        'id': c.id,
        'title': c.title,
        'message_count': ChatMessage.objects.filter(conversation_id=c.id).count(),
        'updated_at': timezone.localtime(c.updated_at).strftime('%m-%d %H:%M'),
    }


def api_ai_chat(request):
    """AI 智能助手 API（多会话）：
    GET    /api/ai/chat/                     → 启用模型列表 + 会话列表 + 最近会话的消息
    GET    /api/ai/chat/?conversation_id=X   → 指定会话的消息
    POST   /api/ai/chat/                     → 发送消息（conversation_id 缺省时自动新建会话）
    DELETE /api/ai/chat/?conversation_id=X   → 清空指定会话的记录；缺省清空全部
    """
    if request.method == 'GET':
        models = AIModel.objects.filter(enabled=True).order_by('id')
        conversations = Conversation.objects.all()

        conv_id = request.GET.get('conversation_id')
        if conv_id:
            try:
                current_conv = Conversation.objects.get(id=int(conv_id))
            except (Conversation.DoesNotExist, ValueError, TypeError):
                current_conv = conversations.first()
        else:
            current_conv = conversations.first()

        if current_conv:
            msgs = ChatMessage.objects.filter(conversation_id=current_conv.id).order_by('id')
        else:
            msgs = ChatMessage.objects.none()

        return JsonResponse({
            'success': True,
            'models': [serialize_ai_model(m) for m in models],
            'conversations': [_ser_conversation(c) for c in conversations],
            'current_conversation_id': current_conv.id if current_conv else None,
            'messages': [_ser_chat_msg(m) for m in msgs],
        })

    if request.method == 'DELETE':
        conv_id = request.GET.get('conversation_id')
        if conv_id:
            try:
                conv = Conversation.objects.get(id=int(conv_id))
            except (Conversation.DoesNotExist, ValueError, TypeError):
                return JsonResponse({'error': '会话不存在'}, status=404)
            ChatMessage.objects.filter(conversation_id=conv.id).delete()
        else:
            ChatMessage.objects.all().delete()
        return JsonResponse({'success': True})

    # ---- POST：对话 ----
    try:
        data = json.loads(request.body)
    except Exception:
        data = {}
    user_text = (data.get('message') or '').strip()
    if not user_text and not data.get('files'):
        return JsonResponse({'error': '消息不能为空'}, status=400)

    # 会话：指定 ID 则复用，否则新建
    conv_id = data.get('conversation_id')
    conv = None
    if conv_id:
        try:
            conv = Conversation.objects.get(id=int(conv_id))
        except (Conversation.DoesNotExist, ValueError, TypeError):
            return JsonResponse({'error': '会话不存在，请刷新重试'}, status=404)
    if conv is None:
        conv = Conversation.objects.create(
            title=(user_text or '（文件）')[:30] or '新对话')

    # 模型选择：前端指定 model_id（数据库记录）> 设置页小助手模型 > 第一个启用模型
    settings_obj = UserSettings.get_settings()
    chosen_id = data.get('model_id')
    if not chosen_id and settings_obj.assistant_model_id:
        chosen_id = settings_obj.assistant_model_id
    try:
        cfg = resolve_ai_model({'model_id': chosen_id} if chosen_id else {})
    except ValueError as e:
        conv.delete()  # 新建但模型不可用 → 回滚会话
        return JsonResponse({'error': str(e)}, status=400)

    # 解析文件附件：图片 → data URI（视觉模型）；文本/其它 → 截断文本
    files = data.get('files') or []
    content_parts = []
    file_notes = []
    for f in files[:8]:  # 最多 8 个附件
        name = (f.get('name') or '附件').strip()[:120]
        mime = (f.get('mime') or '').lower()
        raw = f.get('content') or ''
        if mime.startswith('image/') and raw:
            content_parts.append({
                'type': 'image_url',
                'image_url': {'url': 'data:%s;base64,%s' % (mime, raw)},
            })
            file_notes.append({'name': name, 'type': 'image', 'mime': mime})
        else:
            try:
                text = raw if isinstance(raw, str) else base64.b64decode(raw).decode('utf-8', 'ignore')
            except Exception:
                text = ''
            text = text[:50000]
            content_parts.append({'type': 'text', 'text': '【文件：%s】\n%s' % (name, text)})
            file_notes.append({'name': name, 'type': 'text'})

    if user_text:
        content_parts.append({'type': 'text', 'text': user_text})
    if not content_parts:
        return JsonResponse({'error': '无法解析附件内容'}, status=400)

    # 构造消息链：系统提示 + 本会话最近 20 条历史 + 当前消息（含附件）
    messages = [{'role': 'system', 'content': ASSISTANT_SYSTEM_PROMPT}]
    for m in ChatMessage.objects.filter(conversation_id=conv.id).order_by('-id')[:20][::-1]:
        messages.append({'role': m.role, 'content': m.content[:2000]})

    user_msg = ChatMessage.objects.create(
        role='user',
        content=user_text or '（已发送 %d 个文件）' % len(file_notes),
        conversation_id=conv.id,
        attachments=file_notes,
    )

    payload = {
        'model': cfg['model_id'],
        'temperature': 0.7,
        'max_tokens': 2000,
        'messages': messages + [{'role': 'user', 'content': content_parts}],
    }

    req = urllib.request.Request(
        resolve_ai_endpoint(cfg['base_url'], cfg['endpoint']),
        data=json.dumps(payload).encode('utf-8'),
        headers=build_ai_headers(cfg['api_key']),
        method='POST',
    )

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'ignore')
        user_msg.delete()
        if not ChatMessage.objects.filter(conversation_id=conv.id).exists():
            conv.delete()  # 新建会话无消息 → 回滚
        return JsonResponse({'error': 'AI 接口错误 (HTTP %s): %s' % (e.code, body[:400])}, status=502)
    except urllib.error.URLError as e:
        user_msg.delete()
        if not ChatMessage.objects.filter(conversation_id=conv.id).exists():
            conv.delete()
        return JsonResponse({'error': '无法连接 AI 服务: %s' % e.reason}, status=502)

    try:
        content = _ai_extract_content(_ai_parse_json_body(raw))
    except _AIRequestError:
        user_msg.delete()
        if not ChatMessage.objects.filter(conversation_id=conv.id).exists():
            conv.delete()
        return JsonResponse({'error': 'AI 未返回有效内容'}, status=502)

    ai_msg = ChatMessage.objects.create(
        role='assistant', content=content, conversation_id=conv.id)

    return JsonResponse({
        'success': True,
        'conversation': _ser_conversation(conv),
        'user_message': _ser_chat_msg(user_msg),
        'assistant_message': _ser_chat_msg(ai_msg),
    })


@require_http_methods(['POST'])
def api_ai_chat_new(request):
    """新建 AI 会话"""
    conv = Conversation.objects.create(title='新对话')
    return JsonResponse({'success': True, 'conversation': _ser_conversation(conv)})


@require_http_methods(['DELETE'])
def api_ai_chat_delete(request, conv_id):
    """删除 AI 会话（连同其消息记录）"""
    try:
        conv = Conversation.objects.get(id=conv_id)
    except Conversation.DoesNotExist:
        return JsonResponse({'error': '会话不存在'}, status=404)
    ChatMessage.objects.filter(conversation_id=conv.id).delete()
    conv.delete()
    return JsonResponse({'success': True})


@require_http_methods(['GET', 'POST'])
def api_assistant_model(request):
    """小助手模型配置：GET 查询当前指定 / POST 设置（null = 自动使用第一个启用模型）"""
    settings_obj = UserSettings.get_settings()

    if request.method == 'GET':
        m = settings_obj.assistant_model
        return JsonResponse({
            'assistant_model_id': m.id if m else None,
            'assistant_model_name': (m.display_name or m.model_id) if m else None,
        })

    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': '请求格式错误'}, status=400)

    mid = data.get('model_id')
    if mid:
        try:
            m = AIModel.objects.get(id=int(mid))
        except (AIModel.DoesNotExist, ValueError, TypeError):
            return JsonResponse({'error': '模型不存在'}, status=404)
        if not m.enabled:
            return JsonResponse({'error': '该模型已被禁用，请先启用'}, status=400)
        settings_obj.assistant_model = m
    else:
        settings_obj.assistant_model = None
    settings_obj.save()

    cur = settings_obj.assistant_model
    return JsonResponse({
        'success': True,
        'assistant_model_id': cur.id if cur else None,
        'assistant_model_name': (cur.display_name or cur.model_id) if cur else None,
    })


@require_http_methods(['POST', 'DELETE'])
def api_favorite(request, word_id):
    word = get_object_or_404(Word, id=word_id)
    if request.method == 'DELETE':
        Favorite.objects.filter(word=word).delete()
        return JsonResponse({'success': True, 'is_favorite': False})
    fav, created = Favorite.objects.get_or_create(word=word)
    if not created:
        fav.delete()
        return JsonResponse({'success': True, 'is_favorite': False})
    return JsonResponse({'success': True, 'is_favorite': True})


def api_learn_words(request):
    """返回按 scope+units 筛选的单词列表（与 learn_session 逻辑一致）"""
    scope = request.GET.get('scope', 'unknown')
    unit_ids = [u for u in request.GET.getlist('units') if str(u).strip().isdigit()]
    mode = request.GET.get('mode', 'sequential')

    query = Word.objects.all()
    if unit_ids:
        query = query.filter(unit__number__in=unit_ids)

    today = timezone.localdate()
    if scope == 'daily':
        # 今日自选：以当天清单为准（选了就背，含已掌握/永不忘记）
        pick_ids = list(
            DailyPick.objects.filter(date=today).order_by('id').values_list('word_id', flat=True)
        )
        words = list(Word.objects.filter(id__in=pick_ids))
        if mode != 'random':
            order = {wid: i for i, wid in enumerate(pick_ids)}
            words.sort(key=lambda w: order.get(w.id, 0))
        else:
            random.shuffle(words)
    elif scope == 'favorites':
        # 收藏词汇：以收藏表为准（收藏了就背，含已掌握/永不忘记）
        fav_ids = list(
            Favorite.objects.order_by('-created_at').values_list('word_id', flat=True)
        )
        words = list(Word.objects.filter(id__in=fav_ids))
        if mode == 'random':
            random.shuffle(words)
        else:
            order = {wid: i for i, wid in enumerate(fav_ids)}
            words.sort(key=lambda w: order.get(w.id, 0))
    else:
        # 永不忘记的词不再出现
        query = query.exclude(progress__is_excluded=True)

        if scope == 'mastered':
            query = query.filter(progress__status='mastered')
        else:
            query = query.exclude(progress__status='mastered')

        words = list(query)

        if mode == 'random':
            random.shuffle(words)
        else:
            words = sorted(words, key=lambda w: (w.unit.number, w.list_number))

    # 批量预取进度，避免 N+1
    word_ids = [w.id for w in words]
    progress_map = {
        p.word_id: p for p in StudyProgress.objects.filter(word_id__in=word_ids)
    }
    favorite_ids = set(
        Favorite.objects.filter(word_id__in=word_ids).values_list('word_id', flat=True)
    )

    word_data = []
    for w in words:
        p = progress_map.get(w.id)
        word_data.append({
            'id': w.id,
            'word': w.word,
            'phonetic_us': w.phonetic_us,
            'pos': w.pos,
            'meanings': w.get_meanings(),
            'meanings_by_pos': w.get_meanings_by_pos(),
            'example_en': w.example_en,
            'example_zh': w.example_zh,
            'unit_number': w.unit.number if w.unit else None,
            'uncommon_pos': parse_uncommon_pos(p.uncommon_pos if p else None),
            'is_favorite': w.id in favorite_ids,
        })

    return JsonResponse({'words': word_data, 'total': len(word_data)})


def api_exam_words(request):
    """生成模拟考试题目（4选1选择题，不返回答案给前端）"""
    scope = request.GET.get('scope', 'all')
    unit_ids = [u for u in request.GET.getlist('units') if str(u).strip().isdigit()]
    category = request.GET.get('category', '')
    direction = request.GET.get('direction', 'en2zh')
    try:
        count = int(request.GET.get('count', 20))
    except (TypeError, ValueError):
        count = 20

    query = Word.objects.all()
    if unit_ids:
        query = query.filter(unit__number__in=unit_ids)
    if category:
        query = query.filter(category=category)

    if scope == 'unknown':
        query = query.exclude(progress__status='mastered')
    elif scope == 'mastered':
        query = query.filter(progress__status='mastered')
    elif scope == 'favorites':
        query = query.filter(favorite__isnull=False)
    elif scope == 'errors':
        query = query.filter(progress__error_count__gt=0)
    # 'all'：全部单词

    words = list(query)
    random.shuffle(words)
    if count > 0 and len(words) > count:
        words = words[:count]

    # 干扰项候选池
    all_words = list(Word.objects.all())
    en_pool = [w.word for w in all_words]
    zh_pool = []
    for w in all_words:
        for m in w.get_meanings():
            zh_pool.append(m)

    questions = []
    for w in words:
        meanings = w.get_meanings()
        if direction == 'spelling':
            # 拼写默写：看中文写英文（本地判定）
            questions.append({
                'id': w.id,
                'word': w.word,
                'phonetic_us': w.phonetic_us,
                'pos': w.pos,
                'prompt': '；'.join(meanings) if meanings else w.word,
                'options': [],
                'correct_index': None,
                'type': 'spelling',
            })
            continue
        if direction == 'meaning':
            # 释义默写：看英文写中文（AI 判定）
            questions.append({
                'id': w.id,
                'word': w.word,
                'phonetic_us': w.phonetic_us,
                'pos': w.pos,
                'prompt': w.word,
                'options': [],
                'correct_index': None,
                'type': 'meaning',
            })
            continue
        if direction == 'en2zh':
            if not meanings:
                continue
            # 正确选项只取单个释义，避免"一长串=正确答案"被看穿
            correct = meanings[0]
            candidates = [m for m in zh_pool if m != correct]
            distractors = random.sample(candidates, 3) if len(candidates) >= 3 else candidates
            options = list(distractors) + [correct]
        else:
            correct = w.word
            candidates = [x for x in en_pool if x != correct]
            distractors = random.sample(candidates, 3) if len(candidates) >= 3 else candidates
            options = list(distractors) + [correct]
        random.shuffle(options)
        correct_index = options.index(correct)
        questions.append({
            'id': w.id,
            'word': w.word,
            'phonetic_us': w.phonetic_us,
            'pos': w.pos,
            'prompt': w.word if direction == 'en2zh'
                      else ('；'.join(meanings) if meanings else w.word),
            'options': options,
            'correct_index': correct_index,
            'type': 'choice',
        })

    # 存入 session 供交卷时评分
    request.session['exam_direction'] = direction
    request.session['exam_questions'] = [
        {'id': q['id'], 'options': q['options'], 'correct_index': q['correct_index'], 'type': q.get('type', 'choice')}
        for q in questions
    ]
    # 新开考试：清掉上一场的逐题判定缓存
    request.session['exam_judgments'] = {}

    # 返回前移除答案字段
    for q in questions:
        q.pop('correct_index', None)

    return JsonResponse({'questions': questions, 'direction': direction, 'total': len(questions)})


@require_http_methods(['POST'])
def api_exam_judge_meaning(request):
    """模拟考试中逐题判定释义默写：只判定、不写库。
    结果缓存到 session，学生改答案后重判会覆盖旧结果——以最后一次作答为准。"""
    try:
        data = json.loads(request.body)
        word_id = data.get('word_id')
        answer = (data.get('answer') or '').strip()
        stored = request.session.get('exam_questions', [])
        if not stored:
            return JsonResponse({'error': '考试已过期，请重新开始'}, status=400)
        sq = next((q for q in stored
                   if q['id'] == word_id and q.get('type') == 'meaning'), None)
        if not sq:
            return JsonResponse({'error': '该题不在本场考试中'}, status=400)
        word = Word.objects.filter(id=word_id).first()
        if not word:
            return JsonResponse({'error': '单词不存在'}, status=404)

        policy = _judge_meaning_core(word, answer, data)
        cache = request.session.get('exam_judgments') or {}
        cache[str(word_id)] = {
            'answer': answer, 'verdict': policy['verdict'],
            'comment': policy['comment'], 'source': policy['source'],
            'reason_code': policy['reason_code'], 'is_final': policy['is_final'],
        }
        request.session['exam_judgments'] = cache
        return JsonResponse({
            'success': True, 'word_id': word_id, 'answer': answer,
            'verdict': policy['verdict'], 'comment': policy['comment'],
            'source': policy['source'], 'reason_code': policy['reason_code'],
            'is_final': policy['is_final'],
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@require_http_methods(['GET'])
def api_exam_judgments(request):
    """回填本场考试已缓存的逐题判定（刷新页面后恢复判定状态）"""
    return JsonResponse({'judgments': request.session.get('exam_judgments') or {}})


@require_http_methods(['POST'])
def api_exam_submit(request):
    """交卷评分：更新会/不会状态、记录会话、更新统计"""
    try:
        data = json.loads(request.body)
        answers = data.get('answers', [])
        try:
            duration = int(data.get('duration', 0) or 0)
        except (TypeError, ValueError):
            duration = 0
        duration = max(0, min(duration, 7200))
        stored = request.session.get('exam_questions', [])

        if not stored:
            return JsonResponse({'error': '考试已过期，请重新开始'}, status=400)

        answer_map = {a['word_id']: a for a in answers}
        cached_judgments = request.session.get('exam_judgments') or {}
        exam_direction = request.session.get('exam_direction', 'choice')

        # ===== 阶段一：全部题先在内存里判定完，此阶段不写库 =====
        # 释义题优先使用答题过程中逐题判定的缓存（答案必须与缓存一致，改了答案则重判），
        # 未缓存/已变更的残留释义题收集起来走批量判定（10 题/组、统一标准）。
        word_objs = {w.id: w for w in Word.objects.filter(id__in=[sq['id'] for sq in stored])}
        residual_pairs = []          # (word, answer) 需批量判定
        graded = []                  # [{sq, ans, word, is_correct, comment, source, pending}]
        for sq in stored:
            qid = sq['id']
            qtype = sq.get('type', 'choice')
            ans = answer_map.get(qid) or {}
            word = word_objs.get(qid)
            is_correct = False
            is_pending = False
            reason_code = ''
            comment = ''
            source = 'local'

            if qtype == 'spelling':
                text = (ans.get('answer') or '').strip()
                answered = bool(text)
                is_correct = bool(word) and answered and _normalize_spelling(text) == _normalize_spelling(word.word)
            elif qtype == 'meaning':
                text = (ans.get('answer') or '').strip()
                answered = bool(text)
                cj = cached_judgments.get(str(qid))
                if word and cj and (cj.get('answer') or '') == text:
                    # 缓存命中：以最后一次保存并判定过的答案为准
                    if cj.get('verdict') == 'pending_review':
                        # 答题过程中已判定为待复核：交卷时保持待复核，不重复出网
                        is_pending = True
                        source = 'pending'
                        reason_code = cj.get('reason_code') or ''
                        comment = cj.get('comment') or ''
                    else:
                        is_correct = cj.get('verdict') == 'correct'
                        comment = cj.get('comment') or ''
                        source = cj.get('source') or 'ai'
                elif word and answered:
                    # 已作答但无缓存/答案已变更 → 批量判定；未作答的题不浪费 AI 调用
                    residual_pairs.append((word, text))
                elif word:
                    source = 'local-blank'
                    comment = '未作答'
            else:
                selected = ans.get('selected')
                answered = selected is not None
                is_correct = answered and (selected == sq['correct_index'])

            graded.append({'sq': sq, 'ans': ans, 'word': word,
                           'is_correct': is_correct, 'answered': answered,
                           'comment': comment, 'pending': is_pending,
                           'reason_code': reason_code,
                           'source': source, 'need_batch': qtype == 'meaning' and answered and not (
                               word and cached_judgments.get(str(qid))
                               and (cached_judgments[str(qid)].get('answer') or '') == (ans.get('answer') or '').strip())})

        # 残留释义题一次批量判定（同组统一标准；整组失败/冲突一律落待复核）
        batch_results = _judge_meaning_batch(residual_pairs, data) if residual_pairs else {}
        for g in graded:
            if g['need_batch'] and g['word']:
                policy = batch_results.get(g['word'].id) or _policy_local('wrong', '未作答')
                if policy['verdict'] == 'pending_review':
                    g['pending'] = True
                    g['is_correct'] = False
                    g['reason_code'] = policy['reason_code']
                else:
                    g['is_correct'] = policy['verdict'] == 'correct'
                g['comment'] = policy['comment']
                g['source'] = policy['source']
            g.pop('need_batch', None)

        # ===== 阶段二：判定全部完成后，统一写库（中途失败则零写入，可安全重新交卷）=====
        today = timezone.localdate()
        now = timezone.now()
        checkin, _ = DailyCheckIn.objects.get_or_create(date=today)

        results = []
        correct_count = 0
        pending_count = 0
        source_stats = {'ai': 0, 'local_exact': 0, 'local_blank': 0, 'pending': 0}
        for g in graded:
            sq, ans, word = g['sq'], g['ans'], g['word']
            qid, qtype = sq['id'], sq.get('type', 'choice')
            is_correct = g['is_correct']
            answered = g['answered']
            if qtype == 'meaning':
                if g['pending']:
                    source_stats['pending'] += 1
                elif g['source'] == 'ai':
                    source_stats['ai'] += 1
                elif g['source'] == 'local-blank':
                    source_stats['local_blank'] += 1
                else:
                    source_stats['local_exact'] += 1

            if word and g['pending']:
                # 待复核：保存题目与答案供后续补判，绝不写 StudyRecord/进度/今日对错
                pending = _get_or_create_pending_judgment(
                    word, 'exam_meaning', ans.get('answer') or '',
                    question={'type': qtype, 'word_id': qid,
                              'meanings': word.get_meanings(),
                              'correct_index': sq.get('correct_index')},
                    mode='exam_meaning', source='exam',
                    reason_code=g['reason_code'],
                    dedupe_key=_pending_dedupe_key(
                        'exam_meaning', 'exam', 'exam_meaning', qid,
                        request.session.session_key or ''))
                pending_count += 1
                results.append({
                    'word_id': qid, 'word': word.word, 'meanings': word.get_meanings(),
                    'correct': False, 'pending': True, 'pending_id': pending.id,
                    'reason_code': g['reason_code'],
                    'selected_index': ans.get('selected'),
                    'correct_index': sq.get('correct_index'), 'type': qtype,
                    'comment': g['comment'], 'source': 'pending',
                    'answer': ans.get('answer') or '',
                    'progress_note': '待复核，未影响掌握状态',
                })
                continue

            if is_correct:
                correct_count += 1

            if word and not answered:
                # 未作答：考试成绩按错题计分，但不改动任何学习进度
                # （避免空交一场考试就把已掌握的词批量打成「不会」）
                results.append({
                    'word_id': qid,
                    'word': word.word,
                    'meanings': word.get_meanings(),
                    'correct': False,
                    'selected_index': ans.get('selected'),
                    'correct_index': sq.get('correct_index'),
                    'type': qtype,
                    'comment': g['comment'],
                    'source': g['source'],
                    'answer': ans.get('answer') or '',
                    'progress_note': '未作答，未影响掌握状态',
                })
                continue

            if word:
                progress, _ = StudyProgress.objects.get_or_create(word=word)
                progress_note = ''
                if progress.status == 'new':
                    progress.learned_date = today
                    progress.is_today_new = True
                if qtype == 'spelling':
                    progress.spelling_attempts += 1
                    if is_correct:
                        progress.spelling_correct += 1
                elif qtype == 'meaning':
                    progress.meaning_attempts += 1
                    if is_correct:
                        progress.meaning_correct += 1
                if is_correct:
                    # 答对 → 会
                    progress.mastery_level = 5
                    progress.status = 'mastered'
                    checkin.today_correct += 1
                else:
                    # 真实答错：记错因一次；但已掌握的词不因一次考试失误清零降级
                    checkin.today_wrong += 1
                    progress.error_count += 1
                    if progress.status == 'mastered':
                        # 保持「会」，连续答对计数重新积累，并安排明天复习巩固
                        progress.consecutive_correct = 0
                        progress_note = '已掌握的词答错一次：保持「会」，已安排明天复习巩固'
                    else:
                        progress.mastery_level = 0
                        progress.status = 'learning'
                        progress_note = '已标记为「不会」'
                progress.review_count += 1
                progress.last_review = now
                # 自动复习调度：今天考过的词 → 明天自动进入复习栏目
                _schedule_next_review(progress, today)
                progress.save()

                # 真实答错 → 沉淀错误事件（只记录知识错误，不改计分/掌握口径）
                if not is_correct:
                    _record_study(
                        word, 'meaning_wrong' if qtype == 'meaning' else
                        ('spelling_wrong' if qtype == 'spelling' else 'unknown'),
                        ans,
                        mode=f'exam_{qtype}',
                        mistake_type=_exam_mistake_type(qtype, exam_direction),
                        error_detail=_exam_error_detail(qtype, word, ans, sq),
                    )
                results.append({
                    'word_id': qid,
                    'word': word.word,
                    'meanings': word.get_meanings(),
                    'correct': is_correct,
                    'selected_index': ans.get('selected'),
                    'correct_index': sq.get('correct_index'),
                    'type': qtype,
                    'comment': g['comment'],
                    'source': g['source'],
                    'answer': ans.get('answer') or '',
                    'progress_note': progress_note,
                })
            else:
                results.append({
                    'word_id': qid, 'word': '?', 'meanings': [],
                    'correct': is_correct, 'selected_index': ans.get('selected'),
                    'correct_index': sq.get('correct_index'),
                    'type': qtype,
                    'answer': ans.get('answer') or '',
                    'progress_note': '',
                })

        checkin.save()
        auto_checked = update_daily_checkin()

        total = len(stored)
        StudySession.objects.create(
            date=today,
            mode='exam',
            end_time=now,
            words_count=total,
            correct_count=correct_count,
        )

        # 清除本次考试数据（含逐题判定缓存）
        request.session.pop('exam_direction', None)
        request.session.pop('exam_questions', None)
        request.session.pop('exam_judgments', None)

        judged_count = total - pending_count
        meaning_total = (source_stats['ai'] + source_stats['local_exact']
                         + source_stats['local_blank'] + source_stats['pending'])
        return JsonResponse({
            'success': True,
            'total': total,
            'correct_count': correct_count,
            'wrong_count': judged_count - correct_count,
            'pending_count': pending_count,
            'correct_rate': round(correct_count / judged_count * 100, 1) if judged_count else 0,
            'duration': duration,
            'results': results,
            'auto_checked': auto_checked,
            'meaning_judge_stats': {
                'total': meaning_total,
                'ai': source_stats['ai'],
                'local_exact': source_stats['local_exact'],
                'local_fallback': 0,
                'blank': source_stats['local_blank'],
                'pending': source_stats['pending'],
            },
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


def api_units(request):
    units = Unit.objects.annotate(
        mastered=Count('words__progress', filter=Q(words__progress__status='mastered'))
    ).all()
    data = []
    for u in units:
        data.append({
            'number': u.number,
            'name': u.name,
            'category': u.category,
            'category_display': u.get_category_display(),
            'word_count': u.word_count,
            'mastered_count': u.mastered_count(),
            'progress_percent': u.progress_percent(),
        })
    return JsonResponse({'units': data})


@require_http_methods(['POST'])
def api_unit_create(request):
    """创建新单元（词汇类别可为必考/基础/超纲，也支持自定义）"""
    try:
        data = json.loads(request.body)
        number = int(data.get('number', 0))
        name = data.get('name', '').strip()
        category = (data.get('category') or 'required').strip()

        if not name:
            return JsonResponse({'error': '单元名称不能为空'}, status=400)
        if not category or len(category) > 20:
            return JsonResponse({'error': '词汇类别不能超过20个字符'}, status=400)
        if Unit.objects.filter(number=number).exists():
            return JsonResponse({'error': f'单元编号 {number} 已存在'}, status=400)

        unit = Unit.objects.create(
            number=number,
            name=name,
            category=category,
            word_count=0,
        )
        return JsonResponse({
            'success': True,
            'unit': {
                'id': unit.id,
                'number': unit.number,
                'name': unit.name,
                'category': unit.category,
                'word_count': unit.word_count,
            }
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@require_http_methods(['POST', 'DELETE'])
def api_unit_delete(request, unit_number):
    """删除单元及其所有单词"""
    try:
        unit = get_object_or_404(Unit, number=unit_number)
        word_count = unit.words.count()
        unit_name = unit.name
        unit.delete()
        return JsonResponse({
            'success': True,
            'message': f'已删除单元 {unit_name}（含 {word_count} 个单词）',
            'deleted_words': word_count,
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@require_http_methods(['POST'])
def api_unit_update(request, unit_number):
    """更新单元信息"""
    try:
        data = json.loads(request.body)
        unit = get_object_or_404(Unit, number=unit_number)
        new_name = data.get('name', '').strip()
        new_category = (data.get('category') or '').strip()

        if not new_name:
            return JsonResponse({'error': '单元名称不能为空'}, status=400)

        unit.name = new_name
        if new_category:
            if new_category == '__custom__':
                return JsonResponse({'error': '词汇类别无效'}, status=400)
            if len(new_category) > 20:
                return JsonResponse({'error': '词汇类别不能超过20个字符'}, status=400)
            unit.category = new_category
        unit.save()

        return JsonResponse({
            'success': True,
            'unit': {
                'number': unit.number,
                'name': unit.name,
                'category': unit.category,
                'word_count': unit.word_count,
            }
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


# ─── 手动添加 / AI 导入单词 ────────────────────────────────

@require_http_methods(['POST'])
def api_word_create(request):
    """手动添加或 AI 导入单词（统一走规范化层）"""
    try:
        data = json.loads(request.body)
        nd = normalize_word_data(data)
        if not nd:
            return JsonResponse({'error': '单词不能为空'}, status=400)

        # 如果单词已存在，返回已存在信息
        existing = Word.objects.filter(word__iexact=nd['word']).first()
        if existing:
            return JsonResponse({'error': '单词已存在', 'word_id': existing.id}, status=409)

        # 自动获取或创建单元
        unit_number = int(data.get('unit_number', 99) or 99)
        unit, _ = Unit.objects.get_or_create(
            number=unit_number,
            defaults={
                'name': data.get('unit_name') or f'自定义 List {unit_number}',
                'category': data.get('category', 'required'),
            }
        )

        word = Word.objects.create(
            word=nd['word'],
            phonetic_us=nd['phonetic_us'],
            phonetic_uk=nd['phonetic_uk'],
            pos=nd['pos'],
            meanings=json.dumps(nd['meanings'], ensure_ascii=False),
            meanings_by_pos=json.dumps(nd['meanings_by_pos'], ensure_ascii=False),
            uncommon_meanings=json.dumps(nd['uncommon_meanings'], ensure_ascii=False),
            collocations=json.dumps(nd['collocations'], ensure_ascii=False),
            word_forms=json.dumps(nd['word_forms'], ensure_ascii=False),
            example_en=nd['example_en'],
            example_zh=nd['example_zh'],
            category=data.get('category', 'required'),
            unit=unit,
            list_number=unit.words.count() + 1,
        )

        # 更新单元词数
        unit.word_count = unit.words.count()
        unit.save()

        # 导入后自动 AI 补全（按词性释义 + 例句），失败不影响导入结果
        try:
            ai_complete_words([word])
        except Exception:
            pass

        return JsonResponse({'success': True, 'word_id': word.id})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@require_http_methods(['POST'])
def api_word_bulk_import(request):
    """批量导入单词 - AI识别结果批量导入（统一走规范化层）"""
    try:
        data = json.loads(request.body)
        words_data = data.get('words', [])
        if not words_data:
            return JsonResponse({'error': '没有单词数据'}, status=400)

        imported = 0
        skipped = 0
        imported_words = []
        new_word_objs = []
        unit_number = int(data.get('unit_number', 99) or 99)
        unit, _ = Unit.objects.get_or_create(
            number=unit_number,
            defaults={
                'name': data.get('unit_name') or f'自定义 List {unit_number}',
                'category': data.get('category', 'required'),
            }
        )

        next_list_number = (unit.words.aggregate(m=Max('list_number'))['m'] or 0) + 1

        # 批量预取已存在单词（小写集合），避免逐词 N+1 查询
        existing_lower = {w.lower() for w in Word.objects.values_list('word', flat=True)}

        for wd in words_data:
            nd = normalize_word_data(wd)
            if not nd:
                continue
            if nd['word'].lower() in existing_lower:
                skipped += 1
                continue
            existing_lower.add(nd['word'].lower())

            word = Word.objects.create(
                word=nd['word'],
                phonetic_us=nd['phonetic_us'],
                phonetic_uk=nd['phonetic_uk'],
                pos=nd['pos'],
                meanings=json.dumps(nd['meanings'], ensure_ascii=False),
                meanings_by_pos=json.dumps(nd['meanings_by_pos'], ensure_ascii=False),
                uncommon_meanings=json.dumps(nd['uncommon_meanings'], ensure_ascii=False),
                collocations=json.dumps(nd['collocations'], ensure_ascii=False),
                word_forms=json.dumps(nd['word_forms'], ensure_ascii=False),
                example_en=nd['example_en'],
                example_zh=nd['example_zh'],
                category=nd.get('category') or data.get('category', 'required'),
                unit=unit,
                list_number=next_list_number,
            )
            next_list_number += 1
            imported += 1
            imported_words.append(nd['word'])
            new_word_objs.append(word)

        unit.word_count = unit.words.count()
        unit.save()

        # 写入导入记录
        ImportLog.objects.create(
            source=data.get('source', 'text'),
            unit_number=unit_number,
            unit_name=unit.name,
            imported_count=imported,
            skipped_count=skipped,
            words_list=json.dumps(imported_words, ensure_ascii=False),
        )

        # 导入后自动 AI 补全（按词性释义 + 例句）改为后台异步执行：
        # 不让用户卡在导入按钮上，补全结果稍后自动出现在词库中；失败不影响导入结果。
        if new_word_objs:
            def _async_complete(objs):
                close_old_connections()
                try:
                    ai_complete_words(objs)
                except Exception:
                    pass
                finally:
                    close_old_connections()
            threading.Thread(target=_async_complete, args=(new_word_objs,), daemon=True).start()

        return JsonResponse({
            'success': True,
            'imported': imported,
            'skipped': skipped,
            'completing': bool(new_word_objs),
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


def import_logs(request):
    """导入记录页面：查看历史导入记录"""
    logs = ImportLog.objects.all()[:100]
    total_imported = ImportLog.objects.aggregate(
        t=Sum('imported_count'))['t'] or 0
    total_logs = ImportLog.objects.count()
    return render(request, 'import_logs.html', {
        'logs': logs,
        'total_imported': total_imported,
        'total_logs': total_logs,
    })


@require_http_methods(['POST', 'DELETE'])
def api_import_log_delete(request, log_id):
    """删除一条导入记录（仅删除记录，不影响已导入的单词）"""
    log = get_object_or_404(ImportLog, id=log_id)
    log.delete()
    return JsonResponse({'success': True, 'message': '记录已删除'})


@require_http_methods(['POST'])
def api_word_update(request, word_id):
    """更新单词信息（完整字段）"""
    try:
        data = json.loads(request.body)
        word = get_object_or_404(Word, id=word_id)

        word.word = data.get('word', word.word).strip()
        word.phonetic_us = data.get('phonetic_us', word.phonetic_us)
        word.phonetic_uk = data.get('phonetic_uk', word.phonetic_uk)
        word.pos = data.get('pos', word.pos)
        word.category = data.get('category', word.category)

        meanings = data.get('meanings')
        meanings_by_pos = data.get('meanings_by_pos')
        if meanings_by_pos is not None:
            # 前端词性标签结构：以按词性分组为准，同步生成 pos 与 meanings（三字段一致）
            nd = normalize_word_data({
                'word': word.word,
                'pos': data.get('pos', word.pos),
                'meanings_by_pos': meanings_by_pos,
            })
            word.meanings_by_pos = json.dumps(nd['meanings_by_pos'], ensure_ascii=False)
            word.meanings = json.dumps(nd['meanings'], ensure_ascii=False)
            if nd['pos']:
                word.pos = nd['pos']
        elif meanings is not None:
            # 旧方式：手动修改释义文本 → 识别词性前缀重建分组
            nd = normalize_word_data({
                'word': word.word,
                'pos': data.get('pos', word.pos),
                'meanings': meanings,
            })
            word.meanings = json.dumps(nd['meanings'], ensure_ascii=False)
            word.meanings_by_pos = json.dumps(nd['meanings_by_pos'], ensure_ascii=False)
            if nd['pos']:
                word.pos = nd['pos']

        uncommon_meanings = data.get('uncommon_meanings')
        if uncommon_meanings is not None:
            word.uncommon_meanings = json.dumps(uncommon_meanings, ensure_ascii=False)

        collocations = data.get('collocations')
        if collocations is not None:
            word.collocations = json.dumps(collocations, ensure_ascii=False)

        word_forms = data.get('word_forms')
        if word_forms is not None:
            word.word_forms = json.dumps(word_forms, ensure_ascii=False)

        word.example_en = data.get('example_en', word.example_en)
        word.example_zh = data.get('example_zh', word.example_zh)

        if 'list_number' in data and data['list_number']:
            word.list_number = int(data['list_number'])

        # 单元变更：移动到指定单元（缺省时保持原单元）
        unit_number = data.get('unit_number')
        if unit_number:
            unit, _ = Unit.objects.get_or_create(
                number=int(unit_number),
                defaults={
                    'name': data.get('unit_name') or f'自定义 List {unit_number}',
                    'category': word.category,
                }
            )
            word.unit = unit

        word.save()

        # 更新新旧单元的词数
        if unit_number:
            for u in Unit.objects.filter(id__in=[word.unit_id]):
                u.word_count = u.words.count()
                u.save()
        word.unit.word_count = word.unit.words.count()
        word.unit.save()

        return JsonResponse({'success': True, 'message': '单词已更新'})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


@require_http_methods(['POST', 'DELETE'])
def api_word_delete(request, word_id):
    """删除单词"""
    try:
        word = get_object_or_404(Word, id=word_id)
        word_text = word.word
        unit = word.unit
        word.delete()

        if unit:
            unit.word_count = unit.words.count()
            unit.save()

        return JsonResponse({
            'success': True,
            'message': f'已删除单词 "{word_text}"',
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


# ─── AI 图片识别导入 ─────────────────────────────────────────

def extract_json_array(content):
    """从 AI 输出中稳健提取 JSON 数组"""
    if not content:
        return []
    content = content.strip()
    # 去掉可能的代码块围栏
    if content.startswith('```'):
        content = re.sub(r'^```[a-zA-Z]*\n?', '', content)
        content = re.sub(r'```$', '', content).strip()
    try:
        parsed = json.loads(content)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and isinstance(parsed.get('words'), list):
            return parsed['words']
        return []
    except (json.JSONDecodeError, TypeError):
        pass
    # 兜底：截取首个 [ 到最后一个 ]
    start = content.find('[')
    end = content.rfind(']')
    if start != -1 and end > start:
        try:
            return json.loads(content[start:end + 1])
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def normalize_phonetic(ph):
    """规范化音标：确保用 / 包裹，去除多余空白"""
    ph = (ph or '').strip()
    if not ph:
        return ''
    # 已有斜杠包裹的直接返回
    if ph.startswith('/') and ph.endswith('/') and len(ph) > 1:
        return ph
    # 去除首尾零散斜杠后重新包裹
    ph = ph.strip('/').strip()
    return '/' + ph + '/' if ph else ''


# ─── 词性标签：识别与分组 ──────────────────────────────────
UNCLASSIFIED_POS = '未分类'
KNOWN_POS_TAGS = {
    'v.', 'vi.', 'vt.', 'n.', 'adj.', 'adv.', 'prep.', 'pron.',
    'conj.', 'num.', 'art.', 'aux.', 'interj.', 'int.', 'abbr.',
    'phr.', 'modal.', 'det.', 'pro.',
}
POS_PREFIX_RE = re.compile(r'^([a-zA-Z]+\.)\s*(.*)$')


def split_meanings_by_pos(meanings):
    """把释义文本识别词性前缀，解析为按词性分组字典。

    例如 ['v. 产生', 'v. 生成', 'n. 一代', '生成'] →
    {'v.': ['产生', '生成'], 'n.': ['一代'], '未分类': ['生成']}
    """
    result = {}
    for m in meanings:
        mm = str(m).strip()
        if not mm:
            continue
        mo = POS_PREFIX_RE.match(mm)
        if mo and mo.group(1).lower() in KNOWN_POS_TAGS:
            key = mo.group(1).lower()
            text = mo.group(2).strip()
            if text:
                result.setdefault(key, []).append(text)
        else:
            result.setdefault(UNCLASSIFIED_POS, []).append(mm)
    return result


def normalize_word_data(w):
    """规范化单个单词数据（音标、字段清理），所有导入入口共用。

    核心规则：pos / meanings / meanings_by_pos 三字段自动同步——
    - 若传入了 meanings_by_pos（词性标签 → 释义），则自动推导 pos 与 meanings
    - 若未传 meanings_by_pos，则从 meanings 文本中识别词性前缀自动分组
    """
    if not isinstance(w, dict):
        return None
    word_text = str(w.get('word', '')).strip()
    if not word_text:
        return None
    phonetic_us = normalize_phonetic(w.get('phonetic_us', '') or w.get('phonetic', ''))
    phonetic_uk = normalize_phonetic(w.get('phonetic_uk', ''))
    # 英式音标缺失时用美式填充，避免导入后音标空白
    if not phonetic_uk and phonetic_us:
        phonetic_uk = phonetic_us

    def _str_list(v):
        if isinstance(v, str):
            return [s.strip() for s in v.replace('；', ';').split(';') if s.strip()]
        if isinstance(v, (list, tuple)):
            return [str(s).strip() for s in v if str(s).strip()]
        return []

    def _str_dict(v):
        if not isinstance(v, dict):
            return {}
        clean = {}
        for k, vals in v.items():
            if isinstance(vals, (list, tuple)):
                items = [str(x).strip() for x in vals if str(x).strip()]
                if items:
                    clean[str(k).strip()] = items
        return clean

    meanings = _str_list(w.get('meanings'))
    meanings_by_pos = _str_dict(w.get('meanings_by_pos'))
    pos = str(w.get('pos', '') or '').strip()

    # 未提供按词性分组时，尝试从释义文本中识别词性前缀
    if not meanings_by_pos:
        meanings_by_pos = split_meanings_by_pos(meanings)

    # 按词性分组生效时，同步生成 pos（标签串）与 meanings（合并释义）
    if meanings_by_pos:
        pos_keys = [k for k in meanings_by_pos if k != UNCLASSIFIED_POS]
        if pos_keys:
            pos = '/'.join(pos_keys)
        merged = []
        for k in meanings_by_pos:
            merged.extend(meanings_by_pos[k])
        if merged:
            meanings = merged

    return {
        'word': word_text,
        'phonetic_us': phonetic_us,
        'phonetic_uk': phonetic_uk,
        'pos': pos,
        'meanings': meanings,
        'meanings_by_pos': meanings_by_pos,
        'uncommon_meanings': _str_list(w.get('uncommon_meanings')),
        'collocations': _str_list(w.get('collocations')),
        'word_forms': _str_dict(w.get('word_forms')),
        'example_en': str(w.get('example_en', '') or '').strip(),
        'example_zh': str(w.get('example_zh', '') or '').strip(),
    }


def normalize_ai_words(words):
    """规范化 AI 识别结果（基于公共规范化函数）"""
    clean = []
    for w in words:
        nd = normalize_word_data(w)
        if nd:
            clean.append(nd)
    return clean


# ─── AI 请求健康包装层（HANDOVER 第八节）───────────────────────
AI_JUDGE_TIMEOUT = 8            # 判定专用短超时（秒）；生成类长任务 300s 契约不变
AI_HEALTH_FAIL_THRESHOLD = 3    # 连续可用性失败达此值即熔断
AI_HEALTH_COOLDOWN_SECONDS = 300
AI_PROBE_LOCK_SECONDS = 60      # 半开探测锁窗口，防止多页面同时重试造成请求风暴
AI_JUDGE_MAX_ATTEMPTS = 2
# 只有这些「可用性失败」参与熔断计数；invalid（空内容/非法结构）只算输出质量问题，
# config（401/403/404）单独标记为配置错误并要求手动测试成功后才清除。
AI_HEALTH_AVAILABILITY_CATEGORIES = ('network', 'timeout', 'ratelimit', 'server')


class _AIRequestError(Exception):
    """AI 请求失败：category ∈ timeout / network / ratelimit / server / config / invalid"""
    def __init__(self, category, message):
        super().__init__(message)
        self.category = category
        self.message = message


def _ai_first_choice(result):
    """校验 OpenAI 兼容响应结构并返回 choices[0]（dict）。
    结构异常（根节点非对象 / choices 缺失或为空 / 元素非对象）一律抛
    _AIRequestError('invalid')，避免调用方再直接假设 choices[0] 存在。"""
    if not isinstance(result, dict):
        raise _AIRequestError('invalid', 'AI 响应不是 JSON 对象')
    choices = result.get('choices')
    if not isinstance(choices, list) or not choices:
        raise _AIRequestError('invalid', 'AI 响应缺少 choices')
    first = choices[0]
    if not isinstance(first, dict):
        raise _AIRequestError('invalid', 'AI 响应 choices 结构异常')
    return first


def _ai_extract_content(result):
    """结构校验 + 提取非空 choices[0].message.content；异常抛 _AIRequestError('invalid')。"""
    first = _ai_first_choice(result)
    message = first.get('message')
    if not isinstance(message, dict):
        raise _AIRequestError('invalid', 'AI 响应缺少 message')
    content = message.get('content')
    if not isinstance(content, str) or not content.strip():
        raise _AIRequestError('invalid', 'AI 返回为空')
    return content.strip()


def _ai_parse_json_body(raw):
    """响应体 → JSON 对象；解码或解析失败归为 invalid（不回显第三方原始内容）。"""
    try:
        text = raw.decode('utf-8') if isinstance(raw, (bytes, bytearray)) else str(raw)
        return json.loads(text)
    except (UnicodeDecodeError, ValueError, TypeError):
        raise _AIRequestError('invalid', 'AI 响应不是合法 JSON')


def _ai_post_json(cfg, payload, timeout):
    """底层 OpenAI 兼容 POST：成功返回内容文本；失败抛已分类的 _AIRequestError。
    读取、UTF-8 解码、JSON 解析与响应结构校验全部在此收口，
    畸形 2xx（HTML body / 非法 JSON / choices 异常 / 空内容）一律归为 invalid，
    保证判定链路能安全降级为 pending_review，而不是 500。"""
    req = urllib.request.Request(
        resolve_ai_endpoint(cfg['base_url'], cfg['endpoint']),
        data=json.dumps(payload).encode('utf-8'),
        headers=build_ai_headers(cfg['api_key']),
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'ignore')
        code = e.code
        if code in (401, 403):
            raise _AIRequestError('config', '密钥或权限无效 (HTTP %s)' % code)
        if code == 404:
            raise _AIRequestError('config', '模型或接口地址不存在 (HTTP 404)')
        if code == 429:
            raise _AIRequestError('ratelimit', '请求过于频繁 (HTTP 429)')
        if code >= 500:
            raise _AIRequestError('server', '服务端错误 (HTTP %s)' % code)
        raise _AIRequestError('config', '接口错误 (HTTP %s): %s' % (code, body[:200]))
    except (socket.timeout, TimeoutError):
        raise _AIRequestError('timeout', 'AI 请求超时')
    except urllib.error.URLError as e:
        reason = getattr(e, 'reason', e)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise _AIRequestError('timeout', 'AI 请求超时')
        raise _AIRequestError('network', '无法连接 AI 服务: %s' % reason)
    return _ai_extract_content(_ai_parse_json_body(raw))


def _get_model_health(model_pk):
    """取（或建）某 AIModel 的健康记录；model_pk 缺失/非法时返回 None（如旧版内联配置）。"""
    if not model_pk:
        return None
    try:
        m = AIModel.objects.filter(id=int(model_pk)).first()
    except (TypeError, ValueError):
        return None
    if m is None:
        return None
    health, _ = AIModelHealth.objects.get_or_create(model=m)
    return health


def _acquire_ai_slot(health, now):
    """熔断门禁：返回 (allowed, reason_code)。
    未熔断放行；熔断中拒绝；冷却到点用条件 UPDATE 抢半开锁，同一时刻只放行一条请求。"""
    if health is None:
        return True, ''
    co = health.circuit_open_until
    if co is None:
        return True, ''
    if now < co:
        return False, 'ai_circuit_open'
    lock_until = now + timedelta(seconds=AI_PROBE_LOCK_SECONDS)
    updated = AIModelHealth.objects.filter(
        id=health.id, circuit_open_until__lte=now).update(
        circuit_open_until=lock_until, last_probe_at=now)
    if updated:
        return True, ''
    return False, 'ai_circuit_open'


def _ai_health_record_success(model_pk):
    """记录一次可用性成功：清零失败计数与熔断。"""
    health = _get_model_health(model_pk)
    if health is None:
        return
    health.last_success_at = timezone.now()
    health.fail_streak = 0
    health.circuit_open_until = None
    health.last_error = ''
    health.last_failure_category = ''
    health.save(update_fields=['last_success_at', 'fail_streak', 'circuit_open_until',
                               'last_error', 'last_failure_category'])


def _ai_health_record_failure(model_pk, category, message):
    """记录一次失败。
    - 只有「可用性失败」（network / timeout / ratelimit / server）参与熔断计数；
    - `invalid`（空内容 / 非法结构）只记录输出质量错误，不改变连接健康，避免误熔断；
    - 配置类错误（401/403/404）单独标记并立即熔断，需手动探活成功后才清除。"""
    health = _get_model_health(model_pk)
    if health is None:
        return
    now = timezone.now()
    health.last_failure_at = now
    health.last_error = (message or '')[:200]
    health.last_failure_category = category or ''
    if category == 'config':
        health.fail_streak = AI_HEALTH_FAIL_THRESHOLD
        health.circuit_open_until = now + timedelta(seconds=AI_HEALTH_COOLDOWN_SECONDS)
    elif category in AI_HEALTH_AVAILABILITY_CATEGORIES:
        health.fail_streak = (health.fail_streak or 0) + 1
        if health.fail_streak >= AI_HEALTH_FAIL_THRESHOLD:
            health.circuit_open_until = now + timedelta(seconds=AI_HEALTH_COOLDOWN_SECONDS)
    health.save(update_fields=['last_failure_at', 'last_error', 'last_failure_category',
                               'fail_streak', 'circuit_open_until'])


def _ai_reason_code(category):
    """失败类别 → 前端可解释的原因码（reason_code）。"""
    return {
        'timeout': 'ai_timeout',
        'network': 'ai_network_error',
        'ratelimit': 'ai_rate_limited',
        'server': 'ai_server_error',
        'config': 'ai_config_error',
        'invalid': 'ai_invalid_output',
        'circuit': 'ai_circuit_open',
    }.get(category or '', 'ai_unavailable')


def _ai_health_status(m):
    """供设置页展示的模型健康状态：
    available / config_error / cooling / output_error / unknown。"""
    health = getattr(m, 'health', None)
    if health is None:
        return {'state': 'unknown', 'label': '未检测',
                'circuit_open_until': None, 'last_error': '', 'last_success_at': None}
    now = timezone.now()
    cooling = bool(health.circuit_open_until and health.circuit_open_until > now)
    if health.last_failure_category == 'config':
        state, label = 'config_error', '配置错误'
    elif cooling:
        state, label = 'cooling', '暂不可用，冷却至 %s' % timezone.localtime(
            health.circuit_open_until).strftime('%H:%M')
    elif health.last_failure_category == 'invalid':
        # 连接正常但输出不合法：只作输出质量提示，不算「不可用」
        state, label = 'output_error', '输出异常'
    elif health.last_success_at:
        state, label = 'available', '可用'
    else:
        state, label = 'unknown', '未检测'
    return {
        'state': state, 'label': label,
        'circuit_open_until': health.circuit_open_until.isoformat() if health.circuit_open_until else None,
        'last_error': health.last_error,
        'last_failure_category': health.last_failure_category,
        'last_success_at': health.last_success_at.isoformat() if health.last_success_at else None,
    }


def _ai_gate(model_pk):
    """统一熔断门禁：返回 '' 表示放行，否则返回原因码（此时不得出网）。
    判定类与生成类共用；手动探活 api_ai_test 属于显式连通性测试，不经过此门禁。"""
    allowed, reason = _acquire_ai_slot(_get_model_health(model_pk), timezone.now())
    return '' if allowed else reason


def _ai_judge_call(cfg, prompt, max_tokens=500, temperature=0.1):
    """判定类 AI 调用：8 秒专用超时；可用性失败最多快速重试一次；配置类错误不重试。
    返回 (content, error, reason_code)：成功时后两项均为 ''。
    熔断开启时不出网，直接返回 reason_code='ai_circuit_open'。"""
    model_pk = cfg.get('id')
    payload = {
        'model': cfg['model_id'],
        'temperature': temperature,
        'max_tokens': max_tokens,
        'messages': [{'role': 'user', 'content': prompt}],
    }
    reason = _ai_gate(model_pk)
    if reason:
        return None, '', reason
    last = None
    for _attempt in range(AI_JUDGE_MAX_ATTEMPTS):
        try:
            content = _ai_post_json(cfg, payload, AI_JUDGE_TIMEOUT)
            _ai_health_record_success(model_pk)
            return content, '', ''
        except _AIRequestError as e:
            last = e
            if e.category == 'config':
                break
    _ai_health_record_failure(model_pk, last.category, last.message)
    return None, last.message, _ai_reason_code(last.category)


def _ai_category_from_code(code):
    """HTTP 状态码 → 失败类别（供探活/生成类调用记录健康状态）。"""
    if code == 429:
        return 'ratelimit'
    if code >= 500:
        return 'server'
    return 'config'


def _ai_chat_once(cfg, prompt, max_tokens=4000, temperature=0.5):
    """单次调用 AI（OpenAI 兼容），返回纯文本内容。
    生成/讲解类沿用 180 秒长超时（契约不变）；判定类请用 _ai_judge_call。
    同样先过统一熔断门禁，避免失效模型在后台任务里反复等待。"""
    if _ai_gate(cfg.get('id')):
        raise _AIRequestError('circuit', 'ai_circuit_open：AI 暂不可用（熔断冷却中），已跳过请求')
    payload = {
        'model': cfg['model_id'],
        'temperature': temperature,
        'max_tokens': max_tokens,
        'messages': [{'role': 'user', 'content': prompt}],
    }
    return _ai_post_json(cfg, payload, 180)


def _extract_json_object(content):
    """从 AI 响应中提取 JSON 对象（兼容 ```json 包裹）"""
    s = content.find('{')
    e = content.rfind('}')
    if s == -1 or e == -1 or e <= s:
        return {}
    try:
        return json.loads(content[s:e + 1])
    except Exception:
        return {}


def _extract_json_array(content):
    """从 AI 响应中提取 JSON 数组（兼容 ```json 包裹）"""
    s = content.find('[')
    e = content.rfind(']')
    if s == -1 or e == -1 or e <= s:
        return []
    try:
        val = json.loads(content[s:e + 1])
        return val if isinstance(val, list) else []
    except Exception:
        return []


def ai_complete_words(word_objs):
    """导入后自动 AI 补全：为缺音标 / 缺按词性释义 / 缺例句的单词批量生成（失败静默，不影响导入结果）"""
    if not word_objs:
        return
    # 缺音标：美式音标为空（英式缺失会在写入时用美式填充）
    need_ph = [w for w in word_objs if not w.phonetic_us.strip()]
    # 需要按词性归类的：没有按词性释义，或全部被归到「未分类」
    need_pos = [
        w for w in word_objs
        if not w.get_meanings_by_pos() or all(k == '未分类' for k in w.get_meanings_by_pos())
    ]
    need_ex = [w for w in word_objs if not w.example_en.strip()]
    if not need_ph and not need_pos and not need_ex:
        return
    settings_obj = UserSettings.get_settings()
    model_data = {}
    if settings_obj.recognize_model:
        model_data['model_id'] = settings_obj.recognize_model_id
    try:
        cfg = resolve_ai_model(model_data)
    except ValueError:
        return
    batch = 30

    def _word_line(w):
        return '%s | %s | %s' % (w.word, w.pos or '', '；'.join(w.get_meanings()))

    try:
        # 1) 补音标（temperature 0.1，保证读音标注稳定）
        for i in range(0, len(need_ph), batch):
            chunk = need_ph[i:i + batch]
            try:
                data = _extract_json_object(
                    _ai_chat_once(cfg, phonetics_prompt([w.word for w in chunk]),
                                  max_tokens=2000, temperature=0.1))
            except Exception:
                data = {}
            for w in chunk:
                item = data.get(w.word)
                if not isinstance(item, dict):
                    continue
                us = normalize_phonetic(item.get('us') or item.get('phonetic_us'))
                uk = normalize_phonetic(item.get('uk') or item.get('phonetic_uk'))
                if not us:
                    continue
                if not uk:
                    uk = us
                w.phonetic_us = us
                w.phonetic_uk = uk
                w.save(update_fields=['phonetic_us', 'phonetic_uk'])
            time.sleep(0.5)
        # 2) 补按词性释义
        for i in range(0, len(need_pos), batch):
            chunk = need_pos[i:i + batch]
            lines = [_word_line(w) for w in chunk]
            prompt = pos_grouping_prompt(lines)
            try:
                data = _extract_json_object(_ai_chat_once(cfg, prompt))
            except Exception:
                data = {}
            for w in chunk:
                item = data.get(w.word)
                if isinstance(item, dict) and item:
                    w.meanings_by_pos = json.dumps(item, ensure_ascii=False)
                    w.save(update_fields=['meanings_by_pos'])
            time.sleep(0.5)
        # 3) 补例句
        for i in range(0, len(need_ex), batch):
            chunk = need_ex[i:i + batch]
            lines = [_word_line(w) for w in chunk]
            prompt = examples_prompt(lines)
            try:
                data = _extract_json_object(_ai_chat_once(cfg, prompt))
            except Exception:
                data = {}
            for w in chunk:
                item = data.get(w.word)
                if isinstance(item, dict) and item.get('en'):
                    w.example_en = item['en'].strip()
                    w.example_zh = (item.get('zh') or '').strip()
                    w.save(update_fields=['example_en', 'example_zh'])
            time.sleep(0.5)
    except Exception:
        pass


def resolve_ai_endpoint(base_url, endpoint):
    """拼接 OpenAI 兼容请求地址；endpoint 为完整 URL 时优先使用"""
    if endpoint and endpoint.strip():
        return endpoint.strip()
    return base_url.rstrip('/') + '/chat/completions'


def _model_has_key(m):
    return bool(m.api_key)


def _model_plain_key(m):
    """供 AI 调用取明文密钥：密钥以明文保存在本机数据库，直接读取。"""
    return m.api_key or ''


def _store_model_key(m, plain):
    """密钥以明文保存在本机数据库（不加密、不上传服务器）。"""
    m.api_key = plain or ''
    m.api_key_encrypted = ''


def build_ai_headers(api_key):
    """构造请求头；无密钥时省略 Authorization（本机无鉴权服务如 opencode）。
    部分中转服务商（Codex2API 等）前端挂了 Cloudflare，会把 Python-urllib UA 拦成 502，
    这里统一填充浏览器 UA 以保证兼容性。"""
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    }
    if api_key and api_key.strip():
        headers['Authorization'] = 'Bearer ' + api_key.strip()
    return headers


def serialize_ai_model(m):
    """AIModel 数据库记录 → 前端 JSON。密钥明文不回传列表，
    只给 has_key 供前端展示状态；查看明文走 /api/ai-models/key/。"""
    return {
        'id': m.id,
        'provider': m.provider,
        'model_id': m.model_id,
        'display_name': m.display_name,
        'base_url': m.base_url,
        'endpoint': m.endpoint,
        'has_key': _model_has_key(m),
        'context': m.context,
        'vision': m.vision,
        'enabled': m.enabled,
    }


def resolve_ai_model(data):
    """解析 AI 调用所需的模型配置（配置统一存数据库 AIModel 表，由设置页管理）。
    优先级：前端指定 model_id（数据库模型）> 前端内联配置（旧版兼容）> 数据库第一个启用模型。
    未找到时抛 ValueError，由调用方转为 400 响应。
    """
    model_id = data.get('model_id')
    if model_id:
        try:
            m = AIModel.objects.get(id=int(model_id))
        except (AIModel.DoesNotExist, ValueError, TypeError):
            raise ValueError('指定的模型不存在，请到「设置 → AI 模型」中重新选择')
        if not m.enabled:
            raise ValueError('该模型已被禁用，请到「设置 → AI 模型」中启用')
        cfg = serialize_ai_model(m)
        cfg['api_key'] = _model_plain_key(m)
        return cfg
    # 旧版兼容：前端直接传 api_key/base_url/endpoint/model
    if data.get('model') or data.get('api_key') or data.get('base_url') or data.get('endpoint'):
        cfg = {
            'id': None,
            'provider': 'custom',
            'model_id': (data.get('model') or '').strip(),
            'display_name': data.get('model') or '',
            'base_url': (data.get('base_url') or 'https://api.openai.com/v1').strip(),
            'endpoint': (data.get('endpoint') or '').strip(),
            'api_key': (data.get('api_key') or '').strip(),
            'context': '128K',
            'vision': True,
            'enabled': True,
        }
        if not cfg['model_id']:
            raise ValueError('请填写模型 ID')
        return cfg
    m = AIModel.objects.filter(enabled=True).order_by('id').first()
    if not m:
        raise ValueError('尚未配置 AI 模型，请先到「设置 → AI 模型」中添加并启用一个模型')
    cfg = serialize_ai_model(m)
    cfg['api_key'] = _model_plain_key(m)
    return cfg


@require_http_methods(['GET', 'POST', 'DELETE'])
def api_ai_models(request):
    """AI 模型管理：GET 列表 / POST 新增或更新 / DELETE 删除（配置统一存数据库，设置页管理）"""
    if request.method == 'GET':
        models = AIModel.objects.select_related('health').all().order_by('-enabled', 'id')
        return JsonResponse({'models': [
            dict(serialize_ai_model(m), health=_ai_health_status(m)) for m in models
        ]})

    if request.method == 'DELETE':
        try:
            body = json.loads(request.body)
        except Exception:
            body = {}
        mid = body.get('id') or request.GET.get('id')
        if mid:
            try:
                AIModel.objects.filter(id=int(mid)).delete()
            except (ValueError, TypeError):
                pass
        return JsonResponse({'success': True, 'deleted': mid})

    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': '请求格式错误'}, status=400)

    mid = data.get('id')
    if mid:
        try:
            m = AIModel.objects.get(id=int(mid))
        except (AIModel.DoesNotExist, ValueError, TypeError):
            return JsonResponse({'error': '模型不存在'}, status=404)
    else:
        m = AIModel()

    m.provider = (data.get('provider') or 'custom').strip() or 'custom'
    m.model_id = (data.get('model_id') or '').strip()
    m.display_name = (data.get('display_name') or '').strip()
    m.base_url = (data.get('base_url') or 'https://api.openai.com/v1').strip()
    m.endpoint = (data.get('endpoint') or '').strip()
    # 仅当请求显式带 api_key 时才改密钥；留空且模型已有密钥 = 不修改（启停等操作不受影响）
    if 'api_key' in data:
        plain_key = (data.get('api_key') or '').strip()
        if plain_key or not _model_has_key(m):
            _store_model_key(m, plain_key)
    m.context = (data.get('context') or '128K').strip() or '128K'
    if 'vision' in data:
        m.vision = bool(data.get('vision'))
    if 'enabled' in data:
        m.enabled = bool(data.get('enabled'))
    if not m.model_id:
        return JsonResponse({'error': '请填写模型 ID'}, status=400)
    m.save()
    return JsonResponse({'success': True, 'model': serialize_ai_model(m)})


@require_http_methods(['POST'])
def api_ai_model_key(request):
    """查看某个模型的 API 密钥明文（密钥仅保存在本机，编辑模型时回填用）。"""
    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': '请求格式错误'}, status=400)
    try:
        m = AIModel.objects.get(id=int(data.get('id') or 0))
    except (AIModel.DoesNotExist, ValueError, TypeError):
        return JsonResponse({'error': '模型不存在'}, status=404)
    return JsonResponse({'success': True, 'api_key': _model_plain_key(m)})


@require_http_methods(['POST'])
def api_ai_recognize(request):
    """AI 识别单词 → 结构化数据（OpenAI 兼容接口）
    支持三种输入源：图片(image) / 纯文本(text_content) / 文本文件(file_content)
    """
    try:
        data = json.loads(request.body)
        image_b64 = data.get('image', '')
        text_content = data.get('text_content', '')
        file_content = data.get('file_content', '')
        file_name = data.get('file_name', '')

        settings_obj = UserSettings.get_settings()
        if settings_obj.recognize_model and not data.get('model_id'):
            data['model_id'] = settings_obj.recognize_model_id

        try:
            cfg = resolve_ai_model(data)
        except ValueError as e:
            return JsonResponse({'error': str(e)}, status=400)
        api_key = cfg['api_key']
        base_url = cfg['base_url']
        endpoint = cfg['endpoint']
        model = cfg['model_id']

        if image_b64:
            prompt = recognize_image_prompt()
            payload = {
                'model': model,
                'temperature': 0.1,
                'messages': [{
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': prompt},
                        {'type': 'image_url', 'image_url': {
                            'url': 'data:image/jpeg;base64,' + image_b64
                        }},
                    ],
                }],
            }
        elif text_content:
            if len(text_content) > 30000:
                return JsonResponse({'error': '文本过长，请控制在 30000 字符以内'}, status=400)
            prompt = recognize_text_prompt(text_content)
            payload = {
                'model': model,
                'temperature': 0.1,
                'messages': [{'role': 'user', 'content': prompt}],
            }
        elif file_content:
            if len(file_content) > 60000:
                return JsonResponse({'error': '文件内容过长，请控制在 60000 字符以内'}, status=400)
            prompt = recognize_file_prompt(file_name, file_content)
            payload = {
                'model': model,
                'temperature': 0.1,
                'messages': [{'role': 'user', 'content': prompt}],
            }
        else:
            return JsonResponse({'error': '请上传图片、粘贴文本或上传文件'}, status=400)

        req = urllib.request.Request(
            resolve_ai_endpoint(base_url, endpoint),
            data=json.dumps(payload).encode('utf-8'),
            headers=build_ai_headers(api_key),
            method='POST',
        )

        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'ignore')
            return JsonResponse({'error': 'AI 接口错误 (HTTP %s): %s' % (e.code, body[:400])}, status=502)
        except urllib.error.URLError as e:
            url = resolve_ai_endpoint(base_url, endpoint)
            return JsonResponse({'error': '无法连接 %s: %s' % (url, e.reason)}, status=502)

        try:
            content = _ai_extract_content(_ai_parse_json_body(raw))
        except _AIRequestError as e:
            return JsonResponse({'error': 'AI 返回内容不合法：%s' % e.message}, status=502)
        words = normalize_ai_words(extract_json_array(content))
        if not words:
            return JsonResponse({'error': 'AI 未能识别出有效单词，请检查输入内容或更换模型'}, status=422)

        return JsonResponse({'words': words, 'count': len(words)})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@require_http_methods(['POST'])
def api_ai_review(request):
    """AI 复审：对识别结果进行二次校验，返回问题与修正建议"""
    try:
        data = json.loads(request.body)
        words = data.get('words', [])

        settings_obj = UserSettings.get_settings()
        if settings_obj.review_model and not data.get('model_id'):
            data['model_id'] = settings_obj.review_model_id

        try:
            cfg = resolve_ai_model(data)
        except ValueError as e:
            return JsonResponse({'error': str(e)}, status=400)
        api_key = cfg['api_key']
        base_url = cfg['base_url']
        endpoint = cfg['endpoint']
        model = cfg['model_id']

        if not words:
            return JsonResponse({'error': '没有可复审的单词'}, status=400)

        words_json = json.dumps(words, ensure_ascii=False)
        prompt = ai_review_prompt(words_json)

        payload = {
            'model': model,
            'temperature': 0.1,
            'messages': [{'role': 'user', 'content': prompt}],
        }

        req = urllib.request.Request(
            resolve_ai_endpoint(base_url, endpoint),
            data=json.dumps(payload).encode('utf-8'),
            headers=build_ai_headers(api_key),
            method='POST',
        )

        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'ignore')
            return JsonResponse({'error': 'AI 接口错误 (HTTP %s): %s' % (e.code, body[:400])}, status=502)
        except urllib.error.URLError as e:
            url = resolve_ai_endpoint(base_url, endpoint)
            return JsonResponse({'error': '无法连接 %s: %s' % (url, e.reason)}, status=502)

        try:
            content = _ai_extract_content(_ai_parse_json_body(raw))
        except _AIRequestError as e:
            return JsonResponse({'error': 'AI 返回内容不合法：%s' % e.message}, status=502)
        review = extract_json_array(content)

        # 规范化：保证数量与输入一致，缺失项按"正确"处理
        normalized = []
        for i, w in enumerate(words):
            item = review[i] if i < len(review) and isinstance(review[i], dict) else {}
            norm = {
                'word': str(w.get('word', '')),
                'correct': bool(item.get('correct', True)),
                'issues': [str(x) for x in (item.get('issues') or []) if str(x).strip()],
            }
            suggested = item.get('suggested')
            if suggested:
                norm['suggested'] = str(suggested)
            fix = item.get('fix')
            if isinstance(fix, dict):
                clean_fix = {}
                if fix.get('word') and str(fix.get('word')).strip() != str(w.get('word', '')):
                    clean_fix['word'] = str(fix['word']).strip()
                if fix.get('pos') and str(fix.get('pos')).strip() != str(w.get('pos', '')):
                    clean_fix['pos'] = str(fix['pos']).strip()
                if fix.get('meanings') and isinstance(fix['meanings'], list):
                    m = [str(x).strip() for x in fix['meanings'] if str(x).strip()]
                    if m:
                        clean_fix['meanings'] = m
                if fix.get('phonetic_us'):
                    clean_fix['phonetic_us'] = normalize_phonetic(fix['phonetic_us'])
                if fix.get('phonetic_uk'):
                    clean_fix['phonetic_uk'] = normalize_phonetic(fix['phonetic_uk'])
                if fix.get('example_en'):
                    clean_fix['example_en'] = str(fix['example_en']).strip()
                if fix.get('example_zh'):
                    clean_fix['example_zh'] = str(fix['example_zh']).strip()
                if clean_fix:
                    norm['fix'] = clean_fix
            normalized.append(norm)

        issue_count = sum(1 for r in normalized if not r['correct'])
        return JsonResponse({'review': normalized, 'issues': issue_count, 'total': len(normalized)})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@require_http_methods(['POST'])
def api_ai_export_pdf(request):
    """人工审核导出 PDF：将 AI 识别结果导出为可离线核对的 PDF 清单"""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))

    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': '请求格式错误'}, status=400)
    words = data.get('words') or []
    if not words:
        return JsonResponse({'error': '没有可导出的单词'}, status=400)

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            topMargin=20 * mm, bottomMargin=20 * mm,
                            leftMargin=15 * mm, rightMargin=15 * mm)
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle('title', fontName='STSong-Light', fontSize=14,
                                 leading=20, spaceAfter=4, alignment=1)
    sub_style = ParagraphStyle('sub', fontName='STSong-Light', fontSize=9,
                               leading=13, textColor=colors.grey, alignment=1)

    elements = [Paragraph('人工审核清单', title_style),
                Paragraph('共 %d 词 · 请离线核对打勾，完成后回到系统逐条确认' % len(words), sub_style),
                Spacer(1, 6 * mm)]

    table_data = [['序号', '单词', '音标', '词性', '释义', '状态']]
    for i, w in enumerate(words, 1):
        if not isinstance(w, dict):
            continue
        meanings_raw = w.get('meanings') or []
        if not isinstance(meanings_raw, list):
            meanings_raw = [meanings_raw]
        meanings_str = '; '.join([str(m) for m in meanings_raw if str(m).strip()])
        status = '待复核'
        if w.get('humanChecked'):
            status = '人工已核对'
        elif w.get('aiIssue'):
            status = 'AI 已修正'
        elif w.get('aiChecked'):
            status = 'AI 通过'
        table_data.append([
            str(i),
            str(w.get('word') or ''),
            str(w.get('phonetic_us') or w.get('phonetic_uk') or ''),
            str(w.get('pos') or ''),
            meanings_str,
            status,
        ])

    col_widths = [12 * mm, 42 * mm, 40 * mm, 14 * mm, 63 * mm, 24 * mm]
    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, 0), 'STSong-Light'),
        ('FONTNAME', (0, 0), (0, -1), 'STSong-Light'),
        ('FONTNAME', (1, 1), (1, -1), 'Helvetica'),
        ('FONTNAME', (2, 1), (2, -1), 'Helvetica'),
        ('FONTNAME', (3, 0), (3, -1), 'STSong-Light'),
        ('FONTNAME', (4, 0), (4, -1), 'STSong-Light'),
        ('FONTNAME', (5, 0), (5, -1), 'STSong-Light'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('GRID', (0, 0), (-1, -1), 0.3, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BACKGROUND', (0, 0), (-1, 0), colors.Color(0.9, 0.9, 0.9)),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.Color(0.95, 0.95, 0.95)]),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(t)

    doc.build(elements)
    buf.seek(0)
    return FileResponse(buf, content_type='application/pdf',
                        filename='人工审核_%s.pdf' % timezone.localdate().strftime('%Y%m%d'),
                        as_attachment=True)


@require_http_methods(['POST'])
def api_ai_test(request):
    """测试 API 密钥与模型连通性（发送最小请求验证）"""
    try:
        data = json.loads(request.body)
        # 支持只传 model_id：直接用库里已保存的配置（密钥从本机库读取，不经过前端）
        if data.get('model_id') and not data.get('api_key'):
            try:
                saved = AIModel.objects.get(id=int(data['model_id']))
            except (AIModel.DoesNotExist, ValueError, TypeError):
                return JsonResponse({'error': '模型不存在'}, status=404)
            api_key = _model_plain_key(saved)
            base_url = saved.base_url
            endpoint = saved.endpoint
            model = saved.model_id
            model_pk = saved.id
        else:
            api_key = data.get('api_key', '')
            base_url = data.get('base_url', 'https://api.openai.com/v1')
            endpoint = data.get('endpoint', '')
            model = data.get('model', 'gpt-4o-mini')
            model_pk = None

        if not model:
            return JsonResponse({'error': '请填写模型 ID'}, status=400)

        payload = {
            'model': model,
            'max_tokens': 1,
            'messages': [{'role': 'user', 'content': 'ping'}],
        }

        req = urllib.request.Request(
            resolve_ai_endpoint(base_url, endpoint),
            data=json.dumps(payload).encode('utf-8'),
            headers=build_ai_headers(api_key),
            method='POST',
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'ignore')
            err = '连接失败 (HTTP %s): %s' % (e.code, body[:200])
            _ai_health_record_failure(model_pk, _ai_category_from_code(e.code), err)
            # 401/403 = 密钥无效；404 = 模型或地址错误；其余透出
            return JsonResponse({
                'error': err,
                'key_invalid': e.code in (401, 403),
                'model_invalid': e.code == 404,
            }, status=502)
        except (socket.timeout, TimeoutError) as e:
            _ai_health_record_failure(model_pk, 'timeout', '探活超时')
            return JsonResponse({'error': '连接超时: %s' % e}, status=502)
        except urllib.error.URLError as e:
            url = resolve_ai_endpoint(base_url, endpoint)
            _ai_health_record_failure(model_pk, 'network', '无法连接 %s' % url)
            return JsonResponse({'error': '无法连接 %s: %s' % (url, e.reason)}, status=502)

        # 畸形 2xx（HTML / 非法 JSON / choices 异常）也必须返回可读 502，不能裸奔成 500。
        # 探活只校验响应结构，max_tokens=1 时 content 允许为空。
        try:
            _ai_first_choice(_ai_parse_json_body(raw))
        except _AIRequestError as e:
            _ai_health_record_failure(model_pk, 'invalid', e.message)
            return JsonResponse({'error': '接口响应异常：%s' % e.message}, status=502)
        _ai_health_record_success(model_pk)
        return JsonResponse({'success': True, 'message': '连接成功，密钥有效'})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ============================================================
# 考研写作工坊
# ============================================================

def builtin_ai_exam_call(prompt, temperature=0.7, data=None, action='exam_generate', practice=None,
                         settings_model_field='exam_model'):
    """考研工坊统一 AI 调用：复用现有模型配置体系（支持 DeepSeek 等 OpenAI 兼容接口）
    模型优先级：前端指定 model_id > 设置页 settings_model_field 对应模型 > 考研写作工坊模型
    > 第一个启用模型。六级相关接口传 settings_model_field='cet6_translation_model' 即可复用
    模型调用、调用日志和异常处理逻辑。
    调用详情（模型、耗时、成败、提示词预览）写入 AICallLog 供追溯。
    """
    import time as _time
    t0 = _time.monotonic()
    data = data or {}
    if not data.get('model_id'):
        settings_obj = UserSettings.get_settings()
        for field in (settings_model_field, 'exam_model'):
            model_id = getattr(settings_obj, f'{field}_id', None)
            if model_id:
                data['model_id'] = model_id
                break
    try:
        cfg = resolve_ai_model(data)
    except ValueError as e:
        _log_ai_call(action=action, practice=practice, model_id='', success=False,
                     error=str(e), prompt=prompt, t0=t0, duration_ms=0)
        return None, str(e)
    model_pk = cfg.get('id')
    # 生成类同样先过统一熔断门禁：熔断中立即快速返回可读错误，不再空等 300 秒。
    # 探活 api_ai_test 属于显式连接测试，刻意不经过此门禁。
    gate_reason = _ai_gate(model_pk)
    if gate_reason:
        err = 'ai_circuit_open：AI 暂不可用（熔断冷却中），已跳过请求'
        _log_ai_call(action=action, practice=practice, model_id=cfg.get('model_id') or '',
                     success=False, error=err, prompt=prompt, t0=t0, duration_ms=0)
        return None, err
    payload = {
        'model': cfg['model_id'],
        'temperature': temperature,
        'messages': [{'role': 'user', 'content': prompt}],
    }
    req = urllib.request.Request(
        resolve_ai_endpoint(cfg['base_url'], cfg['endpoint']),
        data=json.dumps(payload).encode('utf-8'),
        headers=build_ai_headers(cfg['api_key']),
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read()
        # 在 HTTP 请求完成后计算真实耗时（包含网络 + AI 生成时间）
        duration_ms = int((_time.monotonic() - t0) * 1000)
    except urllib.error.HTTPError as e:
        duration_ms = int((_time.monotonic() - t0) * 1000)
        body = e.read().decode('utf-8', 'ignore')
        err = f'AI 接口错误 (HTTP {e.code}): {body[:300]}'
        _log_ai_call(action, practice, cfg['model_id'], False, err, prompt, t0, duration_ms)
        _ai_health_record_failure(model_pk, _ai_category_from_code(e.code), err)
        return None, err
    except (socket.timeout, TimeoutError) as e:
        duration_ms = int((_time.monotonic() - t0) * 1000)
        err = f'AI 请求超时: {e}'
        _log_ai_call(action, practice, cfg['model_id'], False, err, prompt, t0, duration_ms)
        _ai_health_record_failure(model_pk, 'timeout', err)
        return None, err
    except urllib.error.URLError as e:
        duration_ms = int((_time.monotonic() - t0) * 1000)
        err = f'无法连接 AI 服务: {e.reason}'
        _log_ai_call(action, practice, cfg['model_id'], False, err, prompt, t0, duration_ms)
        _ai_health_record_failure(model_pk, 'network', err)
        return None, err
    # 空内容 / 非法 JSON / choices 结构异常一律记失败，不能当成功写入日志与健康状态
    try:
        content = _ai_extract_content(_ai_parse_json_body(raw))
    except _AIRequestError as e:
        _log_ai_call(action, practice, cfg['model_id'], False, e.message, prompt, t0,
                     duration_ms, response=e.message)
        _ai_health_record_failure(model_pk, e.category, e.message)
        return None, e.message
    _log_ai_call(action, practice, cfg['model_id'], True, '', prompt, t0, duration_ms, response=content)
    _ai_health_record_success(model_pk)
    return content, None


def _log_ai_call(action, practice, model_id, success, error, prompt, t0, duration_ms, response=''):
    """写入 AI 调用日志（内部辅助，异常不影响主流程）"""
    try:
        log_kwargs = {
            'practice': practice if isinstance(practice, WritingPractice) else None,
            'translation_practice': practice if isinstance(practice, TranslationPractice) else None,
            'cet6_practice': practice if isinstance(practice, Cet6TranslationPractice) else None,
        }
        AICallLog.objects.create(
            **log_kwargs,
            action=action or 'exam_generate',
            model_id=model_id or '',
            success=bool(success),
            error=(error or '')[:2000],
            duration_ms=duration_ms,
            prompt_preview=(prompt or '')[:500],
            response_preview=(response or '')[:500],
        )
    except Exception:
        pass


def _bind_practice_to_last_log(action, practice):
    """把最近一条未关联练习的指定动作日志绑定到新建的练习记录"""
    try:
        log = (AICallLog.objects
               .filter(action=action, practice__isnull=True,
                       translation_practice__isnull=True,
                       cet6_practice__isnull=True)
               .order_by('-id').first())
        if log:
            if isinstance(practice, TranslationPractice):
                log.translation_practice = practice
                log.save(update_fields=['translation_practice'])
            elif isinstance(practice, Cet6TranslationPractice):
                log.cet6_practice = practice
                log.save(update_fields=['cet6_practice'])
            else:
                log.practice = practice
                log.save(update_fields=['practice'])
    except Exception:
        pass


def exam_workshop(request):
    """考研工坊首页"""
    qs = ExamQuestion.objects.all()
    stats = {
        'total': qs.count(),
        'english1': qs.filter(exam_type='english1').count(),
        'english2': qs.filter(exam_type='english2').count(),
        'essays': qs.filter(question_type__in=['small_essay', 'big_essay']).count(),
        'translations': qs.filter(question_type='translation').count(),
        'practices': WritingPractice.objects.count(),
        'phrases': WritingPhrase.objects.filter(is_collected=True).count(),
    }
    # 最近练习
    recent = WritingPractice.objects.select_related('question').exclude(question__isnull=True).all()[:8]
    return render(request, 'exam_workshop.html', {
        'stats': stats,
        'recent_practices': recent,
        'active_nav': 'workshop',
        'essay_cards': [
            {
                'url': '/workshop/questions/?exam_type=english1&type=small_essay',
                'icon': 'ph-envelope-simple', 'tone': 'red',
                'title': '英语一小作文', 'desc': '书信 / 通知 / 邀请',
            },
            {
                'url': '/workshop/questions/?exam_type=english1&type=big_essay',
                'icon': 'ph-picture', 'tone': 'blue',
                'title': '英语一大作文', 'desc': '图画 / 图表作文',
            },
            {
                'url': '/workshop/questions/?exam_type=english2&type=small_essay',
                'icon': 'ph-pen-nib', 'tone': 'green',
                'title': '英语二小作文', 'desc': '书信 / 通知 / 邀请',
            },
            {
                'url': '/workshop/questions/?exam_type=english2&type=big_essay',
                'icon': 'ph-chart-bar', 'tone': 'amber',
                'title': '英语二大作文', 'desc': '图表作文',
            },
        ],
        'trans_cards': [
            {
                'url': '/workshop/trans/',
                'icon': 'ph-article', 'tone': 'red',
                'title': '长难句训练', 'desc': '英语一真题逐句翻译 · 五维批改',
            },
            {
                'url': '/workshop/questions/?exam_type=english1&type=translation',
                'icon': 'ph-text-aa', 'tone': 'blue',
                'title': '英语一翻译', 'desc': '长难句拆解',
            },
            {
                'url': '/workshop/questions/?exam_type=english2&type=translation',
                'icon': 'ph-translate', 'tone': 'green',
                'title': '英语二翻译', 'desc': '段落翻译',
            },
            {
                'url': '/workshop/questions/?exam_type=cet6&type=translation',
                'icon': 'ph-text-a-underline', 'tone': 'amber',
                'title': '六级翻译', 'desc': '汉译英段落翻译',
            },
        ],
    })


def exam_questions_page(request):
    """真题列表页"""
    exam_type = request.GET.get('exam_type', 'english1')
    qtype = request.GET.get('type', '')
    year = request.GET.get('year', '')
    qs = ExamQuestion.objects.all()
    if exam_type in ('english1', 'english2', 'cet6'):
        qs = qs.filter(exam_type=exam_type)
    if qtype in ('small_essay', 'big_essay', 'translation'):
        qs = qs.filter(question_type=qtype)
    if year:
        qs = qs.filter(year=int(year))
    years = ExamQuestion.objects.values_list('year', flat=True).distinct().order_by('-year')
    # 渲染题目内容（转换图片作文的文本描述）
    questions = []
    for q in qs:
        questions.append({
            'id': q.id,
            'year': q.year,
            'exam_type': q.get_exam_type_display(),
            'question_type': q.get_question_type_display(),
            'genre': q.genre,
            'title': q.title,
            'content': q.content,
            'tags': q.tags,
            'exam_type_code': q.exam_type,
            'qtype_code': q.question_type,
        })
    return render(request, 'exam_questions.html', {
        'questions': questions,
        'years': years,
        'current_exam_type': exam_type,
        'current_type': qtype,
        'current_year': year,
        'type_options': [
            ('small_essay', '小作文'),
            ('big_essay', '大作文'),
            ('translation', '翻译'),
        ],
        'active_nav': 'workshop',
    })


def exam_write_page(request, qid):
    """写作练习页（作文/翻译共用）"""
    q = get_object_or_404(ExamQuestion, id=qid)
    practice = WritingPractice.objects.filter(question=q).select_related('question').first()
    ctx = {
        'question': q,
        'exam_type': q.get_exam_type_display(),
        'question_type': q.get_question_type_display(),
        'active_nav': 'workshop',
    }
    if practice:
        ctx['last_practice'] = practice
        ctx['last_score'] = practice.get_score()
    return render(request, 'exam_write.html', ctx)


def exam_phrases_page(request):
    """好句/错题本"""
    phrases = WritingPhrase.objects.select_related('practice__question').order_by('-created_at')[:100]
    nice = [p for p in phrases if p.phrase_type == 'nice']
    errors = [p for p in phrases if p.phrase_type == 'error']
    replaces = [p for p in phrases if p.phrase_type == 'replace']
    return render(request, 'exam_phrases.html', {
        'nice': nice, 'errors': errors, 'replaces': replaces,
        'active_nav': 'workshop',
    })


def exam_history_page(request):
    """练习历史列表页：展示全部写作/翻译练习记录"""
    qs = WritingPractice.objects.select_related('question').exclude(question__isnull=True).order_by('-created_at')
    exam_type = request.GET.get('exam_type', '')
    qtype = request.GET.get('type', '')
    source = request.GET.get('source', '')
    if exam_type:
        qs = qs.filter(question__exam_type=exam_type)
    if qtype:
        qs = qs.filter(question__question_type=qtype)
    if source:
        qs = qs.filter(source=source)
    total = qs.count()
    practices = qs[:200]
    # 计算均值
    scores = []
    for p in practices:
        s = p.get_score()
        if isinstance(s, dict) and s.get('total'):
            try:
                scores.append(float(s['total']))
            except (TypeError, ValueError):
                pass
    avg = round(sum(scores) / len(scores), 1) if scores else None
    return render(request, 'exam_history.html', {
        'practices': practices,
        'total': total,
        'avg': avg,
        'current_exam_type': exam_type,
        'current_type': qtype,
        'current_source': source,
        'type_options': [
            ('small_essay', '小作文'),
            ('big_essay', '大作文'),
            ('translation', '翻译'),
        ],
        'active_nav': 'workshop',
    })


def exam_logs_page(request):
    """AI 调用日志页"""
    action = request.GET.get('action', '')
    qs = AICallLog.objects.select_related(
        'practice__question', 'translation_practice__sentence',
        'cet6_practice__passage').order_by('-created_at')
    if action:
        qs = qs.filter(action=action)
    qs = qs[:300]
    success_count = AICallLog.objects.filter(success=True).count()
    total_count = AICallLog.objects.count()
    fail_count = total_count - success_count
    return render(request, 'exam_logs.html', {
        'logs': qs,
        'total_count': total_count,
        'success_count': success_count,
        'fail_count': fail_count,
        'action_options': AICallLog.ACTION_CHOICES,
        'current_action': action,
        'active_nav': 'workshop',
    })


@require_http_methods(['GET'])
def api_exam_logs(request):
    """AI 调用日志 API：GET 列表，可按动作筛选"""
    qs = AICallLog.objects.select_related(
        'practice__question', 'translation_practice__sentence',
        'cet6_practice__passage').order_by('-created_at')
    action = request.GET.get('action', '')
    if action:
        qs = qs.filter(action=action)
    limit = int(request.GET.get('limit', 50) or 50)
    logs = qs[:limit]
    data = []
    for log in logs:
        data.append({
            'id': log.id,
            'action': log.action,
            'action_display': log.get_action_display(),
            'model_id': log.model_id,
            'success': log.success,
            'error': log.error,
            'duration_ms': log.duration_ms,
            'prompt_preview': log.prompt_preview,
            'response_preview': log.response_preview,
            'created_at': log.created_at.strftime('%Y-%m-%d %H:%M:%S') if log.created_at else '',
            'question': log.practice.question.year if log.practice and log.practice.question else None,
            'practice_type': ('writing' if log.practice_id else
                              'translation' if log.translation_practice_id else
                              'cet6' if log.cet6_practice_id else None),
            'practice_id': (log.practice_id or log.translation_practice_id
                            or log.cet6_practice_id),
        })
    return JsonResponse({'success': True, 'logs': data})


@require_http_methods(['POST'])
def api_exam_workshop_data(request):
    """工坊数据统计 API"""
    qs = ExamQuestion.objects.all()
    return JsonResponse({
        'success': True,
        'total': qs.count(),
        'english1': qs.filter(exam_type='english1').count(),
        'english2': qs.filter(exam_type='english2').count(),
        'essays': qs.filter(question_type__in=['small_essay', 'big_essay']).count(),
        'translations': qs.filter(question_type='translation').count(),
        'practices': WritingPractice.objects.count(),
        'phrases': WritingPhrase.objects.filter(is_collected=True).count(),
    })


@require_http_methods(['POST'])
def api_exam_generate_essay(request):
    """AI 生成作文（根据词汇画像）"""
    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': '请求格式错误'}, status=400)

    qid = data.get('question_id')
    style = data.get('style', 'standard')
    question = get_object_or_404(ExamQuestion, id=qid)
    profile = build_vocabulary_profile()

    prompt = essay_generation_prompt(question, profile, style)
    content, err = builtin_ai_exam_call(prompt, temperature=0.8, data=data, action='exam_generate')
    if err:
        return JsonResponse({'error': err}, status=502)

    # 保存练习记录
    practice = WritingPractice.objects.create(
        question=question,
        mode='essay',
        source='ai',
        ai_output=content.strip(),
        score_json=json.dumps({'style': style}),
    )
    _bind_practice_to_last_log('exam_generate', practice)
    return JsonResponse({'success': True, 'content': content.strip(), 'practice_id': practice.id})


@require_http_methods(['POST'])
def api_exam_grade(request):
    """AI 批改用户作文"""
    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': '请求格式错误'}, status=400)

    qid = data.get('question_id')
    essay = (data.get('essay') or '').strip()
    if not essay:
        return JsonResponse({'error': '请先输入作文内容'}, status=400)
    question = get_object_or_404(ExamQuestion, id=qid)

    prompt = essay_grading_prompt(question, essay)
    content, err = builtin_ai_exam_call(prompt, temperature=0.3, data=data, action='exam_grade')
    if err:
        return JsonResponse({'error': err}, status=502)

    # 解析 JSON
    score_data = extract_json_object(content)
    if not score_data:
        score_data = {'raw': content}

    # 保存练习记录并沉淀好句/错误
    practice = WritingPractice.objects.create(
        question=question,
        mode='essay',
        source='manual',
        user_input=essay,
        ai_output=content,
        score_json=json.dumps(score_data, ensure_ascii=False),
        feedback=score_data.get('comments', ''),
    )
    _bind_practice_to_last_log('exam_grade', practice)

    # 沉淀错误点
    for e in (score_data.get('errors') or [])[:5]:
        if e.get('original') and e.get('corrected'):
            WritingPhrase.objects.create(
                practice=practice,
                phrase=f'{e["original"]} → {e["corrected"]}',
                meaning=e.get('reason', ''),
                phrase_type='error',
            )
    # 沉淀高级替换
    for i, e in enumerate((score_data.get('advanced_expressions') or [])[:8]):
        if e.get('advanced'):
            WritingPhrase.objects.create(
                practice=practice,
                phrase=f'{e.get("advanced", "")}（替换: {e.get("original", "")}）',
                meaning=e.get('explain', ''),
                phrase_type='replace',
            )

    return JsonResponse({'success': True, 'result': score_data, 'practice_id': practice.id})


@require_http_methods(['POST'])
def api_exam_translate_analyze(request):
    """翻译真题句子解析"""
    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': '请求格式错误'}, status=400)

    qid = data.get('question_id')
    sentence = (data.get('sentence') or '').strip()
    if not sentence:
        return JsonResponse({'error': '请输入要解析的句子'}, status=400)
    question = get_object_or_404(ExamQuestion, id=qid)

    profile = build_vocabulary_profile()
    prompt = translation_help_prompt(question, sentence, profile)
    content, err = builtin_ai_exam_call(prompt, temperature=0.6, data=data, action='exam_translate_analyze')
    if err:
        return JsonResponse({'error': err}, status=502)
    return JsonResponse({'success': True, 'content': content.strip()})


@require_http_methods(['POST'])
def api_exam_grade_translation(request):
    """AI 批改用户译文"""
    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': '请求格式错误'}, status=400)

    qid = data.get('question_id')
    user_trans = (data.get('translation') or '').strip()
    if not user_trans:
        return JsonResponse({'error': '请先输入你的译文'}, status=400)
    question = get_object_or_404(ExamQuestion, id=qid)

    prompt = translation_grading_prompt(question, user_trans)
    content, err = builtin_ai_exam_call(prompt, temperature=0.3, data=data, action='exam_grade_translation')
    if err:
        return JsonResponse({'error': err}, status=502)

    score_data = extract_json_object(content)
    if not score_data:
        score_data = {'raw': content}

    practice = WritingPractice.objects.create(
        question=question,
        mode='translation',
        source='manual',
        user_input=user_trans,
        ai_output=content,
        score_json=json.dumps(score_data, ensure_ascii=False),
        feedback=score_data.get('comments', ''),
    )
    _bind_practice_to_last_log('exam_grade_translation', practice)
    return JsonResponse({'success': True, 'result': score_data, 'practice_id': practice.id})


@require_http_methods(['POST'])
def api_cet6_translate_analyze(request):
    """六级段落翻译（汉译英）解析：分析中文段落，给出词汇、句式、参考译文"""
    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': '请求格式错误'}, status=400)

    qid = data.get('question_id')
    paragraph = (data.get('paragraph') or '').strip()
    if not paragraph:
        return JsonResponse({'error': '请输入要翻译的中文段落'}, status=400)
    question = get_object_or_404(ExamQuestion, id=qid)

    profile = build_vocabulary_profile()
    prompt = cet6_translation_help_prompt(question, paragraph, profile)
    content, err = builtin_ai_exam_call(prompt, temperature=0.6, data=data,
                                        action='cet6_translate_analyze',
                                        settings_model_field='cet6_translation_model')
    if err:
        return JsonResponse({'error': err}, status=502)
    return JsonResponse({'success': True, 'content': content.strip()})


@require_http_methods(['POST'])
def api_cet6_grade_translation(request):
    """六级段落翻译（汉译英）AI 批改：批改用户英文译文"""
    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': '请求格式错误'}, status=400)

    qid = data.get('question_id')
    user_trans = (data.get('translation') or '').strip()
    if not user_trans:
        return JsonResponse({'error': '请先输入你的英文译文'}, status=400)
    question = get_object_or_404(ExamQuestion, id=qid)

    reference = (question.model_answer or '').strip() or None
    prompt = cet6_translation_grading_prompt(question, user_trans, reference=reference)
    content, err = builtin_ai_exam_call(prompt, temperature=0.3, data=data,
                                        action='cet6_grade_translation',
                                        settings_model_field='cet6_translation_model')
    if err:
        return JsonResponse({'error': err}, status=502)

    score_data = extract_json_object(content)
    if not score_data:
        score_data = {'raw': content}

    practice = WritingPractice.objects.create(
        question=question,
        mode='translation',
        source='manual',
        user_input=user_trans,
        ai_output=content,
        score_json=json.dumps(score_data, ensure_ascii=False),
        feedback=score_data.get('comments', ''),
    )
    _bind_practice_to_last_log('cet6_grade_translation', practice)

    for e in (score_data.get('errors') or [])[:5]:
        if e.get('original') and e.get('corrected'):
            WritingPhrase.objects.create(
                practice=practice,
                phrase=f'{e["original"]} → {e["corrected"]}',
                meaning=e.get('reason', ''),
                phrase_type='error',
            )

    return JsonResponse({'success': True, 'result': score_data, 'practice_id': practice.id})


@require_http_methods(['POST'])
def api_exam_themes_report(request):
    """历年真题主题规律分析（AI 报告）"""
    try:
        data = json.loads(request.body)
    except Exception:
        data = {}
    qs = ExamQuestion.objects.filter(question_type__in=['small_essay', 'big_essay'])
    if data.get('exam_type'):
        qs = qs.filter(exam_type=data['exam_type'])
    rows = []
    for q in qs.order_by('-year')[:60]:
        rows.append(f'{q.year}年 {q.get_exam_type_display()} {q.get_question_type_display()}'
                    f'{(" - " + q.genre) if q.genre else ""}: {q.content}')
    if not rows:
        return JsonResponse({'error': '暂无真题数据'}, status=400)
    prompt = themes_report_prompt('\n'.join(rows))
    content, err = builtin_ai_exam_call(prompt, temperature=0.5, data=data, action='exam_themes_report')
    if err:
        return JsonResponse({'error': err}, status=502)
    return JsonResponse({'success': True, 'content': content.strip()})


@require_http_methods(['POST'])
def api_exam_personal_template(request):
    """生成用户专属作文模板：结合词汇画像 + 历史练习记录（得分、错误点、优秀表达）"""
    try:
        data = json.loads(request.body)
    except Exception:
        data = {}

    profile = build_vocabulary_profile()

    # 收集历史练习摘要（排除模板记录：question=None）
    recents = WritingPractice.objects.select_related('question').exclude(question__isnull=True).order_by('-created_at')[:10]
    lines = []
    scores = []
    for p in recents:
        s = p.get_score()
        total = s.get('total') if isinstance(s, dict) else None
        if total:
            try:
                scores.append(float(total))
            except (TypeError, ValueError):
                pass
        lines.append(
            f"- {p.created_at.strftime('%Y-%m-%d')} "
            f"{p.question.get_exam_type_display()}（{p.question.year}）"
            f"{p.question.get_question_type_display()}（{'AI' if p.source == 'ai' else '手动'}）"
            f"{('得分 ' + str(total)) if total else ''}"
        )
    practice_part = ['最近练习记录：'] + (lines or ['（暂无练习记录）'])

    if scores:
        avg = round(sum(scores) / len(scores), 1)
        practice_part.append(f'批改平均得分：{avg} 分（最高分参考）。')

    # 常犯错误 + 优秀表达（从沉淀的好句本取）
    errors = WritingPhrase.objects.filter(phrase_type='error').order_by('-created_at')[:8]
    replaces = WritingPhrase.objects.filter(phrase_type='replace').order_by('-created_at')[:8]
    if errors:
        practice_part.append('历史批改中常犯错误：')
        for e in errors:
            practice_part.append(f'- {e.phrase}{("（" + e.meaning + "）") if e.meaning else ""}')
    if replaces:
        practice_part.append('历史批改推荐的高级表达：')
        for r in replaces:
            practice_part.append(f'- {r.phrase}{("（" + r.meaning + "）") if r.meaning else ""}')
    if not errors and not replaces and not lines:
        practice_part.append('暂无历史批改数据，请先尝试「AI 代写」或「自己写 → AI 批改」，模板将更精准。')

    practice_summary = '\n'.join(practice_part)
    prompt = personal_template_prompt(profile, practice_summary)
    content, err = builtin_ai_exam_call(prompt, temperature=0.6, data=data, action='exam_template')
    if err:
        return JsonResponse({'error': err}, status=502)

    # 保存模板为一条练习记录（source=tmpl，便于复现）
    practice = WritingPractice.objects.create(
        mode='essay',
        source='template',
        ai_output=content.strip(),
    )
    _bind_practice_to_last_log('exam_template', practice)
    return JsonResponse({'success': True, 'content': content.strip(), 'practice_id': practice.id,
                         'has_practice': bool(recents), 'practice_count': len(recents)})


@require_http_methods(['POST'])
def api_exam_export_pdf(request):
    """导出专属作文模板为 PDF：接收 {title, content}，将 AI 生成的 Markdown 排版为可下载的 PDF"""
    import re as _re
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                     TableStyle, Flowable)
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT, TA_CENTER
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': '请求格式错误'}, status=400)

    title = (data.get('title') or '我的专属作文模板').strip()
    content = data.get('content') or ''
    if not content.strip():
        return JsonResponse({'error': '没有可导出的内容'}, status=400)

    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    re = _re  # 统一使用别名模块，避免重复 import
    RED = colors.HexColor('#8b0000')
    LIGHT_BG = colors.HexColor('#faf5f5')
    BORDER = colors.HexColor('#e5d7d7')

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            topMargin=18 * mm, bottomMargin=18 * mm,
                            leftMargin=16 * mm, rightMargin=16 * mm,
                            title=title, author='考研英语学习平台')

    # ========== 样式定义 ==========
    s_title = ParagraphStyle('t', fontName='STSong-Light', fontSize=18,
                              leading=26, alignment=TA_CENTER, spaceAfter=4,
                              textColor=RED, fontName_CJK='STSong-Light')
    s_meta = ParagraphStyle('m', fontName='STSong-Light', fontSize=8.5,
                             leading=12, alignment=TA_CENTER, textColor=colors.grey,
                             spaceAfter=2)
    s_h1 = ParagraphStyle('h1', fontName='STSong-Light', fontSize=14, leading=20,
                           spaceBefore=12, spaceAfter=6, textColor=RED)
    s_h2 = ParagraphStyle('h2', fontName='STSong-Light', fontSize=12, leading=18,
                           spaceBefore=10, spaceAfter=5, textColor=RED,
                           borderPadding=(0, 0, 4, 0))
    s_h3 = ParagraphStyle('h3', fontName='STSong-Light', fontSize=10.5, leading=16,
                           spaceBefore=8, spaceAfter=3, textColor=colors.HexColor('#551a1a'))
    s_h4 = ParagraphStyle('h4', fontName='STSong-Light', fontSize=10, leading=15,
                           spaceBefore=6, spaceAfter=2, textColor=colors.HexColor('#666'))
    s_body = ParagraphStyle('b', fontName='STSong-Light', fontSize=9.5, leading=16,
                             spaceAfter=4)
    s_ul = ParagraphStyle('ul', parent=s_body, leftIndent=14, bulletIndent=2,
                           spaceAfter=2)
    s_ol = ParagraphStyle('ol', parent=s_body, leftIndent=16, bulletIndent=2,
                           spaceAfter=2)
    s_code = ParagraphStyle('code', fontName='STSong-Light', fontSize=9, leading=14,
                             backColor=LIGHT_BG, borderPadding=(3, 6, 3, 6),
                             borderColor=BORDER, borderWidth=0.5, textColor=RED,
                             spaceBefore=2, spaceAfter=2)
    s_quote = ParagraphStyle('q', parent=s_body, leftIndent=18, spaceBefore=4,
                              spaceAfter=4, textColor=colors.HexColor('#555'),
                              fontSize=9.3)
    s_th = ParagraphStyle('th', fontName='STSong-Light', fontSize=9.3, leading=14,
                           textColor=colors.white, fontName_CJK='STSong-Light')
    s_td = ParagraphStyle('td', fontName='STSong-Light', fontSize=9, leading=14)

    # ========== 工具函数 ==========
    def esc(t):
        return (t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                .replace('"', '&quot;'))

    def inline(txt):
        """转换 Markdown 内联标记为 ReportLab Paragraph 支持的 HTML"""
        t = esc(txt)
        # **bold** → <b>
        while '**' in t:
            a = t.find('**')
            b = t.find('**', a + 2)
            if b == -1:
                break
            t = t[:a] + '<b>' + t[a + 2:b] + '</b>' + t[b + 2:]
        # *italic* → <i> (avoid matching **)
        t = re.sub(r'(^|[^*])\*([^*\n]+)\*(?!\*)', r'\1<i>\2</i>', t)
        # `code` → red monospace font
        def _code(m):
            return ('<font name="Courier" color="#8b0000" size="8.5">%s</font>'
                    % m.group(1))
        t = re.sub(r'`([^`]+)`', _code, t)
        # ~~strike~~ (just in case)
        t = re.sub(r'~~([^~]+)~~', r'<strike>\1</strike>', t)
        return t

    def hr_flowable():
        """水平线：用 1x1 Table 模拟"""
        t = Table([['']],colWidths=[doc.width], rowHeights=[0.5])
        t.setStyle(TableStyle([
            ('LINEBELOW', (0, 0), (-1, -1), 0.5, BORDER),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ]))
        return t

    def quote_flowable(text_lines):
        """引用块：左侧红色竖条 + 缩进 + 浅底"""
        inner = '<br/>'.join(inline(l) for l in text_lines)
        # 用两列表格模拟：左列是红色竖条(宽度小)，右列是文字
        quote_text = Paragraph(inner, s_quote)
        tbl = Table([['', quote_text]], colWidths=[2.5 * mm, doc.width - 2.5 * mm - 8 * mm])
        tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, 0), RED),
            ('BACKGROUND', (1, 0), (1, 0), LIGHT_BG),
            ('LEFTPADDING', (0, 0), (0, 0), 0),
            ('RIGHTPADDING', (0, 0), (0, 0), 0),
            ('TOPPADDING', (0, 0), (0, 0), 8),
            ('BOTTOMPADDING', (0, 0), (0, 0), 8),
            ('LEFTPADDING', (1, 0), (1, 0), 8),
            ('RIGHTPADDING', (1, 0), (1, 0), 8),
            ('TOPPADDING', (1, 0), (1, 0), 6),
            ('BOTTOMPADDING', (1, 0), (1, 0), 6),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('ROUNDEDCORNERS', [1, 1, 1, 1]),
        ]))
        return tbl

    def is_pipe_row(s):
        return s.count('|') >= 2 and '|' in s

    def is_sep_row(s):
        stripped = s.strip().lstrip('|').rstrip('|').strip()
        if not stripped:
            return False
        parts = [p.strip() for p in stripped.split('|')]
        return len(parts) >= 2 and all(
            re.match(r'^:?-{3,}:?$', p) for p in parts if p != ''
        )

    def split_row(s):
        t = s.strip()
        if t.startswith('|'):
            t = t[1:]
        if t.endswith('|'):
            t = t[:-1]
        return [c.strip() for c in t.split('|')]

    def render_table(header, body_rows):
        """渲染 Markdown 表格为 ReportLab Table"""
        # 计算列宽：平均分配 doc.width
        col_count = len(header)
        col_w = doc.width / col_count
        # 确保最后一列不会溢出
        col_widths = [doc.width / col_count] * col_count
        # 表头
        th_cells = [Paragraph(inline(h), s_th) for h in header]
        data = [th_cells]
        for row in body_rows:
            padded = row + [''] * (col_count - len(row))
            data.append([Paragraph(inline(c), s_td) for c in padded[:col_count]])
        tbl = Table(data, colWidths=col_widths, repeatRows=1)
        style_cmds = [
            # 表头样式
            ('BACKGROUND', (0, 0), (-1, 0), RED),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'STSong-Light'),
            ('FONTSIZE', (0, 0), (-1, 0), 9.3),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 7),
            ('TOPPADDING', (0, 0), (-1, 0), 7),
            # 单元格内边距
            ('LEFTPADDING', (0, 0), (-1, -1), 7),
            ('RIGHTPADDING', (0, 0), (-1, -1), 7),
            ('TOPPADDING', (0, 1), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 1), (-1, -1), 5),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            # 边框
            ('GRID', (0, 0), (-1, -1), 0.4, BORDER),
        ]
        # 斑马纹
        for i in range(1, len(data)):
            if i % 2 == 0:
                style_cmds.append(('BACKGROUND', (0, i), (-1, i), LIGHT_BG))
        tbl.setStyle(TableStyle(style_cmds))
        return tbl

    # ========== 开始构建 PDF ==========
    elements = [
        Paragraph(inline(title), s_title),
        Paragraph('考研英语学习平台 · 专属作文模板 · 生成于 %s' %
                  timezone.localdate().strftime('%Y年%m月%d日'), s_meta),
        hr_flowable(),
        Spacer(1, 3 * mm),
    ]

    lines = content.split('\n')
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i].rstrip()
        s = raw.strip()
        i += 1

        if not s:
            continue

        # 标题
        m = re.match(r'^(#{1,4})\s+(.*)$', s)
        if m:
            level = len(m.group(1))
            txt = m.group(2)
            if level == 1:
                p = Paragraph(inline(txt), s_h1)
                elements.append(p)
                # H1 下面加一条红色分割线
                elements.append(hr_flowable())
            elif level == 2:
                elements.append(Paragraph(inline(txt), s_h2))
            elif level == 3:
                elements.append(Paragraph(inline(txt), s_h3))
            else:
                elements.append(Paragraph(inline(txt), s_h4))
            continue

        # HR --- 或 ***
        if re.match(r'^\s*(-{3,}|\*{3,})\s*$', s):
            elements.append(Spacer(1, 2 * mm))
            elements.append(hr_flowable())
            elements.append(Spacer(1, 2 * mm))
            continue

        # Blockquote > (连续多行)
        if re.match(r'^\s*>\s?', s):
            q_lines = []
            while True:
                q_lines.append(re.sub(r'^\s*>\s?', '', s))
                if i < n and re.match(r'^\s*>\s?', lines[i].strip()):
                    s = lines[i].strip()
                    i += 1
                else:
                    break
            elements.append(quote_flowable(q_lines))
            continue

        # 代码块 ``` (仅简单支持，把它当 code 样式段落)
        if re.match(r'^\s*```', s):
            buf_code = []
            while i < n and not re.match(r'^\s*```', lines[i]):
                buf_code.append(lines[i].rstrip())
                i += 1
            if i < n:
                i += 1  # skip closing ```
            code_text = '<br/>'.join(esc(c) for c in buf_code)
            p_code = Paragraph(code_text, ParagraphStyle(
                'blkcode', parent=s_code, fontName='Courier', textColor=colors.black))
            elements.append(p_code)
            continue

        # 无序列表 - 或 *
        if re.match(r'^\s*[-*+]\s+', s):
            items = []
            while True:
                items.append(re.sub(r'^\s*[-*+]\s+', '', s))
                if i < n and re.match(r'^\s*[-*+]\s+', lines[i].strip()):
                    s = lines[i].strip()
                    i += 1
                else:
                    break
            for it in items:
                elements.append(Paragraph('•  ' + inline(it), s_ul))
            continue

        # 有序列表 1. 2. 或 1) 2)
        if re.match(r'^\s*\d+[.)]\s+', s):
            items = []
            start_num = int(re.match(r'^\s*(\d+)[.)]\s+', s).group(1))
            while True:
                items.append(re.sub(r'^\s*\d+[.)]\s+', '', s))
                if i < n and re.match(r'^\s*\d+[.)]\s+', lines[i].strip()):
                    s = lines[i].strip()
                    i += 1
                else:
                    break
            for idx, it in enumerate(items):
                num = idx + start_num
                elements.append(Paragraph('%d.  %s' % (num, inline(it)), s_ol))
            continue

        # Markdown 表格：如果当前行像表头，且下一行是分隔符行
        # 注意：因为已经 i += 1 过，所以当前 lines[i-1] 是本行，检查下一行 lines[i]
        if is_pipe_row(s) and i < n and is_sep_row(lines[i].strip()):
            header = split_row(s)
            i += 1  # 跳过分隔行
            body_rows = []
            while i < n and is_pipe_row(lines[i].strip()):
                body_rows.append(split_row(lines[i].strip()))
                i += 1
            if header and len(header) >= 2:
                elements.append(Spacer(1, 1.5 * mm))
                elements.append(render_table(header, body_rows))
                elements.append(Spacer(1, 1.5 * mm))
            continue

        # 独立加粗段（整行 **...**）
        if s.startswith('**') and s.endswith('**') and s.count('**') == 2:
            elements.append(Paragraph('<b>' + inline(s[2:-2]) + '</b>',
                                      ParagraphStyle('strongline', parent=s_body,
                                                     spaceBefore=4, textColor=RED)))
            continue

        # 普通段落
        elements.append(Paragraph(inline(s), s_body))

    doc.build(elements)
    buf.seek(0)
    return FileResponse(buf, content_type='application/pdf',
                        filename='我的专属作文模板_%s.pdf' % timezone.localdate().strftime('%Y%m%d'),
                        as_attachment=True)


@require_http_methods(['POST'])
def api_exam_phrase_toggle(request):
    """好句收藏/取消"""
    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': '请求格式错误'}, status=400)
    pid = data.get('phrase_id')
    try:
        phrase = WritingPhrase.objects.get(id=int(pid))
    except (WritingPhrase.DoesNotExist, TypeError, ValueError):
        return JsonResponse({'error': '句子不存在'}, status=404)
    phrase.is_collected = not phrase.is_collected
    phrase.save()
    return JsonResponse({'success': True, 'is_collected': phrase.is_collected})


def extract_json_object(text):
    """从 AI 输出中提取 JSON 对象（兼容 ```json 代码块包裹）"""
    if not text:
        return {}
    text = text.strip()
    # 去掉 ```json ... ``` 代码块
    m = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
    if m:
        text = m.group(1).strip()
    # 提取最外层大括号
    start = text.find('{')
    if start == -1:
        return {}
    depth = 0
    for i in range(start, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


# ─── 长难句翻译训练（英语一逐句）─────────────────────────────────

# 批改错误类型 → error_tags 五类代码
ISSUE_TYPE_TO_CODE = {
    '主干': 'main', '从句': 'subordinate', '词义': 'meaning',
    '逻辑': 'logic', '中文表达': 'expression',
}
ERROR_CODE_NAMES = {
    'main': '主干', 'subordinate': '从句', 'meaning': '词义',
    'logic': '逻辑', 'expression': '中文表达',
}
TRANS_GRADE_PASS = 40      # 总分（满分 50）达到此值视为掌握
TRANS_REVIEW_INTERVAL_OK = 7   # 掌握后下次复习间隔（天）
TRANS_REVIEW_INTERVAL_ERR = 1  # 未掌握下次复习间隔（天）


def _request_user(request):
    return request.user if getattr(request, 'user', None) and request.user.is_authenticated else None


def _parse_practice_total(practice):
    """解析练习记录总分（int/float），失败返回 None"""
    score = practice.get_score()
    total = score.get('total')
    return total if isinstance(total, (int, float)) else None


def _is_error_practice(practice):
    """是否错句：最近一次总分 < 40 或带错因标签"""
    total = _parse_practice_total(practice)
    if total is None:
        return bool(practice.error_tags)
    return total < TRANS_GRADE_PASS or bool(practice.error_tags)


def _latest_practice_map():
    """{sentence_id: 最近一次 TranslationPractice}"""
    latest = {}
    for p in (TranslationPractice.objects
              .only('id', 'sentence_id', 'score_json', 'error_tags',
                    'next_review_date', 'consecutive_correct')
              .order_by('created_at')):
        latest[p.sentence_id] = p
    return latest


def trans_home(request):
    """长难句翻译训练首页（句库列表只列真题；AI 题走"AI 专项组卷"入口）"""
    year = request.GET.get('year', '')
    try:
        year_int = int(year) if year else None
    except (TypeError, ValueError):
        year_int = None
    sentences = TranslationSentence.objects.select_related('question').filter(
        source_type='past_paper')
    years = list(ExamQuestion.objects.filter(
        exam_type='english1', question_type='translation')
        .values_list('year', flat=True).distinct().order_by('-year'))
    if year_int in years:
        sentences = sentences.filter(question__year=year_int)
    latest = _latest_practice_map()
    fav_ids = set(TranslationFavorite.objects.filter(
        user=_request_user(request)).values_list('sentence_id', flat=True))

    items = []
    for s in sentences.order_by('-question__year', 'order'):
        p = latest.get(s.id)
        total = _parse_practice_total(p) if p else None
        items.append({
            'id': s.id, 'num': s.order + 45, 'year': s.question.year,
            'difficulty': s.difficulty, 'syntax_tags': s.syntax_tags or [],
            'is_favorite': s.id in fav_ids,
            'status': ('new' if p is None else ('error' if _is_error_practice(p) else 'ok')),
            'last_total': int(total) if total is not None else None,
            'review_date': p.next_review_date.strftime('%m-%d') if (p and p.next_review_date) else '',
        })
    total_count = TranslationSentence.objects.filter(source_type='past_paper').count()
    practiced_count = TranslationPractice.objects.values('sentence_id').distinct().count()
    error_count = sum(1 for p in latest.values() if _is_error_practice(p))
    fav_count = TranslationFavorite.objects.filter(user=_request_user(request)).count()
    paper_count = GeneratedPaper.objects.filter(status='ready').count()
    return render(request, 'trans_home.html', {
        'items': items,
        'years': years,
        'cur_year': year_int,
        'total_count': total_count,
        'practiced_count': practiced_count,
        'error_count': error_count,
        'fav_count': fav_count,
        'paper_count': paper_count,
        'active_nav': 'workshop',
        'ws_active': 'trans',
    })


def api_trans_today(request):
    """今日推荐 3-5 句：到期错句（含 AI 题）优先，再补未练真题，不足随机补已练真题"""
    today = timezone.localdate()
    latest = _latest_practice_map()
    due_ids, fresh_ids, rest_ids = [], [], []
    for s in TranslationSentence.objects.select_related('question').order_by(
            '-question__year', 'order'):
        p = latest.get(s.id)
        if p is None:
            if s.source_type == 'past_paper':
                fresh_ids.append(s.id)
        elif _is_error_practice(p) and (p.next_review_date or today) <= today:
            due_ids.append(s.id)
        elif s.source_type == 'past_paper':
            rest_ids.append(s.id)
    picked = due_ids[:3]
    picked += fresh_ids[:5 - len(picked)]
    if len(picked) < 3:
        pool = rest_ids[:]
        random.shuffle(pool)
        picked += pool[:3 - len(picked)]
    picked = picked[:5]
    order_map = {sid: i for i, sid in enumerate(picked)}
    out = []
    for s in TranslationSentence.objects.filter(id__in=picked).select_related('question'):
        is_ai = s.source_type == 'ai_generated'
        out.append({
            'id': s.id, 'order': s.order,
            'num': s.order if is_ai else s.order + 45,
            'year': (s.question.year if s.question_id else None),
            'difficulty': s.difficulty,
            'is_ai': is_ai,
        })
    out.sort(key=lambda x: order_map[x['id']])
    return JsonResponse({'success': True, 'sentences': out})


def trans_practice_page(request, sid):
    """逐句翻译练习页（真题句与 AI 训练题共用）"""
    s = get_object_or_404(
        TranslationSentence.objects.select_related('question'), id=sid)
    is_ai = s.source_type == 'ai_generated'
    is_fav = TranslationFavorite.objects.filter(
        user=_request_user(request), sentence=s).exists()
    last_practice = s.practices.order_by('-created_at').first()
    gq = getattr(s, 'generated_question', None)
    paper = gq.paper if gq else None
    next_s = None
    if is_ai and gq:
        # AI 题：卷内下一句
        next_gq = (GeneratedQuestion.objects
                   .filter(paper=gq.paper, order__gt=gq.order)
                   .select_related('sentence').order_by('order').first())
        if next_gq:
            next_s = next_gq.sentence
    elif not is_ai:
        # 真题：同年下一句 → 更早年份的第一句（列表按年份倒序排列）
        next_s = TranslationSentence.objects.filter(
            question=s.question, order__gt=s.order).order_by('order').first()
        if next_s is None:
            next_q = ExamQuestion.objects.filter(
                exam_type='english1', question_type='translation',
                year__lt=s.question.year).order_by('-year').first()
            if next_q:
                next_s = TranslationSentence.objects.filter(
                    question=next_q).order_by('order').first()
    return render(request, 'trans_practice.html', {
        'sentence': s,
        'num': s.order if is_ai else s.order + 45,
        'is_ai': is_ai,
        'paper': paper,
        'gq': gq,
        'syntax_tag_names': [TRANS_SYNTAX_TAG_NAMES.get(t, t) for t in (s.syntax_tags or [])],
        'is_fav': is_fav,
        'last_practice': last_practice,
        'next_s': next_s,
        'pass_score': TRANS_GRADE_PASS,
        'active_nav': 'workshop',
        'ws_active': 'trans',
    })


@require_http_methods(['POST'])
def api_trans_submit(request, sid):
    """提交译文：保存练习 → AI 五维批改 → 复习排期（AI 不可用时离线兜底）"""
    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': '请求格式错误'}, status=400)
    user_trans = (data.get('translation') or '').strip()
    if not user_trans:
        return JsonResponse({'error': '请先输入你的译文'}, status=400)
    s = get_object_or_404(
        TranslationSentence.objects.select_related('question'), id=sid)
    try:
        duration = int(data.get('duration_seconds') or 0)
    except (TypeError, ValueError):
        duration = 0
    practice = TranslationPractice.objects.create(
        user=_request_user(request),
        sentence=s,
        user_translation=user_trans,
        duration_seconds=max(0, min(duration, 7200)),
    )

    content, err = builtin_ai_exam_call(
        trans_grading_prompt_v2(s.sentence_en, user_trans, s.sentence_zh or None),
        temperature=0.3, data=data, action='trans_grade', practice=practice)
    if err:
        # 无 AI 兜底：练习照存，返回对照与字面相似度
        practice.score_json = json.dumps({'ungraded': True}, ensure_ascii=False)
        practice.save(update_fields=['score_json'])
        similarity = 0
        if s.sentence_zh:
            similarity = round(difflib.SequenceMatcher(
                None, user_trans, s.sentence_zh).ratio() * 100)
        return JsonResponse({
            'success': True, 'graded': False, 'error': err,
            'practice_id': practice.id, 'reference': s.sentence_zh,
            'similarity': similarity,
        })

    score_data = extract_json_object(content)
    score_data, validation_error = _validate_translation_grade_result(
        score_data,
        ('structure', 'clause', 'lexicon', 'logic', 'expression'))
    if validation_error:
        practice.score_json = json.dumps(
            {'ungraded': True, 'validation_error': validation_error,
             'raw': (content or '')[:2000]}, ensure_ascii=False)
        practice.save(update_fields=['score_json'])
        return JsonResponse({
            'success': True, 'graded': False,
            'error': 'AI 返回格式异常，本次已保存但未批改，可稍后重练',
            'practice_id': practice.id, 'reference': s.sentence_zh,
        })

    # 错因标签（去重）
    codes = []
    for e in score_data.get('errors') or []:
        if not isinstance(e, dict):
            continue
        code = ISSUE_TYPE_TO_CODE.get((e.get('issue_type') or '').strip())
        if code and code not in codes:
            codes.append(code)
    total = score_data.get('total')
    total = int(total) if isinstance(total, (int, float)) else 0

    # 复习排期（SRS）：掌握 +7 天连对 +1；未掌握明天再练、连对清零
    prev = s.practices.exclude(id=practice.id).order_by('-created_at').first()
    if total >= TRANS_GRADE_PASS:
        practice.consecutive_correct = (prev.consecutive_correct + 1) if prev else 1
        practice.next_review_date = timezone.localdate() + timedelta(days=TRANS_REVIEW_INTERVAL_OK)
    else:
        practice.consecutive_correct = 0
        practice.next_review_date = timezone.localdate() + timedelta(days=TRANS_REVIEW_INTERVAL_ERR)
    practice.score_json = json.dumps(score_data, ensure_ascii=False)
    practice.feedback = score_data.get('comments', '')
    practice.error_tags = codes
    practice.save()
    return JsonResponse({
        'success': True, 'graded': True, 'result': score_data,
        'practice_id': practice.id, 'reference': s.sentence_zh,
        'next_review_date': practice.next_review_date.strftime('%Y-%m-%d'),
        'consecutive_correct': practice.consecutive_correct,
    })


@require_http_methods(['POST'])
def api_trans_hint(request, sid):
    """AI 解析：语法拆解 + 词汇点拨 + 参考译文（Markdown）"""
    try:
        data = json.loads(request.body)
    except Exception:
        data = {}
    s = get_object_or_404(
        TranslationSentence.objects.select_related('question'), id=sid)
    q = s.question if s.question_id else _TransLiteQuestion()
    prompt = translation_help_prompt(q, s.sentence_en, build_vocabulary_profile())
    content, err = builtin_ai_exam_call(prompt, temperature=0.6, data=data, action='trans_hint')
    if err:
        return JsonResponse({'error': err}, status=502)
    return JsonResponse({'success': True, 'content': content.strip()})


@require_http_methods(['POST'])
def api_trans_fav(request, sid):
    """收藏/取消收藏句子"""
    s = get_object_or_404(TranslationSentence, id=sid)
    user = _request_user(request)
    fav, created = TranslationFavorite.objects.get_or_create(user=user, sentence=s)
    if not created:
        fav.delete()
    return JsonResponse({'success': True, 'is_favorite': created})


def trans_history_page(request):
    """练习历史与学习报告（本期：统计 + 年份表 + 记录 + 错句清单）"""
    only_errors = request.GET.get('filter') == 'error'
    latest = _latest_practice_map()
    all_practices = TranslationPractice.objects.select_related('sentence__question')

    # 总体统计
    total_practices = all_practices.count()
    totals = [t for t in (_parse_practice_total(p) for p in latest.values()) if t is not None]
    avg_total = round(sum(totals) / len(totals), 1) if totals else None
    error_count = sum(1 for p in latest.values() if _is_error_practice(p))
    error_sentences = []

    # 错句清单 + 按年份聚合（AI 题无年份，单独计数不进年份表）
    year_stats = {}
    ai_error_count = 0
    for s in TranslationSentence.objects.select_related('question'):
        p = latest.get(s.id)
        if p is None:
            continue
        if s.question_id:
            year_stats.setdefault(s.question.year, []).append(_parse_practice_total(p))
        if _is_error_practice(p):
            if not s.question_id:
                ai_error_count += 1
            error_sentences.append({
                'id': s.id,
                'num': s.order if not s.question_id else s.order + 45,
                'year': s.question.year if s.question_id else None,
                'is_ai': s.source_type == 'ai_generated',
                'sentence_en': s.sentence_en[:80],
                'last_total': int(t) if (t := _parse_practice_total(p)) is not None else None,
                'error_tags': [ERROR_CODE_NAMES.get(c, c) for c in (p.error_tags or [])],
                'review_date': p.next_review_date.strftime('%Y-%m-%d') if p.next_review_date else '',
            })
    error_sentences.sort(key=lambda x: (x['review_date'] or '9999', -(x['year'] or 0)))
    year_rows = []
    for year in sorted(year_stats, reverse=True):
        vals = [v for v in year_stats[year] if v is not None]
        year_rows.append({
            'year': year,
            'practiced': len(year_stats[year]),
            'avg': round(sum(vals) / len(vals), 1) if vals else None,
        })
    recent = all_practices.order_by('-created_at')[:50]
    error_sids = {e['id'] for e in error_sentences}
    recent_items = [{
        'id': p.id,
        'sid': p.sentence_id,
        'year': p.sentence.question.year if p.sentence.question_id else None,
        'num': p.sentence.order if not p.sentence.question_id else p.sentence.order + 45,
        'is_ai': p.sentence.source_type == 'ai_generated',
        'snippet': p.sentence.sentence_en[:60],
        'total': (int(t) if (t := _parse_practice_total(p)) is not None else None),
        'ungraded': bool(p.get_score().get('ungraded')),
        'duration_fmt': (
            f'{p.duration_seconds // 60}分{p.duration_seconds % 60:02d}秒'
            if p.duration_seconds >= 60
            else (f'{p.duration_seconds}秒' if p.duration_seconds else '')),
        'created_at': p.created_at.strftime('%m-%d %H:%M'),
    } for p in recent]
    if only_errors:
        recent_items = [it for it in recent_items if it['sid'] in error_sids]
    return render(request, 'trans_history.html', {
        'total_practices': total_practices,
        'avg_total': avg_total,
        'error_count': error_count,
        'ai_error_count': ai_error_count,
        'error_sentences': error_sentences,
        'year_rows': year_rows,
        'recent_items': recent_items,
        'only_errors': request.GET.get('filter') == 'error',
        'active_nav': 'workshop',
        'ws_active': 'trans',
    })


# ─── 长难句 AI 专项组卷 ──────────────────────────────────────────

class _TransLiteQuestion:
    """AI 训练题没有关联 ExamQuestion，用此替身满足解析 prompt 的字段调用"""
    year = ''
    title = 'AI 根据你的薄弱点与本地词库生成（非真题）'

    def get_exam_type_display(self):
        return 'AI 专项训练题'


TRANS_PAPER_MODES = {
    'weakness': '按薄弱点推荐',
    'syntax': '指定句法专项',
    'words': '按词库单词组卷',
    'rewrite': '真题同难度改写',
}
TRANS_PAPER_COUNTS = (3, 5, 10)
TRANS_PAPER_DIFFS = {'basic': 2, 'standard': 3, 'advanced': 4}
TRANS_PAPER_TOPICS = ['社会', '科技', '教育', '文化', '环境', '经济']
TRANS_PAPER_WORD_SOURCES = {
    'unmastered': '未掌握词',
    'error': '近期错词',
    'favorite': '收藏词',
}
# 批改错因代码 → 组卷句法代码
TRANS_ERROR_TO_SYNTAX = {
    'main': ['passive', 'inversion', 'abstract_noun'],
    'subordinate': ['attributive_clause', 'noun_clause', 'non_finite', 'parenthesis'],
    'logic': ['comparison', 'passive'],
    'meaning': ['non_finite', 'attributive_clause'],
    'expression': ['attributive_clause', 'parenthesis'],
}


def _validate_translation_grade_result(value, dimensions, allowed_error_tags=None):
    """严格校验翻译批改 JSON，返回 (规范化结果, 错误信息)。"""
    if not isinstance(value, dict):
        return None, 'AI 返回不是 JSON 对象'
    scores = value.get('scores')
    if not isinstance(scores, dict):
        return None, '缺少 scores 评分对象'
    normalized_scores = {}
    for key in dimensions:
        raw = scores.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            return None, f'scores.{key} 不是有效数字'
        if not 0 <= float(raw) <= 10:
            return None, f'scores.{key} 超出 0-10 范围'
        normalized_scores[key] = round(float(raw), 2)
    total = value.get('total')
    if isinstance(total, bool) or not isinstance(total, (int, float)) or not math.isfinite(total):
        return None, 'total 不是有效数字'
    if not 0 <= float(total) <= 50:
        return None, 'total 超出 0-50 范围'
    if abs(float(total) - sum(normalized_scores.values())) > 2.01:
        return None, 'total 与分项分数不一致'
    comments = value.get('comments')
    model_translation = value.get('model_translation')
    if not isinstance(comments, str) or not comments.strip():
        return None, '缺少 comments 总评'
    if not isinstance(model_translation, str) or not model_translation.strip():
        return None, '缺少 model_translation 参考译文'
    errors = value.get('errors')
    if not isinstance(errors, list) or any(not isinstance(item, dict) for item in errors):
        return None, 'errors 必须是对象数组'
    suggestions = value.get('suggestions', [])
    if not isinstance(suggestions, list) or any(not isinstance(item, str) for item in suggestions):
        return None, 'suggestions 必须是字符串数组'
    result = dict(value)
    result['scores'] = normalized_scores
    result['total'] = round(float(total), 2)
    result['errors'] = errors
    result['suggestions'] = suggestions
    if allowed_error_tags is not None:
        tags = value.get('error_tags', [])
        if not isinstance(tags, list) or any(tag not in allowed_error_tags for tag in tags):
            return None, 'error_tags 包含不支持的标签'
        result['error_tags'] = list(dict.fromkeys(tags))[:6]
    return result, None
# 真题中文句法标签 → 句法代码（改写模式用）
TRANS_ZH_SYNTAX_MAP = [
    ('定语从句', 'attributive_clause'), ('同位语', 'noun_clause'),
    ('名词性从句', 'noun_clause'), ('非谓语', 'non_finite'),
    ('插入', 'parenthesis'), ('倒装', 'inversion'),
    ('被动', 'passive'), ('比较', 'comparison'), ('抽象名词', 'abstract_noun'),
]
TRANS_SYNTAX_TAG_NAMES = {k: v.split('（')[0] for k, v in TRANS_SYNTAX_TAGS.items()}
TRANS_BATCH_SIZE = 5


def _trans_norm_ws(text):
    return ' '.join((text or '').split())


def _trans_word_tokens(en):
    return re.findall(r"[A-Za-z][A-Za-z'\-]*", en or '')


def _trans_word_in_sentence(word, en_lower):
    """目标词是否出现在句中（允许常见词形变化，最多 4 个字母后缀）"""
    return re.search(r'\b' + re.escape(word.lower()) + r"[a-z]{0,4}\b", en_lower) is not None


def _trans_weak_syntax(latest, limit=3, exclude=False):
    """根据错句的错因标签推导应重点训练的句法代码"""
    from collections import Counter
    counter = Counter()
    for p in latest.values():
        if not _is_error_practice(p):
            continue
        for code in (p.error_tags or []):
            for syn in TRANS_ERROR_TO_SYNTAX.get(code, []):
                counter[syn] += 1
    weak = [c for c, _ in counter.most_common()]
    if exclude:
        weak = [c for c in TRANS_SYNTAX_TAGS if c not in weak]
    return (weak or ['non_finite', 'attributive_clause', 'passive'])[:limit]


def _trans_pick_words(word_source, limit=6):
    """后端从本地词库选词；前端不能传入词库外单词"""
    if word_source == 'error':
        # 近期错词：必须已有学习进度才有错词记录
        qs = Word.objects.filter(progress__error_count__gt=0,
                                 progress__is_excluded=False)
    elif word_source == 'favorite':
        qs = Word.objects.filter(favorite__isnull=False)
    else:
        # 未掌握：从未学过（无进度记录）或进度非 mastered，且未被排除
        qs = (Word.objects.exclude(progress__is_excluded=True)
              .exclude(progress__status='mastered'))
    ids = list(qs.values_list('id', flat=True)[:200])
    if len(ids) > limit:
        ids = random.sample(ids, limit)
    return list(Word.objects.filter(id__in=ids))


def _trans_build_config(raw, latest=None):
    """校验并归一化前端组卷配置；返回 (config_dict, error)。目标词完全由后端决定。"""
    latest = latest if latest is not None else _latest_practice_map()
    mode = raw.get('mode', 'weakness')
    if mode not in TRANS_PAPER_MODES:
        mode = 'weakness'
    diff_key = raw.get('difficulty', 'standard')
    difficulty = TRANS_PAPER_DIFFS.get(diff_key, 3)
    try:
        count = int(raw.get('count', 5))
    except (TypeError, ValueError):
        count = 5
    if count not in TRANS_PAPER_COUNTS:
        count = 5
    length = raw.get('length', 'standard')
    if length not in TRANS_LENGTH_RANGES:
        length = 'standard'
    syntax = [c for c in (raw.get('syntax') or []) if c in TRANS_SYNTAX_TAGS][:4]
    topics = [t for t in (raw.get('topics') or []) if t in TRANS_PAPER_TOPICS][:3]
    word_source = raw.get('word_source', 'unmastered')
    if word_source not in TRANS_PAPER_WORD_SOURCES:
        word_source = 'unmastered'
    reuse_error = bool(raw.get('reuse_error', True))

    # 句法：各模式的默认推导
    if mode == 'weakness':
        syntax = _trans_weak_syntax(latest, exclude=not reuse_error)
    elif mode == 'syntax' and not syntax:
        syntax = ['non_finite', 'attributive_clause']
    elif mode in ('words', 'rewrite') and not syntax:
        syntax = []

    # 目标词：仅 weakness/syntax/words 模式使用，由后端从词库挑选
    target_words = []
    if mode == 'words':
        target_words = _trans_pick_words(word_source, 6)
    elif mode == 'weakness':
        target_words = _trans_pick_words('error', 4) or _trans_pick_words('unmastered', 4)
    elif mode == 'syntax':
        target_words = _trans_pick_words(word_source, 4)

    cfg = {
        'mode': mode, 'difficulty': difficulty, 'count': count,
        'length': length, 'syntax': syntax, 'topics': topics,
        'word_source': word_source, 'reuse_error': reuse_error,
        'target_word_ids': [w.id for w in target_words],
        'target_words': [w.word for w in target_words],
    }
    return cfg, None


def _trans_recommendation():
    """首页默认推荐：根据错句 + 错词生成一套默认配置"""
    latest = _latest_practice_map()
    cfg, _ = _trans_build_config({'mode': 'weakness'}, latest)
    syntax_names = '、'.join(TRANS_SYNTAX_TAG_NAMES.get(c, c) for c in cfg['syntax'])
    words = cfg['target_words']
    focus_parts = []
    if syntax_names:
        focus_parts.append(syntax_names)
    if words:
        focus_parts.append('目标词 ' + '、'.join(words[:4]))
    minutes = {3: 8, 5: 12, 10: 24}[cfg['count']]
    summary = (f"5 句 · 标准难度 · 约 {minutes} 分钟。重点：{('；'.join(focus_parts) or '非谓语、定语从句')}。")
    return cfg, summary


def _trans_seed_sentences(difficulty, count):
    """改写模式：按难度抽取真题句作为改写参考"""
    qs = TranslationSentence.objects.filter(source_type='past_paper')
    if difficulty <= 2:
        qs = qs.filter(difficulty__lte=2)
    elif difficulty >= 4:
        qs = qs.filter(difficulty__gte=4)
    ids = list(qs.values_list('id', flat=True))
    if len(ids) > count:
        ids = random.sample(ids, count)
    seeds = list(TranslationSentence.objects.filter(id__in=ids))
    if not seeds:  # 难度档无题时回退全部真题
        ids = list(TranslationSentence.objects.filter(
            source_type='past_paper').values_list('id', flat=True))
        if len(ids) > count:
            ids = random.sample(ids, count)
        seeds = list(TranslationSentence.objects.filter(id__in=ids))
    return seeds


def _trans_validate_batch(raw_questions, expected, length_key, target_words,
                          lexicon_set, dup_pool, focus_syntax=None,
                          already_hit=None, required_words=None):
    """本地质量校验。返回 (kept[归一化题], feedback[失败说明行])。
    - focus_syntax：用户所选专项句法，题目标签需至少命中其一
    - target_words：本批提示词要求使用的词
    - required_words：截至本批整卷应覆盖的词（分批时为前 N 批的并集）
    - already_hit：此前批次句子中已经命中的词
    """
    already_hit = already_hit or set()
    if required_words is None:
        required_words = target_words
    lo, hi, _ = TRANS_LENGTH_RANGES[length_key]
    feedback, kept = [], []
    if not isinstance(raw_questions, list):
        return [], ['返回的 questions 不是数组，无法解析']
    if len(raw_questions) != expected:
        feedback.append(f'数量不符：要求 {expected} 句，实际返回 {len(raw_questions)} 句')

    batch_en_norm = []
    for i, q in enumerate(raw_questions, 1):
        if not isinstance(q, dict):
            feedback.append(f'第 {i} 句不是有效对象')
            continue
        problems = []
        en = _trans_norm_ws(str(q.get('sentence_en') or ''))
        zh = str(q.get('reference_zh') or '').strip()
        if not en:
            feedback.append(f'第 {i} 句缺少英文原句')
            continue
        tokens = _trans_word_tokens(en)
        if not (lo <= len(tokens) <= hi):
            problems.append(f'词数 {len(tokens)}，超出要求 {lo}-{hi} 词')
        if not (6 <= len(zh) <= 320):
            problems.append('参考译文为空或长度异常')
        tags = []
        for t in (q.get('syntax_tags') or []):
            if t in TRANS_SYNTAX_TAGS and t not in tags:
                tags.append(t)
        if not tags:
            problems.append('缺少合法的句法标签')
        elif focus_syntax and not (set(tags) & set(focus_syntax)):
            problems.append('句法标签与所选专项不符（需包含：'
                            + '、'.join(TRANS_SYNTAX_TAG_NAMES.get(t, t)
                                        for t in focus_syntax) + '）')

        # 句法树：只保留逐字摘自原句的片段
        tree = []
        en_flat = ' '.join(en.split()).lower()
        for node in (q.get('syntax_tree') or []):
            if isinstance(node, dict):
                frag = _trans_norm_ws(str(node.get('fragment') or ''))
                role = str(node.get('role') or '').strip()
                if frag and role and frag.lower() in en_flat:
                    tree.append({'fragment': frag, 'role': role[:30]})

        # 词库范围：超纲内容词比例
        content_tokens = [t.lower() for t in tokens if len(t) >= 5]
        unknown = 0
        for t in content_tokens:
            if any(t == w or (t.startswith(w) and len(t) - len(w) <= 4)
                   or (w.startswith(t) and len(w) - len(t) <= 3)
                   for w in lexicon_set):
                continue
            unknown += 1
        if content_tokens and unknown / len(content_tokens) > 0.4:
            problems.append(f'超纲词比例过高（{unknown}/{len(content_tokens)}），请改用词库内词汇')

        # 重复度：同卷 / 近期 AI 题
        for other in batch_en_norm:
            if difflib.SequenceMatcher(None, en.lower(), other).ratio() > 0.85:
                problems.append('与同卷其他句子高度相似')
                break
        for other, is_seed in dup_pool:
            ratio = difflib.SequenceMatcher(None, en.lower(), other).ratio()
            if ratio > (0.92 if is_seed else 0.88):
                problems.append('与近期已有题目高度相似')
                break

        # AI 自报目标词：只保留确实出现在句中的
        hit_words = [w for w in target_words if _trans_word_in_sentence(w, en_flat)]
        diff_in = q.get('difficulty')
        try:
            diff_in = int(diff_in)
            if not 1 <= diff_in <= 5:
                diff_in = None
        except (TypeError, ValueError):
            diff_in = None

        if problems:
            feedback.append(f'第 {i} 句：' + '；'.join(problems))
        else:
            kept.append({
                'sentence_en': en, 'reference_zh': zh, 'syntax_tags': tags,
                'target_words': hit_words,
                'difficulty': diff_in,
                'difficulty_reason': str(q.get('difficulty_reason') or '')[:300],
                'syntax_tree': tree,
            })
            batch_en_norm.append(en.lower())

    # 超出要求数量时按顺序截断，避免入库题数超出配置
    if len(kept) > expected:
        kept = kept[:expected]

    # 目标词覆盖率按整卷累计（截至本批应覆盖的词 ≥80%）
    if required_words:
        hit_set = ({w for item in kept for w in item['target_words']}
                   | set(already_hit))
        required = list(dict.fromkeys(required_words))
        ratio = sum(1 for w in required if w in hit_set) / len(required)
        if ratio < 0.8:
            missing = [w for w in required if w not in hit_set]
            feedback.append('目标词覆盖率不足 80%，缺失：' + '、'.join(missing))
    return kept, feedback


def _trans_dup_pool(paper, seed_ids):
    """重复度比对池：近期 AI 题 + 本卷已生成 + 真题（改写种子标记 is_seed）"""
    pool = []
    recent = (TranslationSentence.objects.filter(source_type='ai_generated')
              .order_by('-id').values_list('sentence_en', flat=True)[:40])
    pool += [(_trans_norm_ws(x).lower(), False) for x in recent]
    for gq in paper.questions.select_related('sentence'):
        pool.append((_trans_norm_ws(gq.sentence.sentence_en).lower(), False))
    past = (TranslationSentence.objects.filter(source_type='past_paper')
            .values_list('id', 'sentence_en'))
    for sid, en in past:
        pool.append((_trans_norm_ws(en).lower(), sid in seed_ids))
    return pool


def _trans_run_paper(paper):
    """执行组卷（生成接口与重试接口共用）：分批 ≤5 句，每批失败带原因重试一次。"""
    cfg = paper.config_json or {}
    expected = int(cfg.get('count', 5))
    length_key = cfg.get('length', 'standard')
    difficulty = int(cfg.get('difficulty', 3))
    syntax = cfg.get('syntax') or []
    topics = cfg.get('topics') or []
    target_words = cfg.get('target_words') or []
    word_map = {w.word.lower(): w for w in Word.objects.filter(
        id__in=cfg.get('target_word_ids') or [])}
    lexicon_set = set(w.lower() for w in Word.objects.values_list('word', flat=True))

    paper.status = 'generating'
    paper.save(update_fields=['status'])
    t0 = time.monotonic()
    last_err = ''
    last_raw = ''

    # 分批计划：每批 ≤5 句；目标词按批轮转分配，覆盖率按整卷累计
    remaining = expected - paper.questions.count()
    batch_sizes = []
    while remaining > 0:
        batch_sizes.append(min(TRANS_BATCH_SIZE, remaining))
        remaining -= batch_sizes[-1]
    batch_targets = [[] for _ in batch_sizes]
    for i, w in enumerate(target_words):
        batch_targets[i % len(batch_sizes)].append(w)

    def _existing_hits():
        hits = set()
        for g in paper.questions.select_related('sentence'):
            en = ' '.join(g.sentence.sentence_en.split()).lower()
            hits.update(w for w in target_words if _trans_word_in_sentence(w, en))
        return hits

    for bi, batch_n in enumerate(batch_sizes):
        batch_words = batch_targets[bi]
        required_so_far = [w for chunk in batch_targets[:bi + 1] for w in chunk]
        seeds, seed_ids = [], set()
        if cfg.get('mode') == 'rewrite':
            # 每次按缺口取新种子，已被本卷参考过的种子尽量不重复
            used = set()
            for gq in paper.questions.select_related('sentence'):
                note = gq.sentence.source_note
                used.update(re.findall(r'#(\d+)#', note))
            seeds = _trans_seed_sentences(difficulty, batch_n)
            seeds = [s for s in seeds if str(s.id) not in used] or seeds
            seed_ids = {s.id for s in seeds}
        avoid = [_trans_norm_ws(g.sentence.sentence_en)
                 for g in (GeneratedQuestion.objects.filter(paper=paper)
                           .select_related('sentence').order_by('-id')[:6])]
        avoid += [_trans_norm_ws(x) for x in
                  TranslationSentence.objects.filter(source_type='ai_generated')
                  .order_by('-id').values_list('sentence_en', flat=True)[:8]]
        already_hit = _existing_hits()
        prompt = trans_paper_generation_prompt(
            batch_n, length_key, difficulty, syntax, batch_words, topics,
            rewrite_sources=[s.sentence_en for s in seeds] or None,
            avoid_sentences=avoid or None)
        data = {}
        settings_obj = UserSettings.get_settings()
        if settings_obj.exam_model_id:
            data['model_id'] = settings_obj.exam_model_id
            paper.model_id = (AIModel.objects.filter(
                id=settings_obj.exam_model_id).values_list('model_id', flat=True).first() or '')
        content, err = builtin_ai_exam_call(
            prompt, temperature=0.8, data=data, action='trans_paper_generate')
        if err:
            last_err = err
            break
        last_raw = (content or '')[:8000]
        parsed = extract_json_object(content)
        raw_questions = parsed.get('questions') if isinstance(parsed, dict) else None
        dup_pool = _trans_dup_pool(paper, seed_ids)
        kept, fb = _trans_validate_batch(
            raw_questions, batch_n, length_key, batch_words,
            lexicon_set, dup_pool, focus_syntax=syntax or None,
            already_hit=already_hit, required_words=required_so_far)

        # 不合格：带原因定向重试一次
        if fb or len(kept) != batch_n:
            paper.retry_count += 1
            paper.save(update_fields=['retry_count'])
            retry_prompt = trans_paper_generation_prompt(
                batch_n, length_key, difficulty, syntax, batch_words, topics,
                rewrite_sources=[s.sentence_en for s in seeds] or None,
                avoid_sentences=avoid or None,
                retry_feedback='\n'.join(fb) or '上一批句子未全部达标，请重新生成。')
            content2, err2 = builtin_ai_exam_call(
                retry_prompt, temperature=0.8, data=data, action='trans_paper_generate')
            if err2:
                last_err = err2
                break
            last_raw = (content2 or '')[:8000]
            parsed2 = extract_json_object(content2)
            raw2 = parsed2.get('questions') if isinstance(parsed2, dict) else None
            kept2, fb2 = _trans_validate_batch(
                raw2, batch_n, length_key, batch_words,
                lexicon_set, dup_pool, focus_syntax=syntax or None,
                already_hit=already_hit, required_words=required_so_far)
            if kept2:
                kept, fb = kept2, fb2

        if not kept:
            last_err = '生成结果未通过本地校验：' + '；'.join(fb[:4])
            break

        start_order = paper.questions.count() + 1
        with transaction.atomic():
            for idx, item in enumerate(kept):
                order = start_order + idx
                note = f"AI组卷#{paper.id} {TRANS_PAPER_MODES.get(cfg.get('mode'), '')}"
                if cfg.get('mode') == 'rewrite' and idx < len(seeds):
                    note += f' 改写自真题句#{seeds[idx].id}#'
                ts = TranslationSentence.objects.create(
                    question=None, order=order,
                    sentence_en=item['sentence_en'],
                    sentence_zh=item['reference_zh'],
                    difficulty=item['difficulty'] or difficulty,
                    syntax_tags=item['syntax_tags'],
                    source_type='ai_generated', source_note=note, verified=False)
                tids = [word_map[w].id for w in item['target_words'] if w in word_map]
                GeneratedQuestion.objects.create(
                    paper=paper, sentence=ts, order=order,
                    syntax_tree_json=item['syntax_tree'],
                    target_word_ids=tids,
                    difficulty=item['difficulty'] or difficulty,
                    difficulty_reason=item['difficulty_reason'],
                    quality_score=100.0 if not fb else max(40.0, 100.0 - 12 * len(fb)),
                    quality_notes='\n'.join(fb)[:1000],
                    is_approved=not fb)

    paper.duration_ms = int((time.monotonic() - t0) * 1000)
    paper.raw_response = last_raw[:8000]
    paper.prompt_hash = hashlib.sha256(
        json.dumps(cfg, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    done = paper.questions.count()
    if done >= expected:
        # 整卷复检目标词覆盖率：达标则全部题目视为通过校验（覆盖是整卷性质）
        required = list(dict.fromkeys(target_words))
        hits = _existing_hits()
        ratio = (sum(1 for w in required if w in hits) / len(required)
                 if required else 1.0)
        if ratio >= 0.8:
            paper.questions.filter(is_approved=False).update(
                is_approved=True, quality_score=100.0, quality_notes='')
            paper.status = 'ready'
            paper.error_message = ''
        else:
            missing = [w for w in required if w not in hits]
            paper.status = 'failed'
            paper.error_message = (
                '已生成 %d/%d 句，但整卷目标词覆盖率不足 80%%（缺失：%s）。'
                '重试将替换未达标的句子，也可降低难度/减少条件后重新组卷。'
                % (done, expected, '、'.join(missing[:6])))
    elif done > 0:
        paper.status = 'failed'
        paper.error_message = f'已生成 {done}/{expected} 句。' + (last_err or '部分句子未通过校验，请重试补齐，或降低难度/减少条件。')
    else:
        paper.status = 'failed'
        paper.error_message = last_err or 'AI 返回内容未通过本地校验，请降低难度或减少条件后重试。'
    paper.save()
    return done, paper.error_message


@require_http_methods(['GET'])
def api_trans_recommendation(request):
    """默认推荐配置（根据错句与错词）"""
    cfg, summary = _trans_recommendation()
    return JsonResponse({
        'success': True, 'config': cfg, 'summary': summary,
        'syntax_names': {k: TRANS_SYNTAX_TAG_NAMES[k] for k in cfg['syntax']},
    })


def trans_paper_new_page(request):
    """AI 专项组卷配置页"""
    cfg, summary = _trans_recommendation()
    recent_papers = []
    for p in GeneratedPaper.objects.all()[:8]:
        recent_papers.append({
            'id': p.id, 'title': p.title, 'mode_name': TRANS_PAPER_MODES.get(p.mode, p.mode),
            'status': p.status, 'status_name': p.get_status_display(),
            'count': p.questions.count(), 'total': (p.config_json or {}).get('count', 0),
            'created_at': p.created_at.strftime('%m-%d %H:%M'),
        })
    return render(request, 'trans_paper_new.html', {
        'rec_cfg': cfg, 'rec_summary': summary,
        'recent_papers': recent_papers,
        'modes': TRANS_PAPER_MODES,
        'diffs': TRANS_PAPER_DIFFS,
        'lengths': {k: v[2] for k, v in TRANS_LENGTH_RANGES.items()},
        'syntax_options': TRANS_SYNTAX_TAG_NAMES,
        'topics': TRANS_PAPER_TOPICS,
        'word_sources': TRANS_PAPER_WORD_SOURCES,
        'active_nav': 'workshop', 'ws_active': 'trans',
    })


@require_http_methods(['POST'])
def api_trans_paper_generate(request):
    """接收组卷配置 → 后端选词 → AI 生成 → 校验 → 保存"""
    try:
        raw = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': '请求格式错误'}, status=400)
    cfg, _ = _trans_build_config(raw)
    title = (f"AI 专项训练 · {TRANS_PAPER_MODES.get(cfg['mode'])} · "
             f"{timezone.now():%m-%d %H:%M}")
    paper = GeneratedPaper.objects.create(
        title=title, mode=cfg['mode'], source='ai', config_json=cfg,
        target_word_ids=cfg['target_word_ids'], syntax_tags=cfg['syntax'],
        difficulty=cfg['difficulty'], status='pending')
    done, err_msg = _trans_run_paper(paper)
    return JsonResponse({
        'success': paper.status == 'ready',
        'paper_id': paper.id,
        'status': paper.status,
        'created': done,
        'expected': cfg['count'],
        'error': err_msg,
        'redirect': f'/workshop/trans/papers/{paper.id}/',
    })


def trans_paper_detail_page(request, pid):
    """AI 卷详情：状态、题目列表、失败重试入口"""
    paper = get_object_or_404(GeneratedPaper, id=pid)
    latest = _latest_practice_map()
    questions = []
    for gq in paper.questions.select_related('sentence').order_by('order'):
        p = latest.get(gq.sentence_id)
        total = _parse_practice_total(p) if p else None
        questions.append({
            'sid': gq.sentence_id, 'order': gq.order,
            'sentence_en': gq.sentence.sentence_en,
            'sentence_zh': gq.sentence.sentence_zh,
            'syntax_tags': [TRANS_SYNTAX_TAG_NAMES.get(t, t) for t in (gq.sentence.syntax_tags or [])],
            'syntax_tree': gq.syntax_tree_json or [],
            'difficulty': gq.difficulty,
            'quality_score': gq.quality_score,
            'difficulty_reason': gq.difficulty_reason,
            'status': ('new' if p is None else ('error' if _is_error_practice(p) else 'ok')),
            'last_total': int(total) if total is not None else None,
        })
    cfg = paper.config_json or {}
    return render(request, 'trans_paper_detail.html', {
        'paper': paper,
        'questions': questions,
        'expected': cfg.get('count', 0),
        'mode_name': TRANS_PAPER_MODES.get(paper.mode, paper.mode),
        'diff_name': TRANS_DIFFICULTY_NAMES.get(paper.difficulty, str(paper.difficulty)),
        'length_name': TRANS_LENGTH_RANGES.get(cfg.get('length'), ('', '', '标准'))[2],
        'syntax_names': '、'.join(TRANS_SYNTAX_TAG_NAMES.get(t, t) for t in (paper.syntax_tags or [])),
        'target_words': cfg.get('target_words') or [],
        'active_nav': 'workshop', 'ws_active': 'trans',
    })


@require_http_methods(['POST'])
def api_trans_paper_retry(request, pid):
    """重试失败/部分失败的试卷：只补齐缺失句子，已生成的保留不丢；
    整卷句数已够但存在未通过整卷校验的句子时，删除后重新生成替换。"""
    paper = get_object_or_404(GeneratedPaper, id=pid)
    if paper.status == 'generating':
        return JsonResponse({'error': '试卷正在生成中，请勿重复提交'}, status=409)
    expected = int((paper.config_json or {}).get('count', 5))
    if paper.questions.count() >= expected:
        bad = paper.questions.filter(is_approved=False)
        if not bad.exists():
            return JsonResponse({'success': True, 'paper_id': paper.id,
                                 'status': paper.status, 'created': 0,
                                 'redirect': f'/workshop/trans/papers/{paper.id}/'})
        # 删除未通过校验的句子（级联删除组卷题目），再按升序重排卷内序号
        TranslationSentence.objects.filter(
            id__in=list(bad.values_list('sentence_id', flat=True))).delete()
        for i, gq in enumerate(
                paper.questions.select_related('sentence').order_by('order'), 1):
            if gq.order != i:
                gq.order = i
                gq.save(update_fields=['order'])
    done, err_msg = _trans_run_paper(paper)
    return JsonResponse({
        'success': paper.status == 'ready',
        'paper_id': paper.id,
        'status': paper.status,
        'created': done,
        'expected': expected,
        'error': err_msg,
        'redirect': f'/workshop/trans/papers/{paper.id}/',
    })


@require_http_methods(['POST'])
def api_trans_paper_delete(request, pid):
    """删除失败的英语一 AI 组卷记录，保留已有练习数据。"""
    paper = get_object_or_404(GeneratedPaper, id=pid)
    if paper.status != 'failed':
        return JsonResponse({'error': '只能删除失败的组卷记录'}, status=400)
    sentence_ids = list(paper.questions.values_list('sentence_id', flat=True))
    if TranslationPractice.objects.filter(sentence_id__in=sentence_ids).exists():
        return JsonResponse({'error': '该组卷已有练习记录，不能删除，请保留后重试'}, status=409)
    paper.delete()
    return JsonResponse({'success': True, 'deleted': pid})


# ============================================================
# 六级汉译英专题（中文段落 → 英文表达；训练单位与英语一逐句英译汉不同）
# ============================================================

CET6_GRADE_PASS = 40   # 总分 50，≥40 且无错因标签视为达标：复习间隔+7天，否则次日重练
CET6_COVER_RATIO = 0.8
CET6_PAPER_COUNTS = [1, 3, 5]
CET6_DIFFS = {'basic': 2, 'standard': 3, 'advanced': 4}
CET6_WORD_SOURCES = {'unmastered': '未掌握词', 'error': '近期错词', 'favorite': '收藏词'}
CET6_ERROR_TO_SKILL = {
    'omission': ['cohesion', 'collocation'],
    'culture': ['culture'],
    'grammar': ['tense_voice', 'attributive_clause', 'non_finite'],
    'collocation': ['collocation', 'conversion'],
    'logic': ['cohesion'],
    'fluency': ['conversion', 'cohesion'],
}
_CET6_PAST_IMPORTED = False


def _cet6_zh_len(text):
    """中文段字数（含标点，不含空白）"""
    return len([c for c in (text or '') if not c.isspace()])


def _cet6_latest_practice_map():
    """{passage_id: 最近一次 Cet6TranslationPractice}"""
    latest = {}
    for p in (Cet6TranslationPractice.objects
              .only('id', 'passage_id', 'score_json', 'error_tags',
                    'next_review_date', 'consecutive_correct')
              .order_by('created_at')):
        latest[p.passage_id] = p
    return latest


def _cet6_parse_total(practice):
    score = practice.get_score()
    total = score.get('total')
    return total if isinstance(total, (int, float)) else None


def _cet6_is_error_practice(practice):
    """是否错段：最近一次总分 < 达标线 或带错因标签"""
    total = _cet6_parse_total(practice)
    if total is None:
        return bool(practice.error_tags)
    return total < CET6_GRADE_PASS or bool(practice.error_tags)


def _cet6_import_past_papers():
    """幂等导入六级真题段落：ExamQuestion(cet6 翻译) → Cet6TranslationPassage。
    现有真题仅有中文（参考译文库中为空），导入时 reference_en 留空、verified=False，
    参考译文由首次 AI 批改的 model_translation 回填。"""
    created = 0
    for q in ExamQuestion.objects.filter(exam_type='cet6', question_type='translation'):
        zh = (q.content or '').strip()
        if not zh:
            continue
        _, is_new = Cet6TranslationPassage.objects.get_or_create(
            question=q, source_type='past_paper',
            defaults={
                'source_zh': zh,
                'reference_en': (q.model_answer or '').strip(),
                'topic_tags': [t for t in (q.tags or []) if t in CET6_TOPICS][:3],
                'skill_tags': [],
                'difficulty': q.difficulty or 3,
                'verified': bool((q.model_answer or '').strip()),
                'source_note': f'{q.year}年真题',
            })
        if is_new:
            created += 1
    return created


def _cet6_error_focus(limit=3):
    """根据错段错因标签推导近期薄弱考点（六级专项代码）"""
    from collections import Counter
    counter = Counter()
    for p in Cet6TranslationPractice.objects.all():
        if _cet6_is_error_practice(p):
            for t in (p.error_tags or []):
                for s in CET6_ERROR_TO_SKILL.get(t, []):
                    counter[s] += 1
    return [c for c, _ in counter.most_common(limit)]


def _cet6_recommendation():
    """默认推荐配置：专项取错因推导，目标表达取近期错词"""
    skills = _cet6_error_focus(2) or ['culture', 'collocation']
    words = _trans_pick_words('error', 4) or _trans_pick_words('unmastered', 4)
    cfg = {
        'mode': 'weakness', 'difficulty': 3, 'count': 1, 'length': 'standard',
        'topics': [], 'skills': skills,
        'word_source': 'error' if words else 'unmastered',
        'target_word_ids': [w.id for w in words],
        'target_words': [w.word for w in words],
    }
    skill_names = '、'.join(CET6_SKILL_TAG_NAMES.get(c, c) for c in skills)
    summary = f'1 段 · 标准难度 · 约 15 分钟。重点：{skill_names}'
    return cfg, summary


def cet6_home(request):
    """六级汉译英专题首页：今日推荐 + 真题段落列表（年份筛选）+ 统计"""
    global _CET6_PAST_IMPORTED
    if not _CET6_PAST_IMPORTED:
        _cet6_import_past_papers()
        _CET6_PAST_IMPORTED = True

    year = request.GET.get('year', '')
    try:
        year_int = int(year) if year else None
    except (TypeError, ValueError):
        year_int = None
    passages = Cet6TranslationPassage.objects.select_related('question')
    past = passages.filter(source_type='past_paper')
    years = list(ExamQuestion.objects.filter(
        exam_type='cet6', question_type='translation')
        .values_list('year', flat=True).distinct().order_by('-year'))
    if year_int in years:
        past = past.filter(question__year=year_int)

    latest = _cet6_latest_practice_map()
    today = timezone.localdate()
    due_ids = [pid for pid, p in latest.items()
               if p.next_review_date and p.next_review_date <= today]
    err_ids = [pid for pid, p in latest.items() if _cet6_is_error_practice(p)]
    unpracticed = list(past.exclude(id__in=latest.keys()).values_list('id', flat=True))
    if due_ids:
        rec = Cet6TranslationPassage.objects.select_related('question').get(
            id=random.choice(due_ids))
        rec_reason = '到期的复习段落'
    elif err_ids:
        rec = Cet6TranslationPassage.objects.select_related('question').get(
            id=random.choice(err_ids))
        rec_reason = '你的错段，重练巩固'
    elif unpracticed:
        rec = Cet6TranslationPassage.objects.select_related('question').get(
            id=random.choice(unpracticed))
        rec_reason = '未练过的真题段落'
    else:
        rec = past.order_by('-created_at').first()
        rec_reason = '今日练习'
    focus = _cet6_error_focus(2)
    focus_names = '、'.join(CET6_SKILL_TAG_NAMES.get(c, c) for c in focus) \
        if focus else '文化表达、动词搭配'

    rows = []
    for p in past:
        lp = latest.get(p.id)
        rows.append({
            'id': p.id, 'year': p.question.year if p.question else None,
            'title': (p.question.title if p.question else '') or p.source_note,
            'head': p.source_zh[:60],
            'difficulty': p.difficulty,
            'status': ('new' if lp is None
                       else ('error' if _cet6_is_error_practice(lp) else 'ok')),
            'total': _cet6_parse_total(lp) if lp else None,
            'due': bool(lp and lp.next_review_date and lp.next_review_date <= today),
        })
    recent = (Cet6TranslationPractice.objects.select_related('passage')
              .order_by('-created_at')[:8])
    stats = {
        'total': past.count(),
        'practiced': past.filter(id__in=list(latest.keys())).count(),
        'errors': len(err_ids),
        'due': len(due_ids),
    }
    return render(request, 'cet6_home.html', {
        'passages': rows, 'years': years, 'year': year_int,
        'rec': rec, 'rec_reason': rec_reason, 'focus_names': focus_names,
        'stats': stats, 'recent': recent,
        'pass_name': CET6_GRADE_PASS,
        'active_nav': 'workshop', 'ws_active': 'cet6',
    })


def cet6_practice_page(request, pid):
    """六级段落翻译练习页：先独立翻译，批改后展示分维结果与解析（默认折叠）"""
    passage = get_object_or_404(Cet6TranslationPassage, id=pid)
    latest = _cet6_latest_practice_map().get(passage.id)
    return render(request, 'cet6_practice.html', {
        'passage': passage,
        'is_ai': passage.source_type == 'ai_generated',
        'latest': latest,
        'last_total': _cet6_parse_total(latest) if latest else None,
        'skill_names': [CET6_SKILL_TAG_NAMES.get(t, t) for t in (passage.skill_tags or [])],
        'key_points': passage.key_points or [],
        'error_tag_names': {t: n for t, n in CET6_ERROR_TAGS.items()},
        'pass_name': CET6_GRADE_PASS,
        'active_nav': 'workshop', 'ws_active': 'cet6',
    })


@require_http_methods(['POST'])
def api_cet6_submit(request, pid):
    """提交六级段落翻译：AI 五维批改（无 AI 时兜底展示参考译文）+ SRS 排期"""
    passage = get_object_or_404(Cet6TranslationPassage, id=pid)
    try:
        data = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, TypeError):
        return JsonResponse({'error': '请求格式错误'}, status=400)
    user_trans = (data.get('translation') or '').strip()
    if not user_trans:
        return JsonResponse({'error': '请先输入你的英文译文'}, status=400)
    try:
        duration = max(0, int(data.get('duration') or 0))
    except (TypeError, ValueError):
        duration = 0
    duration = max(0, min(duration, 7200))

    prev = _cet6_latest_practice_map().get(passage.id)
    prompt = cet6_grading_prompt_v2(
        passage.source_zh, user_trans,
        reference=(passage.reference_en or '').strip() or None,
        key_points=passage.key_points or [], skill_tags=passage.skill_tags or [])
    content, err = builtin_ai_exam_call(
        prompt, temperature=0.3, action='cet6_grade_passage',
        settings_model_field='cet6_translation_model')

    score_data = extract_json_object(content) if content else None
    score_data, validation_error = _validate_translation_grade_result(
        score_data,
        ('accuracy', 'culture', 'grammar', 'collocation', 'fluency'),
        allowed_error_tags=CET6_ERROR_TAGS)
    if err or validation_error:
        # 无 AI 兜底：不评分，保存作答并展示参考译文供自评，次日安排重练
        sim = None
        if passage.reference_en:
            sim = round(difflib.SequenceMatcher(
                None, user_trans.lower().split(), passage.reference_en.lower().split()).ratio(), 4)
        fallback_practice = Cet6TranslationPractice.objects.create(
            passage=passage, user_translation=user_trans,
            score_json=json.dumps({'fallback': True, 'similarity': sim}, ensure_ascii=False),
            feedback='AI 服务暂不可用，已展示参考译文供自评；本次不计入达标统计。',
            error_tags=[], duration_seconds=duration,
            next_review_date=timezone.localdate() + timedelta(days=1),
            consecutive_correct=(prev.consecutive_correct if prev else 0))
        _bind_practice_to_last_log('cet6_grade_passage', fallback_practice)
        return JsonResponse({
            'success': True, 'fallback': True,
            'reference_en': passage.reference_en or '',
            'message': 'AI 批改暂不可用，已保留本次作答并展示参考译文，稍后可重试。',
        })

    error_tags = score_data.get('error_tags', [])
    practice = Cet6TranslationPractice.objects.create(
        passage=passage, user_translation=user_trans,
        score_json=json.dumps(score_data, ensure_ascii=False),
        feedback=str(score_data.get('comments') or ''),
        error_tags=error_tags, duration_seconds=duration)
    _bind_practice_to_last_log('cet6_grade_passage', practice)
    total = _cet6_parse_total(practice)
    today = timezone.localdate()
    if total is not None and total >= CET6_GRADE_PASS and not error_tags:
        practice.next_review_date = today + timedelta(days=7)
        practice.consecutive_correct = (prev.consecutive_correct + 1) if prev else 1
    else:
        practice.next_review_date = today + timedelta(days=1)
        practice.consecutive_correct = 0
    practice.save(update_fields=['next_review_date', 'consecutive_correct'])

    # 参考译文回填：真题段库中无参考译文时，采用 AI 批改返回的更佳译文
    if not passage.reference_en:
        mt = (score_data.get('model_translation') or '').strip()
        if mt:
            passage.reference_en = mt
            passage.save(update_fields=['reference_en'])
    # 表达词库沉淀：把批改给出的替换表达合并进 key_points（按中文概念去重）
    _cet6_merge_key_points(passage, score_data.get('replacements') or [])

    return JsonResponse({
        'success': True, 'fallback': False, 'result': score_data,
        'practice_id': practice.id,
        'reference_en': passage.reference_en or '',
        'next_review': practice.next_review_date.isoformat() if practice.next_review_date else None,
    })


def _cet6_merge_key_points(passage, replacements):
    """把 AI 批改的替换表达合并进段落 key_points（按中文概念去重，最多 12 条）"""
    existing = {str(kp.get('zh') or '').strip().lower()
                for kp in (passage.key_points or []) if isinstance(kp, dict)}
    merged = list(passage.key_points or [])
    for r in replacements:
        if not isinstance(r, dict):
            continue
        zh = str(r.get('zh') or '').strip()
        better = str(r.get('better') or '').strip()
        if not zh or not better or zh.lower() in existing:
            continue
        merged.append({'zh': zh, 'recommended_en': better,
                       'alternatives': [str(r.get('basic') or '').strip()] if r.get('basic') else []})
        existing.add(zh.lower())
    if merged != list(passage.key_points or []):
        passage.key_points = merged[:12]
        passage.save(update_fields=['key_points'])


def cet6_errors_page(request):
    """我的错段：最近一次练习未达标或带错因标签的段落 + SRS 到期状态"""
    latest = _cet6_latest_practice_map()
    today = timezone.localdate()
    rows = []
    for pid, p in latest.items():
        if not _cet6_is_error_practice(p):
            continue
        rows.append((pid, p))
    rows.sort(key=lambda x: x[1].created_at, reverse=True)
    passages = Cet6TranslationPassage.objects.in_bulk([pid for pid, _ in rows])
    items = []
    for pid, p in rows:
        passage = passages.get(pid)
        if not passage:
            continue
        items.append({
            'passage': passage,
            'total': _cet6_parse_total(p),
            'error_names': [CET6_ERROR_TAGS.get(t, t) for t in (p.error_tags or [])],
            'next_review': p.next_review_date,
            'due': bool(p.next_review_date and p.next_review_date <= today),
            'attempts': Cet6TranslationPractice.objects.filter(passage_id=pid).count(),
        })
    return render(request, 'cet6_errors.html', {
        'items': items, 'pass_name': CET6_GRADE_PASS,
        'active_nav': 'workshop', 'ws_active': 'cet6',
    })


def cet6_expressions_page(request):
    """表达词库：聚合各段落关键表达点（中文概念 → 推荐英文 → 替代表达），标注掌握状态"""
    kp_map = {}
    for p in Cet6TranslationPassage.objects.all():
        for kp in (p.key_points or []):
            if not isinstance(kp, dict):
                continue
            zh = str(kp.get('zh') or '').strip()
            en = str(kp.get('recommended_en') or '').strip()
            if not zh or not en:
                continue
            item = kp_map.setdefault(zh.lower(), {
                'zh': zh, 'recommended_en': en, 'alternatives': [],
                'count': 0, 'status': ''})
            item['count'] += 1
            for a in (kp.get('alternatives') or []):
                a = str(a).strip()
                if a and a not in item['alternatives'] and a.lower() != en.lower():
                    item['alternatives'].append(a)
    # 掌握状态：以最近一次练习的关键表达点判定结果为准
    latest = _cet6_latest_practice_map()
    for p in latest.values():
        for r in (p.get_score().get('key_point_results') or []):
            if not isinstance(r, dict):
                continue
            item = kp_map.get(str(r.get('zh') or '').strip().lower())
            if not item:
                continue
            status = r.get('status')
            if status == 'missed':
                item['status'] = 'missed'
            elif status in ('conveyed', 'rephrased') and item['status'] != 'missed':
                item['status'] = 'conveyed'
    items = sorted(kp_map.values(),
                   key=lambda x: (x['status'] != 'missed', -x['count'], x['zh']))
    return render(request, 'cet6_expressions.html', {
        'items': items,
        'active_nav': 'workshop', 'ws_active': 'cet6',
    })


def cet6_report_page(request):
    """学习报告：练习量、平均分、达标率、分维均分、错因分布、最近练习"""
    practices = list(Cet6TranslationPractice.objects
                     .select_related('passage').order_by('created_at'))
    dim_names = {'accuracy': '信息完整', 'culture': '文化表达', 'grammar': '语法句法',
                 'collocation': '搭配词性', 'fluency': '衔接自然'}
    dim_acc, tag_counter, totals, rows = {}, {}, [], []
    for p in practices:
        s = p.get_score()
        for k, v in (s.get('scores') or {}).items():
            if isinstance(v, (int, float)):
                dim_acc.setdefault(k, []).append(v)
        for t in (p.error_tags or []):
            tag_counter[t] = tag_counter.get(t, 0) + 1
        t = s.get('total')
        if isinstance(t, (int, float)):
            totals.append(t)
        rows.append({'practice': p, 'total': t if isinstance(t, (int, float)) else None})
    dims = [{'key': k, 'name': dim_names.get(k, k), 'avg': round(sum(v) / len(v), 1)}
            for k, v in dim_acc.items()]
    dims.sort(key=lambda d: d['avg'])
    max_tag = max(tag_counter.values()) if tag_counter else 1
    tags = [{'name': CET6_ERROR_TAGS.get(t, t), 'count': c,
             'pct': int(100 * c / max_tag)}
            for t, c in sorted(tag_counter.items(), key=lambda x: (-x[1], x[0]))]
    recent = list(reversed(rows))[:10]
    stats = {
        'count': len(practices),
        'avg': round(sum(totals) / len(totals), 1) if totals else None,
        'best': max(totals) if totals else None,
        'pass_rate': (int(100 * sum(1 for t in totals if t >= CET6_GRADE_PASS) / len(totals))
                      if totals else None),
    }
    return render(request, 'cet6_report.html', {
        'stats': stats, 'dims': dims, 'tags': tags, 'recent': recent,
        'pass_name': CET6_GRADE_PASS,
        'active_nav': 'workshop', 'ws_active': 'cet6',
    })


def _cet6_seed_passages(difficulty, count):
    """改写模式：按难度抽取真题段作为改写参考"""
    qs = Cet6TranslationPassage.objects.filter(source_type='past_paper')
    if difficulty <= 2:
        qs = qs.filter(difficulty__lte=2)
    elif difficulty >= 4:
        qs = qs.filter(difficulty__gte=4)
    ids = list(qs.values_list('id', flat=True))
    if len(ids) > count:
        ids = random.sample(ids, count)
    seeds = list(Cet6TranslationPassage.objects.filter(id__in=ids))
    if not seeds:  # 难度档无段时回退全部真题
        seeds = list(Cet6TranslationPassage.objects.filter(
            source_type='past_paper').order_by('?')[:count])
    return seeds


def _cet6_dup_pool(paper, seed_ids):
    """重复度比对池：近期 AI 段 + 本卷已生成 + 真题中文段（改写种子标记 is_seed）"""
    pool = []
    recent = (Cet6TranslationPassage.objects.filter(source_type='ai_generated')
              .order_by('-id').values_list('source_zh', flat=True)[:40])
    pool += [(_trans_norm_ws(x), False) for x in recent]
    for gq in paper.questions.select_related('passage'):
        pool.append((_trans_norm_ws(gq.passage.source_zh), False))
    past = (Cet6TranslationPassage.objects.filter(source_type='past_paper')
            .values_list('id', 'source_zh'))
    for sid, zh in past:
        pool.append((_trans_norm_ws(zh), sid in seed_ids))
    return pool


def _cet6_validate_batch(raw_passages, expected, length_key, target_words,
                         dup_pool, focus_skills=None, already_hit=None,
                         required_words=None):
    """本地质量校验。返回 (kept[归一化段], feedback[失败说明行])。
    - target_words：本批提示词要求使用的目标表达
    - required_words：截至本批整卷应覆盖的词（分批时为前 N 批的并集）
    - already_hit：此前批次参考译文中已经命中的词"""
    already_hit = already_hit or set()
    if required_words is None:
        required_words = target_words
    lo, hi, _ = CET6_LENGTH_RANGES[length_key]
    feedback, kept = [], []
    if not isinstance(raw_passages, list):
        return [], ['返回的 passages 不是数组，无法解析']
    if len(raw_passages) != expected:
        feedback.append(f'数量不符：要求 {expected} 段，实际返回 {len(raw_passages)} 段')

    batch_zh_norm = []
    for i, q in enumerate(raw_passages, 1):
        if not isinstance(q, dict):
            feedback.append(f'第 {i} 段不是有效对象')
            continue
        problems = []
        zh = _trans_norm_ws(str(q.get('source_zh') or ''))
        en = _trans_norm_ws(str(q.get('reference_en') or ''))
        if not zh:
            feedback.append(f'第 {i} 段缺少中文材料')
            continue
        zh_n = _cet6_zh_len(zh)
        if not (lo <= zh_n <= hi):
            problems.append(f'中文字数 {zh_n}，超出要求 {lo}-{hi} 字')
        en_words = len(_trans_word_tokens(en))
        if not en or not (0.35 * zh_n <= en_words <= 1.1 * zh_n):
            problems.append('参考英文译文为空或长度异常')
        skills = []
        for t in (q.get('skill_tags') or []):
            if t in CET6_SKILL_TAGS and t not in skills:
                skills.append(t)
        if not skills:
            problems.append('缺少合法的考点标签')
        elif focus_skills and not (set(skills) & set(focus_skills)):
            problems.append('考点标签与所选专项不符（需包含：'
                            + '、'.join(CET6_SKILL_TAG_NAMES.get(t, t)
                                        for t in focus_skills) + '）')
        topics = [t for t in (q.get('topic_tags') or []) if t in CET6_TOPICS]

        # 关键表达点：只保留推荐英文确实出现在参考译文中的点（允许词形变化）
        en_flat = en.lower()
        key_points = []
        for kp in (q.get('key_points') or []):
            if not isinstance(kp, dict):
                continue
            kp_zh = _trans_norm_ws(str(kp.get('zh') or ''))
            kp_en = _trans_norm_ws(str(kp.get('recommended_en') or ''))
            if not kp_zh or not kp_en:
                continue
            toks = [t.lower() for t in _trans_word_tokens(kp_en) if len(t) >= 3]
            if toks and all(_trans_word_in_sentence(t, en_flat) for t in toks):
                alts = [str(a).strip() for a in (kp.get('alternatives') or [])
                        if str(a).strip()][:3]
                key_points.append({'zh': kp_zh, 'recommended_en': kp_en,
                                   'alternatives': alts})
        if len(key_points) < 2:
            problems.append('关键表达点不足 2 条或与参考译文不符')

        # 禁止无依据统计数字（AI 生成材料；真题含数据不在校验范围）
        if re.search(r'\d+(\.\d+)?\s*(%|％|万|亿)|\d+\s*个百分点', zh):
            problems.append('包含疑似统计数据，请改用定性描述')

        # 重复度：同卷 / 近期 AI 段 / 真题（改写种子用更宽松阈值）
        for other in batch_zh_norm:
            if difflib.SequenceMatcher(None, zh, other).ratio() > 0.85:
                problems.append('与同卷其他段落高度相似')
                break
        for other, is_seed in dup_pool:
            if difflib.SequenceMatcher(None, zh, other).ratio() > (0.92 if is_seed else 0.88):
                problems.append('与近期已有材料高度相似')
                break

        # AI 自报目标表达：只保留确实出现在参考译文中的
        hit_words = [w for w in target_words if _trans_word_in_sentence(w, en_flat)]
        diff_in = q.get('difficulty')
        try:
            diff_in = int(diff_in)
            if not 1 <= diff_in <= 5:
                diff_in = None
        except (TypeError, ValueError):
            diff_in = None

        if problems:
            feedback.append(f'第 {i} 段：' + '；'.join(problems))
        else:
            kept.append({
                'source_zh': zh, 'reference_en': en, 'topic_tags': topics,
                'skill_tags': skills, 'key_points': key_points,
                'target_words': hit_words, 'difficulty': diff_in,
                'difficulty_reason': str(q.get('difficulty_reason') or '')[:300],
            })
            batch_zh_norm.append(zh)

    # 超出要求数量时按顺序截断，避免入库段数超出配置
    if len(kept) > expected:
        kept = kept[:expected]

    # 目标表达覆盖率按整卷累计（截至本批应覆盖的词 ≥80%）
    if required_words:
        hit_set = ({w for item in kept for w in item['target_words']}
                   | set(already_hit))
        required = list(dict.fromkeys(required_words))
        ratio = sum(1 for w in required if w in hit_set) / len(required)
        if ratio < CET6_COVER_RATIO:
            missing = [w for w in required if w not in hit_set]
            feedback.append('目标表达覆盖率不足 80%，缺失：' + '、'.join(missing))
    return kept, feedback


def _cet6_build_config(raw, latest=None):
    """校验并归一化前端组卷配置；目标表达完全由后端决定。返回 (config_dict, error)。"""
    mode = raw.get('mode', 'weakness')
    if mode not in ('weakness', 'topic', 'expressions', 'rewrite'):
        mode = 'weakness'
    diff_key = raw.get('difficulty', 'standard')
    difficulty = CET6_DIFFS.get(diff_key, 3)
    try:
        count = int(raw.get('count', 1))
    except (TypeError, ValueError):
        count = 1
    if count not in CET6_PAPER_COUNTS:
        count = 1
    length = raw.get('length', 'standard')
    if length not in CET6_LENGTH_RANGES:
        length = 'standard'
    topics = [t for t in (raw.get('topics') or []) if t in CET6_TOPICS][:4]
    skills = [c for c in (raw.get('skills') or []) if c in CET6_SKILL_TAGS][:4]
    word_source = raw.get('word_source', 'unmastered')
    if word_source not in CET6_WORD_SOURCES:
        word_source = 'unmastered'

    # 专项：各模式默认推导
    if mode == 'weakness':
        skills = _cet6_error_focus(2) or ['culture', 'collocation']
    elif mode in ('topic', 'rewrite') and not skills:
        skills = ['culture', 'collocation']
    elif mode == 'expressions' and not skills:
        skills = ['collocation', 'conversion']

    # 目标表达：仅 expressions/topic/weakness 模式使用，由后端从词库挑选
    target_words = []
    if mode == 'expressions':
        target_words = _trans_pick_words(word_source, 6)
    elif mode == 'weakness':
        target_words = _trans_pick_words('error', 4) or _trans_pick_words('unmastered', 4)
    elif mode == 'topic':
        target_words = _trans_pick_words(word_source, 4)

    cfg = {
        'mode': mode, 'difficulty': difficulty, 'count': count,
        'length': length, 'topics': topics, 'skills': skills,
        'word_source': word_source,
        'target_word_ids': [w.id for w in target_words],
        'target_words': [w.word for w in target_words],
    }
    return cfg, None


def _cet6_run_paper(paper):
    """执行六级组卷（生成与重试共用）：分批 ≤2 段，每批失败带原因重试一次。"""
    cfg = paper.config_json or {}
    expected = int(cfg.get('count', 1))
    length_key = cfg.get('length', 'standard')
    difficulty = int(cfg.get('difficulty', 3))
    skills = cfg.get('skills') or []
    topics = cfg.get('topics') or []
    target_words = cfg.get('target_words') or []
    word_map = {w.word.lower(): w for w in Word.objects.filter(
        id__in=cfg.get('target_word_ids') or [])}

    paper.status = 'generating'
    paper.save(update_fields=['status'])
    t0 = time.monotonic()
    last_err = ''
    last_raw = ''

    # 分批计划：每批 ≤2 段；目标表达按批轮转分配，覆盖率按整卷累计
    remaining = expected - paper.questions.count()
    batch_sizes = []
    while remaining > 0:
        batch_sizes.append(min(CET6_BATCH_SIZE, remaining))
        remaining -= batch_sizes[-1]
    batch_targets = [[] for _ in batch_sizes]
    for i, w in enumerate(target_words):
        batch_targets[i % len(batch_sizes)].append(w)

    def _existing_hits():
        hits = set()
        for g in paper.questions.select_related('passage'):
            en = ' '.join(g.passage.reference_en.split()).lower()
            hits.update(w for w in target_words if _trans_word_in_sentence(w, en))
        return hits

    for bi, batch_n in enumerate(batch_sizes):
        batch_words = batch_targets[bi]
        required_so_far = [w for chunk in batch_targets[:bi + 1] for w in chunk]
        seeds, seed_ids = [], set()
        if cfg.get('mode') == 'rewrite':
            # 每次按缺口取新种子，已被本卷参考过的种子尽量不重复
            used = set()
            for gq in paper.questions.select_related('passage'):
                used.update(re.findall(r'#(\d+)#', gq.passage.source_note))
            seeds = _cet6_seed_passages(difficulty, batch_n)
            seeds = [s for s in seeds if str(s.id) not in used] or seeds
            seed_ids = {s.id for s in seeds}
        avoid = [_trans_norm_ws(g.passage.source_zh)
                 for g in (GeneratedCet6Question.objects.filter(paper=paper)
                           .select_related('passage').order_by('-id')[:4])]
        avoid += [_trans_norm_ws(x) for x in
                  Cet6TranslationPassage.objects.filter(source_type='ai_generated')
                  .order_by('-id').values_list('source_zh', flat=True)[:6]]
        already_hit = _existing_hits()
        prompt = cet6_paper_generation_prompt(
            batch_n, length_key, difficulty, topics, skills, batch_words,
            rewrite_sources=[s.source_zh for s in seeds] or None,
            avoid_texts=avoid or None)
        data = {}
        settings_obj = UserSettings.get_settings()
        if settings_obj.cet6_translation_model_id:
            data['model_id'] = settings_obj.cet6_translation_model_id
            paper.model_id = (AIModel.objects.filter(
                id=settings_obj.cet6_translation_model_id).values_list('model_id', flat=True).first() or '')
        content, err = builtin_ai_exam_call(
            prompt, temperature=0.8, data=data, action='cet6_paper_generate',
            settings_model_field='cet6_translation_model')
        if err:
            last_err = err
            break
        last_raw = (content or '')[:8000]
        parsed = extract_json_object(content)
        raw_passages = parsed.get('passages') if isinstance(parsed, dict) else None
        dup_pool = _cet6_dup_pool(paper, seed_ids)
        kept, fb = _cet6_validate_batch(
            raw_passages, batch_n, length_key, batch_words,
            dup_pool, focus_skills=skills or None,
            already_hit=already_hit, required_words=required_so_far)

        # 不合格：带原因定向重试一次
        if fb or len(kept) != batch_n:
            paper.retry_count += 1
            paper.save(update_fields=['retry_count'])
            retry_prompt = cet6_paper_generation_prompt(
                batch_n, length_key, difficulty, topics, skills, batch_words,
                rewrite_sources=[s.source_zh for s in seeds] or None,
                avoid_texts=avoid or None,
                retry_feedback='\n'.join(fb) or '上一批段落未全部达标，请重新生成。')
            content2, err2 = builtin_ai_exam_call(
                retry_prompt, temperature=0.8, data=data,
                action='cet6_paper_generate',
                settings_model_field='cet6_translation_model')
            if err2:
                last_err = err2
                break
            last_raw = (content2 or '')[:8000]
            parsed2 = extract_json_object(content2)
            raw2 = parsed2.get('passages') if isinstance(parsed2, dict) else None
            kept2, fb2 = _cet6_validate_batch(
                raw2, batch_n, length_key, batch_words,
                dup_pool, focus_skills=skills or None,
                already_hit=already_hit, required_words=required_so_far)
            if kept2:
                kept, fb = kept2, fb2

        if not kept:
            last_err = '生成结果未通过本地校验：' + '；'.join(fb[:4])
            break

        start_order = paper.questions.count() + 1
        with transaction.atomic():
            for idx, item in enumerate(kept):
                order = start_order + idx
                note = f"AI组卷#{paper.id} {paper.get_mode_display()}"
                if cfg.get('mode') == 'rewrite' and idx < len(seeds):
                    note += f' 改写自真题段#{seeds[idx].id}#'
                cp = Cet6TranslationPassage.objects.create(
                    question=None, source_type='ai_generated', source_note=note,
                    source_zh=item['source_zh'], reference_en=item['reference_en'],
                    topic_tags=item['topic_tags'], skill_tags=item['skill_tags'],
                    key_points=item['key_points'],
                    difficulty=item['difficulty'] or difficulty, verified=False)
                tids = [word_map[w].id for w in item['target_words'] if w in word_map]
                GeneratedCet6Question.objects.create(
                    paper=paper, passage=cp, order=order,
                    quality_score=100.0 if not fb else max(40.0, 100.0 - 12 * len(fb)),
                    quality_notes='\n'.join(fb)[:1000],
                    is_approved=not fb)

    paper.duration_ms = int((time.monotonic() - t0) * 1000)
    paper.raw_response = last_raw[:8000]
    paper.prompt_hash = hashlib.sha256(
        json.dumps(cfg, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    done = paper.questions.count()
    if done >= expected:
        # 整卷复检目标表达覆盖率：达标则全部题目视为通过校验（覆盖是整卷性质）
        required = list(dict.fromkeys(target_words))
        hits = _existing_hits()
        ratio = (sum(1 for w in required if w in hits) / len(required)
                 if required else 1.0)
        if ratio >= CET6_COVER_RATIO:
            paper.questions.filter(is_approved=False).update(
                is_approved=True, quality_score=100.0, quality_notes='')
            paper.status = 'ready'
            paper.error_message = ''
        else:
            missing = [w for w in required if w not in hits]
            paper.status = 'failed'
            paper.error_message = (
                '已生成 %d/%d 段，但整卷目标表达覆盖率不足 80%%（缺失：%s）。'
                '重试将替换未达标的段落，也可降低难度/减少条件后重新组卷。'
                % (done, expected, '、'.join(missing[:6])))
    elif done > 0:
        paper.status = 'failed'
        paper.error_message = f'已生成 {done}/{expected} 段。' + (last_err or '部分段落未通过校验，请重试补齐，或降低难度/减少条件。')
    else:
        paper.status = 'failed'
        paper.error_message = last_err or 'AI 返回内容未通过本地校验，请降低难度或减少条件后重试。'
    paper.save()
    return done, paper.error_message


@require_http_methods(['GET'])
def api_cet6_recommendation(request):
    """六级组卷默认推荐配置"""
    cfg, summary = _cet6_recommendation()
    return JsonResponse({
        'success': True, 'config': cfg, 'summary': summary,
        'skill_names': {k: CET6_SKILL_TAG_NAMES[k] for k in cfg['skills']},
    })


def cet6_paper_new_page(request):
    """六级 AI 专项组卷配置页"""
    cfg, summary = _cet6_recommendation()
    recent_papers = []
    for p in GeneratedCet6Paper.objects.all()[:6]:
        recent_papers.append({
            'id': p.id, 'title': p.title, 'status': p.status,
            'mode_name': p.get_mode_display(),
            'count': p.questions.count(),
            'expected': (p.config_json or {}).get('count', 1),
            'created_at': p.created_at,
        })
    return render(request, 'cet6_paper_new.html', {
        'cfg': cfg, 'summary': summary,
        'skills': CET6_SKILL_TAGS, 'skill_names': CET6_SKILL_TAG_NAMES,
        'topics': CET6_TOPICS, 'word_sources': CET6_WORD_SOURCES,
        'counts': CET6_PAPER_COUNTS,
        'diffs': [('basic', '基础'), ('standard', '标准'), ('advanced', '拔高')],
        'lengths': [(k, v[2]) for k, v in CET6_LENGTH_RANGES.items()],
        'recent_papers': recent_papers,
        'active_nav': 'workshop', 'ws_active': 'cet6',
    })


@require_http_methods(['POST'])
def api_cet6_paper_generate(request):
    """创建并执行六级 AI 组卷（同步生成，段落分批）"""
    try:
        data = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, TypeError):
        return JsonResponse({'error': '请求格式错误'}, status=400)
    cfg, error = _cet6_build_config(data)
    if error:
        return JsonResponse({'error': error}, status=400)
    title = f'六级翻译专项卷 {timezone.localtime():%m-%d %H:%M}'
    paper = GeneratedCet6Paper.objects.create(
        title=title, mode=cfg['mode'], config_json=cfg,
        target_word_ids=cfg['target_word_ids'])
    done, err_msg = _cet6_run_paper(paper)
    return JsonResponse({
        'success': paper.status == 'ready',
        'paper_id': paper.id, 'status': paper.status,
        'created': done, 'expected': cfg['count'],
        'error': err_msg,
        'redirect': f'/workshop/cet6/papers/{paper.id}/',
    })


def cet6_paper_detail_page(request, pid):
    """六级 AI 卷详情：状态、段落列表、失败重试入口"""
    paper = get_object_or_404(GeneratedCet6Paper, id=pid)
    latest = _cet6_latest_practice_map()
    questions = []
    for gq in paper.questions.select_related('passage').order_by('order'):
        p = latest.get(gq.passage_id)
        total = _cet6_parse_total(p) if p else None
        questions.append({
            'pid': gq.passage_id, 'order': gq.order,
            'source_zh': gq.passage.source_zh,
            'has_ref': bool(gq.passage.reference_en),
            'topic_tags': gq.passage.topic_tags or [],
            'skill_tags': [CET6_SKILL_TAG_NAMES.get(t, t)
                           for t in (gq.passage.skill_tags or [])],
            'difficulty': gq.passage.difficulty,
            'quality_score': gq.quality_score,
            'is_approved': gq.is_approved,
            'status': ('new' if p is None
                       else ('error' if _cet6_is_error_practice(p) else 'ok')),
            'last_total': int(total) if total is not None else None,
        })
    cfg = paper.config_json or {}
    return render(request, 'cet6_paper_detail.html', {
        'paper': paper, 'questions': questions,
        'expected': cfg.get('count', 0),
        'mode_name': paper.get_mode_display(),
        'diff_name': CET6_DIFFICULTY_NAMES.get(cfg.get('difficulty', 3), '标准'),
        'length_name': CET6_LENGTH_RANGES.get(cfg.get('length'),
                                              ('', '', '标准段'))[2],
        'skill_names': '、'.join(CET6_SKILL_TAG_NAMES.get(t, t)
                                for t in (cfg.get('skills') or [])),
        'target_words': cfg.get('target_words') or [],
        'active_nav': 'workshop', 'ws_active': 'cet6',
    })


@require_http_methods(['POST'])
def api_cet6_paper_retry(request, pid):
    """重试失败/部分失败的六级试卷：只补齐缺失段落；
    整卷段数已够但存在未通过整卷校验的段落时，删除后重新生成替换。"""
    paper = get_object_or_404(GeneratedCet6Paper, id=pid)
    if paper.status == 'generating':
        return JsonResponse({'error': '试卷正在生成中，请勿重复提交'}, status=409)
    expected = int((paper.config_json or {}).get('count', 1))
    if paper.questions.count() >= expected:
        bad = paper.questions.filter(is_approved=False)
        if not bad.exists():
            return JsonResponse({'success': True, 'paper_id': paper.id,
                                 'status': paper.status, 'created': 0,
                                 'redirect': f'/workshop/cet6/papers/{paper.id}/'})
        # 删除未通过校验的段落（级联删除组卷题目），再按升序重排卷内序号
        Cet6TranslationPassage.objects.filter(
            id__in=list(bad.values_list('passage_id', flat=True))).delete()
        for i, gq in enumerate(
                paper.questions.select_related('passage').order_by('order'), 1):
            if gq.order != i:
                gq.order = i
                gq.save(update_fields=['order'])
    done, err_msg = _cet6_run_paper(paper)
    return JsonResponse({
        'success': paper.status == 'ready',
        'paper_id': paper.id,
        'status': paper.status,
        'created': done,
        'expected': expected,
        'error': err_msg,
        'redirect': f'/workshop/cet6/papers/{paper.id}/',
    })


@require_http_methods(['POST'])
def api_cet6_paper_delete(request, pid):
    """删除失败的六级 AI 组卷记录，避免失败草稿长期出现在最近记录中。"""
    paper = get_object_or_404(GeneratedCet6Paper, id=pid)
    if paper.status != 'failed':
        return JsonResponse({'error': '只能删除失败的组卷记录'}, status=400)
    passage_ids = list(paper.questions.values_list('passage_id', flat=True))
    if Cet6TranslationPractice.objects.filter(passage_id__in=passage_ids).exists():
        return JsonResponse({'error': '该组卷已有练习记录，不能删除，请保留后重试'}, status=409)
    paper.delete()
    return JsonResponse({'success': True, 'deleted': pid})


# ==================== 词性学习 / 词族与构词法（HANDOVER 第十二节） ====================

COMMON_POS_LABELS = ('n.', 'v.', 'adj.', 'adv.', 'prep.', 'conj.', 'pron.', 'num.', 'art.', 'int.', 'abbr.')
POS_EXERCISE_TYPES = {'pos_choice', 'pos_match', 'form_convert'}


def _resolve_pos_learning_model_id(data=None):
    """词性学习模型回退链（按方案契约）：接口显式 model_id → 词性学习模型 →
    复审模型 → 第一个已启用模型。返回 AIModel.id 或 None。"""
    data = data or {}
    explicit = data.get('model_id')
    if explicit:
        try:
            return AIModel.objects.get(id=int(explicit), enabled=True).id
        except (AIModel.DoesNotExist, ValueError, TypeError):
            return None
    settings_obj = UserSettings.get_settings()
    for field in ('pos_learning_model_id', 'review_model_id'):
        mid = getattr(settings_obj, field, None)
        if mid and AIModel.objects.filter(id=mid, enabled=True).exists():
            return mid
    first = AIModel.objects.filter(enabled=True).order_by('id').first()
    return first.id if first else None


def _extract_json_obj(text):
    """从 AI 返回文本中提取第一个 JSON 对象；容忍代码块包裹。失败返回 None。"""
    if not text:
        return None
    s = text.strip()
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', s, re.S)
    if m:
        s = m.group(1)
    start = s.find('{')
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == '{':
            depth += 1
        elif s[i] == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


def _build_local_pos_exercises(word, focus_pos=''):
    """完全基于本地词库数据生成词性客观题（无 AI 也可学、可判）。
    题型：pos_choice（释义→词性）、pos_match（词性→释义）、form_convert（词形转换）。
    只产出满足「≥2 个不重复选项且答案在选项中」的题。"""
    exercises = []
    by_pos = word.get_meanings_by_pos() or {}
    pos_labels = [p for p, ms in by_pos.items() if ms]
    distractor_pool = [p for p in COMMON_POS_LABELS if p not in pos_labels]

    # pos_choice：给释义选词性
    for pos in pos_labels:
        for m in by_pos[pos][:1]:
            options = list(pos_labels)
            for d in distractor_pool:
                if len(options) >= 4:
                    break
                options.append(d)
            options = options[:4]
            random.shuffle(options)
            exercises.append({
                'type': 'pos_choice',
                'target_pos': pos,
                'prompt': f'「{word.word}」的释义「{m}」对应的词性是？',
                'options': options,
                'answer': pos,
            })

    # pos_match：给词性选释义
    all_meanings = [(p, m) for p, ms in by_pos.items() for m in ms]
    for pos in pos_labels:
        correct_list = by_pos[pos][:1]
        others = [m for p, m in all_meanings if p != pos]
        if correct_list and others:
            options = correct_list + others[:3]
            options = list(dict.fromkeys(options))[:4]
            if len(options) >= 2:
                random.shuffle(options)
                exercises.append({
                    'type': 'pos_match',
                    'target_pos': pos,
                    'prompt': f'「{word.word}」作 {pos} 时的释义是？',
                    'options': options,
                    'answer': correct_list[0],
                })

    # form_convert：词形转换（基于 word_forms）
    forms = word.get_word_forms() or {}
    flat = []
    for k, v in forms.items():
        values = v if isinstance(v, list) else [v]
        for val in values:
            if isinstance(val, str) and val and val.lower() != word.word.lower():
                flat.append((k, val))
    if len(flat) >= 2:
        for k, val in flat[:3]:
            distractors = [v2 for k2, v2 in flat if v2 != val][:3]
            options = list(dict.fromkeys([val] + distractors))[:4]
            if len(options) >= 2:
                random.shuffle(options)
                exercises.append({
                    'type': 'form_convert',
                    'target_pos': k,
                    'prompt': f'「{word.word}」的 {k} 形式是？',
                    'options': options,
                    'answer': val,
                })

    # focus_pos 优先排前
    if focus_pos:
        exercises.sort(key=lambda e: 0 if e.get('target_pos') == focus_pos else 1)
    return exercises[:6]


def _validate_pos_lesson(payload, word):
    """校验 AI 词性讲解 JSON（方案第四节）。返回 (clean, warnings) 或 (None, [错误])。"""
    warnings = []
    if not isinstance(payload, dict):
        return None, ['返回不是 JSON 对象']
    if payload.get('word') != word.word:
        return None, ['word 字段与请求目标不一致']
    allowed_pos = set((word.get_meanings_by_pos() or {}).keys()) | set(COMMON_POS_LABELS)
    if word.pos:
        allowed_pos.add(word.pos)

    clean = {'lesson_version': payload.get('lesson_version', 1), 'word': word.word}

    pos_groups = payload.get('pos_groups') or []
    if not isinstance(pos_groups, list):
        return None, ['pos_groups 不是数组']
    groups_out = []
    for g in pos_groups[:6]:
        if not isinstance(g, dict):
            continue
        pos = (g.get('pos') or '').strip()
        if pos not in allowed_pos:
            warnings.append(f'忽略未知词性标签 {pos}')
            continue
        groups_out.append({
            'pos': pos,
            'meaning': str(g.get('meaning') or '')[:200],
            'usage': str(g.get('usage') or '')[:300],
            'memory_tip': str(g.get('memory_tip') or '')[:200],
        })
    clean['pos_groups'] = groups_out

    confs_out = []
    for c in (payload.get('confusions') or [])[:3]:
        if isinstance(c, dict) and c.get('word'):
            try:
                conf = float(c.get('confidence', 0.5))
            except (TypeError, ValueError):
                conf = 0.5
            confs_out.append({
                'word': str(c['word'])[:60],
                'difference': str(c.get('difference') or '')[:300],
                'confidence': max(0.0, min(1.0, conf)),
            })
    clean['confusions'] = confs_out

    pats_out = []
    for p in (payload.get('patterns') or [])[:3]:
        if isinstance(p, dict) and p.get('name'):
            pats_out.append({
                'name': str(p['name'])[:40],
                'rule': str(p.get('rule') or '')[:300],
                'exception': str(p.get('exception') or '')[:300],
            })
    clean['patterns'] = pats_out

    exs_out = []
    for e in (payload.get('exercises') or [])[:3]:
        if not isinstance(e, dict):
            continue
        etype = e.get('type')
        if etype not in POS_EXERCISE_TYPES:
            warnings.append(f'忽略未知题型 {etype}')
            continue
        options = [str(o)[:100] for o in (e.get('options') or []) if str(o).strip()]
        options = list(dict.fromkeys(options))
        answer = str(e.get('answer') or '')[:100]
        prompt = str(e.get('prompt') or '')[:300]
        if len(options) < 2 or answer not in options or not prompt:
            warnings.append('忽略一道选项/答案不合法的客观题')
            continue
        exs_out.append({'type': etype, 'prompt': prompt, 'options': options,
                        'answer': answer, 'target_pos': str(e.get('target_pos') or '')[:20]})
    clean['exercises'] = exs_out
    return clean, warnings


@require_http_methods(['POST'])
def api_pos_lesson(request):
    """生成或读取词性讲解与客观题候选。
    请求：{word_id, focus_pos?, model_id?, use_ai?}
    - use_ai 为假或未配置模型：只返回本地讲解与本地客观题（保证无 AI 可学）。
    - use_ai 为真：调用 AI 生成讲解；同词同提示词版本命中缓存直接返回。
    失败（模型不可用/非 JSON/校验不过）不写任何学习数据，返回可读错误。"""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': '请求体不是合法 JSON'}, status=400)
    word = get_object_or_404(Word, id=data.get('word_id'))
    focus_pos = (data.get('focus_pos') or '').strip()[:20]
    use_ai = bool(data.get('use_ai'))

    progress = StudyProgress.objects.filter(word=word).first()
    uncommon = parse_uncommon_pos(progress.uncommon_pos) if progress else []
    by_pos = word.get_meanings_by_pos() or {}

    local_lesson = {
        'pos_groups': [
            {'pos': p, 'meaning': '；'.join(ms), 'usage': '', 'memory_tip': '',
             'is_uncommon': p in uncommon}
            for p, ms in by_pos.items() if ms
        ],
        'word_forms': word.get_word_forms() or {},
        'example_en': word.example_en or '',
    }
    exercises = _build_local_pos_exercises(word, focus_pos)

    resp = {
        'success': True,
        'word_id': word.id,
        'word': word.word,
        'lesson': local_lesson,
        'exercises': exercises,
        'model_id': '',
        'cache_hit': False,
        'content_version': '',
        'warnings': [],
    }

    if not use_ai:
        return JsonResponse(resp)

    model_pk = _resolve_pos_learning_model_id(data)
    if not model_pk:
        return JsonResponse({
            'error': '未配置可用 AI 模型；本地讲解与练习仍可学习',
            'fallback': resp,
        }, status=400)
    model_row = AIModel.objects.get(id=model_pk)
    resp['model_id'] = model_row.model_id

    cached = PosLessonCache.objects.filter(
        word=word, prompt_version=POS_LESSON_PROMPT_VERSION).first()
    if cached:
        resp['ai_lesson'] = cached.content
        resp['cache_hit'] = True
        resp['content_version'] = cached.prompt_version
        return JsonResponse(resp)

    prompt = pos_lesson_prompt(
        word.word, by_pos, focus_pos=focus_pos,
        uncommon_pos=uncommon, example_en=word.example_en or '')
    content, err = builtin_ai_exam_call(
        prompt, temperature=0.3, data={'model_id': model_pk}, action='pos_lesson')
    if err:
        return JsonResponse({
            'error': f'AI 生成失败：{err}',
            'fallback': {k: resp[k] for k in ('success', 'word_id', 'word', 'lesson', 'exercises')},
        }, status=502)
    payload = _extract_json_obj(content)
    clean, warns = _validate_pos_lesson(payload, word) if payload else (None, ['AI 返回不是合法 JSON'])
    if clean is None:
        return JsonResponse({
            'error': 'AI 输出未通过校验：' + '；'.join(warns),
            'fallback': {k: resp[k] for k in ('success', 'word_id', 'word', 'lesson', 'exercises')},
        }, status=502)

    PosLessonCache.objects.update_or_create(
        word=word,
        defaults={'content': clean, 'model_id': model_row.model_id,
                  'prompt_version': POS_LESSON_PROMPT_VERSION})
    resp['ai_lesson'] = clean
    resp['content_version'] = POS_LESSON_PROMPT_VERSION
    resp['warnings'] = warns
    return JsonResponse(resp)


def _norm_answer(s):
    return re.sub(r'\s+', ' ', (s or '').strip()).lower()


@require_http_methods(['POST'])
def api_pos_grade(request):
    """判定词性作答。
    请求：{attempt_id?, word_id?, question?, answer, model_id?}
    - 客观题（pos_choice/pos_match/form_convert）本地判分；
    - 开放题（open）调用 AI 判定（8 秒短超时），verdict 仅接受 correct/partial/wrong；
    - AI 不可用/超时/输出非法时落「待复核」，不判错、不写任何学习数据；
    - 仅**最终确认**的 wrong 写 StudyRecord(mistake_type='pos')；partial 不加风险。"""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': '请求体不是合法 JSON'}, status=400)

    question = data.get('question') or {}
    qtype = question.get('type') or ''
    answer = str(data.get('answer') or '')

    attempt = None
    reason_code = ''
    if data.get('attempt_id'):
        attempt = PosLearningAttempt.objects.filter(id=data['attempt_id']).first()
        if attempt is None:
            return JsonResponse({'error': '作答记录不存在'}, status=404)
        if attempt.result:
            return JsonResponse({
                'success': True, 'result': attempt.result, 'judged_by': attempt.judged_by,
                'feedback': attempt.feedback, 'reason_code': attempt.reason_code,
                'idempotent': True})
        word = attempt.word
        question = attempt.question or question
        qtype = attempt.question_type
    else:
        word = get_object_or_404(Word, id=data.get('word_id'))
        if qtype not in POS_EXERCISE_TYPES and qtype != 'open':
            return JsonResponse({'error': '未知题型'}, status=400)

    if qtype == 'open':
        if not answer.strip():
            return JsonResponse({'error': '答案为空'}, status=400)
        model_pk = _resolve_pos_learning_model_id(data)
        result = 'pending_review'
        judged_by = 'pending'
        feedback = '已保存，待 AI 可用后复核'
        if not model_pk:
            # 无可用模型：落待复核，不再整次失败
            reason_code = 'ai_no_model'
        else:
            prompt = pos_open_grade_prompt(
                word.word, question.get('target_pos', ''),
                question.get('prompt', ''), question.get('reference', ''), answer)
            content, err = None, ''
            try:
                cfg = resolve_ai_model({'model_id': model_pk})
                import time as _time
                t0 = _time.monotonic()
                content, err, reason = _ai_judge_call(
                    cfg, prompt, max_tokens=600, temperature=0.1)
                _log_ai_call('pos_grade', None, cfg.get('model_id') or '', bool(content),
                             err or '', prompt, t0,
                             int((_time.monotonic() - t0) * 1000), response=content or '')
            except ValueError:
                reason = 'ai_no_model'
            if not content:
                reason_code = reason or 'ai_unavailable'
            else:
                payload = _extract_json_obj(content)
                verdict = (payload or {}).get('verdict')
                if verdict not in ('correct', 'partial', 'wrong'):
                    reason_code = 'ai_invalid_output'
                else:
                    result, judged_by, reason_code = verdict, 'ai', ''
                    feedback = str(payload.get('feedback') or '')[:200]
    else:
        correct_ans = question.get('answer')
        if correct_ans is None:
            return JsonResponse({'error': '题目缺少标准答案'}, status=400)
        result = 'correct' if _norm_answer(answer) == _norm_answer(str(correct_ans)) else 'wrong'
        judged_by = 'local'
        feedback = ''

    is_pending = result == 'pending_review'

    # 保存/更新作答记录
    if attempt is None:
        attempt = PosLearningAttempt.objects.create(
            word=word, target_pos=str(question.get('target_pos') or '')[:20],
            question_type=qtype, question=question,
            user_answer=answer, result=result, judged_by=judged_by, feedback=feedback,
            pending_review=is_pending, reason_code=reason_code if is_pending else '')
    else:
        attempt.user_answer = answer
        attempt.result = result
        attempt.judged_by = judged_by
        attempt.feedback = feedback
        attempt.pending_review = is_pending
        attempt.reason_code = reason_code if is_pending else ''
        attempt.save(update_fields=['user_answer', 'result', 'judged_by', 'feedback',
                                    'pending_review', 'reason_code'])

    if is_pending:
        # 待复核：只保存作答与题面供后续补判/人工改判，绝不写错误事件与进度
        pending = _get_or_create_pending_judgment(
            word, 'pos_open', answer,
            question={'type': qtype,
                      'target_pos': str(question.get('target_pos') or '')[:20],
                      'prompt': str(question.get('prompt') or '')[:300],
                      'reference': str(question.get('reference') or '')[:300]},
            mode='pos_open', source='pos', reason_code=reason_code, attempt=attempt,
            dedupe_key=_pending_dedupe_key(
                'pos_open', 'pos', 'pos_open', word.id, _ensure_session_key(request)))
        return JsonResponse({
            'success': True, 'attempt_id': attempt.id, 'result': 'pending_review',
            'judged_by': judged_by, 'feedback': feedback,
            'reason_code': reason_code, 'pending_id': pending.id,
        })

    # 仅最终确认的 wrong 写错误事件（服务端唯一写入点），与背诵/考试错题同口径
    if result == 'wrong':
        _apply_wrong_progress(word)
        _record_study(
            word, 'meaning_wrong', {'source': 'pos'},
            mode=f'pos_{qtype}', mistake_type='pos',
            error_detail={
                'target_pos': str(question.get('target_pos') or '')[:20],
                'question_type': qtype,
                'user_answer': answer[:200],
                'correct': str(question.get('answer') or question.get('reference') or '')[:200],
                'judged_by': judged_by,
            })

    return JsonResponse({
        'success': True, 'attempt_id': attempt.id, 'result': result,
        'judged_by': judged_by, 'feedback': feedback,
    })


# ─── 待复核队列（HANDOVER 第八节相位 3）───────────────────────
PENDING_REASON_LABELS = {
    'ai_timeout': 'AI 判定超时',
    'ai_network_error': '网络不通',
    'ai_rate_limited': '请求过于频繁',
    'ai_server_error': 'AI 服务端错误',
    'ai_config_error': 'AI 配置错误（密钥/模型/地址）',
    'ai_invalid_output': 'AI 输出不合法',
    'ai_circuit_open': 'AI 暂不可用（熔断冷却中）',
    'ai_conflict': 'AI 判定冲突',
    'ai_no_model': '未配置可用 AI 模型',
    'ai_disabled': '已关闭 AI 判定',
    'ai_unavailable': 'AI 暂不可用',
}


def _serialize_pending_judgment(pj):
    """待复核记录 → 前端 JSON（不含任何密钥）。"""
    return {
        'id': pj.id,
        'word_id': pj.word_id,
        'word': pj.word.word if pj.word else '',
        'meanings': pj.word.get_meanings() if pj.word else [],
        'kind': pj.kind,
        'kind_display': pj.get_kind_display(),
        'user_answer': pj.user_answer,
        'reason_code': pj.reason_code,
        'reason_label': PENDING_REASON_LABELS.get(pj.reason_code, '待复核'),
        'source': pj.source,
        'mode': pj.mode,
        'created_at': pj.created_at.isoformat(),
    }


def pending_reviews_page(request):
    """待复核列表页：AI 不可用期间暂存的判定，可一键补判或人工改判。"""
    return render(request, 'pending_reviews.html', {
        'reason_labels': PENDING_REASON_LABELS,
    })


@require_http_methods(['GET'])
def api_pending_judgments(request):
    """未复核的待复核列表（最多 500 条）。"""
    qs = PendingJudgment.objects.filter(resolved=False).select_related('word')
    return JsonResponse({
        'items': [_serialize_pending_judgment(pj) for pj in qs.order_by('-created_at')[:500]],
        'count': qs.count(),
    })


def _rejudge_pos_open(pj, data):
    """词性开放题待复核记录重新判定（沿用 8 秒短超时判定通道）。"""
    q = pj.question or {}
    model_pk = _resolve_pos_learning_model_id(data)
    if not model_pk:
        return _policy_pending('ai_no_model', '已保存，待配置可用 AI 模型后复核')
    try:
        cfg = resolve_ai_model({'model_id': model_pk})
    except ValueError:
        return _policy_pending('ai_no_model', '已保存，待配置可用 AI 模型后复核')
    prompt = pos_open_grade_prompt(pj.word.word, q.get('target_pos', ''),
                                   q.get('prompt', ''), q.get('reference', ''),
                                   pj.user_answer)
    content, _err, reason = _ai_judge_call(cfg, prompt, max_tokens=600, temperature=0.1)
    if not content:
        return _policy_pending(reason)
    payload = _extract_json_obj(content) or {}
    verdict = payload.get('verdict')
    if verdict not in ('correct', 'partial', 'wrong'):
        return _policy_pending('ai_invalid_output')
    return {'verdict': verdict, 'source': 'ai', 'is_final': True, 'reason_code': '',
            'confidence': 'medium', 'comment': str(payload.get('feedback') or '')[:200]}


def _rejudge_pending(pj, data):
    """按待复核类型选择补判通道。"""
    if pj.kind == 'pos_open':
        return _rejudge_pos_open(pj, data)
    return judge_with_policy(pj.word, pj.user_answer, data)


@require_http_methods(['POST'])
def api_pending_judgment_rejudge(request, pj_id):
    """单题 AI 补判：连接恢复后重新判定并幂等落库（熔断中保持待复核）。"""
    pj = PendingJudgment.objects.filter(id=pj_id).select_related('word').first()
    if pj is None:
        return JsonResponse({'error': '待复核记录不存在'}, status=404)
    if pj.resolved:
        return JsonResponse({'success': True, 'result': pj.final_verdict,
                             'message': '已复核', 'idempotent': True})
    try:
        data = json.loads(request.body or '{}')
    except Exception:
        data = {}
    policy = _rejudge_pending(pj, data)
    if policy['verdict'] == 'pending_review':
        if pj.reason_code != policy['reason_code']:
            pj.reason_code = policy['reason_code']
            pj.save(update_fields=['reason_code'])
        return JsonResponse({
            'success': False, 'result': 'pending_review',
            'reason_code': policy['reason_code'],
            'message': policy['comment'] or PENDING_REASON_LABELS.get(
                policy['reason_code'], '仍待复核')})
    ok, message = _finalize_pending_judgment(
        pj, policy['verdict'], policy['comment'], policy['source'], 'ai', data)
    return JsonResponse({'success': ok, 'result': policy['verdict'],
                         'message': message, 'comment': policy['comment']})


@require_http_methods(['POST'])
def api_pending_judgment_manual(request, pj_id):
    """人工改判（优先级最高）：直接给定 correct/partial/wrong，幂等只落库一次。"""
    pj = PendingJudgment.objects.filter(id=pj_id).select_related('word').first()
    if pj is None:
        return JsonResponse({'error': '待复核记录不存在'}, status=404)
    try:
        data = json.loads(request.body or '{}')
    except Exception:
        return JsonResponse({'error': '请求体不是合法 JSON'}, status=400)
    verdict = (data.get('verdict') or '').strip()
    if verdict not in ('correct', 'partial', 'wrong'):
        return JsonResponse({'error': '请输入 correct / partial / wrong'}, status=400)
    ok, message = _finalize_pending_judgment(pj, verdict, '人工改判', 'manual', 'manual', data)
    return JsonResponse({'success': ok, 'result': verdict,
                         'message': message if message != '已复核' else '已改判'})


@require_http_methods(['POST'])
def api_pending_judgment_rejudge_all(request):
    """批量补判：逐条重判；熔断中的题保持待复核并立即停止，避免请求风暴。"""
    try:
        data = json.loads(request.body or '{}')
    except Exception:
        data = {}
    items = list(PendingJudgment.objects.filter(resolved=False)
                 .select_related('word').order_by('created_at')[:200])
    resolved, still_pending, failed = 0, 0, 0
    for pj in items:
        policy = _rejudge_pending(pj, data)
        if policy['verdict'] == 'pending_review':
            still_pending += 1
            if pj.reason_code != policy['reason_code']:
                pj.reason_code = policy['reason_code']
                pj.save(update_fields=['reason_code'])
            if policy['reason_code'] == 'ai_circuit_open':
                break
            continue
        ok, _message = _finalize_pending_judgment(
            pj, policy['verdict'], policy['comment'], policy['source'], 'ai', data)
        if ok:
            resolved += 1
        else:
            failed += 1
    return JsonResponse({
        'success': True, 'resolved': resolved, 'pending': still_pending,
        'failed': failed,
        'remaining': PendingJudgment.objects.filter(resolved=False).count()})


def _validate_family_candidate(payload):
    """校验 AI 词族候选 JSON。返回 (clean, warnings) 或 (None, [错误])。"""
    warnings = []
    if not isinstance(payload, dict):
        return None, ['返回不是 JSON 对象']
    fam = payload.get('family') or {}
    key = str(fam.get('key') or '').strip()[:60]
    title = str(fam.get('title') or '').strip()[:120]
    if not key or not title:
        return None, ['family.key / family.title 缺失']
    clean = {
        'family': {'key': key, 'title': title,
                   'description': str(fam.get('description') or '')[:400]},
        'members': [], 'patterns': [], 'relations': [],
    }
    members = payload.get('members') or []
    if not isinstance(members, list) or not members:
        return None, ['members 为空或不是数组']
    valid_rel = {'core', 'derived', 'inflection', 'compound'}
    for m in members[:8]:
        if not isinstance(m, dict) or not m.get('word'):
            continue
        rel = m.get('relation') if m.get('relation') in valid_rel else 'derived'
        clean['members'].append({
            'word': str(m['word']).strip()[:100],
            'pos': str(m.get('pos') or '')[:20],
            'meaning': str(m.get('meaning') or '').strip()[:100],
            'relation': rel,
            'note': str(m.get('note') or '')[:200],
        })
    if not any(m['relation'] == 'core' for m in clean['members']):
        return None, ['members 缺少核心词（relation=core）']
    valid_kind = {'prefix', 'suffix', 'root'}
    for p in (payload.get('patterns') or [])[:4]:
        if isinstance(p, dict) and p.get('name'):
            clean['patterns'].append({
                'name': str(p['name'])[:40],
                'kind': p['kind'] if p.get('kind') in valid_kind else 'suffix',
                'from_pos': str(p.get('from_pos') or '')[:20],
                'to_pos': str(p.get('to_pos') or '')[:20],
                'rule': str(p.get('rule') or '')[:300],
                'exception': str(p.get('exception') or '')[:300],
            })
    valid_reltype = {'confusing', 'collocation', 'synonym', 'antonym', 'derived'}
    for r in (payload.get('relations') or [])[:4]:
        if not isinstance(r, dict) or not r.get('word'):
            continue
        try:
            conf = max(0.0, min(1.0, float(r.get('confidence', 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        clean['relations'].append({
            'word': str(r['word']).strip()[:100],
            'rel_type': r['rel_type'] if r.get('rel_type') in valid_reltype else 'confusing',
            'note': str(r.get('note') or '')[:200],
            'confidence': conf,
        })
    return clean, warnings


def _normalize_family_key(raw):
    """规范化词族标识：去空白、统一小写、空白折叠为连字符，长度对齐模型字段。"""
    k = re.sub(r'\s+', '-', (raw or '').strip().lower())
    return k[:60]


@require_http_methods(['GET'])
def api_word_family_get(request, word_id):
    """返回某词的词族变式（同根派生词，如 operate → operation / operator / operational）。
    优先取已人工确认的词族，其次取 AI 候选；无则 family 为 null。"""
    word = get_object_or_404(Word, id=word_id)
    memberships = list(
        WordFamilyMember.objects.filter(word=word).select_related('family'))
    if not memberships:
        return JsonResponse({'success': True, 'family': None,
                             'members': [], 'patterns': []})
    # 已确认优先，其次取最早候选
    memberships.sort(key=lambda m: (m.family.confirmed_at is None, m.family.id))
    family = memberships[0].family
    members = []
    for m in family.members.select_related('word').order_by('sort_order', 'id'):
        # 库内词优先取词库释义（权威、可随编辑更新），库外词用 AI 生成时保存的释义
        lib_meaning = '；'.join(m.word.get_meanings()) if m.word_id else ''
        meaning = (lib_meaning or m.meaning or '').strip()
        members.append({
            'word': m.word.word if m.word_id else (m.outside_word or ''),
            'in_library': bool(m.word_id),
            'pos': m.pos,
            'meaning': meaning[:60],
            'relation': m.relation,
            'relation_label': m.get_relation_display(),
            'note': m.note,
            'is_core': m.relation == 'core',
        })
    patterns = [
        {
            'name': p.name, 'kind': p.kind, 'kind_label': p.get_kind_display(),
            'rule': p.rule, 'exception': p.exception,
        }
        for p in family.patterns.order_by('id')
    ]
    return JsonResponse({
        'success': True,
        'family': {
            'key': family.key, 'title': family.title,
            'description': family.description,
            'is_confirmed': family.confirmed_at is not None,
            'source': family.source,
        },
        'members': members,
        'patterns': patterns,
    })


@require_http_methods(['POST'])
def api_word_family_candidates(request):
    """生成待确认词族/构词法/易混关系候选。
    请求：{word_id, model_id?}
    成功：候选落库为未确认状态（source=ai_candidate，confirmed_at 空 / is_confirmed=False），
    词库不存在的成员标记 outside_word（绝不暗中创建 Word）。
    失败（无模型/非 JSON/校验不过）：零写入，返回可读错误。"""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': '请求体不是合法 JSON'}, status=400)
    word = get_object_or_404(Word, id=data.get('word_id'))

    model_pk = _resolve_pos_learning_model_id(data)
    if not model_pk:
        return JsonResponse({'error': '未配置可用 AI 模型，无法生成候选；未写入任何数据'}, status=400)
    model_row = AIModel.objects.get(id=model_pk)

    # 词库中可能相关的词：同前缀（前 4 字母）或词形变化中出现的
    prefix = word.word[:4].lower()
    related_qs = Word.objects.filter(word__istartswith=prefix).exclude(id=word.id)
    candidate_words = list(related_qs.values_list('word', flat=True)[:40])

    prompt = word_family_candidate_prompt(
        word.word, word.pos, word.get_word_forms() or {},
        word.get_meanings(), candidate_words)
    content, err = builtin_ai_exam_call(
        prompt, temperature=0.3, data={'model_id': model_pk},
        action='word_family_candidate')
    if err:
        return JsonResponse({'error': f'AI 生成失败：{err}；未写入任何数据'}, status=502)
    payload = _extract_json_obj(content)
    clean, warns = _validate_family_candidate(payload) if payload else (None, ['AI 返回不是合法 JSON'])
    if clean is None:
        return JsonResponse({'error': 'AI 输出未通过校验：' + '；'.join(warns) + '；未写入任何数据'}, status=502)

    word_map = {w.word: w for w in Word.objects.filter(
        word__in=[m['word'] for m in clean['members']] +
                 [r['word'] for r in clean['relations']])}

    normalized_key = _normalize_family_key(clean['family']['key'])
    clean['family']['key'] = normalized_key
    with transaction.atomic():
        # 写前规范化 key + get_or_create/唯一冲突兜底：已有同 key 词族一律不覆盖，
        # 转为可读的成功复用响应，避免唯一约束把 AI 成功后页面打成 500。
        existing = WordFamily.objects.filter(key=normalized_key).first()
        if existing is not None:
            return JsonResponse({
                'success': True, 'reused': True, 'candidate_id': existing.id,
                'key': existing.key, 'model_id': model_row.model_id,
                'warnings': warns + ['已存在同标识词族，未覆盖既有内容；未新建候选'],
            })
        try:
            family = WordFamily.objects.create(
                key=normalized_key, title=clean['family']['title'],
                description=clean['family']['description'],
                source='ai_candidate', confirmed_at=None)
        except IntegrityError:
            existing = WordFamily.objects.filter(key=normalized_key).first()
            return JsonResponse({
                'success': True, 'reused': True,
                'candidate_id': existing.id if existing else None,
                'key': normalized_key, 'model_id': model_row.model_id,
                'warnings': warns + ['并发写入冲突：已存在同标识词族，未覆盖'],
            })
        outside = []
        for i, m in enumerate(clean['members']):
            w = word_map.get(m['word'])
            if w is None:
                outside.append(m['word'])
            WordFamilyMember.objects.create(
                family=family, word=w, outside_word='' if w else m['word'],
                pos=m['pos'], meaning=m['meaning'], relation=m['relation'], note=m['note'],
                sort_order=i, source='ai_candidate', confirmed_at=None)
        for p in clean['patterns']:
            WordFormationPattern.objects.create(
                family=family, name=p['name'], kind=p['kind'],
                from_pos=p['from_pos'], to_pos=p['to_pos'],
                rule=p['rule'], exception=p['exception'],
                confidence=0.5, is_confirmed=False, source='ai_candidate')
        for r in clean['relations']:
            w = word_map.get(r['word'])
            if w is None:
                outside.append(r['word'])
            WordRelation.objects.create(
                family=family, word=word, related_word=w,
                outside_word='' if w else r['word'],
                rel_type=r['rel_type'], note=r['note'],
                confidence=r['confidence'], is_confirmed=False, source='ai_candidate')
        # confirm=1：生成即当正式数据入库（背诵页/详情页触发时使用），跳过候选确认流程
        if data.get('confirm'):
            now = timezone.now()
            family.confirmed_at = now
            family.save(update_fields=['confirmed_at'])
            family.members.update(confirmed_at=now)
            family.patterns.update(is_confirmed=True)
            family.relations.update(is_confirmed=True)

    if outside:
        warns.append('以下词不在词库中，已标记为「词库外示例」：' + '、'.join(sorted(set(outside))))
    return JsonResponse({
        'success': True, 'candidate_id': family.id, 'model_id': model_row.model_id,
        'confirmed': bool(data.get('confirm')),
        'cache_hit': False, 'content_version': '', 'warnings': warns,
        'candidate': {
            'family': clean['family'],
            'members': clean['members'],
            'patterns': clean['patterns'],
            'relations': clean['relations'],
        },
    })


@require_http_methods(['POST'])
def api_word_family_candidate_confirm(request, cid):
    """人工确认 / 拒绝 / 编辑候选批次（WordFamily 及其成员、构词法、关系）。
    请求：{action: confirm|reject|edit, payload?}
    - confirm：整批确认（confirmed_at=now / is_confirmed=True），此后可用于正式推荐；
    - reject：整批删除，不留库；
    - edit：按 payload 调整 family 标题/说明后再确认。"""
    family = get_object_or_404(WordFamily, id=cid)
    if family.source != 'ai_candidate' or family.confirmed_at is not None:
        return JsonResponse({'error': '该记录不是待确认候选'}, status=400)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': '请求体不是合法 JSON'}, status=400)
    action = data.get('action')

    if action == 'reject':
        family.delete()  # 级联删除成员/构词法/关系
        return JsonResponse({'success': True, 'rejected': cid})
    if action not in ('confirm', 'edit'):
        return JsonResponse({'error': 'action 必须是 confirm / reject / edit'}, status=400)

    if action == 'edit':
        payload = data.get('payload') or {}
        if payload.get('title'):
            family.title = str(payload['title'])[:120]
        if payload.get('description') is not None:
            family.description = str(payload['description'])[:400]
        family.save(update_fields=['title', 'description'])

    now = timezone.now()
    with transaction.atomic():
        family.confirmed_at = now
        family.save(update_fields=['confirmed_at'])
        family.members.update(confirmed_at=now)
        family.patterns.update(is_confirmed=True)
        family.relations.update(is_confirmed=True)
    return JsonResponse({'success': True, 'confirmed': cid})


@require_http_methods(['POST'])
def api_knowledge_feedback(request):
    """写入知识反馈：重点/已掌握/不相关/暂停。
    请求：{target_type, target_id, action}
    「不相关」会立刻屏蔽对应未确认候选/关系（is_confirmed=False）。"""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': '请求体不是合法 JSON'}, status=400)
    target_type = data.get('target_type')
    action = data.get('action')
    valid_targets = {c[0] for c in KnowledgeFeedback.TARGET_CHOICES}
    valid_actions = {c[0] for c in KnowledgeFeedback.ACTION_CHOICES}
    if target_type not in valid_targets or action not in valid_actions:
        return JsonResponse({'error': 'target_type 或 action 不合法'}, status=400)
    try:
        target_id = int(data.get('target_id'))
    except (TypeError, ValueError):
        return JsonResponse({'error': 'target_id 必须是整数'}, status=400)

    target_model = {
        'word': Word, 'family': WordFamily, 'member': WordFamilyMember,
        'pattern': WordFormationPattern, 'relation': WordRelation,
    }[target_type]
    if not target_model.objects.filter(id=target_id).exists():
        return JsonResponse({'error': '目标对象不存在'}, status=404)

    fb = KnowledgeFeedback.objects.create(
        target_type=target_type, target_id=target_id, action=action)

    # 「不相关」立即屏蔽该候选/关系
    if action == 'irrelevant' and target_type in ('relation', 'pattern', 'member'):
        obj = target_model.objects.get(id=target_id)
        if hasattr(obj, 'is_confirmed'):
            obj.is_confirmed = False
            obj.save(update_fields=['is_confirmed'])
        elif hasattr(obj, 'confirmed_at'):
            obj.confirmed_at = None
            obj.save(update_fields=['confirmed_at'])

    return JsonResponse({'success': True, 'feedback_id': fb.id})


# ==================== AI 词性扩展（HANDOVER 第十四节：背诵页 / 今日自选入口） ====================

def _word_needs_pos_expand(word):
    """是否需要 AI 扩展词性：无按词性释义，或全部归「未分类」。已有有效分组的一律不覆盖。"""
    existing = word.get_meanings_by_pos()
    return not existing or all(k == '未分类' for k in existing)


def _validate_pos_grouping(item, word):
    """校验 AI 按词性分组结果：词性标签白名单过滤（COMMON_POS_LABELS ∪ 该词 pos 字段）、
    释义统一转字符串截断 100 字。返回 (cleaned|None, warnings)。"""
    warnings = []
    if not isinstance(item, dict) or not item:
        return None, warnings
    allowed = set(COMMON_POS_LABELS) | {p for p in (word.pos or '').split() if p}
    cleaned = {}
    for pos, meanings in item.items():
        p = str(pos).strip()
        if p == '未分类' or p not in allowed:
            warnings.append(f'已丢弃非标准词性标签「{p}」')
            continue
        if not isinstance(meanings, list):
            meanings = [meanings]
        ms = [str(m).strip()[:100] for m in meanings if str(m).strip()]
        if ms:
            cleaned[p] = ms
    if not cleaned:
        return None, warnings
    return cleaned, warnings


def _pos_expand_lines(words):
    return ['%s | %s | %s' % (w.word, w.pos or '', '；'.join(w.get_meanings()))
            for w in words]


@require_http_methods(['POST'])
def api_word_pos_expand(request, word_id):
    """单个词 AI 扩展词性（背诵页入口）：仅补缺，不覆盖已有按词性释义。
    请求：{model_id?}；返回：{success, updated, meanings_by_pos, skipped_reason?, warnings?}"""
    word = get_object_or_404(Word, id=word_id)
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        data = {}
    if not _word_needs_pos_expand(word):
        return JsonResponse({
            'success': True, 'updated': False,
            'skipped_reason': '该词已有按词性释义，未做改动',
            'meanings_by_pos': word.get_meanings_by_pos(),
        })
    model_id = _resolve_pos_learning_model_id(data)
    if not model_id:
        return JsonResponse({'error': '未配置可用 AI 模型，请先在「设置 → AI模型」中配置'}, status=400)
    content, err = builtin_ai_exam_call(
        pos_grouping_prompt(_pos_expand_lines([word])), temperature=0.1,
        data={'model_id': model_id}, action='pos_expand',
        settings_model_field='pos_learning_model')
    if err:
        return JsonResponse({'error': f'AI 调用失败：{err}'}, status=502)
    payload = _extract_json_obj(content)
    if not isinstance(payload, dict):
        return JsonResponse({'error': 'AI 返回格式异常，未写入任何内容'}, status=502)
    cleaned, warnings = _validate_pos_grouping(payload.get(word.word), word)
    if cleaned is None:
        return JsonResponse({'error': 'AI 未返回该词的有效词性分组，未写入任何内容',
                             'warnings': warnings}, status=502)
    word.meanings_by_pos = json.dumps(cleaned, ensure_ascii=False)
    word.save(update_fields=['meanings_by_pos'])
    return JsonResponse({'success': True, 'updated': True,
                         'meanings_by_pos': cleaned, 'warnings': warnings})


@require_http_methods(['POST'])
def api_daily_pick_pos_expand(request):
    """批量扩展今日自选清单中缺词性释义的词（30个/批，同步执行，失败零写入）。
    请求：{model_id?}；返回：{success, total, updated, failed, skipped, warnings?}"""
    rollover_daily_picks()
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        data = {}
    words = [p.word for p in
             DailyPick.objects.filter(date=timezone.localdate()).select_related('word')]
    need = [w for w in words if _word_needs_pos_expand(w)]
    skipped = len(words) - len(need)
    if not words:
        return JsonResponse({'error': '今日自选清单为空'}, status=400)
    if not need:
        return JsonResponse({'success': True, 'total': len(words), 'updated': 0,
                             'failed': 0, 'skipped': skipped})
    model_id = _resolve_pos_learning_model_id(data)
    if not model_id:
        return JsonResponse({'error': '未配置可用 AI 模型，请先在「设置 → AI模型」中配置'}, status=400)
    updated = 0
    failed = 0
    warnings = []
    batch = 30
    for i in range(0, len(need), batch):
        chunk = need[i:i + batch]
        content, err = builtin_ai_exam_call(
            pos_grouping_prompt(_pos_expand_lines(chunk)), temperature=0.1,
            data={'model_id': model_id}, action='pos_expand',
            settings_model_field='pos_learning_model')
        if err:
            failed += len(chunk)
            continue
        payload = _extract_json_obj(content)
        if not isinstance(payload, dict):
            failed += len(chunk)
            continue
        for w in chunk:
            cleaned, ws = _validate_pos_grouping(payload.get(w.word), w)
            warnings.extend(ws)
            if cleaned is None:
                failed += 1
                continue
            w.meanings_by_pos = json.dumps(cleaned, ensure_ascii=False)
            w.save(update_fields=['meanings_by_pos'])
            updated += 1
    return JsonResponse({'success': True, 'total': len(words), 'updated': updated,
                         'failed': failed, 'skipped': skipped, 'warnings': warnings[:5]})


def pos_learning_page(request, word_id):
    """词性学习页：本地讲解 + 客观题 + 开放题 + AI 讲解/词族候选入口。"""
    word = get_object_or_404(Word, id=word_id)
    progress = StudyProgress.objects.filter(word=word).first()
    recent_attempts = PosLearningAttempt.objects.filter(word=word)[:10]
    return render(request, 'pos_learning.html', {
        'word': word,
        'uncommon_pos': parse_uncommon_pos(progress.uncommon_pos) if progress else [],
        'recent_attempts': recent_attempts,
        'has_ai_model': _resolve_pos_learning_model_id({}) is not None,
    })


def word_family_candidates_page(request):
    """候选确认队列：列出全部待确认的词族候选批次。"""
    families = (WordFamily.objects
                .filter(source='ai_candidate', confirmed_at__isnull=True)
                .prefetch_related('members__word', 'patterns', 'relations__related_word')
                .order_by('-created_at'))
    return render(request, 'word_family_candidates.html', {'families': families})
