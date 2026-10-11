import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import urllib.error
import base64
from datetime import date, timedelta, datetime
from io import BytesIO

from django.conf import settings
from django.core.paginator import Paginator
from django.db import close_old_connections
from django.db.models import Q, Count, Sum, Avg, F, Max
from django.http import JsonResponse, HttpResponse, FileResponse, Http404
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.db import IntegrityError
from django.views.decorators.http import require_http_methods

from .models import (Unit, Word, StudyProgress, StudyPlan,
                     DailyCheckIn, Favorite, Note, QuickMemory, DailyPick, AIModel, StudySession, UserSettings, ChatMessage, ImportLog, Conversation, LearningReport, StudyRecord, StudyPreset, ExamQuestion, WritingPractice, WritingPhrase, AICallLog)
from .ai_prompts import (
    quick_memory_prompt, ASSISTANT_SYSTEM_PROMPT, assistant_word_context,
    pos_grouping_prompt, examples_prompt, phonetics_prompt,
    RECOGNIZE_JSON_SCHEMA, RECOGNIZE_COMMON_RULES,
    recognize_image_prompt, recognize_text_prompt, recognize_file_prompt,
    ai_review_prompt, weekly_report_prompt,
)
from .ai_exam_prompts import (
    build_vocabulary_profile, essay_generation_prompt, essay_grading_prompt,
    translation_help_prompt, translation_grading_prompt, themes_report_prompt,
    personal_template_prompt, cet6_translation_help_prompt, cet6_translation_grading_prompt,
)
from . import vault


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

    context = {
        'today_new': today_new,
        'today_review': today_review,
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
    })


def learn_session(request):
    mode = request.GET.get('mode', 'sequential')
    check = request.GET.get('check', 'none')
    unit_ids = [u for u in request.GET.getlist('units') if str(u).strip().isdigit()]
    scope = request.GET.get('scope', 'unknown')

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

    query = Word.objects.all()
    daily_pick_ids = None
    fav_ids = None
    if scope == 'daily':
        # 今日自选：以当天勾选清单为准（跨天未背完的自动顺延），忽略单元参数
        rollover_daily_picks()
        daily_pick_ids = list(
            DailyPick.objects.filter(date=timezone.localdate())
            .order_by('id').values_list('word_id', flat=True)
        )
        query = query.filter(id__in=daily_pick_ids)
    elif scope == 'favorites':
        # 收藏词汇：以收藏表为准，忽略单元参数；顺序与收藏列表一致（最新收藏在前）
        fav_ids = list(
            Favorite.objects.order_by('-created_at').values_list('word_id', flat=True)
        )
        query = query.filter(id__in=fav_ids)
    elif unit_ids:
        query = query.filter(unit__number__in=unit_ids)

    # 今日自选 / 收藏词汇均为「选了就背」，不做掌握状态/永不忘记过滤；其余范围沿用旧规则
    if scope not in ('daily', 'favorites'):
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

    # 用 session 记录词 ID 列表（供复习页用）
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
    return render(request, 'daily_pick.html', {
        'units': units,
        'picked_ids_json': json.dumps(picked_ids, ensure_ascii=False),
        'words_json': json.dumps(word_items, ensure_ascii=False),
        'picked_count': len(picked_ids),
        'batch_size': settings_obj.batch_size,
    })


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
    # 今日到期总数：供「今日复习」卡片展示（昨天学的今天自动到期）
    today = timezone.localdate()
    yesterday = today - timedelta(days=1)
    base_progress = StudyProgress.objects.exclude(status='new').exclude(is_excluded=True)
    due_today = base_progress.filter(
        Q(next_review__isnull=True) | Q(next_review__lte=today)).count()
    # 每日新学记录：昨天新学的词今天可专项复习；今天新学的明天可复习
    new_yesterday = base_progress.filter(learned_date=yesterday).count()
    new_today = base_progress.filter(learned_date=today).count()
    return render(request, 'review_start.html', {
        'units': units, 'batch_size': settings_obj.batch_size,
        'batch_options': [10, 20, 30, 50],
        'daily_review_target': settings_obj.daily_review_target,
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

    # 随机复习模式
    random_mode = request.GET.get('mode') == 'random' or request.GET.get('random') == '1'
    if request.GET.get('mode') == 'random':
        # 新一轮随机复习：清空历史记录
        request.session['random_review_done'] = []

    # 本轮抽取数量
    target = max(1, settings_obj.daily_review_target)
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
    """薄弱词：从全部薄弱词中精选最需要巩固的 50 个。
    选词机制（薄弱指数综合评分）：
      - 错误次数 ×3（核心信号）
      - 未掌握 +2（学习中 / 复习中）
      - 掌握等级越低加分越多（(5-等级)×0.4）
      - 反复复习仍出错加分（复习次数 ×0.2，封顶 20 次）
      - 近期仍在复习的优先（7 天内 +1.5，30 天内 +0.8）
    按指数从高到低取前 50 个；顶部统计仍展示全部薄弱词总览。
    """
    progress_qs = (
        StudyProgress.objects
        .filter(Q(error_count__gt=0) | Q(status__in=['learning', 'reviewing']))
        .select_related('word__unit')
    )
    now = timezone.now()

    pool = []
    total_errors = 0
    reviewed = 0
    for p in progress_qs:
        total_errors += p.error_count
        if p.review_count > 0:
            reviewed += 1
        score = p.error_count * 3.0
        if p.status != 'mastered':
            score += 2.0
        score += max(0, 5 - p.mastery_level) * 0.4
        score += min(p.review_count, 20) * 0.2
        if p.last_review:
            days_since = (now - p.last_review).days
            if days_since <= 7:
                score += 1.5
            elif days_since <= 30:
                score += 0.8
        pool.append((score, p))

    pool.sort(key=lambda x: (-x[0], -x[1].error_count, -x[1].review_count))
    selected = pool[:50]

    items = []
    for score, p in selected:
        w = p.word
        items.append({
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
            'score': round(score, 1),
        })

    stats = {
        'total': len(pool),
        'unmastered': sum(1 for _, p in pool if p.status != 'mastered'),
        'total_errors': total_errors,
        'reviewed': reviewed,
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


def _record_study(word, action, data=None, mode=''):
    """写一条背词历史记录；失败不影响主流程"""
    try:
        payload = data or {}
        src = payload.get('source', 'learn')
        if src not in ('learn', 'review', 'favorite'):
            src = 'learn'
        StudyRecord.objects.create(
            word=word,
            action=action,
            source=src,
            mode=(mode or payload.get('mode') or '')[:30],
        )
    except Exception:
        pass


CONSECUTIVE_THRESHOLD = 20


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


def _judge_meaning_core(word, answer, data):
    """判定释义默写答案：AI 优先（跟随设置里选的模型），失败退回本地比对。
    返回 (verdict, comment, source)，source ∈ ai / local-exact / local-fallback。
    只判定、不写库——模拟考试逐题判定与交卷汇总共用。"""
    answer = (answer or '').strip()
    if not answer:
        return 'wrong', '未作答', 'local-blank'

    settings_obj = UserSettings.get_settings()
    if settings_obj.meaning_check_model and not data.get('model_id'):
        data['model_id'] = settings_obj.meaning_check_model_id

    # 本地强匹配先筛：明显一致的直接判对，省一次 AI 调用
    hit, meaning_parts = _local_meaning_match(word, answer)
    if hit:
        return 'correct', '本地判定：与标准释义一致', 'local-exact'

    if settings_obj.use_ai_meaning_check:
        meaning_text = '；'.join(meaning_parts)
        try:
            cfg = resolve_ai_model(data)
            prompt = MEANING_JUDGE_PROMPT.format(
                word=word.word, meanings=meaning_text, answer=answer)
            content = _ai_chat_once(cfg, prompt, max_tokens=500, temperature=0.1)
            parsed = _extract_json_object(content)
            v = parsed.get('verdict')
            if v in ('correct', 'partial', 'wrong'):
                comment = (parsed.get('comment') or '').strip()
                return v, comment, 'ai'
        except Exception:
            pass

    # AI 不可用 / 判定异常 → 本地兜底（标准偏严，需明确告知用户）
    return 'wrong', '本地判定：与标准释义不符', 'local-fallback'


def _judge_meaning_answer(word, answer, data):
    """判定释义默写答案：AI 优先（跟随设置里选的模型），失败退回本地比对。
    返回 (verdict, comment, used_ai)。"""
    verdict, comment, source = _judge_meaning_core(word, answer, data)
    return verdict, comment, source == 'ai'


def _judge_meaning_batch(pairs, data, chunk_size=10):
    """批量判定多道释义默写题，pairs=[(word, answer), ...]。
    本地强匹配先筛 → 其余按 chunk_size 分组，每组一次 AI 调用统一标准 →
    失败组重试 1 次 → 仍失败的逐题补判（单次 AI + 本地兜底）。
    返回 {word_id: (verdict, comment, source)}。"""
    results = {}
    pending = []  # (word, answer) 待 AI 判定
    for word, answer in pairs:
        answer = (answer or '').strip()
        if not answer:
            results[word.id] = ('wrong', '未作答', 'local-blank')
            continue
        hit, _ = _local_meaning_match(word, answer)
        if hit:
            results[word.id] = ('correct', '本地判定：与标准释义一致', 'local-exact')
        else:
            pending.append((word, answer))

    if not pending:
        return results

    settings_obj = UserSettings.get_settings()
    use_ai = settings_obj.use_ai_meaning_check

    for start in range(0, len(pending), chunk_size):
        group = pending[start:start + chunk_size]
        judged = {}
        if use_ai:
            if settings_obj.meaning_check_model and not data.get('model_id'):
                data['model_id'] = settings_obj.meaning_check_model_id
            items = [
                {'id': w.id, 'word': w.word,
                 'meanings': '；'.join(_local_meaning_match(w, a)[1]),
                 'answer': a}
                for w, a in group
            ]
            for attempt in range(2):  # 首调 + 失败重试 1 次
                try:
                    cfg = resolve_ai_model(data)
                    prompt = MEANING_BATCH_JUDGE_PROMPT.format(
                        n=len(items), items=json.dumps(items, ensure_ascii=False))
                    content = _ai_chat_once(
                        cfg, prompt, max_tokens=400 * len(items), temperature=0.1)
                    arr = _extract_json_array(content)
                    got = {}
                    for el in arr:
                        if not isinstance(el, dict):
                            continue
                        vid = el.get('id')
                        v = el.get('verdict')
                        if vid is not None and v in ('correct', 'partial', 'wrong'):
                            got[int(vid)] = (v, (el.get('comment') or '').strip())
                    # 必须整组 ID 齐全且合法才算成功，避免漏判悄悄混入
                    if all(w.id in got for w, _ in group):
                        judged = got
                        break
                except Exception:
                    judged = {}

        for w, a in group:
            if w.id in judged:
                results[w.id] = (judged[w.id][0], judged[w.id][1], 'ai')
            else:
                # 批量失败/缺项 → 逐题补判（内部还会尝试单次 AI，再失败才本地兜底）
                results[w.id] = _judge_meaning_core(w, a, data)[:3]
    return results


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
        _record_study(word, 'spelling_ok' if correct else 'spelling_wrong', data)

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
    """释义默写：看英文写中文意思，AI 判定正确 / 部分正确 / 错误（失败时退回本地比对）"""
    try:
        data = json.loads(request.body)
        answer = (data.get('answer') or '').strip()
        word = get_object_or_404(Word, id=word_id)
        progress, created = StudyProgress.objects.get_or_create(word=word)

        verdict, comment, used_ai = _judge_meaning_answer(word, answer, data)

        today = timezone.localdate()
        now = timezone.now()
        if created or progress.status == 'new':
            progress.learned_date = today
            progress.is_today_new = True

        progress.meaning_attempts += 1
        ok = verdict in ('correct', 'partial')
        if ok:
            progress.meaning_correct += 1
            progress.mastery_level = 5
            progress.status = 'mastered'
        else:
            progress.error_count += 1
            progress.mastery_level = 0
            progress.status = 'learning'
        auto_excluded = _track_consecutive(progress, ok)
        progress.review_count += 1
        progress.last_review = now
        # 自动复习调度：今天默写过的词 → 明天自动进入复习栏目
        _schedule_next_review(progress, today)
        progress.save()

        checkin, _ = DailyCheckIn.objects.get_or_create(date=today)
        if ok:
            checkin.today_correct += 1
        else:
            checkin.today_wrong += 1
        checkin.save()

        # 记录背词历史
        _record_study(word, 'meaning_ok' if ok else 'meaning_wrong', data)

        auto_checked = update_daily_checkin()

        return JsonResponse({
            'success': True,
            'verdict': verdict,
            'comment': comment,
            'used_ai': used_ai,
            'status': progress.status,
            'auto_checked': auto_checked,
            'auto_excluded': auto_excluded,
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
    settings_obj = UserSettings.get_settings()

    # 今日新词 - 如果没有设置今日新词，从新词中取 daily_new_target 个
    new_words = Word.objects.filter(progress__isnull=True)[:settings_obj.daily_new_target]
    new_list = [{
        'id': w.id,
        'word': w.word,
        'phonetic_us': w.phonetic_us,
        'pos': w.pos,
        'meanings': w.get_meanings(),
        'meanings_by_pos': w.get_meanings_by_pos(),
    } for w in new_words]

    # 今日需复习：优先「今日到期」的词（昨天学的今天自动到期），
    # 不够目标量时从更早已学但今天尚未安排的词中补足；今天刚学的安排在明天。
    all_progress = list(StudyProgress.objects.exclude(status='new')
                        .exclude(is_excluded=True).select_related('word'))
    due_list = [p for p in all_progress
                if p.next_review is None or p.next_review <= today]
    if len(due_list) < settings_obj.daily_review_target:
        due_ids = {p.id for p in due_list}
        fill_pool = [p for p in all_progress if p.id not in due_ids
                     and (p.learned_date is None or p.learned_date < today)]
        fill_pool.sort(key=lambda p: (p.learned_date or today))
        due_list.extend(fill_pool)
    random.shuffle(due_list)
    review_batch = due_list[:settings_obj.daily_review_target]

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

    # 今日到期数：昨天学会/复习过的词今天自动到期（供界面「今日待复习」展示）
    due_count = len([p for p in all_progress
                     if p.next_review is None or p.next_review <= today])

    return JsonResponse({
        'new_words': new_list,
        'review_words': review_list,
        'new_count': len(new_list),
        'review_count': len(review_list),
        'due_count': due_count,
        'daily_new_target': settings_obj.daily_new_target,
        'daily_review_target': settings_obj.daily_review_target,
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
    '1826548016-cloud/Postgraduate-Vocabulary-Memorization-System/releases/latest'
)
RELEASE_PAGE = ('https://github.com/1826548016-cloud/'
                'Postgraduate-Vocabulary-Memorization-System/releases/latest')


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


def _build_backup_payload(settings_obj):
    """全量备份数据结构：词库（单元+单词）、设置、学习进度、背词记录、打卡、收藏、笔记、
    速记、计划、预设、AI 模型、会话与消息、写作练习、写作好句、导入记录、学习周报。"""
    from django.core.serializers.json import DjangoJSONEncoder

    data = {
        'version': '2.1',
        'exported_at': timezone.localtime().isoformat(),
        'settings': _backup_settings(settings_obj),
        # 密钥保险箱元数据：跨机恢复后可用原密码解锁；密码本身不在备份中
        'vault': {'salt': settings_obj.vault_salt, 'verifier': settings_obj.vault_verifier},
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
                # 已设保险箱：导出密文、绝不导出明文；未设：照旧导出明文（兼容旧版）
                'api_key': '' if settings_obj.vault_salt else m.api_key,
                'api_key_enc': m.api_key_encrypted if settings_obj.vault_salt else '',
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

        def _resolve_word_id(row, table):
            """严格按 word 字符串映射当前库的 word_id；查不到返回 None（跳过）。
            不回退使用原 word_id，避免 id 撞车错挂到别的单词上。"""
            w = row.get('word')
            if w and w in exe_word_map:
                return exe_word_map[w]
            skipped[table] = skipped.get(table, 0) + 1
            return None

        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute('PRAGMA foreign_keys = OFF')
            try:
                # --- 0) 补齐词库：v2.1 备份携带 units+words，只增不改 ---
                units_added = 0
                words_added = 0
                skipped_keys = 0
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

                # --- 0) 密钥保险箱：本机未设密码时采纳备份的 salt/校验串，
                #       跨机恢复后用原来的保险箱密码即可解锁；本机已设密码则保留本机体系 ---
                cur_s = UserSettings.get_settings()
                backup_vault = data.get('vault') or {}
                if backup_vault.get('salt') and not cur_s.vault_salt:
                    cur_s.vault_salt = backup_vault['salt']
                    cur_s.vault_verifier = backup_vault.get('verifier', '')
                    cur_s.save(update_fields=['vault_salt', 'vault_verifier'])
                    vault.lock()  # 校验体系变更，强制下次重新解锁

                # --- 1) AI 模型（必须先恢复，其他表 FK 引用模型 id）---
                id_map = {}
                if data.get('ai_models'):
                    AIModel.objects.all().delete()
                    local_salt = UserSettings.get_settings().vault_salt
                    backup_salt = backup_vault.get('salt', '')
                    for m in data['ai_models']:
                        old_id = m.get('id')
                        enc = m.get('api_key_enc', '') or ''
                        plain = m.get('api_key', '') or ''
                        key_field = {'api_key': '', 'api_key_encrypted': ''}
                        if enc and backup_salt and backup_salt == local_salt:
                            # 同一密码体系（同机自恢复 / 已采纳备份保险箱）：密文原样可用
                            key_field['api_key_encrypted'] = enc
                        elif plain:
                            # 旧版备份明文：本机已设密码且已解锁 → 立即加密；
                            # 已设但锁定 → 暂存明文，输入密码解锁时自动迁移加密
                            if local_salt and vault.is_unlocked():
                                key_field['api_key_encrypted'] = vault.encrypt(plain)
                            else:
                                key_field['api_key'] = plain
                        elif enc:
                            # 备份为密文但密码体系不同且无明文：无法还原，需重新填写
                            skipped_keys += 1
                        m2 = AIModel.objects.create(
                            provider=m.get('provider', 'openai'),
                            model_id=m['model_id'],
                            display_name=m.get('display_name', ''),
                            base_url=m.get('base_url', 'https://api.openai.com/v1'),
                            endpoint=m.get('endpoint', ''),
                            context=m.get('context', '128K'),
                            vision=m.get('vision', True),
                            enabled=m.get('enabled', True),
                            **key_field
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
                        )

                # --- 4) 背词记录 ---
                if data.get('study_records'):
                    StudyRecord.objects.all().delete()
                    for r in data['study_records']:
                        new_wid = _resolve_word_id(r, 'study_records')
                        if new_wid is None:
                            continue
                        StudyRecord.objects.create(
                            word_id=new_wid, action=r['action'],
                            source=r.get('source', 'learn'), mode=r.get('mode', ''),
                        )

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
            finally:
                with connection.cursor() as cur2:
                    cur2.execute('PRAGMA foreign_keys = ON')

        msg = '恢复完成'
        if words_added or units_added:
            msg += f'（已随备份补齐 {units_added} 个单元、{words_added} 个词条）'
        if skipped:
            total_skipped = sum(skipped.values())
            msg += f'（跳过 {total_skipped} 条词库中不存在的单词数据：'
            msg += ', '.join(f'{k} {v}条' for k, v in skipped.items()) + '）'
            if not data.get('words'):
                msg += ('。该备份由旧版本生成，不包含词库本体，新增的单元/单词无法随备份恢复；'
                        '请在新版程序中重新备份，或先在本机导入对应词库后再恢复')
        if skipped_keys:
            msg += f'（另有 {skipped_keys} 个模型的 API 密钥因保险箱密码不同无法恢复，请重新填写）'
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
            result = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'ignore')
        return JsonResponse({'error': 'AI 接口错误 (HTTP %s): %s' % (e.code, body[:400])}, status=502)
    except urllib.error.URLError as e:
        return JsonResponse({'error': '无法连接 %s: %s' % (resolve_ai_endpoint(base_url, endpoint), e.reason)}, status=502)

    content = (result.get('choices', [{}])[0].get('message', {}).get('content', '') or '').strip()
    if not content:
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
            result = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'ignore')
        user_msg.delete()
        return JsonResponse({'error': 'AI 接口错误 (HTTP %s): %s' % (e.code, body[:400])}, status=502)
    except urllib.error.URLError as e:
        user_msg.delete()
        return JsonResponse({'error': '无法连接 %s: %s' % (resolve_ai_endpoint(cfg['base_url'], cfg['endpoint']), e.reason)}, status=502)

    content = (result.get('choices', [{}])[0].get('message', {}).get('content', '') or '').strip()
    if not content:
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
            result = json.loads(resp.read().decode('utf-8'))
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

    content = (result.get('choices', [{}])[0].get('message', {}).get('content', '') or '').strip()
    if not content:
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

        verdict, comment, source = _judge_meaning_core(word, answer, data)
        cache = request.session.get('exam_judgments') or {}
        cache[str(word_id)] = {
            'answer': answer, 'verdict': verdict,
            'comment': comment, 'source': source,
        }
        request.session['exam_judgments'] = cache
        return JsonResponse({
            'success': True, 'word_id': word_id, 'answer': answer,
            'verdict': verdict, 'comment': comment, 'source': source,
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
        stored = request.session.get('exam_questions', [])

        if not stored:
            return JsonResponse({'error': '考试已过期，请重新开始'}, status=400)

        answer_map = {a['word_id']: a for a in answers}
        cached_judgments = request.session.get('exam_judgments') or {}

        # ===== 阶段一：全部题先在内存里判定完，此阶段不写库 =====
        # 释义题优先使用答题过程中逐题判定的缓存（答案必须与缓存一致，改了答案则重判），
        # 未缓存/已变更的残留释义题收集起来走批量判定（10 题/组、统一标准）。
        word_objs = {w.id: w for w in Word.objects.filter(id__in=[sq['id'] for sq in stored])}
        residual_pairs = []          # (word, answer) 需批量判定
        graded = []                  # [{sq, ans, word, is_correct, comment, source}]
        for sq in stored:
            qid = sq['id']
            qtype = sq.get('type', 'choice')
            ans = answer_map.get(qid) or {}
            word = word_objs.get(qid)
            is_correct = False
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
                           'comment': comment,
                           'source': source, 'need_batch': qtype == 'meaning' and answered and not (
                               word and cached_judgments.get(str(qid))
                               and (cached_judgments[str(qid)].get('answer') or '') == (ans.get('answer') or '').strip())})

        # 残留释义题一次批量判定（同组统一标准，失败自动逐题兜底）
        batch_results = _judge_meaning_batch(residual_pairs, data) if residual_pairs else {}
        for g in graded:
            if g['need_batch'] and g['word']:
                verdict, comment, source = batch_results.get(
                    g['word'].id, ('wrong', '未作答', 'local-blank'))
                g['is_correct'] = verdict == 'correct'
                g['comment'] = comment
                g['source'] = source
            g.pop('need_batch', None)

        # ===== 阶段二：判定全部完成后，统一写库（中途失败则零写入，可安全重新交卷）=====
        today = timezone.localdate()
        now = timezone.now()
        checkin, _ = DailyCheckIn.objects.get_or_create(date=today)

        results = []
        correct_count = 0
        source_stats = {'ai': 0, 'local-exact': 0, 'local-fallback': 0, 'local-blank': 0}
        for g in graded:
            sq, ans, word = g['sq'], g['ans'], g['word']
            qid, qtype = sq['id'], sq.get('type', 'choice')
            is_correct = g['is_correct']
            answered = g['answered']
            if is_correct:
                correct_count += 1
            if qtype == 'meaning':
                source_stats[g['source'] if g['source'] in source_stats else 'local-fallback'] += 1

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

        meaning_total = (source_stats['ai'] + source_stats['local-exact']
                         + source_stats['local-fallback'] + source_stats['local-blank'])
        return JsonResponse({
            'success': True,
            'total': total,
            'correct_count': correct_count,
            'wrong_count': total - correct_count,
            'correct_rate': round(correct_count / total * 100, 1) if total else 0,
            'duration': duration,
            'results': results,
            'auto_checked': auto_checked,
            'meaning_judge_stats': {
                'total': meaning_total,
                'ai': source_stats['ai'],
                'local_exact': source_stats['local-exact'],
                'local_fallback': source_stats['local-fallback'],
                'blank': source_stats['local-blank'],
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


def _ai_chat_once(cfg, prompt, max_tokens=4000, temperature=0.5):
    """单次调用 AI（OpenAI 兼容），返回纯文本内容"""
    payload = {
        'model': cfg['model_id'],
        'temperature': temperature,
        'max_tokens': max_tokens,
        'messages': [{'role': 'user', 'content': prompt}],
    }
    req = urllib.request.Request(
        resolve_ai_endpoint(cfg['base_url'], cfg['endpoint']),
        data=json.dumps(payload).encode('utf-8'),
        headers=build_ai_headers(cfg['api_key']),
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        result = json.loads(resp.read().decode('utf-8'))
    content = (result.get('choices', [{}])[0].get('message', {}).get('content', '') or '').strip()
    if not content:
        raise RuntimeError('AI 返回为空')
    return content


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


VAULT_LOCKED_MSG = 'API 密钥已加密，请先到「设置 → AI 模型」输入保险箱密码解锁后再使用'
VAULT_LOCKED_CODE = 'vault_locked'


def _model_has_key(m):
    return bool(getattr(m, 'api_key_encrypted', '') or m.api_key)


def _model_plain_key(m):
    """供 AI 调用取明文密钥：加密字段优先（需保险箱已解锁），否则回退旧版明文。"""
    enc = getattr(m, 'api_key_encrypted', '')
    if enc:
        if not vault.is_unlocked():
            raise ValueError(VAULT_LOCKED_MSG)
        return vault.decrypt(enc)
    return m.api_key or ''


def _store_model_key(m, plain):
    """按保险箱状态写入密钥：已配置密码则加密并清空明文，未配置则按旧版明文存储。"""
    s = UserSettings.get_settings()
    if s.vault_salt:
        if not vault.is_unlocked():
            raise ValueError(VAULT_LOCKED_MSG)
        m.api_key_encrypted = vault.encrypt(plain) if plain else ''
        m.api_key = ''
    else:
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
    """AIModel 数据库记录 → 前端 JSON。安全原则：任何情况下都不回传密钥明文，
    只给 has_key 供前端展示状态；查看明文走 /api/vault/reveal/（需解锁）。"""
    return {
        'id': m.id,
        'provider': m.provider,
        'model_id': m.model_id,
        'display_name': m.display_name,
        'base_url': m.base_url,
        'endpoint': m.endpoint,
        'has_key': _model_has_key(m),
        'encrypted': bool(getattr(m, 'api_key_encrypted', '')),
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
        models = AIModel.objects.all().order_by('-enabled', 'id')
        return JsonResponse({'models': [serialize_ai_model(m) for m in models]})

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
        if plain_key:
            try:
                _store_model_key(m, plain_key)
            except ValueError as e:
                return JsonResponse({'error': str(e), 'code': VAULT_LOCKED_CODE}, status=403)
        elif not _model_has_key(m):
            try:
                _store_model_key(m, '')
            except ValueError as e:
                return JsonResponse({'error': str(e), 'code': VAULT_LOCKED_CODE}, status=403)
    m.context = (data.get('context') or '128K').strip() or '128K'
    if 'vision' in data:
        m.vision = bool(data.get('vision'))
    if 'enabled' in data:
        m.enabled = bool(data.get('enabled'))
    if not m.model_id:
        return JsonResponse({'error': '请填写模型 ID'}, status=400)
    m.save()
    return JsonResponse({'success': True, 'model': serialize_ai_model(m)})


# ==================== API 密钥保险箱 ====================

def _vault_payload(s):
    return {
        'configured': bool(s.vault_salt),
        'unlocked': vault.is_unlocked(),
        'key_count': AIModel.objects.exclude(api_key='', api_key_encrypted='').count(),
    }


@require_http_methods(['GET'])
def api_vault_status(request):
    """保险箱状态：是否已设密码、本次运行是否已解锁、已保存密钥数量"""
    s = UserSettings.get_settings()
    return JsonResponse(_vault_payload(s))


@require_http_methods(['POST'])
def api_vault_setup(request):
    """首次设置保险箱密码：校验串落库、内存解锁，并把存量明文密钥全部迁移为密文。"""
    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': '请求格式错误'}, status=400)
    password = data.get('password') or ''
    if len(password) < 4:
        return JsonResponse({'error': '密码至少 4 位'}, status=400)
    s = UserSettings.get_settings()
    if s.vault_salt:
        return JsonResponse({'error': '已设置过保险箱密码，无需重复设置'}, status=400)

    salt = vault.new_salt()
    verifier = vault.make_verifier(password, salt)
    key = vault.verify_password(password, salt, verifier)
    vault.unlock_in_memory(key)
    s.vault_salt = salt
    s.vault_verifier = verifier
    s.save(update_fields=['vault_salt', 'vault_verifier'])

    migrated = 0
    for m in AIModel.objects.exclude(api_key=''):
        m.api_key_encrypted = vault.encrypt(m.api_key)
        m.api_key = ''
        m.save(update_fields=['api_key', 'api_key_encrypted'])
        migrated += 1
    return JsonResponse({'success': True, 'migrated': migrated, **_vault_payload(s)})


@require_http_methods(['POST'])
def api_vault_unlock(request):
    """用密码解锁保险箱（派生密钥只存进程内存，重启程序自动锁定）。"""
    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': '请求格式错误'}, status=400)
    password = data.get('password') or ''
    s = UserSettings.get_settings()
    if not s.vault_salt:
        return JsonResponse({'error': '尚未设置保险箱密码'}, status=400)
    key = vault.verify_password(password, s.vault_salt, s.vault_verifier)
    if key is None:
        return JsonResponse({'error': '密码不正确'}, status=403)
    vault.unlock_in_memory(key)
    # 恢复旧版备份时可能暂存了明文密钥，解锁后立即迁移为密文
    migrated = 0
    for m in AIModel.objects.exclude(api_key=''):
        m.api_key_encrypted = vault.encrypt(m.api_key)
        m.api_key = ''
        m.save(update_fields=['api_key', 'api_key_encrypted'])
        migrated += 1
    return JsonResponse({'success': True, 'migrated': migrated, **_vault_payload(s)})


@require_http_methods(['POST'])
def api_vault_lock(request):
    """立即锁定保险箱。"""
    vault.lock()
    s = UserSettings.get_settings()
    return JsonResponse({'success': True, **_vault_payload(s)})


@require_http_methods(['POST'])
def api_vault_reset(request):
    """忘记密码的唯一办法：清空全部已保存的 API 密钥与保险箱密码，
    模型的其他配置（服务商/模型ID/地址）保留，之后重新填写密钥即可。"""
    try:
        data = json.loads(request.body or b'{}')
    except Exception:
        data = {}
    if not data.get('confirm'):
        return JsonResponse({'error': '缺少确认参数'}, status=400)
    vault.lock()
    cleared = AIModel.objects.exclude(api_key='', api_key_encrypted='').update(
        api_key='', api_key_encrypted='')
    s = UserSettings.get_settings()
    s.vault_salt = ''
    s.vault_verifier = ''
    s.save(update_fields=['vault_salt', 'vault_verifier'])
    return JsonResponse({'success': True, 'cleared': cleared, **_vault_payload(s)})


@require_http_methods(['POST'])
def api_vault_reveal(request):
    """查看某个模型的密钥明文：必须已设置密码并在本次运行中解锁。"""
    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': '请求格式错误'}, status=400)
    s = UserSettings.get_settings()
    if not s.vault_salt:
        # 未设置保险箱 = 没有加密也没有锁，明文可直接查看
        try:
            m = AIModel.objects.get(id=int(data.get('id') or 0))
        except (AIModel.DoesNotExist, ValueError, TypeError):
            return JsonResponse({'error': '模型不存在'}, status=404)
        return JsonResponse({'success': True, 'api_key': m.api_key or ''})
    if not vault.is_unlocked():
        return JsonResponse({'error': '请先输入保险箱密码解锁', 'code': VAULT_LOCKED_CODE}, status=403)
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
                result = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'ignore')
            return JsonResponse({'error': 'AI 接口错误 (HTTP %s): %s' % (e.code, body[:400])}, status=502)
        except urllib.error.URLError as e:
            url = resolve_ai_endpoint(base_url, endpoint)
            return JsonResponse({'error': '无法连接 %s: %s' % (url, e.reason)}, status=502)

        content = result.get('choices', [{}])[0].get('message', {}).get('content', '')
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
                result = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'ignore')
            return JsonResponse({'error': 'AI 接口错误 (HTTP %s): %s' % (e.code, body[:400])}, status=502)
        except urllib.error.URLError as e:
            url = resolve_ai_endpoint(base_url, endpoint)
            return JsonResponse({'error': '无法连接 %s: %s' % (url, e.reason)}, status=502)

        content = result.get('choices', [{}])[0].get('message', {}).get('content', '')
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
        # 支持只传 model_id：直接用库里已保存的配置（密钥在后端解密，不经过前端）
        if data.get('model_id') and not data.get('api_key'):
            try:
                saved = AIModel.objects.get(id=int(data['model_id']))
            except (AIModel.DoesNotExist, ValueError, TypeError):
                return JsonResponse({'error': '模型不存在'}, status=404)
            try:
                api_key = _model_plain_key(saved)
            except ValueError:
                return JsonResponse({'error': VAULT_LOCKED_MSG, 'code': VAULT_LOCKED_CODE}, status=403)
            base_url = saved.base_url
            endpoint = saved.endpoint
            model = saved.model_id
        else:
            api_key = data.get('api_key', '')
            base_url = data.get('base_url', 'https://api.openai.com/v1')
            endpoint = data.get('endpoint', '')
            model = data.get('model', 'gpt-4o-mini')

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
                result = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'ignore')
            # 401/403 = 密钥无效；404 = 模型或地址错误；其余透出
            return JsonResponse({
                'error': '连接失败 (HTTP %s): %s' % (e.code, body[:200]),
                'key_invalid': e.code in (401, 403),
                'model_invalid': e.code == 404,
            }, status=502)
        except urllib.error.URLError as e:
            url = resolve_ai_endpoint(base_url, endpoint)
            return JsonResponse({'error': '无法连接 %s: %s' % (url, e.reason)}, status=502)

        if result.get('choices'):
            return JsonResponse({'success': True, 'message': '连接成功，密钥有效'})
        return JsonResponse({'error': '接口响应异常'}, status=502)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ============================================================
# 考研写作工坊
# ============================================================

def builtin_ai_exam_call(prompt, temperature=0.7, data=None, action='exam_generate', practice=None):
    """考研工坊统一 AI 调用：复用现有模型配置体系（支持 DeepSeek 等 OpenAI 兼容接口）
    模型优先级：前端指定 model_id > 设置页「考研写作工坊模型」 > 第一个启用模型。
    调用详情（模型、耗时、成败、提示词预览）写入 AICallLog 供追溯。
    """
    import time as _time
    t0 = _time.monotonic()
    data = data or {}
    if not data.get('model_id'):
        settings_obj = UserSettings.get_settings()
        if settings_obj.exam_model_id:
            data['model_id'] = settings_obj.exam_model_id
    try:
        cfg = resolve_ai_model(data)
    except ValueError as e:
        _log_ai_call(action=action, practice=practice, model_id='', success=False,
                     error=str(e), prompt=prompt, t0=t0, duration_ms=0)
        return None, str(e)
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
            result = json.loads(resp.read().decode('utf-8'))
        # 在 HTTP 请求完成后计算真实耗时（包含网络 + AI 生成时间）
        duration_ms = int((_time.monotonic() - t0) * 1000)
    except urllib.error.HTTPError as e:
        duration_ms = int((_time.monotonic() - t0) * 1000)
        body = e.read().decode('utf-8', 'ignore')
        err = f'AI 接口错误 (HTTP {e.code}): {body[:300]}'
        _log_ai_call(action, practice, cfg['model_id'], False, err, prompt, t0, duration_ms)
        return None, err
    except urllib.error.URLError as e:
        duration_ms = int((_time.monotonic() - t0) * 1000)
        err = f'无法连接 AI 服务: {e.reason}'
        _log_ai_call(action, practice, cfg['model_id'], False, err, prompt, t0, duration_ms)
        return None, err
    content = result.get('choices', [{}])[0].get('message', {}).get('content', '')
    _log_ai_call(action, practice, cfg['model_id'], True, '', prompt, t0, duration_ms, response=content)
    return content, None


def _log_ai_call(action, practice, model_id, success, error, prompt, t0, duration_ms, response=''):
    """写入 AI 调用日志（内部辅助，异常不影响主流程）"""
    try:
        AICallLog.objects.create(
            practice=practice,
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
        log = AICallLog.objects.filter(action=action, practice__isnull=True).order_by('-id').first()
        if log:
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
    qs = AICallLog.objects.select_related('practice__question').order_by('-created_at')
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
    qs = AICallLog.objects.select_related('practice__question').order_by('-created_at')
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
    content, err = builtin_ai_exam_call(prompt, temperature=0.6, data=data, action='cet6_translate_analyze')
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
    content, err = builtin_ai_exam_call(prompt, temperature=0.3, data=data, action='cet6_grade_translation')
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
