"""Configuración central: lee `.env` y expone los ajustes del proyecto."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # IA (apagada mientras no haya clave: costo $0)
    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "medium"  # low | medium | high | xhigh | max
    pagespeed_api_key: str = ""

    # Google Maps
    default_region: str = "CL"
    maps_language: str = "es"
    search_limit_default: int = Field(20, ge=1)
    search_limit_max: int = Field(50, ge=1)
    delay_min_seconds: float = Field(2.0, ge=0)
    delay_max_seconds: float = Field(6.0, ge=0)
    maps_timeout_seconds: int = Field(20, ge=5)
    maps_retries: int = Field(1, ge=0)  # reintentos si una ficha no carga (nunca ante un captcha)
    maps_open_about_tab: bool = True
    # Sección 7: reintentar la ficha en vista móvil si no apareció un WhatsApp. Apagado por
    # defecto: en las pruebas la versión móvil web de Maps mostró menos enlaces, no más.
    maps_mobile_retry: bool = False

    # Análisis de webs
    web_concurrency: int = Field(3, ge=1)
    reanalyze_after_days: int = Field(30, ge=0)
    web_timeout_seconds: int = Field(15, ge=3)  # espera máxima por cada página que se descarga
    web_retries: int = Field(1, ge=0)  # reintentos si una web falla por algo pasajero
    web_retry_pause_seconds: float = Field(3.0, ge=0)
    # Tiempo máximo total para revisar una web (por si una página nunca termina de cargar).
    web_verify_max_seconds: int = Field(120, ge=10)  # comprobar si abre y buscar su WhatsApp
    web_review_max_seconds: int = Field(300, ge=30)  # revisión a fondo: capturas, mediciones y hallazgos
    web_max_internal_pages: int = Field(3, ge=0)  # páginas internas (contacto, servicios…) donde buscar WhatsApp
    web_max_links_check: int = Field(20, ge=0)  # enlaces internos que se revisan por si están rotos
    web_max_images_check: int = Field(15, ge=0)
    screenshot_max_height: int = Field(6000, ge=900)  # alto máximo de la captura de página completa

    # Navegador
    headless: bool = False
    browser_executable_path: str = ""
    browser_use_telemetry: bool = False

    # Datos
    data_dir: Path = Path("data")
    communes_file: Path = Path("data/comunas.json")

    @model_validator(mode="after")
    def _check_consistency(self) -> "Settings":
        if self.delay_min_seconds > self.delay_max_seconds:
            raise ValueError("DELAY_MIN_SECONDS no puede ser mayor que DELAY_MAX_SECONDS")
        if self.search_limit_default > self.search_limit_max:
            raise ValueError("SEARCH_LIMIT_DEFAULT no puede ser mayor que SEARCH_LIMIT_MAX")
        if not self.data_dir.is_absolute():
            self.data_dir = PROJECT_ROOT / self.data_dir
        if not self.communes_file.is_absolute():
            self.communes_file = PROJECT_ROOT / self.communes_file
        self.default_region = self.default_region.upper()
        return self

    @property
    def db_path(self) -> Path:
        return self.data_dir / "prospector.db"

    @property
    def ai_enabled(self) -> bool:
        """El análisis con IA solo corre si hay clave de Anthropic en `.env`."""
        return bool(self.anthropic_api_key.strip())


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
