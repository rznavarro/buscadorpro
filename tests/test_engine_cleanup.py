from app.browser.engine import _remove_browser_use_temp_dirs


def test_removes_only_browser_use_temp_dirs(tmp_path):
    profile = tmp_path / "browser-use-user-data-dir-abc123"
    downloads = tmp_path / "browser-use-downloads-abc123"
    real_profile = tmp_path / "mi-perfil-de-chrome"
    for folder in (profile, downloads, real_profile):
        folder.mkdir()
        (folder / "archivo.txt").write_text("x")

    _remove_browser_use_temp_dirs([profile, str(downloads), real_profile, None])

    assert not profile.exists()
    assert not downloads.exists()
    assert real_profile.exists()
