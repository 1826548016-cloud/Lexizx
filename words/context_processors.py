# -*- coding: utf-8 -*-
"""全局模板上下文：隐私声明版本与本机同意状态。

数据只保存在本机 SQLite，上下文处理器不发起任何网络请求。
"""
from django.conf import settings


def privacy(request):
    agreed_version = ''
    agreed_hash = ''
    try:
        from .models import UserSettings
        obj = UserSettings.objects.first()
        if obj:
            agreed_version = obj.privacy_agreed_version or ''
            agreed_hash = obj.privacy_agreed_hash or ''
    except Exception:
        # 数据库未就绪（如 migrate 之前）按未同意处理，不阻断页面渲染
        agreed_version = ''
        agreed_hash = ''

    current = getattr(settings, 'PRIVACY_VERSION', '')
    return {
        'privacy_version': current,
        'privacy_agreed_version': agreed_version,
        'privacy_agreed_hash': agreed_hash,
        'privacy_agreed': bool(agreed_version) and agreed_version == current,
    }
