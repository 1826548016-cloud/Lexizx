import json
from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    Cet6TranslationPassage,
    Cet6TranslationPractice,
    DailyCheckIn,
    GeneratedCet6Paper,
    GeneratedCet6Question,
    GeneratedPaper,
    GeneratedQuestion,
    StudyPlan,
    StudyProgress,
    StudyRecord,
    TranslationPractice,
    TranslationSentence,
    Unit,
    UserSettings,
    Word,
)
from .views import _get_today_task, _today_practiced_word_ids, _validate_translation_grade_result


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
    """今日已背 · 重复练习：列表口径、选择校验、自测会话不写正式数据。"""

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

    def test_today_set_dedupes_multiple_records(self):
        self._record(self.words[0], 'known')
        self._record(self.words[0], 'spelling_ok')
        self._record(self.words[0], 'meaning_ok')

        response = self.client.get(reverse('words:today_words'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['word_count'], 1)

    def test_today_words_page_empty_state(self):
        response = self.client.get(reverse('words:today_words'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['word_count'], 0)

    def test_start_rejects_empty_selection(self):
        self._record(self.words[0], 'known')
        response = self.client.post(
            reverse('words:api_today_practice_start'),
            data=json.dumps({'word_ids': []}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_start_filters_ids_outside_today_set(self):
        self._record(self.words[0], 'known')
        # words[1] 今天没背过，混入选择中应被过滤
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
        self._record(self.words[0], 'known')
        response = self.client.post(
            reverse('words:api_today_practice_start'),
            data=json.dumps({'word_ids': [self.words[1].id, 999999]}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_practice_session_requires_valid_session(self):
        # 未从列表启动直接访问 practice=1 → 重定向回今日列表
        response = self.client.get(reverse('words:learn_session') + '?practice=1')
        self.assertRedirects(response, reverse('words:today_words'))

    def test_practice_session_renders_only_validated_words(self):
        self._record(self.words[0], 'known')
        self._record(self.words[1], 'unknown')
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
