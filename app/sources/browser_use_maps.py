"""Implementación por defecto de `MapsSource`: Google Maps en el navegador.

Browser Use abre y mantiene la sesión de Chromium. Las acciones predecibles (abrir la
búsqueda, hacer scroll en la lista, abrir cada ficha) se hacen con Playwright sobre ese
mismo Chromium: la API directa de Browser Use ("Actor") está marcada como legacy en su
documentación, y su propio ejemplo recomienda Playwright para acciones precisas.
Python lee el HTML en `maps_parser.py`.

Reglas (sección 2.6 y 9): una sola sesión, pausas aleatorias, límite de resultados y,
ante un captcha o "tráfico inusual", detenerse con `MapsBlockedError`. Nunca se intenta
resolver un captcha.
"""

import asyncio
import random
import re
from collections.abc import AsyncIterator, Callable
from types import TracebackType
from urllib.parse import quote_plus

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.browser.engine import BrowserEngine
from app.config import Settings, get_settings
from app.extract.html_facts import CommuneCatalog, load_communes
from app.extract.whatsapp import extract_whatsapp
from app.logs import log
from app.models import WhatsAppSource
from app.sources.base import MapsBlockedError, MapsListing, MapsPlace, MapsPlaceError, MapsSource
from app.sources.maps_parser import detect_block, feed_reached_end, parse_feed, parse_place

_FEED = 'div[role="feed"]'
_PLACE_NAME = 'div[role="main"] h1'
_MAX_STALE_SCROLLS = 3  # scrolls seguidos sin resultados nuevos antes de darse por terminado
_MAX_EMPTY_PLACES = 2  # fichas vacías seguidas que se consideran bloqueo


class BrowserUseMapsSource(MapsSource):
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        engine: BrowserEngine | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._engine = engine
        self._owns_engine = engine is None
        self._page: Page | None = None  # pestaña con la lista de resultados
        self._detail_page: Page | None = None  # pestaña donde se abren las fichas
        self._empty_places = 0
        self._warned_reviews = False
        self._log = on_progress or (lambda message: None)
        communes_file = self.settings.communes_file
        self.communes: CommuneCatalog | None = load_communes(communes_file) if communes_file.exists() else None

    async def __aenter__(self) -> "BrowserUseMapsSource":
        if self._engine is None:
            self._engine = BrowserEngine(self.settings)
            await self._engine.start()
        self._page = await self._engine.page()
        await self._page.set_viewport_size({"width": 1366, "height": 900})
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._owns_engine and self._engine is not None:
            await self._engine.close()
            self._engine = None

    # --- Utilidades -----------------------------------------------------------------

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Usa 'async with BrowserUseMapsSource() as source:'.")
        return self._page

    async def _details_tab(self) -> Page:
        """Segunda pestaña de la misma sesión: abrir fichas ahí no pierde la posición en la lista."""
        if self._detail_page is None or self._detail_page.is_closed():
            assert self._engine is not None
            self._detail_page = await self._engine.new_page(1366, 900)
        await self._detail_page.bring_to_front()
        return self._detail_page

    @property
    def _timeout_ms(self) -> int:
        return self.settings.maps_timeout_seconds * 1000

    async def _pause(self) -> None:
        """Pausa aleatoria entre acciones, a ritmo humano."""
        await asyncio.sleep(random.uniform(self.settings.delay_min_seconds, self.settings.delay_max_seconds))

    def _with_language(self, url: str) -> str:
        if re.search(r"[?&]hl=", url):
            return url
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}hl={self.settings.maps_language}"

    async def _goto(self, page: Page, url: str) -> None:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
        except PlaywrightTimeoutError as exc:
            raise MapsPlaceError(f"Google Maps no respondió a tiempo ({url[:120]}).") from exc
        await self._handle_consent(page)
        reason = detect_block(page.url)
        if reason:
            raise MapsBlockedError(reason)

    @staticmethod
    async def _handle_consent(page: Page) -> None:
        """Si Google pide aceptar cookies, se rechazan (no es un captcha)."""
        if "consent.google." not in page.url:
            return
        reject = page.get_by_role("button", name=re.compile(r"Rechazar todo|Reject all", re.IGNORECASE))
        if not await reject.count():
            raise MapsBlockedError("Google pidió aceptar cookies y no apareció el botón 'Rechazar todo'.")
        await reject.first.click()
        await page.wait_for_load_state("domcontentloaded")

    @staticmethod
    async def _raise_if_blocked_page(page: Page) -> None:
        """Se llama cuando no apareció el contenido esperado: revisa si es un bloqueo."""
        text = await page.evaluate("() => document.body ? document.body.innerText.slice(0, 5000) : ''")
        reason = detect_block(page.url, text)
        if reason:
            raise MapsBlockedError(reason)

    @staticmethod
    async def _outer_html(page: Page, selector: str) -> str:
        return await page.evaluate(
            "(selector) => { const node = document.querySelector(selector); return node ? node.outerHTML : ''; }",
            selector,
        )

    # --- Búsqueda ---------------------------------------------------------------------

    async def search(self, query: str, limit: int) -> list[MapsListing]:
        listings: list[MapsListing] = []
        async for listing in self.iter_search(query, max_results=limit):
            listings.append(listing)
        return listings

    async def iter_search(self, query: str, max_results: int = 120) -> AsyncIterator[MapsListing]:
        """Entrega los resultados de a uno; solo baja en la lista cuando se necesitan más."""
        region = self.settings.default_region.lower()
        url = f"https://www.google.com/maps/search/{quote_plus(query)}/?hl={self.settings.maps_language}&gl={region}"
        self._log(f"Abriendo Google Maps: {query}")
        await self.page.bring_to_front()
        for attempt in range(self.settings.maps_retries + 1):
            try:
                await self._goto(self.page, url)
                break
            except MapsPlaceError:
                if attempt == self.settings.maps_retries:
                    raise
                self._log("Google Maps tardó en responder; se intenta otra vez.")
                await self._pause()
        try:
            await self.page.wait_for_selector(f"{_FEED}, {_PLACE_NAME}", timeout=self._timeout_ms)
        except PlaywrightTimeoutError:
            await self._raise_if_blocked_page(self.page)
            self._log("Maps no mostró resultados para esta búsqueda.")
            return

        if not await self.page.query_selector(_FEED):
            # Un solo resultado: Maps abre la ficha directamente.
            name = await self.page.inner_text(_PLACE_NAME)
            yield MapsListing(position=1, name=name.strip() or None, url=self.page.url)
            return

        delivered: set[str] = set()
        stale = 0
        while True:
            feed_html = await self._outer_html(self.page, _FEED)
            fresh = [card for card in parse_feed(feed_html, self.settings.default_region) if card.url not in delivered]
            for card in fresh:
                delivered.add(card.url)
                yield MapsListing(
                    position=len(delivered),
                    name=card.name,
                    url=card.url,
                    card_seen=True,
                    website=card.website,
                    phone_e164=card.phone_e164,
                )
                if len(delivered) >= max_results:
                    return
            if feed_reached_end(feed_html):
                self._log("Se llegó al final de la lista de Maps.")
                return
            stale = 0 if fresh else stale + 1
            if stale >= _MAX_STALE_SCROLLS:
                return
            self._log(f"{len(delivered)} resultados vistos; bajando en la lista…")
            await self.page.bring_to_front()
            await self._scroll_feed()
            await self._pause()

    async def _scroll_feed(self) -> None:
        """Rueda del mouse sobre la lista, como una persona."""
        box = await self.page.locator(_FEED).bounding_box()
        if box:
            await self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        for _ in range(random.randint(3, 5)):
            await self.page.mouse.wheel(0, random.randint(500, 900))
            await asyncio.sleep(random.uniform(0.2, 0.6))

    # --- Ficha -----------------------------------------------------------------------

    async def get_details(self, listing: MapsListing) -> MapsPlace:
        """Lee una ficha. Si no carga, se reintenta (`MAPS_RETRIES`); ante un bloqueo, nunca."""
        attempts = self.settings.maps_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return await self._get_details_once(listing, last_attempt=attempt == attempts)
            except MapsPlaceError as exc:
                if attempt == attempts:
                    raise
                log.info("Ficha de Maps sin cargar (%s); se reintenta: %s", exc, listing.url)
                self._log(f"   La ficha no cargó; se intenta otra vez: {listing.name or 'negocio'}")
        raise AssertionError("inalcanzable")

    async def _get_details_once(self, listing: MapsListing, *, last_attempt: bool) -> MapsPlace:
        await self._pause()
        page = await self._details_tab()
        try:
            await self._goto(page, self._with_language(listing.url))
        except PlaywrightError as exc:
            if "closed" not in str(exc):
                raise
            # Se cerró la pestaña de fichas: se abre otra y se reintenta una vez. Si se cerró
            # el navegador completo, abrir la pestaña falla y el error sube con su motivo.
            self._log("Se cerró la pestaña de fichas; se abre otra.")
            self._detail_page = None
            page = await self._details_tab()
            await self._goto(page, self._with_language(listing.url))
        try:
            await page.wait_for_selector(_PLACE_NAME, timeout=self._timeout_ms)
        except PlaywrightTimeoutError as exc:
            await self._raise_if_blocked_page(page)
            if not last_attempt:
                raise MapsPlaceError(f"La ficha no cargó: {listing.name or listing.url}") from exc
            # Se cuenta una vez por negocio (no por intento): dos negocios seguidos vacíos = bloqueo.
            self._empty_places += 1
            if self._empty_places >= _MAX_EMPTY_PLACES:
                raise MapsBlockedError(
                    f"Google devolvió {self._empty_places} fichas vacías seguidas; se detiene para no insistir."
                ) from exc
            raise MapsPlaceError(f"La ficha no cargó: {listing.name or listing.url}") from exc
        self._empty_places = 0

        # Dirección, teléfono y web aparecen un instante después del nombre.
        try:
            await page.wait_for_selector('div[role="main"] [data-item-id]', timeout=5000)
        except PlaywrightTimeoutError:
            pass  # hay fichas sin dirección ni teléfono
        main_html = await self._outer_html(page, 'div[role="main"]')

        about_html = None
        if self.settings.maps_open_about_tab:
            about_tab = page.locator('[role="tab"][aria-label^="Información sobre"]')
            if await about_tab.count():
                await about_tab.first.click()
                await asyncio.sleep(random.uniform(1.5, 3.0))
                about_html = await self._outer_html(page, 'div[role="main"]')

        try:
            place = parse_place(
                main_html,
                url=listing.url,
                region=self.settings.default_region,
                communes=self.communes,
                about_html=about_html,
            )
        except ValueError as exc:
            raise MapsPlaceError(f"No se pudo leer la ficha: {listing.name or listing.url}") from exc

        if self.settings.maps_mobile_retry and not place.whatsapp.candidates:
            place = await self._retry_mobile(listing, place)
        if place.rating is not None and place.review_count is None and self.settings.headless and not self._warned_reviews:
            # Probado el 2026-10-04: sin ventana, Google muestra una "vista limitada" sin el número
            # de reseñas. No se inventa: el campo queda vacío (y no borra el que ya estaba guardado).
            self._warned_reviews = True
            message = (
                "⚠ Sin ventana (HEADLESS=true) Google oculta el número de reseñas. "
                "Para verlas, deja HEADLESS=false en .env."
            )
            log.warning(message)
            self._log(message)
        return place

    async def _retry_mobile(self, listing: MapsListing, place: MapsPlace) -> MapsPlace:
        """Sección 7: si no hubo WhatsApp en escritorio, mira la misma ficha en vista móvil."""
        assert self._engine is not None
        await self._pause()
        mobile = await self._engine.new_mobile_page()
        try:
            try:
                await mobile.goto(self._with_language(listing.url), wait_until="commit", timeout=self._timeout_ms)
            except PlaywrightTimeoutError:
                return place
            await asyncio.sleep(random.uniform(4, 6))
            reason = detect_block(mobile.url)
            if reason:
                raise MapsBlockedError(reason)
            html = await mobile.content()
        finally:
            await mobile.close()
        found = extract_whatsapp(
            html, source=WhatsAppSource.MAPS, page_url=listing.url, region=self.settings.default_region
        )
        if not found.candidates:
            return place
        self._log("WhatsApp encontrado en la vista móvil de Maps.")
        return place.model_copy(update={"whatsapp": found})
