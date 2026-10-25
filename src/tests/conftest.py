import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def isolated_appdata(tmp_path, monkeypatch):
    """Point LOCALAPPDATA at a temp dir so tests never touch the real config."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return tmp_path
