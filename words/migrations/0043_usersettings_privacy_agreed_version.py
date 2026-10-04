# -*- coding: utf-8 -*-
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('words', '0042_aimodel_api_key_encrypted_usersettings_vault_salt_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='usersettings',
            name='privacy_agreed_version',
            field=models.CharField(
                blank=True,
                default='',
                max_length=20,
                verbose_name='已同意的隐私声明版本',
            ),
        ),
    ]
