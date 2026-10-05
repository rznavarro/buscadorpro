"""Paso de verificación: web + WhatsApp combinando Maps y la web (sin internet)."""

import asyncio

import httpx
import pytest

from app.analyze.collector import WebCollector
from app.config import Settings
from app.db.repository import Repository
from app.db.schema import create_db_engine, init_db
from app.extract.whatsapp import extract_whatsapp
from app.models import Business, WebsiteStatus, WhatsAppConfidence, WhatsAppSource, WhatsAppStatus
from app.pipeline import run_maps_step, run_verify_step
from app.sources.base import MapsListing, MapsPlace, MapsSource

FILLER = "<p>" + "Cerrajería a domicilio en Rancagua, apertura de puertas y cambio de chapas. " * 6 + "</p>"


def page(body: str) -> str:
    return f"<html><head><title>Negocio</title></head><body>{FILLER}{body}</body></html>"


def html(text: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=text, headers={"content-type": "text/html; charset=utf-8"})


@pytest.fixture
def repo() -> Repository:
    engine = create_db_engine(":memory:")
    init_db(engine)
    return Repository(engine)


def maps_business(repo: Repository, name: str, *, website: str | None, phone: str | None, maps_wa: str | None = None) -> Business:
    """Negocio como lo deja el paso de Maps."""
    place = MapsPlace(
        maps_url=f"https://www.google.com/maps/place/{name}",
        name=name,
        phone_raw=phone,
        phone_e164=phone,
        website=website,
        website_kind=None if website is None else "web_propia",
        whatsapp=extract_whatsapp(
            f'<a href="https://wa.me/{maps_wa}">WA</a>' if maps_wa else "", source=WhatsAppSource.MAPS
        ),
    )
    business, _ = repo.upsert_business(place.to_business())
    return business


def verify_search(repo: Repository, routes: dict[str, httpx.Response], businesses: list[Business]) -> list[dict]:
    search = repo.create_search("cerrajeros en Rancagua", 10)
    for position, business in enumerate(businesses, start=1):
        repo.link_search_result(search.id, business.id, position)

    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404))

    async def go():
        settings = Settings(_env_file=None, web_max_internal_pages=2)
        async with WebCollector(settings, transport=httpx.MockTransport(handler), use_browser=False) as collector:
            return await run_verify_step(collector, repo, search.id, concurrency=2)

    return asyncio.run(go())


def test_web_whatsapp_matching_maps_phone_is_high_confidence(repo):
    business = maps_business(repo, "CGC Fenix", website="https://cgc.cl/", phone="+56993557317")
    [row] = verify_search(repo, {"https://cgc.cl/": html(page('<a href="https://wa.me/56993557317">WA</a>'))}, [business])
    stored = repo.get_business(business.id)
    assert stored.website_status == WebsiteStatus.OK
    assert stored.whatsapp_status == WhatsAppStatus.VERIFICADO
    assert stored.whatsapp_url == "https://wa.me/56993557317"
    assert stored.whatsapp_confidence == WhatsAppConfidence.ALTA
    assert row["whatsapp_por_que"] == "Publicado en su web y coincide con el teléfono de la ficha de Maps."
    assert stored.field_sources["website_status"] == "web: la web abre correctamente"


def test_same_number_in_maps_and_web_is_high_confidence(repo):
    business = maps_business(repo, "JT Keys", website="https://jt.cl/", phone=None, maps_wa="56957734621")
    verify_search(repo, {"https://jt.cl/": html(page('<a href="https://wa.me/56957734621">WA</a>'))}, [business])
    stored = repo.get_business(business.id)
    assert stored.whatsapp_confidence == WhatsAppConfidence.ALTA
    assert stored.whatsapp_source == WhatsAppSource.MAPS  # Maps tiene prioridad como fuente principal
    assert {c["source"] for c in stored.whatsapp_candidates} == {"maps", "web"}


def test_single_source_is_medium_and_broken_button_is_flagged(repo):
    business = maps_business(repo, "Gasfiter", website="https://gas.cl/", phone="+56722234567")
    body = '<a href="https://wa.me/56987654321">WA</a><a href="https://wa.me/123">roto</a>'
    verify_search(repo, {"https://gas.cl/": html(page(body))}, [business])
    stored = repo.get_business(business.id)
    assert stored.whatsapp_confidence == WhatsAppConfidence.MEDIA
    assert stored.whatsapp_broken_button is True


def test_down_website_keeps_url_and_records_reason(repo):
    business = maps_business(repo, "Caido", website="https://caido.cl/", phone="+56987654321")
    [row] = verify_search(repo, {"https://caido.cl/": html("error", 500)}, [business])
    stored = repo.get_business(business.id)
    assert stored.website_status == WebsiteStatus.CAIDO
    assert stored.website == "https://caido.cl/"
    assert "500" in stored.field_sources["website_status"]
    assert stored.whatsapp_status == WhatsAppStatus.NO_CONFIRMADO
    assert row["web_motivo"].startswith("La web responde con error 500")


def test_redirect_updates_website_to_final_url(repo):
    business = maps_business(repo, "Redir", website="http://redir.cl/", phone=None)
    routes = {
        "http://redir.cl/": httpx.Response(301, headers={"location": "https://www.redir.cl/"}),
        "https://www.redir.cl/": html(page("<p>Hola</p>")),
    }
    verify_search(repo, routes, [business])
    stored = repo.get_business(business.id)
    assert stored.website == "https://www.redir.cl/"
    assert stored.field_sources["website_maps"] == "http://redir.cl/"


def test_business_without_website_still_gets_confidence(repo):
    business = maps_business(repo, "Solo Maps", website=None, phone="+56957734621", maps_wa="56957734621")
    [row] = verify_search(repo, {}, [business])
    stored = repo.get_business(business.id)
    assert stored.whatsapp_confidence == WhatsAppConfidence.ALTA
    assert row["web_estado"] == "SIN_WEB"


def test_searching_maps_again_keeps_whatsapp_found_on_the_web(repo):
    business = maps_business(repo, "CGC Fenix", website="https://cgc.cl/", phone="+56993557317")
    verify_search(repo, {"https://cgc.cl/": html(page('<a href="https://wa.me/56993557317">WA</a>'))}, [business])

    class SameMaps(MapsSource):
        async def search(self, query, limit):
            return [MapsListing(position=1, name="CGC Fenix", url=business.google_maps_url)]

        async def get_details(self, listing):
            return MapsPlace(
                maps_url=business.google_maps_url, name="CGC Fenix", phone_raw="+56993557317",
                phone_e164="+56993557317", website="https://cgc.cl/", website_kind="web_propia",
                whatsapp=extract_whatsapp("", source=WhatsAppSource.MAPS),
            )

    asyncio.run(run_maps_step(SameMaps(), repo, "cerrajeros en Rancagua", 5))
    stored = repo.get_business(business.id)
    assert stored.whatsapp_status == WhatsAppStatus.VERIFICADO
    assert stored.whatsapp_url == "https://wa.me/56993557317"
    assert stored.whatsapp_confidence == WhatsAppConfidence.ALTA
    assert stored.field_sources["website_status"] == "web: la web abre correctamente"
