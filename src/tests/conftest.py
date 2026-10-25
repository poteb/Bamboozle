import pytest


@pytest.fixture(autouse=True)
def isolated_appdata(tmp_path, monkeypatch):
    """Point LOCALAPPDATA at a temp dir so tests never touch the real config."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return tmp_path
