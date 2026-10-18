"""Test settings that keep Django's test database in memory."""

from .settings import *  # noqa: F403

# Prevent test runs from creating a test_db.sqlite3 beside the user's database.
# 注意：不可整体覆盖 TEST 字典，否则会丢失 Django 注入的 MIRROR 等默认键导致测试报 KeyError。
DATABASES['default'].setdefault('TEST', {})['NAME'] = ':memory:'  # noqa: F405
