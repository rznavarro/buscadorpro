import pytest
from pydantic import ValidationError

from app.config import PROJECT_ROOT, Settings


def make_settings(**overrides) -> Settings:
    # _env_file=None: los tests no dependen del .env real de la máquina.
    return Settings(_env_file=None, **overrides)


def test_defaults_match_spec():
    settings = make_settings()
    assert settings.default_region == "CL"
    assert settings.maps_language == "es"
    assert settings.search_limit_default == 20
    assert (settings.delay_min_seconds, settings.delay_max_seconds) == (2.0, 6.0)
    assert settings.web_concurrency == 3
    assert settings.reanalyze_after_days == 30
    assert settings.headless is False
    assert settings.browser_use_telemetry is False


def test_environment_variables_override_defaults(monkeypatch):
    monkeypatch.setenv("SEARCH_LIMIT_DEFAULT", "5")
    monkeypatch.setenv("DEFAULT_REGION", "ar")
    monkeypatch.setenv("HEADLESS", "true")
    settings = make_settings()
    assert settings.search_limit_default == 5
    assert settings.default_region == "AR"
    assert settings.headless is True


def test_relative_data_dir_is_resolved_from_project_root():
    settings = make_settings(data_dir="data")
    assert settings.data_dir == PROJECT_ROOT / "data"
    assert settings.db_path == PROJECT_ROOT / "data" / "prospector.db"
    assert settings.communes_file == PROJECT_ROOT / "data" / "comunas.json"
    assert settings.communes_file.exists()


def test_min_delay_cannot_exceed_max_delay():
    with pytest.raises(ValidationError):
        make_settings(delay_min_seconds=7, delay_max_seconds=3)


def test_default_limit_cannot_exceed_max_limit():
    with pytest.raises(ValidationError):
        make_settings(search_limit_default=100, search_limit_max=50)
