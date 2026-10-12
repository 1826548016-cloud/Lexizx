from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('words', '0051_cet6translationpassage_generatedcet6paper_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='aicalllog',
            name='cet6_practice',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='ai_calls', to='words.cet6translationpractice', verbose_name='关联六级翻译练习'),
        ),
        migrations.AddField(
            model_name='aicalllog',
            name='translation_practice',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='ai_calls', to='words.translationpractice', verbose_name='关联长难句练习'),
        ),
        migrations.AlterField(
            model_name='generatedpaper',
            name='model_id',
            field=models.CharField(blank=True, default='', max_length=120, verbose_name='使用的AI模型'),
        ),
    ]
