import json

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
    TranslationPractice,
    TranslationSentence,
    Unit,
    Word,
)
from .views import _validate_translation_grade_result


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
