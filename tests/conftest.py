import os
import tempfile
from pathlib import Path

# Keep the auth SQLite database out of the project tree during tests.
os.environ.setdefault(
    "ALBION_USERS_DB",
    str(Path(tempfile.mkdtemp(prefix="albion-auth-")) / "users.db"),
)
