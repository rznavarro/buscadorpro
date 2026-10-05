"""Fase 7: reintentos con límite, tiempos máximos, errores en palabras simples y registro del día."""

import asyncio
import socket
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.analyze.collector import WebCollector
from app.config import Settings
from app.db.repository import Repository
from app.db.schema import create_db_engine, init_db
from app.logs import log, log_file, read_today, remove_old_logs, setup_logging
from app.models import Business, WebsiteStatus
from app.pipeline import (
    LeadsProgress,
    ReviewTimeout,
    friendly_error,
    review_website,
    review_website_with_retry,
    review_websites,
    verify_business,
)
from app.sources.base import MapsBlockedError, MapsListing, MapsPlaceError
from app.web.app import create_app

FILLER = "<p>" + "Cerrajería a domicilio en Rancagua, apertura de puertas y cambio de chapas. " * 6 + "</p>"
PAGE = f"<html><head><title>Negocio</title></head><body><h1>Negocio</h1>{FILLER}</body></html>"


@pytest.fixture
def repo() -> Repository:
    engine = create_db_engine(":memory:")
    init_db(engine)
    return Repository(engine)


class Sequence:
    """Transporte simulado que responde, en orden, lo que se le indique (y cuenta las llamadas)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        response = self.responses[min(self.calls, len(self.responses)) - 1]
        if isinstance(response, Exception):
            raise response
        return response


def ok() -> httpx.Response:
    return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})


def collector_for(handler, **settings) -> WebCollector:
    return WebCollector(Settings(_env_file=None, web_max_internal_pages=0, **settings),
                        transport=httpx.MockTransport(handler), use_browser=False)


def fetch(handler, url="https://negocio.cl/", **settings):
    async def go():
        async with collector_for(handler, **settings) as collector:
            return await collector.fetch(url)

    return asyncio.run(go())


# --- Reintentos de webs ------------------------------------------------------------------------


def test_a_web_that_fails_once_is_retried_and_counts_as_working():
    site = Sequence(httpx.Response(503, text="caído un momento"), ok())
    result = fetch(site)
    assert (site.calls, result.status_code, result.attempts) == (2, 200, 2)


def test_a_web_that_never_answers_is_retried_only_once_and_says_so():
    site = Sequence(httpx.ReadTimeout("lenta"))
    result = fetch(site)
    assert site.calls == 2  # 1 intento + 1 reintento (WEB_RETRIES=1)
    assert result.error == "La web no respondió en 15 segundos (se intentó 2 veces)"


@pytest.mark.parametrize(
    "failure",
    [httpx.Response(404, text="no existe"), httpx.ConnectError("[Errno 11001] getaddrinfo failed")],
)
def test_permanent_failures_are_not_retried(failure):
    site = Sequence(failure)
    fetch(site)
    assert site.calls == 1  # un 404 o un dominio que no existe no cambian por esperar


def test_retries_can_be_turned_off():
    site = Sequence(httpx.Response(503, text="caído"))
    fetch(site, web_retries=0)
    assert site.calls == 1


def test_a_link_only_counts_as_broken_if_it_keeps_failing():
    calls: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls[url] = calls.get(url, 0) + 1
        if url.endswith("/pasajero"):
            return httpx.Response(503) if calls[url] <= 2 else httpx.Response(200)  # HEAD+GET fallan, luego abre
        if url.endswith("/roto"):
            return httpx.Response(404)
        return httpx.Response(200)

    async def go():
        async with collector_for(handler) as collector:
            return await collector.check_links(["https://negocio.cl/pasajero", "https://negocio.cl/roto"])

    broken = asyncio.run(go())
    assert [b.url for b in broken] == ["https://negocio.cl/roto"]
    assert calls["https://negocio.cl/roto"] == 2  # HEAD + GET, sin reintento: un 404 es definitivo


# --- Tiempos máximos -------------------------------------------------------------------------------


class SlowCollector(WebCollector):
    async def verify(self, url, *, region=None, _depth=0):
        await asyncio.sleep(5)

    async def collect(self, url, *, region=None):
        await asyncio.sleep(5)


def business_with_web(repo) -> Business:
    business, _ = repo.upsert_business(
        Business(google_maps_url="https://www.google.com/maps/place/lenta", business_name="Lenta", phone_e164="+56993557317",
                 website="https://lenta.cl/", website_status=WebsiteStatus.OK)
    )
    return business


def test_a_web_that_never_finishes_is_cut_and_the_search_continues(repo):
    business = business_with_web(repo)
    messages: list[str] = []

    async def go():
        async with SlowCollector(Settings(_env_file=None), transport=httpx.MockTransport(lambda r: ok())) as collector:
            collector.settings.web_verify_max_seconds = 0.05  # en la vida real: 120 s
            return await verify_business(collector, repo, business, progress=messages.append)

    _, verification, error = asyncio.run(go())
    assert verification is None
    assert error == "La revisión de su web tardó más de 0.05 segundos y se cortó"
    assert "tardó más de" in messages[0]


def test_a_slow_deep_review_is_cut_with_a_clear_message(repo):
    business = business_with_web(repo)
    progress = LeadsProgress(query="x", target=1)

    async def go():
        async with SlowCollector(Settings(_env_file=None), transport=httpx.MockTransport(lambda r: ok())) as collector:
            collector.settings.web_review_max_seconds = 0.05  # en la vida real: 300 s
            with pytest.raises(ReviewTimeout):
                await review_website(collector, repo, business)
            await review_websites(collector, repo, [business], progress)

    asyncio.run(go())
    assert progress.webs_done == 1
    assert any("No se pudo revisar la web de Lenta: La revisión a fondo tardó más de 0.05 segundos" in line for line in progress.log)


def test_a_deep_review_that_crashes_once_is_retried(repo, monkeypatch):
    business = business_with_web(repo)
    attempts: list[int] = []

    async def flaky(collector, repo, business, *, llm=None, refresh_business=False):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Target page, context or browser has been closed")
        return "análisis"

    monkeypatch.setattr("app.pipeline.review_website", flaky)

    async def go():
        async with collector_for(lambda r: ok()) as collector:
            return await review_website_with_retry(collector, repo, business)

    assert asyncio.run(go()) == "análisis"
    assert len(attempts) == 2


def test_a_timed_out_review_is_not_retried(repo, monkeypatch):
    business = business_with_web(repo)
    attempts: list[int] = []

    async def slow(collector, repo, business, *, llm=None, refresh_business=False):
        attempts.append(1)
        raise ReviewTimeout("se cortó")

    monkeypatch.setattr("app.pipeline.review_website", slow)

    async def go():
        async with collector_for(lambda r: ok()) as collector:
            await review_website_with_retry(collector, repo, business)

    with pytest.raises(ReviewTimeout):
        asyncio.run(go())
    assert len(attempts) == 1  # tardaría lo mismo otra vez


# --- Reintentos en Google Maps (sin navegador) ----------------------------------------------------


def maps_source(retries=1):
    from app.sources.browser_use_maps import BrowserUseMapsSource

    return BrowserUseMapsSource(Settings(_env_file=None, maps_retries=retries))


def test_a_maps_place_that_does_not_load_is_retried_once(monkeypatch):
    source = maps_source()
    outcomes = [MapsPlaceError("La ficha no cargó"), "ficha"]
    seen: list[bool] = []

    async def once(listing, *, last_attempt):
        seen.append(last_attempt)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(source, "_get_details_once", once)
    listing = MapsListing(position=1, name="A", url="https://www.google.com/maps/place/A")
    assert asyncio.run(source.get_details(listing)) == "ficha"
    assert seen == [False, True]  # solo el último intento cuenta como ficha vacía


def test_a_captcha_is_never_retried(monkeypatch):
    source = maps_source(retries=3)
    calls: list[int] = []

    async def blocked(listing, *, last_attempt):
        calls.append(1)
        raise MapsBlockedError("captcha")

    monkeypatch.setattr(source, "_get_details_once", blocked)
    with pytest.raises(MapsBlockedError):
        asyncio.run(source.get_details(MapsListing(position=1, url="https://www.google.com/maps/place/A")))
    assert len(calls) == 1  # regla 6: ante un bloqueo se detiene, nunca insiste


# --- Errores en palabras simples ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Page.goto: net::ERR_INTERNET_DISCONNECTED at https://www.google.com/maps", "no hay conexión a internet"),
        ("BrowserType.launch: Executable doesn't exist at C:\\ms-playwright\\chrome.exe", "uv run playwright install chromium"),
        ("Target page, context or browser has been closed", "Se cerró la ventana del navegador"),
        ("algo raro", "RuntimeError: algo raro"),
    ],
)
def test_friendly_errors(message, expected):
    assert expected in friendly_error(RuntimeError(message))


# --- Registro del día -------------------------------------------------------------------------------


@pytest.fixture
def daily_log(tmp_path):
    folder = setup_logging(tmp_path)
    yield tmp_path, folder
    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()


def test_search_messages_and_errors_go_to_the_daily_log(daily_log):
    data_dir, folder = daily_log
    LeadsProgress(query="cerrajeros en Rancagua", target=5).say("✔ Lead 1/5: Cerrajería XYZ")
    try:
        raise ValueError("detalle técnico")
    except ValueError:
        log.exception("No se pudo revisar la web de X")
    text = log_file(data_dir).read_text(encoding="utf-8")
    assert "| INFO    | [cerrajeros en Rancagua] ✔ Lead 1/5: Cerrajería XYZ" in text
    assert "| ERROR   | No se pudo revisar la web de X" in text
    assert "ValueError: detalle técnico" in text  # el detalle técnico queda guardado
    assert log_file(data_dir).parent == folder
    assert read_today(data_dir)[-1] == "ValueError: detalle técnico"


def test_logs_older_than_30_days_are_deleted(tmp_path):
    folder = tmp_path / "logs"
    folder.mkdir()
    today = date(2026, 10, 4)
    old = folder / f"prospector-{(today - timedelta(days=31)).isoformat()}.log"
    recent = folder / f"prospector-{(today - timedelta(days=29)).isoformat()}.log"
    other = folder / "notas.txt"
    for path in (old, recent, other):
        path.write_text("x", encoding="utf-8")
    remove_old_logs(tmp_path, today)
    assert not old.exists() and recent.exists() and other.exists()


def test_log_page_shows_today_newest_first(daily_log, repo):
    data_dir, _ = daily_log
    log.info("primero")
    log.warning("segundo")

    async def no_runner(progress, repo):
        return progress

    with TestClient(create_app(Settings(_env_file=None, data_dir=data_dir), repo=repo, runner=no_runner)) as client:
        html = client.get("/registro").text
    assert html.index("segundo") < html.index("primero")
    assert 'class="sev-media"' in html  # las advertencias se destacan


# --- Abrir el programa dos veces -------------------------------------------------------------------


def test_opening_the_program_twice_reuses_the_open_page(capsys):
    from app.cli import _serve

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert _serve(port, open_browser=False) == 0  # no intenta abrir otro servidor en el mismo puerto
    assert "ya está abierto" in capsys.readouterr().err
