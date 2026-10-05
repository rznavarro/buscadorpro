"""Recolección completa con navegador real sobre una web local (sin internet).

Correr con: uv run pytest -m browser
"""

import asyncio
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.analyze.collector import WebCollector
from app.config import Settings
from app.models import WebsiteStatus

pytestmark = pytest.mark.browser

SITE = Path(__file__).parent / "fixtures" / "web_local"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request: object, client_address: object) -> None:
        pass  # el navegador corta conexiones al cerrar pestañas: no es un error de la prueba


@pytest.fixture
def local_site():
    server = _QuietServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(SITE)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()


def test_collect_measures_screenshots_buttons_and_broken_links(local_site, tmp_path):
    async def go():
        async with WebCollector(Settings(_env_file=None, data_dir=tmp_path)) as collector:
            return await collector.collect(local_site)

    collection = asyncio.run(go())
    verification = collection.verification
    assert verification.status == WebsiteStatus.OK
    assert verification.pages_visited == [f"{local_site}contacto/"]
    assert {c.number for c in verification.whatsapp_candidates} == {"56987654321"}
    assert verification.whatsapp_floating is True

    desktop, mobile = collection.desktop, collection.mobile
    for metrics in (desktop, mobile):
        assert (tmp_path / metrics.screenshot).exists()
        assert (tmp_path / metrics.screenshot_full).exists()
        assert metrics.whatsapp_first_screen is True
        assert "Cotiza aquí" in metrics.ctas_first_screen
        assert metrics.fake_ctas_first_screen == ["Llamar ahora"]
        assert metrics.load_ms is not None
    assert desktop.viewport == "1440x900"
    assert mobile.viewport == "390x844"
    assert mobile.horizontal_overflow is True  # el bloque de 1200 px se sale de la pantalla
    assert mobile.zoomed_out_on_mobile is False  # tiene viewport: la página está adaptada
    assert desktop.horizontal_overflow is False

    assert [b.url for b in collection.broken_links] == [f"{local_site}no-existe/"]
    assert [b.url for b in collection.broken_images] == [f"{local_site}no-existe.jpg"]
    assert collection.facts.title == "Cerrajería de Prueba | Rancagua"


def test_page_without_viewport_is_zoomed_out_on_mobile(local_site, tmp_path):
    async def go():
        async with WebCollector(Settings(_env_file=None, data_dir=tmp_path)) as collector:
            return await collector.collect(f"{local_site}contacto/")

    mobile = asyncio.run(go()).mobile
    assert mobile.viewport == "390x844"
    assert mobile.zoomed_out_on_mobile is True
