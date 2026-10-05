"""Envoltorio del navegador: Browser Use lleva el navegador, Playwright lee el DOM.

Browser Use 0.13 controla Chromium directamente por CDP (ya no usa Playwright por
dentro). Para leer el DOM, tomar capturas y medir tiempos, Playwright se conecta a
ese mismo Chromium con `connect_over_cdp`. El resto del sistema solo habla con
`BrowserEngine`, así un cambio de API de Browser Use se arregla solo aquí.
"""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from playwright.async_api import Browser as PlaywrightBrowser
from playwright.async_api import Page, Playwright, async_playwright

from app.config import Settings, get_settings

if TYPE_CHECKING:
    from browser_use import Browser

# Carpetas temporales que Browser Use crea en cada arranque y no borra al cerrar.
_BROWSER_USE_TEMP_PREFIXES = ("browser-use-user-data-dir-", "browser-use-downloads-")


class BrowserEngine:
    """Una sesión de navegador compartida por Browser Use y Playwright.

    Uso:
        async with BrowserEngine() as engine:
            page = await engine.page()
            await page.goto("https://example.com")
    """

    def __init__(self, settings: Settings | None = None, *, headless: bool | None = None) -> None:
        self.settings = settings or get_settings()
        self.headless = self.settings.headless if headless is None else headless
        self.session: "Browser | None" = None
        self._playwright: Playwright | None = None
        self._pw_browser: PlaywrightBrowser | None = None
        self._downloads_dir: str | None = None

    async def __aenter__(self) -> "BrowserEngine":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def start(self) -> None:
        # Browser Use lee estas variables de entorno al importarse.
        os.environ["ANONYMIZED_TELEMETRY"] = "true" if self.settings.browser_use_telemetry else "false"
        # Sin su propia configuración de logs: solo se ven advertencias y errores.
        os.environ.setdefault("BROWSER_USE_SETUP_LOGGING", "false")
        from browser_use import Browser

        options: dict[str, Any] = {
            "headless": self.headless,
            # El ciclo de vida lo controla este envoltorio, no el agente de Browser Use.
            "keep_alive": True,
            # Sin bloqueador de anuncios ni extensiones de cookies: alterarían lo que
            # vemos de cada web (por ejemplo, ocultando un botón de WhatsApp).
            "enable_default_extensions": False,
            # Regla 6: nunca intentar resolver un captcha.
            "captcha_solver": False,
            # Con Chromium 153+ este argumento por defecto de Browser Use 0.13.10 impide
            # que se abra el puerto CDP y el arranque se cuelga 30 s.
            "ignore_default_args": ["--extensions-on-chrome-urls"],
        }
        # Si no se le da una, Browser Use crea una carpeta de descargas cada vez que
        # valida su configuración, y deja huérfanas las que no usa.
        self._downloads_dir = tempfile.mkdtemp(prefix=_BROWSER_USE_TEMP_PREFIXES[1])
        options["downloads_path"] = self._downloads_dir

        try:
            self._playwright = await async_playwright().start()
            # Por defecto, el mismo Chromium que trae Playwright. Browser Use 0.13.10 no
            # reconoce su carpeta (chrome-win64) y caería en el Chrome del sistema.
            options["executable_path"] = (
                self.settings.browser_executable_path or self._playwright.chromium.executable_path
            )
            self.session = Browser(**options)
            await self.session.start()
            self._pw_browser = await self._playwright.chromium.connect_over_cdp(self.session.cdp_url)
        except BaseException:
            # Si la limpieza también falla, que no tape el error original del arranque.
            try:
                await self.close()
            except Exception:
                pass
            raise

    async def page(self) -> Page:
        """Página de Playwright sobre el Chromium abierto por Browser Use."""
        if self._pw_browser is None:
            raise RuntimeError("El navegador no está iniciado: usa 'async with BrowserEngine()'.")
        contexts = self._pw_browser.contexts
        context = contexts[0] if contexts else await self._pw_browser.new_context()
        return context.pages[0] if context.pages else await context.new_page()

    async def new_page(self, width: int = 1440, height: int = 900) -> Page:
        """Pestaña nueva de escritorio en el mismo Chromium (para analizar varias webs a la vez)."""
        context = (await self.page()).context
        page = await context.new_page()
        await page.set_viewport_size({"width": width, "height": height})
        return page

    async def new_mobile_page(self, width: int = 390, height: int = 844) -> Page:
        """Pestaña nueva que se presenta como un celular Android (vista móvil de una página).

        La emulación se hace por CDP sobre el contexto normal: un contexto nuevo de
        Playwright no carga bien sobre el Chromium que abre Browser Use.
        """
        context = (await self.page()).context
        mobile = await context.new_page()
        cdp = await context.new_cdp_session(mobile)
        major = (self._pw_browser.version if self._pw_browser else "").split(".")[0] or "130"
        user_agent = (
            "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Mobile Safari/537.36"
        )
        await cdp.send("Emulation.setUserAgentOverride", {"userAgent": user_agent, "platform": "Android"})
        await cdp.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 3, "mobile": True},
        )
        await cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": True})
        return mobile

    @property
    def is_connected(self) -> bool:
        return self._pw_browser is not None and self._pw_browser.is_connected()

    async def close(self) -> None:
        """Suelta Playwright y cierra Chromium. Intenta todos los pasos aunque alguno falle."""
        errors: list[BaseException] = []
        # Con connect_over_cdp, close() solo desconecta Playwright; no mata Chromium.
        if self._pw_browser is not None:
            try:
                await self._pw_browser.close()
            except Exception as exc:
                errors.append(exc)
            self._pw_browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:
                errors.append(exc)
            self._playwright = None
        temp_dirs: list[str | Path | None] = [self._downloads_dir, _default_profile_downloads_dir()]
        self._downloads_dir = None
        if self.session is not None:
            temp_dirs.append(self.session.browser_profile.user_data_dir)
            try:
                # kill() cierra el proceso de Chromium aunque keep_alive esté activo.
                await self.session.kill()
                # En Windows, los canales de salida de Chromium se cierran en la siguiente
                # vuelta del event loop; sin esta pausa, Python avisa "unclosed transport" al salir.
                await asyncio.sleep(0.25)
            except Exception as exc:
                errors.append(exc)
            self.session = None
        _remove_browser_use_temp_dirs(temp_dirs)
        if errors:
            raise errors[0]


def _default_profile_downloads_dir() -> str | Path | None:
    """Carpeta de descargas del perfil por defecto que Browser Use crea al importarse (no la usa)."""
    try:
        from browser_use.browser.session import DEFAULT_BROWSER_PROFILE
    except ImportError:
        return None
    return getattr(DEFAULT_BROWSER_PROFILE, "downloads_path", None)


def _remove_browser_use_temp_dirs(paths: list[str | Path | None]) -> None:
    for path in paths:
        if path and Path(path).name.startswith(_BROWSER_USE_TEMP_PREFIXES):
            shutil.rmtree(path, ignore_errors=True)
