import os
import tempfile
from pathlib import Path

# Keep the auth SQLite database out of the project tree during tests.
_test_data_dir = Path(tempfile.mkdtemp(prefix="albion-auth-"))
os.environ.setdefault("ALBION_USERS_DB", str(_test_data_dir / "users.db"))
# Tests must not leave global screenshot settings in the project tree.
os.environ.setdefault("ALBION_SETTINGS_FILE", str(_test_data_dir / "user_settings.json"))
# Тестовые учётки не должны упираться в принудительную смену пароля.
os.environ.setdefault("ALBION_REQUIRE_PASSWORD_CHANGE", "0")
# Provider retries/cache would make unit tests slow and stateful.
os.environ.setdefault("ALBION_UPSTREAM_RETRY_ATTEMPTS", "1")
os.environ.setdefault("ALBION_UPSTREAM_CACHE_TTL", "0")
os.environ.setdefault("ALBION_LOG_LEVEL", "CRITICAL")

# Prevent the ignored developer .env from enabling real provider calls in tests.
for _name in ("YANDEX_API_KEY", "YANDEX_SUGGEST_API_KEY", "YANDEX_JS_API_KEY"):
    os.environ[_name] = ""
