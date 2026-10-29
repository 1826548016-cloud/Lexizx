"""
Django settings for vocab_project project.
"""

import os
import secrets
import sys
from pathlib import Path

# 应用版本号（检查更新、关于页展示统一引用此处）
APP_VERSION = '3.4.7'

# 本版更新说明（关于页「版本」卡片下方展示；发布 Release 时同步写入说明）
APP_VERSION_NOTE = '优化更新步骤'

# 软件使用、隐私、知识产权及二次创作声明版本（用发布日期标识）：首次使用需同意，声明修订后老用户需重新确认
PRIVACY_VERSION = '2026-10-28'

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

# 每次安装各自生成一把稳定密钥，优先允许发布环境通过环境变量注入。
# 密钥文件与 SQLite 数据库同属本机用户数据，不进入源码或备份导出。
_secret_key_path = DATA_DIR / '.django_secret'
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '').strip()
if not SECRET_KEY:
    try:
        if _secret_key_path.exists():
            SECRET_KEY = _secret_key_path.read_text(encoding='utf-8').strip()
        if len(SECRET_KEY) < 50:
            SECRET_KEY = secrets.token_urlsafe(50)
            _secret_key_path.write_text(SECRET_KEY, encoding='utf-8')
            try:
                os.chmod(_secret_key_path, 0o600)
            except OSError:
                pass
    except OSError as exc:
        raise RuntimeError('无法创建本机 Django 密钥文件') from exc

# 调试页面仅供源码开发使用；冻结版 exe 必须关闭，避免错误页暴露配置和路径。
DEBUG = not FROZEN

ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '::1']

# 桌面应用仅通过回环 HTTP 提供页面；没有 TLS 时不能设置 Secure，否则会让
# Cookie 完全失效。HttpOnly 与 Strict SameSite 仍可防止脚本读取会话并阻断
# 跨站请求携带 Cookie。
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Strict'
SESSION_COOKIE_SECURE = False
SESSION_COOKIE_AGE = 60 * 60 * 24 * 14  # 本机会话保留 14 天，关闭 exe 后仍有效
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
CSRF_COOKIE_SAMESITE = 'Strict'
CSRF_COOKIE_SECURE = False
CSRF_COOKIE_AGE = 60 * 60 * 24 * 365  # CSRF 防护 Cookie 保留一年
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'

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

DATA_UPLOAD_MAX_MEMORY_SIZE = 200 * 1024 * 1024

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
