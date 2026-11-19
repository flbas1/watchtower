import pytest

from watchtower import icons


@pytest.fixture
def web_folder(tmp_path, monkeypatch):
    """A throwaway web folder, with icons cached in it as watchtower does."""
    folder = tmp_path / "web"
    folder.mkdir()
    monkeypatch.setattr(icons, "ICON_FOLDER", folder / "icons")
    return folder
