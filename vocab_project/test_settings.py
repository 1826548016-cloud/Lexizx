"""Test settings that keep Django's test database in memory."""

from .settings import *  # noqa: F403

# Prevent test runs from creating a test_db.sqlite3 beside the user's database.
DATABASES['default']['TEST'] = {'NAME': ':memory:'}  # noqa: F405
