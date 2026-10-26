import json
from datetime import timedelta
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    AICallLog,
    AIModel,
    AIModelHealth,
    Cet6TranslationPassage,
    Cet6TranslationPractice,
    DailyCheckIn,
    DailyPick,
    GeneratedCet6Paper,
    GeneratedCet6Question,
    GeneratedPaper,
    GeneratedQuestion,
    KnowledgeFeedback,
    PendingJudgment,
    PickRecord,
    PickRecordWord,
    PosLearningAttempt,
    PosLessonCache,
    StudyPlan,
    StudyProgress,
    StudyRecord,
    TranslationPractice,
    TranslationSentence,
    Unit,
    UserSettings,
    Word,
    WordFamily,
    WordFamilyMember,
    WordFormationPattern,
    WordRelation,
)
from .views import (
    _get_today_task, _today_practiced_word_ids, _validate_translation_grade_result,
    _batch_error_events, _sort_bucket_with_risk_jitter,
    _build_backup_payload, _resolve_pos_learning_model_id,
    judge_with_policy, judge_meaning_batch_with_policy, _AIRequestError,
    AI_HEALTH_FAIL_THRESHOLD,
    _ai_post_json, _ai_chat_once, builtin_ai_exam_call, _ai_health_status,
    compute_risk_score, RISK_TIER_LOW, RISK_TIER_MEDIUM, RISK_ERROR_ACTIONS,
    _get_or_create_pending_judgment,
    _model_plain_key, _store_model_key,
)


class ApiKeyVaultTests(TestCase):
    """模型密钥只能在 Windows 当前用户上下文中解密，数据库不保留明文。"""

    def test_store_key_uses_dpapi_and_migrates_legacy_plaintext(self):
        model = AIModel.objects.create(model_id='test-model', api_key='legacy-secret')
        self.assertEqual(_model_plain_key(model), 'legacy-secret')
        model.refresh_from_db()
        self.assertEqual(model.api_key, '')
        self.assertTrue(model.api_key_encrypted.startswith('dpapi:v1:'))
        self.assertEqual(_model_plain_key(model), 'legacy-secret')

        _store_model_key(model, 'replacement-secret')
        model.save()
        model.refresh_from_db()
        self.assertEqual(model.api_key, '')
        self.assertNotIn('replacement-secret', model.api_key_encrypted)
        self.assertEqual(_model_plain_key(model), 'replacement-secret')


class TranslationGradeValidationTests(SimpleTestCase):
    dimensions = ('structure', 'clause', 'lexicon', 'logic', 'expression')

    def valid_result(self):
        return {
            'scores': {key: 8 for key in self.dimensions},
            'total': 40,
            'comments': '表达准确。',
            'model_translation': 'A reference translation.',
            'errors': [],
            'suggestions': [],
        }

    def test_accepts_and_normalizes_valid_result(self):
        result, error = _validate_translation_grade_result(
            self.valid_result(), self.dimensions)

        self.assertIsNone(error)
        self.assertEqual(result['total'], 40.0)
        self.assertEqual(result['scores']['structure'], 8.0)

    def test_rejects_missing_score(self):
        value = self.valid_result()
        del value['scores']['logic']

        result, error = _validate_translation_grade_result(value, self.dimensions)

        self.assertIsNone(result)
        self.assertIn('scores.logic', error)

    def test_rejects_boolean_and_out_of_range_scores(self):
        for score in (True, 10.1):
            with self.subTest(score=score):
                value = self.valid_result()
                value['scores']['logic'] = score
                result, error = _validate_translation_grade_result(
                    value, self.dimensions)
                self.assertIsNone(result)
                self.assertIsNotNone(error)

    def test_rejects_inconsistent_total_and_missing_reference(self):
        value = self.valid_result()
        value['total'] = 10
        result, error = _validate_translation_grade_result(value, self.dimensions)
        self.assertIsNone(result)
        self.assertIn('total', error)

        value = self.valid_result()
        value['model_translation'] = '  '
        result, error = _validate_translation_grade_result(value, self.dimensions)
        self.assertIsNone(result)
        self.assertIn('model_translation', error)


class StudyTimerTests(TestCase):
    def test_timer_adds_reported_seconds_and_caps_single_report(self):
        response = self.client.post(
            reverse('words:api_study_duration'),
            data=json.dumps({'seconds': 120}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['added'], 90)
        checkin = DailyCheckIn.objects.get(date=timezone.localdate())
        self.assertEqual(checkin.study_duration, 90)

        self.client.post(
            reverse('words:api_study_duration'),
            data=json.dumps({'seconds': 12}),
            content_type='application/json',
        )
        checkin.refresh_from_db()
        self.assertEqual(checkin.study_duration, 102)

    def test_timer_rejects_malformed_payload_without_creating_checkin(self):
        response = self.client.post(
            reverse('words:api_study_duration'),
            data='{bad json',
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(DailyCheckIn.objects.exists())


class EfficientMemorizationProgressTests(TestCase):
    def test_session_keeps_base_word_count_when_repeats_and_interleaves_enabled(self):
        unit = Unit.objects.create(number=1, name='测试单元', category='required')
        words = [
            Word.objects.create(
                word=f'word-{index}', category='required', unit=unit,
                list_number=index, meanings='["释义"]',
            )
            for index in range(1, 3)
        ]

        response = self.client.get(reverse('words:learn_session'), {
            'mode': 'sequential',
            'scope': 'all',
            'repeats': 4,
            'interleave': 3,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['word_count'], len(words))
        self.assertEqual(response.context['gate_repeats'], 4)
        self.assertEqual(response.context['gate_interleave'], 3)
        # 进度分母使用原始词数，而不是把重复/穿插次数乘入总数。
        self.assertContains(response, 'displayTotal = gateBaseTotal;')


class GeneratedPaperDeletionTests(TestCase):
    def test_english_paper_cannot_be_deleted_if_it_has_practice(self):
        paper = GeneratedPaper.objects.create(title='英语一测试卷', status='failed')
        sentence = TranslationSentence.objects.create(
            order=1, sentence_en='A test sentence.', source_type='ai_generated')
        GeneratedQuestion.objects.create(paper=paper, sentence=sentence, order=1)
        TranslationPractice.objects.create(
            sentence=sentence, user_translation='测试译文')

        response = self.client.post(reverse('words:api_trans_paper_delete', args=[paper.id]))

        self.assertEqual(response.status_code, 409)
        self.assertTrue(GeneratedPaper.objects.filter(pk=paper.pk).exists())

    def test_failed_english_paper_without_practice_can_be_deleted(self):
        paper = GeneratedPaper.objects.create(title='失败卷', status='failed')

        response = self.client.post(reverse('words:api_trans_paper_delete', args=[paper.id]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(GeneratedPaper.objects.filter(pk=paper.pk).exists())

    def test_non_failed_english_paper_cannot_be_deleted(self):
        paper = GeneratedPaper.objects.create(title='已完成卷', status='ready')

        response = self.client.post(reverse('words:api_trans_paper_delete', args=[paper.id]))

        self.assertEqual(response.status_code, 400)
        self.assertTrue(GeneratedPaper.objects.filter(pk=paper.pk).exists())

    def test_cet6_paper_cannot_be_deleted_if_it_has_practice(self):
        paper = GeneratedCet6Paper.objects.create(title='六级测试卷', mode='topic', status='failed')
        passage = Cet6TranslationPassage.objects.create(
            source_type='ai_generated', source_zh='测试中文段落')
        GeneratedCet6Question.objects.create(paper=paper, passage=passage, order=1)
        Cet6TranslationPractice.objects.create(
            passage=passage, user_translation='An English translation.')

        response = self.client.post(reverse('words:api_cet6_paper_delete', args=[paper.id]))

        self.assertEqual(response.status_code, 409)
        self.assertTrue(GeneratedCet6Paper.objects.filter(pk=paper.pk).exists())

    def test_failed_cet6_paper_without_practice_can_be_deleted(self):
        paper = GeneratedCet6Paper.objects.create(
            title='六级失败卷', mode='topic', status='failed')

        response = self.client.post(reverse('words:api_cet6_paper_delete', args=[paper.id]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(GeneratedCet6Paper.objects.filter(pk=paper.pk).exists())


class TodayPracticeTests(TestCase):
    """复习自选（原「今日已背」）：已学词列表口径、选择校验、两态映射、记录字段、自测会话不写正式数据。"""

    def setUp(self):
        self.unit = Unit.objects.create(number=1, name='测试单元', category='required')
        self.words = [
            Word.objects.create(
                word=f'practice-{i}', category='required', unit=self.unit,
                list_number=i, meanings='["释义"]',
            )
            for i in range(1, 6)
        ]

    def _record(self, word, action='known', when=None):
        record = StudyRecord.objects.create(word=word, action=action, source='learn')
        if when is not None:
            StudyRecord.objects.filter(pk=record.pk).update(created_at=when)
            record.refresh_from_db()
        return record

    def _progress(self, word, status='learning', review_count=0):
        return StudyProgress.objects.create(
            word=word, status=status, review_count=review_count)

    def test_today_set_includes_records_and_new_learned(self):
        today = timezone.localdate()
        # 1. 今天有背诵明细 → 入列
        self._record(self.words[0], 'known')
        # 2. 今天只有 skip 明细 → 不入列
        self._record(self.words[1], 'skip')
        # 3. 无明细但 learned_date=today（新学词）→ 入列
        StudyProgress.objects.create(word=self.words[2], status='learning', learned_date=today)
        # 4. 无任何记录 → 不入列
        # 5. 昨天的记录 → 不入列
        yesterday = timezone.now() - timedelta(days=1)
        self._record(self.words[4], 'unknown', when=yesterday)

        ids = _today_practiced_word_ids(today)

        self.assertIn(self.words[0].id, ids)
        self.assertNotIn(self.words[1].id, ids)
        self.assertIn(self.words[2].id, ids)
        self.assertNotIn(self.words[3].id, ids)
        self.assertNotIn(self.words[4].id, ids)

    def test_learned_list_only_includes_learned_words(self):
        self._progress(self.words[0], status='learning')
        self._record(self.words[0], 'known')
        self._record(self.words[0], 'spelling_ok')
        # words[1] 无 StudyProgress（status=new）→ 不属于已学，不入列
        self._record(self.words[1], 'known')

        response = self.client.get(reverse('words:today_words'))

        self.assertEqual(response.status_code, 200)
        # 只有 words[0] 已学入列，多条明细不重复计数
        self.assertEqual(response.context['word_count'], 1)

    def test_learned_list_empty_state(self):
        response = self.client.get(reverse('words:today_words'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['word_count'], 0)

    def test_status_maps_to_two_states(self):
        self._progress(self.words[0], status='mastered')   # 会
        self._progress(self.words[1], status='learning')   # 不会
        self._progress(self.words[2], status='reviewing')  # 不会
        response = self.client.get(reverse('words:today_words'))
        items = json.loads(response.context['words_json'])
        by_id = {w['id']: w for w in items}
        self.assertEqual(by_id[self.words[0].id]['sn'], '会')
        self.assertEqual(by_id[self.words[1].id]['sn'], '不会')
        self.assertEqual(by_id[self.words[2].id]['sn'], '不会')

    def test_record_fields_displayed(self):
        self._progress(self.words[0], status='learning', review_count=3)
        # 最近一次动作 = 拼写正确
        self._record(self.words[0], 'spelling_ok')
        response = self.client.get(reverse('words:today_words'))
        items = json.loads(response.context['words_json'])
        w = items[0]
        self.assertEqual(w['rc'], 3)
        self.assertEqual(w['la'], '拼写正确')
        self.assertTrue(w['lt'])  # 最近练习时间非空

    def test_start_rejects_empty_selection(self):
        self._record(self.words[0], 'known')
        response = self.client.post(
            reverse('words:api_today_practice_start'),
            data=json.dumps({'word_ids': []}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_start_filters_ids_outside_learned_set(self):
        self._progress(self.words[0], status='learning')
        # words[1] 未学（无 StudyProgress），混入选择中应被过滤
        response = self.client.post(
            reverse('words:api_today_practice_start'),
            data=json.dumps({'word_ids': [self.words[0].id, self.words[1].id]}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['count'], 1)
        session = self.client.session
        self.assertEqual(session['practice_word_ids'], [self.words[0].id])

    def test_start_rejects_all_invalid_ids(self):
        # 传的都不是已学词 id（words[0] 无进度、999999 不存在）→ 400
        response = self.client.post(
            reverse('words:api_today_practice_start'),
            data=json.dumps({'word_ids': [self.words[0].id, 999999]}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_practice_session_requires_valid_session(self):
        # 未从列表启动直接访问 practice=1 → 重定向回列表
        response = self.client.get(reverse('words:learn_session') + '?practice=1')
        self.assertRedirects(response, reverse('words:today_words'))

    def test_practice_session_renders_only_validated_words(self):
        self._progress(self.words[0], status='learning')
        self._progress(self.words[1], status='reviewing')
        self.client.post(
            reverse('words:api_today_practice_start'),
            data=json.dumps({'word_ids': [self.words[0].id, self.words[1].id]}),
            content_type='application/json',
        )
        response = self.client.get(reverse('words:learn_session') + '?practice=1')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['practice_only'])
        self.assertEqual(response.context['check'], 'none')
        self.assertEqual(response.context['word_count'], 2)
        words_json = json.loads(response.context['words_json'])
        self.assertEqual({w['id'] for w in words_json}, {self.words[0].id, self.words[1].id})

    def test_practice_session_does_not_write_formal_data(self):
        self._record(self.words[0], 'known')
        progress = StudyProgress.objects.create(
            word=self.words[0], status='mastered', mastery_level=5,
            error_count=2, next_review=timezone.localdate() + timedelta(days=3),
        )
        checkin = DailyCheckIn.objects.create(date=timezone.localdate(), today_correct=5)
        self.client.post(
            reverse('words:api_today_practice_start'),
            data=json.dumps({'word_ids': [self.words[0].id]}),
            content_type='application/json',
        )
        before_records = StudyRecord.objects.count()

        response = self.client.get(reverse('words:learn_session') + '?practice=1')

        self.assertEqual(response.status_code, 200)
        # 打开自测会话不产生任何新的背词明细 / 打卡 / 进度变化
        self.assertEqual(StudyRecord.objects.count(), before_records)
        progress.refresh_from_db()
        self.assertEqual(progress.status, 'mastered')
        self.assertEqual(progress.error_count, 2)
        self.assertEqual(progress.next_review, timezone.localdate() + timedelta(days=3))
        checkin.refresh_from_db()
        self.assertEqual(checkin.today_correct, 5)

    def test_daily_scope_state_filter(self):
        today = timezone.localdate()
        mastered, learning, fresh = self.words[0], self.words[1], self.words[2]
        for w in (mastered, learning, fresh):
            DailyPick.objects.create(date=today, word_id=w.id)
        StudyProgress.objects.create(word=mastered, status='mastered')
        StudyProgress.objects.create(word=learning, status='learning')
        base = reverse('words:learn_session') + '?scope=daily&mode=sequential&check=none'

        res = self.client.get(base + '&state=mastered')
        self.assertEqual({w['id'] for w in json.loads(res.context['words_json'])},
                         {mastered.id})
        res = self.client.get(base + '&state=unmastered')
        self.assertEqual({w['id'] for w in json.loads(res.context['words_json'])},
                         {learning.id, fresh.id})
        res = self.client.get(base)  # 默认全部
        self.assertEqual({w['id'] for w in json.loads(res.context['words_json'])},
                         {mastered.id, learning.id, fresh.id})

    def test_review_order_uses_list_number_not_id(self):
        # 同单元内，创建 ID 顺序与 list_number 相反
        unit = Unit.objects.create(number=99, name='顺序单元', category='required')
        first_created = Word.objects.create(
            word='order-second', category='required', unit=unit,
            list_number=2, meanings='["b"]')
        second_created = Word.objects.create(
            word='order-first', category='required', unit=unit,
            list_number=1, meanings='["a"]')
        self.assertGreater(second_created.id, first_created.id)
        StudyProgress.objects.create(word=first_created, status='learning')
        StudyProgress.objects.create(word=second_created, status='learning')

        response = self.client.get(reverse('words:today_words'))
        items = json.loads(response.context['words_json'])
        sub = [w['id'] for w in items
               if w['id'] in (first_created.id, second_created.id)]
        # 按 list_number 升序（1 在前），而非数据库 ID 顺序
        self.assertEqual(sub, [second_created.id, first_created.id])

        # 启动练习的 session 顺序同样按 list_number
        res = self.client.post(
            reverse('words:api_today_practice_start'),
            data=json.dumps({'word_ids': [first_created.id, second_created.id]}),
            content_type='application/json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.client.session['practice_word_ids'],
                         [second_created.id, first_created.id])


class PickRecordTests(TestCase):
    """选词记录：快照保存 / 载入续作 / 改名 / 删除，会话本地用户（session）。"""

    def setUp(self):
        self.unit = Unit.objects.create(number=1, name='测试单元', category='required')
        self.words = [
            Word.objects.create(
                word=f'record-{i}', category='required', unit=self.unit,
                list_number=i, meanings='["释义"]',
            )
            for i in range(1, 4)
        ]
        self.today = timezone.localdate()

    def _seed_picks(self, word_ids):
        for wid in word_ids:
            DailyPick.objects.create(date=self.today, word_id=wid)

    def test_create_rejects_empty_picklist(self):
        response = self.client.post(
            reverse('words:api_pick_record_create'),
            data=json.dumps({}), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['success'])

    def test_create_saves_snapshot_with_order_and_default_name(self):
        self._seed_picks([self.words[0].id, self.words[1].id])
        response = self.client.post(
            reverse('words:api_pick_record_create'),
            data=json.dumps({}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['success'])
        self.assertEqual(body['record']['count'], 2)
        self.assertTrue(body['record']['name'].startswith('选词记录 '))
        record = PickRecord.objects.get(id=body['record']['id'])
        self.assertEqual(
            list(record.words.order_by('position').values_list('word_id', flat=True)),
            [self.words[0].id, self.words[1].id])

    def test_create_uses_custom_name(self):
        self._seed_picks([self.words[0].id])
        response = self.client.post(
            reverse('words:api_pick_record_create'),
            data=json.dumps({'name': '  我的清单  '}), content_type='application/json')
        self.assertEqual(response.json()['record']['name'], '我的清单')

    def test_load_replaces_current_picklist(self):
        self._seed_picks([self.words[0].id])
        record = PickRecord.objects.create(name='记录A')
        PickRecordWord.objects.create(record=record, word=self.words[1], position=0)
        PickRecordWord.objects.create(record=record, word=self.words[2], position=1)
        response = self.client.post(
            reverse('words:api_pick_record_load', args=[record.id]),
            data=json.dumps({}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['success'])
        self.assertEqual(body['count'], 2)
        self.assertEqual(
            list(DailyPick.objects.filter(date=self.today).order_by('id')
                 .values_list('word_id', flat=True)),
            [self.words[1].id, self.words[2].id])

    def test_rename_rejects_empty_and_updates(self):
        self._seed_picks([self.words[0].id])
        record = PickRecord.objects.create(name='旧名')
        response = self.client.post(
            reverse('words:api_pick_record_rename', args=[record.id]),
            data=json.dumps({'name': '  '}), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            reverse('words:api_pick_record_rename', args=[record.id]),
            data=json.dumps({'name': '新名'}), content_type='application/json')
        record.refresh_from_db()
        self.assertEqual(record.name, '新名')

    def test_delete_cascades_words(self):
        record = PickRecord.objects.create(name='待删')
        PickRecordWord.objects.create(record=record, word=self.words[0], position=0)
        response = self.client.post(
            reverse('words:api_pick_record_delete', args=[record.id]),
            data=json.dumps({}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PickRecord.objects.filter(id=record.id).exists())
        self.assertFalse(PickRecordWord.objects.filter(record_id=record.id).exists())

    def test_daily_pick_page_provides_records_context(self):
        record = PickRecord.objects.create(name='记录X')
        PickRecordWord.objects.create(record=record, word=self.words[0], position=0)
        PickRecordWord.objects.create(record=record, word=self.words[1], position=1)
        response = self.client.get(reverse('words:daily_pick'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['pick_records'][0]['name'], '记录X')
        self.assertEqual(response.context['pick_records'][0]['count'], 2)

    def test_backup_restore_roundtrip_preserves_records(self):
        """导出→清库→恢复：记录数、名称、创建时间、成员及顺序完全一致。"""
        past = timezone.now() - timedelta(days=3)
        r1 = PickRecord.objects.create(name='记录一')
        PickRecord.objects.filter(pk=r1.pk).update(created_at=past)
        # 成员顺序与 ID 顺序相反（words[2] 在前）
        PickRecordWord.objects.create(record=r1, word=self.words[2], position=0)
        PickRecordWord.objects.create(record=r1, word=self.words[0], position=1)
        r2 = PickRecord.objects.create(name='记录二')
        PickRecordWord.objects.create(record=r2, word=self.words[1], position=0)

        payload = _build_backup_payload(UserSettings.get_settings())
        self.assertEqual(len(payload['pick_records']), 2)
        PickRecord.objects.all().delete()

        res = self.client.post(
            reverse('words:api_restore'),
            data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            set(PickRecord.objects.values_list('name', flat=True)),
            {'记录一', '记录二'})
        restored = PickRecord.objects.get(name='记录一')
        self.assertEqual(restored.created_at.date(), past.date())
        self.assertEqual(
            list(restored.words.order_by('position').values_list('word_id', flat=True)),
            [self.words[2].id, self.words[0].id])

    def test_old_backup_without_records_does_not_error(self):
        """旧备份缺 pick_records 键：按空集合兼容，不报错、不误删。"""
        PickRecord.objects.create(name='留存记录')
        res = self.client.post(
            reverse('words:api_restore'),
            data=json.dumps({'settings': {}}), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(PickRecord.objects.filter(name='留存记录').exists())

    def test_load_rolls_over_old_picklist_first(self):
        """载入记录前先滚动旧日期自选，当天清单严格等于记录，旧词不残留。"""
        yesterday = self.today - timedelta(days=1)
        DailyPick.objects.create(date=yesterday, word_id=self.words[0].id)
        DailyPick.objects.create(date=self.today, word_id=self.words[2].id)
        record = PickRecord.objects.create(name='不含旧词')
        PickRecordWord.objects.create(record=record, word=self.words[1], position=0)

        res = self.client.post(
            reverse('words:api_pick_record_load', args=[record.id]),
            data=json.dumps({}), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        today_ids = set(
            DailyPick.objects.filter(date=self.today).values_list('word_id', flat=True))
        self.assertEqual(today_ids, {self.words[1].id})
        self.assertFalse(DailyPick.objects.filter(date=yesterday).exists())


class TodayTaskUnifiedTests(TestCase):
    """统一今日计划与复习计划：目标来源、单元范围、复习顺序与会话取词一致。"""

    def setUp(self):
        self.unit1 = Unit.objects.create(number=1, name='U1', category='required')
        self.unit2 = Unit.objects.create(number=2, name='U2', category='required')
        self.w1 = [
            Word.objects.create(word=f'u1-{i}', category='required', unit=self.unit1,
                                list_number=i, meanings='["x"]')
            for i in range(3)
        ]
        self.w2 = [
            Word.objects.create(word=f'u2-{i}', category='required', unit=self.unit2,
                                list_number=i, meanings='["x"]')
            for i in range(3)
        ]
        self.today = timezone.localdate()
        self.yesterday = self.today - timedelta(days=1)

    def _plan(self, new=10, review=10, units=()):
        return StudyPlan.objects.create(
            name='P', daily_new_words=new, daily_review_count=review,
            unit_range=json.dumps(list(units)), is_active=True,
        )

    def _progress(self, word, learned_date=None, error_count=0, next_review=None):
        return StudyProgress.objects.create(
            word=word, status='learning', learned_date=learned_date,
            error_count=error_count, next_review=next_review,
        )

    def test_no_plan_falls_back_to_settings(self):
        s = UserSettings.get_settings()
        s.daily_new_target = 20
        s.daily_review_target = 40
        s.save()
        task = _get_today_task(self.today)
        self.assertEqual(task['new_target'], 20)
        self.assertEqual(task['review_target'], 40)
        self.assertEqual(task['unit_nums'], [])

    def test_plan_is_single_source_of_targets_and_range(self):
        self._plan(new=5, review=8, units=[1])
        task = _get_today_task(self.today)
        self.assertEqual(task['new_target'], 5)
        self.assertEqual(task['review_target'], 8)
        self.assertEqual(task['unit_nums'], [1])

    def test_plan_limits_new_candidates_to_unit_range(self):
        self._plan(new=10, review=10, units=[1])
        task = _get_today_task(self.today)
        ids = set(task['new_candidates'].values_list('id', flat=True))
        self.assertEqual(ids, {w.id for w in self.w1})

    def test_plan_limits_review_candidates_to_unit_range(self):
        for w in self.w1 + self.w2:
            self._progress(w, learned_date=self.yesterday)
        self._plan(new=0, review=10, units=[1])
        task = _get_today_task(self.today)
        ids = {p.word_id for p in task['review_ordered']}
        self.assertEqual(ids, {w.id for w in self.w1})

    def test_new_remaining_respects_today_done(self):
        self._progress(self.w1[0], learned_date=self.today)
        self._plan(new=3, review=10, units=[1])
        task = _get_today_task(self.today)
        self.assertEqual(task['new_done'], 1)
        self.assertEqual(task['new_remaining'], 2)

    def test_target_completed_yields_zero_remaining(self):
        for w in self.w1:
            self._progress(w, learned_date=self.today)
        self._plan(new=3, review=10, units=[1])
        task = _get_today_task(self.today)
        self.assertEqual(task['new_done'], 3)
        self.assertEqual(task['new_remaining'], 0)

    def test_plan_range_with_no_new_words(self):
        for w in self.w1:
            self._progress(w, learned_date=self.yesterday)
        self._plan(new=3, review=10, units=[1])
        task = _get_today_task(self.today)
        self.assertEqual(list(task['new_candidates']), [])
        self.assertEqual(task['new_remaining'], 3)

    def test_review_priority_order(self):
        p_wrong = self._progress(self.w1[0], learned_date=self.yesterday, error_count=1, next_review=self.today)
        p_new = self._progress(self.w1[1], learned_date=self.yesterday, next_review=self.today + timedelta(days=1))
        p_due = self._progress(self.w1[2], learned_date=self.yesterday - timedelta(days=2), next_review=self.today)
        p_old = self._progress(self.w2[0], learned_date=self.yesterday - timedelta(days=5), next_review=self.today + timedelta(days=1))
        self._plan(new=0, review=10, units=[1, 2])
        ordered = _get_today_task(self.today)['review_ordered']
        self.assertEqual([p.id for p in ordered], [p_wrong.id, p_new.id, p_due.id, p_old.id])

    def test_review_fills_beyond_due(self):
        for w in self.w1:
            self._progress(w, learned_date=self.yesterday, next_review=self.today + timedelta(days=1))
        self._progress(self.w2[0], learned_date=self.yesterday, next_review=self.today)
        self._plan(new=0, review=3, units=[1, 2])
        task = _get_today_task(self.today)
        self.assertGreaterEqual(len(task['review_ordered']), 3)

    def test_api_today_matches_plan_scope(self):
        self._plan(new=10, review=10, units=[1])
        res = self.client.get(reverse('words:api_today'))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        new_ids = {w['id'] for w in data['new_words']}
        self.assertEqual(new_ids, {w.id for w in self.w1})
        self.assertEqual(data['new_count'], 3)
        self.assertEqual(data['daily_new_target'], 10)
        self.assertEqual(data['daily_review_target'], 10)

    def test_learn_session_plan_limits_units_and_count(self):
        self._plan(new=2, review=10, units=[1])
        res = self.client.get(reverse('words:learn_session') + '?plan=1')
        self.assertEqual(res.status_code, 200)
        words_json = json.loads(res.context['words_json'])
        ids = [w['id'] for w in words_json]
        self.assertEqual(ids, [self.w1[0].id, self.w1[1].id])

    def test_review_session_plan_limits_units(self):
        self._progress(self.w1[0], learned_date=self.yesterday, next_review=self.today)
        self._progress(self.w2[0], learned_date=self.yesterday, next_review=self.today)
        self._plan(new=0, review=1, units=[1])
        res = self.client.get(reverse('words:review_session') + '?plan=1&mode=random')
        self.assertEqual(res.status_code, 200)
        words_json = json.loads(res.context['words_json'])
        ids = {w['id'] for w in words_json}
        self.assertEqual(ids, {self.w1[0].id})


class RestoreBackupTimestampTests(TestCase):
    """恢复备份时保留背词记录的原始时间，避免历史词被误计为当日已背。"""

    def setUp(self):
        self.unit = Unit.objects.create(number=1, name='测试单元', category='required')
        self.word = Word.objects.create(
            word='restore-ts', category='required', unit=self.unit,
            list_number=1, meanings='["x"]',
        )

    def _post_restore(self, records):
        return self.client.post(
            reverse('words:api_restore'),
            data=json.dumps({'study_records': records}),
            content_type='application/json',
        )

    def test_restore_preserves_original_created_at(self):
        past = timezone.now() - timedelta(days=2)
        res = self._post_restore([
            {'word': self.word.word, 'action': 'known', 'source': 'learn',
             'mode': '', 'created_at': past.isoformat()},
        ])
        self.assertEqual(res.status_code, 200)

        rec = StudyRecord.objects.get(word=self.word)
        self.assertEqual(rec.created_at.date(), past.date())
        self.assertNotIn(self.word.id, _today_practiced_word_ids(timezone.localdate()))

    def test_restore_skips_record_without_created_at(self):
        res = self._post_restore([
            {'word': self.word.word, 'action': 'known', 'source': 'learn', 'mode': ''},
        ])
        self.assertEqual(res.status_code, 200)
        self.assertFalse(StudyRecord.objects.filter(word=self.word).exists())
        self.assertNotIn(self.word.id, _today_practiced_word_ids(timezone.localdate()))
        self.assertIn('因缺少时间被跳过', res.json()['message'])

    def test_restore_history_not_counted_as_today(self):
        """历史进度与明细恢复到空库后，不得被仪表盘算作今日新学/复习/已背。

        复现 v3.4.1 换机恢复事故：背词明细时间戳丢失后全部记为恢复当天，
        导致「今日已背」显示备份中的全部历史单词。
        """
        past = (timezone.now() - timedelta(days=2)).isoformat()
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        res = self.client.post(
            reverse('words:api_restore'),
            data=json.dumps({
                'progress': [{
                    'word': self.word.word, 'status': 'mastered',
                    'mastery_level': 3, 'review_count': 2, 'error_count': 0,
                    'last_review': past, 'next_review': yesterday,
                    'is_today_new': False, 'learned_date': yesterday,
                    'uncommon_pos': '[]', 'spelling_attempts': 0,
                    'spelling_correct': 0, 'meaning_attempts': 0,
                    'meaning_correct': 0, 'consecutive_correct': 2,
                    'is_excluded': False, 'manual_level': 1,
                }],
                'study_records': [{
                    'word': self.word.word, 'action': 'known', 'source': 'learn',
                    'mode': '', 'created_at': past,
                }],
            }),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 200)
        ctx = self.client.get(reverse('words:dashboard')).context
        self.assertEqual(ctx['today_new'], 0)
        self.assertEqual(ctx['today_review'], 0)
        self.assertEqual(ctx['checkin_words'], 0)
        self.assertNotIn(self.word.id, _today_practiced_word_ids(timezone.localdate()))

    def test_restore_todays_own_records_still_counted(self):
        """备份中本来就属于今天的记录，恢复后仍正常计入今日（不能一刀切清零）。"""
        now = timezone.now().isoformat()
        today = timezone.localdate().isoformat()
        res = self.client.post(
            reverse('words:api_restore'),
            data=json.dumps({
                'progress': [{
                    'word': self.word.word, 'status': 'learning',
                    'mastery_level': 1, 'review_count': 1, 'error_count': 0,
                    'last_review': now, 'next_review': None,
                    'is_today_new': True, 'learned_date': today,
                    'uncommon_pos': '[]', 'spelling_attempts': 0,
                    'spelling_correct': 0, 'meaning_attempts': 0,
                    'meaning_correct': 0, 'consecutive_correct': 1,
                    'is_excluded': False, 'manual_level': 3,
                }],
                'study_records': [{
                    'word': self.word.word, 'action': 'known', 'source': 'learn',
                    'mode': '', 'created_at': now,
                }],
            }),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 200)
        ctx = self.client.get(reverse('words:dashboard')).context
        self.assertEqual(ctx['today_new'], 1)
        self.assertIn(self.word.id, _today_practiced_word_ids(timezone.localdate()))


class RestoreUploadLimitTests(TestCase):
    """恢复备份：整份备份以 JSON 请求体提交，请求体上限必须能容纳大数据量备份。

    回归 2026-10-18 用户反馈「之前的备份不能导入」：备份体积超过 Django 默认
    2.5MB 上限时触发 RequestDataTooBig，恢复直接失败。见 settings.DATA_UPLOAD_MAX_MEMORY_SIZE。
    """

    def setUp(self):
        self.unit = Unit.objects.create(number=1, name='测试单元', category='required')
        self.word = Word.objects.create(
            word='upload-limit', category='required', unit=self.unit,
            list_number=1, meanings='["x"]',
        )

    def test_large_backup_body_accepted(self):
        payload = {
            'study_records': [{
                'word': self.word.word, 'action': 'known', 'source': 'learn',
                'mode': '', 'created_at': timezone.now().isoformat(),
            }],
            # 未知字段仅用于撑大请求体（超过默认 2.5MB），恢复逻辑会忽略它
            'padding': 'x' * (3 * 1024 * 1024),
        }
        res = self.client.post(
            reverse('words:api_restore'),
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 200, res.content[:300])
        self.assertTrue(res.json().get('success'))
        self.assertTrue(StudyRecord.objects.filter(word=self.word).exists())


class RiskEngineTests(TestCase):
    """错误风险引擎：档位边界、手动覆盖优先、错误样本、复习融合、备份往返。"""

    def setUp(self):
        self.unit = Unit.objects.create(number=1, name='测试单元', category='required')
        self.w1 = Word.objects.create(
            word='alpha', category='required', unit=self.unit,
            list_number=1, meanings='["a1", "a2"]',
        )
        self.w2 = Word.objects.create(
            word='beta', category='required', unit=self.unit,
            list_number=2, meanings='["b1"]',
        )

    def _progress(self, word):
        p, _ = StudyProgress.objects.get_or_create(word=word)
        return p

    def _wrong_event(self, word, action='spelling_wrong', mistake_type='spelling'):
        StudyRecord.objects.create(
            word=word, action=action, source='review',
            mistake_type=mistake_type,
            error_detail={'user_answer': 'x', 'correct': word.word},
        )

    # --- 档位边界 ---
    def test_low_risk_when_no_errors(self):
        p = self._progress(self.w1)
        score, tier, reasons, overridden = compute_risk_score(p)
        # 未掌握+逾期 + 错误率修正，但无错误事件 → 低风险
        self.assertLess(score, RISK_TIER_LOW)
        self.assertEqual(tier, 0)
        self.assertFalse(overridden)

    def test_high_risk_when_many_recent_errors(self):
        p = self._progress(self.w1)
        p.status = 'learning'
        p.error_count = 5
        p.save()
        for _ in range(5):
            self._wrong_event(self.w1)
        score, tier, reasons, overridden = compute_risk_score(p)
        self.assertGreaterEqual(score, RISK_TIER_MEDIUM)
        self.assertEqual(tier, 2)
        self.assertTrue(reasons)
        self.assertFalse(overridden)

    def test_medium_risk_boundary(self):
        w = self.w2
        p, _ = StudyProgress.objects.get_or_create(word=w, status='learning')
        p.error_count = 2
        p.save()
        self._wrong_event(w, action='meaning_wrong', mistake_type='meaning')
        self._wrong_event(w, action='meaning_wrong', mistake_type='meaning')
        score, tier, _, _ = compute_risk_score(p)
        # 单次=不高于中风险起点，断言落在低或中，但不会是高
        self.assertLess(tier, 2)
        self.assertGreaterEqual(score, 0)

    # --- 手动覆盖优先 ---
    def test_manual_override_takes_priority(self):
        p = self._progress(self.w1)

        p.risk_override = 2
        p.save()
        score, tier, reasons, overridden = compute_risk_score(p)
        self.assertIsNone(score)
        self.assertEqual(tier, 2)
        self.assertTrue(overridden)

        p.risk_override = 0
        p.save()
        _, tier, _, overridden = compute_risk_score(p)
        self.assertEqual(tier, 0)
        self.assertTrue(overridden)

        # 清除覆盖 → 回到自动计算
        p.risk_override = None
        p.save()
        score, tier, _, overridden = compute_risk_score(p)
        self.assertIsNotNone(score)
        self.assertFalse(overridden)

    # --- 拼写/释义错误样本 ---
    def test_spelling_check_writes_error_sample(self):
        res = self.client.post(
            reverse('words:api_spelling_check', args=[self.w1.id]),
            data=json.dumps({'answer': 'wrongly', 'source': 'learn'}),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 200)
        rec = StudyRecord.objects.filter(word=self.w1, action='spelling_wrong').first()
        self.assertIsNotNone(rec)
        self.assertEqual(rec.mistake_type, 'spelling')
        self.assertEqual(rec.error_detail['user_answer'], 'wrongly')
        self.assertEqual(rec.error_detail['correct'], self.w1.word)

    def test_meaning_check_writes_error_sample(self):
        # 第八节起：只有 AI（或人工）**最终确认**的 wrong 才写错误样本；
        # AI 不可用时落 pending_review、不判错。此处模拟 AI 确认答错。
        AIModel.objects.create(provider='test', model_id='m', base_url='https://x/v1',
                               api_key='k', enabled=True)
        with patch('words.views._ai_post_json',
                   return_value='{"verdict": "wrong", "comment": "释义不符"}'):
            res = self.client.post(
                reverse('words:api_meaning_check', args=[self.w1.id]),
                data=json.dumps({'answer': '完全不对', 'source': 'learn'}),
                content_type='application/json',
            )
        self.assertEqual(res.status_code, 200)
        rec = StudyRecord.objects.filter(word=self.w1, action='meaning_wrong').first()
        self.assertIsNotNone(rec)
        self.assertEqual(rec.mistake_type, 'meaning')
        self.assertEqual(rec.error_detail['correct'], self.w1.get_meanings())

    def test_meaning_check_local_fallback_writes_error_sample(self):
        # AI 不可用（断网）→ 本地兜底判错，写错误样本、改掌握状态
        with patch('words.views._ai_post_json',
                   side_effect=_AIRequestError('network', '无法连接')):
            res = self.client.post(
                reverse('words:api_meaning_check', args=[self.w1.id]),
                data=json.dumps({'answer': '完全不对', 'source': 'learn'}),
                content_type='application/json',
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['verdict'], 'wrong')
        self.assertTrue(StudyRecord.objects.filter(word=self.w1).exists())
        p = StudyProgress.objects.get(word=self.w1)
        self.assertEqual(p.error_count, 1)
        self.assertEqual(p.status, 'learning')

    # --- 复习融合：高风险提前 ---
    def test_high_risk_reviewed_early_in_bucket(self):
        today = timezone.localdate()
        # 两个昨天新学(to)的词：一个有多次错误(高风险)，一个无误(低风险)
        hp = self._progress(self.w1)
        hp.learned_date = today - timedelta(days=1)
        hp.is_today_new = False
        hp.error_count = 6
        hp.status = 'learning'
        hp.save()
        for _ in range(6):
            self._wrong_event(self.w1)
        lp = self._progress(self.w2)
        lp.learned_date = today - timedelta(days=1)
        lp.is_today_new = False
        lp.status = 'mastered'
        lp.consecutive_correct = 5
        lp.save()

        ordered = _get_today_task(today)['review_ordered']
        hi = next(i for i, p in enumerate(ordered) if p.word_id == self.w1.id)
        lo = next(i for i, p in enumerate(ordered) if p.word_id == self.w2.id)
        self.assertLess(hi, lo)

    # --- 备份往返新字段 ---
    def test_backup_roundtrip_risk_fields(self):
        p = self._progress(self.w1)
        p.risk_override = 1
        p.error_count = 2
        p.save()
        self._wrong_event(self.w1, action='meaning_wrong', mistake_type='meaning')

        backup = self.client.get(reverse('words:api_backup')).json()
        rec = next(r for r in backup['study_records'] if r['word_id'] == self.w1.id)
        self.assertEqual(rec['mistake_type'], 'meaning')
        self.assertEqual(rec['error_detail']['correct'], self.w1.word)
        prog = next(x for x in backup['progress'] if x['word_id'] == self.w1.id)
        self.assertEqual(prog['risk_override'], 1)

        # 恢复后字段仍保留
        res = self.client.post(
            reverse('words:api_restore'),
            data=json.dumps(backup),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 200)
        restored_prog = StudyProgress.objects.get(word=self.w1)
        self.assertEqual(restored_prog.risk_override, 1)
        restored_rec = StudyRecord.objects.filter(word=self.w1).first()
        self.assertEqual(restored_rec.mistake_type, 'meaning')

    def test_manual_override_api(self):
        res = self.client.post(
            reverse('words:api_risk_override', args=[self.w1.id]),
            data=json.dumps({'level': 2}),
            content_type='application/json',
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body['success'])
        self.assertEqual(body['risk_tier'], 2)
        self.assertTrue(body['risk_overridden'])

        # 清除
        res = self.client.post(
            reverse('words:api_risk_override', args=[self.w1.id]),
            data=json.dumps({'level': -1}),
            content_type='application/json',
        )
        self.assertTrue(res.json()['success'])
        self.assertFalse(res.json()['risk_overridden'])

    def test_exam_wrong_writes_real_exam_source(self):
        """考试真实答错时，StudyRecord.source 应记录为 exam 而非 learn。"""
        from django.test import Client
        c = Client()
        # 直接构造 session 中的考试题目与方向，避免依赖组卷词库数量
        session = c.session
        session['exam_questions'] = [{
            'id': self.w1.id,
            'type': 'choice',
            'options': ['错误选项A', '错误选项B', '错误选项C', '正确选项'],
            'correct_index': 3,
        }]
        session['exam_direction'] = 'en2zh'
        session.save()
        res = c.post(reverse('words:api_exam_submit'), data=json.dumps({
            'answers': [{
                'word_id': self.w1.id, 'type': 'choice',
                'selected': 0, 'unanswered': False,
            }],
            'duration': 10,
        }), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        rec = StudyRecord.objects.filter(
            word=self.w1, action__in=['meaning_wrong', 'spelling_wrong', 'unknown']).first()
        self.assertIsNotNone(rec)
        self.assertEqual(rec.source, 'exam')
        self.assertEqual(rec.mode, 'exam_choice')


class RiskEngineBatchTests(TestCase):
    """批量风险查询与同优先级随机排序的回归测试。"""

    def setUp(self):
        self.unit = Unit.objects.create(number=1, name='测试单元', category='required')
        self.w1 = Word.objects.create(word='batch1', category='required', unit=self.unit,
                                      list_number=1, meanings='["m1"]')
        self.w2 = Word.objects.create(word='batch2', category='required', unit=self.unit,
                                      list_number=2, meanings='["m2"]')
        self.w3 = Word.objects.create(word='batch3', category='required', unit=self.unit,
                                      list_number=3, meanings='["m3"]')

    def _wrong(self, word, mistake_type='spelling'):
        StudyRecord.objects.create(
            word=word, action='spelling_wrong', source='review',
            mistake_type=mistake_type,
            error_detail={'user_answer': 'x', 'correct': word.word},
        )

    def test_batch_error_events_groups_by_word(self):
        self._wrong(self.w1)
        self._wrong(self.w2, mistake_type='meaning')
        StudyRecord.objects.create(
            word=self.w3, action='known', source='review', mistake_type='',
            error_detail={},
        )  # 正确动作不进错误事件池
        buckets = _batch_error_events([self.w1.id, self.w2.id, self.w3.id])
        self.assertEqual(len(buckets[self.w1.id]), 1)
        self.assertEqual(len(buckets[self.w2.id]), 1)
        self.assertNotIn(self.w3.id, buckets)

    def test_batch_events_produce_same_score_as_single(self):
        p = StudyProgress.objects.create(word=self.w1, status='learning', error_count=2)
        self._wrong(self.w1)
        self._wrong(self.w1)
        single = compute_risk_score(p)
        batched = compute_risk_score(p, events=_batch_error_events([self.w1.id]).get(self.w1.id, []))
        self.assertEqual(single[0], batched[0])
        self.assertEqual(single[1], batched[1])

    def test_jitter_only_shuffles_within_same_priority_group(self):
        """随机只发生在相同排序 key 的连续区间，不能跨优先级交换。"""
        today = timezone.localdate()
        # 三个词：两个同手动等级/同风险/同到期，一个手动等级更高
        ps = []
        for i, w in enumerate([self.w1, self.w2, self.w3]):
            p = StudyProgress.objects.create(
                word=w, status='learning', error_count=1,
                learned_date=today - timedelta(days=1),
                manual_level=3,
            )
            self._wrong(w)
            ps.append(p)
        p_high = StudyProgress.objects.create(
            word=Word.objects.create(word='prio', category='required', unit=self.unit,
                                     list_number=4, meanings='["p"]'),
            status='learning', error_count=1,
            learned_date=today - timedelta(days=1),
            manual_level=1,  # 人工重点，必须始终排最前
        )
        self._wrong(p_high.word)
        ps.append(p_high)

        # 直接验证分组打乱逻辑：构造排序 key 相同/不同混合
        class FakeP:
            def __init__(self, key, name):
                self.key = key
                self.name = name
        # key 越小优先级越高；排序后高优先级应占据前两位，低优先级后两位
        items = [
            FakeP((2, -1, 1.0, 1), 'low1'),
            FakeP((2, -1, 1.0, 1), 'low2'),
            FakeP((0, -1, 5.0, 1), 'high1'),
            FakeP((0, -1, 5.0, 1), 'high2'),
        ]
        ordered = _sort_bucket_with_risk_jitter(items, lambda x: x.key)
        names = [x.name for x in ordered]
        self.assertIn(names[0], ('high1', 'high2'))
        self.assertIn(names[1], ('high1', 'high2'))
        self.assertIn(names[2], ('low1', 'low2'))
        self.assertIn(names[3], ('low1', 'low2'))
        self.assertLess(names.index('high1'), names.index('low1'))
        self.assertLess(names.index('high2'), names.index('low2'))


class PosLearningTests(TestCase):
    """词性学习 / 词族与构词法（HANDOVER 第十二节验收清单）：
    模型回退、AI 非法 JSON、客观题本地判分、开放题 wrong 写 pos 事件、
    partial 不加风险、候选确认/拒绝、备份恢复、断网/超时零写入。"""

    def setUp(self):
        self.unit = Unit.objects.create(number=1, name='测试单元', category='required')
        self.word = Word.objects.create(
            word='conduct', category='required', unit=self.unit, list_number=1,
            meanings='["传导；实施", "行为；举止"]',
            meanings_by_pos='{"v.": ["传导；实施"], "n.": ["行为；举止"]}',
            word_forms='{"第三人称单数": "conducts", "过去式": "conducted"}',
            pos='v. n.',
        )
        self.model = AIModel.objects.create(
            provider='test', model_id='test-model-1', display_name='测试模型1',
            base_url='https://example.com/v1', enabled=True,
        )

    def _post(self, url_name, body, **kwargs):
        return self.client.post(
            reverse(url_name, kwargs=kwargs or None),
            data=json.dumps(body), content_type='application/json')

    # --- 1. 模型回退链 ---
    def test_model_fallback_chain(self):
        settings_obj = UserSettings.get_settings()
        # 未配置任何功能模型：回退第一个启用模型
        self.assertEqual(_resolve_pos_learning_model_id({}), self.model.id)
        # 显式指定优先
        m2 = AIModel.objects.create(provider='test', model_id='test-model-2', enabled=True)
        self.assertEqual(_resolve_pos_learning_model_id({'model_id': m2.id}), m2.id)
        # 显式指定禁用模型 → None（禁用模型不可用）
        m3 = AIModel.objects.create(provider='test', model_id='test-model-3', enabled=False)
        self.assertIsNone(_resolve_pos_learning_model_id({'model_id': m3.id}))
        # 复审模型回退
        settings_obj.review_model = m2
        settings_obj.save()
        self.assertEqual(_resolve_pos_learning_model_id({}), m2.id)
        # 词性学习模型优先于复审模型
        settings_obj.pos_learning_model = self.model
        settings_obj.save()
        self.assertEqual(_resolve_pos_learning_model_id({}), self.model.id)

    # --- 2. 本地讲解不依赖 AI ---
    def test_local_lesson_without_ai(self):
        res = self._post('words:api_pos_lesson', {'word_id': self.word.id, 'use_ai': False})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(len(data['lesson']['pos_groups']), 2)
        self.assertTrue(data['exercises'])
        self.assertFalse(data['cache_hit'])
        # 本地出题答案必须在选项中
        for ex in data['exercises']:
            self.assertIn(ex['answer'], ex['options'])

    # --- 3. AI 非法 JSON → 502 + fallback，零写入 ---
    @patch('words.views.builtin_ai_exam_call')
    def test_lesson_invalid_json_zero_write(self, mock_call):
        mock_call.return_value = ('这根本不是JSON', None)
        res = self._post('words:api_pos_lesson', {'word_id': self.word.id, 'use_ai': True})
        self.assertEqual(res.status_code, 502)
        self.assertIn('fallback', res.json())
        self.assertFalse(PosLessonCache.objects.exists())

    # --- 4. AI 网络失败 → 502，零写入（断网/超时） ---
    @patch('words.views.builtin_ai_exam_call')
    def test_lesson_network_error_zero_write(self, mock_call):
        mock_call.return_value = (None, '连接超时')
        res = self._post('words:api_pos_lesson', {'word_id': self.word.id, 'use_ai': True})
        self.assertEqual(res.status_code, 502)
        self.assertIn('fallback', res.json())
        self.assertFalse(PosLessonCache.objects.exists())

    # --- 5. AI 合法讲解 → 校验通过 + 缓存命中 ---
    @patch('words.views.builtin_ai_exam_call')
    def test_lesson_ai_success_and_cache(self, mock_call):
        payload = {
            'lesson_version': 1, 'word': 'conduct',
            'pos_groups': [{'pos': 'v.', 'meaning': '传导', 'usage': 'u', 'memory_tip': 'm'}],
            'confusions': [{'word': 'conduit', 'difference': 'd', 'confidence': 0.8}],
            'patterns': [{'name': 'con-', 'rule': '共同', 'exception': ''}],
            'exercises': [{'type': 'pos_choice', 'prompt': 'p',
                           'options': ['v.', 'n.'], 'answer': 'v.'}],
        }
        mock_call.return_value = (json.dumps(payload, ensure_ascii=False), None)
        res = self._post('words:api_pos_lesson', {'word_id': self.word.id, 'use_ai': True})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn('ai_lesson', data)
        self.assertFalse(data['cache_hit'])
        self.assertTrue(PosLessonCache.objects.filter(word=self.word).exists())
        # 第二次：缓存命中，不再调 AI
        mock_call.reset_mock()
        res2 = self._post('words:api_pos_lesson', {'word_id': self.word.id, 'use_ai': True})
        self.assertEqual(res2.status_code, 200)
        self.assertTrue(res2.json()['cache_hit'])
        mock_call.assert_not_called()

    # --- 6. 客观题本地判分：答对不写事件，答错写 pos 事件 ---
    def test_objective_grade_local(self):
        q = {'type': 'pos_choice', 'target_pos': 'v.', 'prompt': 'p',
             'options': ['v.', 'n.'], 'answer': 'v.'}
        res = self._post('words:api_pos_grade',
                         {'word_id': self.word.id, 'question': q, 'answer': ' v. '})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data['result'], 'correct')
        self.assertEqual(data['judged_by'], 'local')
        self.assertFalse(StudyRecord.objects.filter(mistake_type='pos').exists())

        res2 = self._post('words:api_pos_grade',
                          {'word_id': self.word.id, 'question': q, 'answer': 'n.'})
        self.assertEqual(res2.json()['result'], 'wrong')
        rec = StudyRecord.objects.get(mistake_type='pos')
        self.assertEqual(rec.source, 'pos')
        self.assertEqual(rec.error_detail['question_type'], 'pos_choice')
        self.assertEqual(rec.error_detail['target_pos'], 'v.')

    # --- 7. 幂等：已判定 attempt 直接返回 ---
    def test_grade_idempotent(self):
        attempt = PosLearningAttempt.objects.create(
            word=self.word, target_pos='v.', question_type='pos_choice',
            question={'type': 'pos_choice', 'answer': 'v.'},
            user_answer='v.', result='correct', judged_by='local')
        res = self._post('words:api_pos_grade',
                         {'attempt_id': attempt.id, 'answer': 'n.'})
        data = res.json()
        self.assertEqual(data['result'], 'correct')
        self.assertTrue(data['idempotent'])

    # --- 8. 开放题 wrong 写 pos 事件 ---
    @patch('words.views._ai_judge_call')
    def test_open_grade_wrong_writes_pos_event(self, mock_call):
        mock_call.return_value = (json.dumps(
            {'verdict': 'wrong', 'feedback': '词性用错', 'error_type': 'pos'}), '', '')
        q = {'type': 'open', 'target_pos': 'v.', 'prompt': '造句', 'reference': ''}
        res = self._post('words:api_pos_grade',
                         {'word_id': self.word.id, 'question': q,
                          'answer': 'He conduct is good.'})
        data = res.json()
        self.assertEqual(data['result'], 'wrong')
        self.assertEqual(data['judged_by'], 'ai')
        rec = StudyRecord.objects.get(mistake_type='pos')
        self.assertEqual(rec.source, 'pos')
        attempt = PosLearningAttempt.objects.get(word=self.word)
        self.assertEqual(attempt.result, 'wrong')

    # --- 9. 开放题 partial 不加风险 ---
    @patch('words.views._ai_judge_call')
    def test_open_grade_partial_no_event(self, mock_call):
        mock_call.return_value = (json.dumps(
            {'verdict': 'partial', 'feedback': '基本可以'}), '', '')
        q = {'type': 'open', 'target_pos': 'n.', 'prompt': '造句', 'reference': ''}
        res = self._post('words:api_pos_grade',
                         {'word_id': self.word.id, 'question': q,
                          'answer': 'His conduct was fine.'})
        self.assertEqual(res.json()['result'], 'partial')
        self.assertFalse(StudyRecord.objects.filter(mistake_type='pos').exists())

    # --- 10. 开放题 AI 越界输出 / 失败 → 落待复核（不判错、零错误事件） ---
    @patch('words.views._ai_judge_call')
    def test_open_grade_invalid_verdict_pending_review(self, mock_call):
        mock_call.return_value = (json.dumps({'verdict': 'great'}), '', '')
        q = {'type': 'open', 'target_pos': 'v.', 'prompt': '造句', 'reference': ''}
        res = self._post('words:api_pos_grade',
                         {'word_id': self.word.id, 'question': q, 'answer': 'x'})
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['result'], 'pending_review')
        self.assertEqual(data['reason_code'], 'ai_invalid_output')
        attempt = PosLearningAttempt.objects.get(word=self.word)
        self.assertEqual(attempt.result, 'pending_review')
        self.assertTrue(attempt.pending_review)
        self.assertFalse(StudyRecord.objects.filter(mistake_type='pos').exists())
        self.assertFalse(StudyProgress.objects.filter(
            word=self.word, error_count__gt=0).exists())
        self.assertTrue(PendingJudgment.objects.filter(
            word=self.word, kind='pos_open', resolved=False).exists())

    @patch('words.views._ai_judge_call')
    def test_open_grade_network_error_pending_review(self, mock_call):
        mock_call.return_value = (None, '网络断开', 'ai_network_error')
        q = {'type': 'open', 'target_pos': 'v.', 'prompt': '造句', 'reference': ''}
        res = self._post('words:api_pos_grade',
                         {'word_id': self.word.id, 'question': q, 'answer': 'x'})
        data = res.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['result'], 'pending_review')
        self.assertEqual(data['reason_code'], 'ai_network_error')
        attempt = PosLearningAttempt.objects.get(word=self.word)
        self.assertTrue(attempt.pending_review)
        self.assertFalse(StudyRecord.objects.filter(mistake_type='pos').exists())

    # --- 11. 词族候选生成：词库外词标记 outside_word，未确认状态 ---
    @patch('words.views.builtin_ai_exam_call')
    def test_family_candidate_generation(self, mock_call):
        other = Word.objects.create(
            word='conduction', category='required', unit=self.unit,
            list_number=2, meanings='["传导"]')
        payload = {
            'family': {'key': 'conduct-', 'title': 'conduct 词族', 'description': 'd'},
            'members': [
                {'word': 'conduct', 'pos': 'v.', 'relation': 'core', 'note': ''},
                {'word': 'conduction', 'pos': 'n.', 'relation': 'derived', 'note': ''},
                {'word': 'conductivity', 'pos': 'n.', 'relation': 'derived', 'note': ''},
            ],
            'patterns': [{'name': '-tion', 'kind': 'suffix', 'from_pos': 'v.',
                          'to_pos': 'n.', 'rule': 'r', 'exception': ''}],
            'relations': [{'word': 'behavior', 'rel_type': 'synonym',
                           'note': '', 'confidence': 0.7}],
        }
        mock_call.return_value = (json.dumps(payload, ensure_ascii=False), None)
        res = self._post('words:api_word_family_candidates', {'word_id': self.word.id})
        self.assertEqual(res.status_code, 200)
        family = WordFamily.objects.get()
        self.assertEqual(family.source, 'ai_candidate')
        self.assertIsNone(family.confirmed_at)
        members = {m.word.word if m.word else m.outside_word: m
                   for m in family.members.all()}
        self.assertEqual(members['conduct'].word, self.word)
        self.assertEqual(members['conduction'].word, other)
        self.assertEqual(members['conductivity'].outside_word, 'conductivity')
        self.assertIsNone(members['conductivity'].word)
        # 词库外词绝不创建 Word
        self.assertFalse(Word.objects.filter(word='conductivity').exists())
        rel = family.relations.get()
        self.assertEqual(rel.outside_word, 'behavior')
        self.assertIsNone(rel.related_word)
        self.assertFalse(rel.is_confirmed)

    # --- 12. 候选生成失败零写入 ---
    @patch('words.views.builtin_ai_exam_call')
    def test_family_candidate_error_zero_write(self, mock_call):
        mock_call.return_value = (None, '连接超时')
        res = self._post('words:api_word_family_candidates', {'word_id': self.word.id})
        self.assertEqual(res.status_code, 502)
        self.assertFalse(WordFamily.objects.exists())

    @patch('words.views.builtin_ai_exam_call')
    def test_family_candidate_invalid_json_zero_write(self, mock_call):
        mock_call.return_value = ('{"family": {}}', None)  # 缺 key/title/members
        res = self._post('words:api_word_family_candidates', {'word_id': self.word.id})
        self.assertEqual(res.status_code, 502)
        self.assertFalse(WordFamily.objects.exists())

    # --- 13. 候选确认 / 拒绝 / 重复确认 ---
    def _make_candidate(self):
        family = WordFamily.objects.create(
            key='cand-1', title='测试词族', source='ai_candidate')
        WordFamilyMember.objects.create(
            family=family, word=self.word, relation='core', source='ai_candidate')
        WordFormationPattern.objects.create(
            family=family, name='-x', kind='suffix', is_confirmed=False,
            source='ai_candidate')
        return family

    def test_candidate_confirm(self):
        family = self._make_candidate()
        res = self._post('words:api_word_family_candidate_confirm',
                         {'action': 'confirm'}, cid=family.id)
        self.assertEqual(res.status_code, 200)
        family.refresh_from_db()
        self.assertIsNotNone(family.confirmed_at)
        self.assertIsNotNone(family.members.get().confirmed_at)
        self.assertTrue(family.patterns.get().is_confirmed)

    def test_candidate_reject_deletes_batch(self):
        family = self._make_candidate()
        res = self._post('words:api_word_family_candidate_confirm',
                         {'action': 'reject'}, cid=family.id)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(WordFamily.objects.exists())
        self.assertFalse(WordFamilyMember.objects.exists())
        self.assertFalse(WordFormationPattern.objects.exists())

    def test_candidate_confirm_twice_rejected(self):
        family = self._make_candidate()
        self._post('words:api_word_family_candidate_confirm',
                   {'action': 'confirm'}, cid=family.id)
        res = self._post('words:api_word_family_candidate_confirm',
                         {'action': 'confirm'}, cid=family.id)
        self.assertEqual(res.status_code, 400)

    # --- 14. 知识反馈：irrelevant 立即屏蔽未确认项 ---
    def test_knowledge_feedback_irrelevant_hides_pattern(self):
        pattern = WordFormationPattern.objects.create(
            family=None, name='-y', kind='suffix', is_confirmed=True, source='manual')
        res = self._post('words:api_knowledge_feedback',
                         {'target_type': 'pattern', 'target_id': pattern.id,
                          'action': 'irrelevant'})
        self.assertEqual(res.status_code, 200)
        pattern.refresh_from_db()
        self.assertFalse(pattern.is_confirmed)

    def test_knowledge_feedback_invalid_target(self):
        res = self._post('words:api_knowledge_feedback',
                         {'target_type': 'word', 'target_id': 99999, 'action': 'focus'})
        self.assertEqual(res.status_code, 404)

    # --- 15. 备份导出包含新表 + 恢复往返 ---
    def test_backup_restore_roundtrip(self):
        past = timezone.now() - timedelta(days=1)
        attempt = PosLearningAttempt.objects.create(
            word=self.word, target_pos='v.', question_type='pos_choice',
            question={'type': 'pos_choice', 'answer': 'v.'},
            user_answer='v.', result='correct', judged_by='local')
        family = WordFamily.objects.create(
            key='fam-1', title='往返词族', source='ai_candidate',
            confirmed_at=timezone.now())
        WordFamilyMember.objects.create(
            family=family, word=self.word, relation='core', source='ai_candidate',
            confirmed_at=timezone.now())
        WordRelation.objects.create(
            family=family, word=self.word, related_word=None,
            outside_word='demeanor', rel_type='synonym', source='ai_candidate')
        KnowledgeFeedback.objects.create(
            target_type='word', target_id=self.word.id, action='focus')

        payload = _build_backup_payload(UserSettings.get_settings())
        for key in ('pos_attempts', 'word_families', 'knowledge_feedback'):
            self.assertIn(key, payload)
        self.assertEqual(len(payload['pos_attempts']), 1)
        self.assertEqual(len(payload['word_families']), 1)

        PosLearningAttempt.objects.all().delete()
        WordFamily.objects.all().delete()
        KnowledgeFeedback.objects.all().delete()

        res = self.client.post(reverse('words:api_restore'),
                               data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        restored_attempt = PosLearningAttempt.objects.get()
        self.assertEqual(restored_attempt.word, self.word)
        self.assertEqual(restored_attempt.result, 'correct')
        self.assertEqual(restored_attempt.created_at.date(), attempt.created_at.date())
        restored_family = WordFamily.objects.get(key='fam-1')
        self.assertTrue(restored_family.is_confirmed)
        self.assertEqual(restored_family.members.get().word, self.word)
        self.assertEqual(restored_family.relations.get().outside_word, 'demeanor')
        fb = KnowledgeFeedback.objects.get()
        self.assertEqual(fb.target_type, 'word')
        self.assertEqual(fb.target_id, self.word.id)

    # --- 16. 词性答错进入错误知识库：更新 StudyProgress 计数/状态/排期 ---
    def test_wrong_updates_study_progress(self):
        q = {'type': 'pos_choice', 'target_pos': 'v.', 'prompt': 'p',
             'options': ['v.', 'n.'], 'answer': 'v.'}
        self.assertFalse(StudyProgress.objects.filter(word=self.word).exists())
        res = self._post('words:api_pos_grade',
                         {'word_id': self.word.id, 'question': q, 'answer': 'n.'})
        self.assertEqual(res.json()['result'], 'wrong')
        prog = StudyProgress.objects.get(word=self.word)
        self.assertEqual(prog.error_count, 1)
        self.assertEqual(prog.status, 'learning')
        self.assertEqual(prog.review_count, 1)
        self.assertIsNotNone(prog.next_review)

        # 已掌握词答错：保持「会」不降级，仅错误计数+1、连续答对清零并排明天复习
        prog.status = 'mastered'
        prog.mastery_level = 5
        prog.consecutive_correct = 10
        prog.save()
        self._post('words:api_pos_grade',
                   {'word_id': self.word.id, 'question': q, 'answer': 'n.'})
        prog.refresh_from_db()
        self.assertEqual(prog.status, 'mastered')
        self.assertEqual(prog.error_count, 2)
        self.assertEqual(prog.consecutive_correct, 0)
        self.assertIsNotNone(prog.next_review)

    # --- 17. partial 不增加错误风险（不创建/改动 StudyProgress） ---
    @patch('words.views._ai_judge_call')
    def test_partial_does_not_touch_progress(self, mock_call):
        mock_call.return_value = (json.dumps(
            {'verdict': 'partial', 'feedback': 'ok'}), '', '')
        q = {'type': 'open', 'target_pos': 'n.', 'prompt': '造句', 'reference': ''}
        self._post('words:api_pos_grade',
                   {'word_id': self.word.id, 'question': q, 'answer': 'x'})
        self.assertFalse(StudyProgress.objects.filter(
            word=self.word, error_count__gt=0).exists())

    # --- 18. 词性学习模型随备份导出/恢复；旧备份缺键确定置空 ---
    def test_backup_restores_pos_learning_model(self):
        settings_obj = UserSettings.get_settings()
        settings_obj.pos_learning_model = self.model
        settings_obj.save()
        payload = _build_backup_payload(settings_obj)
        self.assertEqual(payload['settings']['pos_learning_model_id'], self.model.id)

        settings_obj.pos_learning_model = None
        settings_obj.save()
        res = self.client.post(reverse('words:api_restore'),
                               data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        new_id = AIModel.objects.get(model_id='test-model-1').id
        self.assertEqual(UserSettings.get_settings().pos_learning_model_id, new_id)

    def test_backup_without_pos_model_key_clears_setting(self):
        settings_obj = UserSettings.get_settings()
        settings_obj.pos_learning_model = self.model
        settings_obj.save()
        payload = _build_backup_payload(settings_obj)
        del payload['settings']['pos_learning_model_id']
        res = self.client.post(reverse('words:api_restore'),
                               data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(UserSettings.get_settings().pos_learning_model_id)

    # --- 19. 知识反馈 member/pattern/relation 稳定引用往返恢复 ---
    def test_knowledge_feedback_member_pattern_relation_roundtrip(self):
        family = WordFamily.objects.create(
            key='fam-fb', title='反馈词族', source='manual', confirmed_at=timezone.now())
        member = WordFamilyMember.objects.create(
            family=family, word=self.word, relation='core', pos='v.',
            source='manual', confirmed_at=timezone.now())
        pattern = WordFormationPattern.objects.create(
            family=family, name='-tion', kind='suffix', source='manual', is_confirmed=True)
        rel = WordRelation.objects.create(
            family=family, word=self.word, outside_word='demeanor',
            rel_type='synonym', source='manual', is_confirmed=True)
        KnowledgeFeedback.objects.create(target_type='member', target_id=member.id, action='focus')
        KnowledgeFeedback.objects.create(target_type='pattern', target_id=pattern.id, action='irrelevant')
        KnowledgeFeedback.objects.create(target_type='relation', target_id=rel.id, action='focus')

        payload = _build_backup_payload(UserSettings.get_settings())
        refs = {fb['target_type']: fb['ref'] for fb in payload['knowledge_feedback']}
        self.assertEqual(refs['member']['family_key'], 'fam-fb')
        self.assertEqual(refs['member']['word'], self.word.word)
        self.assertEqual(refs['pattern']['name'], '-tion')
        self.assertEqual(refs['relation']['rel_type'], 'synonym')

        WordFamily.objects.all().delete()
        KnowledgeFeedback.objects.all().delete()

        res = self.client.post(reverse('words:api_restore'),
                               data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(KnowledgeFeedback.objects.count(), 3)
        new_family = WordFamily.objects.get(key='fam-fb')
        got = {fb.target_type: fb.target_id for fb in KnowledgeFeedback.objects.all()}
        self.assertEqual(got['member'], new_family.members.get().id)
        self.assertEqual(got['pattern'], new_family.patterns.get().id)
        self.assertEqual(got['relation'], new_family.relations.get().id)

    def test_knowledge_feedback_unresolvable_counted(self):
        payload = {'knowledge_feedback': [
            {'target_type': 'member',
             'ref': {'family_key': '不存在', 'word': 'x', 'relation': 'core'},
             'action': 'focus'},
        ]}
        res = self.client.post(reverse('words:api_restore'),
                               data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        self.assertIn('knowledge_feedback', res.json()['message'])
        self.assertFalse(KnowledgeFeedback.objects.exists())

    # --- 20. 词族候选同 key 复用不 500，既有词族不被覆盖 ---
    def _candidate_payload(self, key='fam-x'):
        return {
            'family': {'key': key, 'title': '词族X', 'description': ''},
            'members': [{'word': self.word.word, 'pos': 'v.',
                         'relation': 'core', 'note': ''}],
            'patterns': [], 'relations': [],
        }

    @patch('words.views.builtin_ai_exam_call')
    def test_candidate_same_key_reused_not_500(self, mock_call):
        mock_call.return_value = (
            json.dumps(self._candidate_payload(), ensure_ascii=False), None)
        res1 = self._post('words:api_word_family_candidates', {'word_id': self.word.id})
        self.assertEqual(res1.status_code, 200)
        self.assertFalse(res1.json().get('reused'))
        first_id = res1.json()['candidate_id']
        # 第二次同 key：复用，不新建、不覆盖，返回正常响应
        res2 = self._post('words:api_word_family_candidates', {'word_id': self.word.id})
        self.assertEqual(res2.status_code, 200)
        self.assertTrue(res2.json()['reused'])
        self.assertEqual(res2.json()['candidate_id'], first_id)
        self.assertEqual(WordFamily.objects.count(), 1)

    @patch('words.views.builtin_ai_exam_call')
    def test_candidate_does_not_overwrite_confirmed_family(self, mock_call):
        family = WordFamily.objects.create(
            key='fam-x', title='既有确认词族', source='manual', confirmed_at=timezone.now())
        mock_call.return_value = (
            json.dumps(self._candidate_payload(), ensure_ascii=False), None)
        res = self._post('words:api_word_family_candidates', {'word_id': self.word.id})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['reused'])
        family.refresh_from_db()
        self.assertEqual(family.title, '既有确认词族')
        self.assertTrue(family.is_confirmed)

    # --- 21. 词族变式读取接口 ---
    def test_word_family_get_returns_members_and_outside(self):
        url = reverse('words:api_word_family_get', args=[self.word.id])
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.json()['family'])  # 无词族

        family = WordFamily.objects.create(
            key='fam-get', title='get词族', source='manual', confirmed_at=timezone.now())
        WordFamilyMember.objects.create(
            family=family, word=self.word, relation='core', pos='v.',
            source='manual', confirmed_at=timezone.now())
        WordFamilyMember.objects.create(
            family=family, word=None, outside_word='operator', relation='derived',
            pos='n.', meaning='操作员', sort_order=1, source='manual',
            confirmed_at=timezone.now())
        data = self.client.get(url).json()
        self.assertTrue(data['family']['is_confirmed'])
        self.assertEqual(data['family']['key'], 'fam-get')
        words = [m['word'] for m in data['members']]
        self.assertIn(self.word.word, words)
        self.assertIn('operator', words)
        out = [m for m in data['members'] if m['word'] == 'operator'][0]
        self.assertFalse(out['in_library'])
        self.assertEqual(out['meaning'], '操作员')  # 库外词用保存的 AI 释义
        core = [m for m in data['members'] if m['word'] == self.word.word][0]
        self.assertEqual(core['meaning'],
                         '；'.join(self.word.get_meanings()))  # 库内词取词库释义

    @patch('words.views.builtin_ai_exam_call')
    def test_candidate_confirm_writes_confirmed_with_meaning(self, mock_call):
        payload = self._candidate_payload()
        payload['members'][0]['meaning'] = '有效的'
        mock_call.return_value = (json.dumps(payload, ensure_ascii=False), None)
        res = self._post('words:api_word_family_candidates',
                         {'word_id': self.word.id, 'confirm': True})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['confirmed'])
        fam = WordFamily.objects.get(key='fam-x')
        self.assertIsNotNone(fam.confirmed_at)
        member = fam.members.first()
        self.assertIsNotNone(member.confirmed_at)
        self.assertEqual(member.meaning, '有效的')

    def test_word_family_get_prefers_confirmed(self):
        cand = WordFamily.objects.create(
            key='fam-cand', title='候选', source='ai_candidate', confirmed_at=None)
        WordFamilyMember.objects.create(
            family=cand, word=self.word, relation='core', source='ai_candidate')
        conf = WordFamily.objects.create(
            key='fam-conf', title='确认', source='manual', confirmed_at=timezone.now())
        WordFamilyMember.objects.create(
            family=conf, word=self.word, relation='core', source='manual',
            confirmed_at=timezone.now())
        data = self.client.get(
            reverse('words:api_word_family_get', args=[self.word.id])).json()
        self.assertEqual(data['family']['key'], 'fam-conf')


class PosExpandTests(TestCase):
    """AI 词性扩展（HANDOVER 第十四节验收）：成功写回、已有词性跳过不覆盖、
    白名单过滤、无模型/非法 JSON/超时零写入、批量只处理今日自选缺词性词。"""

    def setUp(self):
        self.unit = Unit.objects.create(number=1, name='测试单元', category='required')
        # 缺词性释义的词
        self.word = Word.objects.create(
            word='conduct', category='required', unit=self.unit, list_number=1,
            meanings='["传导；实施", "行为；举止"]', pos='v. n.',
            meanings_by_pos='{}')
        # 已有有效词性释义的词
        self.word_full = Word.objects.create(
            word='present', category='required', unit=self.unit, list_number=2,
            meanings='["呈现", "礼物"]', pos='v. n.',
            meanings_by_pos='{"v.": ["呈现"], "n.": ["礼物"]}')
        # 全部归「未分类」的词（视为缺词性）
        self.word_unclassified = Word.objects.create(
            word='yield', category='required', unit=self.unit, list_number=3,
            meanings='["产出；屈服"]', pos='v.',
            meanings_by_pos='{"未分类": ["产出；屈服"]}')
        self.model = AIModel.objects.create(
            provider='test', model_id='test-model-1', enabled=True)

    def _post(self, url_name, body, **kwargs):
        return self.client.post(
            reverse(url_name, kwargs=kwargs or None),
            data=json.dumps(body), content_type='application/json')

    # --- 1. 单个扩展成功写回 ---
    @patch('words.views.builtin_ai_exam_call')
    def test_single_expand_success(self, mock_call):
        mock_call.return_value = (json.dumps(
            {'conduct': {'v.': ['传导；实施'], 'n.': ['行为；举止']}}, ensure_ascii=False), None)
        res = self._post('words:api_word_pos_expand', {}, word_id=self.word.id)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data['updated'])
        self.word.refresh_from_db()
        self.assertEqual(self.word.get_meanings_by_pos(),
                         {'v.': ['传导；实施'], 'n.': ['行为；举止']})

    # --- 2. 已有有效词性释义：跳过不覆盖，且不调用 AI ---
    @patch('words.views.builtin_ai_exam_call')
    def test_single_expand_skip_existing(self, mock_call):
        res = self._post('words:api_word_pos_expand', {}, word_id=self.word_full.id)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data['updated'])
        self.assertIn('skipped_reason', data)
        mock_call.assert_not_called()
        self.word_full.refresh_from_db()
        self.assertEqual(self.word_full.get_meanings_by_pos(),
                         {'v.': ['呈现'], 'n.': ['礼物']})

    # --- 3. 全部「未分类」视为缺词性，可扩展并替换 ---
    @patch('words.views.builtin_ai_exam_call')
    def test_single_expand_unclassified(self, mock_call):
        mock_call.return_value = (json.dumps(
            {'yield': {'v.': ['产出', '屈服']}}, ensure_ascii=False), None)
        res = self._post('words:api_word_pos_expand', {}, word_id=self.word_unclassified.id)
        self.assertTrue(res.json()['updated'])
        self.word_unclassified.refresh_from_db()
        self.assertEqual(self.word_unclassified.get_meanings_by_pos(),
                         {'v.': ['产出', '屈服']})

    # --- 4. 无可用 AI 模型 → 400，零写入 ---
    def test_single_expand_no_model(self):
        AIModel.objects.all().update(enabled=False)
        res = self._post('words:api_word_pos_expand', {}, word_id=self.word.id)
        self.assertEqual(res.status_code, 400)
        self.word.refresh_from_db()
        self.assertEqual(self.word.get_meanings_by_pos(), {})

    # --- 5. AI 非法 JSON → 502，零写入 ---
    @patch('words.views.builtin_ai_exam_call')
    def test_single_expand_invalid_json_zero_write(self, mock_call):
        mock_call.return_value = ('不是JSON', None)
        res = self._post('words:api_word_pos_expand', {}, word_id=self.word.id)
        self.assertEqual(res.status_code, 502)
        self.word.refresh_from_db()
        self.assertEqual(self.word.get_meanings_by_pos(), {})

    # --- 6. AI 网络失败 → 502，零写入 ---
    @patch('words.views.builtin_ai_exam_call')
    def test_single_expand_network_error_zero_write(self, mock_call):
        mock_call.return_value = (None, '无法连接 AI 服务')
        res = self._post('words:api_word_pos_expand', {}, word_id=self.word.id)
        self.assertEqual(res.status_code, 502)
        self.word.refresh_from_db()
        self.assertEqual(self.word.get_meanings_by_pos(), {})

    # --- 7. 词性白名单：非法标签丢弃、全部非法则 502 零写入 ---
    @patch('words.views.builtin_ai_exam_call')
    def test_single_expand_whitelist_filter(self, mock_call):
        mock_call.return_value = (json.dumps(
            {'conduct': {'v.': ['传导'], '名词': ['行为'], 'adj.': ['导电的']}},
            ensure_ascii=False), None)
        res = self._post('words:api_word_pos_expand', {}, word_id=self.word.id)
        data = res.json()
        self.assertTrue(data['updated'])
        self.word.refresh_from_db()
        # 「名词」被丢弃；v./adj. 保留（adj. 在白名单内）
        self.assertEqual(self.word.get_meanings_by_pos(),
                         {'v.': ['传导'], 'adj.': ['导电的']})
        self.assertTrue(data['warnings'])

    @patch('words.views.builtin_ai_exam_call')
    def test_single_expand_all_invalid_zero_write(self, mock_call):
        mock_call.return_value = (json.dumps(
            {'conduct': {'名词': ['行为']}}, ensure_ascii=False), None)
        res = self._post('words:api_word_pos_expand', {}, word_id=self.word.id)
        self.assertEqual(res.status_code, 502)
        self.word.refresh_from_db()
        self.assertEqual(self.word.get_meanings_by_pos(), {})

    # --- 8. 批量：只处理缺词性词，计数正确，成功词写回 ---
    @patch('words.views.builtin_ai_exam_call')
    def test_batch_expand_counts(self, mock_call):
        today = timezone.localdate()
        DailyPick.objects.create(date=today, word=self.word)
        DailyPick.objects.create(date=today, word=self.word_full)
        DailyPick.objects.create(date=today, word=self.word_unclassified)
        mock_call.return_value = (json.dumps({
            'conduct': {'v.': ['传导'], 'n.': ['行为']},
            'yield': {'v.': ['产出']},
        }, ensure_ascii=False), None)
        res = self._post('words:api_daily_pick_pos_expand', {})
        data = res.json()
        self.assertEqual(data['total'], 3)
        self.assertEqual(data['updated'], 2)
        self.assertEqual(data['failed'], 0)
        self.assertEqual(data['skipped'], 1)  # word_full 已有词性释义
        self.word.refresh_from_db()
        self.assertEqual(self.word.get_meanings_by_pos(), {'v.': ['传导'], 'n.': ['行为']})
        self.word_unclassified.refresh_from_db()
        self.assertEqual(self.word_unclassified.get_meanings_by_pos(), {'v.': ['产出']})
        # 已有词性的词不被覆盖
        self.word_full.refresh_from_db()
        self.assertEqual(self.word_full.get_meanings_by_pos(),
                         {'v.': ['呈现'], 'n.': ['礼物']})

    # --- 9. 批量：清单为空 → 400 ---
    def test_batch_expand_empty_pick(self):
        res = self._post('words:api_daily_pick_pos_expand', {})
        self.assertEqual(res.status_code, 400)

    # --- 10. 批量：全部已有词性 → updated=0 不调 AI ---
    @patch('words.views.builtin_ai_exam_call')
    def test_batch_expand_all_full_skip(self, mock_call):
        DailyPick.objects.create(date=timezone.localdate(), word=self.word_full)
        res = self._post('words:api_daily_pick_pos_expand', {})
        data = res.json()
        self.assertEqual(data['updated'], 0)
        self.assertEqual(data['skipped'], 1)
        mock_call.assert_not_called()

    # --- 11. 批量：AI 返回缺某词 → 该词计 failed，其余正常写回 ---
    @patch('words.views.builtin_ai_exam_call')
    def test_batch_expand_partial_failure(self, mock_call):
        today = timezone.localdate()
        DailyPick.objects.create(date=today, word=self.word)
        DailyPick.objects.create(date=today, word=self.word_unclassified)
        # AI 只返回 conduct，缺 yield
        mock_call.return_value = (json.dumps(
            {'conduct': {'v.': ['传导']}}, ensure_ascii=False), None)
        res = self._post('words:api_daily_pick_pos_expand', {})
        data = res.json()
        self.assertEqual(data['updated'], 1)
        self.assertEqual(data['failed'], 1)
        self.word.refresh_from_db()
        self.assertEqual(self.word.get_meanings_by_pos(), {'v.': ['传导']})
        self.word_unclassified.refresh_from_db()
        self.assertEqual(self.word_unclassified.get_meanings_by_pos(),
                         {'未分类': ['产出；屈服']})  # 失败词保持原样

    # --- 12. 批量：AI 整批网络失败 → 全部计 failed，零写入 ---
    @patch('words.views.builtin_ai_exam_call')
    def test_batch_expand_network_error_zero_write(self, mock_call):
        DailyPick.objects.create(date=timezone.localdate(), word=self.word)
        mock_call.return_value = (None, '连接超时')
        res = self._post('words:api_daily_pick_pos_expand', {})
        data = res.json()
        self.assertEqual(data['updated'], 0)
        self.assertEqual(data['failed'], 1)
        self.word.refresh_from_db()
        self.assertEqual(self.word.get_meanings_by_pos(), {})


class AIJudgePolicyTests(TestCase):
    """统一判定策略（本地确定层 → AI 仲裁 → 本地兜底判错）。

    覆盖：断网/DNS、8 秒超时、401/403/404、429、5xx、非法 JSON、verdict 越界、
    连续 3 次熔断 + 冷却后半开探测成功。AI 失败时一律本地兜底判错。"""

    def setUp(self):
        self.unit = Unit.objects.create(number=1, name='测试单元', category='required')
        self.word = Word.objects.create(
            word='abandon', category='required', unit=self.unit, list_number=1,
            meanings='["放弃；抛弃"]')
        self.model = AIModel.objects.create(
            provider='deepseek', model_id='deepseek-chat',
            base_url='https://api.deepseek.com/v1', api_key='sk-test', enabled=True)

    def _data(self):
        return {'model_id': self.model.id}

    def _raising(self, category, message='失败'):
        def _fake(cfg, payload, timeout):
            raise _AIRequestError(category, message)
        return patch('words.views._ai_post_json', side_effect=_fake)

    def _assert_local_fallback(self, policy):
        self.assertEqual(policy['verdict'], 'wrong')
        self.assertTrue(policy['is_final'])
        self.assertEqual(policy['source'], 'local_fallback')
        self.assertEqual(policy['confidence'], 'low')

    def _assert_no_error_side_effects(self):
        self.assertFalse(StudyRecord.objects.filter(word=self.word).exists())
        self.assertFalse(KnowledgeFeedback.objects.exists())
        progress = StudyProgress.objects.filter(word=self.word).first()
        self.assertTrue(progress is None or progress.error_count == 0)

    # --- 1. 空答：确定性题本地立即判定，不出网 ---
    def test_blank_answer_local_deterministic(self):
        with patch('words.views._ai_post_json', side_effect=AssertionError('不应出网')):
            policy = judge_with_policy(self.word, '   ', self._data())
        self.assertEqual(policy['verdict'], 'wrong')
        self.assertEqual(policy['source'], 'local_exact')
        self.assertTrue(policy['is_final'])

    # --- 2. 断网 / DNS 失败 → 本地兜底判错 ---
    def test_network_failure_local_fallback(self):
        with self._raising('network', '无法连接 AI 服务'):
            policy = judge_with_policy(self.word, '坚持', self._data())
        self._assert_local_fallback(policy)
        self._assert_no_error_side_effects()

    # --- 3. 8 秒超时 → 本地兜底判错 ---
    def test_timeout_local_fallback(self):
        with self._raising('timeout', 'AI 请求超时'):
            policy = judge_with_policy(self.word, '坚持', self._data())
        self._assert_local_fallback(policy)

    # --- 4. 401/403/404 配置错误：不重试，本地兜底判错 ---
    def test_config_error_no_retry(self):
        calls = []

        def _fake(cfg, payload, timeout):
            calls.append(timeout)
            raise _AIRequestError('config', '密钥或权限无效 (HTTP 401)')

        with patch('words.views._ai_post_json', side_effect=_fake):
            policy = judge_with_policy(self.word, '坚持', self._data())
        self._assert_local_fallback(policy)
        self.assertEqual(len(calls), 1)  # 配置错误不重试
        self.assertEqual(calls[0], 8)    # 判定专用 8 秒短超时

    # --- 5. 429/5xx 可用性失败：最多快速重试一次 ---
    def test_server_error_retries_once(self):
        calls = []

        def _fake(cfg, payload, timeout):
            calls.append(1)
            raise _AIRequestError('server', '服务端错误 (HTTP 500)')

        with patch('words.views._ai_post_json', side_effect=_fake):
            policy = judge_with_policy(self.word, '坚持', self._data())
        self._assert_local_fallback(policy)
        self.assertEqual(len(calls), 2)

    # --- 6. 返回非 JSON / verdict 越界 → 本地兜底判错 ---
    def test_invalid_output_local_fallback(self):
        with patch('words.views._ai_post_json', return_value='这不是 JSON'):
            policy = judge_with_policy(self.word, '坚持', self._data())
        self._assert_local_fallback(policy)

        with patch('words.views._ai_post_json',
                   return_value='{"verdict": "great", "comment": "x"}'):
            policy = judge_with_policy(self.word, '坚持', self._data())
        self._assert_local_fallback(policy)

    # --- 7. 连续 3 次可用性失败 → 熔断；熔断期不出网，本地兜底判错 ---
    def test_circuit_opens_after_three_failures(self):
        with self._raising('network'):
            for _ in range(AI_HEALTH_FAIL_THRESHOLD):
                judge_with_policy(self.word, '坚持', self._data())
        health = AIModelHealth.objects.get(model=self.model)
        self.assertIsNotNone(health.circuit_open_until)
        self.assertGreaterEqual(health.fail_streak, AI_HEALTH_FAIL_THRESHOLD)

        with patch('words.views._ai_post_json', side_effect=AssertionError('熔断期不应出网')):
            policy = judge_with_policy(self.word, '坚持', self._data())
        self._assert_local_fallback(policy)

    # --- 8. 冷却结束后的半开探测成功 → 熔断关闭、计数清零 ---
    def test_half_open_probe_success_closes_circuit(self):
        with self._raising('network'):
            for _ in range(AI_HEALTH_FAIL_THRESHOLD):
                judge_with_policy(self.word, '坚持', self._data())
        health = AIModelHealth.objects.get(model=self.model)
        health.circuit_open_until = timezone.now() - timedelta(seconds=1)
        health.save()

        with patch('words.views._ai_post_json',
                   return_value='{"verdict": "wrong", "comment": "释义不符"}'):
            policy = judge_with_policy(self.word, '坚持', self._data())
        self.assertEqual(policy['verdict'], 'wrong')
        self.assertEqual(policy['source'], 'ai')
        health.refresh_from_db()
        self.assertIsNone(health.circuit_open_until)
        self.assertEqual(health.fail_streak, 0)


class PendingJudgmentTests(TestCase):
    """HANDOVER 第八节相位 3：待复核队列、人工改判、幂等补判、备份往返。"""

    def setUp(self):
        self.unit = Unit.objects.create(number=1, name='测试单元', category='required')
        self.word = Word.objects.create(
            word='abandon', category='required', unit=self.unit, list_number=1,
            meanings='["放弃；抛弃"]')
        self.model = AIModel.objects.create(
            provider='deepseek', model_id='deepseek-chat',
            base_url='https://api.deepseek.com/v1', api_key='sk-test', enabled=True)

    def _raising(self, category='network'):
        def _fake(cfg, payload, timeout):
            raise _AIRequestError(category, '失败')
        return patch('words.views._ai_post_json', side_effect=_fake)

    def _make_pending(self, answer='坚持'):
        """直接造一条待复核记录（释义判定已恢复本地兜底，不再自动产生 pending）。"""
        key = 'meaning:learn:meaning:%d:abcdef0123456789' % self.word.id
        return _get_or_create_pending_judgment(
            self.word, 'meaning', answer, mode='meaning', source='learn',
            reason_code='ai_network_error', dedupe_key=key)

    def _post(self, url_name, body, **kwargs):
        return self.client.post(
            reverse(url_name, kwargs=kwargs or None),
            data=json.dumps(body), content_type='application/json')

    # --- 1. 待复核记录不判错、不改掌握状态、今日不记错 ---
    def test_meaning_check_pending_no_wrong(self):
        pj = self._make_pending()
        self.assertIsNotNone(pj)
        self.assertEqual(PendingJudgment.objects.filter(
            word=self.word, kind='meaning', resolved=False).count(), 1)
        # 未复核前不写进度、不写学习记录、今日不计错
        self.assertFalse(StudyProgress.objects.filter(word=self.word).exists())
        self.assertFalse(StudyRecord.objects.exists())
        checkin, _ = DailyCheckIn.objects.get_or_create(date=timezone.localdate())
        self.assertEqual(checkin.today_wrong, 0)
        self.assertEqual(checkin.today_correct, 0)

    # --- 2. 页面重复提交同一 pending 题：不产生重复记录 ---
    def test_duplicate_submit_single_pending_record(self):
        self._make_pending()
        self._make_pending()
        self.assertEqual(PendingJudgment.objects.filter(
            word=self.word, kind='meaning').count(), 1)

    # --- 3. 人工改判（优先级最高）：立即生效且只写一次 ---
    def test_manual_verdict_writes_once(self):
        self._make_pending()
        pj = PendingJudgment.objects.get(word=self.word)
        first = self._post('words:api_pending_judgment_manual',
                           {'verdict': 'wrong'}, pj_id=pj.id)
        self.assertTrue(first.json()['success'])
        second = self._post('words:api_pending_judgment_manual',
                            {'verdict': 'wrong'}, pj_id=pj.id)
        self.assertTrue(second.json()['success'])
        progress = StudyProgress.objects.get(word=self.word)
        self.assertEqual(progress.error_count, 1)
        self.assertEqual(StudyRecord.objects.filter(
            word=self.word, action='meaning_wrong').count(), 1)
        pj.refresh_from_db()
        self.assertTrue(pj.resolved)
        self.assertTrue(pj.applied)
        self.assertEqual(pj.final_verdict, 'wrong')
        self.assertEqual(pj.resolved_by, 'manual')

    # --- 4. 连接恢复后补判：幂等只落库一次 ---
    def test_rejudge_idempotent(self):
        self._make_pending()
        pj = PendingJudgment.objects.get(word=self.word)
        with patch('words.views._ai_post_json',
                   return_value='{"verdict": "correct", "comment": "与标准释义一致"}'):
            res = self._post('words:api_pending_judgment_rejudge', {}, pj_id=pj.id)
            self.assertTrue(res.json()['success'])
            self.assertEqual(res.json()['result'], 'correct')
            # 再次补判：已复核，直接返回，不重复写学习记录
            res2 = self._post('words:api_pending_judgment_rejudge', {}, pj_id=pj.id)
        self.assertTrue(res2.json().get('idempotent'))
        progress = StudyProgress.objects.get(word=self.word)
        self.assertEqual(progress.status, 'mastered')
        self.assertEqual(progress.meaning_correct, 1)
        self.assertEqual(StudyRecord.objects.filter(
            word=self.word, action='meaning_ok').count(), 1)
        self.assertEqual(PendingJudgment.objects.filter(resolved=False).count(), 0)

    # --- 5. 熔断期间补判：本地兜底判错，不再保持待复核 ---
    def test_rejudge_local_fallback_when_circuit_open(self):
        self._make_pending()
        pj = PendingJudgment.objects.get(word=self.word)
        with patch('words.views._ai_judge_call',
                   return_value=(None, '', 'ai_circuit_open')):
            res = self._post('words:api_pending_judgment_rejudge', {}, pj_id=pj.id)
        body = res.json()
        self.assertTrue(body['success'])
        self.assertEqual(body['result'], 'wrong')
        pj.refresh_from_db()
        self.assertTrue(pj.resolved)
        self.assertEqual(pj.final_verdict, 'wrong')

    # --- 6. 列表接口 + 批量补判 ---
    def test_list_and_rejudge_all(self):
        self._make_pending()
        listing = self.client.get(reverse('words:api_pending_judgments')).json()
        self.assertEqual(listing['count'], 1)
        self.assertEqual(listing['items'][0]['kind'], 'meaning')
        self.assertNotIn('api_key', json.dumps(listing))

        with patch('words.views._ai_post_json',
                   return_value='{"verdict": "wrong", "comment": "释义不符"}'):
            res = self._post('words:api_pending_judgment_rejudge_all', {})
        body = res.json()
        self.assertEqual(body['resolved'], 1)
        self.assertEqual(body['remaining'], 0)
        self.assertEqual(StudyRecord.objects.filter(
            word=self.word, action='meaning_wrong').count(), 1)

    # --- 7. 备份往返：待复核导出/恢复；健康状态不导出 ---
    def test_backup_roundtrip_pending_without_health(self):
        self._make_pending()
        AIModelHealth.objects.get_or_create(model=self.model)
        payload = _build_backup_payload(UserSettings.get_settings())
        self.assertIn('pending_judgments', payload)
        self.assertEqual(len(payload['pending_judgments']), 1)
        self.assertEqual(payload['pending_judgments'][0]['word'], self.word.word)
        # 健康状态是运行时数据，刻意不随备份导出
        self.assertEqual([k for k in payload if 'health' in k.lower()], [])

        PendingJudgment.objects.all().delete()
        res = self.client.post(reverse('words:api_restore'),
                               data=json.dumps(payload), content_type='application/json')
        self.assertEqual(res.status_code, 200)
        restored = PendingJudgment.objects.get()
        self.assertEqual(restored.word, self.word)
        self.assertEqual(restored.kind, 'meaning')
        self.assertEqual(restored.reason_code, 'ai_network_error')
        self.assertFalse(restored.resolved)


class ExamPendingReviewTests(TestCase):
    """HANDOVER 第八节：模拟考中的待复核题不计对错、不写进度，交卷后进待复核队列。"""

    def setUp(self):
        self.unit = Unit.objects.create(number=1, name='测试单元', category='required')
        self.word = Word.objects.create(
            word='abandon', category='required', unit=self.unit, list_number=1,
            meanings='["放弃；抛弃"]')
        AIModel.objects.create(provider='deepseek', model_id='deepseek-chat',
                               base_url='https://api.deepseek.com/v1',
                               api_key='sk-test', enabled=True)

    def test_exam_pending_not_counted_wrong(self):
        session = self.client.session
        session['exam_questions'] = [{'id': self.word.id, 'type': 'meaning'}]
        session['exam_judgments'] = {str(self.word.id): {
            'answer': '坚持', 'verdict': 'pending_review',
            'comment': '已保存，待 AI 可用后复核', 'source': 'pending',
            'reason_code': 'ai_circuit_open', 'is_final': False}}
        session['exam_direction'] = 'meaning'
        session.save()

        res = self.client.post(
            reverse('words:api_exam_submit'),
            data=json.dumps({'answers': [{'word_id': self.word.id, 'answer': '坚持'}]}),
            content_type='application/json')
        body = res.json()
        self.assertTrue(body['success'])
        self.assertEqual(body['pending_count'], 1)
        self.assertEqual(body['correct_count'], 0)
        self.assertEqual(body['wrong_count'], 0)
        self.assertEqual(body['correct_rate'], 0)
        self.assertEqual(body['meaning_judge_stats']['pending'], 1)
        self.assertTrue(body['results'][0]['pending'])
        self.assertFalse(body['results'][0]['correct'])
        # 不写进度、不写学习记录、今日不计错
        self.assertFalse(StudyProgress.objects.filter(word=self.word).exists())
        self.assertFalse(StudyRecord.objects.exists())
        checkin = DailyCheckIn.objects.get(date=timezone.localdate())
        self.assertEqual(checkin.today_wrong, 0)
        self.assertEqual(checkin.today_correct, 0)
        self.assertEqual(PendingJudgment.objects.filter(
            word=self.word, kind='exam_meaning', resolved=False).count(), 1)


class _FakeHTTPResponse:
    """urlopen 返回值替身：注入任意响应体，用于验证畸形 2xx 的分类。"""

    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class AIApiCallReviewTests(TestCase):
    """HANDOVER 第十一节：AI API 调用审查修复验收。

    P1-1 畸形 2xx 响应收口为 invalid（判定降级 pending_review、探活可读 502、生成记失败）；
    P1-2 生成调用同样走熔断门禁，手动探活可显式绕过并清除熔断；
    P1-3 待复核去重有数据库部分唯一约束，并发 / 重发只留一条未复核；
    P2-1 invalid 不计入可用性失败，不误触发熔断。"""

    def setUp(self):
        self.unit = Unit.objects.create(number=1, name='测试单元', category='required')
        self.word = Word.objects.create(
            word='abandon', category='required', unit=self.unit, list_number=1,
            meanings='["放弃；抛弃"]')
        self.model = AIModel.objects.create(
            provider='deepseek', model_id='deepseek-chat',
            base_url='https://api.deepseek.com/v1', api_key='sk-test', enabled=True)

    def _cfg(self):
        return {'id': self.model.id, 'model_id': self.model.model_id,
                'base_url': self.model.base_url, 'endpoint': '', 'api_key': 'sk-test'}

    def _post(self, url_name, body, **kwargs):
        return self.client.post(
            reverse(url_name, kwargs=kwargs or None),
            data=json.dumps(body), content_type='application/json')

    def _ok_body(self, content):
        return json.dumps({'choices': [{'message': {'content': content}}]}).encode('utf-8')

    def _health_status(self):
        """按主键重新取模型再取健康状态：避免实例上的 related 对象缓存读到旧值。"""
        return _ai_health_status(AIModel.objects.get(pk=self.model.pk))

    # --- P1-1：畸形 2xx 不得冲出判定策略，也不得 500（本地兜底判错）---
    def test_malformed_2xx_body_classified_invalid_without_500(self):
        bad_bodies = [
            ('html', b'<html><body>502 Bad Gateway</body></html>'),
            ('not_object', b'[]'),
            ('no_choices', b'{}'),
            ('empty_choices', b'{"choices": []}'),
            ('null_choices', b'{"choices": null}'),
            ('null_first', b'{"choices": [null]}'),
            ('no_message', b'{"choices": [{}]}'),
            ('no_content', b'{"choices": [{"message": {}}]}'),
            ('empty_content', b'{"choices": [{"message": {"content": "   "}}]}'),
        ]
        for name, body in bad_bodies:
            with self.subTest(name=name):
                with patch('words.views.urllib.request.urlopen',
                           return_value=_FakeHTTPResponse(body)):
                    policy = judge_with_policy(self.word, '坚持', {'model_id': self.model.id})
                self.assertEqual(policy['verdict'], 'wrong')
                self.assertEqual(policy['source'], 'local_fallback')
                self.assertTrue(policy['is_final'])
        self.assertFalse(StudyRecord.objects.exists())
        self.assertFalse(StudyProgress.objects.filter(error_count__gt=0).exists())

    def test_malformed_2xx_body_raises_invalid_category(self):
        for body in (b'<html>', b'{}', b'{"choices": null}'):
            with self.subTest(body=body):
                with patch('words.views.urllib.request.urlopen',
                           return_value=_FakeHTTPResponse(body)):
                    with self.assertRaises(_AIRequestError) as ctx:
                        _ai_post_json(self._cfg(), {'model': 'm'}, 8)
                self.assertEqual(ctx.exception.category, 'invalid')

    def test_exam_judge_malformed_returns_wrong_not_500(self):
        session = self.client.session
        session['exam_questions'] = [{'id': self.word.id, 'type': 'meaning'}]
        session.save()
        with patch('words.views.urllib.request.urlopen',
                   return_value=_FakeHTTPResponse(b'<html>502</html>')):
            res = self._post('words:api_exam_judge_meaning',
                             {'word_id': self.word.id, 'answer': '坚持'})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body['success'])
        self.assertEqual(body['verdict'], 'wrong')

    # --- P1-1：探活畸形响应返回可读 502（不是 500） ---
    def test_ai_test_malformed_response_returns_502(self):
        with patch('words.views.urllib.request.urlopen',
                   return_value=_FakeHTTPResponse(b'not json at all')):
            res = self._post('words:api_ai_test', {'model_id': self.model.id})
        self.assertEqual(res.status_code, 502)
        self.assertIn('接口响应异常', res.json()['error'])
        health = AIModelHealth.objects.get(model=self.model)
        self.assertEqual(health.last_failure_category, 'invalid')
        self.assertIsNone(health.circuit_open_until)

    def test_ai_test_allows_structural_ok_empty_content_ping(self):
        # max_tokens=1 的探活允许 content 为空，只要响应结构合法
        body = json.dumps({'choices': [{'message': {'content': ''}}]}).encode('utf-8')
        with patch('words.views.urllib.request.urlopen',
                   return_value=_FakeHTTPResponse(body)):
            res = self._post('words:api_ai_test', {'model_id': self.model.id})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()['success'])

    # --- P1-1：生成调用空内容 / 非法响应必须记失败 ---
    def test_generation_malformed_or_empty_records_failure(self):
        for body in (b'<html>', b'{}', b'{"choices": [{"message": {"content": "  "}}]}'):
            with self.subTest(body=body):
                with patch('words.views.urllib.request.urlopen',
                           return_value=_FakeHTTPResponse(body)):
                    content, err = builtin_ai_exam_call('提示词', data={'model_id': self.model.id})
                self.assertIsNone(content)
                self.assertTrue(err)
        self.assertFalse(AICallLog.objects.filter(success=True).exists())
        self.assertGreaterEqual(AICallLog.objects.filter(success=False).count(), 3)

    # --- P1-2：生成调用走熔断门禁；手动探活可绕过并清除熔断 ---
    def test_circuit_blocks_generation_and_manual_probe_clears_it(self):
        for _ in range(AI_HEALTH_FAIL_THRESHOLD):
            with patch('words.views._ai_post_json',
                       side_effect=_AIRequestError('network', '断网')):
                judge_with_policy(self.word, '坚持', {'model_id': self.model.id})
        self.assertIsNotNone(AIModelHealth.objects.get(model=self.model).circuit_open_until)

        # 生成调用 / 后台生成调用都不再出网，快速返回可读冷却错误
        with patch('words.views.urllib.request.urlopen',
                   side_effect=AssertionError('熔断期不应出网')):
            content, err = builtin_ai_exam_call('提示词', data={'model_id': self.model.id})
            self.assertIsNone(content)
            self.assertIn('ai_circuit_open', err)
            with self.assertRaises(_AIRequestError) as ctx:
                _ai_chat_once(self._cfg(), '提示词')
            self.assertEqual(ctx.exception.category, 'circuit')

        # 手动探活是显式连接测试：绕过门禁，成功即清除熔断
        with patch('words.views.urllib.request.urlopen',
                   return_value=_FakeHTTPResponse(self._ok_body('pong'))):
            res = self._post('words:api_ai_test', {'model_id': self.model.id})
        self.assertEqual(res.status_code, 200)
        health = AIModelHealth.objects.get(model=self.model)
        self.assertIsNone(health.circuit_open_until)
        self.assertEqual(health.fail_streak, 0)

        # 熔断清除后生成调用恢复出网
        with patch('words.views.urllib.request.urlopen',
                   return_value=_FakeHTTPResponse(self._ok_body('恢复可用'))):
            content2, err2 = builtin_ai_exam_call('提示词', data={'model_id': self.model.id})
        self.assertEqual(content2, '恢复可用')
        self.assertIsNone(err2)

    # --- P2-1：invalid 只记输出质量问题，不误触发熔断 ---
    def test_repeated_invalid_output_does_not_open_circuit(self):
        for _ in range(AI_HEALTH_FAIL_THRESHOLD + 2):
            with patch('words.views.urllib.request.urlopen',
                       return_value=_FakeHTTPResponse(b'not json')):
                policy = judge_with_policy(self.word, '坚持', {'model_id': self.model.id})
            self.assertEqual(policy['verdict'], 'wrong')
            self.assertEqual(policy['source'], 'local_fallback')
        health = AIModelHealth.objects.get(model=self.model)
        self.assertIsNone(health.circuit_open_until)
        self.assertEqual(health.fail_streak, 0)
        self.assertEqual(health.last_failure_category, 'invalid')
        self.assertEqual(self._health_status()['state'], 'output_error')
        self.assertEqual(self._health_status()['label'], '输出异常')

        # 连接其实可用：随后一次合法响应即可正常判定（未被熔断挡住）
        with patch('words.views.urllib.request.urlopen',
                   return_value=_FakeHTTPResponse(
                       self._ok_body('{"verdict": "wrong", "comment": "释义不符"}'))):
            policy = judge_with_policy(self.word, '坚持', {'model_id': self.model.id})
        self.assertEqual(policy['verdict'], 'wrong')
        self.assertEqual(policy['source'], 'ai')
        self.assertEqual(self._health_status()['state'], 'available')

    def test_network_failures_still_open_circuit(self):
        for _ in range(AI_HEALTH_FAIL_THRESHOLD):
            with patch('words.views._ai_post_json',
                       side_effect=_AIRequestError('timeout', '超时')):
                judge_with_policy(self.word, '坚持', {'model_id': self.model.id})
        health = AIModelHealth.objects.get(model=self.model)
        self.assertIsNotNone(health.circuit_open_until)
        self.assertGreaterEqual(health.fail_streak, AI_HEALTH_FAIL_THRESHOLD)
        self.assertEqual(self._health_status()['state'], 'cooling')

    # --- P1-3：待复核去重的数据库部分唯一约束 ---
    def test_open_pending_rows_are_unique_per_key(self):
        key = 'meaning:learn:meaning:%d:abcdef0123456789' % self.word.id
        PendingJudgment.objects.create(
            word=self.word, kind='meaning', dedupe_key=key, user_answer='第一次')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PendingJudgment.objects.create(
                    word=self.word, kind='meaning', dedupe_key=key, user_answer='第二次')
        self.assertEqual(PendingJudgment.objects.filter(resolved=False).count(), 1)

    def test_resolved_row_frees_dedupe_key(self):
        # 直接测试待复核去重机制：resolved 后同 key 可新建
        key = 'meaning:learn:meaning:%d:abcdef0123456789' % self.word.id
        pj = PendingJudgment.objects.create(
            word=self.word, kind='meaning', dedupe_key=key, user_answer='第一次')
        self.assertTrue(pj.dedupe_key)

        # 人工改判 → 该记录关闭
        manual = self._post('words:api_pending_judgment_manual',
                            {'verdict': 'wrong'}, pj_id=pj.id)
        self.assertTrue(manual.json()['success'])
        pj.refresh_from_db()
        self.assertTrue(pj.resolved)

        # resolved 后再次同 key 创建不会被唯一约束挡住
        PendingJudgment.objects.create(
            word=self.word, kind='meaning', dedupe_key=key, user_answer='第二次')
        self.assertEqual(PendingJudgment.objects.filter(resolved=False).count(), 1)
        self.assertEqual(PendingJudgment.objects.count(), 2)

    def test_duplicate_resend_reuses_same_open_record(self):
        # 直接测试 _get_or_create_pending_judgment 的幂等去重
        key = 'meaning:learn:meaning:%d:abcdef0123456789' % self.word.id
        first = _get_or_create_pending_judgment(
            self.word, 'meaning', '坚持', mode='meaning', source='learn',
            reason_code='ai_network_error', dedupe_key=key)
        second = _get_or_create_pending_judgment(
            self.word, 'meaning', '坚持', mode='meaning', source='learn',
            reason_code='ai_network_error', dedupe_key=key)
        self.assertEqual(first.id, second.id)
        self.assertEqual(PendingJudgment.objects.count(), 1)

        # 最终只写一次学习记录
        self._post('words:api_pending_judgment_manual', {'verdict': 'wrong'}, pj_id=first.id)
        self._post('words:api_pending_judgment_manual', {'verdict': 'wrong'}, pj_id=first.id)
        self.assertEqual(StudyRecord.objects.filter(action='meaning_wrong').count(), 1)


class UpdaterCoreTests(SimpleTestCase):
    """自动更新核心逻辑（不触网、不启动真实进程）"""

    def test_version_compare(self):
        import updater
        self.assertEqual(updater.parse_version('v3.4.5'), (3, 4, 5))
        self.assertEqual(updater.parse_version('3.4'), None)
        self.assertEqual(updater.compare_versions('v3.4.5', '3.4.1'), 1)
        self.assertEqual(updater.compare_versions('v3.4.1', 'v3.4.5'), -1)
        self.assertEqual(updater.compare_versions('v3.4.5', '3.4.5'), 0)
        self.assertEqual(updater.compare_versions('unknown', '3.4.5'), 0)

    def test_tag_validation_blocks_url_injection(self):
        import updater
        self.assertEqual(updater.normalize_tag(' v3.4.5 '), 'v3.4.5')
        for bad in ('', 'latest', 'v3.4', '../../evil', 'v3.4.5/../../x'):
            with self.assertRaises(updater.UpdateError):
                updater.normalize_tag(bad)

    def test_asset_urls_only_official_repo(self):
        import updater
        exe_url, sha_url = updater.asset_urls('v3.4.5')
        self.assertEqual(
            exe_url,
            'https://github.com/1826548016-cloud/Lexizx/releases/download/v3.4.5/word.exe')
        self.assertTrue(sha_url.endswith('/word.exe.sha256'))

    def test_sha_parsing_and_verify(self):
        import hashlib
        import os
        import tempfile
        import updater
        digest = hashlib.sha256(b'abc').hexdigest()
        self.assertEqual(updater.parse_sha_text(digest + '  word.exe'), digest)
        self.assertIsNone(updater.parse_sha_text('not-a-hash'))
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b'abc')
            name = f.name
        try:
            self.assertTrue(updater.verify_sha256(name, digest))
            self.assertFalse(updater.verify_sha256(name, '0' * 64))
            self.assertFalse(updater.verify_sha256(name, ''))
        finally:
            os.unlink(name)

    def test_status_roundtrip(self):
        import tempfile
        import updater
        with tempfile.TemporaryDirectory() as d:
            p = f'{d}/last_update.json'
            updater.write_status(p, {'state': 'done', 'message': 'ok'})
            data = updater.read_status(p)
            self.assertEqual(data['state'], 'done')
            self.assertTrue(data.get('at'))

    def test_run_updater_replaces_and_reports(self):
        import os
        import tempfile
        import updater
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, 'word.exe')
            new = os.path.join(d, 'new.exe')
            status = os.path.join(d, 'last_update.json')
            open(target, 'wb').write(b'OLD')
            open(new, 'wb').write(b'NEW-BUILD')

            with patch.object(updater, 'MIN_EXE_BYTES', 1), \
                 patch.object(updater, '_start_detached'), \
                 patch.object(updater, 'wait_for_boot', return_value=True):
                code = updater.run_updater([
                    'word.exe', '--apply-update', '--pid', '0',
                    '--target', target, '--new', new,
                    '--backup', os.path.join(d, 'word.exe.bak'),
                    '--status', status, '--to-version', 'v3.4.6',
                ])

            self.assertEqual(code, 0)
            with open(target, 'rb') as f:
                self.assertEqual(f.read(), b'NEW-BUILD')
            with open(os.path.join(d, 'word.exe.bak'), 'rb') as f:
                self.assertEqual(f.read(), b'OLD')
            data = updater.read_status(status)
            self.assertEqual(data['state'], 'done')
            self.assertEqual(data['to_version'], 'v3.4.6')
            self.assertTrue(os.path.exists(os.path.join(d, 'updater.log')))

    def test_run_updater_keeps_installed_until_boot_confirmed(self):
        import os
        import tempfile
        import updater
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, 'word.exe')
            new = os.path.join(d, 'new.exe')
            status = os.path.join(d, 'last_update.json')
            open(target, 'wb').write(b'OLD')
            open(new, 'wb').write(b'NEW-BUILD')

            with patch.object(updater, 'MIN_EXE_BYTES', 1), \
                 patch.object(updater, '_start_detached'), \
                 patch.object(updater, 'wait_for_boot', return_value=False):
                code = updater.run_updater([
                    'word.exe', '--apply-update', '--pid', '0',
                    '--target', target, '--new', new,
                    '--status', status, '--to-version', 'v3.4.6',
                ])

            self.assertEqual(code, 0)
            # 未收到启动确认时不得宣称更新成功
            self.assertEqual(updater.read_status(status)['state'], 'installed')

    def test_confirm_boot_promotes_installed_to_done(self):
        import tempfile
        import updater
        with tempfile.TemporaryDirectory() as d:
            updater.write_status(
                f'{d}/last_update.json',
                {'state': 'installed', 'to_version': 'v3.4.6'})
            updater.confirm_boot(d, 'v3.4.6')
            data = updater.read_status(f'{d}/last_update.json')
            self.assertEqual(data['state'], 'done')
            self.assertTrue(updater.wait_for_boot(d, 'v3.4.6', timeout=0.1))

    def test_confirm_boot_ignores_version_mismatch(self):
        import tempfile
        import updater
        with tempfile.TemporaryDirectory() as d:
            updater.write_status(
                f'{d}/last_update.json',
                {'state': 'installed', 'to_version': 'v3.4.6'})
            updater.confirm_boot(d, '3.4.5')
            data = updater.read_status(f'{d}/last_update.json')
            self.assertEqual(data['state'], 'installed')

    def test_run_updater_rejects_small_file(self):
        import os
        import tempfile
        import updater
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, 'word.exe')
            new = os.path.join(d, 'new.exe')
            status = os.path.join(d, 'last_update.json')
            open(target, 'wb').write(b'OLD')
            open(new, 'wb').write(b'x')

            code = updater.run_updater([
                'word.exe', '--apply-update', '--pid', '0',
                '--target', target, '--new', new, '--status', status,
            ])
            self.assertEqual(code, 2)
            with open(target, 'rb') as f:
                self.assertEqual(f.read(), b'OLD')
            self.assertEqual(updater.read_status(status)['state'], 'failed')

    def test_run_updater_rolls_back_when_launch_fails(self):
        import os
        import tempfile
        import updater
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, 'word.exe')
            new = os.path.join(d, 'new.exe')
            status = os.path.join(d, 'last_update.json')
            open(target, 'wb').write(b'OLD')
            open(new, 'wb').write(b'NEW-BUILD')

            # 替换成功但启动新版失败 -> 回滚到旧版本
            with patch.object(updater, 'MIN_EXE_BYTES', 1), \
                 patch.object(updater, '_start_detached', side_effect=[RuntimeError('boom'), None]):
                code = updater.run_updater([
                    'word.exe', '--apply-update', '--pid', '0',
                    '--target', target, '--new', new,
                    '--backup', os.path.join(d, 'word.exe.bak'), '--status', status,
                ])

            self.assertEqual(code, 1)
            self.assertEqual(updater.read_status(status)['state'], 'rolled_back')
            with open(target, 'rb') as f:
                self.assertEqual(f.read(), b'OLD')

    def test_run_updater_rollback_restores_file_even_if_relaunch_fails(self):
        """回滚文件成功但旧版拉不起来时，仍应报 rolled_back 而不是回滚失败"""
        import os
        import tempfile
        import updater
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, 'word.exe')
            new = os.path.join(d, 'new.exe')
            status = os.path.join(d, 'last_update.json')
            open(target, 'wb').write(b'OLD')
            open(new, 'wb').write(b'NEW-BUILD')

            with patch.object(updater, 'MIN_EXE_BYTES', 1), \
                 patch.object(updater, '_start_detached',
                              side_effect=[RuntimeError('bad exe'), OSError('denied')]):
                code = updater.run_updater([
                    'word.exe', '--apply-update', '--pid', '0',
                    '--target', target, '--new', new,
                    '--backup', os.path.join(d, 'word.exe.bak'), '--status', status,
                ])

            self.assertEqual(code, 1)
            data = updater.read_status(status)
            self.assertEqual(data['state'], 'rolled_back')
            self.assertIn('手动打开', data['message'])
            with open(target, 'rb') as f:
                self.assertEqual(f.read(), b'OLD')


class UpdateApiTests(TestCase):
    """更新接口：必须 POST-only，且拒绝非法标签"""

    def test_download_requires_post(self):
        self.assertEqual(self.client.get('/api/update/download/').status_code, 405)

    def test_cancel_requires_post(self):
        self.assertEqual(self.client.get('/api/update/download/cancel/').status_code, 405)

    def test_status_is_get(self):
        self.assertEqual(self.client.post('/api/update/download/status/').status_code, 405)

    def test_result_ack_requires_post(self):
        self.assertEqual(self.client.get('/api/update/result/ack/').status_code, 405)

    def test_download_rejects_bad_tag(self):
        resp = self.client.post(
            '/api/update/download/',
            data=json.dumps({'tag': '../../evil'}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()['success'])
