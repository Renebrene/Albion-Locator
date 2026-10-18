import os
import tempfile
from pathlib import Path

# Keep the auth SQLite database out of the project tree during tests.
os.environ.setdefault(
    "ALBION_USERS_DB",
    str(Path(tempfile.mkdtemp(prefix="albion-auth-")) / "users.db"),
)

# Prevent the ignored developer .env from enabling real provider calls in tests.
for _name in ("YANDEX_API_KEY", "YANDEX_SUGGEST_API_KEY", "YANDEX_JS_API_KEY"):
    os.environ[_name] = ""
