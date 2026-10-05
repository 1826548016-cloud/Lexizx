"""
Django settings for vocab_project project.
"""

import os
import sys
from pathlib import Path

# 应用版本号（检查更新、关于页展示统一引用此处）
APP_VERSION = '3.3.0'

# 软件使用、隐私、知识产权及二次创作声明版本（用发布日期标识）：首次使用需同意，声明修订后老用户需重新确认
PRIVACY_VERSION = '2026-10-04'

# ===== 运行模式检测 =====
# PyInstaller 打包后：sys.frozen=True，sys._MEIPASS 指向解压的只读资源目录
# 开发模式：BASE_DIR 是项目根目录
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    FROZEN = True
    BASE_DIR = Path(sys._MEIPASS)
    # 可写数据目录：exe 同目录的 ./data/（避免 _MEIPASS 临时目录被清空）
    DATA_DIR = Path(sys.executable).resolve().parent / 'data'
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / 'backups').mkdir(exist_ok=True)
else:
    FROZEN = False
    BASE_DIR = Path(__file__).resolve().parent.parent
    DATA_DIR = BASE_DIR

SECRET_KEY = 'django-insecure-%3#v_5wpj1rord)qcqs*s0v0@hfb-y#8=q6k=(oy7ho=4vdxk'

DEBUG = True

ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '::1']

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'words',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]
ROOT_URLCONF = 'vocab_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'words' / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'words.context_processors.privacy',
            ],
        },
    },
]

WSGI_APPLICATION = 'vocab_project.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': DATA_DIR / 'db.sqlite3',
    }
}

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = 'zh-hans'

TIME_ZONE = 'Asia/Shanghai'

USE_I18N = True

USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [
    BASE_DIR / 'words' / 'static',
]

BACKUP_DIR = DATA_DIR / 'backups'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
