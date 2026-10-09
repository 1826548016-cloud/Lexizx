from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('words', '0047_usersettings_privacy_agreed_hash'),
    ]

    operations = [
        migrations.CreateModel(
            name='DailyPick',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField(db_index=True, verbose_name='日期')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='加入时间')),
                ('word', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                    related_name='daily_picks', to='words.word', verbose_name='单词')),
            ],
            options={
                'verbose_name': '今日自选',
                'verbose_name_plural': '今日自选',
                'ordering': ['date', 'id'],
                'unique_together': {('date', 'word')},
            },
        ),
    ]
