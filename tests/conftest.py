import os
import tempfile
from pathlib import Path

# Keep the auth SQLite database out of the project tree during tests.
_test_data_dir = Path(tempfile.mkdtemp(prefix="albion-auth-"))
os.environ.setdefault("ALBION_USERS_DB", str(_test_data_dir / "users.db"))
# Tests must not leave global screenshot settings in the project tree.
os.environ.setdefault("ALBION_SETTINGS_FILE", str(_test_data_dir / "user_settings.json"))

# Prevent the ignored developer .env from enabling real provider calls in tests.
for _name in ("YANDEX_API_KEY", "YANDEX_SUGGEST_API_KEY", "YANDEX_JS_API_KEY"):
    os.environ[_name] = ""
