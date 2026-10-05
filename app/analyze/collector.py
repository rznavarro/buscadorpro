"""Recolector web (sección 5, pasos 6 y 7, y sección 8.1).

Dos niveles:
- `verify(url)`: lo más importante para Vortexia, y rápido. ¿La web abre de verdad? ¿Cuál es
  su URL final? ¿Qué WhatsApp publica en su inicio, en 2–3 páginas internas (contacto,
  servicios, nosotros) y en su Linktree? Usa httpx; el navegador solo si la web se arma con
  JavaScript, tiene protección anti-bots o un problema de certificado.
- `collect(url)`: verify + navegador real. Tiempos de carga, capturas de escritorio (1440 px)
  y celular (390 px), qué se ve en la primera pantalla, scroll horizontal en celular, enlaces e
  imágenes rotas y PageSpeed (si hay clave en `.env`).
"""

import asyncio
import re
import ssl
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from types import TracebackType
from urllib.parse import urljoin, urlsplit

import httpx
import truststore
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel, Field
from selectolax.lexbor import LexborHTMLParser

from app.analyze.web_status import (
    detect_dead_site,
    http_error_reason,
    is_bot_wall,
    login_wall_reason,
    needs_render,
    pick_internal_pages,
)
from app.browser.engine import BrowserEngine
from app.config import Settings, get_settings
from app.extract.html_facts import CommuneCatalog, HtmlFacts, extract_html_facts, load_communes, visible_text
from app.extract.links import LINK_AGGREGATOR_HOSTS, LinkKind, classify_link, extract_hrefs, normalize_url
from app.extract.socials import extract_socials
from app.extract.whatsapp import WhatsAppExtraction, extract_whatsapp, merge_candidates, resolve_shortlinks
from app.logs import log
from app.models import WebsiteStatus, WhatsAppCandidate, WhatsAppSource

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/153.0.0.0 Safari/537.36"
)
SSL_REASON = (
    "El certificado de seguridad (SSL) es inválido o está vencido: el navegador les muestra una "
    "advertencia a los visitantes"
)
BOT_WALL_REASON = "La web tiene una protección anti-bots y no dejó revisarla"
_MAX_HTML_BYTES = 5_000_000
# Enlaces técnicos que no son parte de la web visible. Algunos son trampas para robots
# (imunify-bot-check): visitarlos puede hacer que el hosting nos bloquee.
_TECHNICAL_LINK = re.compile(
    r"/(?:wp-admin|wp-login\.php|wp-json|xmlrpc\.php|cdn-cgi|imunify[\w-]*|feed|comments/feed)(?:/|$|\?)",
    re.IGNORECASE,
)


# --- Resultados ---------------------------------------------------------------------


class FetchResult(BaseModel):
    url: str
    final_url: str | None = None
    status_code: int | None = None
    redirects: list[str] = Field(default_factory=list)
    content_type: str | None = None
    elapsed_ms: int | None = None
    html: str = Field(default="", exclude=True)
    error: str | None = None  # por qué no se pudo descargar, en palabras simples
    ssl_problem: bool = False  # se descargó ignorando un certificado inválido
    transient: bool = False  # falla que puede ser pasajera (no respondió, servidor caído un momento)
    attempts: int = 1


class RenderResult(BaseModel):
    """Una página abierta en el navegador real."""

    final_url: str
    status_code: int | None = None
    html: str = Field(default="", exclude=True)
    error: str | None = None


class WebVerification(BaseModel):
    """Lo más importante: si la web existe, su URL final y el WhatsApp que publica."""

    url: str
    final_url: str | None = None
    status: WebsiteStatus
    reason: str | None = None  # por qué no está OK, en palabras simples
    http_status: int | None = None
    redirects: list[str] = Field(default_factory=list)
    https: bool = False
    rendered: bool = False  # hubo que abrirla en el navegador
    pages_visited: list[str] = Field(default_factory=list)
    whatsapp_candidates: list[WhatsAppCandidate] = Field(default_factory=list)
    whatsapp_broken: list[str] = Field(default_factory=list)  # botones de WhatsApp que no funcionan
    whatsapp_floating: bool = False
    socials: dict[str, list[str]] = Field(default_factory=dict)
    phones: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    linktree_urls: list[str] = Field(default_factory=list)
    website_from_linktree: str | None = None  # web propia encontrada dentro de su Linktree
    facts: HtmlFacts | None = Field(default=None, exclude=True)


class BrokenLink(BaseModel):
    url: str
    reason: str


class PageMetrics(BaseModel):
    viewport: str
    dom_content_loaded_ms: int | None = None
    load_ms: int | None = None
    requests: int = 0
    weight_kb: int = 0  # aproximado (lo que informa el navegador)
    screenshot: str | None = None  # primera pantalla
    screenshot_full: str | None = None  # página completa, con alto máximo
    whatsapp_first_screen: bool = False
    tel_first_screen: bool = False
    ctas_first_screen: list[str] = Field(default_factory=list)
    # Se ven como botones ("Llamar", "Cotiza") pero no son enlaces: al tocarlos no pasa nada.
    fake_ctas_first_screen: list[str] = Field(default_factory=list)
    horizontal_overflow: bool = False  # en celular: la página se sale de la pantalla hacia el lado
    # En celular la página se ve "alejada", como la versión de computador (no está adaptada).
    zoomed_out_on_mobile: bool = False
    scroll_width: int = 0
    layout_width: int = 0  # ancho con que el navegador dibujó la página


class WebCollection(BaseModel):
    verification: WebVerification
    facts: HtmlFacts | None = None
    desktop: PageMetrics | None = None
    mobile: PageMetrics | None = None
    broken_links: list[BrokenLink] = Field(default_factory=list)
    links_checked: int = 0
    broken_images: list[BrokenLink] = Field(default_factory=list)
    images_checked: int = 0
    psi_mobile_score: int | None = None
    notes: list[str] = Field(default_factory=list)


# --- Utilidades ----------------------------------------------------------------------

_META_CHARSET = re.compile(rb"<meta[^>]+charset=[\"']?([\w-]+)", re.IGNORECASE)


def _decode(response: httpx.Response) -> str:
    """Texto de la respuesta respetando el `<meta charset>` (muchas webs chilenas usan latin-1)."""
    content = response.content[:_MAX_HTML_BYTES]
    encoding = response.charset_encoding
    if not encoding:
        match = _META_CHARSET.search(content[:4096])
        encoding = match.group(1).decode("ascii", "ignore") if match else "utf-8"
    try:
        return content.decode(encoding, errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


DNS_REASON = "El dominio no existe o no tiene la web configurada (sin DNS)"
# Respuestas que pueden ser pasajeras: vale la pena esperar y reintentar una vez.
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504, 520, 521, 522, 523, 524})


def _connect_reason(message: str) -> str:
    lowered = message.lower()
    if any(hint in lowered for hint in ("getaddrinfo", "name or service not known", "nodename", "11001", "no address")):
        return DNS_REASON
    if any(hint in lowered for hint in ("refused", "10061", "deneg", "rechaz")):
        return "El servidor rechaza la conexión"
    return "No se pudo conectar con el servidor de la web"


def _browser_error_reason(message: str) -> str:
    if "ERR_CERT" in message or "SSL" in message:
        return SSL_REASON
    if "ERR_NAME_NOT_RESOLVED" in message:
        return DNS_REASON
    if "ERR_CONNECTION_REFUSED" in message:
        return "El servidor rechaza la conexión"
    if "Timeout" in message or "ERR_TIMED_OUT" in message:
        return "La web no terminó de cargar a tiempo"
    return "El navegador no pudo abrir la web"


def _title(html: str) -> str | None:
    node = LexborHTMLParser(html).css_first("title")
    return node.text(strip=True) if node else None


def _host(url: str | None) -> str:
    return (urlsplit(url or "").hostname or "").lower().removeprefix("www.")


# --- Recolector -------------------------------------------------------------------------


class WebCollector:
    """Uso: `async with WebCollector() as collector: await collector.verify(url)`."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        engine: BrowserEngine | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        renderer: Callable[[str], Awaitable[RenderResult | None]] | None = None,
        use_browser: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self._engine = engine
        self._owns_engine = False
        self._transport = transport  # para tests
        self._renderer = renderer  # para tests: reemplaza al navegador
        self._use_browser = use_browser
        self._browser_lock = asyncio.Lock()
        self.client: httpx.AsyncClient | None = None
        self._insecure_client: httpx.AsyncClient | None = None
        communes_file = self.settings.communes_file
        self.communes: CommuneCatalog | None = load_communes(communes_file) if communes_file.exists() else None

    async def __aenter__(self) -> "WebCollector":
        options = {
            "headers": {"User-Agent": USER_AGENT, "Accept-Language": "es-CL,es;q=0.9,en;q=0.5"},
            "follow_redirects": True,
            "max_redirects": 10,
            "timeout": httpx.Timeout(self.settings.web_timeout_seconds),
        }
        if self._transport is not None:
            self.client = httpx.AsyncClient(transport=self._transport, **options)
            self._insecure_client = self.client
        else:
            # Certificados de Windows, como el navegador: si no, webs válidas saldrían con error SSL.
            self.client = httpx.AsyncClient(verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), **options)
            self._insecure_client = httpx.AsyncClient(verify=False, **options)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        for client in {self.client, self._insecure_client}:
            if client is not None:
                await client.aclose()
        if self._owns_engine and self._engine is not None:
            await self._engine.close()
            self._engine = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self.client is None:
            raise RuntimeError("Usa 'async with WebCollector() as collector:'.")
        return self.client

    async def _browser(self) -> BrowserEngine:
        # Varias revisiones en paralelo piden el navegador a la vez: se abre una sola vez y
        # nadie lo usa antes de que termine de arrancar.
        async with self._browser_lock:
            if self._engine is None:
                engine = BrowserEngine(self.settings, headless=True)
                await engine.start()
                self._engine, self._owns_engine = engine, True
        return self._engine

    # --- Descarga -----------------------------------------------------------------

    async def fetch(self, url: str) -> FetchResult:
        """Descarga la página. Si falla por algo que puede ser pasajero, espera y reintenta.

        No se reintenta lo que no cambia con esperar: un dominio que no existe, un 404, etc.
        """
        result = await self._fetch_once(url)
        while result.transient and result.attempts <= self.settings.web_retries:
            log.info("La web %s falló (%s); se reintenta en %s s", url, result.error or result.status_code,
                     self.settings.web_retry_pause_seconds)
            await asyncio.sleep(self.settings.web_retry_pause_seconds)
            attempts = result.attempts + 1
            result = await self._fetch_once(url)
            result.attempts = attempts
        if result.attempts > 1 and result.error:
            result.error += f" (se intentó {result.attempts} veces)"
        return result

    async def _fetch_once(self, url: str) -> FetchResult:
        start = time.perf_counter()
        try:
            response = await self._http.get(url)
        except httpx.ConnectError as exc:
            message = str(exc)
            if any(hint in message for hint in ("SSL", "CERTIFICATE", "certificate")):
                return await self._fetch_insecure(url, start)
            reason = _connect_reason(message)
            return FetchResult(url=url, error=reason, transient=reason != DNS_REASON)
        except httpx.TimeoutException:
            return FetchResult(
                url=url, error=f"La web no respondió en {self.settings.web_timeout_seconds} segundos", transient=True
            )
        except httpx.TooManyRedirects:
            return FetchResult(url=url, error="La web redirige en círculos (demasiadas redirecciones)")
        except httpx.HTTPError as exc:
            return FetchResult(url=url, error=f"No se pudo abrir la web ({type(exc).__name__})", transient=True)
        result = self._fetch_result(url, response, start)
        result.transient = response.status_code in _TRANSIENT_STATUS
        return result

    async def _fetch_insecure(self, url: str, start: float) -> FetchResult:
        assert self._insecure_client is not None
        try:
            response = await self._insecure_client.get(url)
        except httpx.HTTPError:
            return FetchResult(url=url, error=SSL_REASON, ssl_problem=True)
        result = self._fetch_result(url, response, start)
        result.ssl_problem = True
        return result

    @staticmethod
    def _fetch_result(url: str, response: httpx.Response, start: float) -> FetchResult:
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        is_html = content_type in ("", "text/html", "application/xhtml+xml")
        return FetchResult(
            url=url,
            final_url=str(response.url),
            status_code=response.status_code,
            redirects=[str(r.url) for r in response.history],
            content_type=content_type or None,
            elapsed_ms=round((time.perf_counter() - start) * 1000),
            html=_decode(response) if is_html else "",
        )

    async def render(self, url: str) -> RenderResult | None:
        """Abre la página en el navegador real. None si el navegador no está disponible."""
        if self._renderer is not None:
            return await self._renderer(url)
        if not self._use_browser:
            return None
        engine = await self._browser()
        page = await engine.new_page()
        try:
            response = None
            try:
                response = await page.goto(url, wait_until="load", timeout=self.settings.web_timeout_seconds * 2000)
            except PlaywrightTimeoutError:
                pass  # se usa lo que alcanzó a cargar
            await page.wait_for_timeout(1500)
            return RenderResult(
                final_url=page.url,
                status_code=response.status if response else None,
                html=await page.content(),
            )
        except PlaywrightError as exc:
            return RenderResult(final_url=url, error=_browser_error_reason(str(exc)))
        finally:
            await page.close()

    # --- Verificación (lo más importante) ----------------------------------------------

    async def verify(self, url: str, *, region: str | None = None, _depth: int = 0) -> WebVerification:
        """¿La web existe y abre? ¿Cuál es su URL final? ¿Qué WhatsApp publica?

        `region`: país del negocio ("CL", "US"…), para completar números sin código de país.
        """
        region = region or self.settings.default_region
        url = normalize_url(url) or url
        kind = classify_link(url)
        if kind == LinkKind.AGREGADOR:
            return await self._verify_link_page(url, region, _depth)
        if kind == LinkKind.RED_SOCIAL:
            return WebVerification(
                url=url, status=WebsiteStatus.SOLO_REDES, reason="El enlace es una red social, no una web propia",
                socials=extract_socials([url]),
            )
        if kind == LinkKind.WHATSAPP:
            return WebVerification(url=url, status=WebsiteStatus.SIN_WEB, reason="El enlace es un WhatsApp, no una web")
        if kind != LinkKind.WEB_PROPIA:
            return WebVerification(url=url, status=WebsiteStatus.CAIDO, reason="La dirección de la web no es válida")

        fetch = await self.fetch(url)
        result = WebVerification(
            url=url,
            final_url=fetch.final_url,
            status=WebsiteStatus.OK,
            http_status=fetch.status_code,
            redirects=fetch.redirects,
        )
        html = fetch.html

        if fetch.ssl_problem:
            # httpx no confió en el certificado: el navegador real decide si los visitantes ven una advertencia.
            render = await self.render(url)
            if render is not None and not render.error and render.html:
                html, result.final_url, result.rendered = render.html, render.final_url, True
            else:
                result.status, result.reason = WebsiteStatus.CAIDO, SSL_REASON
                if not html:
                    return result
                # Aunque los visitantes vean una advertencia, el WhatsApp publicado se sigue buscando.
        if fetch.error and not fetch.ssl_problem:
            render = await self.render(url) if "no respondió" in fetch.error else None
            if render is None or render.error or not render.html:
                result.status, result.reason = WebsiteStatus.CAIDO, fetch.error
                return result
            html, result.final_url, result.rendered = render.html, render.final_url, True
        elif fetch.status_code and fetch.status_code >= 400:
            if fetch.status_code in (401, 403, 429, 503) or is_bot_wall(_title(html), html[:5000]):
                render = await self.render(url)
                text = visible_text(LexborHTMLParser(render.html)) if render and render.html else ""
                if render and not render.error and (render.status_code or 200) < 400 and not is_bot_wall(_title(render.html), text):
                    html, result.final_url, result.rendered = render.html, render.final_url, True
                    result.http_status = render.status_code or result.http_status
                else:
                    blocked = render is not None and render.html and is_bot_wall(_title(render.html), text)
                    result.status = WebsiteStatus.BLOQUEADO if blocked or fetch.status_code in (403, 429) else WebsiteStatus.CAIDO
                    result.reason = BOT_WALL_REASON if result.status == WebsiteStatus.BLOQUEADO else http_error_reason(fetch.status_code)
                    return result
            else:
                result.status, result.reason = WebsiteStatus.CAIDO, http_error_reason(fetch.status_code)
                return result
        elif not html:
            result.status = WebsiteStatus.CAIDO
            result.reason = f"La dirección no lleva a una página web (es un archivo {fetch.content_type})"
            return result

        final_url = result.final_url or url
        result.https = final_url.startswith("https://")
        if classify_link(final_url) == LinkKind.RED_SOCIAL:
            result.status, result.reason = WebsiteStatus.SOLO_REDES, "La web redirige a una red social"
            result.socials = extract_socials([final_url])
            return result
        if login_wall_reason(final_url):
            # Ej.: un Google Sites privado. Revisar la página de inicio de sesión sería revisar la web de Google.
            result.status, result.reason = WebsiteStatus.CAIDO, login_wall_reason(final_url)
            return result

        # Webs armadas con JavaScript: el HTML descargado casi no trae texto.
        text = visible_text(LexborHTMLParser(html))
        raw_html = html
        if needs_render(text) and not result.rendered and result.status == WebsiteStatus.OK:
            render = await self.render(final_url)
            if render is not None and not render.error and render.html:
                html, final_url, result.rendered = render.html, render.final_url, True
                result.final_url = final_url
                text = visible_text(LexborHTMLParser(html))
                if login_wall_reason(final_url):
                    result.status, result.reason = WebsiteStatus.CAIDO, login_wall_reason(final_url)
                    return result

        title = _title(html)
        if is_bot_wall(title, text):
            result.status, result.reason = WebsiteStatus.BLOQUEADO, BOT_WALL_REASON
            return result
        dead = detect_dead_site(title, text)
        if dead:
            result.status, result.reason = WebsiteStatus.CAIDO, dead
            return result

        await self._read_content(result, html, raw_html, final_url, region)
        return result

    async def _read_content(
        self, result: WebVerification, html: str, raw_html: str, final_url: str, region: str
    ) -> None:
        """WhatsApp, redes, teléfonos y correos del inicio, de páginas internas y de su Linktree."""
        facts = extract_html_facts(html, page_url=final_url, region=region, communes=self.communes)
        result.facts = facts
        extractions: list[WhatsAppExtraction] = [facts.whatsapp]
        if raw_html is not html:
            # Los plugins guardan el número en scripts que a veces el navegador ya no muestra.
            extractions.append(extract_whatsapp(raw_html, source=WhatsAppSource.WEB, page_url=final_url, region=region))
        hrefs = extract_hrefs(html, base_url=final_url)
        socials = [*hrefs]
        phones, emails = list(facts.phones), list(facts.emails)

        for page_url in pick_internal_pages(facts.internal_links, final_url, self.settings.web_max_internal_pages):
            page_fetch = await self.fetch(page_url)
            page_html = page_fetch.html if not page_fetch.error and (page_fetch.status_code or 500) < 400 else ""
            if page_html and needs_render(visible_text(LexborHTMLParser(page_html))):
                render = await self.render(page_url)
                page_html = render.html if render and not render.error else page_html
            if not page_html:
                continue
            result.pages_visited.append(page_url)
            page_facts = extract_html_facts(page_html, page_url=page_url, region=region)
            extractions.append(page_facts.whatsapp)
            page_hrefs = extract_hrefs(page_html, base_url=page_url)
            hrefs.extend(page_hrefs)
            socials.extend(page_hrefs)
            phones.extend(page_facts.phones)
            emails.extend(page_facts.emails)

        for link in dict.fromkeys(h for h in hrefs if classify_link(h) == LinkKind.AGREGADOR):
            extraction, link_hrefs = await self._read_link_page(link, region)
            if extraction is not None:
                result.linktree_urls.append(link)
                extractions.append(extraction)
                socials.extend(link_hrefs)

        extractions = [await resolve_shortlinks(e, self._http, region=region) for e in extractions]
        result.whatsapp_candidates = merge_candidates(*(e.candidates for e in extractions))
        result.whatsapp_broken = list(dict.fromkeys(b for e in extractions for b in e.broken_evidence()))
        result.whatsapp_floating = any(e.floating_button for e in extractions)
        result.socials = extract_socials(socials)
        result.phones = list(dict.fromkeys(phones))
        result.emails = list(dict.fromkeys(emails))

    async def _read_link_page(self, url: str, region: str) -> tuple[WhatsAppExtraction | None, list[str]]:
        """Página tipo Linktree: su WhatsApp (fuente "linktree") y todos sus enlaces."""
        fetch = await self.fetch(url)
        if fetch.error or not fetch.html or (fetch.status_code or 500) >= 400:
            return None, []
        final_url = fetch.final_url or url
        extraction = extract_whatsapp(
            fetch.html, source=WhatsAppSource.LINKTREE, page_url=final_url, region=region
        )
        return extraction, extract_hrefs(fetch.html, base_url=final_url)

    async def _verify_link_page(self, url: str, region: str, depth: int) -> WebVerification:
        """El "sitio web" es un Linktree: se busca ahí el WhatsApp y, si enlaza una web propia, se verifica."""
        extraction, hrefs = await self._read_link_page(url, region)
        if extraction is None:
            return WebVerification(url=url, status=WebsiteStatus.CAIDO, reason="La página de enlaces (Linktree) no abre")
        extraction = await resolve_shortlinks(extraction, self._http, region=region)
        link_host = _host(url)
        own_site = next(
            (
                h for h in hrefs
                if classify_link(h) == LinkKind.WEB_PROPIA and _host(h) != link_host and _host(h) not in LINK_AGGREGATOR_HOSTS
            ),
            None,
        )
        if own_site and depth == 0:
            inner = await self.verify(own_site, region=region, _depth=1)
            inner.whatsapp_candidates = merge_candidates(inner.whatsapp_candidates, extraction.candidates)
            inner.whatsapp_broken = list(dict.fromkeys([*inner.whatsapp_broken, *extraction.broken_evidence()]))
            inner.linktree_urls.insert(0, url)
            inner.website_from_linktree = own_site
            for network, profiles in extract_socials(hrefs).items():
                inner.socials.setdefault(network, profiles)
            return inner
        return WebVerification(
            url=url,
            final_url=extraction.page_url,
            status=WebsiteStatus.SOLO_REDES,
            reason="Usa una página de enlaces (Linktree o similar) en vez de una web propia",
            whatsapp_candidates=extraction.candidates,
            whatsapp_broken=extraction.broken_evidence(),
            socials=extract_socials(hrefs),
            linktree_urls=[url],
        )

    # --- Recolección completa (navegador real) -----------------------------------------

    async def collect(self, url: str, *, region: str | None = None) -> WebCollection:
        region = region or self.settings.default_region
        verification = await self.verify(url, region=region)
        collection = WebCollection(verification=verification, facts=verification.facts)
        target = verification.final_url
        if verification.status != WebsiteStatus.OK or not target or not self._use_browser:
            return collection

        shots = self.settings.data_dir / "screenshots" / _host(target) / datetime.now().strftime("%Y%m%d-%H%M%S")
        shots.mkdir(parents=True, exist_ok=True)
        desktop_html = ""
        try:
            collection.desktop, desktop_html = await self._measure(target, shots, mobile=False, notes=collection.notes)
            collection.mobile, _ = await self._measure(target, shots, mobile=True, notes=collection.notes)
        except PlaywrightError as exc:
            collection.notes.append(f"No se pudo medir la web en el navegador: {_browser_error_reason(str(exc))}")

        if desktop_html:
            collection.facts = extract_html_facts(desktop_html, page_url=target, region=region, communes=self.communes)
            # Botones de WhatsApp que solo aparecen después de cargar el JavaScript.
            rendered = await resolve_shortlinks(collection.facts.whatsapp, self._http, region=region)
            verification.whatsapp_candidates = merge_candidates(verification.whatsapp_candidates, rendered.candidates)
            verification.whatsapp_floating = verification.whatsapp_floating or rendered.floating_button
            verification.whatsapp_broken = list(dict.fromkeys([*verification.whatsapp_broken, *rendered.broken_evidence()]))

        facts = collection.facts
        if facts is not None:
            links = [link for link in facts.internal_links if not _TECHNICAL_LINK.search(link)]
            links = links[: self.settings.web_max_links_check]
            collection.broken_links = await self.check_links(links)
            collection.links_checked = len(links)
            images = _image_urls(desktop_html or "", target)[: self.settings.web_max_images_check]
            collection.broken_images = await self.check_links(images)
            collection.images_checked = len(images)

        collection.psi_mobile_score = await self.pagespeed(target, collection.notes)
        if collection.psi_mobile_score is None and collection.desktop:
            collection.notes.append("Velocidad medida desde este computador: es referencial (sin PageSpeed).")
        return collection

    async def _measure(self, url: str, shots: Path, *, mobile: bool, notes: list[str]) -> tuple[PageMetrics, str]:
        engine = await self._browser()
        width, height = (390, 844) if mobile else (1440, 900)
        page: Page = await (engine.new_mobile_page(width, height) if mobile else engine.new_page(width, height))
        label = "celular" if mobile else "escritorio"
        requests = 0

        def count_request(_request: object) -> None:
            nonlocal requests
            requests += 1

        page.on("request", count_request)
        try:
            try:
                await page.goto(url, wait_until="load", timeout=30_000)
            except PlaywrightTimeoutError:
                notes.append(f"La web no terminó de cargar en 30 s ({label}); se midió lo que alcanzó a cargar.")
            await page.wait_for_timeout(2500)  # los botones flotantes suelen aparecer después de la carga
            data = await page.evaluate(_PAGE_METRICS_JS)
            first = shots / f"{label}.jpg"
            full = shots / f"{label}-completa.jpg"
            await page.screenshot(path=str(first), type="jpeg", quality=80)
            full_height = min(int(data["scrollHeight"] or 0), self.settings.screenshot_max_height)
            await page.screenshot(
                path=str(full), type="jpeg", quality=70, full_page=True,
                clip={"x": 0, "y": 0, "width": int(data["viewportWidth"]), "height": full_height or height},
            )
            html = await page.content()
        finally:
            await page.close()

        relative = lambda path: str(path.relative_to(self.settings.data_dir)).replace("\\", "/")  # noqa: E731
        metrics = PageMetrics(
            viewport=f"{width}x{height}",
            dom_content_loaded_ms=data["domContentLoadedMs"],
            load_ms=data["loadMs"],
            requests=requests or data["resources"],
            weight_kb=round((data["bytes"] or 0) / 1024),
            screenshot=relative(first),
            screenshot_full=relative(full),
            whatsapp_first_screen=data["whatsappFirstScreen"],
            tel_first_screen=data["telFirstScreen"],
            ctas_first_screen=data["ctasFirstScreen"],
            fake_ctas_first_screen=data["fakeCtasFirstScreen"],
            horizontal_overflow=bool(mobile and data["scrollWidth"] > data["layoutWidth"] + 2),
            zoomed_out_on_mobile=bool(mobile and data["layoutWidth"] > width + 10),
            scroll_width=data["scrollWidth"],
            layout_width=data["layoutWidth"],
        )
        return metrics, html

    async def check_links(self, urls: list[str]) -> list[BrokenLink]:
        """Enlaces o imágenes que responden con error. Primero HEAD; si el servidor no lo acepta, GET."""
        semaphore = asyncio.Semaphore(5)

        async def check_once(url: str) -> tuple[BrokenLink | None, bool]:
            """(enlace roto o None, si la falla puede ser pasajera)."""
            try:
                response = await self._http.head(url)
                if response.status_code >= 400:
                    response = await self._http.get(url)
            except httpx.TimeoutException:
                return BrokenLink(url=url, reason="No respondió a tiempo"), True
            except httpx.HTTPError as exc:
                reason = _connect_reason(str(exc))
                return BrokenLink(url=url, reason=reason), reason != DNS_REASON
            if response.status_code >= 400:
                return BrokenLink(url=url, reason=http_error_reason(response.status_code)), (
                    response.status_code in _TRANSIENT_STATUS
                )
            return None, False

        async def check(url: str) -> BrokenLink | None:
            # Un enlace solo cuenta como roto si sigue fallando después de reintentar.
            async with semaphore:
                broken, transient = await check_once(url)
                for _ in range(self.settings.web_retries):
                    if broken is None or not transient:
                        break
                    await asyncio.sleep(self.settings.web_retry_pause_seconds)
                    broken, transient = await check_once(url)
                return broken

        results = await asyncio.gather(*(check(url) for url in dict.fromkeys(urls)))
        return [broken for broken in results if broken]

    async def pagespeed(self, url: str, notes: list[str]) -> int | None:
        """Puntaje de rendimiento móvil de PageSpeed Insights (0–100), si hay clave en `.env`."""
        key = self.settings.pagespeed_api_key
        if not key:
            return None
        try:
            response = await self._http.get(
                "https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
                params={"url": url, "strategy": "mobile", "category": "performance", "key": key},
                timeout=120,
            )
            response.raise_for_status()
            score = response.json()["lighthouseResult"]["categories"]["performance"]["score"]
            return round(score * 100)
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            notes.append(f"PageSpeed no respondió ({type(exc).__name__}); se usa la medición local.")
            return None


def _image_urls(html: str, base_url: str) -> list[str]:
    urls: dict[str, None] = {}
    for img in LexborHTMLParser(html).css("img"):
        src = (img.attributes.get("src") or img.attributes.get("data-src") or "").strip()
        if not src or src.startswith("data:"):
            continue
        absolute = normalize_url(urljoin(base_url, src))
        if absolute:
            urls.setdefault(absolute, None)
    return list(urls)


_PAGE_METRICS_JS = """() => {
  const nav = performance.getEntriesByType('navigation')[0];
  const resources = performance.getEntriesByType('resource');
  const size = (entry) => entry.transferSize || entry.encodedBodySize || 0;
  const vw = window.innerWidth, vh = window.innerHeight;
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || r.bottom <= 0 || r.top >= vh || r.right <= 0 || r.left >= vw) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') > 0.1;
  };
  const waSelector = 'a[href*="wa.me"], a[href*="whatsapp"], [class*="joinchat"], [class*="whatsapp"], [id*="whatsapp"], [class*="ht-ctc"]';
  // Se compara sin tildes: "Contáctanos", "Cotízanos", "Llámanos", "Solicítalo".
  const ctaPattern = /whats|cotiz|contact|llam|escrib|agend|reserv|presupuesto|solicit|pide|pidel|compr/i;
  const fold = (text) => text.normalize('NFD').replace(/[\\u0300-\\u036f]/g, '');
  const label = (el) => (el.innerText || el.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ');
  const isCta = (el) => ctaPattern.test(fold(label(el)));
  const clickable = (el) =>
    !!el.closest('a[href], button, [role="button"], [onclick], label[for]') || getComputedStyle(el).cursor === 'pointer';
  const painted = (el) => {
    for (let n = el, i = 0; n && i < 3; n = n.parentElement, i++) {
      const s = getComputedStyle(n);
      if (!/rgba\\(0, 0, 0, 0\\)|transparent/.test(s.backgroundColor) || parseFloat(s.borderTopWidth) > 0) return n;
    }
    return null;
  };
  // Texto de llamado a la acción en la primera pantalla (botones reales y "botones" que no son enlace).
  const textual = [...document.querySelectorAll('a, button, [role="button"], [onclick], h1, h2, h3, h4, h5, h6, p, span, div')]
    .filter((el) => el.children.length <= 2 && visible(el) && label(el).length <= 40 && isCta(el));
  const ctas = textual.filter(clickable);
  // Parece un botón (fondo o borde propio, tamaño de botón) pero no lleva a ninguna parte al tocarlo.
  const fakeCtas = textual.filter((el) => {
    if (clickable(el)) return false;
    const box = painted(el);
    if (!box) return false;
    const r = box.getBoundingClientRect();
    return r.height <= 90 && r.width <= 450;
  });
  const doc = document.documentElement;
  return {
    domContentLoadedMs: nav ? Math.round(nav.domContentLoadedEventEnd) : null,
    loadMs: nav && nav.loadEventEnd ? Math.round(nav.loadEventEnd) : null,
    resources: resources.length + 1,
    bytes: resources.reduce((total, entry) => total + size(entry), nav ? size(nav) : 0),
    scrollHeight: Math.max(doc.scrollHeight, document.body ? document.body.scrollHeight : 0),
    scrollWidth: doc.scrollWidth,
    layoutWidth: doc.clientWidth,  // ancho con que se arma la página (sin el zoom del celular)
    viewportWidth: vw,
    viewportHeight: vh,
    whatsappFirstScreen: [...document.querySelectorAll(waSelector)].some(visible),
    telFirstScreen: [...document.querySelectorAll('a[href^="tel:"]')].some(visible),
    ctasFirstScreen: [...new Set(ctas.map((el) => label(el).slice(0, 60)))].slice(0, 10),
    fakeCtasFirstScreen: [...new Set(fakeCtas.map((el) => label(el).slice(0, 60)))]
      .filter((text) => !ctas.some((el) => label(el) === text)).slice(0, 10),
  };
}"""
