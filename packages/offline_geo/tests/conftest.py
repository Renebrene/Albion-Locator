"""Make the local src-layout package importable in direct pytest runs."""
from pathlib import Path
import sys

PACKAGE_SRC = Path(__file__).resolve().parents[1] / "src"
if str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))
