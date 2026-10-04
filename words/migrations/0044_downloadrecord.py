# -*- coding: utf-8 -*-
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('words', '0043_usersettings_privacy_agreed_version'),
    ]

    operations = [
        migrations.CreateModel(
            name='DownloadRecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(
                    choices=[('backup', '备份文件')],
                    default='backup', max_length=20, verbose_name='类型')),
                ('filename', models.CharField(max_length=200, verbose_name='文件名')),
                ('size', models.BigIntegerField(default=0, verbose_name='文件大小(字节)')),
                ('status', models.CharField(
                    choices=[('pending', '待保存'), ('saved', '已保存'), ('browser', '浏览器下载')],
                    default='pending', max_length=10, verbose_name='状态')),
                ('copy_path', models.CharField(blank=True, default='', max_length=500, verbose_name='本机副本路径')),
                ('saved_path', models.CharField(blank=True, default='', max_length=500, verbose_name='用户保存路径')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
            ],
            options={
                'verbose_name': '下载记录',
                'verbose_name_plural': '下载记录',
                'ordering': ['-created_at'],
            },
        ),
    ]
